"""PostgreSQL database adapter using psycopg2.

Implements the :class:`Database` ABC with:
- Server-side (named) cursors for memory-efficient streaming
- Keyset pagination for deterministic, resumable row iteration
- Schema discovery via ``information_schema`` and ``pg_catalog``
- Identity column detection (GENERATED ALWAYS / BY DEFAULT)
- Trigger management (DISABLE / ENABLE TRIGGER ALL)
- Sequence and identity reset
- COPY-based bulk load support
"""

from __future__ import annotations

import io
import csv
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Generator, Optional

import psycopg2
import psycopg2.extras
import psycopg2.extensions

from dbmigrate.database import Database, register_adapter
from dbmigrate.models import (
    ColumnMetadata,
    ForeignKeyMetadata,
    IdentityStrategy,
    PrimaryKeyMetadata,
    SequenceMetadata,
    TriggerMetadata,
)

logger = logging.getLogger(__name__)


def _qualified(schema: str, table: str) -> str:
    """Return a schema-qualified, identifier-quoted table reference."""
    return f'"{schema}"."{table}"'


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier."""
    return f'"{name}"'


@register_adapter("postgresql")
class PostgreSQLAdapter(Database):
    """PostgreSQL adapter backed by *psycopg2*."""

    # ------------------------------------------------------------------ init

    def __init__(self, dsn: str, schema: str = "public") -> None:
        super().__init__(dsn, schema)

    # --------------------------------------------------------- engine_name

    @property
    def engine_name(self) -> str:  # noqa: D401
        """Human-readable engine name."""
        return "PostgreSQL"

    # ------------------------------------------------ connection lifecycle

    def connect(self) -> None:
        """Open a connection to PostgreSQL.

        The connection is put into *autocommit* mode by default so that
        callers can opt-in to transactions explicitly via
        :meth:`begin_transaction`.
        """
        if self._connection is not None and not self._connection.closed:
            return
        logger.info("Connecting to PostgreSQL: %s", self.dsn.split("@")[-1])
        self._connection = psycopg2.connect(self.dsn)
        self._connection.set_session(autocommit=True)
        logger.debug("PostgreSQL connection established (server_version=%s)", self._connection.server_version)

    def close(self) -> None:
        """Close the connection and release resources."""
        if self._connection is not None and not self._connection.closed:
            self._connection.close()
            logger.info("PostgreSQL connection closed")
        self._connection = None

    def _ensure_connected(self) -> psycopg2.extensions.connection:
        """Return the live connection, raising if not connected."""
        if self._connection is None or self._connection.closed:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._connection

    @contextmanager
    def _cursor(self) -> Generator[psycopg2.extensions.cursor, None, None]:
        """Yield a standard client-side cursor."""
        conn = self._ensure_connected()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    @contextmanager
    def _named_cursor(self, name: Optional[str] = None) -> Generator[psycopg2.extensions.cursor, None, None]:
        """Yield a server-side (named) cursor for streaming large result sets.

        Server-side cursors require a transaction context, so we temporarily
        leave autocommit mode if necessary.
        """
        conn = self._ensure_connected()
        was_autocommit = conn.autocommit
        if was_autocommit:
            conn.set_session(autocommit=False)
        cursor_name = name or f"srv_{uuid.uuid4().hex[:12]}"
        cur = conn.cursor(name=cursor_name)
        cur.itersize = 5000
        try:
            yield cur
        finally:
            cur.close()
            if was_autocommit:
                conn.rollback()  # close the implicit txn opened for the server-side cursor
                conn.set_session(autocommit=True)

    # ------------------------------------------------- schema discovery

    def get_tables(self) -> list[str]:
        """Return all user table names in *self.schema*."""
        sql = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema,))
            return [row[0] for row in cur.fetchall()]

    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        """Return column metadata from ``information_schema.columns``."""
        sql = """
            SELECT
                column_name,
                data_type,
                character_maximum_length,
                numeric_precision,
                numeric_scale,
                is_nullable,
                column_default,
                ordinal_position,
                is_identity,
                identity_generation
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema, table_name))
            columns: list[ColumnMetadata] = []
            for row in cur.fetchall():
                (
                    col_name, data_type, max_len, num_prec, num_scale,
                    is_nullable_str, default_val, ordinal,
                    is_identity_str, identity_gen,
                ) = row

                is_ident = is_identity_str == "YES"
                ident_strategy: Optional[IdentityStrategy] = None
                if is_ident:
                    ident_strategy = (
                        IdentityStrategy.ALWAYS
                        if identity_gen == "ALWAYS"
                        else IdentityStrategy.BY_DEFAULT
                    )

                columns.append(ColumnMetadata(
                    name=col_name,
                    data_type=data_type,
                    max_length=max_len,
                    numeric_precision=num_prec,
                    numeric_scale=num_scale,
                    is_nullable=(is_nullable_str == "YES"),
                    is_identity=is_ident,
                    identity_generation=ident_strategy,
                    default_value=default_val,
                    ordinal_position=ordinal,
                ))
            return columns

    def get_primary_key(self, table_name: str) -> Optional[PrimaryKeyMetadata]:
        """Discover the primary key constraint via ``pg_catalog``."""
        sql = """
            SELECT
                kcu.column_name,
                tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema, table_name))
            rows = cur.fetchall()
        if not rows:
            return None
        columns = [r[0] for r in rows]
        constraint_name = rows[0][1]
        return PrimaryKeyMetadata(columns=columns, constraint_name=constraint_name)

    def get_foreign_keys(self, table_name: str) -> list[ForeignKeyMetadata]:
        """Discover foreign keys via ``information_schema``."""
        sql = """
            SELECT
                tc.constraint_name,
                kcu.column_name,
                ccu.table_name  AS referenced_table,
                ccu.column_name AS referenced_column,
                ccu.table_schema AS referenced_schema
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            JOIN information_schema.referential_constraints rc
                ON tc.constraint_name = rc.constraint_name
                AND tc.table_schema = rc.constraint_schema
            JOIN information_schema.constraint_column_usage ccu
                ON rc.unique_constraint_name = ccu.constraint_name
                AND rc.unique_constraint_schema = ccu.constraint_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
              AND tc.constraint_type = 'FOREIGN KEY'
            ORDER BY tc.constraint_name, kcu.ordinal_position
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema, table_name))
            rows = cur.fetchall()

        # Group by constraint name (composite FKs span multiple rows)
        fk_map: dict[str, dict[str, Any]] = {}
        for constraint_name, col, ref_table, ref_col, ref_schema in rows:
            if constraint_name not in fk_map:
                fk_map[constraint_name] = {
                    "columns": [],
                    "referenced_table": ref_table,
                    "referenced_columns": [],
                    "referenced_schema": ref_schema,
                }
            fk_map[constraint_name]["columns"].append(col)
            fk_map[constraint_name]["referenced_columns"].append(ref_col)

        return [
            ForeignKeyMetadata(
                constraint_name=name,
                columns=info["columns"],
                referenced_table=info["referenced_table"],
                referenced_columns=info["referenced_columns"],
                referenced_schema=info["referenced_schema"],
            )
            for name, info in fk_map.items()
        ]

    def get_identity_columns(self, table_name: str) -> list[ColumnMetadata]:
        """Return columns with ``is_identity = 'YES'``."""
        sql = """
            SELECT
                column_name,
                data_type,
                character_maximum_length,
                numeric_precision,
                numeric_scale,
                is_nullable,
                column_default,
                ordinal_position,
                identity_generation
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND is_identity = 'YES'
            ORDER BY ordinal_position
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema, table_name))
            result: list[ColumnMetadata] = []
            for row in cur.fetchall():
                (
                    col_name, data_type, max_len, num_prec, num_scale,
                    is_nullable_str, default_val, ordinal, identity_gen,
                ) = row
                result.append(ColumnMetadata(
                    name=col_name,
                    data_type=data_type,
                    max_length=max_len,
                    numeric_precision=num_prec,
                    numeric_scale=num_scale,
                    is_nullable=(is_nullable_str == "YES"),
                    is_identity=True,
                    identity_generation=(
                        IdentityStrategy.ALWAYS
                        if identity_gen == "ALWAYS"
                        else IdentityStrategy.BY_DEFAULT
                    ),
                    default_value=default_val,
                    ordinal_position=ordinal,
                ))
            return result

    def get_sequences(self) -> list[SequenceMetadata]:
        """Return all sequences (identity-owned and standalone)."""
        sql = """
            SELECT
                s.sequence_name,
                s.start_value::bigint,
                s.increment::bigint,
                s.cache_value::bigint,
                pg_get_serial_sequence(c.table_name, c.column_name) IS NOT NULL AS is_identity,
                c.table_name AS assoc_table,
                c.column_name AS assoc_column
            FROM information_schema.sequences s
            LEFT JOIN information_schema.columns c
                ON c.column_default LIKE '%%' || s.sequence_name || '%%'
                AND c.table_schema = s.sequence_schema
            WHERE s.sequence_schema = %s
            ORDER BY s.sequence_name
        """
        # Simpler, more robust approach using pg_catalog
        sql = """
            SELECT
                seq.relname AS sequence_name,
                s.start_value,
                s.increment_by,
                s.cache_value,
                d.refobjid IS NOT NULL AS is_identity,
                tab.relname AS assoc_table,
                a.attname AS assoc_column
            FROM pg_class seq
            JOIN pg_namespace ns ON ns.oid = seq.relnamespace
            JOIN pg_sequences s ON s.schemaname = ns.nspname AND s.sequencename = seq.relname
            LEFT JOIN pg_depend d
                ON d.objid = seq.oid
                AND d.deptype IN ('a', 'i')
                AND d.classid = 'pg_class'::regclass
            LEFT JOIN pg_class tab
                ON tab.oid = d.refobjid
            LEFT JOIN pg_attribute a
                ON a.attrelid = d.refobjid
                AND a.attnum = d.refobjsubid
            WHERE seq.relkind = 'S'
              AND ns.nspname = %s
            ORDER BY seq.relname
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema,))
            sequences: list[SequenceMetadata] = []
            seen: set[str] = set()
            for row in cur.fetchall():
                seq_name, start_val, increment, cache, is_ident, assoc_table, assoc_col = row
                if seq_name in seen:
                    continue
                seen.add(seq_name)

                # Fetch last_value
                last_val: Optional[int] = None
                try:
                    cur2 = self._ensure_connected().cursor()
                    cur2.execute(f"SELECT last_value FROM {_qualified(self.schema, seq_name)}")
                    last_val = cur2.fetchone()[0]
                    cur2.close()
                except Exception:
                    logger.debug("Could not read last_value for sequence %s", seq_name)

                sequences.append(SequenceMetadata(
                    name=seq_name,
                    last_value=last_val,
                    start_value=int(start_val) if start_val is not None else 1,
                    increment=int(increment) if increment is not None else 1,
                    cache_size=int(cache) if cache is not None else 1,
                    is_identity_sequence=bool(is_ident),
                    associated_table=assoc_table,
                    associated_column=assoc_col,
                ))
            return sequences

    def get_triggers(self, table_name: str) -> list[TriggerMetadata]:
        """Discover triggers from ``information_schema.triggers``."""
        sql = """
            SELECT
                trigger_name,
                event_manipulation,
                action_timing,
                action_statement
            FROM information_schema.triggers
            WHERE trigger_schema = %s
              AND event_object_table = %s
            ORDER BY trigger_name, event_manipulation
        """
        with self._cursor() as cur:
            cur.execute(sql, (self.schema, table_name))
            triggers: list[TriggerMetadata] = []
            seen: set[str] = set()
            for trigger_name, event, timing, body in cur.fetchall():
                # A trigger with multiple events appears as separate rows;
                # collapse them into one entry with a comma-separated event.
                if trigger_name in seen:
                    # Append event to existing trigger
                    for t in triggers:
                        if t.name == trigger_name:
                            t.event = f"{t.event},{event}"
                            break
                    continue
                seen.add(trigger_name)
                triggers.append(TriggerMetadata(
                    name=trigger_name,
                    table_name=table_name,
                    event=event,
                    timing=timing,
                    body=body,
                ))
            return triggers

    def get_row_count(self, table_name: str) -> int:
        """Return the exact row count for *table_name*."""
        fq = _qualified(self.schema, table_name)
        with self._cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {fq}")  # noqa: S608
            return cur.fetchone()[0]

    def get_max_primary_key(self, table_name: str, pk_column: str) -> Optional[int]:
        """Return the maximum PK value, or ``None`` if the table is empty."""
        fq = _qualified(self.schema, table_name)
        col = _quote_ident(pk_column)
        with self._cursor() as cur:
            cur.execute(f"SELECT MAX({col}) FROM {fq}")  # noqa: S608
            result = cur.fetchone()
            return result[0] if result else None

    # ----------------------------------------------------- data access

    def stream_rows(
        self,
        table_name: str,
        columns: list[str],
        pk_column: Optional[str] = None,
        start_pk: Optional[int] = None,
        end_pk: Optional[int] = None,
        batch_size: int = 5000,
    ) -> Generator[list[dict[str, Any]], None, None]:
        """Stream rows using keyset pagination when a PK is available.

        Falls back to a server-side cursor when no PK column is provided.
        """
        fq = _qualified(self.schema, table_name)
        col_list = ", ".join(_quote_ident(c) for c in columns)

        if pk_column:
            yield from self._stream_keyset(fq, columns, col_list, pk_column, start_pk, end_pk, batch_size)
        else:
            yield from self._stream_server_cursor(fq, columns, col_list, batch_size)

    def _stream_keyset(
        self,
        fq_table: str,
        columns: list[str],
        col_list: str,
        pk_column: str,
        start_pk: Optional[int],
        end_pk: Optional[int],
        batch_size: int,
    ) -> Generator[list[dict[str, Any]], None, None]:
        """Keyset pagination: WHERE pk > last_pk ORDER BY pk LIMIT batch_size."""
        pk_col = _quote_ident(pk_column)
        last_pk = start_pk if start_pk is not None else -1

        while True:
            conditions = [f"{pk_col} > %s"]
            params: list[Any] = [last_pk]
            if end_pk is not None:
                conditions.append(f"{pk_col} <= %s")
                params.append(end_pk)

            where = " AND ".join(conditions)
            sql = f"SELECT {col_list} FROM {fq_table} WHERE {where} ORDER BY {pk_col} LIMIT %s"  # noqa: S608
            params.append(batch_size)

            with self._cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()

            if not rows:
                break

            batch = [dict(zip(columns, row)) for row in rows]
            last_pk = batch[-1][pk_column]
            yield batch

            if len(rows) < batch_size:
                break

    def _stream_server_cursor(
        self,
        fq_table: str,
        columns: list[str],
        col_list: str,
        batch_size: int,
    ) -> Generator[list[dict[str, Any]], None, None]:
        """Fallback streaming via a server-side (named) cursor."""
        sql = f"SELECT {col_list} FROM {fq_table}"  # noqa: S608
        with self._named_cursor() as cur:
            cur.execute(sql)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield [dict(zip(columns, row)) for row in rows]

    def stream_primary_keys(
        self,
        table_name: str,
        pk_column: str,
        batch_size: int = 10000,
    ) -> Generator[list[int], None, None]:
        """Yield sorted PK values in batches using keyset pagination."""
        fq = _qualified(self.schema, table_name)
        pk_col = _quote_ident(pk_column)
        last_pk = -1

        while True:
            sql = f"SELECT {pk_col} FROM {fq} WHERE {pk_col} > %s ORDER BY {pk_col} LIMIT %s"  # noqa: S608
            with self._cursor() as cur:
                cur.execute(sql, (last_pk, batch_size))
                rows = cur.fetchall()
            if not rows:
                break
            pks = [r[0] for r in rows]
            last_pk = pks[-1]
            yield pks
            if len(rows) < batch_size:
                break

    def fetch_rows_by_keys(
        self,
        table_name: str,
        columns: list[str],
        pk_column: str,
        pk_values: list[Any],
    ) -> list[dict[str, Any]]:
        """Fetch specific rows by primary key values."""
        if not pk_values:
            return []
        fq = _qualified(self.schema, table_name)
        col_list = ", ".join(_quote_ident(c) for c in columns)
        pk_col = _quote_ident(pk_column)
        placeholders = ", ".join(["%s"] * len(pk_values))
        sql = f"SELECT {col_list} FROM {fq} WHERE {pk_col} IN ({placeholders}) ORDER BY {pk_col}"  # noqa: S608

        with self._cursor() as cur:
            cur.execute(sql, tuple(pk_values))
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    # ------------------------------------------------- data mutation

    def insert_batch(
        self,
        table_name: str,
        columns: list[str],
        rows: list[tuple[Any, ...]],
        identity_strategy: IdentityStrategy = IdentityStrategy.NONE,
    ) -> int:
        """Insert rows using ``execute_values`` for performance.

        When *identity_strategy* is :attr:`IdentityStrategy.ALWAYS`, the
        ``OVERRIDING SYSTEM VALUE`` clause is injected so that explicit
        identity values are accepted by the server.
        """
        if not rows:
            return 0
        fq = _qualified(self.schema, table_name)
        col_list = ", ".join(_quote_ident(c) for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))

        overriding = ""
        if identity_strategy is IdentityStrategy.ALWAYS:
            overriding = " OVERRIDING SYSTEM VALUE"
        elif identity_strategy is IdentityStrategy.BY_DEFAULT:
            overriding = " OVERRIDING USER VALUE" if False else ""  # BY DEFAULT accepts explicit values by default

        sql = f"INSERT INTO {fq} ({col_list}){overriding} VALUES ({placeholders})"

        with self._cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=1000)
            return len(rows)

    def update_batch(
        self,
        table_name: str,
        pk_columns: list[str],
        update_columns: list[str],
        rows: list[tuple[Any, ...]],
    ) -> int:
        """Update rows by primary key.

        Each tuple in *rows* contains values for ``update_columns + pk_columns``
        in that order.
        """
        if not rows:
            return 0
        fq = _qualified(self.schema, table_name)
        set_clause = ", ".join(f"{_quote_ident(c)} = %s" for c in update_columns)
        where_clause = " AND ".join(f"{_quote_ident(c)} = %s" for c in pk_columns)
        sql = f"UPDATE {fq} SET {set_clause} WHERE {where_clause}"

        with self._cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=1000)
            return len(rows)

    def delete_batch(
        self,
        table_name: str,
        pk_columns: list[str],
        pk_values: list[tuple[Any, ...]],
    ) -> int:
        """Delete rows by primary key."""
        if not pk_values:
            return 0
        fq = _qualified(self.schema, table_name)
        where_clause = " AND ".join(f"{_quote_ident(c)} = %s" for c in pk_columns)
        sql = f"DELETE FROM {fq} WHERE {where_clause}"

        with self._cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, pk_values, page_size=1000)
            return len(pk_values)

    # --------------------------------------------- transaction management

    def begin_transaction(self) -> None:
        """Begin an explicit transaction by leaving autocommit mode."""
        conn = self._ensure_connected()
        if conn.autocommit:
            conn.set_session(autocommit=False)
        # psycopg2 starts a transaction implicitly on the next statement

    def commit(self) -> None:
        """Commit the current transaction."""
        conn = self._ensure_connected()
        conn.commit()

    def rollback_transaction(self) -> None:
        """Roll back the current transaction and restore autocommit."""
        conn = self._ensure_connected()
        conn.rollback()

    # ------------------------------------------- bulk load (COPY)

    def supports_bulk_load(self) -> bool:
        """PostgreSQL supports ``COPY`` for fast bulk loading."""
        return True

    def bulk_load(
        self,
        table_name: str,
        file_path: str,
        columns: list[str],
        identity_override: bool = False,
    ) -> int:
        """Bulk-load a CSV file using ``COPY ... FROM STDIN``."""
        fq = _qualified(self.schema, table_name)
        col_list = ", ".join(_quote_ident(c) for c in columns)
        copy_sql = f"COPY {fq} ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"

        conn = self._ensure_connected()
        count = 0
        with open(file_path, "r", encoding="utf-8") as f:
            # Count rows for reporting (header excluded)
            reader = csv.reader(f)
            next(reader, None)  # skip header
            count = sum(1 for _ in reader)
            f.seek(0)

            cur = conn.cursor()
            try:
                if identity_override:
                    cur.execute(f"ALTER TABLE {fq} ALTER COLUMN {_quote_ident(columns[0])} SET GENERATED BY DEFAULT")
                cur.copy_expert(copy_sql, f)
            finally:
                if identity_override:
                    cur.execute(f"ALTER TABLE {fq} ALTER COLUMN {_quote_ident(columns[0])} SET GENERATED ALWAYS")
                cur.close()

        return count

    # ---------------------------------------------- trigger management

    def disable_triggers(self, table_name: str) -> None:
        """Disable all triggers on *table_name*.

        Requires superuser or table owner privileges.
        """
        fq = _qualified(self.schema, table_name)
        logger.info("Disabling triggers on %s", fq)
        with self._cursor() as cur:
            cur.execute(f"ALTER TABLE {fq} DISABLE TRIGGER ALL")

    def enable_triggers(self, table_name: str) -> None:
        """Re-enable all triggers on *table_name*."""
        fq = _qualified(self.schema, table_name)
        logger.info("Enabling triggers on %s", fq)
        with self._cursor() as cur:
            cur.execute(f"ALTER TABLE {fq} ENABLE TRIGGER ALL")

    # ------------------------------------------- identity / sequence reset

    def reset_identity(self, table_name: str, column_name: str, new_value: int) -> None:
        """Reset an identity column's next generated value.

        Uses ``ALTER TABLE ... ALTER COLUMN ... RESTART WITH``.
        """
        fq = _qualified(self.schema, table_name)
        col = _quote_ident(column_name)
        sql = f"ALTER TABLE {fq} ALTER COLUMN {col} RESTART WITH {int(new_value)}"
        logger.info("Resetting identity %s.%s to %d", table_name, column_name, new_value)
        with self._cursor() as cur:
            cur.execute(sql)

    def reset_sequence(self, sequence_name: str, new_value: int) -> None:
        """Reset a standalone sequence's next value."""
        fq = _qualified(self.schema, sequence_name)
        sql = f"ALTER SEQUENCE {fq} RESTART WITH {int(new_value)}"
        logger.info("Resetting sequence %s to %d", sequence_name, new_value)
        with self._cursor() as cur:
            cur.execute(sql)

    # --------------------------------------------------- utility

    def execute(self, sql: str, params: Optional[tuple[Any, ...]] = None) -> Any:
        """Execute a single SQL statement."""
        with self._cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return cur.rowcount

    def execute_many(self, sql: str, params_list: list[tuple[Any, ...]]) -> int:
        """Execute a SQL statement once per parameter set."""
        if not params_list:
            return 0
        with self._cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, params_list, page_size=1000)
            return len(params_list)

    def get_version(self) -> str:
        """Return the PostgreSQL server version string."""
        with self._cursor() as cur:
            cur.execute("SELECT version()")
            return cur.fetchone()[0]

    def get_encoding(self) -> Optional[str]:
        """Return the database character set encoding."""
        with self._cursor() as cur:
            cur.execute("SHOW server_encoding")
            return cur.fetchone()[0]
