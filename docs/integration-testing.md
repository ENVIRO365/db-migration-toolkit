# Integration Testing

This document covers the integration test infrastructure for the DB Migration Toolkit.

## Overview

The integration test harness populates local database containers with real (or embedded) data, then runs the migration engine against them. This validates the full pipeline — adapters, comparators, checkpoint logic, and safety mechanisms — against actual database engines.

```
┌──────────────────────────────────────────────────────────┐
│              Data Source Resolution                        │
│  --source auto → Db2 → PG → embedded (in order)         │
│  --source db2  → WA_TARGET_DSN (dev Db2)                 │
│  --source pg   → WA_SOURCE_DSN (dev PG)                  │
│  --source embedded → built-in seed data                  │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│           Container Backend Selection                      │
│  --use-compose   → ExternalPgContainer / ExternalDb2     │
│  (default)       → Testcontainers (auto-managed)         │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│           Target Selection                                 │
│  (default)     → Both PG + Db2                            │
│  --pg-only     → PostgreSQL only (skip Db2)               │
│  --db2-only    → Db2 only (skip PostgreSQL)               │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│           Schema + Data Population                         │
│  1. Fetch data from source                                │
│  2. Drop all existing tables (clean slate)                │
│  3. Create schema (DDL)                                   │
│  4. Insert fetched rows                                   │
│  5. Verify row counts                                     │
│  6. Print summary report                                  │
└──────────────────────────────────────────────────────────┘
```

> **Clean slate**: The populate script always drops and recreates all tables
> in the selected target database(s) before inserting data. This guarantees
> idempotent runs — no duplicate rows or stale data from previous invocations.
> Only the target(s) specified by `--pg-only` / `--db2-only` are dropped; the
> other database is left untouched.

## Prerequisites

- Docker and Docker Compose v2
- Python 3.12+ with the project venv activated
- `ibm_db` package (for Db2 source fetching)
- `psycopg2-binary` package (for PG source fetching)
- `.env` file with `WA_TARGET_DSN` and/or `WA_SOURCE_DSN` (for live data sources)

## Quick Start

### 1. Start Containers

```bash
docker compose -f docker-compose.testcontainers.yml up -d --wait
```

The `--wait` flag blocks until healthchecks pass. Db2 takes 3-5 minutes on first run.

### 2. Populate Test Data

```bash
# Auto-detect best data source (Db2 → PG → embedded)
python -m tests.integration.testcontainers.populate_test_dbs --use-compose --pg-only

# Explicit: use dev PG as source
python -m tests.integration.testcontainers.populate_test_dbs --use-compose --pg-only --source pg

# Explicit: use embedded seed data (CI/offline)
python -m tests.integration.testcontainers.populate_test_dbs --use-compose --pg-only --source embedded

# Verbose logging
python -m tests.integration.testcontainers.populate_test_dbs --use-compose --pg-only --source auto -v
```

### 3. Run Tests

```bash
pytest tests/integration/testcontainers/
```

### 4. Tear Down

```bash
docker compose -f docker-compose.testcontainers.yml down -v
```

## CLI Flags

| Flag | Description |
|------|-------------|
| `--use-compose` | Connect to docker-compose containers (no lifecycle management) |
| `--pg-only` | Only populate PostgreSQL (skip Db2 target) |
| `--db2-only` | Only populate Db2 (skip PostgreSQL target) |
| `--source <mode>` | Data source: `auto`, `db2`, `pg`, `embedded` |
| `--embedded` | Shorthand for `--source embedded` |
| `-v` / `--verbose` | Enable DEBUG-level logging |

> `--pg-only` and `--db2-only` are mutually exclusive.

## Data Source Resolution

When `--source auto` (the default):

1. **Db2 first** — checks for `DB2_WEALTH_TST_DSN` or `WA_TARGET_DSN` in environment/`.env`. If found, connects via `ibm_db` and fetches all rows from the WEALTHADAPTER schema.
2. **PG fallback** — if Db2 is unavailable, checks for `WA_SOURCE_DSN`. If found, connects via `psycopg2` and fetches from the `wealthadapter` schema (lowercase).
3. **Embedded fallback** — if neither live source is available, uses hardcoded seed dictionaries.

### DSN Format

**Db2 (WA_TARGET_DSN)**:
```
user:password@hostname:port/database
```
Internally parsed to an `ibm_db`-compatible DSN string:
```
DATABASE=...;HOSTNAME=...;PORT=...;PROTOCOL=TCPIP;UID=...;PWD=...
```

**PostgreSQL (WA_SOURCE_DSN)**:
```
postgresql://user:password@hostname:port/dbname?sslmode=require
```

## Container Configuration

### Default Connection Parameters

