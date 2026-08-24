"""Performance metrics collection and reporting.

Tracks per-table operation throughput, timing, and row counts to
surface bottlenecks and produce run-level summaries.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _OperationRecord:
    """Single tracked operation."""

    table: str
    operation: str
    rows: int
    duration_seconds: float
    timestamp: float


class PerformanceTracker:
    """Collects and reports performance metrics for a migration run.

    Usage::

        tracker = PerformanceTracker()
        tracker.track_operation("users", "insert", rows=500, duration=1.23)
        tracker.track_operation("users", "insert", rows=500, duration=1.10)
        print(tracker.get_throughput("users"))   # rows/sec
        print(tracker.get_summary())
    """

    def __init__(self) -> None:
        self._records: list[_OperationRecord] = []
        self._start_time: float = time.monotonic()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def track_operation(
        self,
        table: str,
        operation: str,
        rows: int,
        duration: float,
    ) -> None:
        """Record a completed operation.

        Parameters
        ----------
        table:
            Table name the operation targeted.
        operation:
            Operation type (e.g. ``"insert"``, ``"update"``, ``"delete"``).
        rows:
            Number of rows processed in this operation.
        duration:
            Wall-clock seconds the operation took.
        """
        self._records.append(
            _OperationRecord(
                table=table,
                operation=operation,
                rows=rows,
                duration_seconds=duration,
                timestamp=time.monotonic(),
            )
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_throughput(self, table: str) -> float:
        """Return aggregate rows-per-second for *table*.

        Parameters
        ----------
        table:
            Table name to compute throughput for.

        Returns
        -------
        float
            Rows per second. Returns ``0.0`` if no operations were
            recorded or total duration is zero.
        """
        table_records = [r for r in self._records if r.table == table]
        if not table_records:
            return 0.0
        total_rows = sum(r.rows for r in table_records)
        total_duration = sum(r.duration_seconds for r in table_records)
        if total_duration <= 0:
            return 0.0
        return total_rows / total_duration

    def get_summary(self) -> dict[str, Any]:
        """Produce an aggregate summary of all tracked operations.

        Returns
        -------
        dict
            Keys: ``total_rows``, ``total_duration_seconds``,
            ``overall_throughput``, ``tables`` (per-table breakdown),
            ``slowest_tables`` (top-5 by duration).
        """
        if not self._records:
            return {
                "total_rows": 0,
                "total_duration_seconds": 0.0,
                "overall_throughput": 0.0,
                "tables": {},
                "slowest_tables": [],
            }

        total_rows = sum(r.rows for r in self._records)
        total_duration = sum(r.duration_seconds for r in self._records)
        overall_throughput = total_rows / total_duration if total_duration > 0 else 0.0

        # Per-table aggregation
        table_stats: dict[str, dict[str, Any]] = {}
        for rec in self._records:
            if rec.table not in table_stats:
                table_stats[rec.table] = {
                    "rows": 0,
                    "duration_seconds": 0.0,
                    "operations": 0,
                    "by_operation": {},
                }
            ts = table_stats[rec.table]
            ts["rows"] += rec.rows
            ts["duration_seconds"] += rec.duration_seconds
            ts["operations"] += 1

            op_key = rec.operation
            if op_key not in ts["by_operation"]:
                ts["by_operation"][op_key] = {"rows": 0, "duration_seconds": 0.0}
            ts["by_operation"][op_key]["rows"] += rec.rows
            ts["by_operation"][op_key]["duration_seconds"] += rec.duration_seconds

        # Compute throughput per table
        for name, ts in table_stats.items():
            dur = ts["duration_seconds"]
            ts["throughput"] = ts["rows"] / dur if dur > 0 else 0.0

        # Slowest tables by total duration
        sorted_tables = sorted(
            table_stats.items(),
            key=lambda kv: kv[1]["duration_seconds"],
            reverse=True,
        )
        slowest = [
            {"table": name, "duration_seconds": round(stats["duration_seconds"], 3), "rows": stats["rows"]}
            for name, stats in sorted_tables[:5]
        ]

        return {
            "total_rows": total_rows,
            "total_duration_seconds": round(total_duration, 3),
            "overall_throughput": round(overall_throughput, 1),
            "tables": table_stats,
            "slowest_tables": slowest,
        }

    def to_json(self) -> str:
        """Serialise the summary to a JSON string.

        Returns
        -------
        str
            Pretty-printed JSON.
        """
        return json.dumps(self.get_summary(), indent=2, default=str)
