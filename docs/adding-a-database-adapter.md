# Adding a New Database Adapter

This guide walks through adding support for a new database engine to the DB Migration Toolkit.

## Overview

Each database engine is implemented as a Python class that extends the `Database` abstract base class. The adapter handles all engine-specific SQL dialects, connection management, identity/sequence handling, and bulk loading.

## Step 1: Create the Adapter File

Create a new file in `src/dbmigrate/database/`:

```
src/dbmigrate/database/<engine>.py
```

For example, to add Oracle support: `src/dbmigrate/database/oracle.py`.

## Step 2: Implement the Database ABC

Every adapter must implement all methods of the `Database` ABC. Here is the complete interface:

```python
from dbmigrate.database import Database, register_adapter
from dbmigrate.models import TableInfo, ColumnInfo, IdentityStrategy

@register_adapter("oracle")
class OracleDatabase(Database):
    """Oracle database adapter."""

    def connect(self, dsn: str) -> None:
        """
        Establish a connection to the database.

        Args:
            dsn: Connection string. Format is engine-specific.
                 For Oracle: "user/password@host:port/service_name"
        """
        ...

    def close(self) -> None:
        """Close the database connection and release resources."""
        ...

    def list_tables(self, schema: str) -> list[TableInfo]:
        """
        List all tables in the given schema.

        Returns:
            List of TableInfo with table_name, row_count (estimate ok),
            and has_identity fields populated.
        """
        ...

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        """
        Get column metadata for a table.

        Returns:
            List of ColumnInfo with column_name, data_type, max_length,
            is_nullable, is_identity, identity_generation fields.
        """
        ...

    def get_primary_key(self, schema: str, table: str) -> list[str]:
        """
        Get the primary key column(s) for a table.

        Returns:
            List of column names forming the PK, in order.
            Empty list if the table has no PK.
        """
        ...

    def get_identity_strategy(self, schema: str, table: str) -> IdentityStrategy:
        """
        Determine the identity generation strategy for the table's PK.

        Returns:
            IdentityStrategy enum: ALWAYS, BY_DEFAULT, NONE, COMPOSITE
        """
        ...

    def count_rows(self, schema: str, table: str) -> int:
        """
        Count rows in a table. Must return an exact count.

        For large tables, this may be slow. The engine calls this
        during comparison and post-migration validation.
        """
        ...

    def checksum_table(self, schema: str, table: str, columns: list[str]) -> str:
        """
        Compute a deterministic checksum over all rows in the table.

        Args:
            columns: The columns to include in the checksum.
                     Ordered consistently between source and target.

        Returns:
            A hex-encoded SHA-256 digest string.

        Implementation notes:
            - SELECT columns ORDER BY primary key
            - Concatenate values with '|' delimiter
            - NULLs as literal '\\N'
            - Timestamps as ISO 8601 with microsecond precision
            - BLOBs as hex-encoded strings
            - SHA-256 over newline-separated row strings
        """
        ...

    def stream_rows(
        self,
        schema: str,
        table: str,
        columns: list[str],
        batch_size: int,
        after_pk: Any = None,
    ) -> Iterator[list[tuple]]:
        """
        Stream rows from a table in batches, ordered by primary key.

        Args:
            columns: Columns to SELECT.
            batch_size: Number of rows per batch.
            after_pk: Resume from this PK value (exclusive).
                      None means start from the beginning.

        Yields:
            Lists of tuples, each list containing up to batch_size rows.
        """
        ...

    def bulk_insert(
        self, schema: str, table: str, columns: list[str], rows: list[tuple]
    ) -> int:
        """
        Insert a batch of rows into the target table.

        Args:
            columns: Column names matching the tuple positions.
            rows: List of tuples to insert.

        Returns:
            Number of rows successfully inserted.
        """
        ...

    def reset_sequence(self, schema: str, table: str, value: int) -> None:
        """
        Reset the identity/sequence for a table to the given value.

        After bulk inserting with explicit IDs, the sequence must be
        set to MAX(id) + 1 to avoid collisions on future inserts.
        """
        ...

    def disable_triggers(self, schema: str, table: str) -> None:
        """Disable all triggers on a table. Engine-specific syntax."""
        ...

    def enable_triggers(self, schema: str, table: str) -> None:
        """Re-enable all triggers on a table."""
        ...

    def begin_identity_override(self, schema: str, table: str) -> None:
        """
        Prepare a GENERATED ALWAYS table to accept explicit identity values.

        For DB2: ALTER TABLE ... SET GENERATED BY DEFAULT
        For PostgreSQL: SET session_replication_role = 'replica'
        For others: engine-specific mechanism
        """
        ...

    def end_identity_override(self, schema: str, table: str) -> None:
        """
        Restore the original identity generation strategy.

        Must be called after all inserts are complete, even if the
        migration fails (use try/finally).
        """
        ...
```

