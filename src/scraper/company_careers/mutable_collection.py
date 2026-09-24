"""Shared evidence helpers for paginating live, mutable job collections."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qsl, urlparse

from src.scraper.company_careers.models import JobRecord, SourceScanMetadata


COLLECTION_CHANGED_REASON = "collection_changed_during_scan"


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
