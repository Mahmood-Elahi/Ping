#!/usr/bin/env python3
"""Manual, read-only live validator for Canada-priority recovery batch 1.

PowerShell:
  .\.venv\Scripts\python.exe scripts\validate_canada_priority_source_recovery_batch1.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.models import AdapterError, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


COMPANIES = (
    "Assent",
    "Calfrac Well Services",
    "Arista Networks",
    "Gameloft",
    "Ubisoft",
)
MAX_REQUESTS_PER_SOURCE = 400
TIMEOUT_SECONDS = 20


class BoundedSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.request_count = 0

    def request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.request_count += 1
        if self.request_count > MAX_REQUESTS_PER_SOURCE:
            raise AdapterError("validator per-source request bound reached")
        return super().request(method, url, *args, **kwargs)


def main() -> int:
    configured = {
        target.name: target
        for target in load_target_companies(apply_reliability_repairs=True)
    }
    missing = sorted(set(COMPANIES) - configured.keys())
    if missing:
        raise ValueError(f"Missing configured targets: {missing}")

    failed = False
    for company in COMPANIES:
        target = configured[company]
        session = BoundedSession()
        jobs = []
        error = None
        try:
            jobs = ADAPTERS[target.platform](session, target, TIMEOUT_SECONDS)
            status = "ok"
        except Exception as exc:  # manual matrix reports every source
            failed = True
            status = "error"
            error = f"{type(exc).__name__}: {exc}"[:1000]
        finally:
            metadata = source_scan_metadata(session)
            row = {
                "company": company,
                "platform": target.platform,
                "status": status,
                "error": error,
                "job_count": len(jobs),
                "sample_job_ids": [job.job_id for job in jobs[:5]],
                "sources": sorted({job.source for job in jobs}),
                "hosts": sorted(
                    {
                        urlparse(job.url).hostname.casefold()
                        for job in jobs
                        if urlparse(job.url).hostname
                    }
                ),
                "reported_total": metadata.get("total_available"),
                "pages_requested": metadata.get("pages_requested"),
                "pages_completed": metadata.get("pages_completed"),
                "scan_complete": bool(metadata.get("scan_complete")),
                "scan_completeness_reason": metadata.get(
                    "scan_completeness_reason"
                ),
                "terminal_evidence": {
                    "type": metadata.get("terminal_evidence_type"),
                    "value": metadata.get("terminal_evidence_value"),
                    "has_more": metadata.get("has_more"),
                    "next_cursor_present": metadata.get("next_cursor_present"),
                    "terminal_page_observed": metadata.get(
                        "terminal_page_observed"
                    ),
                    "complete_collection_response": metadata.get(
                        "complete_collection_response"
                    ),
                },
                "collection_changed": metadata.get(
                    "collection_changed_during_scan"
                ),
                "request_count": session.request_count,
            }
            print(json.dumps(row, ensure_ascii=True, separators=(",", ":")))
            session.close()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
