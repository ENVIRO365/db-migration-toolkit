"""Testcontainers configuration for Db2 and PostgreSQL.

Supports two modes:

  1. **Testcontainers** (default) — spins up ephemeral containers via
     ``testcontainers-python`` (PG) or the Docker SDK (Db2).  Containers
     are created with random host ports and cleaned up on exit.

  2. **Docker-Compose** (``--use-compose``) — connects to long-lived
     containers from ``docker-compose.testcontainers.yml``.  No containers
     are started or stopped by this code.

Db2 container:
  Image: icr.io/db2_community/db2:latest
  Requires: LICENSE=accept, DB2INST1_PASSWORD, DBNAME
  Port: 50000
  Startup: 3–5 min (generous timeout due to slow Db2 init)
  Privileged: required for Db2 memory configuration

PostgreSQL container:
  Image: postgres:16-alpine
  Port: 5432
  Startup: ~10s

Both containers use module-scope singletons so they are started once
per test session and cleaned up at exit.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import DockerException
try:
    from testcontainers.community.postgres import PostgresContainer
except ImportError:
    from testcontainers.postgres import PostgresContainer

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

DB2_IMAGE = os.getenv(
    "DB2_TEST_IMAGE", "icr.io/db2_community/db2:latest"
)
DB2_PORT = 50000
DB2_DBNAME = "WLTHTEST"  # Db2 database names are limited to 8 characters
DB2_PASSWORD = "testpassw0rd"
DB2_STARTUP_TIMEOUT_S = 300  # 5 minutes

PG_IMAGE = os.getenv("PG_TEST_IMAGE", "postgres:16-alpine")
PG_PORT = 5432
PG_DBNAME = "wealth_test"
PG_USER = "testuser"
PG_PASSWORD = "testpassw0rd"


# ── Db2 Container (manual — testcontainers-python has no Db2 module) ─────


@dataclass
class Db2ContainerConfig:
    """All tuning knobs for the Db2 Testcontainer."""

    image: str = DB2_IMAGE
    port: int = DB2_PORT
    db_name: str = DB2_DBNAME
    password: str = DB2_PASSWORD
    startup_timeout: int = DB2_STARTUP_TIMEOUT_S
    privileged: bool = True
    env_overrides: dict[str, str] = field(default_factory=dict)


class Db2Container:
    """Manages a Db2 Docker container lifecycle.

    Uses the Docker SDK directly because ``testcontainers-python`` has
    no built-in Db2 module.  The container is started with the required
    env vars and we poll the logs for ``DB2 START`` or the healthcheck
    before declaring readiness.
    """

    def __init__(self, config: Db2ContainerConfig | None = None) -> None:
        self.config = config or Db2ContainerConfig()
        self._client: docker.DockerClient | None = None
        self._container: Any | None = None
        self._host_port: int | None = None

    # -- Public API --------------------------------------------------------

    def start(self) -> "Db2Container":
        """Pull the image (if needed) and start the container."""
        logger.info(
            "Starting Db2 container (%s) — this may take up to %d s …",
            self.config.image,
            self.config.startup_timeout,
        )

        self._client = docker.from_env()

        env = {
            "LICENSE": "accept",
            "DB2INST1_PASSWORD": self.config.password,
            "DBNAME": self.config.db_name,
            "ARCHIVE_LOGS": "false",
            "AUTOCONFIG": "false",
            **self.config.env_overrides,
        }

        self._container = self._client.containers.run(
            image=self.config.image,
            detach=True,
            environment=env,
            ports={f"{self.config.port}/tcp": None},  # random host port
            privileged=self.config.privileged,
            remove=True,
        )

        self._wait_for_ready()
        self._resolve_host_port()
        logger.info(
            "Db2 container ready — host port %d → %d",
            self._host_port,
            self.config.port,
        )
        return self

    def stop(self) -> None:
        """Stop and remove the container."""
        if self._container is not None:
            try:
                logger.info("Stopping Db2 container %s …", self._container.short_id)
                self._container.stop(timeout=10)
            except Exception:
                logger.warning("Db2 container stop failed; forcing kill.")
                try:
                    self._container.kill()
                except Exception:
                    pass
            self._container = None

    @property
    def host(self) -> str:
        return "localhost"

    @property
    def port(self) -> int:
        if self._host_port is None:
            raise RuntimeError("Container not started")
        return self._host_port

    @property
    def db_name(self) -> str:
        return self.config.db_name

    @property
    def password(self) -> str:
        return self.config.password

    @property
    def username(self) -> str:
        return "db2inst1"

    def get_dsn(self) -> str:
        """Return an ibm_db-compatible DSN string."""
        return (
            f"DATABASE={self.db_name};"
            f"HOSTNAME={self.host};"
            f"PORT={self.port};"
            f"PROTOCOL=TCPIP;"
            f"UID={self.username};"
            f"PWD={self.password}"
        )

    def get_jdbc_url(self) -> str:
        """Return a JDBC-style URL (useful for documentation)."""
        return f"jdbc:db2://{self.host}:{self.port}/{self.db_name}"

    # -- Private -----------------------------------------------------------

    def _wait_for_ready(self) -> None:
        """Block until the Db2 startup log marker appears AND database is connectable.

        Phase 1: Wait for the setup log marker (container initialization).
        Phase 2: Wait for the database to accept connections (CATALOG + ACTIVATE).

        The Db2 community image writes "Setup has completed" when the instance
        is running, but the DBNAME database may still be in CREATE/ACTIVATE
        state.  We exec ``db2 connect to <DBNAME>`` inside the container to
        confirm actual readiness.
        """
        deadline = time.monotonic() + self.config.startup_timeout
        ready_markers = (
            b"Setup has completed",
            b"DB2START processing was successful",
            b"(*) Setup has completed",
        )

        # Phase 1: Wait for log marker
        phase1_done = False
        while time.monotonic() < deadline:
            self._container.reload()
            status = self._container.status
            if status == "exited":
                logs = self._container.logs(tail=50).decode(errors="replace")
                raise RuntimeError(
                    f"Db2 container exited unexpectedly. Last logs:\n{logs}"
                )

            logs = self._container.logs(tail=200)
            if any(marker in logs for marker in ready_markers):
                phase1_done = True
                logger.info("  Db2 setup marker detected; verifying database …")
                break

            remaining = int(deadline - time.monotonic())
            if remaining % 30 == 0:
                logger.info("  … still waiting for Db2 (%d s remaining)", remaining)
            time.sleep(5)

        if not phase1_done:
            logs_tail = self._container.logs(tail=30).decode(errors="replace")
            raise TimeoutError(
                f"Db2 container did not produce setup marker within "
                f"{self.config.startup_timeout}s.\nLast logs:\n{logs_tail}"
            )

        # Phase 2: Wait for database to be connectable via exec
        db_name = self.config.db_name
        connect_cmd = f'su - db2inst1 -c "db2 connect to {db_name}"'
        while time.monotonic() < deadline:
            try:
                exit_code, output = self._container.exec_run(
                    ["bash", "-c", connect_cmd]
                )
                output_str = output.decode(errors="replace") if output else ""
                if exit_code == 0 and "Database Connection Information" in output_str:
                    logger.info("  Db2 database %s is connectable.", db_name)
                    return
                else:
                    logger.debug(
                        "  db2 connect returned code=%d: %s",
                        exit_code,
                        output_str[:200],
                    )
            except Exception as exc:
                logger.debug("  exec_run failed: %s", exc)

            remaining = int(deadline - time.monotonic())
            if remaining % 15 == 0:
                logger.info(
                    "  … waiting for DB %s to become connectable (%d s remaining)",
                    db_name,
                    remaining,
                )
            time.sleep(5)

        logs_tail = self._container.logs(tail=30).decode(errors="replace")
        raise TimeoutError(
            f"Db2 database '{db_name}' did not become connectable within "
            f"{self.config.startup_timeout}s.\nLast logs:\n{logs_tail}"
        )

    def _resolve_host_port(self) -> None:
        """Discover the mapped host port."""
        self._container.reload()
        port_key = f"{self.config.port}/tcp"
        bindings = self._container.ports.get(port_key)
        if not bindings:
            raise RuntimeError(f"No port binding found for {port_key}")
        self._host_port = int(bindings[0]["HostPort"])

    def __enter__(self) -> "Db2Container":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ── PostgreSQL Container (uses testcontainers-python) ────────────────────


@dataclass
class PgContainerConfig:
    """Tuning knobs for the PostgreSQL Testcontainer."""

    image: str = PG_IMAGE
    port: int = PG_PORT
    db_name: str = PG_DBNAME
    user: str = PG_USER
    password: str = PG_PASSWORD


class PgContainer:
    """Thin wrapper around ``testcontainers.postgres.PostgresContainer``."""

    def __init__(self, config: PgContainerConfig | None = None) -> None:
        self.config = config or PgContainerConfig()
        self._tc: PostgresContainer | None = None

    def start(self) -> "PgContainer":
        logger.info("Starting PostgreSQL container (%s) …", self.config.image)
        self._tc = PostgresContainer(
            image=self.config.image,
            port=self.config.port,
            dbname=self.config.db_name,
            username=self.config.user,
            password=self.config.password,
        )
        self._tc.start()
        logger.info(
            "PostgreSQL container ready — %s:%s",
            self._tc.get_container_host_ip(),
            self._tc.get_exposed_port(self.config.port),
        )
        return self

    def stop(self) -> None:
        if self._tc is not None:
            try:
                self._tc.stop()
            except Exception:
                logger.warning("PostgreSQL container stop failed.", exc_info=True)
            self._tc = None

    @property
    def host(self) -> str:
        if self._tc is None:
            raise RuntimeError("Container not started")
        return self._tc.get_container_host_ip()

    @property
    def port(self) -> int:
        if self._tc is None:
            raise RuntimeError("Container not started")
        return int(self._tc.get_exposed_port(self.config.port))

    @property
    def db_name(self) -> str:
        return self.config.db_name

    @property
    def user(self) -> str:
        return self.config.user

    @property
    def password(self) -> str:
        return self.config.password

    def get_dsn(self) -> str:
        """Return a psycopg2-compatible DSN string."""
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.db_name}"
        )

    def __enter__(self) -> "PgContainer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ── Singleton management ─────────────────────────────────────────────────

_db2_singleton: Db2Container | None = None
_pg_singleton: PgContainer | None = None


def get_db2_container() -> Db2Container:
    """Return the module-scoped Db2 container singleton, starting it if needed."""
    global _db2_singleton
    if _db2_singleton is None:
        _db2_singleton = Db2Container()
        _db2_singleton.start()
    return _db2_singleton


def get_pg_container() -> PgContainer:
    """Return the module-scoped PostgreSQL container singleton, starting it if needed."""
    global _pg_singleton
    if _pg_singleton is None:
        _pg_singleton = PgContainer()
        _pg_singleton.start()
    return _pg_singleton


def cleanup_containers() -> None:
    """Stop both container singletons.  Safe to call multiple times."""
    global _db2_singleton, _pg_singleton
    if _db2_singleton is not None:
        _db2_singleton.stop()
        _db2_singleton = None
    if _pg_singleton is not None:
        _pg_singleton.stop()
        _pg_singleton = None


# ── Docker-Compose external containers ───────────────────────────────────
#
# These classes connect to already-running containers (e.g. from
# ``docker-compose.testcontainers.yml``) without managing their lifecycle.
# They expose the same public API (host, port, db_name, get_dsn, …) so
# the populate pipeline can use them interchangeably.
#
# Defaults match docker-compose.testcontainers.yml.  Override via env vars
# (COMPOSE_PG_HOST, COMPOSE_PG_PORT, etc.) or constructor args.


def _compose_pg_defaults() -> dict[str, str | int]:
    """Resolve docker-compose PG connection defaults (lazy, respects .env)."""
    return {
        "host": os.getenv("COMPOSE_PG_HOST", "localhost"),
        "port": int(os.getenv("COMPOSE_PG_PORT", "5433")),
        "db_name": os.getenv("COMPOSE_PG_DBNAME", PG_DBNAME),
        "user": os.getenv("COMPOSE_PG_USER", PG_USER),
        "password": os.getenv("COMPOSE_PG_PASSWORD", PG_PASSWORD),
    }


def _compose_db2_defaults() -> dict[str, str | int]:
    """Resolve docker-compose Db2 connection defaults (lazy, respects .env)."""
    return {
        "host": os.getenv("COMPOSE_DB2_HOST", "localhost"),
        "port": int(os.getenv("COMPOSE_DB2_PORT", "50001")),
        "db_name": os.getenv("COMPOSE_DB2_DBNAME", DB2_DBNAME),
        "username": os.getenv("COMPOSE_DB2_USER", "db2inst1"),
        "password": os.getenv("COMPOSE_DB2_PASSWORD", DB2_PASSWORD),
    }


class ExternalPgContainer:
    """Connects to an existing PostgreSQL instance (no lifecycle management).

    Defaults match the ``postgres-test`` service in
    ``docker-compose.testcontainers.yml`` (host port 5433).
    Override via constructor args or COMPOSE_PG_* env vars.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        db_name: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        defaults = _compose_pg_defaults()
        self._host = host or defaults["host"]
        self._port = port or defaults["port"]
        self._db_name = db_name or defaults["db_name"]
        self._user = user or defaults["user"]
        self._password = password or defaults["password"]

    # Lifecycle no-ops — container is managed externally
    def start(self) -> "ExternalPgContainer":
        logger.info(
            "Using external PostgreSQL at %s:%s/%s",
            self._host, self._port, self._db_name,
        )
        return self

    def stop(self) -> None:
        pass  # We don't own the container

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def db_name(self) -> str:
        return self._db_name

    @property
    def user(self) -> str:
        return self._user

    @property
    def password(self) -> str:
        return self._password

    def get_dsn(self) -> str:
        """Return a psycopg2-compatible DSN string."""
        return (
            f"postgresql://{self._user}:{self._password}"
            f"@{self._host}:{self._port}/{self._db_name}"
        )

    def __enter__(self) -> "ExternalPgContainer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class ExternalDb2Container:
    """Connects to an existing Db2 instance (no lifecycle management).

    Defaults match the ``db2-test`` service in
    ``docker-compose.testcontainers.yml`` (host port 50001).
    Override via constructor args or COMPOSE_DB2_* env vars.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        db_name: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        defaults = _compose_db2_defaults()
        self._host = host or defaults["host"]
        self._port = port or defaults["port"]
        self._db_name = db_name or defaults["db_name"]
        self._username = username or defaults["username"]
        self._password = password or defaults["password"]

    # Lifecycle no-ops
    def start(self) -> "ExternalDb2Container":
        logger.info(
            "Using external Db2 at %s:%s/%s",
            self._host, self._port, self._db_name,
        )
        return self

    def stop(self) -> None:
        pass

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def db_name(self) -> str:
        return self._db_name

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    def get_dsn(self) -> str:
        """Return an ibm_db-compatible DSN string."""
        return (
            f"DATABASE={self._db_name};"
            f"HOSTNAME={self._host};"
            f"PORT={self._port};"
            f"PROTOCOL=TCPIP;"
            f"UID={self._username};"
            f"PWD={self._password}"
        )

    def get_jdbc_url(self) -> str:
        """Return a JDBC-style URL (useful for documentation)."""
        return f"jdbc:db2://{self._host}:{self._port}/{self._db_name}"

    def __enter__(self) -> "ExternalDb2Container":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
