"""Pytest fixtures for Testcontainers-based integration tests.

Provides module-scoped Db2 and PostgreSQL containers that are started
once, populated with WEALTHADAPTER data, and shared across all tests
in the ``testcontainers/`` package.

Usage in tests:
    def test_something(pg_conn, db2_conn):
        cur = pg_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM userrole")
        ...
"""

from __future__ import annotations

import logging
import os

import pytest

from tests.integration.testcontainers.containers import (
    Db2Container,
    PgContainer,
    cleanup_containers,
)
from tests.integration.testcontainers.mcp_fetcher import fetch_all_tables
from tests.integration.testcontainers.schema_manager import (
    TABLES,
    create_schema_db2,
    create_schema_postgres,
    insert_rows_db2,
    insert_rows_postgres,
)

logger = logging.getLogger(__name__)

# ── Skip markers ─────────────────────────────────────────────────────────

requires_docker = pytest.mark.skipif(
    os.getenv("SKIP_TESTCONTAINERS", "").lower() in ("1", "true", "yes"),
    reason="SKIP_TESTCONTAINERS is set",
)


def _docker_available() -> bool:
    """Check if Docker daemon is reachable."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


skip_no_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon not available",
)


# ── PostgreSQL fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_container():
    """Start a PostgreSQL container for the test module."""
    container = PgContainer()
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope="module")
def pg_conn(pg_container):
    """Provide a psycopg2 connection to the test PostgreSQL container."""
    import psycopg2

    conn = psycopg2.connect(pg_container.get_dsn())
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def pg_populated(pg_conn):
    """Create schema and populate PostgreSQL with test data.

    Returns the fetch results dict for verification.
    """
    fetch_results = fetch_all_tables(use_embedded=True)
    create_schema_postgres(pg_conn)

    inserted = {}
    for table_def in TABLES:
        fetch = fetch_results.get(table_def.name)
        if fetch and fetch.ok and fetch.row_count > 0:
            count = insert_rows_postgres(pg_conn, table_def, fetch.rows)
            inserted[table_def.name] = count

    return {"fetch_results": fetch_results, "inserted": inserted}


# ── Db2 fixtures ─────────────────────────────────────────────────────────

skip_no_db2 = pytest.mark.skipif(
    os.getenv("SKIP_DB2_CONTAINER", "").lower() in ("1", "true", "yes"),
    reason="SKIP_DB2_CONTAINER is set (Db2 container is slow to start)",
)


@pytest.fixture(scope="module")
def db2_container():
    """Start a Db2 container for the test module.

    Note: Db2 containers take 3-5 minutes to initialise.
    Set SKIP_DB2_CONTAINER=1 to skip these tests.
    """
    container = Db2Container()
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope="module")
def db2_conn(db2_container):
    """Provide an ibm_db connection to the test Db2 container."""
    import ibm_db

    conn = ibm_db.connect(db2_container.get_dsn(), "", "")
    yield conn
    ibm_db.close(conn)


@pytest.fixture(scope="module")
def db2_populated(db2_conn):
    """Create schema and populate Db2 with test data."""
    fetch_results = fetch_all_tables(use_embedded=True)
    create_schema_db2(db2_conn)

    inserted = {}
    for table_def in TABLES:
        fetch = fetch_results.get(table_def.name)
        if fetch and fetch.ok and fetch.row_count > 0:
            count = insert_rows_db2(db2_conn, table_def, fetch.rows)
            inserted[table_def.name] = count

    return {"fetch_results": fetch_results, "inserted": inserted}


# ── Combined fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def both_populated(pg_conn, pg_populated, db2_conn, db2_populated):
    """Ensure both databases are populated and return connection handles."""
    return {
        "pg_conn": pg_conn,
        "db2_conn": db2_conn,
        "pg_data": pg_populated,
        "db2_data": db2_populated,
    }
