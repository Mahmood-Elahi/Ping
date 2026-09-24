#!/usr/bin/env python3
"""Manual bounded validation matrix for the 1000-request large-board policy.

This validator is intentionally excluded from automated tests. It invokes only
the named representatives, never the full scanner, never writes artifacts, and
fails closed before a representative can start more than 1000 HTTP requests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.models import AdapterError, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


MAX_HTTP_REQUESTS_PER_REPRESENTATIVE = 1_000
TIMEOUT_SECONDS = 20
REPRESENTATIVES = {
    "200_to_300_requests": "Eaton",        # 235 projected listing requests
    "300_to_500_requests": "Schneider Electric",  # 352 projected requests
    "beyond_old_ceiling_now_recoverable": "ABB",  # 214 projected requests
    "detail_coupled_control": "Assent",
    "mutable_control": "ARM",
}
# The latest artifact set has no deterministic listing-only source projected
# above 1000 requests and no source above 20000 raw jobs, so there is no honest
# artifact-backed representative for either optional control category.


class BoundedSession(requests.Session):
    """A stricter total-request cap that also bounds listing requests."""

    def __init__(self) -> None:
        super().__init__()
        self.requests_started = 0

    def request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.requests_started += 1
        if self.requests_started > MAX_HTTP_REQUESTS_PER_REPRESENTATIVE:
            raise AdapterError(
                "Large-board validator exceeded its 1000-request per-source cap"
            )
        return super().request(method, url, *args, **kwargs)


def representative_targets() -> list[tuple[str, object]]:
    configured = {target.name: target for target in load_target_companies()}
    missing = sorted(set(REPRESENTATIVES.values()) - configured.keys())
    if missing:
        raise ValueError(f"Missing large-board representatives: {missing}")
    return [(band, configured[name]) for band, name in REPRESENTATIVES.items()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--representative",
        choices=tuple(REPRESENTATIVES),
        action="append",
        help="Run only the named matrix category; repeat to select several.",
    )
    args = parser.parse_args()
    selected = set(args.representative or REPRESENTATIVES)
    results = []
    for category, target in representative_targets():
        if category not in selected:
            continue
        session = BoundedSession()
        try:
            jobs = ADAPTERS[target.platform](session, target, TIMEOUT_SECONDS)
            metadata = source_scan_metadata(session)
            results.append(
                {
                    "category": category,
                    "company": target.name,
                    "platform": target.platform,
                    "status": "ok",
                    "fetched_count": len(jobs),
                    "scan_complete": bool(metadata.get("scan_complete")),
                    "reason": metadata.get("scan_completeness_reason"),
                    "total_available": metadata.get("total_available"),
                    "pages_requested": metadata.get("pages_requested"),
                    "effective_ceiling": metadata.get(
                        "effective_listing_request_ceiling"
                    ),
                    "detail_requests_coupled": metadata.get(
                        "detail_requests_coupled"
                    ),
                    "collection_changed_during_scan": metadata.get(
                        "collection_changed_during_scan"
                    ),
                    "http_requests_started": session.requests_started,
                }
            )
        except Exception as exc:  # manual matrix reports every representative
            metadata = source_scan_metadata(session)
            results.append(
                {
                    "category": category,
                    "company": target.name,
                    "platform": target.platform,
                    "status": "failed",
                    "scan_complete": bool(metadata.get("scan_complete")),
                    "reason": metadata.get("scan_completeness_reason"),
                    "http_requests_started": session.requests_started,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            )
        finally:
            session.close()

    print(json.dumps(results, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if all(row["status"] == "ok" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
