"""Checkpoint store for migration resume capability.

Uses SQLite to track batch completion status, enabling
interrupted migrations to resume from the last completed batch.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dbmigrate.models import BatchStatus, MigrationBatch, MigrationOperation

logger = logging.getLogger(__name__)

_BATCH_SCHEMA = """\
CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    migration_id    TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    operation       TEXT NOT NULL,
    row_count       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT,
    completed_at    TEXT,
    checksum        TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_migration
    ON batches (migration_id);
CREATE INDEX IF NOT EXISTS idx_batches_table
    ON batches (migration_id, table_name);
"""

_DDL_SCHEMA = """\
CREATE TABLE IF NOT EXISTS ddl_changes (
    ddl_change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT NOT NULL,
    change_type     TEXT NOT NULL,
    original_state  TEXT,
    changed_at      TEXT NOT NULL,
    restored_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ddl_table
    ON ddl_changes (table_name);
"""

_MIGRATION_SCHEMA = """\
CREATE TABLE IF NOT EXISTS migrations (
    migration_id    TEXT PRIMARY KEY,
    profile_name    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
);
"""


class CheckpointStore:
    """SQLite-backed store for migration checkpoint data.

    Each migration run gets its own SQLite database file under
    ``checkpoints/{profile_name}/{migration_id}.db``.  WAL mode
    is enabled for safe concurrent reads.

    Parameters
    ----------
    base_dir:
        Root directory for checkpoint databases.  Defaults to
        ``checkpoints/`` in the current working directory.
    """

    def __init__(self, base_dir: str = "checkpoints") -> None:
        self._base_dir = Path(base_dir)
        self._conn: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None

    # ---- lifecycle -------------------------------------------------------

    def create_migration(self, migration_id: str, profile_name: str) -> None:
        """Initialise a checkpoint database for a new migration run.

        Creates the directory structure, SQLite file, and schema tables.
        If the database already exists (resume scenario), it is reused.

        Parameters
        ----------
        migration_id:
            Unique identifier for the migration run.
        profile_name:
            Name of the migration profile (used as a subdirectory).
        """
        profile_dir = self._base_dir / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        db_path = profile_dir / f"{migration_id}.db"
        self._db_path = db_path

        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row

        # Enable WAL mode for concurrent safety
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._conn.executescript(_MIGRATION_SCHEMA)
        self._conn.executescript(_BATCH_SCHEMA)
        self._conn.executescript(_DDL_SCHEMA)

        # Record migration
        self._conn.execute(
            "INSERT OR IGNORE INTO migrations (migration_id, profile_name, created_at) "
            "VALUES (?, ?, ?)",
            (migration_id, profile_name, datetime.now(tz=timezone.utc).isoformat()),
        )

        logger.info(
            "Checkpoint store initialised at '%s' for migration '%s'",
            db_path,
            migration_id,
        )

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_connected(self) -> sqlite3.Connection:
        """Return the active connection, raising if not initialised."""
        if self._conn is None:
            raise RuntimeError(
                "CheckpointStore not initialised — call create_migration() first"
            )
        return self._conn

    # ---- batch tracking --------------------------------------------------

    def save_batch(self, batch: MigrationBatch) -> None:
        """Persist a batch record (insert or update).

        Parameters
        ----------
        batch:
            The batch to save.  Uses ``batch_id`` as the primary key.
        """
        conn = self._ensure_connected()
        conn.execute(
            "INSERT OR REPLACE INTO batches "
            "(batch_id, migration_id, table_name, operation, row_count, "
            " status, started_at, completed_at, checksum, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch.batch_id,
                batch.batch_id.split(":")[0],  # migration_id is first segment
                batch.table_name,
                batch.operation.value,
                batch.row_count,
                batch.status.value,
                batch.started_at.isoformat() if batch.started_at else None,
                batch.completed_at.isoformat() if batch.completed_at else None,
                batch.checksum,
                batch.error,
            ),
        )

    def get_pending_batches(self, migration_id: str) -> list[MigrationBatch]:
        """Return all batches with ``PENDING`` status for a migration.

        Parameters
        ----------
        migration_id:
            The migration run identifier.

        Returns
        -------
        list[MigrationBatch]
        """
        conn = self._ensure_connected()
        rows = conn.execute(
            "SELECT * FROM batches WHERE migration_id = ? AND status = 'pending' "
            "ORDER BY rowid",
            (migration_id,),
        ).fetchall()
        return [self._row_to_batch(r) for r in rows]

    def get_completed_batches(self, migration_id: str) -> list[MigrationBatch]:
        """Return all batches with ``COMPLETED`` status for a migration.

        Parameters
        ----------
        migration_id:
            The migration run identifier.

        Returns
        -------
        list[MigrationBatch]
        """
        conn = self._ensure_connected()
        rows = conn.execute(
            "SELECT * FROM batches WHERE migration_id = ? AND status = 'completed' "
            "ORDER BY rowid",
            (migration_id,),
        ).fetchall()
        return [self._row_to_batch(r) for r in rows]

    def get_last_completed_batch(
        self, migration_id: str, table_name: str
    ) -> Optional[MigrationBatch]:
        """Return the most recently completed batch for a table.

        Parameters
        ----------
        migration_id:
            The migration run identifier.
        table_name:
            Table name to look up.

        Returns
        -------
        MigrationBatch or None
        """
        conn = self._ensure_connected()
        row = conn.execute(
            "SELECT * FROM batches "
            "WHERE migration_id = ? AND table_name = ? AND status = 'completed' "
            "ORDER BY rowid DESC LIMIT 1",
            (migration_id, table_name),
        ).fetchone()
        return self._row_to_batch(row) if row else None

    def mark_batch_started(self, batch_id: str) -> None:
        """Update a batch to ``RUNNING`` status with a start timestamp.

        Parameters
        ----------
        batch_id:
            The batch identifier.
        """
        conn = self._ensure_connected()
        conn.execute(
            "UPDATE batches SET status = 'running', started_at = ? WHERE batch_id = ?",
            (datetime.now(tz=timezone.utc).isoformat(), batch_id),
        )

    def mark_batch_completed(
        self,
        batch_id: str,
        row_count: int,
        checksum: Optional[str] = None,
    ) -> None:
        """Update a batch to ``COMPLETED`` status.

        Parameters
        ----------
        batch_id:
            The batch identifier.
        row_count:
            Actual number of rows processed.
        checksum:
            Optional checksum of the batch data.
        """
        conn = self._ensure_connected()
        conn.execute(
            "UPDATE batches SET status = 'completed', completed_at = ?, "
            "row_count = ?, checksum = ? WHERE batch_id = ?",
            (datetime.now(tz=timezone.utc).isoformat(), row_count, checksum, batch_id),
        )

    def mark_batch_failed(self, batch_id: str, error: str) -> None:
        """Update a batch to ``FAILED`` status with an error message.

        Parameters
        ----------
        batch_id:
            The batch identifier.
        error:
            Error description.
        """
        conn = self._ensure_connected()
        conn.execute(
            "UPDATE batches SET status = 'failed', completed_at = ?, error = ? "
            "WHERE batch_id = ?",
            (datetime.now(tz=timezone.utc).isoformat(), error, batch_id),
        )

    # ---- DDL change tracking ---------------------------------------------

    def save_ddl_change(
        self, table_name: str, change_type: str, original_state: Optional[str]
    ) -> None:
        """Record a DDL change (e.g. trigger disable) for later restoration.

        Parameters
        ----------
        table_name:
            Table on which the DDL change was made.
        change_type:
            Type of change (e.g. ``"trigger_disable"``, ``"trigger_enable"``).
        original_state:
            Serialised original state so it can be restored.
        """
        conn = self._ensure_connected()
        conn.execute(
            "INSERT INTO ddl_changes (table_name, change_type, original_state, changed_at) "
            "VALUES (?, ?, ?, ?)",
            (table_name, change_type, original_state, datetime.now(tz=timezone.utc).isoformat()),
        )

    def get_unrestored_ddl_changes(self) -> list[dict]:
        """Return DDL changes that have not yet been restored.

        Returns
        -------
        list[dict]
            Each dict contains ``ddl_change_id``, ``table_name``,
            ``change_type``, ``original_state``, and ``changed_at``.
        """
        conn = self._ensure_connected()
        rows = conn.execute(
            "SELECT ddl_change_id, table_name, change_type, original_state, changed_at "
            "FROM ddl_changes WHERE restored_at IS NULL "
            "ORDER BY ddl_change_id",
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_ddl_restored(self, ddl_change_id: int) -> None:
        """Mark a DDL change as restored.

        Parameters
        ----------
        ddl_change_id:
            Primary key of the DDL change record.
        """
        conn = self._ensure_connected()
        conn.execute(
            "UPDATE ddl_changes SET restored_at = ? WHERE ddl_change_id = ?",
            (datetime.now(tz=timezone.utc).isoformat(), ddl_change_id),
        )

    # ---- status summary --------------------------------------------------

    def get_migration_status(self, migration_id: str) -> dict:
        """Return a summary of migration progress.

        Parameters
        ----------
        migration_id:
            The migration run identifier.

        Returns
        -------
        dict
            Keys: ``migration_id``, ``total_batches``, ``completed``,
            ``failed``, ``pending``, ``running``, ``skipped``,
            ``total_rows_processed``, ``tables_touched``,
            ``unrestored_ddl_changes``.
        """
        conn = self._ensure_connected()

        status_counts = conn.execute(
            "SELECT status, COUNT(*) as cnt, SUM(row_count) as rows "
            "FROM batches WHERE migration_id = ? GROUP BY status",
            (migration_id,),
        ).fetchall()

        summary: dict[str, int] = {
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
        }
        total_rows = 0
        total_batches = 0
        for row in status_counts:
            status = row["status"]
            count = row["cnt"]
            rows = row["rows"] or 0
            summary[status] = count
            total_batches += count
            if status == "completed":
                total_rows += rows

        tables_row = conn.execute(
            "SELECT COUNT(DISTINCT table_name) as cnt FROM batches "
            "WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        tables_touched = tables_row["cnt"] if tables_row else 0

        unrestored = len(self.get_unrestored_ddl_changes())

        return {
            "migration_id": migration_id,
            "total_batches": total_batches,
            "completed": summary["completed"],
            "failed": summary["failed"],
            "pending": summary["pending"],
            "running": summary["running"],
            "skipped": summary["skipped"],
            "total_rows_processed": total_rows,
            "tables_touched": tables_touched,
            "unrestored_ddl_changes": unrestored,
        }

    # ---- internal --------------------------------------------------------

    @staticmethod
    def _row_to_batch(row: sqlite3.Row) -> MigrationBatch:
        """Convert a SQLite row to a :class:`MigrationBatch`."""
        started = (
            datetime.fromisoformat(row["started_at"])
            if row["started_at"]
            else None
        )
        completed = (
            datetime.fromisoformat(row["completed_at"])
            if row["completed_at"]
            else None
        )
        return MigrationBatch(
            batch_id=row["batch_id"],
            table_name=row["table_name"],
            operation=MigrationOperation(row["operation"]),
            row_count=row["row_count"],
            status=BatchStatus(row["status"]),
            started_at=started,
            completed_at=completed,
            checksum=row["checksum"],
            error=row["error"],
        )


__all__ = ["CheckpointStore"]
