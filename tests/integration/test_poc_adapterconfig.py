"""Gate 4 — Single-table PoC: adapterconfig.

Exercises the full pipeline against real PG and DB2 databases:
  1. Connect via adapters
  2. Discover schema metadata
  3. Compare schemas
  4. Detect delta (PK set difference)
  5. Create migration plan
  6. Checkpoint store lifecycle
  7. Validation

This test is READ-ONLY — no data is written to either database.
Run with:  .venv/bin/python -m pytest tests/integration/test_poc_adapterconfig.py -v -s
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ── Connection parameters (from docs/databases.yml) ──────────────────────
PG_DSN = (
    "postgresql://cis-lh-adapter-user:2cCsjYD7iUhQUZDi"
    "@postgres.pre.investments.momentum.co.za:5432/cis-lh-adapter-dev"
)
PG_SCHEMA = "wealthadapter"

DB2_DSN = (
    "DATABASE=WEALTH;HOSTNAME=mmidb2wlhdev203.metmom.mmih.biz;"
    "PORT=60000;PROTOCOL=TCPIP;UID=svclhmig;PWD=4rA8whVbKW1e0mhOzeKDYdAU"
)
DB2_SCHEMA = "wealthadapter"

TABLE = "adapterconfig"


# ── Skip if network unavailable ──────────────────────────────────────────
def _can_reach_pg() -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


def _can_reach_db2() -> bool:
    try:
        import ibm_db
        dsn = DB2_DSN
        conn = ibm_db.connect(dsn, "", "")
        ibm_db.close(conn)
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _can_reach_pg(), reason="PG unreachable")
requires_db2 = pytest.mark.skipif(not _can_reach_db2(), reason="DB2 unreachable")
requires_both = pytest.mark.skipif(
    not (_can_reach_pg() and _can_reach_db2()),
    reason="Both PG and DB2 required",
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_adapter():
    from dbmigrate.database.postgresql import PostgreSQLAdapter
    adapter = PostgreSQLAdapter(dsn=PG_DSN, schema=PG_SCHEMA)
    adapter.connect()
    yield adapter
    adapter.close()


@pytest.fixture(scope="module")
def db2_adapter():
    from dbmigrate.database.db2 import DB2Adapter
    adapter = DB2Adapter(dsn=DB2_DSN, schema=DB2_SCHEMA)
    adapter.connect()
    yield adapter
    adapter.close()


@pytest.fixture
def checkpoint_dir(tmp_path):
    return tmp_path / "checkpoints"


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_table_metadata(adapter, table_name):
    """Build a TableMetadata from an adapter."""
    from dbmigrate.models import TableMetadata
    cols = adapter.get_columns(table_name)
    pk = adapter.get_primary_key(table_name)
    fks = adapter.get_foreign_keys(table_name)
    triggers = adapter.get_triggers(table_name)
    count = adapter.get_row_count(table_name)

    # Find identity column
    id_cols = adapter.get_identity_columns(table_name)
    identity_col = id_cols[0] if id_cols else None

    return TableMetadata(
        name=table_name,
        schema=adapter.schema,
        columns=cols,
        primary_key=pk,
        foreign_keys=fks,
        sequences=[],
        triggers=triggers,
        row_count=count,
        identity_column=identity_col,
    )


def _build_db_metadata(adapter, engine_name, table_name):
    """Build a single-table DatabaseMetadata."""
    from dbmigrate.models import DatabaseMetadata
    table_meta = _build_table_metadata(adapter, table_name)
    return DatabaseMetadata(
        engine=engine_name,
        schema=adapter.schema,
        tables={table_name: table_meta},
        standalone_sequences=[],
    )


# ══════════════════════════════════════════════════════════════════════════
# Stage 1: Connection & Version
# ══════════════════════════════════════════════════════════════════════════

@requires_pg
class TestPGConnection:
    def test_version(self, pg_adapter):
        version = pg_adapter.get_version()
        assert "PostgreSQL" in version
        print(f"\n  PG version: {version[:60]}")

    def test_encoding(self, pg_adapter):
        enc = pg_adapter.get_encoding()
        assert enc is not None
        print(f"\n  PG encoding: {enc}")


@requires_db2
class TestDB2Connection:
    def test_version(self, db2_adapter):
        version = db2_adapter.get_version()
        assert version != "unknown"
        print(f"\n  DB2 version: {version}")

    def test_encoding(self, db2_adapter):
        enc = db2_adapter.get_encoding()
        print(f"\n  DB2 codepage: {enc}")


# ══════════════════════════════════════════════════════════════════════════
# Stage 2: Schema Discovery
# ══════════════════════════════════════════════════════════════════════════

@requires_pg
class TestPGDiscovery:
    def test_tables_found(self, pg_adapter):
        tables = pg_adapter.get_tables()
        assert TABLE in tables
        print(f"\n  PG tables ({len(tables)}): {', '.join(sorted(tables))}")

    def test_columns(self, pg_adapter):
        cols = pg_adapter.get_columns(TABLE)
        assert len(cols) == 3  # id, key, value
        names = [c.name for c in cols]
        assert "id" in names
        assert "key" in names
        assert "value" in names
        for c in cols:
            print(f"\n  PG col: {c.name:20s} {c.data_type:20s} nullable={c.is_nullable} identity={c.is_identity}")

    def test_primary_key(self, pg_adapter):
        pk = pg_adapter.get_primary_key(TABLE)
        assert pk is not None
        assert pk.columns == ["id"]
        assert not pk.is_composite
        print(f"\n  PG PK: {pk.columns} constraint={pk.constraint_name}")

    def test_identity(self, pg_adapter):
        from dbmigrate.models import IdentityStrategy
        id_cols = pg_adapter.get_identity_columns(TABLE)
        assert len(id_cols) == 1
        assert id_cols[0].name == "id"
        assert id_cols[0].identity_generation == IdentityStrategy.BY_DEFAULT
        print(f"\n  PG identity: {id_cols[0].name} = {id_cols[0].identity_generation}")

    def test_foreign_keys(self, pg_adapter):
        fks = pg_adapter.get_foreign_keys(TABLE)
        assert len(fks) == 0  # adapterconfig has no FKs
        print(f"\n  PG FKs: {len(fks)}")

    def test_row_count(self, pg_adapter):
        count = pg_adapter.get_row_count(TABLE)
        assert count > 0
        print(f"\n  PG row count: {count}")

    def test_max_pk(self, pg_adapter):
        max_pk = pg_adapter.get_max_primary_key(TABLE, "id")
        assert max_pk is not None
        assert max_pk > 0
        print(f"\n  PG max PK: {max_pk}")


@requires_db2
class TestDB2Discovery:
    def test_tables_found(self, db2_adapter):
        tables = db2_adapter.get_tables()
        assert TABLE in tables
        print(f"\n  DB2 tables ({len(tables)}): {', '.join(sorted(tables))}")

    def test_columns(self, db2_adapter):
        cols = db2_adapter.get_columns(TABLE)
        assert len(cols) == 3
        names = [c.name for c in cols]
        assert "id" in names
        assert "key" in names
        assert "value" in names
        for c in cols:
            print(f"\n  DB2 col: {c.name:20s} {c.data_type:20s} nullable={c.is_nullable} identity={c.is_identity}")

    def test_primary_key(self, db2_adapter):
        pk = db2_adapter.get_primary_key(TABLE)
        assert pk is not None
        assert pk.columns == ["id"]
        print(f"\n  DB2 PK: {pk.columns} constraint={pk.constraint_name}")

    def test_identity(self, db2_adapter):
        from dbmigrate.models import IdentityStrategy
        id_cols = db2_adapter.get_identity_columns(TABLE)
        assert len(id_cols) == 1
        assert id_cols[0].name == "id"
        assert id_cols[0].identity_generation == IdentityStrategy.BY_DEFAULT
        print(f"\n  DB2 identity: {id_cols[0].name} = {id_cols[0].identity_generation}")

    def test_row_count(self, db2_adapter):
        count = db2_adapter.get_row_count(TABLE)
        assert count > 0
        print(f"\n  DB2 row count: {count}")

    def test_max_pk(self, db2_adapter):
        max_pk = db2_adapter.get_max_primary_key(TABLE, "id")
        assert max_pk is not None
        assert max_pk > 0
        print(f"\n  DB2 max PK: {max_pk}")


# ══════════════════════════════════════════════════════════════════════════
# Stage 3: Full Metadata Discovery
# ══════════════════════════════════════════════════════════════════════════

@requires_both
class TestFullDiscovery:
    """Build TableMetadata for adapterconfig from both databases."""

    def test_build_table_metadata(self, pg_adapter, db2_adapter):
        pg_meta = _build_table_metadata(pg_adapter, TABLE)
        db2_meta = _build_table_metadata(db2_adapter, TABLE)

        print(f"\n  PG:  {pg_meta.name} — {len(pg_meta.columns)} cols, {pg_meta.row_count} rows, "
              f"PK={pg_meta.primary_key.columns if pg_meta.primary_key else None}")
        print(f"  DB2: {db2_meta.name} — {len(db2_meta.columns)} cols, {db2_meta.row_count} rows, "
              f"PK={db2_meta.primary_key.columns if db2_meta.primary_key else None}")

        # Same number of columns
        assert len(pg_meta.columns) == len(db2_meta.columns) == 3
        # Same PK
        assert pg_meta.primary_key.columns == db2_meta.primary_key.columns
        # PG has more rows
        assert pg_meta.row_count >= db2_meta.row_count

        delta = pg_meta.row_count - db2_meta.row_count
        print(f"  Delta: PG has {delta} more rows than DB2")


# ══════════════════════════════════════════════════════════════════════════
# Stage 4: Schema Comparison
# ══════════════════════════════════════════════════════════════════════════

@requires_both
class TestSchemaComparison:
    """Use SchemaComparator to compare adapterconfig across PG and DB2."""

    def test_comparator(self, pg_adapter, db2_adapter):
        from dbmigrate.comparison import SchemaComparator

        source = _build_db_metadata(pg_adapter, "postgresql", TABLE)
        target = _build_db_metadata(db2_adapter, "db2", TABLE)

        comparator = SchemaComparator()
        result = comparator.compare(source, target)

        print(f"\n  Comparison result:")
        print(f"    Common tables: {result.common_tables}")
        print(f"    Source-only: {result.source_only_tables}")
        print(f"    Target-only: {result.target_only_tables}")

        assert TABLE in result.common_tables
        assert len(result.source_only_tables) == 0
        assert len(result.target_only_tables) == 0

        # Check column mappings for adapterconfig
        mappings = result.column_mappings.get(TABLE, [])
        print(f"    Column mappings: {len(mappings)}")
        for m in mappings:
            print(f"      {m.source_column:15s} -> {m.target_column:15s}  "
                  f"src_type={m.source_type:20s} tgt_type={m.target_type:20s} "
                  f"cast={m.requires_cast}")

        # All 3 columns should map
        assert len(mappings) == 3


# ══════════════════════════════════════════════════════════════════════════
# Stage 5: PK Delta Detection
# ══════════════════════════════════════════════════════════════════════════

@requires_both
class TestDeltaDetection:
    """Stream PKs from both sides and compute the set difference."""

    def test_pk_delta(self, pg_adapter, db2_adapter):
        # Collect all PKs from PG (source)
        pg_pks: set[int] = set()
        for batch in pg_adapter.stream_primary_keys(TABLE, ["id"], batch_size=1000):
            pg_pks.update(batch)

        # Collect all PKs from DB2 (target)
        db2_pks: set[int] = set()
        for batch in db2_adapter.stream_primary_keys(TABLE, ["id"], batch_size=1000):
            db2_pks.update(batch)

        # Delta
        source_only = sorted(pg_pks - db2_pks)   # in PG but not DB2 -> need INSERT into DB2
        target_only = sorted(db2_pks - pg_pks)    # in DB2 but not PG -> stale in DB2

        print(f"\n  PG PKs: {len(pg_pks)} (range {min(pg_pks)}-{max(pg_pks)})")
        print(f"  DB2 PKs: {len(db2_pks)} (range {min(db2_pks)}-{max(db2_pks)})")
        print(f"  Source-only (need INSERT): {len(source_only)} rows")
        print(f"  Target-only (stale): {len(target_only)} rows")

        if source_only:
            print(f"    INSERT IDs (first 20): {source_only[:20]}")
        if target_only:
            print(f"    Stale IDs (first 20): {target_only[:20]}")

        # For rollback mode (PG->DB2), source_only are the rows to INSERT
        assert len(source_only) > 0, "Expected PG to have rows not in DB2"

    def test_fetch_delta_rows(self, pg_adapter, db2_adapter):
        """Fetch the actual row data for the delta PKs."""
        # Get delta
        pg_pks: set[int] = set()
        for batch in pg_adapter.stream_primary_keys(TABLE, ["id"], batch_size=1000):
            pg_pks.update(batch)
        db2_pks: set[int] = set()
        for batch in db2_adapter.stream_primary_keys(TABLE, ["id"], batch_size=1000):
            db2_pks.update(batch)

        source_only = sorted(pg_pks - db2_pks)
        if not source_only:
            pytest.skip("No delta rows")

        # Fetch actual data for these PKs from PG
        columns = ["id", "key", "value"]
        rows = pg_adapter.fetch_rows_by_keys(TABLE, columns, ["id"], source_only[:10])

        print(f"\n  Sample delta rows from PG ({len(rows)} of {len(source_only)}):")
        for row in rows[:5]:
            val_preview = str(row.get("value", ""))[:60]
            print(f"    id={row['id']} key={row.get('key', 'NULL'):30s} value={val_preview}...")

        assert len(rows) > 0
        assert all(r["id"] in source_only for r in rows)

    def test_delta_detector(self, pg_adapter, db2_adapter):
        """Use the DeltaDetector class to detect delta programmatically."""
        from dbmigrate.comparison import DeltaDetector

        detector = DeltaDetector()
        delta = detector.detect_delta(
            source_db=pg_adapter,
            target_db=db2_adapter,
            table_name=TABLE,
            pk_columns=["id"],
            columns=["id", "key", "value"],
        )

        print(f"\n  DeltaDetector result for {TABLE}:")
        print(f"    insert_pks: {len(delta.insert_pks)} rows")
        print(f"    delete_pks: {len(delta.delete_pks)} rows")
        print(f"    update_pks: {len(delta.update_pks)} rows")
        print(f"    source_count: {delta.source_count}")
        print(f"    target_count: {delta.target_count}")

        assert len(delta.insert_pks) > 0


# ══════════════════════════════════════════════════════════════════════════
# Stage 6: Migration Plan Generation
# ══════════════════════════════════════════════════════════════════════════

@requires_both
class TestMigrationPlan:
    """Generate a migration plan using DeltaPlanner."""

    def test_plan_creation(self, pg_adapter, db2_adapter):
        from dbmigrate.models import MigrationMode
        from dbmigrate.comparison import DeltaDetector, SchemaComparator
        from dbmigrate.comparison.dependency import DependencyGraph
        from dbmigrate.migration import DeltaPlanner
        from dbmigrate.config import load_profile
        from pathlib import Path

        # Build metadata
        source = _build_db_metadata(pg_adapter, "postgresql", TABLE)
        target = _build_db_metadata(db2_adapter, "db2", TABLE)

        # Compare schemas
        comparator = SchemaComparator()
        comparison = comparator.compare(source, target)

        # Detect delta
        detector = DeltaDetector()
        delta = detector.detect_delta(
            source_db=pg_adapter,
            target_db=db2_adapter,
            table_name=TABLE,
            pk_columns=["id"],
            columns=["id", "key", "value"],
        )

        # Build dependency graph (adapterconfig has no FKs)
        dep_graph = DependencyGraph.build(source.tables)

        # Load profile and build planner config
        profiles_dir = Path(__file__).resolve().parents[2] / "profiles"
        profile = load_profile("wealth-adapter-rollback", profiles_dir)

        from dbmigrate.migration import ProfileConfig as PlannerConfig
        planner_config = PlannerConfig(
            batch_size=profile.migration.batch_size,
            mode=profile.migration.mode,
            skip_tables=profile.skip_tables or [],
        )

        # Plan
        planner = DeltaPlanner()
        manifest = planner.plan(
            comparison=comparison,
            deltas={TABLE: delta},
            dep_graph=dep_graph,
            config=planner_config,
        )

        print(f"\n  Migration manifest:")
        print(f"    ID: {manifest.migration_id}")
        print(f"    Mode: {manifest.mode}")
        print(f"    Tables: {len(manifest.tables)}")
        print(f"    Total rows: {manifest.total_rows}")
        print(f"    Batches: {len(manifest.batches)}")
        for tp in manifest.tables:
            print(f"    Table: {tp.table_name} op={tp.operation} "
                  f"rows={tp.row_count} level={tp.dependency_level}")
        for i, b in enumerate(manifest.batches[:5]):
            print(f"    Batch {i}: table={b.table_name} op={b.operation} "
                  f"rows={b.row_count} start_pk={b.start_pk} end_pk={b.end_pk}")

        assert len(manifest.tables) >= 1
        assert manifest.total_rows > 0


# ══════════════════════════════════════════════════════════════════════════
# Stage 7: Checkpoint Store
# ══════════════════════════════════════════════════════════════════════════

class TestCheckpointIntegration:
    """Prove checkpoint store lifecycle with realistic data."""

    def test_full_lifecycle(self, checkpoint_dir):
        from dbmigrate.checkpoint import CheckpointStore
        from dbmigrate.models import MigrationBatch, MigrationOperation, BatchStatus

        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        store = CheckpointStore(str(checkpoint_dir))

        try:
            # Create migration
            migration_id = "poc-test-001"
            store.create_migration(migration_id, "wealth-adapter-rollback")
            print(f"\n  Created migration: {migration_id}")

            # Create some batches (batch_id format: {migration_id}:{table}:{op}:{idx})
            batch1 = MigrationBatch(
                batch_id=f"{migration_id}:{TABLE}:insert:0",
                table_name=TABLE,
                operation=MigrationOperation.INSERT,
                row_count=100,
                start_pk=40016,
                end_pk=40045,
                status=BatchStatus.PENDING,
            )
            batch2 = MigrationBatch(
                batch_id=f"{migration_id}:{TABLE}:insert:1",
                table_name=TABLE,
                operation=MigrationOperation.INSERT,
                row_count=50,
                start_pk=40046,
                end_pk=40060,
                status=BatchStatus.PENDING,
            )

            store.save_batch(batch1)
            store.save_batch(batch2)

            # Mark batch 1 started
            store.mark_batch_started(batch1.batch_id)

            # Mark batch 1 completed
            store.mark_batch_completed(batch1.batch_id, row_count=100, checksum="abc123")

            # Check pending
            pending = store.get_pending_batches(migration_id)
            assert len(pending) == 1
            assert pending[0].batch_id == f"{migration_id}:{TABLE}:insert:1"
            print(f"  Pending batches: {len(pending)}")

            # Mark batch 2 failed
            store.mark_batch_started(batch2.batch_id)
            store.mark_batch_failed(batch2.batch_id, "Simulated error")

            # Status summary
            status = store.get_migration_status(migration_id)
            print(f"  Migration status: {status}")
            assert status["completed"] == 1
            assert status["failed"] == 1

            # Last completed
            last = store.get_last_completed_batch(migration_id, TABLE)
            assert last is not None
            assert last.batch_id == batch1.batch_id
            print(f"  Last completed: {last.batch_id}")

            print("  Checkpoint lifecycle: PASS")
        finally:
            store.close()


# ══════════════════════════════════════════════════════════════════════════
# Stage 8: Data Streaming
# ══════════════════════════════════════════════════════════════════════════

@requires_pg
class TestPGStreaming:
    """Verify streaming works with real data."""

    def test_stream_rows(self, pg_adapter):
        columns = ["id", "key", "value"]
        total = 0
        batches = 0
        for batch in pg_adapter.stream_rows(TABLE, columns, pk_column="id", batch_size=100):
            total += len(batch)
            batches += 1

        expected = pg_adapter.get_row_count(TABLE)
        print(f"\n  Streamed {total} rows in {batches} batches (expected {expected})")
        assert total == expected


@requires_db2
class TestDB2Streaming:
    """Verify DB2 streaming works with real data."""

    def test_stream_rows(self, db2_adapter):
        columns = ["id", "key", "value"]
        total = 0
        batches = 0
        for batch in db2_adapter.stream_rows(TABLE, columns, pk_column="id", batch_size=100):
            total += len(batch)
            batches += 1

        expected = db2_adapter.get_row_count(TABLE)
        print(f"\n  Streamed {total} rows in {batches} batches (expected {expected})")
        assert total == expected


# ══════════════════════════════════════════════════════════════════════════
# Stage 9: Validation (dry-run)
# ══════════════════════════════════════════════════════════════════════════

@requires_both
class TestValidation:
    """Run pre-migration validation against real schemas."""

    def test_pre_validate(self, pg_adapter, db2_adapter):
        from dbmigrate.models import (
            MigrationManifest, MigrationTablePlan, MigrationOperation,
            MigrationMode,
        )
        from dbmigrate.validation import MigrationValidator

        from datetime import datetime, timezone

        # Build metadata
        source = _build_db_metadata(pg_adapter, "postgresql", TABLE)
        target = _build_db_metadata(db2_adapter, "db2", TABLE)

        # Minimal manifest
        plan = MigrationTablePlan(
            table_name=TABLE,
            operation=MigrationOperation.INSERT,
            row_count=10,
            column_mappings=[],
            source_only_columns=[],
            target_only_columns=[],
        )
        manifest = MigrationManifest(
            migration_id="validate-test-001",
            profile_name="wealth-adapter-rollback",
            mode=MigrationMode.ROLLBACK,
            tables=[plan],
            batches=[],
            created_at=datetime.now(tz=timezone.utc),
        )

        validator = MigrationValidator()
        results = validator.pre_validate(source, target, manifest)

        print(f"\n  Validation results ({len(results)}):")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"    [{status}] {r.check_name}: {r.message or 'OK'}")

        # No failures expected for adapterconfig (simple 3-col table)
        failures = [r for r in results if not r.passed]
        assert len(failures) == 0, f"Unexpected validation failures: {failures}"
        print("  Pre-validation: PASS (no failures)")
