#!/usr/bin/env python3
"""Opt-in bounded validation for the targeted terminal-evidence families.

Without ``--run`` this command only prints the representatives and performs no
network requests.  The live mode uses one configured source per changed family,
one search term for query-union adapters, and the production <=10 terminal-page
allowance.  It never runs the full scanner and never writes audit artifacts.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
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
from src.scraper.company_careers.adapters import browser_rendered_html

# Every representative remains independently bounded.  Elevated values are
# explicit observations, not limits learned from live response metadata.
HARD_REQUEST_BUDGET_PER_SOURCE = 12
SOURCE_REQUEST_BUDGETS = {
    # The live board reports about 132 pages; 140 permits the terminal page.
    "Applied Materials": 140,
    # The retained 20260924 scan used 21 requests across all five default terms.
    "Amazon": 25,
    # The current contract exposes about 65 pages.  This diagnostic allowance
    # does not make its malformed pagination metadata acceptable terminal proof.
    "Continental": 70,
    # The current board exceeded 800 rows (40 full Lever pages).  Sixty requests
    # conservatively permits 1,200 rows while remaining far below the production
    # adapter's unchanged 1,000-request / 20,000-row hard ceilings.
    "Veeva Systems": 60,
}
SOURCE_REQUEST_BUDGET_RATIONALES = {
    "Applied Materials": "observed page_count approximately 132; bounded at 140",
    "Amazon": "retained five-term enumeration used 21 requests; bounded at 25",
    "Continental": (
        "diagnostic traversal for observed page_count approximately 65; malformed "
        "metadata remains non-terminal"
    ),
    "Veeva Systems": "live board exceeded 40 full pages; bounded at 60 (1,200 rows)",
}

REPRESENTATIVES = (
    ("static", "static_html", "Applied Materials"),
    ("static", "talentbrew", "Palo Alto Networks"),
    ("browser", "browser_rendered_html", "Stantec"),
    ("terminal_api", "javascript_embedded_data", "LINE"),
    ("terminal_api", "avature", "Bank of America"),
    ("terminal_api", "javascript_embedded_data", "Continental"),
    ("terminal_api", "lever", "Veeva Systems"),
    ("negative_control", "amazon", "Amazon"),
    ("mutable_control", "icims", "ARM"),
)


def representative_targets() -> list[Any]:
    configured = {
        (target.platform, target.name): target
        for target in load_target_companies(apply_reliability_repairs=True)
    }
    missing = [
        (platform, company)
        for _role, platform, company in REPRESENTATIVES
        if (platform, company) not in configured
    ]
    if missing:
        raise ValueError(f"Missing validator representatives: {missing}")
    targets = []
    for _role, platform, company in REPRESENTATIVES:
        target = configured[(platform, company)]
        if platform in {"amazon", "google", "meta", "ibm_search"} and target.search_terms:
            target = replace(target, search_terms=target.search_terms[:1])
        targets.append(target)
    return targets


class _RequestGuard:
    def __init__(self, budget: int = HARD_REQUEST_BUDGET_PER_SOURCE) -> None:
        self.budget = budget
        self.count = 0

    def consume(self) -> None:
        if self.count >= self.budget:
            raise ValidatorRequestBudgetReached(
                f"targeted validator hard request budget exceeded ({self.budget})"
            )
        self.count += 1


class ValidatorRequestBudgetReached(RuntimeError):
    """Independent validator cap reached before the adapter returned."""


def request_budget_for(target: Any) -> int:
    return SOURCE_REQUEST_BUDGETS.get(target.name, HARD_REQUEST_BUDGET_PER_SOURCE)


class _GuardedSession(requests.Session):
    def __init__(self, guard: _RequestGuard) -> None:
        super().__init__()
        self.guard = guard

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        self.guard.consume()
        return super().request(*args, **kwargs)


@contextmanager
def _guard_browser_listing_requests(guard: _RequestGuard):
    """Enforce the same independent budget before each browser navigation."""
    original = browser_rendered_html._RenderedSession.get

    def guarded_get(rendered_session: Any, *args: Any, **kwargs: Any) -> Any:
        guard.consume()
        return original(rendered_session, *args, **kwargs)

    browser_rendered_html._RenderedSession.get = guarded_get
    try:
        yield
    finally:
        browser_rendered_html._RenderedSession.get = original


def _summary(
    target: Any,
    metadata: dict[str, Any],
    fetched_count: int,
    guard: _RequestGuard,
) -> dict[str, Any]:
    role = next(
        role
        for role, platform, company in REPRESENTATIVES
        if platform == target.platform and company == target.name
    )
    return {
        "role": role,
        "company": target.name,
        "platform": target.platform,
        "jobs_fetched": fetched_count,
        "request_budget": guard.budget,
        "effective_request_count": guard.count,
        "pages_requested": metadata.get("pages_requested"),
        "terminal_evidence": {
            "type": metadata.get("terminal_evidence_type"),
            "value": metadata.get("terminal_evidence_value"),
            "has_more": metadata.get("has_more"),
            "next_url_present": metadata.get("next_url_present"),
            "next_cursor_present": metadata.get("next_cursor_present"),
            "next_page": metadata.get("next_page"),
            "page_count": metadata.get("page_count"),
            "disabled_next_control": metadata.get("disabled_next_control"),
            "explicit_final_page": metadata.get("explicit_final_page"),
            "status": metadata.get("pagination_evidence_status"),
            "conflicting": bool(metadata.get("pagination_evidence_conflicting")),
            "malformed": bool(metadata.get("pagination_evidence_malformed")),
            "malformed_fields": metadata.get("pagination_malformed_fields") or [],
            "conflicting_fields": metadata.get("pagination_conflicting_fields") or [],
            "field_diagnostics": metadata.get("pagination_field_diagnostics") or [],
            "status_diagnostics": metadata.get("pagination_evidence_diagnostics")
            or [],
        },
        "scan_complete": bool(metadata.get("scan_complete")),
        "reason": metadata.get("scan_completeness_reason"),
        "stop_reason": metadata.get("stop_reason"),
        "collection_changed_during_scan": bool(
            metadata.get("collection_changed_during_scan")
        ),
        "validator_request_budget_reached": bool(
            metadata.get("validator_request_budget_reached")
        ),
        "query_pagination": metadata.get("query_pagination") or [],
    }


def _partial_fetched_count(metadata: dict[str, Any]) -> int:
    """Return the adapter's durable incremental normalized-job count."""
    value = metadata.get("jobs_normalized", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _mark_validator_budget_exhausted(metadata: dict[str, Any]) -> None:
    metadata["scan_complete"] = False
    metadata["scan_completeness_reason"] = "validator_request_budget_reached"
    metadata["stop_reason"] = "validator_request_budget_reached"
    metadata["validator_request_budget_reached"] = True


def _validate_target(target: Any) -> dict[str, Any]:
    guard = _RequestGuard(request_budget_for(target))
    session = _GuardedSession(guard)
    try:
        if target.platform == "browser_rendered_html":
            with _guard_browser_listing_requests(guard):
                with browser_rendered_html.BrowserBatch(timeout=20) as batch:
                    jobs = batch.fetch_jobs(target, 20)
                    metadata = dict(batch.last_run.get("static_parser") or {})
        else:
            jobs = ADAPTERS[target.platform](session, target, 20)
            metadata = source_scan_metadata(session)
        return _summary(target, metadata, len(jobs), guard)
    except ValidatorRequestBudgetReached:
        metadata = source_scan_metadata(session)
        _mark_validator_budget_exhausted(metadata)
        return _summary(
            target,
            metadata,
            _partial_fetched_count(metadata),
            guard,
        )
    except Exception as exc:  # manual diagnostic keeps every family visible
        metadata = source_scan_metadata(session)
        item = _summary(
            target,
            metadata,
            _partial_fetched_count(metadata),
            guard,
        )
        item["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        return item
    finally:
        session.close()


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
                        "role": next(
                            role
                            for role, platform, company in REPRESENTATIVES
                            if platform == target.platform and company == target.name
                        ),
                        "max_pages": target.max_pages,
                        "max_jobs": target.max_jobs,
                        "hard_request_budget": request_budget_for(target),
                        "budget_rationale": SOURCE_REQUEST_BUDGET_RATIONALES.get(
                            target.name, "default bounded representative budget"
                        ),
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
        results.append(_validate_target(target))
    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    return 0 if all("error" not in item for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
