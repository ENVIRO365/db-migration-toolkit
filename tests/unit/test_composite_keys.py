"""Tests for composite/virtual PK support across the pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml
from pathlib import Path

from dbmigrate.config import ProfileConfig
from dbmigrate.models import (
    ColumnMapping,
    ComparisonStrategy,
    MigrationOperation,
    MigrationTablePlan,
    PrimaryKeyMetadata,
    TableDelta,
    TableMetadata,
    ColumnMetadata,
)
from dbmigrate.comparison import DeltaDetector


# ═══════════════════════════════════════════════════════════════════════════
# Config: virtual_pk validation
# ═══════════════════════════════════════════════════════════════════════════


class TestVirtualPkConfig:
    """Test ProfileConfig.virtual_pk field and validator."""

    def _make_config(self, virtual_pk=None, **overrides):
        data = {
            "name": "test",
            "source": {"type": "postgresql", "schema": "public", "dsn_env": "SRC"},
            "target": {"type": "db2", "schema": "WEALTHADAPTER", "dsn_env": "TGT"},
        }
        if virtual_pk is not None:
            data["virtual_pk"] = virtual_pk
        data.update(overrides)
        return ProfileConfig(**data)

    def test_default_empty(self):
        cfg = self._make_config()
        assert cfg.virtual_pk == {}

    def test_valid_single_column(self):
        cfg = self._make_config(virtual_pk={"incomingfile": ["id"]})
        assert cfg.virtual_pk == {"incomingfile": ["id"]}

    def test_valid_composite(self):
        cfg = self._make_config(
            virtual_pk={"recipient_emailgroup": ["recipientid", "emailgroupid"]}
        )
        assert cfg.virtual_pk == {"recipient_emailgroup": ["recipientid", "emailgroupid"]}

    def test_normalises_to_lowercase(self):
        cfg = self._make_config(
            virtual_pk={"MyTable": ["ColA", "COLB"]}
        )
        assert "mytable" in cfg.virtual_pk
        assert cfg.virtual_pk["mytable"] == ["cola", "colb"]

    def test_rejects_empty_column_list(self):
        with pytest.raises(Exception, match="at least one column"):
            self._make_config(virtual_pk={"bad_table": []})

    def test_multiple_tables(self):
        cfg = self._make_config(virtual_pk={
            "emailgroup_emailaddress": ["emailgroupid", "emailaddressid"],
            "role_accessright": ["roleid", "accessrightid"],
            "incomingfile": ["id"],
        })
        assert len(cfg.virtual_pk) == 3
        assert cfg.virtual_pk["emailgroup_emailaddress"] == ["emailgroupid", "emailaddressid"]


# ═══════════════════════════════════════════════════════════════════════════
# Models: MigrationTablePlan.pk_columns
# ═══════════════════════════════════════════════════════════════════════════


class TestMigrationTablePlanPkColumns:
    """Test the new pk_columns field on MigrationTablePlan."""

    def test_default_empty(self):
        plan = MigrationTablePlan(table_name="foo", operation=MigrationOperation.INSERT)
        assert plan.pk_columns == []

    def test_single_column(self):
        plan = MigrationTablePlan(
            table_name="foo", operation=MigrationOperation.INSERT,
            pk_columns=["id"],
        )
        assert plan.pk_columns == ["id"]

    def test_composite(self):
        plan = MigrationTablePlan(
            table_name="foo", operation=MigrationOperation.INSERT,
            pk_columns=["recipientid", "emailgroupid"],
        )
        assert plan.pk_columns == ["recipientid", "emailgroupid"]


# ═══════════════════════════════════════════════════════════════════════════
# DeltaDetector: composite key handling
# ═══════════════════════════════════════════════════════════════════════════


class TestDeltaDetectorComposite:
    """Test DeltaDetector with composite (multi-column) PKs."""

    def test_extract_pk_single(self):
        row = {"id": 42, "name": "foo"}
        result = DeltaDetector._extract_pk(row, ["id"])
        assert result == 42

    def test_extract_pk_composite(self):
        row = {"recipientid": 10, "emailgroupid": 20, "name": "foo"}
        result = DeltaDetector._extract_pk(row, ["recipientid", "emailgroupid"])
        assert result == (10, 20)

    def test_primary_key_strategy_composite(self):
        """Test delta detection with composite PKs using mocked adapters."""
        source_db = MagicMock()
        target_db = MagicMock()

        # Source has 3 composite keys; target has 2 (one overlaps, one is target-only)
        source_pks = [(1, 10), (2, 20), (3, 30)]
        target_pks = [(1, 10), (4, 40)]

        source_db.get_row_count.return_value = 3
        target_db.get_row_count.return_value = 2

        source_db.stream_primary_keys.return_value = iter([source_pks])
        target_db.stream_primary_keys.return_value = iter([target_pks])

        # For common PK (1,10), fetch identical rows (no update needed)
        source_db.fetch_rows_by_keys.return_value = [
            {"recipientid": 1, "emailgroupid": 10, "name": "Alice"}
        ]
        target_db.fetch_rows_by_keys.return_value = [
            {"recipientid": 1, "emailgroupid": 10, "name": "Alice"}
        ]

        detector = DeltaDetector()
        delta = detector.detect_delta(
            source_db=source_db,
            target_db=target_db,
            table_name="recipient_emailgroup",
            pk_columns=["recipientid", "emailgroupid"],
            columns=["recipientid", "emailgroupid", "name"],
            strategy=ComparisonStrategy.PRIMARY_KEY,
        )

        # (2,20) and (3,30) are in source only -> inserts
        assert sorted(delta.insert_pks) == [(2, 20), (3, 30)]
        # (4,40) is in target only -> deletes
        assert delta.delete_pks == [(4, 40)]
        # (1,10) is common and identical -> unchanged
        assert delta.unchanged_count == 1
        assert delta.update_pks == []

    def test_primary_key_strategy_composite_with_updates(self):
        """Test composite delta detection where common rows differ."""
        source_db = MagicMock()
        target_db = MagicMock()

        source_pks = [(1, 10), (2, 20)]
        target_pks = [(1, 10), (2, 20)]

        source_db.get_row_count.return_value = 2
        target_db.get_row_count.return_value = 2

        source_db.stream_primary_keys.return_value = iter([source_pks])
        target_db.stream_primary_keys.return_value = iter([target_pks])

        # Both common; row (1,10) is same, row (2,20) differs
        source_db.fetch_rows_by_keys.return_value = [
            {"recipientid": 1, "emailgroupid": 10, "name": "Alice"},
            {"recipientid": 2, "emailgroupid": 20, "name": "Bob_new"},
        ]
        target_db.fetch_rows_by_keys.return_value = [
            {"recipientid": 1, "emailgroupid": 10, "name": "Alice"},
            {"recipientid": 2, "emailgroupid": 20, "name": "Bob_old"},
        ]

        detector = DeltaDetector()
        delta = detector.detect_delta(
            source_db=source_db,
            target_db=target_db,
            table_name="recipient_emailgroup",
            pk_columns=["recipientid", "emailgroupid"],
            columns=["recipientid", "emailgroupid", "name"],
            strategy=ComparisonStrategy.PRIMARY_KEY,
        )

        assert delta.insert_pks == []
        assert delta.delete_pks == []
        assert delta.update_pks == [(2, 20)]
        assert delta.unchanged_count == 1

    def test_single_column_backward_compat(self):
        """Ensure single-column PK still works via pk_columns=[\"id\"]."""
        source_db = MagicMock()
        target_db = MagicMock()

        source_db.get_row_count.return_value = 2
        target_db.get_row_count.return_value = 1

        source_db.stream_primary_keys.return_value = iter([[1, 2]])
        target_db.stream_primary_keys.return_value = iter([[1]])

        # Common: id=1 (same data)
        source_db.fetch_rows_by_keys.return_value = [{"id": 1, "val": "a"}]
        target_db.fetch_rows_by_keys.return_value = [{"id": 1, "val": "a"}]

        detector = DeltaDetector()
        delta = detector.detect_delta(
            source_db=source_db,
            target_db=target_db,
            table_name="adapterconfig",
            pk_columns=["id"],
            columns=["id", "val"],
            strategy=ComparisonStrategy.PRIMARY_KEY,
        )

        assert delta.insert_pks == [2]
        assert delta.delete_pks == []
        assert delta.unchanged_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator: _resolve_pk_columns
# ═══════════════════════════════════════════════════════════════════════════


class TestResolvePkColumns:
    """Test the orchestrator's _resolve_pk_columns logic."""

    def _make_orchestrator(self, virtual_pk=None):
        from dbmigrate.orchestration import PipelineOrchestrator

        cfg = ProfileConfig(
            name="test",
            source={"type": "postgresql", "schema": "public", "dsn_env": "SRC"},
            target={"type": "db2", "schema": "WEALTHADAPTER", "dsn_env": "TGT"},
            virtual_pk=virtual_pk or {},
        )
        return PipelineOrchestrator(cfg, profiles_dir=Path("/tmp"))

    def test_real_single_pk(self):
        orch = self._make_orchestrator()
        tbl_meta = MagicMock()
        tbl_meta.primary_key = PrimaryKeyMetadata(columns=["id"])
        result = orch._resolve_pk_columns("adapterconfig", tbl_meta)
        assert result == ["id"]

    def test_real_composite_pk(self):
        orch = self._make_orchestrator()
        tbl_meta = MagicMock()
        tbl_meta.primary_key = PrimaryKeyMetadata(columns=["recipientid", "emailgroupid"])
        result = orch._resolve_pk_columns("recipient_emailgroup", tbl_meta)
        assert result == ["recipientid", "emailgroupid"]

    def test_virtual_pk_overrides_real(self):
        """Virtual PK takes priority even if a real PK exists."""
        orch = self._make_orchestrator(
            virtual_pk={"mytable": ["col_a", "col_b"]}
        )
        tbl_meta = MagicMock()
        tbl_meta.primary_key = PrimaryKeyMetadata(columns=["id"])
        result = orch._resolve_pk_columns("mytable", tbl_meta)
        assert result == ["col_a", "col_b"]

    def test_virtual_pk_for_no_real_pk(self):
        """Virtual PK used when table has no real PK."""
        orch = self._make_orchestrator(
            virtual_pk={"role_accessright": ["roleid", "accessrightid"]}
        )
        tbl_meta = MagicMock()
        tbl_meta.primary_key = None
        result = orch._resolve_pk_columns("role_accessright", tbl_meta)
        assert result == ["roleid", "accessrightid"]

    def test_no_pk_returns_empty(self):
        """No real PK and no virtual PK -> empty list (table skipped)."""
        orch = self._make_orchestrator()
        tbl_meta = MagicMock()
        tbl_meta.primary_key = None
        result = orch._resolve_pk_columns("unknown_table", tbl_meta)
        assert result == []

    def test_none_metadata(self):
        """Handles None metadata gracefully."""
        orch = self._make_orchestrator()
        result = orch._resolve_pk_columns("nonexistent", None)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# BatchExecutor: _get_pk_columns
# ═══════════════════════════════════════════════════════════════════════════


class TestBatchExecutorPkColumns:
    """Test BatchExecutor._get_pk_columns static method."""

    def test_from_plan_pk_columns(self):
        from dbmigrate.migration import BatchExecutor

        plan = MigrationTablePlan(
            table_name="t", operation=MigrationOperation.INSERT,
            pk_columns=["recipientid", "emailgroupid"],
        )
        assert BatchExecutor._get_pk_columns(plan) == ["recipientid", "emailgroupid"]

    def test_fallback_to_first_mapping(self):
        from dbmigrate.migration import BatchExecutor

        plan = MigrationTablePlan(
            table_name="t", operation=MigrationOperation.INSERT,
            pk_columns=[],
            column_mappings=[
                ColumnMapping(source_column="id", target_column="id",
                              source_type="int", target_type="int"),
            ],
        )
        assert BatchExecutor._get_pk_columns(plan) == ["id"]

    def test_empty_fallback(self):
        from dbmigrate.migration import BatchExecutor

        plan = MigrationTablePlan(
            table_name="t", operation=MigrationOperation.INSERT,
            pk_columns=[],
            column_mappings=[],
        )
        assert BatchExecutor._get_pk_columns(plan) == []
