"""Integration tests using populated Testcontainers.

These tests verify that both the Db2 and PostgreSQL containers
have been correctly populated with WEALTHADAPTER data and that
the schema + data match the source.

Run with:
  # PostgreSQL only (fast, no Db2 container):
  pytest tests/integration/testcontainers/test_populated_dbs.py -v -k "Pg"

  # Full (Db2 + PostgreSQL — slow, ~5 min for Db2 startup):
  pytest tests/integration/testcontainers/test_populated_dbs.py -v

  # Skip Testcontainers entirely:
  SKIP_TESTCONTAINERS=1 pytest tests/integration/testcontainers/ -v
"""

from __future__ import annotations

import pytest

from tests.integration.testcontainers.conftest import requires_docker, skip_no_db2, skip_no_docker
from tests.integration.testcontainers.schema_manager import TABLES, TABLE_INDEX


# ══════════════════════════════════════════════════════════════════════════
# PostgreSQL Tests
# ══════════════════════════════════════════════════════════════════════════


@skip_no_docker
@requires_docker
class TestPgSchema:
    """Verify PostgreSQL schema was created correctly."""

    def test_all_tables_exist(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        existing = {row[0] for row in cur.fetchall()}
        cur.close()

        for table in TABLES:
            assert table.name in existing, f"Table {table.name} not found in PG"

    def test_column_counts(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        for table in TABLES:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table.name,),
            )
            col_count = cur.fetchone()[0]
            expected = len(table.columns)
            assert col_count == expected, (
                f"Table {table.name}: expected {expected} columns, got {col_count}"
            )
        cur.close()

    def test_primary_keys(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        for table in TABLES:
            cur.execute(
                """
                SELECT a.attname
                FROM   pg_index i
                JOIN   pg_attribute a ON a.attrelid = i.indrelid
                                     AND a.attnum = ANY(i.indkey)
                WHERE  i.indrelid = %s::regclass
                AND    i.indisprimary
                ORDER BY array_position(i.indkey, a.attnum)
                """,
                (table.name,),
            )
            pk_cols = [row[0] for row in cur.fetchall()]
            assert pk_cols == table.primary_key, (
                f"Table {table.name}: expected PK {table.primary_key}, got {pk_cols}"
            )
        cur.close()


@skip_no_docker
@requires_docker
class TestPgData:
    """Verify PostgreSQL data population."""

    def test_userrole_count(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        cur.execute('SELECT COUNT(*) FROM "userrole"')
        count = cur.fetchone()[0]
        cur.close()
        expected = pg_populated["fetch_results"]["userrole"].row_count
        assert count == expected, f"userrole: expected {expected}, got {count}"

    def test_userrole_data(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        cur.execute('SELECT "id", "role" FROM "userrole" ORDER BY "id"')
        rows = cur.fetchall()
        cur.close()
        assert len(rows) > 0
        # First row should be WealthLineManagerRole (from embedded data)
        assert rows[0][1] == "WealthLineManagerRole"

    def test_adapterconfig_count(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        cur.execute('SELECT COUNT(*) FROM "adapterconfig"')
        count = cur.fetchone()[0]
        cur.close()
        expected = pg_populated["fetch_results"]["adapterconfig"].row_count
        assert count == expected

    def test_role_accessright_composite_pk(self, pg_conn, pg_populated):
        """Verify composite PK table (role_accessright) was populated."""
        cur = pg_conn.cursor()
        cur.execute('SELECT COUNT(*) FROM "role_accessright"')
        count = cur.fetchone()[0]
        cur.close()
        expected = pg_populated["fetch_results"]["role_accessright"].row_count
        assert count == expected

    def test_directorylocation_data(self, pg_conn, pg_populated):
        cur = pg_conn.cursor()
        cur.execute('SELECT "id", "dir" FROM "directorylocation" ORDER BY "id"')
        rows = cur.fetchall()
        cur.close()
        assert len(rows) == pg_populated["fetch_results"]["directorylocation"].row_count
        dirs = [r[1] for r in rows]
        assert "/RMBUT-FNB" in dirs

    def test_all_tables_have_data(self, pg_conn, pg_populated):
        """Every table should have at least one row."""
        cur = pg_conn.cursor()
        for table in TABLES:
            cur.execute(f'SELECT COUNT(*) FROM "{table.name}"')
            count = cur.fetchone()[0]
            assert count > 0, f"Table {table.name} is empty in PostgreSQL"
        cur.close()

    def test_row_counts_match_source(self, pg_conn, pg_populated):
        """Row counts must match the fetched source data."""
        cur = pg_conn.cursor()
        for table in TABLES:
            cur.execute(f'SELECT COUNT(*) FROM "{table.name}"')
            pg_count = cur.fetchone()[0]
            source_count = pg_populated["fetch_results"][table.name].row_count
            assert pg_count == source_count, (
                f"{table.name}: PG has {pg_count} rows, source had {source_count}"
            )
        cur.close()


# ══════════════════════════════════════════════════════════════════════════
# Db2 Tests (skipped if SKIP_DB2_CONTAINER=1)
# ══════════════════════════════════════════════════════════════════════════


@skip_no_docker
@requires_docker
@skip_no_db2
class TestDb2Schema:
    """Verify Db2 schema was created correctly."""

    def test_all_tables_exist(self, db2_conn, db2_populated):
        import ibm_db

        for table in TABLES:
            try:
                stmt = ibm_db.exec_immediate(
                    db2_conn,
                    f'SELECT COUNT(*) AS cnt FROM "{table.name}"',
                )
                row = ibm_db.fetch_assoc(stmt)
                assert row is not None, f"Table {table.name} not found in Db2"
            except Exception as exc:
                pytest.fail(f"Table {table.name} not accessible in Db2: {exc}")


@skip_no_docker
@requires_docker
@skip_no_db2
class TestDb2Data:
    """Verify Db2 data population."""

    def test_userrole_count(self, db2_conn, db2_populated):
        import ibm_db

        stmt = ibm_db.exec_immediate(
            db2_conn, 'SELECT COUNT(*) AS cnt FROM "userrole"'
        )
        row = ibm_db.fetch_assoc(stmt)
        count = row["CNT"]
        expected = db2_populated["fetch_results"]["userrole"].row_count
        assert count == expected

    def test_all_tables_have_data(self, db2_conn, db2_populated):
        import ibm_db

        for table in TABLES:
            stmt = ibm_db.exec_immediate(
                db2_conn, f'SELECT COUNT(*) AS cnt FROM "{table.name}"'
            )
            row = ibm_db.fetch_assoc(stmt)
            count = row["CNT"]
            assert count > 0, f"Table {table.name} is empty in Db2"

    def test_row_counts_match_source(self, db2_conn, db2_populated):
        import ibm_db

        for table in TABLES:
            stmt = ibm_db.exec_immediate(
                db2_conn, f'SELECT COUNT(*) AS cnt FROM "{table.name}"'
            )
            row = ibm_db.fetch_assoc(stmt)
            db2_count = row["CNT"]
            source_count = db2_populated["fetch_results"][table.name].row_count
            assert db2_count == source_count, (
                f"{table.name}: Db2 has {db2_count} rows, source had {source_count}"
            )


# ══════════════════════════════════════════════════════════════════════════
# Cross-database comparison tests
# ══════════════════════════════════════════════════════════════════════════


@skip_no_docker
@requires_docker
@skip_no_db2
class TestCrossDatabase:
    """Verify data consistency between Db2 and PostgreSQL."""

    def test_row_counts_match(self, both_populated):
        """Both databases should have identical row counts per table."""
        import ibm_db

        pg_conn = both_populated["pg_conn"]
        db2_conn = both_populated["db2_conn"]

        pg_cur = pg_conn.cursor()
        for table in TABLES:
            # PostgreSQL count
            pg_cur.execute(f'SELECT COUNT(*) FROM "{table.name}"')
            pg_count = pg_cur.fetchone()[0]

            # Db2 count
            stmt = ibm_db.exec_immediate(
                db2_conn, f'SELECT COUNT(*) AS cnt FROM "{table.name}"'
            )
            row = ibm_db.fetch_assoc(stmt)
            db2_count = row["CNT"]

            assert pg_count == db2_count, (
                f"{table.name}: PG={pg_count} vs Db2={db2_count}"
            )
        pg_cur.close()