## Step 3: Register with @register_adapter

The `@register_adapter("engine_name")` decorator registers your adapter class in the global adapter registry. The engine name must match what users put in `source.type` or `target.type` in their profile YAML.

```python
from dbmigrate.database import register_adapter

@register_adapter("oracle")
class OracleDatabase(Database):
    ...
```

The registry is defined in `src/dbmigrate/database/__init__.py`:

```python
_ADAPTERS: dict[str, type[Database]] = {}

def register_adapter(name: str):
    def decorator(cls: type[Database]) -> type[Database]:
        _ADAPTERS[name] = cls
        return cls
    return decorator

def get_adapter(name: str) -> Database:
    if name not in _ADAPTERS:
        available = ", ".join(sorted(_ADAPTERS.keys()))
        raise ValueError(
            f"Unknown database type '{name}'. Available: {available}"
        )
    return _ADAPTERS[name]()
```

Adapter modules are auto-imported at package init time via a glob import in `__init__.py`.

## Step 4: Test Against a Real Database

Create a test file at `tests/test_adapters/test_<engine>.py`. The test suite must cover:

### Testing Checklist

| # | Test | Description |
|---|------|-------------|
| 1 | `test_connect` | Connect with valid DSN, verify no exception |
| 2 | `test_connect_invalid_dsn` | Connect with bad DSN, verify clear error message |
| 3 | `test_list_tables` | List tables, verify known tables are present |
| 4 | `test_get_columns` | Get columns for a known table, verify names and types |
| 5 | `test_get_primary_key` | Get PK for a table with single PK, composite PK, and no PK |
| 6 | `test_get_identity_strategy` | Verify ALWAYS, BY_DEFAULT, NONE, and COMPOSITE detection |
| 7 | `test_count_rows` | Count a known table, verify exact count |
| 8 | `test_checksum_empty_table` | Checksum an empty table, verify deterministic result |
| 9 | `test_checksum_populated_table` | Checksum a known table, verify consistency across runs |
| 10 | `test_stream_rows_small` | Stream a table with < batch_size rows, verify single batch |
| 11 | `test_stream_rows_batched` | Stream a table with > batch_size rows, verify multiple batches |
| 12 | `test_stream_rows_resume` | Stream with after_pk, verify rows start after that PK |
| 13 | `test_bulk_insert` | Insert rows into a test table, verify count |
| 14 | `test_bulk_insert_with_nulls` | Insert rows with NULL values, verify they survive round-trip |
| 15 | `test_reset_sequence` | Reset sequence, insert without explicit ID, verify new ID |
| 16 | `test_disable_enable_triggers` | Disable triggers, insert, re-enable, verify trigger state |
| 17 | `test_identity_override_always` | Override GENERATED ALWAYS, insert explicit IDs, restore |
| 18 | `test_identity_override_by_default` | Verify BY DEFAULT accepts explicit IDs without override |
| 19 | `test_special_characters` | Insert rows with unicode, newlines, quotes, verify round-trip |
| 20 | `test_timestamp_precision` | Insert timestamp with 6 fractional digits, verify round-trip |
| 21 | `test_blob_round_trip` | Insert binary data, verify identical bytes on read-back |
| 22 | `test_clob_round_trip` | Insert large text (>32KB), verify identical content on read-back |

### Example Test Structure

```python
import pytest
from dbmigrate.database import get_adapter

@pytest.fixture
def oracle_db():
    db = get_adapter("oracle")
    db.connect(os.environ["TEST_ORACLE_DSN"])
    yield db
    db.close()

def test_list_tables(oracle_db):
    tables = oracle_db.list_tables("TEST_SCHEMA")
    table_names = [t.table_name for t in tables]
    assert "KNOWN_TABLE" in table_names

def test_stream_rows_batched(oracle_db):
    batches = list(oracle_db.stream_rows(
        schema="TEST_SCHEMA",
        table="LARGE_TABLE",
        columns=["id", "name", "created_at"],
        batch_size=100,
    ))
    assert len(batches) > 1
    assert all(len(batch) <= 100 for batch in batches)
```

