"""Canonical, fail-closed source-health evaluation.

Adapters collect platform-specific evidence.  This module is the single place
that decides whether that evidence is sufficient for authoritative lifecycle
absence.  Safety budgets are deliberately never terminal evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, MutableMapping, Sequence
from urllib.parse import urlparse


class SourceScanState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class SourceHealthCode(str, Enum):
    HEALTHY = "healthy"
    SOURCE_UNREACHABLE = "source_unreachable"
    SOURCE_STALE = "source_stale"
    SOURCE_MIGRATION_SUSPECTED = "source_migration_suspected"
    AUTHENTICATION_OR_BLOCK = "authentication_or_block"
    TEMPORARY_UNEXPECTED_RESPONSE = "temporary_unexpected_response"
    IDENTITY_FAILURE = "identity_failure"
    MALFORMED_COLLECTION = "malformed_collection"
    MALFORMED_RECORDS = "malformed_records"
    PAGINATION_UNPROVEN = "pagination_unproven"
    PAGINATION_LOOP = "pagination_loop"
    SAFETY_LIMIT_REACHED = "safety_limit_reached"
    COLLECTION_CHANGED = "collection_changed"
    TERMINAL_CONTRADICTION = "terminal_contradiction"
    AUTHORITATIVE_TOTAL_NOT_REACHED = "authoritative_total_not_reached"
    UNPROVEN_EMPTY = "unproven_empty"


_COMPLETE_COLLECTION_REASONS = {
    "complete_collection_response",
    "proven_structured_collection",
}
_TERMINAL_PAGE_REASONS = {
    "natural_terminal_page",
    "explicit_terminal_page",
    "validated_html_terminal",
    "validated_browser_terminal",
    "all_search_terms_terminal",
    "no_next_page",
    "configured_next_link_absent",
    "validated_terminal_empty_page",
}
_LOOP_REASONS = {
    "repeated_page",
    "no_new_stable_ids",
    "repeated_cursor",
    "repeated_stable_ids",
    "cursor_not_advancing",
    "page_number_ignored",
}


def finalize_source_scan(
    metadata: MutableMapping[str, Any],
    *,
    jobs_collected: int,
    failed: bool = False,
    failure_code: str | None = None,
    activation_state: str = "active",
) -> MutableMapping[str, Any]:
    """Normalize adapter evidence and enforce the authoritative boundary.

    This function may downgrade an adapter's claim, but never upgrades an
    unproven claim except when an explicitly authoritative total exactly equals
    the validated inventory.  The normalized ``scan_state`` is what lifecycle
    consumers should persist and inspect.
    """
    jobs_collected = max(0, int(jobs_collected))
    metadata["jobs_collected"] = jobs_collected
    metadata["source_activation_state"] = str(activation_state or "active")
    original_complete = bool(metadata.get("scan_complete"))
    original_reason = str(metadata.get("scan_completeness_reason") or "")
    original_stop_reason = str(metadata.get("stop_reason") or "")

    if failed:
        metadata["scan_complete"] = False
        metadata["scan_state"] = SourceScanState.FAILED.value
        metadata["source_health_code"] = _failure_health_code(
            failure_code or str(metadata.get("stop_reason") or "")
        ).value
        _normalize_started_stop_reason(
            metadata,
            jobs_collected,
            failure_reason=metadata["source_health_code"],
        )
        if metadata["source_health_code"] == SourceHealthCode.IDENTITY_FAILURE.value:
            metadata["source_identity_status"] = "failed"
        return metadata

    _normalize_started_stop_reason(metadata, jobs_collected)
    _normalize_legacy_terminal_evidence(metadata, jobs_collected)
    code = _completion_contradiction(metadata, jobs_collected)
    if str(activation_state or "active").casefold() != "active":
        code = code or SourceHealthCode.PAGINATION_UNPROVEN
        metadata["scan_complete"] = False
        metadata["scan_completeness_reason"] = "source_not_authoritative"
        metadata["stop_reason"] = "source_not_authoritative"
    elif code is not None:
        metadata["scan_complete"] = False
        preserve_adapter_reason = (
            not original_complete
            and original_reason
            not in {"adapter_completeness_not_proven", "not_established", "safety_limit_reached"}
        )
        metadata["scan_completeness_reason"] = (
            original_reason if preserve_adapter_reason else code.value
        )
        if preserve_adapter_reason and original_stop_reason:
            metadata["stop_reason"] = original_stop_reason
        elif not str(metadata.get("stop_reason") or "").strip() or metadata.get(
            "stop_reason"
        ) in _COMPLETE_COLLECTION_REASONS | _TERMINAL_PAGE_REASONS | {
            "reported_total_reached",
            "total_jobs_reached",
            "validated_zero_jobs",
        }:
            metadata["stop_reason"] = code.value

    if metadata.get("scan_complete"):
        metadata["scan_state"] = SourceScanState.COMPLETE.value
        metadata["source_health_code"] = SourceHealthCode.HEALTHY.value
        # Independently proven terminal evidence supersedes a coincident budget.
        metadata["safety_limit_reached"] = False
    else:
        metadata["scan_state"] = SourceScanState.INCOMPLETE.value
        metadata["source_health_code"] = (
            code or _incomplete_health_code(metadata, jobs_collected)
        ).value
    return metadata


def _normalize_started_stop_reason(
    metadata: MutableMapping[str, Any],
    jobs_collected: int,
    *,
    failure_reason: str | None = None,
) -> None:
    """Never describe a scan with observed progress as not started."""
    if str(metadata.get("stop_reason") or "").strip() != "not_started":
        return
    progress = (
        int(metadata.get("requests_started") or 0),
        int(metadata.get("pages_requested") or 0),
        int(metadata.get("pages_completed") or 0),
        int(metadata.get("jobs_normalized") or 0),
        jobs_collected,
    )
    if not any(value > 0 for value in progress):
        return

    if failure_reason and failure_reason != SourceHealthCode.SOURCE_UNREACHABLE.value:
        metadata["stop_reason"] = failure_reason
        return
    completeness_reason = str(metadata.get("scan_completeness_reason") or "").strip()
    if completeness_reason not in {
        "",
        "adapter_completeness_not_proven",
        "not_established",
    }:
        metadata["stop_reason"] = completeness_reason
        return
    metadata["stop_reason"] = "adapter_error_after_progress"


def _normalize_legacy_terminal_evidence(
    metadata: MutableMapping[str, Any], jobs_collected: int
) -> None:
    """Make existing reviewed adapter contracts explicit in diagnostics."""
    reason = str(metadata.get("scan_completeness_reason") or "")
    stop_reason = str(metadata.get("stop_reason") or "")
    total = metadata.get("total_available")

    if (
        metadata.get("authoritative_total_present")
        and _valid_total(total)
        and int(total) == jobs_collected
    ):
        metadata["terminal_evidence_type"] = "authoritative_total"
        metadata["terminal_evidence_value"] = int(total)
        if metadata.get("has_more") is not True:
            metadata["has_more"] = False
        metadata["terminal_page_observed"] = True
        metadata["scan_complete"] = True
        metadata["scan_completeness_reason"] = "reported_total_reached"
        metadata["stop_reason"] = "reported_total_reached"
    elif reason == "reported_total_reached" or stop_reason == "total_jobs_reached":
        if _valid_total(total) and int(total) == jobs_collected:
            metadata["authoritative_total_present"] = True
            metadata["terminal_evidence_type"] = "authoritative_total"
            metadata["terminal_evidence_value"] = int(total)
            if metadata.get("has_more") is not True:
                metadata["has_more"] = False
            metadata["terminal_page_observed"] = True
            metadata["scan_complete"] = True
    elif reason in _COMPLETE_COLLECTION_REASONS or stop_reason in _COMPLETE_COLLECTION_REASONS:
        metadata["terminal_evidence_type"] = "complete_collection_response"
        metadata["terminal_evidence_value"] = reason or stop_reason
        metadata["complete_collection_response"] = True
    elif reason in _TERMINAL_PAGE_REASONS or stop_reason in _TERMINAL_PAGE_REASONS:
        metadata["terminal_evidence_type"] = metadata.get(
            "terminal_evidence_type"
        ) or "terminal_page"
        metadata["terminal_evidence_value"] = metadata.get(
            "terminal_evidence_value"
        ) or reason or stop_reason
        metadata["terminal_page_observed"] = True
    elif stop_reason == "validated_zero_jobs" and jobs_collected == 0:
        metadata["terminal_evidence_type"] = "validated_empty_collection"
        metadata["terminal_evidence_value"] = 0
        metadata["complete_collection_response"] = True
        metadata["terminal_page_observed"] = True


def _completion_contradiction(
    metadata: MutableMapping[str, Any], jobs_collected: int
) -> SourceHealthCode | None:
    reason = str(metadata.get("stop_reason") or "").casefold()
    if metadata.get("collection_changed_during_scan") or metadata.get(
        "total_count_changed"
    ):
        return SourceHealthCode.COLLECTION_CHANGED
    if metadata.get("pagination_evidence_malformed"):
        return SourceHealthCode.MALFORMED_COLLECTION
    if metadata.get("pagination_evidence_conflicting"):
        return SourceHealthCode.TERMINAL_CONTRADICTION
    if reason in _LOOP_REASONS:
        return SourceHealthCode.PAGINATION_LOOP
    if int(metadata.get("rejected_record_count") or 0) > 0 or int(
        metadata.get("malformed_records") or 0
    ) > 0:
        return SourceHealthCode.MALFORMED_RECORDS
    # A validation harness stopping before the next request is not evidence
    # about the source.  Keep any continuation/total diagnostics, but classify
    # the outcome by the artificial safety bound that actually stopped it.
    if reason == "canary_budget_reached":
        return SourceHealthCode.SAFETY_LIMIT_REACHED
    if metadata.get("has_more") is True or metadata.get("next_url_present") is True or metadata.get(
        "next_cursor_present"
    ) is True:
        return SourceHealthCode.TERMINAL_CONTRADICTION

    total = metadata.get("total_available")
    if metadata.get("authoritative_total_present") and _valid_total(total):
        if jobs_collected != int(total):
            return SourceHealthCode.AUTHORITATIVE_TOTAL_NOT_REACHED

    if not metadata.get("scan_complete"):
        return None
    if jobs_collected == 0 and not _valid_empty_evidence(metadata):
        return SourceHealthCode.UNPROVEN_EMPTY
    if not _has_terminal_evidence(metadata, jobs_collected):
        return SourceHealthCode.PAGINATION_UNPROVEN
    if metadata.get("safety_limit_reached") and not _independent_terminal_evidence(
        metadata, jobs_collected
    ):
        return SourceHealthCode.SAFETY_LIMIT_REACHED
    return None


def _has_terminal_evidence(metadata: MutableMapping[str, Any], jobs_collected: int) -> bool:
    evidence_type = str(metadata.get("terminal_evidence_type") or "").strip()
    if not evidence_type:
        return False
    if evidence_type == "authoritative_total":
        total = metadata.get("total_available")
        return _valid_total(total) and int(total) == jobs_collected
    return bool(
        metadata.get("terminal_page_observed")
        or metadata.get("complete_collection_response")
        or metadata.get("has_more") is False
        or metadata.get("next_url_present") is False
        or metadata.get("next_cursor_present") is False
        or metadata.get("disabled_next_control")
        or metadata.get("explicit_final_page")
    )


def _independent_terminal_evidence(
    metadata: MutableMapping[str, Any], jobs_collected: int
) -> bool:
    return _has_terminal_evidence(metadata, jobs_collected) and not any(
        (
            metadata.get("pagination_evidence_malformed"),
            metadata.get("pagination_evidence_conflicting"),
            metadata.get("collection_changed_during_scan"),
        )
    )


def _valid_empty_evidence(metadata: MutableMapping[str, Any]) -> bool:
    total = metadata.get("total_available")
    return bool(
        (metadata.get("authoritative_total_present") and total == 0)
        or metadata.get("complete_collection_response")
        or (
            metadata.get("terminal_page_observed")
            and str(metadata.get("terminal_evidence_type") or "").strip()
        )
    )


def _incomplete_health_code(
    metadata: MutableMapping[str, Any], jobs_collected: int
) -> SourceHealthCode:
    reason = " ".join(
        str(metadata.get(field) or "")
        for field in ("stop_reason", "scan_completeness_reason")
    ).casefold()
    if any(token in reason for token in _LOOP_REASONS):
        return SourceHealthCode.PAGINATION_LOOP
    if "migration" in reason:
        return SourceHealthCode.SOURCE_MIGRATION_SUSPECTED
    if "stale" in reason or re.search(r"\b(?:404|410)\b", reason):
        return SourceHealthCode.SOURCE_STALE
    if "block" in reason or "auth" in reason or "login" in reason:
        return SourceHealthCode.AUTHENTICATION_OR_BLOCK
    if "identity" in reason:
        return SourceHealthCode.IDENTITY_FAILURE
    if "safety" in reason or "maximum_" in reason or "max_" in reason:
        return SourceHealthCode.SAFETY_LIMIT_REACHED
    if jobs_collected == 0:
        return SourceHealthCode.UNPROVEN_EMPTY
    return SourceHealthCode.PAGINATION_UNPROVEN


def _failure_health_code(value: str) -> SourceHealthCode:
    lowered = str(value or "").casefold()
    if (
        "migration" in lowered
        or "official careers page did not link the configured" in lowered
        or "official careers page no longer links the configured" in lowered
    ):
        return SourceHealthCode.SOURCE_MIGRATION_SUSPECTED
    if "stale" in lowered or "unresolved" in lowered or re.search(r"\b(?:404|410)\b", lowered):
        return SourceHealthCode.SOURCE_STALE
    if "block" in lowered or "auth" in lowered or "login" in lowered or "captcha" in lowered:
        return SourceHealthCode.AUTHENTICATION_OR_BLOCK
    if "identity" in lowered or "mismatch" in lowered:
        return SourceHealthCode.IDENTITY_FAILURE
    if "malformed" in lowered or "schema" in lowered or "unsupported" in lowered:
        return SourceHealthCode.MALFORMED_COLLECTION
    if "unexpected_upstream_http" in lowered or re.search(r"\bhttp 202\b", lowered):
        return SourceHealthCode.TEMPORARY_UNEXPECTED_RESPONSE
    return SourceHealthCode.SOURCE_UNREACHABLE


def _valid_total(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class SourceResponseAssessment:
    accepted: bool
    health_code: SourceHealthCode
    original_url: str
    final_url: str
    final_host: str
    redirect_count: int
    host_remained_approved: bool
    reason: str


def record_source_response_assessment(
    metadata: MutableMapping[str, Any], assessment: SourceResponseAssessment
) -> None:
    """Persist bounded redirect/identity evidence in the canonical scan row."""
    metadata.update(
        original_configured_url=assessment.original_url,
        final_url=assessment.final_url,
        final_host=assessment.final_host,
        redirect_count=assessment.redirect_count,
        host_remained_approved=assessment.host_remained_approved,
        source_identity_status="validated" if assessment.accepted else "failed",
        source_response_health_code=assessment.health_code.value,
        source_response_reason=assessment.reason,
    )


def assess_source_response(
    *,
    configured_url: str,
    final_url: str,
    status_code: int,
    allowed_hosts: Sequence[str],
    body_prefix: str = "",
    redirect_count: int = 0,
    required_markers: Sequence[str] = (),
) -> SourceResponseAssessment:
    """Classify a bounded HTTP response without trusting redirects or HTTP 200."""
    original = urlparse(str(configured_url or ""))
    final = urlparse(str(final_url or configured_url or ""))
    final_host = (final.hostname or "").casefold()
    approved = {
        str(host).strip().casefold() for host in allowed_hosts if str(host).strip()
    }
    if original.hostname:
        approved.add(original.hostname.casefold())
    host_ok = bool(final_host) and any(
        final_host == host or final_host.endswith("." + host) for host in approved
    )
    lowered = str(body_prefix or "")[:20_000].casefold()

    def result(accepted: bool, code: SourceHealthCode, reason: str) -> SourceResponseAssessment:
        return SourceResponseAssessment(
            accepted=accepted,
            health_code=code,
            original_url=str(configured_url or ""),
            final_url=str(final_url or configured_url or ""),
            final_host=final_host,
            redirect_count=max(0, int(redirect_count)),
            host_remained_approved=host_ok,
            reason=reason,
        )

    if status_code in {404, 410}:
        return result(False, SourceHealthCode.SOURCE_STALE, f"HTTP {status_code}")
    if status_code in {401, 403, 407, 429, 451}:
        return result(False, SourceHealthCode.AUTHENTICATION_OR_BLOCK, f"HTTP {status_code}")
    if status_code == 202:
        return result(
            False,
            SourceHealthCode.TEMPORARY_UNEXPECTED_RESPONSE,
            "HTTP 202 did not return the reviewed synchronous collection contract",
        )
    if status_code < 200 or status_code >= 300:
        return result(False, SourceHealthCode.SOURCE_UNREACHABLE, f"HTTP {status_code}")
    if not host_ok:
        return result(
            False,
            SourceHealthCode.SOURCE_MIGRATION_SUSPECTED,
            "redirect left the approved source-host contract",
        )
    if redirect_count and final.path.rstrip("/") in {"", "/home", "/careers"} and original.path.rstrip("/") not in {
        "",
        "/home",
        "/careers",
    }:
        return result(False, SourceHealthCode.SOURCE_STALE, "redirect ended at a generic landing page")
    if any(
        marker in lowered
        for marker in (
            "sign in to continue",
            "log in to continue",
            "access denied",
            "captcha",
            "cf-chl-",
            "verify you are human",
        )
    ):
        return result(False, SourceHealthCode.AUTHENTICATION_OR_BLOCK, "login or block page replaced the collection")
    missing = [marker for marker in required_markers if marker.casefold() not in lowered]
    if missing:
        return result(False, SourceHealthCode.IDENTITY_FAILURE, "required source identity marker is absent")
    if not lowered.strip() and not required_markers:
        return result(False, SourceHealthCode.UNPROVEN_EMPTY, "HTTP 200 did not prove a collection")
    return result(True, SourceHealthCode.HEALTHY, "response remained inside the reviewed source contract")
