# DB Migration Toolkit — Architecture

## Design Philosophy

The DB Migration Toolkit is a generic, pluggable, profile-driven data migration engine. It moves rows between heterogeneous databases — PostgreSQL, DB2, MySQL, Oracle, SQL Server, SQLite — without being coupled to any single vendor or use case.

**Core principles:**

1. **Profile-driven** — Every migration is defined by a YAML profile. No hard-coded table lists, no embedded connection strings, no baked-in business logic.
2. **Adapter pattern** — Each database engine is a pluggable adapter behind a common ABC. Adding a new engine means implementing one Python class.
3. **Safety by default** — Dry-run is the default mode. DELETEs require explicit allowlisting. Production environments require manual confirmation. Circuit breakers halt on error thresholds.
4. **Resumable** — Checkpoint files track per-table progress. A crashed migration resumes from the last committed batch, not from scratch.
5. **Observable** — Structured logging, real-time progress bars, and post-run reports with row counts, checksums, and timing.

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI Entry Point                          │
│                     src/dbmigrate/cli.py                        │
│  Commands: compare │ migrate │ validate │ checkpoint │ report   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Profile Loader                             │
│                 src/dbmigrate/profile.py                        │
│  Reads profiles/<name>/profile.yaml                             │
│  Validates schema, resolves DSN env vars, builds config object  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
┌──────────────────┐ ┌──────────┐ ┌──────────────────┐
│  Source Adapter   │ │ Comparator│ │  Target Adapter   │
│  (e.g. PG)       │ │          │ │  (e.g. DB2)       │
│                  │ │ row_count│ │                    │
│ connect()        │ │ pk_diff  │ │ connect()          │
│ list_tables()    │ │ checksum │ │ list_tables()      │
│ get_columns()    │ │ timestamp│ │ get_columns()      │
│ stream_rows()    │ │          │ │ bulk_insert()      │
│ count_rows()     │ │          │ │ execute_load()     │
│ checksum_table() │ │          │ │ reset_sequence()   │
└──────────────────┘ └──────────┘ └──────────────────┘
              │                          │
              └────────────┬─────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Migration Engine                            │
│                src/dbmigrate/engine.py                          │
│                                                                 │
│  For each table (respecting dependency order):                  │
│    1. Compare source vs target (row count, PK diff, checksum)   │
│    2. Classify operation: INSERT / UPDATE / DELETE / NO_ACTION   │
│    3. Apply automation policy (confirm / auto-approve / skip)   │
│    4. Stream rows in batches from source adapter                │
│    5. Write batches to target adapter                           │
│    6. Write checkpoint after each committed batch               │
│    7. Validate: recount, re-checksum                            │
│    8. Circuit-break on error threshold                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Checkpoint Store                              │
│              src/dbmigrate/checkpoint.py                        │
│                                                                 │
│  JSON file per profile per run:                                 │
│  checkpoints/<profile>/<run_id>.json                            │
│  Tracks: table, last_pk, rows_written, batch_number, status     │
└─────────────────────────────────────────────────────────────────┘
```

## Component Details

### CLI (`cli.py`)

The entry point. Built with `click`. Commands:

| Command | Description |
|---------|-------------|
| `compare <profile>` | Run comparison only — show row counts, diffs, checksums. No writes. |
| `migrate <profile>` | Run the full migration pipeline. Defaults to `--dry-run`. |
| `migrate <profile> --execute` | Actually write data. Requires confirmation for production. |
| `validate <profile>` | Post-migration validation — recount and re-checksum every table. |
| `checkpoint list <profile>` | Show checkpoint history for a profile. |
| `checkpoint resume <profile> <run_id>` | Resume a failed run from its last checkpoint. |
| `report <profile> <run_id>` | Generate a summary report for a completed or failed run. |

### Profile Loader (`profile.py`)

Reads `profiles/<name>/profile.yaml` and produces a strongly-typed `MigrationProfile` dataclass. Validates:

- Required fields are present
- DSN environment variables are set (or fails with clear message)
- `skip_tables` and `delete_allowed_tables` are lists of strings
- `known_quirks` entries have `table` and `note` fields
- `migration.mode` is one of `sync`, `rollback`
- `automation.mode` is one of `supervised`, `auto_non_prod`, `auto_approved`

### Database Adapters (`database/`)

Each adapter implements the `Database` ABC:

```python
class Database(ABC):
    @abstractmethod
    def connect(self, dsn: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def list_tables(self, schema: str) -> list[TableInfo]: ...

    @abstractmethod
    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]: ...

    @abstractmethod
    def get_primary_key(self, schema: str, table: str) -> list[str]: ...

    @abstractmethod
    def get_identity_strategy(self, schema: str, table: str) -> IdentityStrategy: ...

    @abstractmethod
    def count_rows(self, schema: str, table: str) -> int: ...

    @abstractmethod
    def checksum_table(self, schema: str, table: str, columns: list[str]) -> str: ...

    @abstractmethod
    def stream_rows(self, schema: str, table: str, columns: list[str],
                    batch_size: int, after_pk: Any = None) -> Iterator[list[tuple]]: ...

    @abstractmethod
    def bulk_insert(self, schema: str, table: str, columns: list[str],
                    rows: list[tuple]) -> int: ...

    @abstractmethod
    def reset_sequence(self, schema: str, table: str, value: int) -> None: ...

    @abstractmethod
    def disable_triggers(self, schema: str, table: str) -> None: ...

    @abstractmethod
    def enable_triggers(self, schema: str, table: str) -> None: ...

    @abstractmethod
    def begin_identity_override(self, schema: str, table: str) -> None: ...

    @abstractmethod
    def end_identity_override(self, schema: str, table: str) -> None: ...
