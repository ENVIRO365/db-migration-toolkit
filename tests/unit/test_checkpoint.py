"""Tests for dbmigrate.checkpoint — SQLite-backed checkpoint store."""

from __future__ import annotations

import pytest

from dbmigrate.checkpoint import CheckpointStore
from dbmigrate.models import BatchStatus, MigrationBatch, MigrationOperation


class TestCreateMigration:
    def test_creates_db_file(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("mig-001", "test-profile")
        db_path = tmp_path / "test-profile" / "mig-001.db"
        assert db_path.exists()
        store.close()

    def test_reopen_existing(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("mig-001", "test-profile")
        store.close()
        # Reopen should not raise
        store2 = CheckpointStore(base_dir=str(tmp_path))
        store2.create_migration("mig-001", "test-profile")
        store2.close()


class TestSaveBatchAndRetrieve:
    def test_save_and_get_pending(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("mig-001", "prof")

        batch = MigrationBatch(
            batch_id="mig-001:t1:insert:0",
            table_name="t1",
            operation=MigrationOperation.INSERT,
            row_count=100,
            status=BatchStatus.PENDING,
        )
        store.save_batch(batch)

        pending = store.get_pending_batches("mig-001")
        assert len(pending) == 1
        assert pending[0].batch_id == "mig-001:t1:insert:0"
        assert pending[0].table_name == "t1"
        assert pending[0].operation == MigrationOperation.INSERT
        assert pending[0].status == BatchStatus.PENDING
        store.close()


class TestBatchLifecycle:
    def test_mark_started_then_completed(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")

        batch = MigrationBatch(
            batch_id="m1:t:insert:0", table_name="t",
            operation=MigrationOperation.INSERT, row_count=50,
        )
        store.save_batch(batch)

        store.mark_batch_started("m1:t:insert:0")
        store.mark_batch_completed("m1:t:insert:0", row_count=50, checksum="abc123")

        completed = store.get_completed_batches("m1")
        assert len(completed) == 1
        assert completed[0].status == BatchStatus.COMPLETED
        assert completed[0].row_count == 50
        assert completed[0].checksum == "abc123"
        assert completed[0].started_at is not None
        assert completed[0].completed_at is not None
        store.close()

    def test_mark_failed(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")

        batch = MigrationBatch(
            batch_id="m1:t:insert:0", table_name="t",
            operation=MigrationOperation.INSERT,
        )
        store.save_batch(batch)
        store.mark_batch_started("m1:t:insert:0")
        store.mark_batch_failed("m1:t:insert:0", "connection lost")

        pending = store.get_pending_batches("m1")
        assert len(pending) == 0  # No longer pending

        # Re-fetch via status summary
        status = store.get_migration_status("m1")
        assert status["failed"] == 1
        store.close()


class TestGetLastCompletedBatch:
    def test_returns_none_when_empty(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")
        assert store.get_last_completed_batch("m1", "t1") is None
        store.close()

    def test_returns_last(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")

        for i in range(3):
            b = MigrationBatch(
                batch_id=f"m1:t1:insert:{i}", table_name="t1",
                operation=MigrationOperation.INSERT, row_count=10,
            )
            store.save_batch(b)
            store.mark_batch_completed(f"m1:t1:insert:{i}", row_count=10)

        last = store.get_last_completed_batch("m1", "t1")
        assert last is not None
        assert last.batch_id == "m1:t1:insert:2"
        store.close()


class TestDDLChangeTracking:
    def test_save_and_get_unrestored(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")

        store.save_ddl_change("t1", "trigger_disable", "CREATE TRIGGER ...")
        store.save_ddl_change("t2", "trigger_disable", "CREATE TRIGGER t2 ...")

        changes = store.get_unrestored_ddl_changes()
        assert len(changes) == 2
        assert changes[0]["table_name"] == "t1"
        assert changes[0]["change_type"] == "trigger_disable"
        assert changes[0]["original_state"] == "CREATE TRIGGER ..."
        store.close()

    def test_mark_restored_removes_from_unrestored(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")

        store.save_ddl_change("t1", "trigger_disable", "original")
        changes = store.get_unrestored_ddl_changes()
        assert len(changes) == 1

        store.mark_ddl_restored(changes[0]["ddl_change_id"])
        assert store.get_unrestored_ddl_changes() == []
        store.close()


class TestMigrationStatus:
    def test_summary(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")

        # 2 pending, 1 completed
        for i in range(3):
            b = MigrationBatch(
                batch_id=f"m1:t:insert:{i}", table_name="t",
                operation=MigrationOperation.INSERT, row_count=100,
            )
            store.save_batch(b)

        store.mark_batch_completed("m1:t:insert:0", row_count=100)

        status = store.get_migration_status("m1")
        assert status["migration_id"] == "m1"
        assert status["total_batches"] == 3
        assert status["completed"] == 1
        assert status["pending"] == 2
        assert status["total_rows_processed"] == 100
        assert status["tables_touched"] == 1
        assert status["unrestored_ddl_changes"] == 0
        store.close()

    def test_unrestored_ddl_in_summary(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        store.create_migration("m1", "p")
        store.save_ddl_change("t1", "trigger_disable", None)

        status = store.get_migration_status("m1")
        assert status["unrestored_ddl_changes"] == 1
        store.close()


class TestEnsureConnected:
    def test_raises_without_init(self, tmp_path):
        store = CheckpointStore(base_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="not initialised"):
            store.save_batch(MigrationBatch(
                batch_id="x", table_name="t",
                operation=MigrationOperation.INSERT,
            ))
