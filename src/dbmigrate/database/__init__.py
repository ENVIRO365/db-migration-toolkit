"""Database adapter abstract base class — the adapter contract.

All database-specific logic is encapsulated behind this interface.
New databases are added by implementing this ABC and registering
the adapter via the :func:`register_adapter` decorator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Generator, Optional

from dbmigrate.models import (
    ColumnMetadata,
    DatabaseMetadata,
    ForeignKeyMetadata,
    IdentityStrategy,
    PrimaryKeyMetadata,
    SequenceMetadata,
    TableMetadata,
    TriggerMetadata,
)

# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: dict[str, type[Database]] = {}


def register_adapter(name: str):
    """Class decorator that registers a :class:`Database` implementation.

    Usage::

        @register_adapter("postgresql")
        class PostgreSQLAdapter(Database):
            ...
    """

    def decorator(cls: type[Database]) -> type[Database]:
        _ADAPTER_REGISTRY[name] = cls
        return cls

    return decorator


def get_adapter(name: str) -> type[Database]:
    """Look up an adapter class by its registered name.

    Raises
    ------
    ValueError
        If *name* has not been registered.
    """
    if name not in _ADAPTER_REGISTRY:
        available = ", ".join(sorted(_ADAPTER_REGISTRY.keys()))
        raise ValueError(f"Unknown database adapter: '{name}'. Available: {available}")
    return _ADAPTER_REGISTRY[name]


def list_adapters() -> list[str]:
    """Return sorted list of registered adapter names."""
    return sorted(_ADAPTER_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class Database(ABC):
    """Abstract base class for database adapters.

    Implementations must be stateless with respect to business logic.
    All table/column-specific behaviour comes from metadata discovery,
    not from hard-coded knowledge of any particular schema.
    """

    def __init__(self, dsn: str, schema: str) -> None:
        self.dsn = dsn
        self.schema = schema
        self._connection: Any = None

    # ---- Connection lifecycle ---------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish a database connection."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the database connection and release resources."""
        ...

    @contextmanager
    def connection(self) -> Generator[None, None, None]:
        """Context manager wrapping :meth:`connect` / :meth:`close`."""
        self.connect()
        try:
            yield
        finally:
            self.close()

    # ---- Schema discovery -------------------------------------------------

    @abstractmethod
    def get_tables(self) -> list[str]:
        """Return all user table names in the configured schema."""
        ...

    @abstractmethod
    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        """Return column metadata for *table_name*."""
        ...

    @abstractmethod
    def get_primary_key(self, table_name: str) -> Optional[PrimaryKeyMetadata]:
        """Return primary key metadata, or ``None`` if no PK exists."""
        ...

    @abstractmethod
    def get_foreign_keys(self, table_name: str) -> list[ForeignKeyMetadata]:
        """Return foreign key constraints referencing other tables."""
        ...

    @abstractmethod
    def get_identity_columns(self, table_name: str) -> list[ColumnMetadata]:
        """Return columns with identity / auto-increment semantics."""
        ...

    @abstractmethod
    def get_sequences(self) -> list[SequenceMetadata]:
        """Return all sequences in the schema (identity + standalone)."""
        ...

    @abstractmethod
    def get_triggers(self, table_name: str) -> list[TriggerMetadata]:
        """Return trigger metadata for *table_name*."""
        ...

    @abstractmethod
    def get_row_count(self, table_name: str) -> int:
        """Return the exact row count for *table_name*."""
        ...

    @abstractmethod
    def get_max_primary_key(self, table_name: str, pk_column: str) -> Optional[int]:
        """Return the maximum primary key value, or ``None`` if the table is empty."""
        ...

    def get_database_metadata(self) -> DatabaseMetadata:
        """Discover and return full schema metadata.

        This default implementation calls the individual discovery methods
        for every table returned by :meth:`get_tables`.  Subclasses may
        override for performance (e.g. batch catalog queries).
        """
        tables: dict[str, TableMetadata] = {}

        for table_name in self.get_tables():
            columns = self.get_columns(table_name)
            pk = self.get_primary_key(table_name)
            fks = self.get_foreign_keys(table_name)
            identity_cols = self.get_identity_columns(table_name)
            triggers = self.get_triggers(table_name)
            row_count = self.get_row_count(table_name)

            identity_col = identity_cols[0] if identity_cols else None
            max_pk: Optional[int] = None
            if pk and not pk.is_composite and len(pk.columns) == 1:
                max_pk = self.get_max_primary_key(table_name, pk.columns[0])

            table_meta = TableMetadata(
                name=table_name,
                schema=self.schema,
                columns=columns,
                primary_key=pk,
                foreign_keys=fks,
                triggers=triggers,
                row_count=row_count,
                identity_column=identity_col,
                max_pk_value=max_pk,
            )
            tables[table_name.lower()] = table_meta

        sequences = self.get_sequences()
        standalone = [s for s in sequences if not s.is_identity_sequence]

        return DatabaseMetadata(
            engine=self.engine_name,
            schema=self.schema,
            tables=tables,
            standalone_sequences=standalone,
            version=self.get_version(),
        )

    # ---- Data access ------------------------------------------------------

    @abstractmethod
    def stream_rows(
        self,
        table_name: str,
        columns: list[str],
        pk_column: Optional[str] = None,
        start_pk: Optional[int] = None,
        end_pk: Optional[int] = None,
        batch_size: int = 5000,
    ) -> Generator[list[dict[str, Any]], None, None]:
        """Yield rows in batches using server-side cursors or keyset pagination."""
        ...

    @abstractmethod
    def fetch_rows_by_keys(
        self,
        table_name: str,
        columns: list[str],
        pk_columns: list[str],
        pk_values: list[Any],
    ) -> list[dict[str, Any]]:
        """Fetch specific rows identified by their primary key values.

        For single-column PKs, *pk_values* is a flat list of scalars.
        For composite PKs, *pk_values* is a list of tuples.
        """
        ...

    # ---- Data mutation ----------------------------------------------------

    @abstractmethod
    def insert_batch(
        self,
        table_name: str,
        columns: list[str],
        rows: list[tuple[Any, ...]],
        identity_strategy: IdentityStrategy = IdentityStrategy.NONE,
    ) -> int:
        """Insert *rows* into *table_name*.  Returns the number of rows inserted."""
        ...

    @abstractmethod
    def update_batch(
        self,
        table_name: str,
        pk_columns: list[str],
        update_columns: list[str],
        rows: list[tuple[Any, ...]],
    ) -> int:
        """Update rows by primary key.  Returns the number of rows updated."""
        ...

    @abstractmethod
    def delete_batch(
        self,
        table_name: str,
        pk_columns: list[str],
        pk_values: list[tuple[Any, ...]],
    ) -> int:
        """Delete rows by primary key.  Returns the number of rows deleted."""
        ...

    # ---- Transaction management -------------------------------------------

    @abstractmethod
    def begin_transaction(self) -> None:
        """Begin an explicit transaction."""
        ...

    @abstractmethod
    def commit(self) -> None:
        """Commit the current transaction."""
        ...

    @abstractmethod
    def rollback_transaction(self) -> None:
        """Roll back the current transaction."""
        ...

    # ---- Bulk load (optional capability) ----------------------------------

    def supports_bulk_load(self) -> bool:
        """Whether this adapter supports bulk-load operations."""
        return False

    def bulk_load(
        self,
        table_name: str,
        file_path: str,
        columns: list[str],
        identity_override: bool = False,
    ) -> int:
        """Bulk-load data from *file_path*.  Returns the number of rows loaded.

        Raises :class:`NotImplementedError` unless overridden.
        """
        raise NotImplementedError(f"{self.engine_name} adapter does not support bulk load")

    # ---- Trigger management -----------------------------------------------

    def disable_triggers(self, table_name: str) -> None:
        """Disable triggers on *table_name* (if supported)."""
        raise NotImplementedError(f"{self.engine_name} adapter does not support trigger management")

    def enable_triggers(self, table_name: str) -> None:
        """Re-enable triggers on *table_name* (if supported)."""
        raise NotImplementedError(f"{self.engine_name} adapter does not support trigger management")

    # ---- Identity management ----------------------------------------------

    @abstractmethod
    def reset_identity(self, table_name: str, column_name: str, new_value: int) -> None:
        """Reset an identity column's next generated value."""
        ...

    def reset_sequence(self, sequence_name: str, new_value: int) -> None:
        """Reset a standalone sequence's next value.

        Raises :class:`NotImplementedError` unless overridden.
        """
        raise NotImplementedError(f"{self.engine_name} adapter does not support sequence reset")

    # ---- Utility ----------------------------------------------------------

    @abstractmethod
    def execute(self, sql: str, params: Optional[tuple[Any, ...]] = None) -> Any:
        """Execute a single SQL statement, optionally with parameters."""
        ...

    @abstractmethod
    def execute_many(self, sql: str, params_list: list[tuple[Any, ...]]) -> int:
        """Execute a SQL statement once per parameter set.  Returns total affected rows."""
        ...

    @abstractmethod
    def get_version(self) -> str:
        """Return the database engine version string."""
        ...

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Human-readable engine name (e.g. ``'PostgreSQL'``, ``'DB2'``)."""
        ...

    def get_encoding(self) -> Optional[str]:
        """Return the database or schema encoding, if discoverable."""
        return None

    # ---- Primary-key streaming for comparison -----------------------------

    @abstractmethod
    def stream_primary_keys(
        self,
        table_name: str,
        pk_columns: list[str],
        batch_size: int = 10000,
    ) -> Generator[list[Any], None, None]:
        """Yield primary-key values in sorted batches for delta comparison.

        For single-column PKs, each element is a scalar value.
        For composite PKs, each element is a tuple of values.
        """
        ...


# ---------------------------------------------------------------------------
# Eagerly import adapters so @register_adapter decorators execute
# ---------------------------------------------------------------------------
from dbmigrate.database import postgresql as _pg, db2 as _db2  # noqa: E402, F401
