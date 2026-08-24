"""Tests for dbmigrate.models — enums, dataclasses, and auto-derived fields."""

from __future__ import annotations

from datetime import datetime

from dbmigrate.models import (
    AutomationMode,
    BatchStatus,
    ColumnMapping,
    ColumnMetadata,
    ComparisonStrategy,
    DatabaseMetadata,
    ForeignKeyMetadata,
    IdentityStrategy,
    MigrationBatch,
    MigrationManifest,
    MigrationMode,
    MigrationOperation,
    MigrationTablePlan,
    PrimaryKeyMetadata,
    SequenceMetadata,
    TableDelta,
    TableMetadata,
    TriggerMetadata,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestIdentityStrategy:
    def test_values(self):
        assert IdentityStrategy.BY_DEFAULT.value == "by_default"
        assert IdentityStrategy.ALWAYS.value == "always"
        assert IdentityStrategy.NONE.value == "none"

    def test_from_value(self):
        assert IdentityStrategy("by_default") is IdentityStrategy.BY_DEFAULT


class TestMigrationOperation:
    def test_values(self):
        assert MigrationOperation.INSERT.value == "insert"
        assert MigrationOperation.UPDATE.value == "update"
        assert MigrationOperation.DELETE.value == "delete"
        assert MigrationOperation.NO_ACTION.value == "no_action"


class TestComparisonStrategy:
    def test_values(self):
        assert ComparisonStrategy.ROW_COUNT.value == "row_count"
        assert ComparisonStrategy.PRIMARY_KEY.value == "primary_key"
        assert ComparisonStrategy.CHECKSUM.value == "checksum"
        assert ComparisonStrategy.TIMESTAMP.value == "timestamp"
        assert ComparisonStrategy.AUTO.value == "auto"


class TestMigrationMode:
    def test_values(self):
        assert MigrationMode.SYNC.value == "sync"
        assert MigrationMode.ROLLBACK.value == "rollback"


class TestAutomationMode:
    def test_values(self):
        assert AutomationMode.SUPERVISED.value == "supervised"
        assert AutomationMode.AUTO_NON_PROD.value == "auto_non_prod"
        assert AutomationMode.AUTO_APPROVED.value == "auto_approved"


class TestBatchStatus:
    def test_values(self):
        assert BatchStatus.PENDING.value == "pending"
        assert BatchStatus.RUNNING.value == "running"
        assert BatchStatus.COMPLETED.value == "completed"
        assert BatchStatus.FAILED.value == "failed"
        assert BatchStatus.SKIPPED.value == "skipped"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestColumnMetadata:
    def test_defaults(self):
        col = ColumnMetadata(name="id", data_type="INTEGER")
        assert col.is_nullable is True
        assert col.is_identity is False
        assert col.identity_generation is None
        assert col.max_length is None
        assert col.ordinal_position == 0

    def test_full_creation(self):
        col = ColumnMetadata(
            name="id", data_type="INTEGER", is_nullable=False,
            is_identity=True, identity_generation=IdentityStrategy.ALWAYS,
            max_length=None, numeric_precision=10, numeric_scale=0,
            default_value=None, ordinal_position=1,
        )
        assert col.is_identity is True
        assert col.identity_generation == IdentityStrategy.ALWAYS


class TestPrimaryKeyMetadata:
    def test_single_column_not_composite(self):
        pk = PrimaryKeyMetadata(columns=["id"])
        assert pk.is_composite is False

    def test_multi_column_is_composite(self):
        pk = PrimaryKeyMetadata(columns=["col_a", "col_b"])
        assert pk.is_composite is True

    def test_empty_columns(self):
        pk = PrimaryKeyMetadata(columns=[])
        assert pk.is_composite is False

    def test_post_init_overrides_default(self):
        # Even if is_composite is explicitly set to False, __post_init__ recalculates
        pk = PrimaryKeyMetadata(columns=["a", "b"], is_composite=False)
        assert pk.is_composite is True


class TestForeignKeyMetadata:
    def test_creation(self):
        fk = ForeignKeyMetadata(
            constraint_name="fk_test",
            columns=["parent_id"],
            referenced_table="parent",
            referenced_columns=["id"],
        )
        assert fk.constraint_name == "fk_test"
        assert fk.referenced_schema is None


class TestSequenceMetadata:
    def test_defaults(self):
        seq = SequenceMetadata(name="seq_test")
        assert seq.start_value == 1
        assert seq.increment == 1
        assert seq.cache_size == 1
        assert seq.is_identity_sequence is False


class TestTriggerMetadata:
    def test_defaults(self):
        trigger = TriggerMetadata(name="trg_test", table_name="t", event="INSERT", timing="BEFORE")
        assert trigger.body is None
        assert trigger.referenced_tables == []


class TestTableMetadata:
    def test_defaults(self):
        table = TableMetadata(name="t", schema="public")
        assert table.columns == []
        assert table.foreign_keys == []
        assert table.row_count == 0
        assert table.primary_key is None
        assert table.identity_column is None


class TestDatabaseMetadata:
    def test_defaults(self):
        db = DatabaseMetadata(engine="postgresql", schema="public")
        assert db.tables == {}
        assert db.standalone_sequences == []
        assert db.encoding is None
        assert db.version is None


class TestColumnMapping:
    def test_source_only(self):
        m = ColumnMapping(
            source_column="legacy_col", target_column="legacy_col",
            source_type="VARCHAR", target_type="", source_only=True,
        )
        assert m.source_only is True
        assert m.target_only is False

    def test_target_only(self):
        m = ColumnMapping(
            source_column="new_col", target_column="new_col",
            source_type="", target_type="integer", target_only=True,
        )
        assert m.target_only is True

    def test_requires_cast(self):
        m = ColumnMapping(
            source_column="val", target_column="val",
            source_type="CLOB", target_type="text", requires_cast=True,
        )
        assert m.requires_cast is True


class TestTableDelta:
    def test_defaults(self):
        d = TableDelta(table_name="t")
        assert d.insert_pks == []
        assert d.update_pks == []
        assert d.delete_pks == []
        assert d.unchanged_count == 0


class TestMigrationManifest:
    def test_creation(self):
        manifest = MigrationManifest(
            migration_id="m-001",
            profile_name="test",
            mode=MigrationMode.SYNC,
        )
        assert manifest.tables == []
        assert manifest.batches == []
        assert manifest.total_rows == 0
        assert manifest.insert_tables == 0
        assert isinstance(manifest.created_at, datetime)


class TestMigrationBatch:
    def test_defaults(self):
        b = MigrationBatch(
            batch_id="b-001", table_name="t",
            operation=MigrationOperation.INSERT,
        )
        assert b.status == BatchStatus.PENDING
        assert b.started_at is None
        assert b.error is None


class TestValidationResult:
    def test_defaults(self):
        r = ValidationResult(table_name="t", check_name="check", passed=True)
        assert r.severity == "error"
        assert r.message is None
