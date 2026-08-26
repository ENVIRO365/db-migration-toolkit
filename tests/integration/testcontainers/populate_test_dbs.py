#!/usr/bin/env python3
"""Orchestrator: start containers → fetch from MCP → populate both DBs → verify.

Usage:
  # Full run (Db2 + PostgreSQL containers, live MCP data):
  python -m tests.integration.testcontainers.populate_test_dbs

  # PostgreSQL only (skip Db2 container, which is slow):
  python -m tests.integration.testcontainers.populate_test_dbs --pg-only

  # Use embedded seed data (no MCP / Db2 source connection needed):
  python -m tests.integration.testcontainers.populate_test_dbs --embedded

  # Use docker-compose containers instead of testcontainers:
  docker compose -f docker-compose.testcontainers.yml up -d --wait
  python -m tests.integration.testcontainers.populate_test_dbs --use-compose --pg-only

  # Use docker-compose containers (Db2 + PG):
  python -m tests.integration.testcontainers.populate_test_dbs --use-compose

  # Custom Db2 source DSN:
  DB2_WEALTH_TST_DSN="DATABASE=...;HOSTNAME=...;..." \\
    python -m tests.integration.testcontainers.populate_test_dbs

Exit codes:
  0  All steps succeeded and verification passed
  1  Fatal error (container startup, connection, or verification mismatch)
"""

from __future__ import annotations

import argparse
import atexit
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table as RichTable

from tests.integration.testcontainers.containers import (
    Db2Container,
    Db2ContainerConfig,
    ExternalDb2Container,
    ExternalPgContainer,
    PgContainer,
    PgContainerConfig,
    cleanup_containers,
)
from tests.integration.testcontainers.mcp_fetcher import (
    FetchResult,
    fetch_all_tables,
    validate_fetch_results,
)
from tests.integration.testcontainers.schema_manager import (
    TABLE_INDEX,
    TABLES,
    VerificationResult,
    create_schema_db2,
    create_schema_postgres,
    insert_rows_db2,
    insert_rows_postgres,
    verify_all,
    verify_row_counts_db2,
    verify_row_counts_postgres,
)

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class RunReport:
    """Summary of the entire populate run."""

    started_at: datetime
    finished_at: datetime | None = None
    fetch_results: dict[str, FetchResult] | None = None
    pg_tables_created: list[str] | None = None
    db2_tables_created: list[str] | None = None
    pg_rows_inserted: dict[str, int] | None = None
    db2_rows_inserted: dict[str, int] | None = None
    verification: list[VerificationResult] | None = None
    errors: list[str] | None = None

    @property
    def success(self) -> bool:
        if self.errors:
            return False
        if self.verification:
            return all(v.pg_match for v in self.verification)
        return False


def _print_report(report: RunReport, *, include_db2: bool = True) -> None:
    """Print a rich summary table."""
    console.print()
    console.rule("[bold cyan]Testcontainers Population Report")
    console.print(f"  Started:  {report.started_at.isoformat()}")
    if report.finished_at:
        elapsed = (report.finished_at - report.started_at).total_seconds()
        console.print(f"  Finished: {report.finished_at.isoformat()}")
        console.print(f"  Elapsed:  {elapsed:.1f}s")
    console.print()

    # ── Fetch summary ──
    if report.fetch_results:
        t = RichTable(title="Data Fetch (MCP / Db2 Source)", show_lines=True)
        t.add_column("Table", style="cyan")
        t.add_column("Rows", justify="right")
        t.add_column("Status")
        for name, r in report.fetch_results.items():
            status = "[green]OK" if r.ok else f"[red]ERROR: {r.error}"
            t.add_row(name, str(r.row_count), status)
        console.print(t)

    # ── Insert summary ──
    if report.pg_rows_inserted or report.db2_rows_inserted:
        t = RichTable(title="Rows Inserted", show_lines=True)
        t.add_column("Table", style="cyan")
        t.add_column("PostgreSQL", justify="right")
        if include_db2:
            t.add_column("Db2", justify="right")
        for table in TABLES:
            pg_count = (report.pg_rows_inserted or {}).get(table.name, "-")
            row_data = [table.name, str(pg_count)]
            if include_db2:
                db2_count = (report.db2_rows_inserted or {}).get(table.name, "-")
                row_data.append(str(db2_count))
            t.add_row(*row_data)
        console.print(t)

    # ── Verification ──
    if report.verification:
        t = RichTable(title="Verification", show_lines=True)
        t.add_column("Table", style="cyan")
        t.add_column("Source", justify="right")
        t.add_column("PG", justify="right")
        t.add_column("PG Match")
        if include_db2:
            t.add_column("Db2", justify="right")
            t.add_column("Db2 Match")
        for v in report.verification:
            pg_match = "[green]YES" if v.pg_match else "[red]NO"
            row_data = [v.table_name, str(v.source_count), str(v.pg_count), pg_match]
            if include_db2:
                db2_match = "[green]YES" if v.db2_match else "[red]NO"
                row_data.extend([str(v.db2_count), db2_match])
            t.add_row(*row_data)
        console.print(t)

    # ── Errors ──
    if report.errors:
        console.print()
        console.print("[bold red]Errors:")
        for err in report.errors:
            console.print(f"  [red]• {err}")

    # ── Final status ──
    console.print()
    if report.success:
        console.print("[bold green]✓ All checks passed.")
    else:
        console.print("[bold red]✗ Some checks failed — see errors above.")
    console.rule()


