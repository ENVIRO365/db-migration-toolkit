"""Pipeline orchestrator — runs the full migration pipeline.

Stages: inspect -> compare -> plan -> validate(pre) -> migrate -> validate(post)
Each stage's output feeds the next automatically.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from dbmigrate.comparison import DeltaDetector, SchemaComparator
from dbmigrate.comparison.dependency import DependencyGraph
from dbmigrate.config import ProfileConfig, load_profile
from dbmigrate.database import Database, get_adapter
from dbmigrate.discovery import discover_both
from dbmigrate.logging import configure_logging, log_event
from dbmigrate.models import (
    MigrationManifest,
    MigrationMode,
    MigrationOperation,
    MigrationTablePlan,
    TableDelta,
)
from dbmigrate.performance import PerformanceTracker
from dbmigrate.policy import AutomationPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """Outcome of a single pipeline stage."""

    stage_name: str
    status: str  # "success", "failed", "skipped"
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    """Aggregate outcome of a full pipeline run."""

    migration_id: str
    profile_name: str
    stages: list[StageResult] = field(default_factory=list)
    total_duration: float = 0.0
    total_rows: int = 0
    success: bool = True
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialise the result to a machine-readable JSON string."""
        return json.dumps(asdict(self), indent=2, default=str)

    def to_summary(self) -> str:
        """Produce a human-readable summary string."""
        lines: list[str] = [
            f"Migration {self.migration_id} ({'SUCCESS' if self.success else 'FAILED'})",
            f"Profile: {self.profile_name}",
            f"Total duration: {self.total_duration:.2f}s",
            f"Total rows: {self.total_rows:,}",
            "",
            "Stages:",
        ]
        for stage in self.stages:
            status_icon = "\u2705" if stage.status == "success" else (
                "\u26a0\ufe0f" if stage.status == "skipped" else "\u274c"
            )
            lines.append(f"  {status_icon} {stage.stage_name}: {stage.status} ({stage.duration_seconds:.2f}s)")
            if stage.errors:
                for err in stage.errors:
                    lines.append(f"      ERROR: {err}")
        if self.errors:
            lines.append("")
            lines.append("Pipeline errors:")
            for err in self.errors:
                lines.append(f"  - {err}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class PipelineOrchestrator:
    """Runs the full migration pipeline end-to-end.

    Parameters
    ----------
    profile:
        Loaded profile configuration.
    profiles_dir:
        Directory containing profile definitions.
    """

    STAGES = ("INSPECT", "COMPARE", "PLAN", "VALIDATE_PRE", "MIGRATE", "VALIDATE_POST")

    def __init__(self, profile: ProfileConfig, profiles_dir: Path) -> None:
        self._profile = profile
        self._profiles_dir = profiles_dir
        self._tracker = PerformanceTracker()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        confirm_deletes: bool = False,
        resume: bool = False,
    ) -> PipelineResult:
        """Execute the full pipeline.

        Parameters
        ----------
        dry_run:
            When ``True`` run inspect/compare/plan/validate only — do
            not mutate the target database.
        confirm:
            Required to be ``True`` for actual migration execution.
        confirm_deletes:
            Required to be ``True`` when rollback mode includes DELETEs.
        resume:
            When ``True`` attempt to resume from the last checkpoint.

        Returns
        -------
        PipelineResult
            Aggregate outcome with per-stage detail.
        """
        migration_id = f"mig-{uuid.uuid4().hex[:12]}"
        configure_logging(migration_id=migration_id)
        log = logging.getLogger(f"dbmigrate.run.{migration_id}")
        result = PipelineResult(migration_id=migration_id, profile_name=self._profile.name)
        pipeline_start = time.monotonic()

        log.info("Starting pipeline %s (profile=%s, dry_run=%s)", migration_id, self._profile.name, dry_run)

        # --- Resolve DSNs from environment ---
        source_dsn = os.environ.get(self._profile.source.dsn_env, "")
        target_dsn = os.environ.get(self._profile.target.dsn_env, "")
        if not source_dsn:
            msg = f"Source DSN environment variable '{self._profile.source.dsn_env}' is not set"
            result.errors.append(msg)
            result.success = False
            log.error(msg)
            return result
        if not target_dsn:
            msg = f"Target DSN environment variable '{self._profile.target.dsn_env}' is not set"
            result.errors.append(msg)
            result.success = False
            log.error(msg)
            return result

        # --- Create adapters ---
        try:
            source_adapter_cls = get_adapter(self._profile.source.type)
            target_adapter_cls = get_adapter(self._profile.target.type)
        except ValueError as exc:
            result.errors.append(str(exc))
            result.success = False
            return result

        source_db = source_adapter_cls(dsn=source_dsn, schema=self._profile.source.schema_name)
        target_db = target_adapter_cls(dsn=target_dsn, schema=self._profile.target.schema_name)

        # --- Automation policy ---
        policy = AutomationPolicy(self._profile.automation, target_dsn)

        # Shared state passed between stages
        source_meta = None
        target_meta = None
        manifest: Optional[MigrationManifest] = None

        try:
            source_db.connect()
            target_db.connect()

            # === STAGE: INSPECT ===
            stage_result = self._run_stage(
                "INSPECT", migration_id,
                lambda: self._stage_inspect(source_db, target_db, migration_id),
            )
            result.stages.append(stage_result)
            if stage_result.status == "failed":
                result.success = False
                return result
            source_meta = stage_result.details.get("source_meta")
            target_meta = stage_result.details.get("target_meta")

            # === STAGE: COMPARE ===
            stage_result = self._run_stage(
                "COMPARE", migration_id,
                lambda: self._stage_compare(
                    source_db, target_db, source_meta, target_meta, migration_id,
                ),
            )
            result.stages.append(stage_result)
            if stage_result.status == "failed":
                result.success = False
                return result

            # === STAGE: PLAN ===
            stage_result = self._run_stage(
                "PLAN", migration_id,
                lambda: self._stage_plan(
                    source_meta, target_meta,
                    stage_result.details.get("deltas", {}),
                    stage_result.details.get("schema_result"),
                    migration_id,
                ),
            )
            result.stages.append(stage_result)
            if stage_result.status == "failed":
                result.success = False
                return result
            manifest = stage_result.details.get("manifest")

            # === STAGE: VALIDATE_PRE ===
            stage_result = self._run_stage(
                "VALIDATE_PRE", migration_id,
                lambda: self._stage_validate_pre(manifest, source_meta, target_meta, migration_id),
            )
            result.stages.append(stage_result)
            if stage_result.status == "failed":
                result.success = False
                return result

            # === STAGE: MIGRATE ===
            if dry_run:
                log.info("Dry-run mode — skipping MIGRATE and VALIDATE_POST stages")
                result.stages.append(StageResult(stage_name="MIGRATE", status="skipped"))
                result.stages.append(StageResult(stage_name="VALIDATE_POST", status="skipped"))
            else:
                # Check confirmation requirements
                if not confirm:
                    msg = "Migration requires --confirm flag to execute"
                    log.warning(msg)
                    result.stages.append(StageResult(stage_name="MIGRATE", status="skipped", errors=[msg]))
                    result.stages.append(StageResult(stage_name="VALIDATE_POST", status="skipped"))
                elif policy.requires_confirmation(manifest) and not confirm:
                    msg = "Automation policy requires interactive confirmation"
                    log.warning(msg)
                    result.stages.append(StageResult(stage_name="MIGRATE", status="skipped", errors=[msg]))
                    result.stages.append(StageResult(stage_name="VALIDATE_POST", status="skipped"))
                elif manifest and policy.requires_delete_confirmation(manifest) and not confirm_deletes:
                    msg = "Manifest contains DELETEs — requires --confirm-deletes flag"
                    log.warning(msg)
                    result.stages.append(StageResult(stage_name="MIGRATE", status="skipped", errors=[msg]))
                    result.stages.append(StageResult(stage_name="VALIDATE_POST", status="skipped"))
                else:
                    stage_result = self._run_stage(
                        "MIGRATE", migration_id,
                        lambda: self._stage_migrate(
                            source_db, target_db, manifest, migration_id,
                        ),
                    )
                    result.stages.append(stage_result)
                    if stage_result.status == "failed":
                        result.success = False
                        return result
                    result.total_rows = stage_result.details.get("total_rows", 0)

                    # === STAGE: VALIDATE_POST ===
                    stage_result = self._run_stage(
                        "VALIDATE_POST", migration_id,
                        lambda: self._stage_validate_post(
                            source_db, target_db, manifest, migration_id,
                        ),
                    )
                    result.stages.append(stage_result)
                    if stage_result.status == "failed":
                        result.success = False

        except Exception as exc:
            log.exception("Pipeline failed with unhandled exception")
            result.errors.append(str(exc))
            result.success = False
        finally:
            source_db.close()
            target_db.close()
            result.total_duration = time.monotonic() - pipeline_start
            log.info("Pipeline %s finished in %.2fs — %s", migration_id, result.total_duration, "SUCCESS" if result.success else "FAILED")

            # Write JSON run report
            self._write_report(result)

        return result

    def run_stage(self, stage_name: str, **kwargs: Any) -> StageResult:
        """Run a single named stage (for CLI per-stage execution).

        Parameters
        ----------
        stage_name:
            One of the ``STAGES`` constants.
        **kwargs:
            Stage-specific keyword arguments.

        Returns
        -------
        StageResult
        """
        if stage_name.upper() not in self.STAGES:
            return StageResult(
                stage_name=stage_name,
                status="failed",
                errors=[f"Unknown stage: {stage_name}. Valid: {', '.join(self.STAGES)}"],
            )
        # Delegate to run() for now — individual stage execution requires
        # more context management (connections, prior-stage outputs).
        # This is a placeholder for future per-stage CLI support.
        return StageResult(
            stage_name=stage_name,
            status="skipped",
            errors=["Per-stage execution not yet implemented — use run()"],
        )

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage_inspect(
        self,
        source_db: Database,
        target_db: Database,
        migration_id: str,
    ) -> dict[str, Any]:
        log_event(migration_id, "INSPECT", None, None, "start")
        source_meta, target_meta = discover_both(source_db, target_db)
        log_event(migration_id, "INSPECT", None, None, "complete", {
            "source_tables": len(source_meta.tables),
            "target_tables": len(target_meta.tables),
        })
        return {"source_meta": source_meta, "target_meta": target_meta}

    def _stage_compare(
        self,
        source_db: Database,
        target_db: Database,
        source_meta: Any,
        target_meta: Any,
        migration_id: str,
    ) -> dict[str, Any]:
        log_event(migration_id, "COMPARE", None, None, "start")

        comparator = SchemaComparator()
        schema_result = comparator.compare(source_meta, target_meta)

        detector = DeltaDetector()
        deltas: dict[str, TableDelta] = {}
        strategy = self._profile.comparison.strategy

        for table_name in schema_result.common_tables:
            if table_name in self._profile.skip_tables:
                logger.info("Skipping table '%s' (in skip_tables)", table_name)
                continue

            tbl_meta = source_meta.tables.get(table_name)
            pk_cols = self._resolve_pk_columns(table_name, tbl_meta)
            if not pk_cols:
                logger.warning(
                    "Skipping delta detection for '%s' — no PK (real or virtual)", table_name
                )
                continue

            columns = [c.name for c in tbl_meta.columns] if tbl_meta else []

            log_event(migration_id, "COMPARE", table_name, None, "delta_start")
            delta = detector.detect_delta(
                source_db, target_db, table_name, pk_cols, columns, strategy,
            )
            deltas[table_name] = delta
            log_event(migration_id, "COMPARE", table_name, None, "delta_complete", {
                "inserts": len(delta.insert_pks),
                "updates": len(delta.update_pks),
                "deletes": len(delta.delete_pks),
                "unchanged": delta.unchanged_count,
            })

        log_event(migration_id, "COMPARE", None, None, "complete", {
            "tables_compared": len(deltas),
        })
        return {"schema_result": schema_result, "deltas": deltas}

    def _resolve_pk_columns(
        self,
        table_name: str,
        tbl_meta: Any,
    ) -> list[str]:
        """Resolve PK columns for a table: real PK > virtual PK > empty.

        Returns an empty list if no PK can be determined (table will be skipped).
        """
        # 1. Check for a virtual PK override in profile config
        virtual = self._profile.virtual_pk.get(table_name.lower())
        if virtual:
            return virtual

        # 2. Use real PK from metadata (single or composite)
        if tbl_meta and tbl_meta.primary_key and tbl_meta.primary_key.columns:
            return [c.lower() for c in tbl_meta.primary_key.columns]

        return []

    def _stage_plan(
        self,
        source_meta: Any,
        target_meta: Any,
        deltas: dict[str, TableDelta],
        schema_result: Any,
        migration_id: str,
    ) -> dict[str, Any]:
        log_event(migration_id, "PLAN", None, None, "start")

        dep_graph = DependencyGraph.build(source_meta.tables)
        mode = self._profile.migration.mode

        table_plans: list[MigrationTablePlan] = []
        total_rows = 0

        delete_allowed = set(self._profile.delete_allowed_tables)

        for table_name, delta in deltas.items():
            level = dep_graph.levels.get(table_name.lower(), 0)

            # Strip unauthorised DELETEs — if the table is not in
            # delete_allowed_tables, drop its delete_pks so neither the
            # plan nor validation ever sees them.
            if delta.delete_pks and table_name not in delete_allowed:
                stripped = len(delta.delete_pks)
                logger.info(
                    "Stripping %d DELETE(s) for '%s' — not in delete_allowed_tables",
                    stripped, table_name,
                )
                delta = TableDelta(
                    table_name=delta.table_name,
                    insert_pks=delta.insert_pks,
                    update_pks=delta.update_pks,
                    delete_pks=[],           # <-- stripped
                    unchanged_count=delta.unchanged_count,
                    source_count=delta.source_count,
                    target_count=delta.target_count,
                )

            # Determine the primary operation
            if delta.insert_pks and not delta.update_pks and not delta.delete_pks:
                operation = MigrationOperation.INSERT
            elif delta.update_pks and not delta.insert_pks and not delta.delete_pks:
                operation = MigrationOperation.UPDATE
            elif not delta.insert_pks and not delta.update_pks and not delta.delete_pks:
                operation = MigrationOperation.NO_ACTION
            else:
                # Mixed — use INSERT as primary, sub-operations handled at batch level
                operation = MigrationOperation.INSERT

            row_count = len(delta.insert_pks) + len(delta.update_pks) + len(delta.delete_pks)

            # Column mappings from schema comparison
            mappings = schema_result.column_mappings.get(table_name, []) if schema_result else []

            # Resolve PK columns (real or virtual)
            tbl_meta = source_meta.tables.get(table_name)
            pk_cols = self._resolve_pk_columns(table_name, tbl_meta)

            plan = MigrationTablePlan(
                table_name=table_name,
                operation=operation,
                row_count=row_count,
                dependency_level=level,
                column_mappings=mappings,
                delta=delta,
                pk_columns=pk_cols,
            )
            table_plans.append(plan)
            total_rows += row_count

        # Sort by dependency level for execution order
        table_plans.sort(key=lambda p: p.dependency_level)

        insert_tables = sum(1 for p in table_plans if p.operation == MigrationOperation.INSERT)
        update_tables = sum(1 for p in table_plans if p.operation == MigrationOperation.UPDATE)
        delete_tables = sum(1 for p in table_plans if p.operation == MigrationOperation.DELETE)
        no_action = sum(1 for p in table_plans if p.operation == MigrationOperation.NO_ACTION)

        manifest = MigrationManifest(
            migration_id=migration_id,
            profile_name=self._profile.name,
            mode=mode,
            tables=table_plans,
            total_rows=total_rows,
            insert_tables=insert_tables,
            update_tables=update_tables,
            delete_tables=delete_tables,
            no_action_tables=no_action,
        )

        log_event(migration_id, "PLAN", None, None, "complete", {
            "total_tables": len(table_plans),
            "total_rows": total_rows,
            "inserts": insert_tables,
            "updates": update_tables,
            "deletes": delete_tables,
            "no_action": no_action,
        })
        return {"manifest": manifest}

    def _stage_validate_pre(
        self,
        manifest: Optional[MigrationManifest],
        source_meta: Any,
        target_meta: Any,
        migration_id: str,
    ) -> dict[str, Any]:
        log_event(migration_id, "VALIDATE_PRE", None, None, "start")
        errors: list[str] = []

        if manifest is None:
            errors.append("No manifest available for pre-validation")
        else:
            # Basic sanity checks
            if manifest.total_rows < 0:
                errors.append("Manifest total_rows is negative")
            for plan in manifest.tables:
                if plan.row_count < 0:
                    errors.append(f"Table '{plan.table_name}' has negative row_count")
                # Check that delete tables are allowed in rollback mode
                if (
                    plan.delta
                    and plan.delta.delete_pks
                    and manifest.mode == MigrationMode.ROLLBACK
                    and plan.table_name not in self._profile.delete_allowed_tables
                ):
                    errors.append(
                        f"Table '{plan.table_name}' has {len(plan.delta.delete_pks)} DELETEs "
                        f"but is not in delete_allowed_tables"
                    )

        status = "failed" if errors else "success"
        log_event(migration_id, "VALIDATE_PRE", None, None, "complete", {
            "status": status,
            "error_count": len(errors),
        })
        return {"validation_errors": errors}

    def _stage_migrate(
        self,
        source_db: Database,
        target_db: Database,
        manifest: Optional[MigrationManifest],
        migration_id: str,
    ) -> dict[str, Any]:
        log_event(migration_id, "MIGRATE", None, None, "start")

        if manifest is None:
            return {"total_rows": 0, "errors": ["No manifest"]}

        total_rows = 0
        errors: list[str] = []
        batch_size = self._profile.migration.batch_size

        for plan in manifest.tables:
            if plan.operation == MigrationOperation.NO_ACTION:
                continue

            log_event(migration_id, "MIGRATE", plan.table_name, None, "table_start", {
                "operation": plan.operation.value,
                "row_count": plan.row_count,
            })

            tbl_start = time.monotonic()
            try:
                rows = self._migrate_table(source_db, target_db, plan, batch_size, migration_id)
                total_rows += rows
                duration = time.monotonic() - tbl_start
                self._tracker.track_operation(
                    plan.table_name, plan.operation.value, rows, duration,
                )
                log_event(migration_id, "MIGRATE", plan.table_name, None, "table_complete", {
                    "rows": rows,
                    "duration_seconds": round(duration, 3),
                })
            except Exception as exc:
                duration = time.monotonic() - tbl_start
                err_msg = f"Migration failed for '{plan.table_name}': {exc}"
                errors.append(err_msg)
                logger.error(err_msg, exc_info=True)
                log_event(migration_id, "MIGRATE", plan.table_name, None, "table_failed", {
                    "error": str(exc),
                })
                # Abort on first failure
                break

        log_event(migration_id, "MIGRATE", None, None, "complete", {
            "total_rows": total_rows,
            "error_count": len(errors),
        })
        return {"total_rows": total_rows, "errors": errors}

    def _stage_validate_post(
        self,
        source_db: Database,
        target_db: Database,
        manifest: Optional[MigrationManifest],
        migration_id: str,
    ) -> dict[str, Any]:
        log_event(migration_id, "VALIDATE_POST", None, None, "start")
        errors: list[str] = []

        if manifest is None:
            errors.append("No manifest for post-validation")
        else:
            for plan in manifest.tables:
                if plan.operation == MigrationOperation.NO_ACTION:
                    continue
                try:
                    src_count = source_db.get_row_count(plan.table_name)
                    tgt_count = target_db.get_row_count(plan.table_name)
                    if src_count != tgt_count:
                        errors.append(
                            f"Row count mismatch for '{plan.table_name}': "
                            f"source={src_count}, target={tgt_count}"
                        )
                except Exception as exc:
                    errors.append(f"Post-validation query failed for '{plan.table_name}': {exc}")

        status = "failed" if errors else "success"
        log_event(migration_id, "VALIDATE_POST", None, None, "complete", {
            "status": status,
            "error_count": len(errors),
        })
        return {"validation_errors": errors}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _migrate_table(
        self,
        source_db: Database,
        target_db: Database,
        plan: MigrationTablePlan,
        batch_size: int,
        migration_id: str,
    ) -> int:
        """Execute row-level migration for a single table. Returns rows processed."""
        delta = plan.delta
        if delta is None:
            return 0

        table_name = plan.table_name
        # Only include columns that exist in BOTH source and target
        source_columns = [m.source_column for m in plan.column_mappings if not m.target_only and not m.source_only]
        target_columns = [m.target_column for m in plan.column_mappings if not m.target_only and not m.source_only]

        # Use resolved pk_columns from plan; fallback to first column
        pk_cols = plan.pk_columns if plan.pk_columns else [source_columns[0]] if source_columns else ["id"]

        total = 0

        # INSERTs
        if delta.insert_pks:
            for i in range(0, len(delta.insert_pks), batch_size):
                batch_pks = delta.insert_pks[i : i + batch_size]
                rows = source_db.fetch_rows_by_keys(table_name, source_columns, pk_cols, batch_pks)
                if rows:
                    row_tuples = [tuple(row.get(c) for c in source_columns) for row in rows]
                    inserted = target_db.insert_batch(
                        table_name, target_columns, row_tuples, plan.identity_strategy,
                    )
                    total += inserted

        # UPDATEs
        if delta.update_pks:
            # Exclude PK columns from update set
            pk_set = set(c.lower() for c in pk_cols)
            update_src_cols = [c for c in source_columns if c.lower() not in pk_set]
            update_tgt_cols = [c for c in target_columns if c.lower() not in pk_set]
            for i in range(0, len(delta.update_pks), batch_size):
                batch_pks = delta.update_pks[i : i + batch_size]
                rows = source_db.fetch_rows_by_keys(table_name, source_columns, pk_cols, batch_pks)
                if rows:
                    # Row tuples: (update_col1, ..., update_colN, pk_col1, ..., pk_colN)
                    row_tuples = [
                        tuple(row.get(c) for c in update_src_cols)
                        + tuple(row.get(c) for c in pk_cols)
                        for row in rows
                    ]
                    updated = target_db.update_batch(table_name, pk_cols, update_tgt_cols, row_tuples)
                    total += updated

        # DELETEs
        if delta.delete_pks:
            for i in range(0, len(delta.delete_pks), batch_size):
                batch_pks = delta.delete_pks[i : i + batch_size]
                # Normalise to tuples for delete_batch
                if len(pk_cols) == 1:
                    pk_tuples = [(pk,) for pk in batch_pks]
                else:
                    pk_tuples = [pk if isinstance(pk, tuple) else (pk,) for pk in batch_pks]
                deleted = target_db.delete_batch(table_name, pk_cols, pk_tuples)
                total += deleted

        return total

    def _run_stage(
        self,
        stage_name: str,
        migration_id: str,
        fn: Any,
    ) -> StageResult:
        """Execute a stage function with timing and error handling."""
        logger.info("=== STAGE: %s ===", stage_name)
        start = time.monotonic()
        try:
            details = fn()
            duration = time.monotonic() - start
            errors = details.pop("errors", []) if isinstance(details, dict) else []
            validation_errors = details.pop("validation_errors", []) if isinstance(details, dict) else []
            all_errors = errors + validation_errors
            status = "failed" if all_errors else "success"
            logger.info("Stage %s completed in %.2fs — %s", stage_name, duration, status)
            return StageResult(
                stage_name=stage_name,
                status=status,
                duration_seconds=round(duration, 3),
                details=details if isinstance(details, dict) else {},
                errors=all_errors,
            )
        except Exception as exc:
            duration = time.monotonic() - start
            logger.error("Stage %s failed after %.2fs: %s", stage_name, duration, exc, exc_info=True)
            return StageResult(
                stage_name=stage_name,
                status="failed",
                duration_seconds=round(duration, 3),
                errors=[str(exc)],
            )

    def _write_report(self, result: PipelineResult) -> None:
        """Write the JSON run report to the logs directory."""
        try:
            logs_dir = Path("logs")
            logs_dir.mkdir(parents=True, exist_ok=True)
            report_path = logs_dir / f"{result.migration_id}-report.json"
            report_path.write_text(result.to_json(), encoding="utf-8")
            logger.info("Run report written to %s", report_path)
        except Exception as exc:
            logger.warning("Failed to write run report: %s", exc)
