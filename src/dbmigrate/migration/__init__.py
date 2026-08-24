"""Migration execution engine.

Transforms comparison results into executable migration plans and
runs them against a target database with batched transactions,
circuit-breaker protection, and checkpoint-based resume capability.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from dbmigrate.checkpoint import CheckpointStore
from dbmigrate.comparison import SchemaComparisonResult
from dbmigrate.comparison.dependency import DependencyGraph
from dbmigrate.database import Database
from dbmigrate.models import (
    BatchStatus,
    ColumnMapping,
    IdentityStrategy,
    MigrationBatch,
    MigrationManifest,
    MigrationMode,
    MigrationOperation,
    MigrationTablePlan,
    TableDelta,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CircuitBreakerError(Exception):
    """Raised when a circuit breaker trips due to excessive failures."""

    def __init__(self, table_name: str, consecutive_failures: int) -> None:
        self.table_name = table_name
        self.consecutive_failures = consecutive_failures
        super().__init__(
            f"Circuit breaker tripped for table '{table_name}': "
            f"{consecutive_failures} consecutive failures"
        )


class MigrationError(Exception):
    """General migration execution error."""


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """Tracks failure rates and stops execution when thresholds are exceeded.

    Maintains per-table consecutive failure counts and an overall failure
    rate across all tables.  When either threshold is breached, subsequent
    calls to :meth:`record_failure` raise :class:`CircuitBreakerError`.

    Parameters
    ----------
    max_consecutive_failures:
        Maximum consecutive failures for a single table before tripping.
    max_overall_failure_rate:
        Maximum ratio of failed batches to total batches (0.0–1.0).
    """

    max_consecutive_failures: int = 3
    max_overall_failure_rate: float = 0.5
    _consecutive_failures: dict[str, int] = field(default_factory=dict)
    _total_batches: int = 0
    _failed_batches: int = 0

    def record_success(self, table_name: str) -> None:
        """Record a successful batch execution for *table_name*."""
        self._consecutive_failures[table_name] = 0
        self._total_batches += 1

    def record_failure(self, table_name: str) -> None:
        """Record a failed batch execution for *table_name*.

        Raises
        ------
        CircuitBreakerError
            If consecutive failures for the table exceed the threshold,
            or the overall failure rate is too high.
        """
        count = self._consecutive_failures.get(table_name, 0) + 1
        self._consecutive_failures[table_name] = count
        self._total_batches += 1
        self._failed_batches += 1

        if count >= self.max_consecutive_failures:
            raise CircuitBreakerError(table_name, count)

        if self._total_batches >= 5:
            rate = self._failed_batches / self._total_batches
            if rate >= self.max_overall_failure_rate:
                raise CircuitBreakerError(
                    table_name,
                    count,
                )

    def is_tripped(self, table_name: str) -> bool:
        """Return whether the breaker is already tripped for *table_name*."""
        return (
            self._consecutive_failures.get(table_name, 0)
            >= self.max_consecutive_failures
        )

    @property
    def overall_failure_rate(self) -> float:
        """Current overall failure rate."""
        if self._total_batches == 0:
            return 0.0
        return self._failed_batches / self._total_batches


# ---------------------------------------------------------------------------
# DeltaPlanner
# ---------------------------------------------------------------------------


@dataclass
class ProfileConfig:
    """Subset of profile configuration relevant to migration planning."""

    batch_size: int = 5000
    mode: MigrationMode = MigrationMode.SYNC
    identity_strategies: dict[str, IdentityStrategy] = field(default_factory=dict)
    skip_tables: list[str] = field(default_factory=list)


class DeltaPlanner:
    """Produces a :class:`MigrationManifest` from comparison results.

    Combines schema comparison output, row-level deltas, dependency graph,
    and profile configuration into a fully ordered, batched execution plan.
    """

    def plan(
        self,
        comparison: SchemaComparisonResult,
        deltas: dict[str, TableDelta],
        dep_graph: DependencyGraph,
        config: ProfileConfig,
    ) -> MigrationManifest:
        """Build a migration manifest.

        Parameters
        ----------
        comparison:
            Schema comparison result with column mappings.
        deltas:
            Row-level deltas keyed by table name.
        dep_graph:
            FK dependency graph for ordering.
        config:
            Profile configuration (batch size, mode, identity strategies).

        Returns
        -------
        MigrationManifest
            Complete execution plan with ordered table plans and batches.
        """
        migration_id = str(uuid.uuid4())
        table_plans: list[MigrationTablePlan] = []
        all_batches: list[MigrationBatch] = []

        excluded = {t.lower() for t in config.skip_tables}
        insert_count = update_count = delete_count = no_action_count = 0
        total_rows = 0

        for table_name in comparison.common_tables:
            if table_name.lower() in excluded:
                logger.debug("Skipping excluded table '%s'", table_name)
                continue

            delta = deltas.get(table_name)
            if delta is None:
                logger.debug("No delta for table '%s' — skipping", table_name)
                continue

            # Determine dependency level
            try:
                dep_level = dep_graph.get_level(table_name)
            except KeyError:
                dep_level = 0

            # Get column mappings and detect source-only columns
            col_mappings = comparison.column_mappings.get(table_name, [])
            source_only_cols = [
                m.source_column for m in col_mappings if m.source_only
            ]
            target_only_cols = [
                m.target_column for m in col_mappings if m.target_only
            ]

            # Identity strategy
            identity_strategy = config.identity_strategies.get(
                table_name, IdentityStrategy.NONE
            )

            # Classify operations and create plans + batches
            plans_for_table = self._classify_operations(
                table_name=table_name,
                delta=delta,
                dep_level=dep_level,
                col_mappings=col_mappings,
                source_only_cols=source_only_cols,
                target_only_cols=target_only_cols,
                identity_strategy=identity_strategy,
                mode=config.mode,
                batch_size=config.batch_size,
                migration_id=migration_id,
            )

            for plan, batches in plans_for_table:
                table_plans.append(plan)
                all_batches.extend(batches)
                total_rows += plan.row_count

                if plan.operation == MigrationOperation.INSERT:
                    insert_count += 1
                elif plan.operation == MigrationOperation.UPDATE:
                    update_count += 1
                elif plan.operation == MigrationOperation.DELETE:
                    delete_count += 1
                else:
                    no_action_count += 1

        # Sort table plans by dependency level
        table_plans.sort(key=lambda p: p.dependency_level)

        manifest = MigrationManifest(
            migration_id=migration_id,
            profile_name="",  # Caller sets this
            mode=config.mode,
            tables=table_plans,
            batches=all_batches,
            total_rows=total_rows,
            insert_tables=insert_count,
            update_tables=update_count,
            delete_tables=delete_count,
            no_action_tables=no_action_count,
        )

        logger.info(
            "Migration plan '%s': %d tables (%d insert, %d update, %d delete, "
            "%d no_action), %d total rows, %d batches",
            migration_id,
            len(table_plans),
            insert_count,
            update_count,
            delete_count,
            no_action_count,
            total_rows,
            len(all_batches),
        )

        return manifest

    # -- private helpers ---------------------------------------------------

    def _classify_operations(
        self,
        table_name: str,
        delta: TableDelta,
        dep_level: int,
        col_mappings: list[ColumnMapping],
        source_only_cols: list[str],
        target_only_cols: list[str],
        identity_strategy: IdentityStrategy,
        mode: MigrationMode,
        batch_size: int,
        migration_id: str,
    ) -> list[tuple[MigrationTablePlan, list[MigrationBatch]]]:
        """Classify a table delta into one or more operation plans with batches.

        Returns a list of (plan, batches) tuples.  A single table may produce
        multiple plans (e.g. INSERT for new rows + UPDATE for changed rows +
        DELETE for removed rows in ROLLBACK mode).
        """
        results: list[tuple[MigrationTablePlan, list[MigrationBatch]]] = []

        # INSERT plan for source-only rows
        if delta.insert_pks:
            plan = MigrationTablePlan(
                table_name=table_name,
                operation=MigrationOperation.INSERT,
                row_count=len(delta.insert_pks),
                identity_strategy=identity_strategy,
                dependency_level=dep_level,
                column_mappings=col_mappings,
                source_only_columns=source_only_cols,
                target_only_columns=target_only_cols,
                delta=delta,
            )
            batches = self._create_batches(
                migration_id, table_name, MigrationOperation.INSERT,
                delta.insert_pks, batch_size,
            )
            results.append((plan, batches))

        # UPDATE plan for changed rows
        if delta.update_pks:
            plan = MigrationTablePlan(
                table_name=table_name,
                operation=MigrationOperation.UPDATE,
                row_count=len(delta.update_pks),
                identity_strategy=IdentityStrategy.NONE,
                dependency_level=dep_level,
                column_mappings=col_mappings,
                source_only_columns=source_only_cols,
                target_only_columns=target_only_cols,
                delta=delta,
            )
            batches = self._create_batches(
                migration_id, table_name, MigrationOperation.UPDATE,
                delta.update_pks, batch_size,
            )
            results.append((plan, batches))

        # DELETE plan for target-only rows (ROLLBACK mode only)
        if delta.delete_pks and mode == MigrationMode.ROLLBACK:
            plan = MigrationTablePlan(
                table_name=table_name,
                operation=MigrationOperation.DELETE,
                row_count=len(delta.delete_pks),
                identity_strategy=IdentityStrategy.NONE,
                dependency_level=dep_level,
                column_mappings=col_mappings,
                source_only_columns=source_only_cols,
                target_only_columns=target_only_cols,
                delta=delta,
            )
            batches = self._create_batches(
                migration_id, table_name, MigrationOperation.DELETE,
                delta.delete_pks, batch_size,
            )
            results.append((plan, batches))

        # NO_ACTION if nothing to do
        if not results:
            plan = MigrationTablePlan(
                table_name=table_name,
                operation=MigrationOperation.NO_ACTION,
                row_count=0,
                dependency_level=dep_level,
                column_mappings=col_mappings,
                source_only_columns=source_only_cols,
                target_only_columns=target_only_cols,
                delta=delta,
            )
            results.append((plan, []))

        return results

    @staticmethod
    def _create_batches(
        migration_id: str,
        table_name: str,
        operation: MigrationOperation,
        pk_values: list[Any],
        batch_size: int,
    ) -> list[MigrationBatch]:
        """Split PK values into sized batches."""
        batches: list[MigrationBatch] = []
        for i in range(0, len(pk_values), batch_size):
            chunk = pk_values[i : i + batch_size]
            start_pk = chunk[0] if chunk else None
            end_pk = chunk[-1] if chunk else None
            batch = MigrationBatch(
                batch_id=f"{migration_id}:{table_name}:{operation.value}:{i // batch_size}",
                table_name=table_name,
                operation=operation,
                start_pk=start_pk,
                end_pk=end_pk,
                row_count=len(chunk),
                status=BatchStatus.PENDING,
            )
            batches.append(batch)
        return batches


# ---------------------------------------------------------------------------
# BatchExecutor
# ---------------------------------------------------------------------------


class BatchExecutor:
    """Executes migration batches against a target database.

    Processes batches in dependency order with per-batch transactions,
    circuit-breaker protection, and checkpoint-based resumability.

    Parameters
    ----------
    source_db:
        Connected adapter for the source database (read-only).
    target_db:
        Connected adapter for the target database (read-write).
    checkpoint:
        Checkpoint store for tracking progress.
    circuit_breaker:
        Optional circuit breaker; a default is created if not supplied.
    """

    def __init__(
        self,
        source_db: Database,
        target_db: Database,
        checkpoint: CheckpointStore,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.source_db = source_db
        self.target_db = target_db
        self.checkpoint = checkpoint
        self.breaker = circuit_breaker or CircuitBreaker()

    def execute(self, manifest: MigrationManifest) -> list[MigrationBatch]:
        """Execute all batches in a manifest.

        Processes INSERT and UPDATE batches in dependency order (parents
        first), and DELETE batches in reverse dependency order (children
        first).

        Parameters
        ----------
        manifest:
            The migration manifest produced by :class:`DeltaPlanner`.

        Returns
        -------
        list[MigrationBatch]
            All batches with updated status information.

        Raises
        ------
        CircuitBreakerError
            If too many consecutive failures occur for any table.
        """
        self.checkpoint.create_migration(manifest.migration_id, manifest.profile_name)

        # Separate batches by operation type
        insert_batches = [
            b for b in manifest.batches
            if b.operation == MigrationOperation.INSERT
        ]
        update_batches = [
            b for b in manifest.batches
            if b.operation == MigrationOperation.UPDATE
        ]
        delete_batches = [
            b for b in manifest.batches
            if b.operation == MigrationOperation.DELETE
        ]

        # Build lookup for table plans
        plan_lookup: dict[tuple[str, MigrationOperation], MigrationTablePlan] = {}
        for plan in manifest.tables:
            plan_lookup[(plan.table_name, plan.operation)] = plan

        completed: list[MigrationBatch] = []

        # INSERT: dependency order (parents first)
        insert_batches.sort(
            key=lambda b: plan_lookup.get(
                (b.table_name, b.operation),
                MigrationTablePlan(table_name=b.table_name, operation=b.operation),
            ).dependency_level
        )

        # UPDATE: dependency order (parents first)
        update_batches.sort(
            key=lambda b: plan_lookup.get(
                (b.table_name, b.operation),
                MigrationTablePlan(table_name=b.table_name, operation=b.operation),
            ).dependency_level
        )

        # DELETE: reverse dependency order (children first)
        delete_batches.sort(
            key=lambda b: plan_lookup.get(
                (b.table_name, b.operation),
                MigrationTablePlan(table_name=b.table_name, operation=b.operation),
            ).dependency_level,
            reverse=True,
        )

        # Execute in order: INSERT -> UPDATE -> DELETE
        for batch in insert_batches + update_batches + delete_batches:
            # Skip already-completed batches (resume support)
            existing = self.checkpoint.get_last_completed_batch(
                manifest.migration_id, batch.table_name
            )
            if existing and existing.batch_id == batch.batch_id:
                logger.info(
                    "Skipping already-completed batch '%s'", batch.batch_id
                )
                batch.status = BatchStatus.SKIPPED
                completed.append(batch)
                continue

            # Skip if circuit breaker already tripped for this table
            if self.breaker.is_tripped(batch.table_name):
                logger.warning(
                    "Circuit breaker tripped — skipping batch '%s'",
                    batch.batch_id,
                )
                batch.status = BatchStatus.SKIPPED
                completed.append(batch)
                continue

            plan = plan_lookup.get((batch.table_name, batch.operation))
            result = self._execute_batch(batch, plan, manifest.migration_id)
            completed.append(result)

        return completed

    def _execute_batch(
        self,
        batch: MigrationBatch,
        plan: Optional[MigrationTablePlan],
        migration_id: str,
    ) -> MigrationBatch:
        """Execute a single batch within a transaction.

        Returns the batch with updated status fields.
        """
        batch.started_at = datetime.now(tz=timezone.utc)
        batch.status = BatchStatus.RUNNING
        self.checkpoint.mark_batch_started(batch.batch_id)
        self.checkpoint.save_batch(batch)

        start_time = time.monotonic()

        try:
            self.target_db.begin_transaction()

            rows_affected = 0
            if batch.operation == MigrationOperation.INSERT:
                rows_affected = self._execute_insert(batch, plan)
            elif batch.operation == MigrationOperation.UPDATE:
                rows_affected = self._execute_update(batch, plan)
            elif batch.operation == MigrationOperation.DELETE:
                rows_affected = self._execute_delete(batch, plan)

            self.target_db.commit()

            duration = time.monotonic() - start_time
            batch.status = BatchStatus.COMPLETED
            batch.completed_at = datetime.now(tz=timezone.utc)
            batch.row_count = rows_affected
            self.checkpoint.mark_batch_completed(batch.batch_id, rows_affected, checksum=None)
            self.breaker.record_success(batch.table_name)

            logger.info(
                "Batch '%s' completed: %d rows in %.2fs (%s %s)",
                batch.batch_id,
                rows_affected,
                duration,
                batch.operation.value,
                batch.table_name,
            )

        except CircuitBreakerError:
            # Re-raise circuit breaker errors without wrapping
            raise

        except Exception as exc:
            self.target_db.rollback_transaction()

            duration = time.monotonic() - start_time
            batch.status = BatchStatus.FAILED
            batch.completed_at = datetime.now(tz=timezone.utc)
            batch.error = str(exc)
            self.checkpoint.mark_batch_failed(batch.batch_id, str(exc))

            logger.error(
                "Batch '%s' failed after %.2fs: %s", batch.batch_id, duration, exc
            )

            self.breaker.record_failure(batch.table_name)

        return batch

    def _execute_insert(
        self,
        batch: MigrationBatch,
        plan: Optional[MigrationTablePlan],
    ) -> int:
        """Stream rows from source and insert into target."""
        if plan is None:
            raise MigrationError(
                f"No table plan found for INSERT on '{batch.table_name}'"
            )

        # Determine columns to transfer (exclude source-only)
        columns = [
            m.source_column
            for m in plan.column_mappings
            if not m.source_only and not m.target_only
        ]
        if not columns:
            logger.warning("No transferable columns for '%s'", batch.table_name)
            return 0

        # Fetch rows by PK range from source
        pk_columns = self._get_pk_columns(plan)
        if pk_columns and batch.start_pk is not None and batch.end_pk is not None:
            rows = self.source_db.fetch_rows_by_keys(
                batch.table_name,
                columns,
                pk_columns,
                list(range(batch.start_pk, batch.end_pk + 1)),
            )
        else:
            # Fallback: fetch all rows in the batch range
            fallback_cols = pk_columns if pk_columns else [columns[0]]
            rows = self.source_db.fetch_rows_by_keys(
                batch.table_name,
                columns,
                fallback_cols,
                [batch.start_pk] if batch.start_pk is not None else [],
            )

        if not rows:
            return 0

        # Convert dicts to tuples in column order
        row_tuples = [tuple(row.get(c) for c in columns) for row in rows]

        return self.target_db.insert_batch(
            batch.table_name,
            columns,
            row_tuples,
            identity_strategy=plan.identity_strategy,
        )

    def _execute_update(
        self,
        batch: MigrationBatch,
        plan: Optional[MigrationTablePlan],
    ) -> int:
        """Stream rows from source and update in target."""
        if plan is None:
            raise MigrationError(
                f"No table plan found for UPDATE on '{batch.table_name}'"
            )

        pk_columns = self._get_pk_columns(plan)
        if not pk_columns:
            raise MigrationError(
                f"Cannot UPDATE table '{batch.table_name}' without PK column(s)"
            )

        # All transferable non-PK columns
        pk_set = set(c.lower() for c in pk_columns)
        update_columns = [
            m.source_column
            for m in plan.column_mappings
            if not m.source_only
            and not m.target_only
            and m.source_column.lower() not in pk_set
        ]
        all_columns = pk_columns + update_columns

        # Fetch source rows for this batch's PK range
        if batch.start_pk is not None and batch.end_pk is not None:
            pk_values = list(range(batch.start_pk, batch.end_pk + 1))
        else:
            pk_values = []

        rows = self.source_db.fetch_rows_by_keys(
            batch.table_name, all_columns, pk_columns, pk_values,
        )

        if not rows:
            return 0

        # Convert to tuples: (update_col1, ..., update_colN, pk_col1, ..., pk_colN)
        row_tuples = [
            tuple(row.get(c) for c in update_columns)
            + tuple(row.get(c) for c in pk_columns)
            for row in rows
        ]

        return self.target_db.update_batch(
            batch.table_name,
            pk_columns,
            update_columns,
            row_tuples,
        )

    def _execute_delete(
        self,
        batch: MigrationBatch,
        plan: Optional[MigrationTablePlan],
    ) -> int:
        """Delete rows from target by PK."""
        if plan is None:
            raise MigrationError(
                f"No table plan found for DELETE on '{batch.table_name}'"
            )

        pk_columns = self._get_pk_columns(plan)
        if not pk_columns:
            raise MigrationError(
                f"Cannot DELETE from table '{batch.table_name}' without PK column(s)"
            )

        if batch.start_pk is not None and batch.end_pk is not None:
            pk_values = [(pk,) for pk in range(batch.start_pk, batch.end_pk + 1)]
        else:
            pk_values = []

        if not pk_values:
            return 0

        return self.target_db.delete_batch(
            batch.table_name, pk_columns, pk_values,
        )

    @staticmethod
    def _get_pk_columns(plan: MigrationTablePlan) -> list[str]:
        """Return PK columns from plan.pk_columns, with fallback to first mapping."""
        if plan.pk_columns:
            return plan.pk_columns
        if plan.column_mappings:
            return [plan.column_mappings[0].source_column]
        return []


__all__ = [
    "BatchExecutor",
    "CircuitBreaker",
    "CircuitBreakerError",
    "DeltaPlanner",
    "MigrationError",
    "ProfileConfig",
]
