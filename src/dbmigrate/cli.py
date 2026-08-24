"""CLI interface for db-migration-toolkit."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

# Auto-load .env from project root (no need for 'export' or 'set -a')
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from dbmigrate.config import list_profiles, load_profile
from dbmigrate.logging import configure_logging


def _profiles_dir() -> Path:
    """Resolve the default profiles directory."""
    return Path(__file__).parent.parent.parent / "profiles"


def _setup_logging(verbose: bool = False) -> None:
    """Configure basic logging for CLI usage."""
    level = logging.DEBUG if verbose else logging.INFO
    configure_logging(level=level)


def _resolve_pk_columns(cfg, table_name: str, tbl_meta) -> list[str]:
    """Resolve PK columns: virtual_pk > real PK > empty (skip)."""
    virtual = cfg.virtual_pk.get(table_name.lower())
    if virtual:
        return virtual
    if tbl_meta and tbl_meta.primary_key and tbl_meta.primary_key.columns:
        return [c.lower() for c in tbl_meta.primary_key.columns]
    return []


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """db-migration-toolkit — database migration pipeline."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.pass_context
def inspect(ctx: click.Context, profile: str) -> None:
    """Discover and display source/target metadata."""
    import os
    from dbmigrate.database import get_adapter
    from dbmigrate.discovery import discover_both

    cfg = load_profile(profile, _profiles_dir())

    source_dsn = os.environ.get(cfg.source.dsn_env, "")
    target_dsn = os.environ.get(cfg.target.dsn_env, "")
    if not source_dsn or not target_dsn:
        click.secho("ERROR: DSN environment variables not set.", fg="red")
        sys.exit(1)

    source_cls = get_adapter(cfg.source.type)
    target_cls = get_adapter(cfg.target.type)
    source_db = source_cls(dsn=source_dsn, schema=cfg.source.schema_name)
    target_db = target_cls(dsn=target_dsn, schema=cfg.target.schema_name)

    try:
        source_db.connect()
        target_db.connect()
        source_meta, target_meta = discover_both(source_db, target_db)

        click.secho(f"\nSource ({cfg.source.type}) — {len(source_meta.tables)} tables", fg="cyan", bold=True)
        _print_table_list(source_meta)
        click.secho(f"\nTarget ({cfg.target.type}) — {len(target_meta.tables)} tables", fg="cyan", bold=True)
        _print_table_list(target_meta)
    finally:
        source_db.close()
        target_db.close()


def _print_table_list(meta: object) -> None:
    """Print a formatted table list from DatabaseMetadata."""
    from dbmigrate.models import DatabaseMetadata

    if not isinstance(meta, DatabaseMetadata):
        return
    click.echo(f"  {'Table':<40} {'Columns':>8} {'Rows':>10} {'PK'}")
    click.echo(f"  {'─' * 40} {'─' * 8} {'─' * 10} {'─' * 20}")
    for name in sorted(meta.tables):
        tbl = meta.tables[name]
        pk_str = ", ".join(tbl.primary_key.columns) if tbl.primary_key else "—"
        click.echo(f"  {tbl.name:<40} {len(tbl.columns):>8} {tbl.row_count:>10,} {pk_str}")


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.option("--table", default=None, help="Compare a single table only.")
@click.pass_context
def compare(ctx: click.Context, profile: str, table: Optional[str]) -> None:
    """Compare schemas and show delta between source and target."""
    import os
    from dbmigrate.comparison import DeltaDetector, SchemaComparator
    from dbmigrate.database import get_adapter
    from dbmigrate.discovery import discover_both

    cfg = load_profile(profile, _profiles_dir())
    source_dsn = os.environ.get(cfg.source.dsn_env, "")
    target_dsn = os.environ.get(cfg.target.dsn_env, "")
    if not source_dsn or not target_dsn:
        click.secho("ERROR: DSN environment variables not set.", fg="red")
        sys.exit(1)

    source_cls = get_adapter(cfg.source.type)
    target_cls = get_adapter(cfg.target.type)
    source_db = source_cls(dsn=source_dsn, schema=cfg.source.schema_name)
    target_db = target_cls(dsn=target_dsn, schema=cfg.target.schema_name)

    try:
        source_db.connect()
        target_db.connect()
        source_meta, target_meta = discover_both(source_db, target_db)

        comparator = SchemaComparator()
        result = comparator.compare(source_meta, target_meta)

        # Schema differences
        if result.source_only_tables:
            click.secho(f"\nSource-only tables ({len(result.source_only_tables)}):", fg="yellow")
            for t in result.source_only_tables:
                click.echo(f"  + {t}")
        if result.target_only_tables:
            click.secho(f"\nTarget-only tables ({len(result.target_only_tables)}):", fg="yellow")
            for t in result.target_only_tables:
                click.echo(f"  - {t}")

        if result.table_differences:
            click.secho(f"\nColumn differences ({len(result.table_differences)} tables):", fg="yellow")
            for tname, diff in sorted(result.table_differences.items()):
                click.echo(f"  {tname}:")
                for cd in diff.column_diffs:
                    click.echo(f"    {cd.difference_type}: {cd.detail}")

        # Row-level delta for common tables
        tables_to_compare = [table] if table else result.common_tables
        detector = DeltaDetector()

        click.secho("\nRow-level delta:", fg="cyan", bold=True)
        click.echo(f"  {'Table':<40} {'Insert':>8} {'Update':>8} {'Delete':>8} {'Unchanged':>10}")
        click.echo(f"  {'─' * 40} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 10}")

        for tname in sorted(tables_to_compare):
            if tname in cfg.skip_tables:
                continue
            tbl_meta = source_meta.tables.get(tname)
            pk_cols = _resolve_pk_columns(cfg, tname, tbl_meta)
            if not pk_cols:
                continue
            columns = [c.name for c in tbl_meta.columns] if tbl_meta else []
            delta = detector.detect_delta(
                source_db, target_db, tname, pk_cols, columns, cfg.comparison.strategy,
            )
            ins_color = "green" if delta.insert_pks else None
            upd_color = "yellow" if delta.update_pks else None
            del_color = "red" if delta.delete_pks else None
            click.echo(
                f"  {tname:<40} "
                f"{click.style(f'{len(delta.insert_pks):>8,}', fg=ins_color)} "
                f"{click.style(f'{len(delta.update_pks):>8,}', fg=upd_color)} "
                f"{click.style(f'{len(delta.delete_pks):>8,}', fg=del_color)} "
                f"{delta.unchanged_count:>10,}"
            )
    finally:
        source_db.close()
        target_db.close()


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.pass_context
def plan(ctx: click.Context, profile: str) -> None:
    """Generate and display a migration plan."""
    from dbmigrate.orchestration import PipelineOrchestrator

    cfg = load_profile(profile, _profiles_dir())
    orchestrator = PipelineOrchestrator(cfg, _profiles_dir())
    result = orchestrator.run(dry_run=True)

    click.echo(result.to_summary())
    if not result.success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# run (full pipeline)