| Container | Host | Port | Database | User | Password |
|-----------|------|------|----------|------|----------|
| PostgreSQL | localhost | 5433 | wealth_test | testuser | testpassw0rd |
| Db2 | localhost | 50001 | WLTHTEST | db2inst1 | testpassw0rd |

### Environment Variable Overrides

Override any default via environment or `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPOSE_PG_HOST` | localhost | PostgreSQL host |
| `COMPOSE_PG_PORT` | 5433 | PostgreSQL port |
| `COMPOSE_PG_DB` | wealth_test | PostgreSQL database name |
| `COMPOSE_PG_USER` | testuser | PostgreSQL user |
| `COMPOSE_PG_PASSWORD` | testpassw0rd | PostgreSQL password |
| `COMPOSE_DB2_HOST` | localhost | Db2 host |
| `COMPOSE_DB2_PORT` | 50001 | Db2 port |
| `COMPOSE_DB2_DB` | WLTHTEST | Db2 database name |
| `COMPOSE_DB2_USER` | db2inst1 | Db2 user |
| `COMPOSE_DB2_PASSWORD` | testpassw0rd | Db2 password |

## Tables Populated

The following tables are seeded (matching the Wealth Adapter schema):

| Table | Description |
|-------|-------------|
| `userrole` | User role definitions |
| `wealthaccessright` | Access right codes |
| `role_accessright` | Role-to-access-right mappings |
| `adapterconfig` | Adapter configuration entries |
| `emailgroup` | Email notification groups |
| `recipient` | Notification recipients |
| `directorylocation` | Directory location references |

## Architecture

### Module Layout

```
tests/integration/testcontainers/
├── __init__.py
├── conftest.py              # pytest fixtures
├── containers.py            # Container classes (Testcontainer + External)
├── mcp_fetcher.py           # Multi-source data fetching
├── schema_manager.py        # DDL generation and INSERT logic
├── populate_test_dbs.py     # CLI orchestrator
└── test_populated_dbs.py    # Integration tests
```

### Container Classes

| Class | Mode | Description |
|-------|------|-------------|
| `PgTestContainer` | Testcontainers | Auto-managed PG via `testcontainers` library |
| `Db2TestContainer` | Testcontainers | Auto-managed Db2 via `testcontainers` library |
| `ExternalPgContainer` | Docker Compose | Connects to pre-started PG at configurable host:port |
| `ExternalDb2Container` | Docker Compose | Connects to pre-started Db2 at configurable host:port |

All container classes expose a common interface: `get_connection_url()`, `host`, `port`, `username`, `password`, `dbname`.

### Data Fetcher (`mcp_fetcher.py`)

The fetcher module provides `fetch_all_tables(source: str) -> dict[str, list[dict]]`:

- Returns a dict mapping table names to lists of row dicts
- Handles source resolution, connection, and error logging
- Falls through gracefully on connection failure (auto mode)

### Schema Manager (`schema_manager.py`)

Handles DDL creation and data insertion for both PostgreSQL and Db2:

- Creates tables with correct types per engine
- Handles GENERATED ALWAYS identity columns
- Maps Python types to engine-specific SQL types

## Testcontainers vs Docker Compose

| Aspect | Testcontainers | Docker Compose |
|--------|---------------|----------------|
| Startup | Automatic (Python manages container lifecycle) | Manual (`docker compose up`) |
| Speed | Slower (creates fresh container each run) | Faster (containers persist between runs) |
| Data persistence | Ephemeral (destroyed on test end) | Persistent (volumes survive restarts) |
| CI suitability | Good (self-contained) | Good (with `--source embedded`) |
| Debugging | Harder (container gone after test) | Easy (exec into running container) |
| Db2 startup time | 3-5 min each run | Only on first `up` |

**Recommendation**: Use `--use-compose` for daily development. Use Testcontainers for CI where you need guaranteed isolation.

## Troubleshooting

### Db2 Container Won't Start
- Check `docker logs wealth-db2-test` for errors
- Ensure `privileged: true` is set (required for Db2)
- First start takes 3-5 minutes — use `--wait` flag

### "Database WLTHTEST not found"
The `DBNAME` env var doesn't always auto-create the database. Create manually:
```bash
docker exec wealth-db2-test su - db2inst1 -c 'db2 create database WLTHTEST'
```

### Connection Refused on Port 5433
```bash
docker compose -f docker-compose.testcontainers.yml ps
```
Ensure the PG container is running and healthy.

### "ibm_db not installed"
```bash
pip install ibm_db
```
Note: `ibm_db` requires the IBM CLIDRIVER. On Linux it's bundled with the pip package.

### Stale Data After Re-run
This is no longer an issue. The populate script automatically drops and
recreates all tables in the target database(s) before inserting data,
ensuring a clean slate on every run. No manual truncation or volume
removal is required between runs.
