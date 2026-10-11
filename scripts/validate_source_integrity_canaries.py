"""Opt-in, read-only live canaries for source-integrity representatives.

Without ``--run`` this command is offline and only prints the selected plan.
Live mode performs no persistence and enforces an independent per-source HTTP
request budget. It never runs the full registry.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.models import (
    AdapterResult,
    SourceScanMetadata,
    adapter_exception_metadata,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS
from src.scraper.company_careers.source_health import finalize_source_scan


DEFAULT_CANARIES = (
    "OpenAI",      # validated complete-collection API (Ashby)
    "ATCO",        # authoritative-total pagination (Phenom)
    "Waymo",       # validated Greenhouse complete-collection source
    "Microsoft",   # mutable paginated collection (Eightfold)
    "CN Rail",     # retained migration-suspected source
)
MAX_CANARIES = 6
DEFAULT_REQUEST_BUDGET = 15
STANDARD_MAX_REQUEST_BUDGET = 52
REVIEWED_HIGH_VOLUME_SMARTRECRUITERS_TOTALS = {
    "Wise": 403,
    "ServiceNow": 708,
}
PRODUCTION_REGISTRY = ROOT / "config" / "target_companies.yaml"


def smartrecruiters_canary_request_budget(total: int, page_size: int = 100) -> int:
    """Budget listing-only SmartRecruiters inventory enumeration."""
    return (total + page_size - 1) // page_size


REVIEWED_HIGH_VOLUME_SMARTRECRUITERS_BUDGETS = {
    company: smartrecruiters_canary_request_budget(total)
    for company, total in REVIEWED_HIGH_VOLUME_SMARTRECRUITERS_TOTALS.items()
}
MAX_REQUEST_BUDGET = STANDARD_MAX_REQUEST_BUDGET


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


def selected_targets(
    names: list[str],
    registry: Path | None = None,
    source_scope: str | None = None,
) -> list[Any]:
    registry_path = (registry or PRODUCTION_REGISTRY).resolve()
    configured = load_target_companies(
        str(registry_path),
        apply_reliability_repairs=registry_path == PRODUCTION_REGISTRY.resolve(),
    )
    selected: list[Any] = []
    missing: list[str] = []
    for name in names:
        matches = [
            target
            for target in configured
            if target.name.casefold() == name.casefold()
            and (source_scope is None or target.source_scope == source_scope)
        ]
        if not matches:
            missing.append(name)
            continue
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous canary company {name!r}; specify --source-scope"
            )
        target = matches[0]
        if target.platform == "browser_rendered_html":
            raise ValueError("This bounded HTTP canary does not launch a browser")
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
    item_failures: list[dict[str, Any]] = []
    try:
        adapter = ADAPTERS[target.platform]
        output = adapter(session, target, 20)
        if isinstance(output, AdapterResult):
            jobs = output.jobs
            metadata = SourceScanMetadata.from_value(output.scan)
            inventory_items_collected = len(output.inventory_items)
            unreadable_items = sum(
                failure.outcome == "unreadable" for failure in output.item_failures
            )
            rejected_items = sum(
                failure.outcome == "rejected" for failure in output.item_failures
            )
            item_failures = [
                {
                    "item_id": failure.item_id,
                    "stage": failure.stage,
                    "outcome": failure.outcome,
                    "retryable": failure.retryable,
                    "reason": failure.reason[:500],
                }
                for failure in output.item_failures[:20]
            ]
        else:
            jobs = list(output)
            metadata = source_scan_metadata(session)
            inventory_items_collected = len(jobs)
            unreadable_items = 0
            rejected_items = 0
        for job in jobs:
            if hasattr(job, "source_scope"):
                job.source_scope = target.source_scope
        _record_job_integrity(metadata, jobs)
        _record_target_contract(metadata, jobs, target, request_budget, session)
        _record_session_diagnostics(metadata, session, target)
        finalize_source_scan(
            metadata,
            jobs_collected=len(jobs),
            inventory_items_collected=inventory_items_collected,
            unreadable_items=unreadable_items,
            rejected_items=rejected_items,
            activation_state=target.source_activation,
        )
        if metadata.get("scan_state") == "complete":
            status = "ok"
            canary_outcome = _complete_outcome(metadata, target)
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
        metadata = adapter_exception_metadata(exc) or source_scan_metadata(session)
        _record_session_diagnostics(metadata, session, target)
        failure_code = f"{type(exc).__name__}: {exc}"
        if metadata.get("source_identity_status") == "failed":
            failure_code = f"identity failure: {failure_code}"
        finalize_source_scan(
            metadata,
            jobs_collected=int(metadata.get("jobs_normalized") or 0),
            failed=True,
            failure_code=failure_code,
            activation_state=target.source_activation,
        )
        status = "failed"
        canary_outcome = _failure_outcome(metadata, exc)
        error = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        session.close()
    return {
        "company": target.name,
        "source_scope": target.source_scope,
        "source_scope_key": target.source_scope_key,
        "platform": target.platform,
        "status": status,
        "canary_outcome": canary_outcome,
        "scan_complete": metadata.get("scan_complete"),
        "scan_state": metadata.get("scan_state"),
        "source_health_code": metadata.get("source_health_code"),
        "scan_completeness_reason": metadata.get("scan_completeness_reason"),
        "stop_reason": metadata.get("stop_reason"),
        "safety_limit_reached": metadata.get("safety_limit_reached"),
        "collection_changed_during_scan": metadata.get(
            "collection_changed_during_scan"
        ),
        "inventory_complete": metadata.get("inventory_complete"),
        "central_finalizer_authoritative": bool(
            metadata.get("scan_state") == "complete"
            and metadata.get("inventory_complete") is True
        ),
        "inventory_items_collected": metadata.get("inventory_items_collected"),
        "unreadable_items": metadata.get("unreadable_items"),
        "rejected_items": metadata.get("rejected_items"),
        "item_failures": item_failures,
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
        "rejected_record_reason": metadata.get("rejected_record_reason"),
        "rejected_record_ids": metadata.get("rejected_record_ids", []),
        "rejected_posting_ids": metadata.get("rejected_posting_ids", []),
        "rejected_requisition_ids": metadata.get(
            "rejected_requisition_ids", []
        ),
        "duplicate_requisitions_skipped": metadata.get(
            "duplicate_requisitions_skipped", 0
        ),
        "duplicate_posting_ids_skipped": metadata.get(
            "duplicate_posting_ids_skipped", []
        ),
        "duplicate_requisition_ids": metadata.get(
            "duplicate_requisition_ids", []
        ),
        "duplicate_ids": metadata.get("duplicate_ids", []),
        "duplicate_canonical_urls": metadata.get(
            "duplicate_canonical_urls", []
        ),
        "stable_ids_unique": _integrity_count_matches(
            metadata, "unique_stable_ids", "duplicate_ids"
        ),
        "canonical_urls_unique": _integrity_count_matches(
            metadata, "unique_canonical_urls", "duplicate_canonical_urls"
        ),
        "normalized_jobs_attributable": metadata.get(
            "normalized_jobs_attributable"
        ),
        "apply_urls_contract_valid": metadata.get(
            "apply_urls_contract_valid"
        ),
        "total_available": metadata.get("total_available"),
        "authoritative_total_present": metadata.get(
            "authoritative_total_present"
        ),
        "pages_requested": metadata.get("pages_requested"),
        "terminal_evidence_type": metadata.get("terminal_evidence_type"),
        "terminal_evidence_value": metadata.get("terminal_evidence_value"),
        "terminal_evidence_valid": _terminal_evidence_valid(metadata),
        "hidden_continuation_absent": _hidden_continuation_absent(metadata),
        "original_configured_url": metadata.get("original_configured_url"),
        "final_url": metadata.get("final_url"),
        "final_host": metadata.get("final_host"),
        "redirect_count": session.last_redirect_count,
        "source_identity_status": metadata.get("source_identity_status"),
        "configured_slug": target.slug,
        "configured_tenant": metadata.get("configured_tenant") or target.tenant,
        "configured_site": metadata.get("configured_site") or target.site_id,
        "source_identity_diagnostic": metadata.get("source_identity_diagnostic"),
        "greenhouse_url_identity_failure": metadata.get(
            "greenhouse_url_identity_failure"
        ),
        "pagination_evidence_diagnostics": metadata.get(
            "pagination_evidence_diagnostics", []
        ),
        "pagination_evidence_conflicting": metadata.get(
            "pagination_evidence_conflicting"
        ),
        "pagination_evidence_malformed": metadata.get(
            "pagination_evidence_malformed"
        ),
        "adapter_pagination": metadata.get("adapter_pagination"),
        "diagnostics": metadata.get("diagnostics", []),
        "warnings": metadata.get("warnings", []),
        "requests_started": session.requests_started,
        "request_budget": request_budget,
        "request_budget_respected": session.requests_started <= request_budget,
        "max_pages": target.max_pages,
        "max_jobs": target.max_jobs,
        "max_response_bytes": target.max_response_bytes,
        "persistence_mode": "none",
        "production_registry_mutated": False,
        "lifecycle_mutations": 0,
        "dashboard_published": False,
        "error": error,
    }


def _record_job_integrity(metadata: dict[str, Any], jobs: list[Any]) -> None:
    """Publish record identity evidence for adapters that validate it internally."""
    if not all(hasattr(job, "job_id") and hasattr(job, "url") for job in jobs):
        return
    ids = [str(job.job_id or "").strip() for job in jobs]
    urls = [str(job.url or "").strip() for job in jobs]
    id_counts = Counter(ids)
    url_counts = Counter(urls)
    duplicate_ids = sorted(value for value, count in id_counts.items() if count > 1)
    duplicate_urls = sorted(value for value, count in url_counts.items() if count > 1)
    if any(not value for value in ids):
        duplicate_ids.append("<missing>")
    if any(not value for value in urls):
        duplicate_urls.append("<missing>")
    metadata.update(
        jobs_normalized=len(jobs),
        unique_stable_ids=len({value for value in ids if value}),
        unique_canonical_urls=len({value for value in urls if value}),
        duplicate_ids=duplicate_ids[:20],
        duplicate_canonical_urls=duplicate_urls[:20],
    )


def _record_target_contract(
    metadata: dict[str, Any],
    jobs: list[Any],
    target: Any,
    request_budget: int,
    session: BoundedSession,
) -> None:
    """Record canary-only attribution and apply-URL contract evidence."""
    allowed_hosts = {
        str(host).strip().casefold().rstrip(".")
        for host in target.allowed_apply_hosts
        if str(host).strip()
    }
    attributable = all(
        str(getattr(job, "company", "")) == target.name
        and str(getattr(job, "source_scope", "")) == target.source_scope
        for job in jobs
    )
    apply_urls_valid = True
    for job in jobs:
        parsed = urlparse(str(getattr(job, "url", "") or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https" or not host or (
            allowed_hosts and host not in allowed_hosts
        ):
            apply_urls_valid = False
            break
        if (
            target.platform == "greenhouse"
            and target.endpoint_variant == "boards_api_v1_strict"
        ):
            job_id = str(getattr(job, "job_id", ""))
            template = str(target.apply_url_template or "")
            if template:
                exact_url_valid = str(getattr(job, "url", "")) == template.replace(
                    "{id}", job_id
                )
            else:
                expected_path = f"/{target.slug}/jobs/{job_id}"
                exact_url_valid = (
                    parsed.path.casefold() == expected_path.casefold()
                    and not parsed.query
                    and not parsed.fragment
                )
            if not exact_url_valid:
                apply_urls_valid = False
                break
    metadata.update(
        normalized_jobs_attributable=attributable,
        apply_urls_contract_valid=apply_urls_valid,
        request_budget_respected=session.requests_started <= request_budget,
    )
    if target.platform == "greenhouse":
        pagination = getattr(session, "greenhouse_pagination", None)
        if isinstance(pagination, dict):
            metadata.update(
                response_fully_read=True,
                maximum_response_bytes=target.max_response_bytes,
                bytes_inspected=sum(
                    int(pagination.get(field) or 0)
                    for field in ("listing_bytes", "department_bytes", "detail_bytes")
                ),
                adapter_pagination=dict(pagination),
            )


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


def _failure_outcome(metadata: dict[str, Any], exc: BaseException) -> str:
    code = str(metadata.get("source_health_code") or "")
    if code == "source_migration_suspected":
        return "MIGRATION_SUSPECTED"
    message = f"{type(exc).__name__}: {exc}".casefold()
    if (
        isinstance(exc, requests.RequestException)
        or code
        in {
            "source_unreachable",
            "temporary_unexpected_response",
            "authentication_or_block",
        }
        or any(
            marker in message
            for marker in (
                "http 408",
                "http 425",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "timeout",
                "timed out",
            )
        )
    ):
        return "FAIL_TRANSIENT"
    if (
        code == "identity_failure"
        or metadata.get("source_identity_status") == "failed"
        or any(
            marker in message
            for marker in ("http 404", "http 410", "identity mismatch")
        )
    ):
        return "FAIL_IDENTITY"
    if code in {
        "malformed_collection",
        "malformed_records",
        "pagination_loop",
        "terminal_contradiction",
        "collection_changed",
    }:
        return "FAIL_CONTRACT"
    return "FAIL_CONTRACT"


def _integrity_count_matches(
    metadata: dict[str, Any], count_field: str, duplicate_field: str
) -> bool:
    count = metadata.get(count_field)
    return bool(
        isinstance(count, int)
        and not isinstance(count, bool)
        and count == int(metadata.get("jobs_collected") or 0)
        and not metadata.get(duplicate_field)
    )


def _complete_outcome(metadata: dict[str, Any], target: Any) -> str:
    identity_status = metadata.get("source_identity_status")
    identity_proven = identity_status == "validated" or (
        target.platform == "public_json" and identity_status == "verified"
    )
    if not identity_proven:
        return "FAIL_IDENTITY"
    required_contract = all(
        (
            metadata.get("scan_state") == "complete",
            metadata.get("inventory_complete") is True,
            _terminal_evidence_valid(metadata),
            _hidden_continuation_absent(metadata),
            not metadata.get("collection_changed_during_scan"),
            not metadata.get("safety_limit_reached"),
            metadata.get("request_budget_respected") is True,
            int(metadata.get("unreadable_items") or 0) == 0,
            int(metadata.get("rejected_items") or 0) == 0,
            int(metadata.get("rejected_record_count") or 0) == 0,
        )
    )
    if not required_contract:
        return "FAIL_CONTRACT"
    if (
        target.platform == "greenhouse"
        and target.endpoint_variant == "boards_api_v1_strict"
        and not all(
            (
                metadata.get("normalized_jobs_attributable") is True,
                metadata.get("apply_urls_contract_valid") is True,
                _integrity_count_matches(
                    metadata, "unique_stable_ids", "duplicate_ids"
                ),
                _integrity_count_matches(
                    metadata,
                    "unique_canonical_urls",
                    "duplicate_canonical_urls",
                ),
            )
        )
    ):
        return "FAIL_CONTRACT"
    if target.platform == "public_json" and not (
        metadata.get("collection_integrity_contract") is True
        and _integrity_count_matches(
            metadata, "unique_stable_ids", "duplicate_ids"
        )
        and _integrity_count_matches(
            metadata, "unique_canonical_urls", "duplicate_canonical_urls"
        )
    ):
        return "FAIL_CONTRACT"
    return "PASS_COMPLETE"


def _terminal_evidence_valid(metadata: dict[str, Any]) -> bool:
    evidence_type = str(metadata.get("terminal_evidence_type") or "")
    if metadata.get("scan_state") != "complete" or not evidence_type:
        return False
    if evidence_type == "authoritative_total":
        total = metadata.get("total_available")
        observed = metadata.get("inventory_items_collected")
        if observed is None:
            observed = metadata.get("jobs_collected")
        return (
            isinstance(total, int)
            and not isinstance(total, bool)
            and total == int(observed or 0)
        )
    return evidence_type in {
        "complete_collection_response",
        "validated_empty_collection",
        "terminal_page",
    }


def _hidden_continuation_absent(metadata: dict[str, Any]) -> bool:
    return not any(
        (
            metadata.get("has_more") is True,
            metadata.get("next_url_present") is True,
            metadata.get("next_cursor_present") is True,
            metadata.get("next_page") is not None,
            metadata.get("pagination_evidence_conflicting"),
            metadata.get("pagination_evidence_malformed"),
        )
    )


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
    parser.add_argument(
        "--registry",
        type=Path,
        help=(
            "Explicit registry to canary. Omit to use the production registry "
            "with reliability repairs."
        ),
    )
    parser.add_argument(
        "--source-scope",
        help="Select one exact logical source scope from the explicit registry",
    )
    parser.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET)
    parser.add_argument("--run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    targets = selected_targets(
        args.companies or list(DEFAULT_CANARIES),
        registry=args.registry,
        source_scope=args.source_scope,
    )
    validate_request_budget(args.request_budget, targets)
    if not args.run:
        print(
            json.dumps(
                [
                    {
                        "company": target.name,
                        "source_scope": target.source_scope,
                        "platform": target.platform,
                        "source_activation": target.source_activation,
                        "max_pages": target.max_pages,
                        "max_jobs": target.max_jobs,
                        "max_response_bytes": target.max_response_bytes,
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
        row["canary_outcome"] == "PASS_COMPLETE" for row in rows
    ) else 1


def validate_request_budget(request_budget: int, targets: list[Any]) -> None:
    if not 1 <= request_budget <= MAX_REQUEST_BUDGET:
        raise ValueError(
            f"--request-budget must be between 1 and {MAX_REQUEST_BUDGET}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
