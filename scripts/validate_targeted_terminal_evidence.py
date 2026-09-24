#!/usr/bin/env python3
"""Opt-in bounded validation for the targeted terminal-evidence families.

Without ``--run`` this command only prints the representatives and performs no
network requests.  The live mode uses one configured source per changed family,
one search term for query-union adapters, and the production <=10 terminal-page
allowance.  It never runs the full scanner and never writes audit artifacts.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


REPRESENTATIVES = {
    "adp": "Diodes Incorporated",
    "amazon": "Amazon",
    "avature": "Bank of America",
    "bamboohr": "Berkshire Grey",
    "google": "Google",
    "ibm_search": "IBM",
    "icims": "Hexagon Autonomy & Positioning / NovAtel",
    "javascript_embedded_data": "Continental",
    "lever": "Veeva Systems",
    "meta": "Meta",
    "paycor": "Calfrac Well Services",
    "peoplesoft": "City of Calgary",
    "smartrecruiters": "Assent",
}


def representative_targets() -> list[Any]:
    configured = {(target.platform, target.name): target for target in load_target_companies()}
    missing = [key for key in REPRESENTATIVES.items() if key not in configured]
    if missing:
        raise ValueError(f"Missing validator representatives: {missing}")
    targets = []
    for platform, company in sorted(REPRESENTATIVES.items()):
        target = configured[(platform, company)]
        if platform in {"amazon", "google", "meta", "ibm_search"} and target.search_terms:
            target = replace(target, search_terms=target.search_terms[:1])
        targets.append(target)
    return targets


def _summary(target: Any, metadata: dict[str, Any], fetched_count: int) -> dict[str, Any]:
    return {
        "company": target.name,
        "platform": target.platform,
        "fetched_count": fetched_count,
        "scan_complete": bool(metadata.get("scan_complete")),
        "scan_completeness_reason": metadata.get("scan_completeness_reason"),
        "stop_reason": metadata.get("stop_reason"),
        "total_available": metadata.get("total_available"),
        "authoritative_total_present": metadata.get("authoritative_total_present"),
        "pages_requested": metadata.get("pages_requested"),
        "terminal_evidence_type": metadata.get("terminal_evidence_type"),
        "terminal_evidence_value": metadata.get("terminal_evidence_value"),
        "has_more": metadata.get("has_more"),
        "next_cursor_present": metadata.get("next_cursor_present"),
        "terminal_page_observed": metadata.get("terminal_page_observed"),
        "complete_collection_response": metadata.get("complete_collection_response"),
        "detail_requests_coupled": metadata.get("detail_requests_coupled"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="perform the bounded live requests; omitted means list-only/offline",
    )
    args = parser.parse_args()
    targets = representative_targets()
    if not args.run:
        print(
            json.dumps(
                [
                    {
                        "company": target.name,
                        "platform": target.platform,
                        "max_pages": target.max_pages,
                        "max_jobs": target.max_jobs,
                    }
                    for target in targets
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    results = []
    for target in targets:
        session = requests.Session()
        try:
            jobs = ADAPTERS[target.platform](session, target, 20)
            results.append(_summary(target, source_scan_metadata(session), len(jobs)))
        except Exception as exc:  # manual diagnostic keeps every family visible
            metadata = source_scan_metadata(session)
            item = _summary(target, metadata, 0)
            item["error"] = f"{type(exc).__name__}: {exc}"[:2000]
            results.append(item)
        finally:
            session.close()
    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    return 0 if all("error" not in item for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