```

Adapters register themselves via a `@register_adapter("db_type")` decorator so the engine can instantiate them by name from the profile.

### Comparator (`comparator.py`)

Strategies for detecting differences between source and target:

| Strategy | Speed | Accuracy | When Used |
|----------|-------|----------|-----------|
| `row_count` | Fast | Low | Quick sanity check — counts only |
| `primary_key` | Medium | High | Compares PK sets to find missing/extra rows |
| `checksum` | Slow | Highest | MD5/SHA256 over sorted, serialized column values |
| `timestamp` | Fast | Medium | Compare MAX(updated_at) — requires timestamp column |
| `auto` | Varies | High | Starts with row_count; if mismatch, escalates to primary_key |

### Migration Engine (`engine.py`)

The orchestrator. For each table in dependency order:

1. **Skip check** — Is the table in `skip_tables`? Skip.
2. **Compare** — Run the configured comparison strategy.
3. **Classify** — Determine operation: INSERT (target missing rows), UPDATE (target has stale rows), DELETE (target has extra rows), NO_ACTION (identical).
4. **Quirk check** — Load any `known_quirks` for the table, apply column exclusions or identity overrides.
5. **Policy check** — Based on `automation.mode`, either auto-approve, prompt the operator, or skip.
6. **DELETE guard** — If operation is DELETE and table is not in `delete_allowed_tables`, refuse.
7. **Execute** — Stream rows from source, write batches to target. For GENERATED ALWAYS tables, call `begin_identity_override()` first.
8. **Checkpoint** — After each committed batch, write checkpoint.
9. **Validate** — Recount target. If checksum strategy, re-checksum.
10. **Circuit break** — If error count exceeds threshold, halt all remaining tables.

### Checkpoint Store (`checkpoint.py`)

Each run gets a JSON file:

```json
{
  "run_id": "20260824-143022",
  "profile": "wealth-adapter-rollback",
  "started_at": "2026-08-24T14:30:22Z",
  "tables": {
    "adapterconfig": {
      "status": "completed",
      "rows_written": 105,
      "last_pk": 528,
      "batches_completed": 1,
      "completed_at": "2026-08-24T14:30:25Z"
    },
    "facsmessageentity": {
      "status": "in_progress",
      "rows_written": 2500000,
      "last_pk": 2500000,
      "batches_completed": 500
    }
  }
}
```

On resume, the engine reads the checkpoint, skips completed tables, and continues from `last_pk` for in-progress tables.

## Data Flow

```
Source DB                    Toolkit                      Target DB
─────────                    ───────                      ─────────
                       ┌─ Profile loaded
                       │  DSN resolved
                       │  Tables enumerated
                       ▼
  list_tables() ◄──── Compare ────► list_tables()
  count_rows()  ◄──── phase   ────► count_rows()
  get_columns() ◄─────────────────► get_columns()
                       │
                       ▼
                  Classification
                  (INSERT/UPDATE/
                   DELETE/NO_ACTION)
                       │
                       ▼
                  Operator prompt
                  (if supervised)
                       │
                       ▼
  stream_rows() ◄──── Execute ────► begin_identity_override()
  (batch N)            │             bulk_insert(batch N)
                       │             checkpoint(batch N)
  stream_rows() ◄──── │ ──────────► bulk_insert(batch N+1)
  (batch N+1)          │             checkpoint(batch N+1)
                       │             ...
                       │             end_identity_override()
                       │             reset_sequence()
                       ▼
                  Validate
                  (recount, checksum)
                       │
                       ▼
                  Report
