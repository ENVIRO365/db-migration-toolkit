"""Schema comparison and delta detection engine.

Compares source and target :class:`DatabaseMetadata` to find structural
differences, then detects row-level deltas using configurable strategies.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from dbmigrate.database import Database
from dbmigrate.models import (
    ColumnMapping,
    ColumnMetadata,
    ComparisonStrategy,
    DatabaseMetadata,
    TableDelta,
    TableMetadata,
)

logger = logging.getLogger(__name__)

# Threshold: tables smaller than this use PRIMARY_KEY; larger use CHECKSUM
_AUTO_PK_THRESHOLD = 500_000
_AUTO_CHECKSUM_THRESHOLD = 5_000_000


# ---------------------------------------------------------------------------
# Schema-level comparison result
# ---------------------------------------------------------------------------


@dataclass
class ColumnDifference:
    """A single column-level difference between source and target."""

    column_name: str
    difference_type: str  # "type_mismatch", "source_only", "target_only"
    source_type: Optional[str] = None
    target_type: Optional[str] = None
    source_nullable: Optional[bool] = None
    target_nullable: Optional[bool] = None
    detail: str = ""


@dataclass
class TableDifference:
    """Aggregated structural differences for one table."""

    table_name: str
    column_diffs: list[ColumnDifference] = field(default_factory=list)
    identity_diff: Optional[str] = None
    sequence_diff: Optional[str] = None

    @property
    def has_differences(self) -> bool:
        return bool(self.column_diffs) or self.identity_diff is not None or self.sequence_diff is not None


@dataclass
class SchemaComparisonResult:
    """Complete result of comparing two database schemas."""

    source_engine: str
    target_engine: str
    source_only_tables: list[str] = field(default_factory=list)
    target_only_tables: list[str] = field(default_factory=list)
    common_tables: list[str] = field(default_factory=list)
    table_differences: dict[str, TableDifference] = field(default_factory=dict)
    column_mappings: dict[str, list[ColumnMapping]] = field(default_factory=dict)

    @property
    def has_differences(self) -> bool:
        if self.source_only_tables or self.target_only_tables:
            return True
        return any(td.has_differences for td in self.table_differences.values())


# ---------------------------------------------------------------------------
# SchemaComparator
# ---------------------------------------------------------------------------


class SchemaComparator:
    """Compares source and target :class:`DatabaseMetadata` structurally.

    Produces a :class:`SchemaComparisonResult` describing tables present
    only on one side, column type mismatches, identity strategy differences,
    and sequence value differences.
    """

    def compare(
        self,
        source: DatabaseMetadata,
        target: DatabaseMetadata,
    ) -> SchemaComparisonResult:
        """Run a full structural comparison.

        Parameters
        ----------
        source:
            Metadata from the authoritative (source) database.
        target:
            Metadata from the migration target database.

        Returns
        -------
        SchemaComparisonResult
        """
        src_names = set(source.tables.keys())
        tgt_names = set(target.tables.keys())

        source_only = sorted(src_names - tgt_names)
        target_only = sorted(tgt_names - src_names)
        common = sorted(src_names & tgt_names)

        result = SchemaComparisonResult(
            source_engine=source.engine,
            target_engine=target.engine,
            source_only_tables=source_only,
            target_only_tables=target_only,
            common_tables=common,
        )

        if source_only:
            logger.warning(
                "Tables in source only (%d): %s",
                len(source_only),
                ", ".join(source_only),
            )
        if target_only:
            logger.warning(
                "Tables in target only (%d): %s",
                len(target_only),
                ", ".join(target_only),
            )

        for table_name in common:
            src_table = source.tables[table_name]
            tgt_table = target.tables[table_name]
            table_diff = self._compare_table(src_table, tgt_table)
            if table_diff.has_differences:
                result.table_differences[table_name] = table_diff
                logger.info(
                    "Table '%s' has %d column diff(s)%s%s",
                    table_name,
                    len(table_diff.column_diffs),
                    f", identity: {table_diff.identity_diff}" if table_diff.identity_diff else "",
                    f", sequence: {table_diff.sequence_diff}" if table_diff.sequence_diff else "",
                )

            # Always build column mappings for common tables
            result.column_mappings[table_name] = self._build_column_mappings(
                src_table, tgt_table
            )

        logger.info(
            "Schema comparison complete: %d common, %d source-only, "
            "%d target-only, %d with differences",
            len(common),
            len(source_only),
            len(target_only),
            len(result.table_differences),
        )
        return result

    # -- private helpers ---------------------------------------------------

    def _compare_table(
        self,
        src: TableMetadata,
        tgt: TableMetadata,
    ) -> TableDifference:
        diff = TableDifference(table_name=src.name)

        src_cols = {c.name.lower(): c for c in src.columns}
        tgt_cols = {c.name.lower(): c for c in tgt.columns}

        src_col_names = set(src_cols.keys())
        tgt_col_names = set(tgt_cols.keys())

        # Source-only columns
        for col_name in sorted(src_col_names - tgt_col_names):
            sc = src_cols[col_name]
            diff.column_diffs.append(
                ColumnDifference(
                    column_name=col_name,
                    difference_type="source_only",
                    source_type=sc.data_type,
                    detail=f"Column '{col_name}' exists in source but not target",
                )
            )

        # Target-only columns
        for col_name in sorted(tgt_col_names - src_col_names):
            tc = tgt_cols[col_name]
            diff.column_diffs.append(
                ColumnDifference(
                    column_name=col_name,
                    difference_type="target_only",
                    target_type=tc.data_type,
                    detail=f"Column '{col_name}' exists in target but not source",
                )
            )

        # Type mismatches on common columns
        for col_name in sorted(src_col_names & tgt_col_names):
            sc = src_cols[col_name]
            tc = tgt_cols[col_name]
            if sc.data_type.lower() != tc.data_type.lower():
                diff.column_diffs.append(
                    ColumnDifference(
                        column_name=col_name,
                        difference_type="type_mismatch",
                        source_type=sc.data_type,
                        target_type=tc.data_type,
                        source_nullable=sc.is_nullable,
                        target_nullable=tc.is_nullable,
                        detail=(
                            f"Type mismatch on '{col_name}': "
                            f"source={sc.data_type}, target={tc.data_type}"
                        ),
                    )
                )

        # Identity strategy differences
        if src.identity_column and tgt.identity_column:
            src_gen = src.identity_column.identity_generation
            tgt_gen = tgt.identity_column.identity_generation
            if src_gen != tgt_gen:
                diff.identity_diff = (
                    f"source={src_gen.value if src_gen else 'none'}, "
                    f"target={tgt_gen.value if tgt_gen else 'none'}"
                )
        elif src.identity_column and not tgt.identity_column:
            diff.identity_diff = "source has identity column, target does not"
        elif not src.identity_column and tgt.identity_column:
            diff.identity_diff = "target has identity column, source does not"

        # Sequence value differences
        if src.sequences and tgt.sequences:
            src_seq_map = {s.name.lower(): s for s in src.sequences}
            tgt_seq_map = {s.name.lower(): s for s in tgt.sequences}
            for sname in src_seq_map:
                if sname in tgt_seq_map:
                    ss = src_seq_map[sname]
                    ts = tgt_seq_map[sname]
                    if ss.last_value != ts.last_value:
                        diff.sequence_diff = (
                            f"Sequence '{sname}' last_value: "
                            f"source={ss.last_value}, target={ts.last_value}"
                        )
                        break  # report first mismatch

        return diff

    def _build_column_mappings(
        self,
        src: TableMetadata,
        tgt: TableMetadata,
    ) -> list[ColumnMapping]:
        src_cols = {c.name.lower(): c for c in src.columns}
        tgt_cols = {c.name.lower(): c for c in tgt.columns}
        mappings: list[ColumnMapping] = []

        all_names = sorted(set(src_cols.keys()) | set(tgt_cols.keys()))
        for col_name in all_names:
            sc = src_cols.get(col_name)
            tc = tgt_cols.get(col_name)
            if sc and tc:
                requires_cast = sc.data_type.lower() != tc.data_type.lower()
                mappings.append(
                    ColumnMapping(
                        source_column=sc.name,
                        target_column=tc.name,
                        source_type=sc.data_type,
                        target_type=tc.data_type,
                        requires_cast=requires_cast,
                    )
                )
            elif sc:
                mappings.append(
                    ColumnMapping(
                        source_column=sc.name,
                        target_column=sc.name,
                        source_type=sc.data_type,
                        target_type="",
                        source_only=True,
                    )
                )
            elif tc:
                mappings.append(
                    ColumnMapping(
                        source_column=tc.name,
                        target_column=tc.name,
                        source_type="",
                        target_type=tc.data_type,
                        target_only=True,
                    )
                )

        return mappings


# ---------------------------------------------------------------------------
# DeltaDetector — row-level comparison
# ---------------------------------------------------------------------------


class DeltaDetector:
    """Detects row-level differences between source and target tables.

    Supports multiple comparison strategies with automatic selection
    based on table size.
    """

    # Batch size for streaming PK comparison
    PK_BATCH_SIZE: int = 10_000
    # Batch size for fetching full rows to compare
    ROW_FETCH_BATCH: int = 1_000

    def detect_delta(
        self,
        source_db: Database,
        target_db: Database,
        table_name: str,
        pk_columns: list[str],
        columns: list[str],
        strategy: ComparisonStrategy = ComparisonStrategy.AUTO,
    ) -> TableDelta:
        """Detect row-level delta between source and target.

        Parameters
        ----------
        source_db:
            Connected adapter for the source database.
        target_db:
            Connected adapter for the target database.
        table_name:
            Table to compare.
        pk_columns:
            Primary key column name(s). For composite keys, pass multiple.
        columns:
            Column names to include in row-level comparison.
        strategy:
            Comparison strategy. ``AUTO`` selects based on table size.

        Returns
        -------
        TableDelta
            Containing insert, update, and delete PK lists.
        """
        source_count = source_db.get_row_count(table_name)
        target_count = target_db.get_row_count(table_name)

        logger.info(
            "Delta detection for '%s' — source: %d rows, target: %d rows, strategy: %s",
            table_name,
            source_count,
            target_count,
            strategy.value,
        )

        resolved = self._resolve_strategy(strategy, source_count, target_count)
        logger.debug("Resolved strategy for '%s': %s", table_name, resolved.value)

        if resolved == ComparisonStrategy.ROW_COUNT:
            return self._delta_row_count(table_name, source_count, target_count)
        elif resolved == ComparisonStrategy.PRIMARY_KEY:
            return self._delta_primary_key(
                source_db, target_db, table_name, pk_columns, columns,
                source_count, target_count,
            )
        elif resolved == ComparisonStrategy.CHECKSUM:
            return self._delta_checksum(
                source_db, target_db, table_name, pk_columns, columns,
                source_count, target_count,
            )
        else:
            raise ValueError(f"Unsupported resolved strategy: {resolved}")

    # -- strategy resolution -----------------------------------------------

    def _resolve_strategy(
        self,
        strategy: ComparisonStrategy,
        source_count: int,
        target_count: int,
    ) -> ComparisonStrategy:
        if strategy != ComparisonStrategy.AUTO:
            return strategy

        # AUTO logic: quick count check first
        if source_count == target_count == 0:
            return ComparisonStrategy.ROW_COUNT

        max_count = max(source_count, target_count)
        if max_count < _AUTO_PK_THRESHOLD:
            return ComparisonStrategy.PRIMARY_KEY
        elif max_count < _AUTO_CHECKSUM_THRESHOLD:
            return ComparisonStrategy.PRIMARY_KEY
        else:
            return ComparisonStrategy.CHECKSUM

    # -- ROW_COUNT strategy ------------------------------------------------

    def _delta_row_count(
        self,
        table_name: str,
        source_count: int,
        target_count: int,
    ) -> TableDelta:
        """Fast but imprecise: only detects whether counts differ."""
        delta = TableDelta(
            table_name=table_name,
            source_count=source_count,
            target_count=target_count,
        )
        if source_count == target_count:
            delta.unchanged_count = source_count
        # Cannot determine individual PKs with count-only strategy
        logger.info(
            "ROW_COUNT delta for '%s': source=%d, target=%d, match=%s",
            table_name,
            source_count,
            target_count,
            source_count == target_count,
        )
        return delta

    # -- PRIMARY_KEY strategy ----------------------------------------------

    def _delta_primary_key(
        self,
        source_db: Database,
        target_db: Database,
        table_name: str,
        pk_columns: list[str],
        columns: list[str],
        source_count: int,
        target_count: int,
    ) -> TableDelta:
        """Compare PK sets, then check common rows for updates."""
        logger.debug("PRIMARY_KEY delta: streaming PKs for '%s'", table_name)

        source_pks = self._collect_all_pks(source_db, table_name, pk_columns)
        target_pks = self._collect_all_pks(target_db, table_name, pk_columns)

        new_pks = sorted(source_pks - target_pks)
        missing_pks = sorted(target_pks - source_pks)
        common_pks = sorted(source_pks & target_pks)

        logger.info(
            "PK comparison for '%s': new=%d, missing=%d, common=%d",
            table_name,
            len(new_pks),
            len(missing_pks),
            len(common_pks),
        )

        # Check common PKs for updates by comparing row content
        update_pks: list[Any] = []
        unchanged = 0

        for batch_start in range(0, len(common_pks), self.ROW_FETCH_BATCH):
            batch_pks = common_pks[batch_start : batch_start + self.ROW_FETCH_BATCH]

            src_rows = source_db.fetch_rows_by_keys(
                table_name, columns, pk_columns, batch_pks
            )
            tgt_rows = target_db.fetch_rows_by_keys(
                table_name, columns, pk_columns, batch_pks
            )

            src_map = {self._extract_pk(row, pk_columns): row for row in src_rows}
            tgt_map = {self._extract_pk(row, pk_columns): row for row in tgt_rows}

            for pk_val in batch_pks:
                src_row = src_map.get(pk_val)
                tgt_row = tgt_map.get(pk_val)
                if src_row is None or tgt_row is None:
                    # Shouldn't happen, but treat as needing update
                    update_pks.append(pk_val)
                elif self._rows_differ(src_row, tgt_row, columns):
                    update_pks.append(pk_val)
                else:
                    unchanged += 1

        logger.info(
            "Row comparison for '%s': updates=%d, unchanged=%d",
            table_name,
            len(update_pks),
            unchanged,
        )

        return TableDelta(
            table_name=table_name,
            insert_pks=new_pks,
            update_pks=update_pks,
            delete_pks=missing_pks,
            unchanged_count=unchanged,
            source_count=source_count,
            target_count=target_count,
        )

    def _collect_all_pks(
        self,
        db: Database,
        table_name: str,
        pk_columns: list[str],
    ) -> set[Any]:
        """Stream all PKs from a table into a set.

        Uses batched streaming to avoid loading the entire result
        set in a single query.
        """
        pks: set[Any] = set()
        for batch in db.stream_primary_keys(
            table_name, pk_columns, batch_size=self.PK_BATCH_SIZE
        ):
            pks.update(batch)
        return pks

    @staticmethod
    def _extract_pk(row: dict[str, Any], pk_columns: list[str]) -> Any:
        """Extract PK value from a row dict — scalar for single, tuple for composite."""
        if len(pk_columns) == 1:
            return row[pk_columns[0]]
        return tuple(row[c] for c in pk_columns)

    # -- CHECKSUM strategy -------------------------------------------------

    def _delta_checksum(
        self,
        source_db: Database,
        target_db: Database,
        table_name: str,
        pk_columns: list[str],
        columns: list[str],
        source_count: int,
        target_count: int,
    ) -> TableDelta:
        """Full row-hash comparison for maximum accuracy.

        Streams rows from both sides, hashes each row deterministically,
        and compares hashes keyed by PK.
        """
        logger.debug("CHECKSUM delta: hashing rows for '%s'", table_name)

        sorted_cols = sorted(columns)

        source_hashes = self._build_hash_map(
            source_db, table_name, pk_columns, sorted_cols
        )
        target_hashes = self._build_hash_map(
            target_db, table_name, pk_columns, sorted_cols
        )

        src_keys = set(source_hashes.keys())
        tgt_keys = set(target_hashes.keys())

        new_pks = sorted(src_keys - tgt_keys)
        missing_pks = sorted(tgt_keys - src_keys)

        update_pks: list[Any] = []
        unchanged = 0
        for pk_val in sorted(src_keys & tgt_keys):
            if source_hashes[pk_val] != target_hashes[pk_val]:
                update_pks.append(pk_val)
            else:
                unchanged += 1

        logger.info(
            "CHECKSUM delta for '%s': insert=%d, update=%d, delete=%d, unchanged=%d",
            table_name,
            len(new_pks),
            len(update_pks),
            len(missing_pks),
            unchanged,
        )

        return TableDelta(
            table_name=table_name,
            insert_pks=new_pks,
            update_pks=update_pks,
            delete_pks=missing_pks,
            unchanged_count=unchanged,
            source_count=source_count,
            target_count=target_count,
        )

    def _build_hash_map(
        self,
        db: Database,
        table_name: str,
        pk_columns: list[str],
        sorted_columns: list[str],
    ) -> dict[Any, str]:
        """Stream all rows and produce {pk: sha256_hex} mapping."""
        hash_map: dict[Any, str] = {}
        # Use first PK column for keyset pagination ordering
        order_col = pk_columns[0] if pk_columns else None
        for batch in db.stream_rows(
            table_name,
            sorted_columns,
            pk_column=order_col,
            batch_size=5000,
        ):
            for row in batch:
                pk_val = self._extract_pk(row, pk_columns)
                row_hash = self._hash_row(row, sorted_columns)
                hash_map[pk_val] = row_hash
        return hash_map

    @staticmethod
    def _hash_row(row: dict[str, Any], sorted_columns: list[str]) -> str:
        """Produce a deterministic SHA-256 hex digest for a row.

        NULL values are normalised to the empty string.  All values
        are converted to their ``str`` representation before hashing.
        """
        hasher = hashlib.sha256()
        for col in sorted_columns:
            val = row.get(col)
            normalised = "" if val is None else str(val)
            hasher.update(col.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(normalised.encode("utf-8"))
            hasher.update(b"\x00")
        return hasher.hexdigest()

    @staticmethod
    def _rows_differ(
        src: dict[str, Any],
        tgt: dict[str, Any],
        columns: list[str],
    ) -> bool:
        """Return ``True`` if any column value differs between two rows."""
        for col in columns:
            sv = src.get(col)
            tv = tgt.get(col)
            if sv != tv:
                # Normalise None vs missing-key
                if sv is None and tv is None:
                    continue
                return True
        return False
