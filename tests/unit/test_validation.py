"""Tests for dbmigrate.validation — MigrationValidator pre_validate."""

from __future__ import annotations

from dbmigrate.models import (
    ColumnMapping,
    ColumnMetadata,
    DatabaseMetadata,
    IdentityStrategy,
    MigrationManifest,
    MigrationMode,
    MigrationOperation,
    MigrationTablePlan,
    PrimaryKeyMetadata,
    TableMetadata,
)
from dbmigrate.validation import MigrationValidator


def _db(tables: dict[str, TableMetadata], encoding: str | None = None) -> DatabaseMetadata:
    return DatabaseMetadata(engine="test", schema="public", tables=tables, encoding=encoding)


def _table(name: str, columns: list[ColumnMetadata], **kwargs) -> TableMetadata:
    return TableMetadata(
        name=name, schema="public", columns=columns,
        primary_key=PrimaryKeyMetadata(columns=[columns[0].name] if columns else []),
        **kwargs,
    )


def _manifest(plans: list[MigrationTablePlan]) -> MigrationManifest:
    return MigrationManifest(
        migration_id="test", profile_name="test",
        mode=MigrationMode.SYNC, tables=plans,
    )


class TestPreValidateCompatible:
    def test_all_pass(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db({"t1": _table("t1", cols)})
        tgt = _db({"t1": _table("t1", cols)})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.INSERT,
            column_mappings=[ColumnMapping(
                source_column="id", target_column="id",
                source_type="integer", target_type="integer",
            )],
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        failures = [r for r in results if not r.passed]
        assert len(failures) == 0


class TestPreValidateTypeMismatch:
    def test_cast_warning(self):
        src_cols = [ColumnMetadata(name="id", data_type="INTEGER")]
        tgt_cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db({"t1": _table("t1", src_cols)})
        tgt = _db({"t1": _table("t1", tgt_cols)})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.INSERT,
            column_mappings=[ColumnMapping(
                source_column="id", target_column="id",
                source_type="INTEGER", target_type="integer",
                requires_cast=True,
            )],
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        warnings = [r for r in results if r.check_name == "column_type_compatible"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert warnings[0].passed is True  # casts are warnings, not failures


class TestPreValidateVarcharTruncation:
    def test_truncation_risk_detected(self):
        src_cols = [ColumnMetadata(name="name", data_type="VARCHAR", max_length=200)]
        tgt_cols = [ColumnMetadata(name="name", data_type="VARCHAR", max_length=100)]
        src = _db({"t1": _table("t1", src_cols)}, encoding="utf-8")
        tgt = _db({"t1": _table("t1", tgt_cols)})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.INSERT,
            column_mappings=[ColumnMapping(
                source_column="name", target_column="name",
                source_type="VARCHAR", target_type="VARCHAR",
            )],
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        truncation = [r for r in results if r.check_name == "varchar_length_sufficient"]
        assert len(truncation) == 1
        assert truncation[0].passed is False
        assert "truncation" in truncation[0].message.lower()

    def test_no_warning_without_multibyte(self):
        src_cols = [ColumnMetadata(name="name", data_type="VARCHAR", max_length=200)]
        tgt_cols = [ColumnMetadata(name="name", data_type="VARCHAR", max_length=100)]
        src = _db({"t1": _table("t1", src_cols)}, encoding="latin1")
        tgt = _db({"t1": _table("t1", tgt_cols)})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.INSERT,
            column_mappings=[ColumnMapping(
                source_column="name", target_column="name",
                source_type="VARCHAR", target_type="VARCHAR",
            )],
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        truncation = [r for r in results if r.check_name == "varchar_length_sufficient"]
        assert len(truncation) == 0


class TestPreValidateNoAction:
    def test_skips_no_action_tables(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db({"t1": _table("t1", cols)})
        tgt = _db({"t1": _table("t1", cols)})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.NO_ACTION,
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        assert len(results) == 0


class TestPreValidateMissingTable:
    def test_source_missing(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db({})
        tgt = _db({"t1": _table("t1", cols)})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.INSERT,
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        errors = [r for r in results if not r.passed]
        assert any("source" in r.check_name.lower() for r in errors)

    def test_target_missing(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db({"t1": _table("t1", cols)})
        tgt = _db({})
        plan = MigrationTablePlan(
            table_name="t1", operation=MigrationOperation.INSERT,
        )
        results = MigrationValidator().pre_validate(src, tgt, _manifest([plan]))
        errors = [r for r in results if not r.passed]
        assert any("target" in r.check_name.lower() for r in errors)
