"""Schema discovery module.

Orchestrates metadata collection from source and target databases
using their respective adapters.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dbmigrate.database import Database

from dbmigrate.models import DatabaseMetadata

logger = logging.getLogger(__name__)


def discover_schema(db: Database) -> DatabaseMetadata:
    """Collect full schema metadata from a single database.

    Delegates to :meth:`Database.get_database_metadata` and logs
    progress for each discovered table.

    Parameters
    ----------
    db:
        A connected :class:`Database` adapter instance.

    Returns
    -------
    DatabaseMetadata
        Complete schema metadata including tables, columns, keys,
        sequences, and triggers.

    Raises
    ------
    RuntimeError
        If the adapter raises during metadata collection.
    """
    engine = db.engine_name
    schema = db.schema
    logger.info("Starting schema discovery for %s (schema: %s)", engine, schema)
    start = time.monotonic()

    try:
        # get_tables first so we can log per-table progress
        table_names = db.get_tables()
        logger.info(
            "Found %d tables in %s.%s",
            len(table_names),
            engine,
            schema,
        )

        # Delegate to the adapter's full discovery method which
        # iterates tables and assembles DatabaseMetadata.
        metadata = db.get_database_metadata()

        elapsed = time.monotonic() - start
        for table_name in sorted(metadata.tables):
            tbl = metadata.tables[table_name]
            logger.debug(
                "  %-40s  cols=%-3d  rows=%-8d  pk=%s",
                tbl.name,
                len(tbl.columns),
                tbl.row_count,
                tbl.primary_key.columns if tbl.primary_key else "none",
            )

        logger.info(
            "Schema discovery completed for %s.%s — %d tables, "
            "%d standalone sequences in %.2fs",
            engine,
            schema,
            len(metadata.tables),
            len(metadata.standalone_sequences),
            elapsed,
        )
        return metadata

    except Exception as exc:
        logger.error(
            "Schema discovery failed for %s.%s: %s",
            engine,
            schema,
            exc,
        )
        raise RuntimeError(
            f"Schema discovery failed for {engine}.{schema}"
        ) from exc


def discover_both(
    source: Database,
    target: Database,
) -> tuple[DatabaseMetadata, DatabaseMetadata]:
    """Discover schema metadata from both source and target databases.

    Parameters
    ----------
    source:
        Connected adapter for the source (authoritative) database.
    target:
        Connected adapter for the target database.

    Returns
    -------
    tuple[DatabaseMetadata, DatabaseMetadata]
        ``(source_metadata, target_metadata)``

    Raises
    ------
    RuntimeError
        If discovery fails for either side.
    """
    logger.info(
        "Beginning paired discovery: source=%s  target=%s",
        source.engine_name,
        target.engine_name,
    )
    overall_start = time.monotonic()

    source_meta = discover_schema(source)
    target_meta = discover_schema(target)

    elapsed = time.monotonic() - overall_start
    logger.info(
        "Paired discovery complete in %.2fs — source: %d tables, target: %d tables",
        elapsed,
        len(source_meta.tables),
        len(target_meta.tables),
    )
    return source_meta, target_meta
