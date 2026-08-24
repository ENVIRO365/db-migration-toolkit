"""Tests for dbmigrate.migration — DeltaPlanner and CircuitBreaker."""

from __future__ import annotations

import pytest

from dbmigrate.comparison import SchemaComparisonResult
from dbmigrate.comparison.dependency import DependencyGraph
from dbmigrate.migration import (
    CircuitBreaker,
    CircuitBreakerError,
    DeltaPlanner,
    ProfileConfig,
)
from dbmigrate.models import (
    ColumnMapping,
    ForeignKeyMetadata,
    IdentityStrategy,
    MigrationMode,
    MigrationOperation,
    PrimaryKeyMetadata,
    TableDelta,
    TableMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _comparison(common: list[str], mappings: dict[str, list[ColumnMapping]] | None = None) -> SchemaComparisonResult:
    return SchemaComparisonResult(
        source_engine="db2",
        target_engine="postgresql",
        common_tables=common,
        column_mappings=mappings or {t: [ColumnMapping(
            source_column="id", target_column="id",
            source_type="INTEGER", target_type="integer",
        )] for t in common},
    )


def _dep_graph(tables: dict[str, TableMetadata]) -> DependencyGraph:
    return DependencyGraph.build(tables)


# ---------------------------------------------------------------------------
# DeltaPlanner
# ---------------------------------------------------------------------------


class TestDeltaPlannerSyncMode:
    def test_inserts_created_deletes_skipped(self):
        comparison = _comparison(["t1"])
        deltas = {
            "t1": TableDelta(
                table_name="t1",
                insert_pks=[1, 2, 3],
                update_pks=[4],
                delete_pks=[10, 11],  # should be ignored in SYNC mode
                source_count=100,
                target_count=97,
            ),
        }
        tables = {"t1": TableMetadata(name="t1", schema="public")}
        config = ProfileConfig(batch_size=1000, mode=MigrationMode.SYNC)

        manifest = DeltaPlanner().plan(comparison, deltas, _dep_graph(tables), config)

        ops = {p.operation for p in manifest.tables}
        assert MigrationOperation.INSERT in ops
        assert MigrationOperation.UPDATE in ops
        assert MigrationOperation.DELETE not in ops  # SYNC mode excludes DELETEs

    def test_no_action_when_no_changes(self):
        comparison = _comparison(["t1"])
        deltas = {
            "t1": TableDelta(table_name="t1", source_count=50, target_count=50, unchanged_count=50),
        }
        tables = {"t1": TableMetadata(name="t1", schema="public")}
        config = ProfileConfig(mode=MigrationMode.SYNC)

        manifest = DeltaPlanner().plan(comparison, deltas, _dep_graph(tables), config)
        assert len(manifest.tables) == 1
        assert manifest.tables[0].operation == MigrationOperation.NO_ACTION


class TestDeltaPlannerRollbackMode:
    def test_deletes_included(self):
        comparison = _comparison(["t1"])
        deltas = {
            "t1": TableDelta(
                table_name="t1",
                insert_pks=[1],
                delete_pks=[10, 11],
                source_count=100,
                target_count=101,
            ),
        }
        tables = {"t1": TableMetadata(name="t1", schema="public")}
        config = ProfileConfig(batch_size=1000, mode=MigrationMode.ROLLBACK)

        manifest = DeltaPlanner().plan(comparison, deltas, _dep_graph(tables), config)

        ops = {p.operation for p in manifest.tables}
        assert MigrationOperation.DELETE in ops


class TestBatchCreation:
    def test_batches_split_correctly(self):
        comparison = _comparison(["t1"])
        deltas = {
            "t1": TableDelta(
                table_name="t1",
                insert_pks=list(range(1, 12)),  # 11 rows
                source_count=11,
                target_count=0,
            ),
        }
        tables = {"t1": TableMetadata(name="t1", schema="public")}
        config = ProfileConfig(batch_size=5, mode=MigrationMode.SYNC)

        manifest = DeltaPlanner().plan(comparison, deltas, _dep_graph(tables), config)
        insert_batches = [b for b in manifest.batches if b.operation == MigrationOperation.INSERT]
        assert len(insert_batches) == 3  # 5 + 5 + 1
        assert insert_batches[0].row_count == 5
        assert insert_batches[1].row_count == 5
        assert insert_batches[2].row_count == 1

    def test_batch_pk_range(self):
        comparison = _comparison(["t1"])
        deltas = {
            "t1": TableDelta(table_name="t1", insert_pks=[10, 20, 30], source_count=3, target_count=0),
        }
        tables = {"t1": TableMetadata(name="t1", schema="public")}
        config = ProfileConfig(batch_size=2, mode=MigrationMode.SYNC)

        manifest = DeltaPlanner().plan(comparison, deltas, _dep_graph(tables), config)
        batches = [b for b in manifest.batches if b.operation == MigrationOperation.INSERT]
        assert batches[0].start_pk == 10
        assert batches[0].end_pk == 20
        assert batches[1].start_pk == 30
        assert batches[1].end_pk == 30


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_trips_on_consecutive_failures(self):
        cb = CircuitBreaker(max_consecutive_failures=3)
        cb.record_failure("t1")
        cb.record_failure("t1")
        with pytest.raises(CircuitBreakerError) as exc_info:
            cb.record_failure("t1")
        assert exc_info.value.table_name == "t1"
        assert exc_info.value.consecutive_failures == 3

    def test_success_resets_counter(self):
        cb = CircuitBreaker(max_consecutive_failures=3)
        cb.record_failure("t1")
        cb.record_failure("t1")
        cb.record_success("t1")  # resets
        cb.record_failure("t1")  # 1st failure again
        # Should not trip yet
        assert not cb.is_tripped("t1")

    def test_is_tripped(self):
        cb = CircuitBreaker(max_consecutive_failures=2)
        assert not cb.is_tripped("t1")
        cb.record_failure("t1")
        assert not cb.is_tripped("t1")
        try:
            cb.record_failure("t1")
        except CircuitBreakerError:
            pass
        assert cb.is_tripped("t1")

    def test_overall_failure_rate(self):
        cb = CircuitBreaker(max_consecutive_failures=100, max_overall_failure_rate=0.5)
        # Need at least 5 batches for rate check
        cb.record_success("t1")
        cb.record_success("t1")
        cb.record_failure("t1")
        cb.record_failure("t1")
        # 4 total, 2 failed = 0.5 rate but < 5 batches, no trip
        # 5th batch as failure: 3/5 = 0.6 >= 0.5 → trips
        with pytest.raises(CircuitBreakerError):
            cb.record_failure("t1")

    def test_overall_failure_rate_property(self):
        cb = CircuitBreaker()
        assert cb.overall_failure_rate == 0.0
        cb.record_success("t1")
        cb.record_failure("t2")
        assert cb.overall_failure_rate == 0.5
