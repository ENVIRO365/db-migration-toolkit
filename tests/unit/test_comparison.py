"""Tests for dbmigrate.comparison — SchemaComparator."""

from __future__ import annotations

from dbmigrate.comparison import SchemaComparator, SchemaComparisonResult
from dbmigrate.models import (
    ColumnMapping,
    ColumnMetadata,
    DatabaseMetadata,
    IdentityStrategy,
    PrimaryKeyMetadata,
    SequenceMetadata,
    TableMetadata,
)


def _db(engine: str, tables: dict[str, TableMetadata]) -> DatabaseMetadata:
    return DatabaseMetadata(engine=engine, schema="public", tables=tables)


def _table(name: str, columns: list[ColumnMetadata], **kwargs) -> TableMetadata:
    return TableMetadata(name=name, schema="public", columns=columns, **kwargs)


class TestMatchingSchemas:
    def test_no_differences(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db("db2", {"t1": _table("t1", cols)})
        tgt = _db("pg", {"t1": _table("t1", cols)})
        result = SchemaComparator().compare(src, tgt)
        assert result.source_only_tables == []
        assert result.target_only_tables == []
        assert result.common_tables == ["t1"]
        assert not result.has_differences


class TestSourceOnlyTable:
    def test_detected(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db("db2", {"t1": _table("t1", cols), "t2": _table("t2", cols)})
        tgt = _db("pg", {"t1": _table("t1", cols)})
        result = SchemaComparator().compare(src, tgt)
        assert result.source_only_tables == ["t2"]
        assert result.has_differences


class TestTargetOnlyTable:
    def test_detected(self):
        cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db("db2", {"t1": _table("t1", cols)})
        tgt = _db("pg", {"t1": _table("t1", cols), "extra": _table("extra", cols)})
        result = SchemaComparator().compare(src, tgt)
        assert result.target_only_tables == ["extra"]


class TestColumnTypeMismatch:
    def test_detected(self):
        src_cols = [ColumnMetadata(name="val", data_type="CLOB")]
        tgt_cols = [ColumnMetadata(name="val", data_type="text")]
        src = _db("db2", {"t1": _table("t1", src_cols)})
        tgt = _db("pg", {"t1": _table("t1", tgt_cols)})
        result = SchemaComparator().compare(src, tgt)
        assert "t1" in result.table_differences
        diffs = result.table_differences["t1"].column_diffs
        assert len(diffs) == 1
        assert diffs[0].difference_type == "type_mismatch"
        assert diffs[0].source_type == "CLOB"
        assert diffs[0].target_type == "text"


class TestSourceOnlyColumn:
    def test_detected(self):
        src_cols = [
            ColumnMetadata(name="id", data_type="integer"),
            ColumnMetadata(name="legacy", data_type="varchar"),
        ]
        tgt_cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db("db2", {"t1": _table("t1", src_cols)})
        tgt = _db("pg", {"t1": _table("t1", tgt_cols)})
        result = SchemaComparator().compare(src, tgt)
        diffs = result.table_differences["t1"].column_diffs
        source_only = [d for d in diffs if d.difference_type == "source_only"]
        assert len(source_only) == 1
        assert source_only[0].column_name == "legacy"


class TestIdentityStrategyDifference:
    def test_detected(self):
        id_always = ColumnMetadata(name="id", data_type="integer", is_identity=True,
                                   identity_generation=IdentityStrategy.ALWAYS)
        id_default = ColumnMetadata(name="id", data_type="integer", is_identity=True,
                                    identity_generation=IdentityStrategy.BY_DEFAULT)
        src = _db("db2", {"t1": _table("t1", [id_always], identity_column=id_always)})
        tgt = _db("pg", {"t1": _table("t1", [id_default], identity_column=id_default)})
        result = SchemaComparator().compare(src, tgt)
        assert "t1" in result.table_differences
        assert result.table_differences["t1"].identity_diff is not None
        assert "always" in result.table_differences["t1"].identity_diff
        assert "by_default" in result.table_differences["t1"].identity_diff

    def test_source_has_identity_target_does_not(self):
        id_col = ColumnMetadata(name="id", data_type="integer", is_identity=True,
                                identity_generation=IdentityStrategy.ALWAYS)
        plain_col = ColumnMetadata(name="id", data_type="integer")
        src = _db("db2", {"t1": _table("t1", [id_col], identity_column=id_col)})
        tgt = _db("pg", {"t1": _table("t1", [plain_col])})
        result = SchemaComparator().compare(src, tgt)
        assert "source has identity" in result.table_differences["t1"].identity_diff


class TestColumnMappingGeneration:
    def test_common_columns_mapped(self):
        src_cols = [ColumnMetadata(name="id", data_type="INTEGER"),
                    ColumnMetadata(name="name", data_type="VARCHAR")]
        tgt_cols = [ColumnMetadata(name="id", data_type="integer"),
                    ColumnMetadata(name="name", data_type="character varying")]
        src = _db("db2", {"t1": _table("t1", src_cols)})
        tgt = _db("pg", {"t1": _table("t1", tgt_cols)})
        result = SchemaComparator().compare(src, tgt)
        mappings = result.column_mappings["t1"]
        assert len(mappings) == 2
        id_mapping = [m for m in mappings if m.source_column == "id"][0]
        assert id_mapping.requires_cast is False  # comparison is case-insensitive
        assert id_mapping.source_only is False
        assert id_mapping.target_only is False

    def test_source_only_column_in_mapping(self):
        src_cols = [ColumnMetadata(name="id", data_type="integer"),
                    ColumnMetadata(name="extra", data_type="varchar")]
        tgt_cols = [ColumnMetadata(name="id", data_type="integer")]
        src = _db("db2", {"t1": _table("t1", src_cols)})
        tgt = _db("pg", {"t1": _table("t1", tgt_cols)})
        result = SchemaComparator().compare(src, tgt)
        mappings = result.column_mappings["t1"]
        extra = [m for m in mappings if m.source_column == "extra"][0]
        assert extra.source_only is True

    def test_target_only_column_in_mapping(self):
        src_cols = [ColumnMetadata(name="id", data_type="integer")]
        tgt_cols = [ColumnMetadata(name="id", data_type="integer"),
                    ColumnMetadata(name="new_col", data_type="text")]
        src = _db("db2", {"t1": _table("t1", src_cols)})
        tgt = _db("pg", {"t1": _table("t1", tgt_cols)})
        result = SchemaComparator().compare(src, tgt)
        mappings = result.column_mappings["t1"]
        new = [m for m in mappings if m.target_column == "new_col"][0]
        assert new.target_only is True
