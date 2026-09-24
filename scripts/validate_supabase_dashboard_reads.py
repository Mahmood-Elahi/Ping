#!/usr/bin/env python3
"""READ-ONLY preflight for the Supabase reads used by the student dashboard.

This script performs no search, scan, migration, lifecycle mutation, Supabase
write, or Google Sheets operation. It only validates setup and reads lifecycle,
active-job, and latest-scan data.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.integrations.supabase_job_store import DashboardData, SupabaseJobStore


def _lifecycle_rows(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    jobs = state.get("jobs")
    if isinstance(jobs, Mapping):
        rows = list(jobs.values())
    elif isinstance(jobs, Sequence) and not isinstance(jobs, (str, bytes)):
        rows = list(jobs)
    else:
        raise ValueError("Lifecycle state jobs must be a mapping or sequence")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("Lifecycle state contains a non-object job")
    return rows


def validate_read_results(
    lifecycle_state: Mapping[str, Any],
    dashboard_data: DashboardData,
    *,
    setup_seconds: float,
    lifecycle_seconds: float,
    dashboard_seconds: float,
) -> tuple[dict[str, Any], list[str]]:
    """Return concise diagnostics and deterministic invariant failures."""
    lifecycle_rows = _lifecycle_rows(lifecycle_state)
    active_rows = list(dashboard_data.active_jobs)
    lifecycle_keys = [str(row.get("source_key") or "") for row in lifecycle_rows]
    active_keys = [str(row.get("source_key") or "") for row in active_rows]
    lifecycle_duplicates = len(lifecycle_keys) - len(set(lifecycle_keys))
    active_duplicates = len(active_keys) - len(set(active_keys))
    latest_scan_present = dashboard_data.latest_scan is not None

    diagnostics = {
        "lifecycle_rows": len(lifecycle_rows),
        "lifecycle_unique_keys": len(set(lifecycle_keys)),
        "lifecycle_duplicates": lifecycle_duplicates,
        "lifecycle_seconds": round(float(lifecycle_seconds), 3),
        "active_jobs": len(active_rows),
        "active_unique_keys": len(set(active_keys)),
        "active_duplicates": active_duplicates,
        "dashboard_seconds": round(float(dashboard_seconds), 3),
        "latest_scan_present": latest_scan_present,
        "setup_seconds": round(float(setup_seconds), 3),
    }

    failures: list[str] = []
    if any(not source_key for source_key in lifecycle_keys):
        failures.append("lifecycle source_keys must be nonempty")
    if lifecycle_duplicates:
        failures.append("lifecycle source_keys must be unique")
    if any(not source_key for source_key in active_keys):
        failures.append("active-job source_keys must be nonempty")
    if active_duplicates:
        failures.append("active-job source_keys must be unique")
    if any(str(row.get("status") or "") != "active" for row in active_rows):
        failures.append("every dashboard job must have status == 'active'")
    if (lifecycle_rows or active_rows) and not latest_scan_present:
        failures.append("latest_scan must be present for a populated dashboard")
    return diagnostics, failures


def _timed(call: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


def main() -> int:
    """Run the read-only preflight and return nonzero on any failure."""
    load_dotenv(dotenv_path=REPOSITORY_ROOT / ".env")
    try:
        store = SupabaseJobStore.from_env()
        _, setup_seconds = _timed(store.validate_setup)
        lifecycle_state, lifecycle_seconds = _timed(store.fetch_lifecycle_state)
        dashboard_data, dashboard_seconds = _timed(store.fetch_dashboard_data)
        diagnostics, failures = validate_read_results(
            lifecycle_state,
            dashboard_data,
            setup_seconds=setup_seconds,
            lifecycle_seconds=lifecycle_seconds,
            dashboard_seconds=dashboard_seconds,
        )
        print(json.dumps(diagnostics, sort_keys=True))
        if failures:
            print("Supabase dashboard read preflight failed: " + "; ".join(failures), file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "read_only": True,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
