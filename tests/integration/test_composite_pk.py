"""Integration tests for composite/virtual PK tables.

Exercises the full pipeline for tables that lack a real single-column PK
and use virtual_pk (composite key) declared in profile config.

Target table: recipient_emailgroup (263 rows, PK=[recipientid, emailgroupid])

This test is READ-ONLY — no data is written to either database.
Run with:  .venv/bin/python -m pytest tests/integration/test_composite_pk.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ── Connection parameters ────────────────────────────────────────────────
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

# Table with composite PK (no real PK in DB — uses virtual_pk)
TABLE = "recipient_emailgroup"
PK_COLUMNS = ["recipients_id", "emailgroups_id"]


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
        conn = ibm_db.connect(DB2_DSN, "", "")
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


# ══════════════════════════════════════════════════════════════════════════
# Test: PG adapter composite PK streaming
# ══════════════════════════════════════════════════════════════════════════


@requires_pg
class TestPgCompositeStream:
    """Test PostgreSQLAdapter.stream_primary_keys with composite PK."""

    def test_stream_composite_pks(self, pg_adapter):
        """Streams all composite PKs for recipient_emailgroup."""
        all_pks = []
        for batch in pg_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=100):
            all_pks.extend(batch)

        # Should have ~263 rows in dev
        assert len(all_pks) >= 200, f"Expected >=200 rows, got {len(all_pks)}"

        # Each PK should be a tuple of 2 ints
        for pk in all_pks[:5]:
            assert isinstance(pk, tuple), f"Expected tuple, got {type(pk)}: {pk}"
            assert len(pk) == 2, f"Expected 2-element tuple, got {pk}"

    def test_near_unique(self, pg_adapter):
        """Composite PKs should be nearly unique (known dup: (395,255))."""
        all_pks = []
        for batch in pg_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=500):
            all_pks.extend(batch)

        unique = set(all_pks)
        duplicates = len(all_pks) - len(unique)
        # Known data quality issue: (395,255) appears twice in both PG and DB2
        assert duplicates <= 1, (
            f"Unexpected duplicates: {duplicates} (only (395,255) expected)"
        )

    def test_fetch_rows_by_composite_keys(self, pg_adapter):
        """Fetch specific rows using composite PK tuples."""
        # Get first 3 PKs
        first_batch = next(pg_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=3))
        assert len(first_batch) >= 1

        # Get all columns for the table
        col_meta = pg_adapter.get_columns(TABLE)
        col_names = [c.name for c in col_meta]

        # Fetch those rows
        rows = pg_adapter.fetch_rows_by_keys(
            TABLE, col_names, PK_COLUMNS, first_batch
        )
        assert len(rows) == len(first_batch)

        # Each row should have recipients_id and emailgroups_id columns
        for row in rows:
            assert "recipients_id" in row
            assert "emailgroups_id" in row


# ══════════════════════════════════════════════════════════════════════════
# Test: DB2 adapter composite PK streaming
# ══════════════════════════════════════════════════════════════════════════


@requires_db2
class TestDb2CompositeStream:
    """Test DB2Adapter.stream_primary_keys with composite PK."""

    def test_stream_composite_pks(self, db2_adapter):
        """Streams all composite PKs for recipient_emailgroup."""
        all_pks = []
        for batch in db2_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=100):
            all_pks.extend(batch)

        assert len(all_pks) >= 200, f"Expected >=200 rows, got {len(all_pks)}"

        for pk in all_pks[:5]:
            assert isinstance(pk, tuple), f"Expected tuple, got {type(pk)}: {pk}"
            assert len(pk) == 2

    def test_near_unique(self, db2_adapter):
        """Composite PKs should be nearly unique (known dup: (395,255))."""
        all_pks = []
        for batch in db2_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=500):
            all_pks.extend(batch)

        unique = set(all_pks)
        duplicates = len(all_pks) - len(unique)
        # Known data quality issue: (395,255) appears twice in both PG and DB2
        assert duplicates <= 1, (
            f"Unexpected duplicates: {duplicates} (only (395,255) expected)"
        )

    def test_fetch_rows_by_composite_keys(self, db2_adapter):
        """Fetch specific rows using composite PK tuples."""
        first_batch = next(db2_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=3))
        assert len(first_batch) >= 1

        # Get all columns for the table
        col_meta = db2_adapter.get_columns(TABLE)
        col_names = [c.name for c in col_meta]

        rows = db2_adapter.fetch_rows_by_keys(
            TABLE, col_names, PK_COLUMNS, first_batch
        )
        assert len(rows) == len(first_batch)

        for row in rows:
            assert "recipients_id" in row or "RECIPIENTS_ID" in row


# ══════════════════════════════════════════════════════════════════════════
# Test: Cross-DB delta detection with composite PKs
# ══════════════════════════════════════════════════════════════════════════


@requires_both
class TestCompositeDeltaDetection:
    """Test DeltaDetector with composite PK across PG and DB2."""

    def test_delta_detect_composite(self, pg_adapter, db2_adapter):
        """Full delta detection for recipient_emailgroup (PG→DB2 direction)."""
        from dbmigrate.comparison import DeltaDetector
        from dbmigrate.models import ComparisonStrategy

        # Get common columns
        pg_cols = pg_adapter.get_columns(TABLE)
        col_names = [c.name for c in pg_cols]

        detector = DeltaDetector()
        delta = detector.detect_delta(
            source_db=pg_adapter,
            target_db=db2_adapter,
            table_name=TABLE,
            pk_columns=PK_COLUMNS,
            columns=col_names,
            strategy=ComparisonStrategy.PRIMARY_KEY,
        )

        # In dev, both DBs should have 263 identical rows → 0 inserts, 0 deletes
        total = delta.unchanged_count + len(delta.insert_pks) + len(delta.update_pks) + len(delta.delete_pks)
        assert total > 0, "Delta detected no rows at all"

        # Report what we found (informational)
        print(f"\n  Delta for {TABLE} (composite PK):")
        print(f"    Unchanged: {delta.unchanged_count}")
        print(f"    Inserts:   {len(delta.insert_pks)}")
        print(f"    Updates:   {len(delta.update_pks)}")
        print(f"    Deletes:   {len(delta.delete_pks)}")

        # In dev, we expect them to be in sync
        assert delta.unchanged_count >= 250, (
            f"Expected >=250 unchanged, got {delta.unchanged_count}"
        )

    def test_pk_sets_match_across_dbs(self, pg_adapter, db2_adapter):
        """Verify PG and DB2 have the same composite PK sets."""
        pg_pks = set()
        for batch in pg_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=500):
            pg_pks.update(batch)

        db2_pks = set()
        for batch in db2_adapter.stream_primary_keys(TABLE, PK_COLUMNS, batch_size=500):
            db2_pks.update(batch)

        pg_only = pg_pks - db2_pks
        db2_only = db2_pks - pg_pks

        print(f"\n  PK comparison for {TABLE}:")
        print(f"    PG total:   {len(pg_pks)}")
        print(f"    DB2 total:  {len(db2_pks)}")
        print(f"    PG-only:    {len(pg_only)}")
        print(f"    DB2-only:   {len(db2_only)}")

        # In dev, expect identical sets
        assert len(pg_only) == 0, f"PG-only PKs: {list(pg_only)[:10]}"
        assert len(db2_only) <= 1, f"DB2-only PKs: {list(db2_only)[:10]}"


# ══════════════════════════════════════════════════════════════════════════
# Test: Multiple composite-PK tables (smoke test)
# ══════════════════════════════════════════════════════════════════════════


@requires_pg
class TestMultipleCompositeTables:
    """Smoke test: stream composite PKs from all virtual_pk tables."""

    TABLES = {
        "emailgroup_emailaddress": ["emailgroup_id", "emailaddresses_id"],
        "recipient_dirlocations": ["recipients_id", "directorylocations_id"],
        "recipient_emailgroup": ["recipients_id", "emailgroups_id"],
        "recipient_webservices": ["recipients_id", "webservice_id"],
        "role_accessright": ["role_id", "accessright_id"],
    }

    @pytest.mark.parametrize("table,pk_cols", TABLES.items())
    def test_stream_pks(self, pg_adapter, table, pk_cols):
        """Each composite-PK table should stream tuples without error."""
        all_pks = []
        for batch in pg_adapter.stream_primary_keys(table, pk_cols, batch_size=100):
            all_pks.extend(batch)

        # All tables should have at least some rows in dev
        # (except incomingfile and webservicestatusmessage which may be 0)
        print(f"    {table}: {len(all_pks)} rows")

        # Verify tuple structure
        if all_pks:
            assert isinstance(all_pks[0], tuple)
            assert len(all_pks[0]) == len(pk_cols)
