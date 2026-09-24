#!/usr/bin/env python3
"""Manual bounded validator for every listing-completion family changed here.

This is deliberately not part of automated tests and must be run manually.
It scans only the named representatives, caps non-browser HTTP traffic per
target, never invokes the full scanner, and prints diagnostics without writing
artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.adapters.browser_rendered_html import BrowserBatch
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


REPRESENTATIVES = (
    "FANUC",                     # ADP MyJobs listing-only path
    "Intuit",                    # avature
    "Porsche",                   # beesite
    "Figure AI",                 # browser-rendered HTML
    "Firefly Aerospace",        # clearcompany
    "OHB SE",                   # cornerstone_csod
    "Dassault Systèmes",         # exalead
    "ATB Financial",            # eightfold
    "Aptiv",                    # hawksearch v2
    "GitHub",                   # icims
    "Stellantis",                # mcloud CWS
    "PETRONAS",                 # oracle_recruiting
    "GE Aerospace",             # phenom
    "Halliburton",              # sap_successfactors
    "Bosch",                     # SmartRecruiters safe page-size path
    "Waymo",                     # static HTML
    "Pason Systems",            # ukg_pro
    "McKesson",                 # workday
)
TIMEOUT_SECONDS = 20
MAX_HTTP_REQUESTS_PER_TARGET = 60


class BoundedSession(requests.Session):
    """Fail closed if a representative unexpectedly exceeds its request cap."""

    def __init__(self) -> None:
        super().__init__()
        self.requests_started = 0

    def request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.requests_started += 1
        if self.requests_started > MAX_HTTP_REQUESTS_PER_TARGET:
            raise AdapterError(
                f"Manual validator exceeded {MAX_HTTP_REQUESTS_PER_TARGET} HTTP requests"
            )
        return super().request(method, url, *args, **kwargs)


def representative_targets():
    configured = {target.name: target for target in load_target_companies()}
    missing = sorted(set(REPRESENTATIVES) - configured.keys())
    if missing:
        raise ValueError(f"Missing adaptive-completion representatives: {missing}")
    return [configured[name] for name in REPRESENTATIVES]


def main() -> int:
    results = []
    targets = representative_targets()
    browser_targets = [
        target for target in targets if target.platform == "browser_rendered_html"
    ]
    non_browser_targets = [
        target for target in targets if target.platform != "browser_rendered_html"
    ]
    for target in non_browser_targets:
        session = BoundedSession()
        try:
            jobs = ADAPTERS[target.platform](session, target, TIMEOUT_SECONDS)
            metadata = source_scan_metadata(session)
            results.append({
                "company": target.name,
                "platform": target.platform,
                "status": "ok",
                "fetched_count": len(jobs),
                "scan_complete": bool(metadata.get("scan_complete")),
                "reason": metadata.get("scan_completeness_reason"),
                "total_available": metadata.get("total_available"),
                "pages_requested": metadata.get("pages_requested"),
                "normal_budget": metadata.get("normal_listing_request_budget"),
                "effective_ceiling": metadata.get("effective_listing_request_ceiling"),
                "stop_reason": metadata.get("stop_reason"),
                "http_requests_started": session.requests_started,
            })
        except Exception as exc:  # manual validator reports every family
            metadata = source_scan_metadata(session)
            results.append({
                "company": target.name,
                "platform": target.platform,
                "status": "failed",
                "scan_complete": False,
                "reason": metadata.get("scan_completeness_reason"),
                "stop_reason": metadata.get("stop_reason"),
                "http_requests_started": session.requests_started,
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            })
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
                        results.append({
                            "company": target.name,
                            "platform": target.platform,
                            "status": "ok",
                            "fetched_count": len(jobs),
                            "scan_complete": bool(metadata.get("scan_complete")),
                            "reason": metadata.get("scan_completeness_reason"),
                            "pages_requested": metadata.get("pages_requested"),
                            "stop_reason": metadata.get("stop_reason"),
                            "pages_rendered": batch.last_run.get("pages_rendered"),
                        })
                    except Exception as exc:
                        metadata = dict(batch.last_run.get("static_parser") or {})
                        results.append({
                            "company": target.name,
                            "platform": target.platform,
                            "status": "failed",
                            "scan_complete": False,
                            "reason": metadata.get("scan_completeness_reason"),
                            "stop_reason": metadata.get("stop_reason"),
                            "error": f"{type(exc).__name__}: {exc}"[:1000],
                        })
        except Exception as exc:
            for target in browser_targets:
                if not any(row["company"] == target.name for row in results):
                    results.append({
                        "company": target.name,
                        "platform": target.platform,
                        "status": "failed",
                        "scan_complete": False,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    })

    order = {name: index for index, name in enumerate(REPRESENTATIVES)}
    results.sort(key=lambda row: order[str(row["company"])])
    print(json.dumps(results, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if all(row["status"] == "ok" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