# ---------------------------------------------------------------------------


@cli.command("run")
@click.option("--profile", required=True, help="Profile name.")
@click.option("--dry-run/--no-dry-run", default=True, help="Dry run (default: True).")
@click.option("--confirm", is_flag=True, help="Confirm migration execution.")
@click.option("--confirm-deletes", is_flag=True, help="Confirm DELETE operations.")
@click.option("--resume", is_flag=True, help="Resume interrupted migration.")
@click.pass_context
def run(
    ctx: click.Context,
    profile: str,
    dry_run: bool,
    confirm: bool,
    confirm_deletes: bool,
    resume: bool,
) -> None:
    """Execute the full migration pipeline."""
    from dbmigrate.orchestration import PipelineOrchestrator

    cfg = load_profile(profile, _profiles_dir())
    orchestrator = PipelineOrchestrator(cfg, _profiles_dir())
    result = orchestrator.run(
        dry_run=dry_run,
        confirm=confirm,
        confirm_deletes=confirm_deletes,
        resume=resume,
    )

    click.echo(result.to_summary())

    # Write JSON output alongside human output
    click.echo(f"\nJSON report: logs/{result.migration_id}-report.json")

    if not result.success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# migrate (execute only)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.option("--confirm", is_flag=True, required=True, help="Required to execute migration.")
@click.option("--confirm-deletes", is_flag=True, help="Confirm DELETE operations.")
@click.pass_context
def migrate(ctx: click.Context, profile: str, confirm: bool, confirm_deletes: bool) -> None:
    """Execute migration (requires --confirm)."""
    from dbmigrate.orchestration import PipelineOrchestrator

    cfg = load_profile(profile, _profiles_dir())
    orchestrator = PipelineOrchestrator(cfg, _profiles_dir())
    result = orchestrator.run(dry_run=False, confirm=confirm, confirm_deletes=confirm_deletes)

    click.echo(result.to_summary())
    if not result.success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.pass_context
def validate(ctx: click.Context, profile: str) -> None:
    """Run pre-migration validation."""
    from dbmigrate.orchestration import PipelineOrchestrator

    cfg = load_profile(profile, _profiles_dir())
    orchestrator = PipelineOrchestrator(cfg, _profiles_dir())
    result = orchestrator.run(dry_run=True)

    # Extract validation results
    for stage in result.stages:
        if stage.stage_name in ("VALIDATE_PRE", "VALIDATE_POST"):
            status_color = "green" if stage.status == "success" else "red"
            click.secho(f"{stage.stage_name}: {stage.status}", fg=status_color, bold=True)
            if stage.errors:
                for err in stage.errors:
                    click.echo(f"  - {err}")

    if not result.success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.pass_context
def status(ctx: click.Context, profile: str) -> None:
    """Show checkpoint status for a profile."""
    logs_dir = Path("logs")
    if not logs_dir.exists():
        click.echo("No migration logs found.")
        return

    reports = sorted(logs_dir.glob(f"mig-*-report.json"), reverse=True)
    if not reports:
        click.echo("No migration reports found.")
        return

    click.secho(f"Recent migrations (profile: {profile}):", fg="cyan", bold=True)
    for report_path in reports[:10]:
        try:
            import json
            data = json.loads(report_path.read_text())
            if data.get("profile_name") == profile:
                status_icon = "\u2705" if data.get("success") else "\u274c"
                click.echo(
                    f"  {status_icon} {data['migration_id']} — "
                    f"{data.get('total_duration', 0):.1f}s, "
                    f"{data.get('total_rows', 0):,} rows"
                )
        except Exception:
            continue


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--profile", required=True, help="Profile name.")
@click.pass_context
def resume(ctx: click.Context, profile: str) -> None:
    """Resume an interrupted migration."""
    from dbmigrate.orchestration import PipelineOrchestrator

    cfg = load_profile(profile, _profiles_dir())
    orchestrator = PipelineOrchestrator(cfg, _profiles_dir())
    result = orchestrator.run(dry_run=False, confirm=True, resume=True)

    click.echo(result.to_summary())
    if not result.success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# profiles subgroup
# ---------------------------------------------------------------------------


@cli.group()
def profiles() -> None:
    """Manage migration profiles."""
    pass


@profiles.command("list")
def profiles_list() -> None:
    """List available profiles."""
    names = list_profiles(_profiles_dir())
    if not names:
        click.echo("No profiles found.")
        return

    click.secho("Available profiles:", fg="cyan", bold=True)
    for name in names:
        click.echo(f"  - {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
