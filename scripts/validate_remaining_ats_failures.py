#!/usr/bin/env python3
"""One-page, read-only validation for completeness-migrated adapter families.

This intentionally does not run the full scanner or write artifacts.  It is a
manual follow-up command: tests must never invoke ``main``.  Representatives
come from the saved Pareto input, one per family migrated by the offline
projection, so the command remains bounded as the registry evolves.
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

from src.scraper.company_careers.models import AdapterError, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS
from scripts.project_completeness_migration import DEFAULT_COVERAGE, build_projection


def _bounded_target(target: Any) -> Any:
    native_page_size = max(1, int(target.page_size or 20))
    return replace(
        target,
        max_pages=1,
        max_jobs=min(max(1, int(target.max_jobs)), native_page_size),
    )


def representative_targets() -> list[Any]:
    projection = build_projection()
    migrated_platforms = {
        str(item["platform"])
        for item in projection["platforms"]
        if item["safe_to_migrate_in_this_pass"]
    }
    coverage = json.loads(DEFAULT_COVERAGE.read_text(encoding="utf-8"))["coverage"]
    configured = {
        (target.name, target.platform): target for target in load_target_companies()
    }
    representatives: dict[str, Any] = {}
    for row in coverage:
        platform = str(row.get("platform"))
        key = (row.get("company"), platform)
        if (
            platform in migrated_platforms
            and row.get("scan_completeness_reason") == "adapter_completeness_not_proven"
            and row.get("scan_health") != "failed"
            and row.get("status") != "failed_technical"
            and platform not in representatives
            and key in configured
        ):
            representatives[platform] = configured[key]
    missing = migrated_platforms - representatives.keys()
    if missing:
        raise ValueError(f"No validation representative for: {sorted(missing)}")
    return [representatives[platform] for platform in sorted(representatives)]


def _adapter_diagnostics(session: requests.Session, platform: str) -> list[Any]:
    generic = source_scan_metadata(session)
    platform_metadata = getattr(session, f"{platform}_pagination", {})
    values: list[Any] = []
    for metadata in (generic, platform_metadata):
        if not isinstance(metadata, dict):
            continue
        for item in metadata.get("diagnostics", []) or []:
            if item not in values:
                rendered = json.dumps(item, ensure_ascii=True, sort_keys=True, default=str)
                values.append(rendered[:2000])
    return values[:8]


def main() -> int:
    results: list[dict[str, Any]] = []
    for configured_target in representative_targets():
        target = _bounded_target(configured_target)
        company = target.name
        session = requests.Session()
        try:
            jobs = ADAPTERS[target.platform](session, target, 20)
            metadata = source_scan_metadata(session)
            scan_complete = bool(metadata.get("scan_complete"))
            results.append(
                {
                    "company": company,
                    "platform": target.platform,
                    "status": "ok",
                    "fetched_count": len(jobs),
                    "health": "complete" if scan_complete else "incomplete",
                    "scan_complete": scan_complete,
                    "completeness": metadata.get("scan_completeness_reason"),
                    "error": None,
                    "location_samples": [job.location for job in jobs[:3]],
                    "diagnostics": _adapter_diagnostics(session, target.platform),
                }
            )
        except (AdapterError, requests.RequestException, TypeError, ValueError) as exc:
            results.append(
                {
                    "company": company,
                    "platform": target.platform,
                    "status": "failed",
                    "fetched_count": 0,
                    "health": "failed",
                    "scan_complete": False,
                    "completeness": source_scan_metadata(session).get(
                        "scan_completeness_reason"
                    ),
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                    "diagnostics": _adapter_diagnostics(session, target.platform),
                }
            )
        finally:
            session.close()
    print(json.dumps(results, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if all(item["status"] == "ok" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
