#!/usr/bin/env python3
"""Dry-run or explicitly execute bounded Supabase retention maintenance."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.integrations.supabase_job_store import (
    DEFAULT_RETENTION_BATCH_SIZE,
    SupabaseJobStore,
)
from src.scraper.company_careers.freshness import (
    DEFAULT_FRESHNESS_CONFIG_PATH,
    load_freshness_config,
    parse_reference_time,
)


EXECUTION_CONFIRMATION = "DELETE_RETENTION_CANDIDATES"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or execute bounded Ping retention maintenance.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--freshness-config",
        default=DEFAULT_FRESHNESS_CONFIG_PATH,
        help="YAML containing the central retention policy",
    )
    parser.add_argument(
        "--as-of",
        type=parse_reference_time,
        help="Deterministic ISO-8601 retention reference time",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_RETENTION_BATCH_SIZE,
        help="Maximum rows deleted from each table per transaction",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=500,
        help="Safety limit for explicit execution",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Opt in to real bounded deletion; default is dry-run",
    )
    parser.add_argument(
        "--confirm",
        help=f"Required with --execute: {EXECUTION_CONFIRMATION}",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 5000:
        parser.error("--batch-size must be between 1 and 5000")
    if args.max_batches < 1:
        parser.error("--max-batches must be at least 1")
    if args.execute and args.confirm != EXECUTION_CONFIRMATION:
        parser.error(
            f"--execute requires --confirm {EXECUTION_CONFIRMATION}"
        )
    if not args.execute and args.confirm:
        parser.error("--confirm is valid only with --execute")
    return args


def run(args: argparse.Namespace) -> int:
    config = load_freshness_config(args.freshness_config)
    as_of = args.as_of or datetime.now(timezone.utc)
    store = SupabaseJobStore.from_env(
        now_factory=lambda: as_of,
        retention_policy=config.retention,
    )
    store.validate_setup()

    initial = store.maintain_retention(
        execute=False,
        as_of=as_of,
        batch_size=args.batch_size,
    )
    if not args.execute:
        print(json.dumps(initial, indent=2, sort_keys=True))
        return 0

    deleted_totals = {
        "tracked_jobs": 0,
        "tracked_job_source_scope_rows": 0,
        "job_sync_sessions": 0,
        "job_sync_chunks": 0,
        "job_sync_stage": 0,
        "scan_source_syncs": 0,
    }
    batches = 0
    while batches < args.max_batches:
        try:
            latest = store.maintain_retention(
                execute=True,
                as_of=as_of,
                batch_size=args.batch_size,
            )
        except Exception:
            checkpoint = {
                "dry_run": False,
                "as_of": as_of.isoformat(),
                "batches_completed": batches,
                "deleted": deleted_totals,
                "complete": False,
                "resumable": True,
            }
            print(json.dumps(checkpoint, indent=2, sort_keys=True), file=sys.stderr)
            raise
        batches += 1
        deleted = latest["deleted"]
        for field in deleted_totals:
            deleted_totals[field] += int(deleted.get(field, 0) or 0)
        if not any(
            int(deleted.get(field, 0) or 0)
            for field in (
                "tracked_jobs",
                "job_sync_sessions",
                "job_sync_chunks",
                "job_sync_stage",
                "scan_source_syncs",
            )
        ):
            break

    final = store.maintain_retention(
        execute=False,
        as_of=as_of,
        batch_size=args.batch_size,
    )
    remaining = sum(
        int(final["before"].get(field, 0) or 0)
        for field in (
            "total_jobs_deletable",
            "old_sync_sessions",
            "old_scan_source_syncs",
        )
    )

    result = {
        "dry_run": False,
        "as_of": as_of.isoformat(),
        "batches": batches,
        "before": initial["before"],
        "deleted": deleted_totals,
        "after": final["before"],
        "complete": remaining == 0,
        "resumable": True,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["complete"]:
        raise RuntimeError(
            "retention maintenance reached --max-batches before cleanup completed"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(
            f"[ERROR] Retention maintenance failed: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