def run(
    *,
    pg_only: bool = False,
    use_embedded: bool = False,
    use_compose: bool = False,
    source: str = "auto",
    db2_dsn: str | None = None,
) -> RunReport:
    """Execute the full populate pipeline.

    Parameters
    ----------
    pg_only:
        Skip Db2 container (PostgreSQL only).
    use_embedded:
        Use embedded seed data instead of fetching from MCP.
    use_compose:
        Connect to existing docker-compose containers instead of
        spinning up ephemeral testcontainers.
    source:
        Data source: "auto", "db2", "pg", or "embedded".
    db2_dsn:
        Explicit DSN for the source Db2 data fetch.
    """
    report = RunReport(started_at=datetime.now(tz=timezone.utc), errors=[])

    db2_container: Db2Container | ExternalDb2Container | None = None
    pg_container: PgContainer | ExternalPgContainer | None = None

    # Only register cleanup for testcontainers (compose containers are external)
    if not use_compose:
        atexit.register(cleanup_containers)

    try:
        # ── Step 1: Start containers ─────────────────────────────────────
        mode_label = "docker-compose" if use_compose else "testcontainers"
        console.print(f"[bold]Step 1/5: Starting containers ({mode_label}) …")

        if use_compose:
            pg_container = ExternalPgContainer()
            pg_container.start()
        else:
            pg_container = PgContainer()
            pg_container.start()
        console.print(f"  PostgreSQL ready at {pg_container.host}:{pg_container.port}")

        if not pg_only:
            try:
                if use_compose:
                    db2_container = ExternalDb2Container()
                    db2_container.start()
                else:
                    db2_container = Db2Container()
                    db2_container.start()
                console.print(
                    f"  Db2 ready at {db2_container.host}:{db2_container.port}"
                )
            except Exception as exc:
                msg = f"Db2 container failed to start: {exc}"
                logger.error(msg)
                report.errors.append(msg)
                console.print(f"  [yellow]Db2 skipped: {exc}")
                db2_container = None

        # ── Step 2: Fetch data ───────────────────────────────────────────
        console.print("[bold]Step 2/5: Fetching data from source …")

        fetch_results = fetch_all_tables(
            use_embedded=use_embedded,
            source=source,
            db2_dsn=db2_dsn,
        )
        report.fetch_results = fetch_results

        if not validate_fetch_results(fetch_results):
            console.print("[yellow]  Warning: some tables had fetch errors.")

        total_rows = sum(r.row_count for r in fetch_results.values())
        console.print(f"  Fetched {total_rows} total rows across {len(fetch_results)} tables.")

        # ── Step 3: Create schemas ───────────────────────────────────────
        console.print("[bold]Step 3/5: Creating schemas …")

        import psycopg2

        pg_conn = psycopg2.connect(pg_container.get_dsn())
        report.pg_tables_created = create_schema_postgres(pg_conn)
        console.print(
            f"  PostgreSQL: {len(report.pg_tables_created)} tables created."
        )

        db2_conn = None
        if db2_container is not None:
            import ibm_db

            # Db2 may need a few extra seconds after internal readiness
            # before accepting external TCP connections.
            db2_dsn_str = db2_container.get_dsn()
            max_retries = 12  # 12 * 5s = 60s extra patience
            for attempt in range(1, max_retries + 1):
                try:
                    db2_conn = ibm_db.connect(db2_dsn_str, "", "")
                    break
                except Exception as exc:
                    if attempt == max_retries:
                        raise RuntimeError(
                            f"Could not connect to Db2 after {max_retries} retries: {exc}"
                        ) from exc
                    logger.info(
                        "  Db2 connect attempt %d/%d failed (%s); retrying in 5s …",
                        attempt,
                        max_retries,
                        exc,
                    )
                    time.sleep(5)

            report.db2_tables_created = create_schema_db2(db2_conn)
            console.print(
                f"  Db2: {len(report.db2_tables_created)} tables created."
            )

        # ── Step 4: Insert data ──────────────────────────────────────────
        console.print("[bold]Step 4/5: Inserting data …")

        report.pg_rows_inserted = {}
        report.db2_rows_inserted = {}

        for table_def in TABLES:
            fetch = fetch_results.get(table_def.name)
            if not fetch or not fetch.ok or fetch.row_count == 0:
                logger.info("Skipping %s (no data).", table_def.name)
                continue

            # PostgreSQL insert
            try:
                count = insert_rows_postgres(pg_conn, table_def, fetch.rows)
                report.pg_rows_inserted[table_def.name] = count
                console.print(f"  PG  {table_def.name}: {count} rows")
            except Exception as exc:
                msg = f"PG insert {table_def.name} failed: {exc}"
                logger.error(msg)
                report.errors.append(msg)
                report.pg_rows_inserted[table_def.name] = 0

            # Db2 insert
            if db2_conn is not None:
                try:
                    count = insert_rows_db2(db2_conn, table_def, fetch.rows)
                    report.db2_rows_inserted[table_def.name] = count
                    console.print(f"  Db2 {table_def.name}: {count} rows")
                except Exception as exc:
                    msg = f"Db2 insert {table_def.name} failed: {exc}"
                    logger.error(msg)
                    report.errors.append(msg)
                    report.db2_rows_inserted[table_def.name] = 0

        # ── Step 5: Verify ───────────────────────────────────────────────
        console.print("[bold]Step 5/5: Verifying row counts …")

        source_counts = {name: r.row_count for name, r in fetch_results.items()}
        pg_counts = verify_row_counts_postgres(pg_conn)

        db2_counts: dict[str, int] = {}
        if db2_conn is not None:
            db2_counts = verify_row_counts_db2(db2_conn)

        report.verification = verify_all(source_counts, db2_counts, pg_counts)

        mismatches = [
            v
            for v in report.verification
            if not v.pg_match or (db2_container and not v.db2_match)
        ]
        if mismatches:
            for m in mismatches:
                report.errors.append(
                    f"Row count mismatch for {m.table_name}: "
                    f"source={m.source_count} pg={m.pg_count} db2={m.db2_count}"
                )

        # ── Cleanup connections ──────────────────────────────────────────
        pg_conn.close()
        if db2_conn is not None:
            import ibm_db

            ibm_db.close(db2_conn)

    except Exception as exc:
        msg = f"Fatal error: {exc}"
        logger.exception(msg)
        report.errors.append(msg)

    finally:
        report.finished_at = datetime.now(tz=timezone.utc)

    return report


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Populate Testcontainers with WEALTHADAPTER data."
    )
    parser.add_argument(
        "--pg-only",
        action="store_true",
        help="Skip Db2 container (PostgreSQL only).",
    )
    parser.add_argument(
        "--embedded",
        action="store_true",
        help="Use embedded seed data instead of MCP fetch.",
    )
    parser.add_argument(
        "--use-compose",
        action="store_true",
        help=(
            "Connect to existing docker-compose containers "
            "(from docker-compose.testcontainers.yml) instead of "
            "spinning up ephemeral testcontainers."
        ),
    )
    parser.add_argument(
        "--db2-dsn",
        default=None,
        help="Explicit ibm_db DSN for the source Db2.",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "db2", "pg", "embedded"],
        default="auto",
        help=(
            "Data source selection: "
            "'auto' (default) tries DB2_WEALTH_TST_DSN → WA_TARGET_DSN → WA_SOURCE_DSN → embedded; "
            "'db2' forces Db2 source only; "
            "'pg' forces PostgreSQL source (WA_SOURCE_DSN) only; "
            "'embedded' uses built-in seed data."
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    report = run(
        pg_only=args.pg_only,
        use_embedded=args.embedded,
        use_compose=args.use_compose,
        source=args.source,
        db2_dsn=args.db2_dsn,
    )

    _print_report(report, include_db2=not args.pg_only)

    # Cleanup containers (no-op for compose mode)
    if not args.use_compose:
        cleanup_containers()

    sys.exit(0 if report.success else 1)


if __name__ == "__main__":
    main()
