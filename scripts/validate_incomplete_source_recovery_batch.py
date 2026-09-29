#!/usr/bin/env python3
"""Read-only, bounded live validation for the 20-source recovery batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.scraper.company_careers.adapters import browser_rendered_html
from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


GROUPS = {
    "mutable": (
        "Atlassian",
        "Bloomberg",
        "GlobalFoundries",
        "HP Inc.",
        "Infineon Technologies",
        "Micron",
        "Schneider Electric",
        "Bombardier",
        "CPKC",
        "Kinross Gold",
        "Seagate",
    ),
    "browser": ("ASML", "Aurora Innovation", "ByteDance", "Deliveroo"),
    "static": ("BlackRock", "SharkNinja", "Siemens", "Caterpillar", "Coupang"),
}


class CountingSession(requests.Session):
    def __init__(self, request_cap: int | None = None) -> None:
        super().__init__()
        self.request_count = 0
        self.request_methods: dict[str, int] = {}
        self.request_cap = request_cap

    def request(self, method: str, url: str, *args: Any, **kwargs: Any):
        if self.request_cap is not None and self.request_count >= self.request_cap:
            raise RuntimeError(
                f"targeted live validation request cap reached ({self.request_cap})"
            )
        self.request_count += 1
        normalized = method.upper()
        self.request_methods[normalized] = self.request_methods.get(normalized, 0) + 1
        return super().request(method, url, *args, **kwargs)


def _row(name: str, platform: str, jobs: int, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": name,
        "platform": platform,
        "status": "ok",
        "jobs": jobs,
        "scan_complete": bool(metadata.get("scan_complete")),
        "scan_completeness_reason": metadata.get("scan_completeness_reason"),
        "stop_reason": metadata.get("stop_reason"),
        "pages_requested": metadata.get("pages_requested"),
        "pages_completed": metadata.get("pages_completed"),
        "collection_restart_count": metadata.get("collection_restart_count", 0),
        "collection_retry_exhausted": bool(metadata.get("collection_retry_exhausted")),
        "stabilized_after_collection_retry": bool(
            metadata.get("stabilized_after_collection_retry")
        ),
        "total_pages_requested_across_traversals": metadata.get(
            "total_pages_requested_across_traversals"
        ),
        "initial_total_available": metadata.get("initial_total_available"),
        "final_total_available": metadata.get("final_total_available"),
        "minimum_total_observed": metadata.get("minimum_total_observed"),
        "maximum_total_observed": metadata.get("maximum_total_observed"),
        "overlapping_jobs_skipped": metadata.get("overlapping_jobs_skipped", 0),
        "terminal_evidence_type": metadata.get("terminal_evidence_type"),
        "terminal_evidence_value": metadata.get("terminal_evidence_value"),
        "terminal_page_fully_processed": metadata.get(
            "terminal_page_fully_processed"
        ),
        "malformed_records": metadata.get("malformed_records", 0),
        "rejected_record_count": metadata.get("rejected_record_count", 0),
        "repeated_page": bool(metadata.get("repeated_page")),
        "repeated_stable_ids": bool(metadata.get("repeated_stable_ids")),
    }


def _validate_http(
    names: tuple[str, ...],
    configured: dict[str, Any],
    request_cap: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in names:
        target = configured[name]
        session = CountingSession(request_cap=request_cap)
        try:
            jobs = ADAPTERS[target.platform](session, target, 30)
            metadata = source_scan_metadata(session)
            row = _row(name, target.platform, len(jobs), metadata)
        except Exception as exc:
            metadata = source_scan_metadata(session)
            row = _row(name, target.platform, 0, metadata)
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        finally:
            session.close()
        row["request_count"] = session.request_count
        row["request_methods"] = session.request_methods
        rows.append(row)
    return rows


def _validate_browser(names: tuple[str, ...], configured: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        batch = browser_rendered_html.BrowserBatch(timeout=30)
        batch.__enter__()
    except Exception as exc:
        return [
            {
                "company": name,
                "platform": configured[name].platform,
                "status": "failed",
                "jobs": 0,
                "scan_complete": False,
                "listing_request_count": 0,
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
            for name in names
        ]
    try:
        for name in names:
            target = configured[name]
            try:
                jobs = batch.fetch_jobs(target, 30)
                metadata = dict(batch.last_run.get("static_parser") or {})
                row = _row(name, target.platform, len(jobs), metadata)
                row["listing_request_count"] = batch.last_run.get("pages_rendered", 0)
                row["rendered_bytes"] = batch.last_run.get("rendered_bytes", 0)
            except Exception as exc:
                row = {
                    "company": name,
                    "platform": target.platform,
                    "status": "failed",
                    "jobs": 0,
                    "scan_complete": False,
                    "listing_request_count": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            rows.append(row)
    finally:
        batch.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("group", choices=(*GROUPS, "all"))
    parser.add_argument(
        "--company",
        action="append",
        default=[],
        help="Validate only this named company within the selected group (repeatable).",
    )
    parser.add_argument(
        "--request-cap",
        type=int,
        default=None,
        help="Stop each HTTP source before issuing more than this many requests.",
    )
    args = parser.parse_args()
    configured = {
        target.name: target
        for target in load_target_companies(apply_reliability_repairs=True)
    }
    selected = GROUPS if args.group == "all" else {args.group: GROUPS[args.group]}
    requested = set(args.company)
    known = {name for names in selected.values() for name in names}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError(f"Requested companies are not in the selected group: {unknown}")
    rows: list[dict[str, Any]] = []
    for group, names in selected.items():
        if requested:
            names = tuple(name for name in names if name in requested)
        missing = sorted(set(names) - configured.keys())
        if missing:
            raise ValueError(f"Missing recovery targets: {missing}")
        if group == "browser":
            browser_names = tuple(
                name for name in names
                if configured[name].platform == "browser_rendered_html"
            )
            http_names = tuple(name for name in names if name not in browser_names)
            if browser_names:
                rows.extend(_validate_browser(browser_names, configured))
            if http_names:
                rows.extend(_validate_http(http_names, configured, args.request_cap))
        else:
            rows.extend(_validate_http(names, configured, args.request_cap))
    print(json.dumps(rows, indent=2, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