## Step 5: Create or Update a Profile

Create a new profile in `profiles/<use-case>/profile.yaml` that uses your adapter:

```yaml
name: my-oracle-migration
description: "Migrate data from PostgreSQL to Oracle"

source:
  type: postgresql
  schema: public
  dsn_env: PG_SOURCE_DSN

target:
  type: oracle
  schema: MY_SCHEMA
  dsn_env: ORACLE_TARGET_DSN

migration:
  mode: sync
  batch_size: 5000
  fetch_size: 5000
  workers: 4
  commit_every: 5000
```

## Engine-Specific Notes

When implementing an adapter, pay attention to these common differences between engines:

| Concern | PostgreSQL | DB2 | Oracle | MySQL | SQL Server |
|---------|-----------|-----|--------|-------|------------|
| Identity keyword | `GENERATED ALWAYS/BY DEFAULT AS IDENTITY` | Same | Same (12c+) | `AUTO_INCREMENT` | `IDENTITY(1,1)` |
| Sequence reset | `ALTER SEQUENCE ... RESTART WITH` | `ALTER TABLE ... RESTART WITH` | `ALTER SEQUENCE ... RESTART START WITH` | N/A (AUTO_INCREMENT) | `DBCC CHECKIDENT` |
| Disable triggers | `ALTER TABLE ... DISABLE TRIGGER ALL` | Engine-specific | `ALTER TRIGGER ... DISABLE` | N/A | `DISABLE TRIGGER ... ON` |
| Bulk insert | `COPY FROM` or `execute_values` | `LOAD FROM` | `SQL*Loader` or `INSERT ALL` | `LOAD DATA INFILE` | `BULK INSERT` |
| VARCHAR semantics | Characters | Bytes (default) | Bytes (default) | Characters | Characters |
| NULL sorting | `NULLS LAST` (default) | `NULLS LAST` | `NULLS LAST` (default) | `NULLS FIRST` | `NULLS FIRST` |
| Schema separator | `.` | `.` | `.` | `.` (database) | `.` |

## Adapter File Template

Save this as your starting point:

```python
"""
<Engine> database adapter for DB Migration Toolkit.

Requirements:
    pip install <driver-package>
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

from dbmigrate.database import Database, register_adapter
from dbmigrate.models import ColumnInfo, IdentityStrategy, TableInfo


@register_adapter("<engine>")
class <Engine>Database(Database):
    """<Engine> database adapter."""

    def __init__(self) -> None:
        self._conn = None

    def connect(self, dsn: str) -> None:
        import <driver>
        self._conn = <driver>.connect(dsn)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def list_tables(self, schema: str) -> list[TableInfo]:
        # Query information_schema or engine-specific catalog
        raise NotImplementedError

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        raise NotImplementedError

    def get_primary_key(self, schema: str, table: str) -> list[str]:
        raise NotImplementedError

    def get_identity_strategy(self, schema: str, table: str) -> IdentityStrategy:
        raise NotImplementedError

    def count_rows(self, schema: str, table: str) -> int:
        cursor = self._conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
        return cursor.fetchone()[0]

    def checksum_table(self, schema: str, table: str, columns: list[str]) -> str:
        raise NotImplementedError

    def stream_rows(
        self,
        schema: str,
        table: str,
        columns: list[str],
        batch_size: int,
        after_pk: Any = None,
    ) -> Iterator[list[tuple]]:
        raise NotImplementedError

    def bulk_insert(
        self, schema: str, table: str, columns: list[str], rows: list[tuple]
    ) -> int:
        raise NotImplementedError

    def reset_sequence(self, schema: str, table: str, value: int) -> None:
        raise NotImplementedError

    def disable_triggers(self, schema: str, table: str) -> None:
        raise NotImplementedError

    def enable_triggers(self, schema: str, table: str) -> None:
        raise NotImplementedError

    def begin_identity_override(self, schema: str, table: str) -> None:
        raise NotImplementedError

    def end_identity_override(self, schema: str, table: str) -> None:
        raise NotImplementedError
```

Replace all `<Engine>`, `<engine>`, and `<driver>` placeholders with your engine's values.
