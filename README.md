# DB Migration Toolkit

A generic, profile-driven data migration engine for moving rows between heterogeneous databases — PostgreSQL, DB2, MySQL, Oracle, SQL Server, and SQLite.

## What This Tool Does

The DB Migration Toolkit compares two databases, classifies what needs to change (INSERT, UPDATE, DELETE, or NO_ACTION) per table, and executes the migration in resumable batches with safety guardrails.

It is **not** a schema migration tool. It moves data (DML), not structure (DDL). Your target schema must already exist.

**Key features:**
- Profile-driven — define migrations in YAML, not code
- Pluggable adapters — add new database engines by implementing one Python class
- Resumable — checkpoint after every batch, resume from where you left off
- Safe by default — dry-run mode, DELETE guards, environment checks, circuit breakers
- Observable — structured logging, progress bars, post-run reports

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Set Connection Strings

```bash
export WA_SOURCE_DSN="postgresql://user:pass@host:5432/dbname"
export WA_TARGET_DSN="DATABASE=WEALTH;HOSTNAME=host;PORT=50000;UID=user;PWD=pass"
```

### 3. Compare (Read-Only)

```bash
dbmigrate compare wealth-adapter-rollback
```

This connects to both databases, compares row counts and schemas, and prints a diff report. No data is modified.

### 4. Dry Run

```bash
dbmigrate migrate wealth-adapter-rollback
```

Shows what the migration *would* do — table-by-table operation classification, row counts, and estimated time — without writing anything.

### 5. Execute

```bash
dbmigrate migrate wealth-adapter-rollback --execute
```

Runs the migration. In `supervised` mode (default), you confirm each table before writes begin.

### 6. Validate

```bash
dbmigrate validate wealth-adapter-rollback
```

Post-migration verification: recounts rows and optionally re-checksums every table.

## Profile System

Every migration is defined by a YAML profile in `profiles/<name>/profile.yaml`. Profiles specify:

- **Source and target** — database type, schema, DSN environment variable
- **Migration settings** — batch size, workers, commit frequency
- **Comparison strategy** — row_count, primary_key, checksum, timestamp, or auto
- **Automation policy** — supervised (confirm each table), auto_non_prod, or auto_approved
- **Skip tables** — infrastructure tables to ignore (e.g., flyway_schema_history)
- **DELETE guards** — tables where DELETE is explicitly allowed
- **Known quirks** — per-table notes on triggers, extra columns, identity strategies

### Create a New Profile

```bash
cp -r profiles/_template profiles/my-new-migration
# Edit profiles/my-new-migration/profile.yaml
```

See `profiles/_template/profile.yaml` for the full template with all options documented.

## CLI Commands

| Command | Description |
|---------|-------------|
| `dbmigrate compare <profile>` | Compare source and target databases (read-only) |
| `dbmigrate migrate <profile>` | Dry-run migration (no writes) |
| `dbmigrate migrate <profile> --execute` | Execute migration with writes |
| `dbmigrate validate <profile>` | Post-migration validation |
| `dbmigrate checkpoint list <profile>` | List checkpoint history |
| `dbmigrate checkpoint resume <profile> <run_id>` | Resume a failed run |
| `dbmigrate report <profile> <run_id>` | Generate a run report |

## Safety Features

| Feature | Description |
|---------|-------------|
| **Dry-run default** | `migrate` is read-only unless `--execute` is passed |
| **DELETE guard** | DELETEs require explicit allowlisting in `delete_allowed_tables` |
| **Environment guard** | `auto_non_prod` mode blocks auto-approval for production DSNs |
| **Circuit breaker** | Halts after 3 consecutive errors to prevent cascading failures |
| **Checkpoints** | Per-table, per-batch progress saved to JSON — resume from any failure |
| **Identity safety** | GENERATED ALWAYS override is wrapped in try/finally to ensure restoration |
| **Operator confirmation** | `supervised` mode requires confirmation before each table write |

## Architecture

```
CLI → Profile Loader → Comparator → Engine → Adapters (Source + Target)
                                       ↓
                                  Checkpoint Store
```

- **Profile Loader** — reads YAML, validates, resolves DSN env vars
- **Comparator** — detects differences between source and target (row count, PK diff, checksum)
- **Engine** — orchestrates the migration: compare → classify → confirm → stream → write → validate
- **Adapters** — pluggable database backends behind a common ABC
- **Checkpoint Store** — JSON files tracking per-table, per-batch progress

See `docs/architecture.md` for the full architecture documentation.

## Adding a New Database Adapter

1. Create `src/dbmigrate/database/<engine>.py`
2. Implement the `Database` ABC (connect, list_tables, stream_rows, bulk_insert, etc.)
3. Decorate with `@register_adapter("<engine>")`
4. Test against a real database
5. Use the engine name in any profile's `source.type` or `target.type`

See `docs/adding-a-database-adapter.md` for the full guide with skeleton code and testing checklist.

## Project Structure

```
db-migration-toolkit/
├── README.md
├── pyproject.toml
├── src/dbmigrate/          # Core library
│   ├── cli.py              # Click CLI
│   ├── profile.py          # Profile loader
│   ├── engine.py           # Migration orchestrator
│   ├── comparator.py       # Comparison strategies
│   ├── checkpoint.py       # Checkpoint store
│   └── database/           # Pluggable adapters
│       ├── __init__.py     # ABC + registry
│       ├── postgresql.py
│       └── db2.py
├── profiles/               # Migration profiles
│   ├── _template/
│   └── wealth-adapter-rollback/
├── checkpoints/            # Runtime checkpoint files
├── docs/                   # Documentation
└── tests/                  # Test suite
```

## Documentation

| Document | Description |
|----------|-------------|
| `docs/architecture.md` | System design, components, data flow |
| `docs/database-strategy.md` | Wealth Adapter schema comparison and strategy |
| `docs/progress.md` | Gate progress tracker |
| `docs/known-limitations.md` | Limitations and unverified assumptions |
| `docs/adding-a-database-adapter.md` | Guide for new adapters |
| `docs/runbook-template.md` | Generic migration runbook |

## License

Internal use only — Momentum Metropolitan.
