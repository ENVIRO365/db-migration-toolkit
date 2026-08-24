"""Automation policy engine.

Controls whether migration stages require human confirmation
based on the profile's automation mode and environment.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from dbmigrate.config import AutomationConfig
from dbmigrate.models import AutomationMode, MigrationManifest, MigrationOperation

logger = logging.getLogger(__name__)

# Hostname patterns that indicate a production database.
_PROD_HOSTNAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|[.\-])prod(?:[.\-]|uction|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-])prd(?:[.\-]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-])live(?:[.\-]|$)", re.IGNORECASE),
]

# Database name patterns that indicate production.
_PROD_DBNAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|[\-_])prod(?:[\-_]|uction|$)", re.IGNORECASE),
    re.compile(r"(?:^|[\-_])prd(?:[\-_]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[\-_])live(?:[\-_]|$)", re.IGNORECASE),
]

# Patterns for non-production environments.
_NON_PROD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|[.\-_])dev(?:[.\-_]|elopment|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-_])test(?:[.\-_]|ing|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-_])staging(?:[.\-_]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-_])stg(?:[.\-_]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-_])uat(?:[.\-_]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-_])sandbox(?:[.\-_]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.\-_])pre(?:[.\-_]|$)", re.IGNORECASE),
    re.compile(r"localhost", re.IGNORECASE),
    re.compile(r"127\.0\.0\.1"),
]


def resolve_environment(dsn: str) -> str:
    """Classify the target environment from a DSN string.

    Parameters
    ----------
    dsn:
        Database connection string (JDBC-style or libpq-style).

    Returns
    -------
    str
        One of ``"production"``, ``"pre-production"``, ``"development"``,
        or ``"unknown"``.  ``"unknown"`` is treated as production for
        safety by all policy checks.
    """
    hostname, dbname = _extract_host_db(dsn)
    combined = f"{hostname} {dbname}"

    # Check production first
    for pat in _PROD_HOSTNAME_PATTERNS:
        if pat.search(hostname):
            return "production"
    for pat in _PROD_DBNAME_PATTERNS:
        if pat.search(dbname):
            return "production"

    # Check non-production
    for pat in _NON_PROD_PATTERNS:
        if pat.search(combined):
            # Distinguish dev from pre-prod
            if re.search(r"(?:pre|staging|stg|uat)", combined, re.IGNORECASE):
                return "pre-production"
            return "development"

    return "unknown"


def _extract_host_db(dsn: str) -> tuple[str, str]:
    """Best-effort extraction of hostname and database name from a DSN."""
    hostname = ""
    dbname = ""

    # Try standard URI parsing first
    try:
        parsed = urlparse(dsn if "://" in dsn else f"db://{dsn}")
        hostname = parsed.hostname or ""
        dbname = (parsed.path or "").lstrip("/").split("/")[0]
    except Exception:
        pass

    # Fallback: JDBC-style or key=value
    if not hostname:
        m = re.search(r"(?:host|server)\s*=\s*([^\s;]+)", dsn, re.IGNORECASE)
        if m:
            hostname = m.group(1)
    if not dbname:
        m = re.search(r"(?:database|dbname)\s*=\s*([^\s;]+)", dsn, re.IGNORECASE)
        if m:
            dbname = m.group(1)

    return hostname, dbname


class AutomationPolicy:
    """Decides whether migration steps require human confirmation.

    Parameters
    ----------
    config:
        The profile's automation configuration.
    target_dsn:
        DSN of the migration target — used for environment detection.
    """

    def __init__(self, config: AutomationConfig, target_dsn: str) -> None:
        self._config = config
        self._target_dsn = target_dsn
        self._environment = resolve_environment(target_dsn)
        logger.info(
            "AutomationPolicy initialised: mode=%s, detected_environment=%s",
            config.mode.value,
            self._environment,
        )

    @property
    def environment(self) -> str:
        """The detected target environment."""
        return self._environment

    def is_production_target(self, dsn: Optional[str] = None) -> bool:
        """Return ``True`` if the target is production or unknown.

        Parameters
        ----------
        dsn:
            DSN to check. Defaults to the target DSN provided at init.

        Returns
        -------
        bool
            ``True`` if the environment is ``"production"`` or
            ``"unknown"`` (safe default).
        """
        env = resolve_environment(dsn) if dsn else self._environment
        return env in ("production", "unknown")

    def requires_confirmation(self, manifest: MigrationManifest) -> bool:
        """Whether the migration requires interactive confirmation.

        Parameters
        ----------
        manifest:
            The migration manifest to evaluate.

        Returns
        -------
        bool
            ``True`` if the operator must confirm before execution.
        """
        mode = self._config.mode

        if mode == AutomationMode.SUPERVISED:
            logger.info("SUPERVISED mode — confirmation required")
            return True

        if mode == AutomationMode.AUTO_NON_PROD:
            if self.is_production_target():
                logger.warning(
                    "AUTO_NON_PROD mode but target looks like production — confirmation required"
                )
                return True
            # Check auto_confirm_below_rows threshold
            if (
                self._config.auto_confirm_below_rows > 0
                and manifest.total_rows > self._config.auto_confirm_below_rows
            ):
                logger.info(
                    "AUTO_NON_PROD mode: total_rows=%d exceeds threshold=%d — confirmation required",
                    manifest.total_rows,
                    self._config.auto_confirm_below_rows,
                )
                return True
            logger.info("AUTO_NON_PROD mode on non-production target — no confirmation required")
            return False

        if mode == AutomationMode.AUTO_APPROVED:
            self.log_automation_warning()
            return False

        # Unknown mode — require confirmation as safe default
        logger.warning("Unknown automation mode '%s' — requiring confirmation", mode)
        return True

    def requires_delete_confirmation(self, manifest: MigrationManifest) -> bool:
        """Whether DELETE operations require separate confirmation.

        In the initial implementation this **always** returns ``True``
        regardless of automation mode, as DELETEs are inherently
        destructive.

        Parameters
        ----------
        manifest:
            The migration manifest to evaluate.

        Returns
        -------
        bool
            Always ``True`` if the manifest contains DELETE operations.
        """
        has_deletes = any(
            t.operation == MigrationOperation.DELETE or (t.delta and t.delta.delete_pks)
            for t in manifest.tables
        )
        if has_deletes:
            delete_count = sum(
                len(t.delta.delete_pks) if t.delta else 0
                for t in manifest.tables
            )
            logger.warning(
                "Manifest contains DELETE operations (%d rows across tables) — "
                "explicit delete confirmation required regardless of automation mode",
                delete_count,
            )
            return True
        return False

    def log_automation_warning(self) -> None:
        """Emit a loud warning when running in fully automated mode."""
        logger.warning("=" * 72)
        logger.warning("  AUTO_APPROVED MODE — RUNNING WITHOUT HUMAN CONFIRMATION")
        logger.warning("  Target: %s (environment: %s)", self._target_dsn, self._environment)
        logger.warning("  Ensure this is intentional and monitored.")
        logger.warning("=" * 72)
