#!/usr/bin/env python3
"""Bounded manual validation for listing identity and mutable pagination.

This command is intentionally excluded from automated tests. It reads only the
five named production targets, caps collection at two pages and twenty jobs,
writes nothing, and prints compact JSON diagnostics.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.scraper.company_careers.adapters.browser_rendered_html import BrowserBatch
from src.scraper.company_careers.models import CompanyTarget, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


REPRESENTATIVES = (
    "Goldman Sachs",
    "Sanctuary AI",
    "Imperial Oil",
    "Deliveroo",
    "Morgan Stanley",
)
MAX_PAGES = 2
MAX_JOBS = 20
TIMEOUT_SECONDS = 20


def _bounded(target: CompanyTarget) -> CompanyTarget:
    return replace(
        target,
        max_pages=min(MAX_PAGES, max(1, int(target.max_pages))),
        max_jobs=min(MAX_JOBS, max(1, int(target.max_jobs))),
    )


def _row(
    target: CompanyTarget,
    jobs: list[Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "company": target.name,
        "platform": target.platform,
        "status": "ok",
        "fetched_count": len(jobs),
        "scan_complete": bool(metadata.get("scan_complete")),
        "scan_completeness_reason": metadata.get("scan_completeness_reason"),
        "pages_requested": metadata.get("pages_requested"),
        "pages_completed": metadata.get("pages_completed"),
        "stop_reason": metadata.get("stop_reason"),
        "malformed_records": metadata.get("malformed_records", 0),
        "equivalent_duplicate_records": metadata.get(
            "equivalent_duplicate_records", 0
        ),
        "diagnostics": list(metadata.get("diagnostics") or [])[:10],
    }


def _failure_row(target: CompanyTarget, exc: Exception, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": target.name,
        "platform": target.platform,
        "status": "failed",
        "fetched_count": 0,
        "scan_complete": False,
        "scan_completeness_reason": metadata.get("scan_completeness_reason"),
        "stop_reason": metadata.get("stop_reason"),
        "diagnostics": list(metadata.get("diagnostics") or [])[:10],
        "error": f"{type(exc).__name__}: {exc}"[:1000],
    }


def main() -> int:
    configured = {target.name: target for target in load_target_companies()}
    missing = sorted(set(REPRESENTATIVES) - configured.keys())
    if missing:
        raise ValueError(f"Missing listing-identity representatives: {missing}")

    targets = [_bounded(configured[name]) for name in REPRESENTATIVES]
    rows: list[dict[str, Any]] = []
    browser_targets = [
        target for target in targets if target.platform == "browser_rendered_html"
    ]
    non_browser_targets = [
        target for target in targets if target.platform != "browser_rendered_html"
    ]

    for target in non_browser_targets:
        session = requests.Session()
        try:
            jobs = ADAPTERS[target.platform](session, target, TIMEOUT_SECONDS)
            rows.append(_row(target, jobs, source_scan_metadata(session)))
        except Exception as exc:  # manual command reports every representative
            rows.append(_failure_row(target, exc, source_scan_metadata(session)))
        finally:
            session.close()

    if browser_targets:
        try:
            with BrowserBatch(timeout=TIMEOUT_SECONDS) as batch:
                for target in browser_targets:
                    batch.last_run = {}
                    try:
                        jobs = batch.fetch_jobs(target, TIMEOUT_SECONDS)
                        metadata = dict(batch.last_run.get("static_parser") or {})
                        rows.append(_row(target, jobs, metadata))
                    except Exception as exc:  # report remaining browser targets
                        metadata = dict(batch.last_run.get("static_parser") or {})
                        rows.append(_failure_row(target, exc, metadata))
        except Exception as exc:
            for target in browser_targets:
                rows.append(_failure_row(target, exc, {}))

    order = {name: index for index, name in enumerate(REPRESENTATIVES)}
    rows.sort(key=lambda row: order[str(row["company"])])
    print(json.dumps(rows, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if all(row["status"] == "ok" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