```

## Safety Mechanisms

### Dry-Run Default

Every `migrate` command runs in dry-run mode unless `--execute` is explicitly passed. Dry-run performs all comparison and classification steps, prints the plan, and exits without writing.

### Environment Guard

The `automation.mode` field controls confirmation behavior:

| Mode | Behavior |
|------|----------|
| `supervised` | Every table requires explicit operator confirmation before writes. |
| `auto_non_prod` | Auto-approve if the DSN hostname does NOT contain `prod`, `prd`, or `live`. Prompt otherwise. |
| `auto_approved` | Auto-approve all operations. Use only for fully tested, scheduled migrations. |

Additionally, `auto_confirm_below_rows` allows auto-approving tables with fewer rows than the threshold, even in supervised mode. Set to `0` to disable.

### DELETE Guard

DELETE operations are blocked by default. A table must be explicitly listed in `delete_allowed_tables` in the profile for DELETEs to proceed. This prevents accidental data loss.

### Circuit Breaker

The engine tracks consecutive errors per run. If the error count exceeds a configurable threshold (default: 3), all remaining tables are skipped and the run is marked as `failed`. The checkpoint file preserves progress so the operator can investigate, fix the issue, and resume.

### Identity Override Safety

For GENERATED ALWAYS tables, the engine:
1. Calls `begin_identity_override()` before the first batch (e.g., `ALTER TABLE ... ALTER COLUMN ... SET GENERATED BY DEFAULT` for DB2)
2. Inserts all batches with explicit identity values
3. Calls `end_identity_override()` after the last batch (restores GENERATED ALWAYS)
4. Calls `reset_sequence()` to set the sequence to MAX(id) + 1

If the run crashes between steps 1 and 3, the checkpoint records that identity override is active, and resume will handle cleanup.

### Checksum Validation

After inserting all rows for a table, the engine optionally re-checksums both source and target to confirm byte-for-byte equivalence. This catches encoding issues, truncation, and rounding errors that row counts alone would miss.

## Extending for New Database Engines

1. Create `src/dbmigrate/database/<engine>.py`
2. Implement the `Database` ABC
3. Decorate with `@register_adapter("<engine_name>")`
4. The engine auto-discovers adapters at startup via the registry
5. Use the new engine name in `source.type` or `target.type` in any profile

See `docs/adding-a-database-adapter.md` for the full guide with a skeleton implementation and testing checklist.

## Directory Layout

```
db-migration-toolkit/
├── README.md
├── pyproject.toml
├── docker-compose.testcontainers.yml  # Local test containers (PG + Db2)
├── src/
│   └── dbmigrate/
│       ├── __init__.py
│       ├── cli.py              # Click CLI entry point
│       ├── profile.py          # Profile loader and validation
│       ├── engine.py           # Migration orchestrator
│       ├── comparator.py       # Comparison strategies
│       ├── checkpoint.py       # Checkpoint store
│       ├── report.py           # Report generation
│       ├── models.py           # Dataclasses (TableInfo, ColumnInfo, etc.)
│       └── database/
│           ├── __init__.py     # ABC + adapter registry
│           ├── postgresql.py   # PostgreSQL adapter
│           ├── db2.py          # DB2 adapter
│           ├── mysql.py        # MySQL adapter (future)
│           └── sqlite.py       # SQLite adapter (testing)
├── profiles/
│   ├── _template/
│   │   └── profile.yaml
│   └── wealth-adapter-rollback/
│       ├── profile.yaml
│       └── investigation.md
├── checkpoints/                # Generated at runtime
├── docs/
│   ├── architecture.md
│   ├── integration-testing.md  # Integration test harness guide
│   ├── progress.md
│   ├── database-strategy.md
│   ├── known-limitations.md
│   ├── adding-a-database-adapter.md
│   └── runbook-template.md
└── tests/
    ├── test_profile.py
    ├── test_comparator.py
    ├── test_engine.py
    ├── test_adapters/
    │   ├── test_postgresql.py
    │   ├── test_db2.py
    │   └── test_sqlite.py
    └── integration/
        └── testcontainers/
            ├── __init__.py
            ├── conftest.py          # pytest fixtures
            ├── containers.py        # Container classes (External + Testcontainer)
            ├── mcp_fetcher.py       # Multi-source data fetcher (Db2/PG/embedded)
            ├── schema_manager.py    # DDL + INSERT logic
            ├── populate_test_dbs.py # CLI orchestrator
            └── test_populated_dbs.py
```

## Testing Strategy

The project uses a layered testing approach:

| Layer | Scope | Speed | Data Source |
|-------|-------|-------|-------------|
| Unit tests | Individual functions/classes | Fast | Mocked |
| Integration tests | Full adapter + engine pipeline | Medium | Docker containers |
| Validation tests | Post-migration correctness | Slow | Live dev databases |

### Integration Test Infrastructure

The integration test harness (`tests/integration/testcontainers/`) supports two modes:

1. **Docker Compose mode** (`--use-compose`) — connects to pre-started containers defined in `docker-compose.testcontainers.yml`. Fastest for iterative development.
2. **Testcontainers mode** (default) — Python manages container lifecycle automatically. Best for CI.

Target selection flags (`--pg-only`, `--db2-only`) control which databases are populated. The populate script always drops and recreates all tables in the selected target(s) before inserting data, ensuring idempotent runs.

Data can be sourced from:
- **Dev Db2** (via `WA_TARGET_DSN`) — production-like data from the WEALTH schema
- **Dev PG** (via `WA_SOURCE_DSN`) — the `wealthadapter` schema on the dev PG instance
- **Embedded seed** — hardcoded minimal data for offline/CI environments

Resolution order in `auto` mode: Db2 → PG → embedded (first available wins).

See `docs/integration-testing.md` for the full setup and usage guide.
