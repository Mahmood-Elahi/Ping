"""Opt-in, read-only live canaries for source-integrity representatives.

Without ``--run`` this command is offline and only prints the selected plan.
Live mode performs no persistence and enforces an independent per-source HTTP
request budget. It never runs the full registry.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS
from src.scraper.company_careers.source_health import finalize_source_scan


DEFAULT_CANARIES = (
    "OpenAI",      # validated complete-collection API (Ashby)
    "ATCO",        # authoritative-total pagination (Phenom)
    "Waymo",       # reviewed static HTML collection
    "Microsoft",   # mutable paginated collection (Eightfold)
    "CN Rail",     # retained migration-suspected source
)
MAX_CANARIES = 6
DEFAULT_REQUEST_BUDGET = 15
MAX_REQUEST_BUDGET = 25


class CanaryBudgetReached(RuntimeError):
    pass


class BoundedSession(requests.Session):
    def __init__(self, budget: int) -> None:
        super().__init__()
        self.budget = budget
        self.requests_started = 0
        self.last_final_url: str | None = None
        self.last_redirect_count = 0

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        if self.requests_started >= self.budget:
            raise CanaryBudgetReached(
                f"live canary reached its independent request budget ({self.budget})"
            )
        self.requests_started += 1
        response = super().request(method, url, *args, **kwargs)
        self.last_final_url = str(getattr(response, "url", "") or "") or None
        self.last_redirect_count = len(getattr(response, "history", ()) or ())
        return response


def selected_targets(names: list[str]) -> list[Any]:
    configured = {
        target.name.casefold(): target
        for target in load_target_companies(apply_reliability_repairs=True)
    }
    selected: list[Any] = []
    missing: list[str] = []
    for name in names:
        target = configured.get(name.casefold())
        if target is None:
            missing.append(name)
        elif target.platform == "browser_rendered_html":
            raise ValueError("This bounded HTTP canary does not launch a browser")
        else:
            selected.append(target)
    if missing:
        raise ValueError(f"Unknown canary companies: {missing}")
    if not 1 <= len(selected) <= MAX_CANARIES:
        raise ValueError(f"Select between 1 and {MAX_CANARIES} canaries")
    return selected


def run_canary(target: Any, request_budget: int) -> dict[str, Any]:
    session = BoundedSession(request_budget)
    metadata: dict[str, Any]
    status: str
    canary_outcome: str
    error: str | None
    try:
        adapter = ADAPTERS[target.platform]
        jobs = adapter(session, target, 20)
        metadata = source_scan_metadata(session)
        _record_session_diagnostics(metadata, session, target)
        finalize_source_scan(
            metadata,
            jobs_collected=len(jobs),
            activation_state=target.source_activation,
        )
        if metadata.get("scan_state") == "complete":
            status = "ok"
            canary_outcome = _complete_outcome(metadata)
            if canary_outcome != "PASS_COMPLETE":
                status = "failed"
        else:
            canary_outcome = _incomplete_outcome(metadata)
            status = "partial" if canary_outcome == "PASS_PARTIAL" else "failed"
        error = None
    except CanaryBudgetReached as exc:
        metadata = source_scan_metadata(session)
        _record_session_diagnostics(metadata, session, target)
        metadata.update(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="canary_budget_reached",
            safety_limit_reached=True,
            canary_budget_reached=True,
        )
        finalize_source_scan(
            metadata,
            jobs_collected=int(metadata.get("jobs_normalized") or 0),
            activation_state=target.source_activation,
        )
        canary_outcome = _incomplete_outcome(metadata)
        status = "partial" if canary_outcome == "PASS_PARTIAL" else "failed"
        error = f"{type(exc).__name__}: {exc}"[:1000]
    except Exception as exc:
        metadata = source_scan_metadata(session)
        _record_session_diagnostics(metadata, session, target)
        finalize_source_scan(
            metadata,
            jobs_collected=int(metadata.get("jobs_normalized") or 0),
            failed=True,
            failure_code=f"{type(exc).__name__}: {exc}",
            activation_state=target.source_activation,
        )
        status = "failed"
        canary_outcome = _failure_outcome(metadata)
        error = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        session.close()
    return {
        "company": target.name,
        "platform": target.platform,
        "status": status,
        "canary_outcome": canary_outcome,
        "scan_state": metadata.get("scan_state"),
        "source_health_code": metadata.get("source_health_code"),
        "scan_completeness_reason": metadata.get("scan_completeness_reason"),
        "stop_reason": metadata.get("stop_reason"),
        "jobs_collected": metadata.get("jobs_collected"),
        "valid_job_records_produced": bool(
            int(metadata.get("jobs_collected") or 0) > 0
            and int(metadata.get("rejected_record_count") or 0) == 0
        ),
        "response_fully_read": metadata.get("response_fully_read"),
        "bytes_inspected": metadata.get("bytes_inspected"),
        "maximum_response_bytes": metadata.get("maximum_response_bytes"),
        "full_response_within_limit": bool(
            metadata.get("response_fully_read")
            and not metadata.get("safety_limit_reached")
            and not metadata.get("response_truncated")
        ),
        "unique_stable_ids": metadata.get("unique_stable_ids"),
        "unique_canonical_urls": metadata.get("unique_canonical_urls"),
        "rejected_record_count": metadata.get("rejected_record_count"),
        "duplicate_ids": metadata.get("duplicate_ids", []),
        "duplicate_canonical_urls": metadata.get(
            "duplicate_canonical_urls", []
        ),
        "stable_ids_unique": bool(
            int(metadata.get("jobs_collected") or 0) > 0
            and int(metadata.get("unique_stable_ids") or 0)
            == int(metadata.get("jobs_collected") or 0)
            and not metadata.get("duplicate_ids")
        ),
        "canonical_urls_unique": bool(
            int(metadata.get("jobs_collected") or 0) > 0
            and int(metadata.get("unique_canonical_urls") or 0)
            == int(metadata.get("jobs_collected") or 0)
            and not metadata.get("duplicate_canonical_urls")
        ),
        "total_available": metadata.get("total_available"),
        "authoritative_total_present": metadata.get(
            "authoritative_total_present"
        ),
        "pages_requested": metadata.get("pages_requested"),
        "terminal_evidence_type": metadata.get("terminal_evidence_type"),
        "terminal_evidence_value": metadata.get("terminal_evidence_value"),
        "original_configured_url": metadata.get("original_configured_url"),
        "final_url": metadata.get("final_url"),
        "final_host": metadata.get("final_host"),
        "redirect_count": session.last_redirect_count,
        "source_identity_status": metadata.get("source_identity_status"),
        "configured_tenant": metadata.get("configured_tenant") or target.tenant,
        "configured_site": metadata.get("configured_site") or target.site_id,
        "source_identity_diagnostic": metadata.get("source_identity_diagnostic"),
        "pagination_evidence_diagnostics": metadata.get(
            "pagination_evidence_diagnostics", []
        ),
        "diagnostics": metadata.get("diagnostics", []),
        "warnings": metadata.get("warnings", []),
        "requests_started": session.requests_started,
        "request_budget": request_budget,
        "error": error,
    }


def _record_session_diagnostics(
    metadata: dict[str, Any], session: BoundedSession, target: Any
) -> None:
    configured_url = next(
        (
            str(value)
            for value in (
                target.endpoint_url,
                target.search_url,
                target.listing_url,
                target.careers_url,
            )
            if str(value or "").strip()
        ),
        None,
    )
    final_url = session.last_final_url or metadata.get("final_url")
    metadata["original_configured_url"] = (
        metadata.get("original_configured_url") or configured_url
    )
    metadata["final_url"] = final_url
    metadata["final_host"] = (
        (urlparse(str(final_url)).hostname or "").casefold() if final_url else None
    )
    metadata["redirect_count"] = session.last_redirect_count
    metadata["requests_started"] = session.requests_started


def _failure_outcome(metadata: dict[str, Any]) -> str:
    code = str(metadata.get("source_health_code") or "")
    if code == "source_migration_suspected":
        return "MIGRATION_SUSPECTED"
    if code in {
        "identity_failure",
        "malformed_collection",
        "malformed_records",
        "pagination_loop",
        "terminal_contradiction",
        "collection_changed",
    }:
        return "FAIL_CONTRACT"
    return "FAIL_SOURCE"


def _complete_outcome(metadata: dict[str, Any]) -> str:
    if metadata.get("source_identity_status") == "validated":
        return "PASS_COMPLETE"
    return "FAIL_CONTRACT"


def _incomplete_outcome(metadata: dict[str, Any]) -> str:
    code = str(metadata.get("source_health_code") or "")
    if code == "source_migration_suspected":
        return "MIGRATION_SUSPECTED"
    if (
        metadata.get("endpoint_variant") == "source_jobs_v1"
        and code in {"pagination_unproven", "authoritative_total_not_reached"}
        and metadata.get("source_identity_status") == "validated"
        and int(metadata.get("jobs_normalized") or 0) > 0
        and int(metadata.get("rejected_record_count") or 0) == 0
        and not metadata.get("duplicate_ids")
        and not metadata.get("duplicate_canonical_urls")
    ):
        # A valid inventory is useful for discovery even when it is not
        # authoritative for absence.  PASS_PARTIAL never marks it healthy.
        return "PASS_PARTIAL"
    if (
        code == "safety_limit_reached"
        and metadata.get("source_identity_status") == "validated"
    ):
        return "PASS_PARTIAL"
    return "FAIL_CONTRACT"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", action="append", dest="companies")
    parser.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET)
    parser.add_argument("--run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.request_budget <= MAX_REQUEST_BUDGET:
        raise ValueError(
            f"--request-budget must be between 1 and {MAX_REQUEST_BUDGET}"
        )
    targets = selected_targets(args.companies or list(DEFAULT_CANARIES))
    if not args.run:
        print(
            json.dumps(
                [
                    {
                        "company": target.name,
                        "platform": target.platform,
                        "source_activation": target.source_activation,
                        "max_pages": target.max_pages,
                        "max_jobs": target.max_jobs,
                        "request_budget": args.request_budget,
                    }
                    for target in targets
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    rows = [run_canary(target, args.request_budget) for target in targets]
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0 if all(
        row["canary_outcome"] in {"PASS_COMPLETE", "PASS_PARTIAL"}
        for row in rows
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
