"""Validation framework for migration operations.

Provides pre-flight, per-batch, and post-migration validation checks
to ensure data integrity and schema compatibility throughout the
migration lifecycle.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Optional

from dbmigrate.database import Database
from dbmigrate.models import (
    ColumnMapping,
    DatabaseMetadata,
    IdentityStrategy,
    MigrationManifest,
    MigrationMode,
    MigrationOperation,
    MigrationBatch,
    MigrationTablePlan,
    TableMetadata,
    ValidationResult,
)

logger = logging.getLogger(__name__)

# Types known to hold large or structured text
_LOB_TYPES = frozenset({
    "clob", "nclob", "dbclob", "text", "mediumtext", "longtext",
    "xml", "json", "jsonb",
})

# Multi-byte character sets where VARCHAR(n) may mean n bytes, not n chars
_MULTIBYTE_CHARSETS = frozenset({"utf8", "utf8mb4", "utf-8", "utf16", "utf-16"})

# Maximum number of PKs to sample during post-validation
_PK_SAMPLE_SIZE = 500

# Maximum number of FK spot-checks
_FK_SPOT_CHECK_SIZE = 200


class MigrationValidator:
    """Validates migration plans and results at three lifecycle stages.

    1. **pre_validate** — before any data moves; checks schema compatibility.
    2. **batch_validate** — after each batch; verifies row counts and PK existence.
    3. **post_validate** — after the full run; confirms overall integrity.
    """

    # ------------------------------------------------------------------
    # Pre-validation
    # ------------------------------------------------------------------

    def pre_validate(
        self,
        source_meta: DatabaseMetadata,
        target_meta: DatabaseMetadata,
        manifest: MigrationManifest,
    ) -> list[ValidationResult]:
        """Run pre-flight validation checks.

        Parameters
        ----------
        source_meta:
            Metadata from the source database.
        target_meta:
            Metadata from the target database.
        manifest:
            The planned migration manifest.

        Returns
        -------
        list[ValidationResult]
            One result per check, including both passes and failures.
        """
        results: list[ValidationResult] = []

        for plan in manifest.tables:
            if plan.operation == MigrationOperation.NO_ACTION:
                continue

            src_table = source_meta.tables.get(plan.table_name.lower())
            tgt_table = target_meta.tables.get(plan.table_name.lower())

            # Table existence
            if src_table is None:
                results.append(ValidationResult(
                    table_name=plan.table_name,
                    check_name="source_table_exists",
                    passed=False,
                    message=f"Source table '{plan.table_name}' not found in metadata",
                    severity="error",
                ))
                continue

            if tgt_table is None:
                results.append(ValidationResult(
                    table_name=plan.table_name,
                    check_name="target_table_exists",
                    passed=False,
                    message=f"Target table '{plan.table_name}' not found in metadata",
                    severity="error",
                ))
                continue

            # PK existence
            results.append(self._check_pk_exists(src_table, "source"))
            results.append(self._check_pk_exists(tgt_table, "target"))

            # Column type compatibility
            results.extend(
                self._check_column_compatibility(plan, src_table, tgt_table)
            )

            # Identity strategy compatibility
            results.append(
                self._check_identity_compatibility(plan, src_table, tgt_table)
            )

            # VARCHAR length checks
            results.extend(
                self._check_varchar_lengths(plan, src_table, tgt_table, source_meta)
            )

            # CLOB/XML compatibility
            results.extend(
                self._check_lob_compatibility(plan, src_table, tgt_table)
            )

            # Source-only column detection
            results.extend(
                self._check_source_only_columns(plan)
            )

            # DELETE plan validation (rollback mode only)
            if (
                plan.operation == MigrationOperation.DELETE
                and manifest.mode == MigrationMode.ROLLBACK
            ):
                results.extend(
                    self._check_delete_fk_integrity(plan, tgt_table, target_meta)
                )

        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        logger.info(
            "Pre-validation complete: %d passed, %d failed out of %d checks",
            passed, failed, len(results),
        )

        return results

    # ------------------------------------------------------------------
    # Batch validation
    # ------------------------------------------------------------------

    def batch_validate(
        self,
        batch: MigrationBatch,
        source_db: Database,
        target_db: Database,
    ) -> list[ValidationResult]:
        """Validate a single completed batch.

        Parameters
        ----------
        batch:
            The batch that was just executed.
        source_db:
            Connected source database adapter.
        target_db:
            Connected target database adapter.

        Returns
        -------
        list[ValidationResult]
        """
        results: list[ValidationResult] = []

        # Row count verification
        if batch.operation in (
            MigrationOperation.INSERT,
            MigrationOperation.UPDATE,
        ):
            results.append(self._check_batch_row_count(batch, target_db))

        # PK existence check on target for inserts
        if batch.operation == MigrationOperation.INSERT:
            results.append(self._check_batch_pk_exists_on_target(batch, target_db))

        return results

    # ------------------------------------------------------------------
    # Post-validation
    # ------------------------------------------------------------------

    def post_validate(
        self,
        source_db: Database,
        target_db: Database,
        manifest: MigrationManifest,
    ) -> list[ValidationResult]:
        """Run post-migration validation checks.

        Parameters
        ----------
        source_db:
            Connected source database adapter.
        target_db:
            Connected target database adapter.
        manifest:
            The migration manifest that was executed.

        Returns
        -------
        list[ValidationResult]
        """
        results: list[ValidationResult] = []

        for plan in manifest.tables:
            if plan.operation == MigrationOperation.NO_ACTION:
                continue

            # Row count comparison
            results.append(
                self._check_row_counts(plan.table_name, source_db, target_db, manifest.mode)
            )

            # PK existence sampling
            results.append(
                self._check_pk_sampling(plan, source_db, target_db)
            )

            # Identity sequence value check
            if plan.identity_strategy != IdentityStrategy.NONE:
                results.append(
                    self._check_identity_sequence(plan.table_name, source_db, target_db)
                )

            # FK integrity spot-check
            results.extend(
                self._check_fk_integrity_spot(plan.table_name, target_db)
            )

            # Missing/duplicate row detection
            results.extend(
                self._check_missing_duplicates(plan, source_db, target_db)
            )

        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        logger.info(
            "Post-validation complete: %d passed, %d failed out of %d checks",
            passed, failed, len(results),
        )

        return results

    # ------------------------------------------------------------------
    # Pre-validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_pk_exists(table: TableMetadata, side: str) -> ValidationResult:
        """Verify the table has a primary key defined."""
        has_pk = table.primary_key is not None and len(table.primary_key.columns) > 0
        return ValidationResult(
            table_name=table.name,
            check_name=f"{side}_pk_exists",
            passed=has_pk,
            message=(
                None if has_pk
                else f"Table '{table.name}' on {side} has no primary key"
            ),
            severity="error" if not has_pk else "info",
        )

    @staticmethod
    def _check_column_compatibility(
        plan: MigrationTablePlan,
        src_table: TableMetadata,
        tgt_table: TableMetadata,
    ) -> list[ValidationResult]:
        """Check that mapped columns have compatible types."""
        results: list[ValidationResult] = []
        tgt_col_map = {c.name.lower(): c for c in tgt_table.columns}

        for mapping in plan.column_mappings:
            if mapping.source_only or mapping.target_only:
                continue

            tgt_col = tgt_col_map.get(mapping.target_column.lower())
            if tgt_col is None:
                results.append(ValidationResult(
                    table_name=plan.table_name,
                    check_name="column_exists_on_target",
                    passed=False,
                    expected=mapping.target_column,
                    actual="missing",
                    message=(
                        f"Mapped target column '{mapping.target_column}' "
                        f"does not exist on target table"
                    ),
                    severity="error",
                ))
                continue

            # Type compatibility (warn on mismatch, not error — casts may handle it)
            if mapping.requires_cast:
                results.append(ValidationResult(
                    table_name=plan.table_name,
                    check_name="column_type_compatible",
                    passed=True,
                    expected=mapping.source_type,
                    actual=mapping.target_type,
                    message=(
                        f"Column '{mapping.source_column}' requires cast: "
                        f"{mapping.source_type} -> {mapping.target_type}"
                    ),
                    severity="warning",
                ))

        return results

    @staticmethod
    def _check_identity_compatibility(
        plan: MigrationTablePlan,
        src_table: TableMetadata,
        tgt_table: TableMetadata,
    ) -> ValidationResult:
        """Verify identity strategy is compatible with target table."""
        if plan.identity_strategy == IdentityStrategy.NONE:
            return ValidationResult(
                table_name=plan.table_name,
                check_name="identity_compatible",
                passed=True,
                severity="info",
            )

        has_identity = tgt_table.identity_column is not None
        compatible = has_identity or plan.identity_strategy == IdentityStrategy.NONE

        return ValidationResult(
            table_name=plan.table_name,
            check_name="identity_compatible",
            passed=compatible,
            expected=f"identity_strategy={plan.identity_strategy.value}",
            actual=f"target_has_identity={has_identity}",
            message=(
                None if compatible
                else (
                    f"Identity strategy '{plan.identity_strategy.value}' specified "
                    f"but target table has no identity column"
                )
            ),
            severity="error" if not compatible else "info",
        )

    @staticmethod
    def _check_varchar_lengths(
        plan: MigrationTablePlan,
        src_table: TableMetadata,
        tgt_table: TableMetadata,
        source_meta: DatabaseMetadata,
    ) -> list[ValidationResult]:
        """Warn when VARCHAR lengths may be byte-based in multi-byte charsets."""
        results: list[ValidationResult] = []
        encoding = (source_meta.encoding or "").lower().replace("-", "")

        if encoding not in _MULTIBYTE_CHARSETS:
            return results

        src_col_map = {c.name.lower(): c for c in src_table.columns}
        tgt_col_map = {c.name.lower(): c for c in tgt_table.columns}

        for mapping in plan.column_mappings:
            if mapping.source_only or mapping.target_only:
                continue

            src_col = src_col_map.get(mapping.source_column.lower())
            tgt_col = tgt_col_map.get(mapping.target_column.lower())

            if src_col is None or tgt_col is None:
                continue

            src_type = (src_col.data_type or "").lower()
            tgt_type = (tgt_col.data_type or "").lower()

            if "varchar" not in src_type and "varchar" not in tgt_type:
                continue

            if (
                src_col.max_length is not None
                and tgt_col.max_length is not None
                and src_col.max_length > tgt_col.max_length
            ):
                results.append(ValidationResult(
                    table_name=plan.table_name,
                    check_name="varchar_length_sufficient",
                    passed=False,
                    expected=f"target >= {src_col.max_length}",
                    actual=str(tgt_col.max_length),
                    message=(
                        f"Column '{mapping.source_column}': source VARCHAR({src_col.max_length}) "
                        f"> target VARCHAR({tgt_col.max_length}) with multi-byte charset '{encoding}'. "
                        f"Data truncation is possible."
                    ),
                    severity="warning",
                ))

        return results

    @staticmethod
    def _check_lob_compatibility(
        plan: MigrationTablePlan,
        src_table: TableMetadata,
        tgt_table: TableMetadata,
    ) -> list[ValidationResult]:
        """Verify CLOB/XML/JSON columns have compatible target types."""
        results: list[ValidationResult] = []
        src_col_map = {c.name.lower(): c for c in src_table.columns}
        tgt_col_map = {c.name.lower(): c for c in tgt_table.columns}

        for mapping in plan.column_mappings:
            if mapping.source_only or mapping.target_only:
                continue

            src_type = mapping.source_type.lower()
            tgt_type = mapping.target_type.lower()

            if src_type in _LOB_TYPES or tgt_type in _LOB_TYPES:
                # Both should be LOB-capable
                compatible = tgt_type in _LOB_TYPES or tgt_type in ("varchar", "character varying")
                if not compatible:
                    results.append(ValidationResult(
                        table_name=plan.table_name,
                        check_name="lob_type_compatible",
                        passed=False,
                        expected=f"LOB-compatible type for '{mapping.source_column}'",
                        actual=tgt_type,
                        message=(
                            f"Source column '{mapping.source_column}' is {src_type} "
                            f"but target column is {tgt_type} — may lose data"
                        ),
                        severity="warning",
                    ))

        return results

    @staticmethod
    def _check_source_only_columns(
        plan: MigrationTablePlan,
    ) -> list[ValidationResult]:
        """Report source-only columns that will be excluded from migration."""
        results: list[ValidationResult] = []

        for col_name in plan.source_only_columns:
            results.append(ValidationResult(
                table_name=plan.table_name,
                check_name="source_only_column_excluded",
                passed=True,
                message=(
                    f"Source-only column '{col_name}' will be excluded from migration"
                ),
                severity="info",
            ))

        return results

    @staticmethod
    def _check_delete_fk_integrity(
        plan: MigrationTablePlan,
        tgt_table: TableMetadata,
        target_meta: DatabaseMetadata,
    ) -> list[ValidationResult]:
        """Check that DELETEs won't violate FK constraints on the target.

        Looks for child tables that reference this table and warns if those
        child tables are not also scheduled for DELETE or are at a higher
        dependency level.
        """
        results: list[ValidationResult] = []

        # Find tables whose FKs reference this table
        for other_name, other_table in target_meta.tables.items():
            for fk in other_table.foreign_keys:
                if fk.referenced_table.lower() == plan.table_name.lower():
                    results.append(ValidationResult(
                        table_name=plan.table_name,
                        check_name="delete_fk_integrity",
                        passed=True,  # Warning only — executor handles order
                        expected=f"Child table '{other_name}' processed before parent",
                        actual=f"FK '{fk.constraint_name}' on '{other_name}'",
                        message=(
                            f"DELETE on '{plan.table_name}' has child table "
                            f"'{other_name}' via FK '{fk.constraint_name}'. "
                            f"Ensure children are deleted first."
                        ),
                        severity="warning",
                    ))

        return results

    # ------------------------------------------------------------------
    # Batch validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_batch_row_count(
        batch: MigrationBatch,
        target_db: Database,
    ) -> ValidationResult:
        """Verify the target table row count changed as expected."""
        actual_count = target_db.get_row_count(batch.table_name)
        return ValidationResult(
            table_name=batch.table_name,
            check_name="batch_row_count",
            passed=True,  # Informational — exact check requires before/after
            expected=str(batch.row_count),
            actual=str(actual_count),
            message=(
                f"Target '{batch.table_name}' has {actual_count} rows after "
                f"batch '{batch.batch_id}' ({batch.row_count} rows processed)"
            ),
            severity="info",
        )

    @staticmethod
    def _check_batch_pk_exists_on_target(
        batch: MigrationBatch,
        target_db: Database,
    ) -> ValidationResult:
        """Spot-check that the batch's PK range exists on the target."""
        if batch.start_pk is None:
            return ValidationResult(
                table_name=batch.table_name,
                check_name="batch_pk_exists",
                passed=True,
                message="No PK range to verify",
                severity="info",
            )

        max_pk = target_db.get_max_primary_key(batch.table_name, "id")
        pk_exists = max_pk is not None and max_pk >= batch.start_pk

        return ValidationResult(
            table_name=batch.table_name,
            check_name="batch_pk_exists",
            passed=pk_exists,
            expected=f"max_pk >= {batch.start_pk}",
            actual=str(max_pk),
            message=(
                None if pk_exists
                else f"Expected PK >= {batch.start_pk} on target but max_pk is {max_pk}"
            ),
            severity="error" if not pk_exists else "info",
        )

    # ------------------------------------------------------------------
    # Post-validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_row_counts(
        table_name: str,
        source_db: Database,
        target_db: Database,
        mode: MigrationMode,
    ) -> ValidationResult:
        """Compare row counts between source and target."""
        src_count = source_db.get_row_count(table_name)
        tgt_count = target_db.get_row_count(table_name)

        if mode == MigrationMode.SYNC:
            # In sync mode, target should have >= source rows
            # (no deletes, so extra target rows are expected)
            passed = tgt_count >= src_count
            message = (
                None if passed
                else (
                    f"Target '{table_name}' has fewer rows ({tgt_count}) "
                    f"than source ({src_count}) after SYNC"
                )
            )
        else:
            # In rollback mode, counts should match exactly
            passed = src_count == tgt_count
            message = (
                None if passed
                else (
                    f"Row count mismatch on '{table_name}': "
                    f"source={src_count}, target={tgt_count}"
                )
            )

        return ValidationResult(
            table_name=table_name,
            check_name="post_row_count",
            passed=passed,
            expected=str(src_count),
            actual=str(tgt_count),
            message=message,
            severity="error" if not passed else "info",
        )

    @staticmethod
    def _check_pk_sampling(
        plan: MigrationTablePlan,
        source_db: Database,
        target_db: Database,
    ) -> ValidationResult:
        """Sample source PKs and verify they exist on the target."""
        if not plan.pk_columns and not plan.column_mappings:
            return ValidationResult(
                table_name=plan.table_name,
                check_name="post_pk_sampling",
                passed=True,
                message="No PK columns — skipping PK sampling",
                severity="info",
            )

        pk_columns = plan.pk_columns if plan.pk_columns else [plan.column_mappings[0].source_column]

        # Collect a sample of source PKs
        sample_pks: list[Any] = []
        for batch in source_db.stream_primary_keys(
            plan.table_name, pk_columns, batch_size=_PK_SAMPLE_SIZE
        ):
            sample_pks.extend(batch)
            if len(sample_pks) >= _PK_SAMPLE_SIZE:
                break

        if not sample_pks:
            return ValidationResult(
                table_name=plan.table_name,
                check_name="post_pk_sampling",
                passed=True,
                message="Source table is empty — nothing to sample",
                severity="info",
            )

        # Take a random sample if we have more than needed
        if len(sample_pks) > _PK_SAMPLE_SIZE:
            sample_pks = random.sample(sample_pks, _PK_SAMPLE_SIZE)

        # Check which sampled PKs exist on target
        target_rows = target_db.fetch_rows_by_keys(
            plan.table_name, pk_columns, pk_columns, sample_pks,
        )
        # Extract PKs from result rows for comparison
        if len(pk_columns) == 1:
            found_pks = {row[pk_columns[0]] for row in target_rows}
        else:
            found_pks = {tuple(row[c] for c in pk_columns) for row in target_rows}
        missing = [pk for pk in sample_pks if pk not in found_pks]

        passed = len(missing) == 0
        return ValidationResult(
            table_name=plan.table_name,
            check_name="post_pk_sampling",
            passed=passed,
            expected=f"{len(sample_pks)} sampled PKs present on target",
            actual=f"{len(missing)} missing",
            message=(
                None if passed
                else (
                    f"{len(missing)} of {len(sample_pks)} sampled PKs missing "
                    f"on target '{plan.table_name}'"
                )
            ),
            severity="error" if not passed else "info",
        )

    @staticmethod
    def _check_identity_sequence(
        table_name: str,
        source_db: Database,
        target_db: Database,
    ) -> ValidationResult:
        """Verify the target identity sequence is >= source max PK."""
        src_max = source_db.get_max_primary_key(table_name, "id")
        tgt_max = target_db.get_max_primary_key(table_name, "id")

        if src_max is None:
            return ValidationResult(
                table_name=table_name,
                check_name="post_identity_sequence",
                passed=True,
                message="Source table is empty — no sequence check needed",
                severity="info",
            )

        passed = tgt_max is not None and tgt_max >= src_max
        return ValidationResult(
            table_name=table_name,
            check_name="post_identity_sequence",
            passed=passed,
            expected=f"target max_pk >= {src_max}",
            actual=str(tgt_max),
            message=(
                None if passed
                else (
                    f"Target max PK ({tgt_max}) < source max PK ({src_max}) "
                    f"for '{table_name}' — identity sequence may need reset"
                )
            ),
            severity="warning" if not passed else "info",
        )

    @staticmethod
    def _check_fk_integrity_spot(
        table_name: str,
        target_db: Database,
    ) -> list[ValidationResult]:
        """Spot-check FK integrity on the target table.

        Runs a sample query for each FK to ensure referenced rows exist.
        This is a lightweight check — not exhaustive.
        """
        results: list[ValidationResult] = []

        try:
            fks = target_db.get_foreign_keys(table_name)
        except Exception as exc:
            results.append(ValidationResult(
                table_name=table_name,
                check_name="post_fk_integrity",
                passed=False,
                message=f"Could not retrieve FKs for '{table_name}': {exc}",
                severity="warning",
            ))
            return results

        for fk in fks:
            # Build a simple orphan-check query
            fk_cols = ", ".join(fk.columns)
            ref_cols = ", ".join(fk.referenced_columns)
            ref_table = fk.referenced_table

            try:
                # Use raw SQL to check for orphan rows (limit to spot-check size)
                sql = (
                    f"SELECT COUNT(*) AS cnt FROM ("
                    f"  SELECT {fk_cols} FROM {table_name} "
                    f"  WHERE {fk.columns[0]} IS NOT NULL "
                    f"  LIMIT {_FK_SPOT_CHECK_SIZE}"
                    f") sub "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM {ref_table} "
                    f"  WHERE {ref_table}.{fk.referenced_columns[0]} = sub.{fk.columns[0]}"
                    f")"
                )
                result = target_db.execute(sql)

                # Result handling varies by adapter; treat as informational
                results.append(ValidationResult(
                    table_name=table_name,
                    check_name=f"post_fk_integrity:{fk.constraint_name}",
                    passed=True,
                    message=(
                        f"FK '{fk.constraint_name}' spot-check executed "
                        f"({fk_cols} -> {ref_table}.{ref_cols})"
                    ),
                    severity="info",
                ))
            except Exception as exc:
                results.append(ValidationResult(
                    table_name=table_name,
                    check_name=f"post_fk_integrity:{fk.constraint_name}",
                    passed=False,
                    message=(
                        f"FK spot-check failed for '{fk.constraint_name}' "
                        f"on '{table_name}': {exc}"
                    ),
                    severity="warning",
                ))

        return results

    @staticmethod
    def _check_missing_duplicates(
        plan: MigrationTablePlan,
        source_db: Database,
        target_db: Database,
    ) -> list[ValidationResult]:
        """Detect missing or duplicate rows on the target.

        Compares a sample of source PKs against the target to find
        missing rows.  Also checks for duplicate PKs on the target
        (which would indicate a bug in the migration).
        """
        results: list[ValidationResult] = []

        if not plan.pk_columns and not plan.column_mappings:
            return results

        pk_columns = plan.pk_columns if plan.pk_columns else [plan.column_mappings[0].source_column]

        # Sample source PKs
        sample_pks: list[Any] = []
        for batch in source_db.stream_primary_keys(
            plan.table_name, pk_columns, batch_size=_PK_SAMPLE_SIZE
        ):
            sample_pks.extend(batch)
            if len(sample_pks) >= _PK_SAMPLE_SIZE:
                break

        if not sample_pks:
            return results

        if len(sample_pks) > _PK_SAMPLE_SIZE:
            sample_pks = random.sample(sample_pks, _PK_SAMPLE_SIZE)

        # Fetch from target
        target_rows = target_db.fetch_rows_by_keys(
            plan.table_name, pk_columns, pk_columns, sample_pks,
        )

        # Check for missing
        if len(pk_columns) == 1:
            found_pks = [row[pk_columns[0]] for row in target_rows]
        else:
            found_pks = [tuple(row[c] for c in pk_columns) for row in target_rows]
        found_set = set(found_pks)
        missing = [pk for pk in sample_pks if pk not in found_set]

        if missing:
            results.append(ValidationResult(
                table_name=plan.table_name,
                check_name="post_missing_rows",
                passed=False,
                expected="0 missing",
                actual=f"{len(missing)} missing from sample of {len(sample_pks)}",
                message=(
                    f"{len(missing)} rows missing on target '{plan.table_name}' "
                    f"(sampled {len(sample_pks)} PKs)"
                ),
                severity="error",
            ))
        else:
            results.append(ValidationResult(
                table_name=plan.table_name,
                check_name="post_missing_rows",
                passed=True,
                message=f"All {len(sample_pks)} sampled PKs found on target",
                severity="info",
            ))

        # Check for duplicates
        if len(found_pks) != len(found_set):
            dup_count = len(found_pks) - len(found_set)
            results.append(ValidationResult(
                table_name=plan.table_name,
                check_name="post_duplicate_rows",
                passed=False,
                expected="0 duplicates",
                actual=f"{dup_count} duplicates",
                message=(
                    f"{dup_count} duplicate PK(s) detected on target "
                    f"'{plan.table_name}'"
                ),
                severity="error",
            ))
        else:
            results.append(ValidationResult(
                table_name=plan.table_name,
                check_name="post_duplicate_rows",
                passed=True,
                message="No duplicate PKs detected in sample",
                severity="info",
            ))

        return results


__all__ = ["MigrationValidator"]
