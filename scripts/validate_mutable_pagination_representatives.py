#!/usr/bin/env python3
"""Bounded manual validation for mutable-pagination representatives.

This command is intentionally excluded from automated tests. It scans only the
named regression representatives, caps paginated adapters at two pages, writes
nothing, and prints compact JSON diagnostics.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


REPRESENTATIVES = (
    "Atlassian",
    "Eaton",
    "Rivian",
    "Bloomberg",
    "Scotiabank",
    "Stellantis",
    "Johnson & Johnson",
    "Siemens Healthineers",
)


def main() -> int:
    configured = {target.name: target for target in load_target_companies()}
    missing = sorted(set(REPRESENTATIVES) - configured.keys())
    if missing:
        raise ValueError(f"Missing mutable-pagination representatives: {missing}")

    rows: list[dict[str, object]] = []
    for name in REPRESENTATIVES:
        configured_target = configured[name]
        page_size = max(1, int(configured_target.page_size or 10))
        target = replace(
            configured_target,
            max_pages=min(2, max(1, int(configured_target.max_pages))),
            max_jobs=min(max(1, int(configured_target.max_jobs)), page_size * 2),
        )
        session = requests.Session()
        try:
            jobs = ADAPTERS[target.platform](session, target, 20)
            metadata = source_scan_metadata(session)
            rows.append(
                {
                    "company": name,
                    "platform": target.platform,
                    "status": "ok",
                    "fetched_count": len(jobs),
                    "scan_complete": bool(metadata.get("scan_complete")),
                    "scan_completeness_reason": metadata.get(
                        "scan_completeness_reason"
                    ),
                    "initial_total_available": metadata.get(
                        "initial_total_available"
                    ),
                    "minimum_total_observed": metadata.get(
                        "minimum_total_observed"
                    ),
                    "maximum_total_observed": metadata.get(
                        "maximum_total_observed"
                    ),
                    "final_total_observed": metadata.get("final_total_observed"),
                    "overlapping_jobs_skipped": metadata.get(
                        "overlapping_jobs_skipped", 0
                    ),
                    "stop_reason": metadata.get("stop_reason"),
                }
            )
        except Exception as exc:  # manual command reports every representative
            metadata = source_scan_metadata(session)
            rows.append(
                {
                    "company": name,
                    "platform": target.platform,
                    "status": "failed",
                    "fetched_count": 0,
                    "scan_complete": False,
                    "scan_completeness_reason": metadata.get(
                        "scan_completeness_reason"
                    ),
                    "stop_reason": metadata.get("stop_reason"),
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            )
        finally:
            session.close()

    print(json.dumps(rows, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if all(row["status"] == "ok" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
