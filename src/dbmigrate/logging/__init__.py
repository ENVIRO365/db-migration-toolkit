"""Structured logging for migration operations.

Configures Python logging with structured format, per-migration log files,
and a migration event logger that records stage/table/batch events with
no sensitive data at INFO level.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_LOGS_DIR = Path("logs")
_CONFIGURED: set[str] = set()


class _JsonFormatter(logging.Formatter):
    """Emits log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        # Attach extra migration context if present
        for attr in ("migration_id", "stage", "table", "batch"):
            val = getattr(record, attr, None)
            if val is not None:
                entry[attr] = val
        return json.dumps(entry, default=str)


class _HumanFormatter(logging.Formatter):
    """Readable format for console and plain-text log files."""

    FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self.FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")


def configure_logging(
    *,
    migration_id: Optional[str] = None,
    level: int = logging.INFO,
    json_format: bool = False,
    debug_row_data: bool = False,
    logs_dir: Optional[Path] = None,
) -> None:
    """Set up logging for a migration run.

    Parameters
    ----------
    migration_id:
        Unique run identifier. When provided a dedicated log file is
        created at ``<logs_dir>/<migration_id>.log``.
    level:
        Root log level. ``DEBUG`` enables row-level data logging
        (only when *debug_row_data* is also ``True``).
    json_format:
        When ``True`` emit JSON lines instead of human-readable text.
    debug_row_data:
        Allow DEBUG-level messages that may contain row payloads.
        Off by default to prevent accidental PII leakage.
    logs_dir:
        Override the default ``logs/`` directory.
    """
    effective_dir = logs_dir or _LOGS_DIR

    # Prevent double-configuration for the same migration
    config_key = migration_id or "__root__"
    if config_key in _CONFIGURED:
        return
    _CONFIGURED.add(config_key)

    root = logging.getLogger("dbmigrate")
    root.setLevel(logging.DEBUG if debug_row_data else level)

    formatter: logging.Formatter = _JsonFormatter() if json_format else _HumanFormatter()

    # Console handler
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(formatter)
        root.addHandler(console)

    # Per-migration file handler
    if migration_id:
        effective_dir.mkdir(parents=True, exist_ok=True)
        log_path = effective_dir / f"{migration_id}.log"
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if debug_row_data else level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(
    migration_id: str,
    stage: str,
    table: Optional[str],
    batch: Optional[str],
    event_type: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Record a structured migration event.

    Events are logged at INFO level and include stage/table/batch
    context as extra fields (visible in JSON format).

    Parameters
    ----------
    migration_id:
        The unique migration run identifier.
    stage:
        Pipeline stage name (e.g. ``"INSPECT"``, ``"MIGRATE"``).
    table:
        Table name, or ``None`` for stage-level events.
    batch:
        Batch identifier, or ``None`` for table-level events.
    event_type:
        Short event label (e.g. ``"start"``, ``"complete"``, ``"error"``).
    details:
        Optional key-value pairs. **Must not contain row payloads,
        passwords, or other sensitive data.**
    """
    logger = logging.getLogger("dbmigrate.events")
    extra = {
        "migration_id": migration_id,
        "stage": stage,
        "table": table or "",
        "batch": batch or "",
    }
    parts = [f"[{stage}]"]
    if table:
        parts.append(f"[{table}]")
    if batch:
        parts.append(f"[batch:{batch}]")
    parts.append(event_type)
    if details:
        # Filter out any keys that look sensitive
        safe = {k: v for k, v in details.items() if k.lower() not in ("password", "dsn", "payload", "row_data")}
        if safe:
            parts.append(json.dumps(safe, default=str))

    logger.info(" ".join(parts), extra=extra)


def get_migration_logger(migration_id: str) -> logging.Logger:
    """Return a logger scoped to a specific migration run.

    Parameters
    ----------
    migration_id:
        The unique migration run identifier.

    Returns
    -------
    logging.Logger
        Logger named ``dbmigrate.run.<migration_id>``.
    """
    return logging.getLogger(f"dbmigrate.run.{migration_id}")
