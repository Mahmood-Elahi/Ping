"""Shared evidence helpers for paginating live, mutable job collections."""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlparse

from src.scraper.company_careers.models import (
    AdapterResult,
    JobRecord,
    SourceScanMetadata,
    source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence


COLLECTION_CHANGED_REASON = "collection_changed_during_scan"
MAX_COLLECTION_RESTARTS = 1


def fetch_with_mutation_restart(
    session: Any,
    fetch_once: Callable[[], list[JobRecord]],
) -> AdapterResult:
    """Return one internally consistent traversal after at most one restart.

    The first traversal is never unioned with the replacement traversal.  A
    replacement can prove completeness only from its own pagination evidence.
    The first traversal's ID set is retained solely as bounded diagnostics.
    """
    first_jobs = fetch_once()
    first_metadata = source_scan_metadata(session)
    if not first_metadata.get("collection_changed_during_scan"):
        return AdapterResult(jobs=first_jobs, scan=first_metadata)

    first_ids = {job.job_id for job in first_jobs}
    first_requests = int(first_metadata.get("pages_requested") or 0)
    first_pages = int(first_metadata.get("pages_completed") or 0)
    second_jobs = fetch_once()
    metadata = source_scan_metadata(session)
    second_ids = {job.job_id for job in second_jobs}
    metadata.update(
        collection_restart_count=MAX_COLLECTION_RESTARTS,
        first_traversal_jobs=len(first_jobs),
        first_traversal_pages=first_pages,
        stable_id_set_match_across_restart=first_ids == second_ids,
        stable_ids_added_after_restart=len(second_ids - first_ids),
        stable_ids_removed_after_restart=len(first_ids - second_ids),
        total_pages_requested_across_traversals=(
            first_requests + int(metadata.get("pages_requested") or 0)
        ),
    )
    if metadata.get("collection_changed_during_scan"):
        metadata["collection_retry_exhausted"] = True
        metadata["scan_complete"] = False
        metadata["scan_completeness_reason"] = COLLECTION_CHANGED_REASON
        metadata["stop_reason"] = COLLECTION_CHANGED_REASON
    elif metadata.get("scan_complete"):
        metadata["stabilized_after_collection_retry"] = True
    return AdapterResult(jobs=second_jobs, scan=metadata)


def initialize_mutable_collection(metadata: SourceScanMetadata) -> None:
    """Add deterministic total/overlap evidence without replacing adapter fields."""
    metadata.setdefault("initial_total_available", None)
    metadata.setdefault("minimum_total_observed", None)
    metadata.setdefault("maximum_total_observed", None)
    metadata.setdefault("final_total_observed", None)
    metadata.setdefault("final_total_available", None)
    metadata.setdefault("observed_total_counts", [])
    metadata.setdefault("total_count_changed", False)
    metadata.setdefault("collection_changed_during_scan", False)
    metadata.setdefault("overlapping_jobs_skipped", 0)
    metadata.setdefault("diagnostics", [])
    metadata.setdefault("warnings", [])


def record_total_observation(
    metadata: SourceScanMetadata,
    total: Optional[int],
    *,
    offset: int,
    source_label: str,
) -> bool:
    """Record a valid total hint and return whether it changed from the prior hint."""
    initialize_mutable_collection(metadata)
    if total is None:
        return False

    metadata["authoritative_total_present"] = True

    previous = metadata["final_total_observed"]
    metadata["observed_total_counts"].append(total)
    if metadata["initial_total_available"] is None:
        metadata["initial_total_available"] = total
    metadata["minimum_total_observed"] = (
        total
        if metadata["minimum_total_observed"] is None
        else min(int(metadata["minimum_total_observed"]), total)
    )
    metadata["maximum_total_observed"] = (
        total
        if metadata["maximum_total_observed"] is None
        else max(int(metadata["maximum_total_observed"]), total)
    )
    changed = previous is not None and total != previous
    if changed:
        message = f"{source_label} total job count changed during pagination: {previous} -> {total}"
        metadata["total_count_changed"] = True
        metadata["warnings"].append(message)
        metadata["diagnostics"].append(
            {
                "offset": offset,
                "level": "warning",
                "warning": message,
                "previous_total_available": previous,
                "total_available": total,
            }
        )
        mark_collection_changed(metadata)
    metadata["final_total_observed"] = total
    metadata["final_total_available"] = total
    # Compatibility alias: this is the latest valid hint, not a snapshot promise.
    metadata["total_available"] = total
    return changed


def mark_collection_changed(
    metadata: SourceScanMetadata,
    *,
    offset: Optional[int] = None,
    overlap_count: int = 0,
) -> None:
    """Record evidence that the traversal cannot represent one complete snapshot."""
    initialize_mutable_collection(metadata)
    metadata["collection_changed_during_scan"] = True
    metadata["scan_complete"] = False
    metadata["scan_completeness_reason"] = COLLECTION_CHANGED_REASON
    if overlap_count:
        metadata["overlapping_jobs_skipped"] += overlap_count
        metadata["diagnostics"].append(
            {
                "offset": offset,
                "overlapping_jobs_skipped": overlap_count,
            }
        )


def finish_terminal(
    metadata: SourceScanMetadata,
    *,
    complete_reason: str,
    stop_reason: Optional[str] = None,
) -> None:
    """Publish terminal evidence without claiming completeness after mutation."""
    terminal = stop_reason or complete_reason
    record_terminal_evidence(
        metadata,
        "terminal_page",
        terminal,
        has_more=False,
        terminal_page_observed=True,
    )
    if metadata.get("collection_changed_during_scan"):
        metadata["terminal_evidence"] = terminal
        metadata["scan_complete"] = False
        metadata["scan_completeness_reason"] = COLLECTION_CHANGED_REASON
        metadata["stop_reason"] = COLLECTION_CHANGED_REASON
        return
    metadata["scan_complete"] = True
    metadata["scan_completeness_reason"] = complete_reason
    metadata["stop_reason"] = terminal


def finish_changed(metadata: SourceScanMetadata, *, terminal_evidence: str) -> None:
    """Retain useful jobs while explicitly blocking absence reconciliation."""
    mark_collection_changed(metadata)
    metadata["terminal_evidence"] = terminal_evidence
    metadata["stop_reason"] = COLLECTION_CHANGED_REASON


def immutable_job_fingerprint(job: JobRecord) -> tuple[Any, ...]:
    """Fingerprint identity-bearing fields while ignoring mutable presentation data."""
    parsed = urlparse(job.url)
    return (
        job.job_id.casefold(),
        job.source.casefold(),
        (parsed.hostname or "").casefold(),
        parsed.path.rstrip("/").casefold(),
        tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
    )
