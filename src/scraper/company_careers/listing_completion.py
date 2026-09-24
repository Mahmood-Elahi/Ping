"""Shared request-budget policy for cheap authoritative listing pagination."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
import re
from typing import Any, MutableMapping, Optional


ADAPTIVE_LISTING_REQUEST_ALLOWANCE = 10
MAX_LISTING_REQUESTS_PER_SOURCE = 1_000
MAX_RAW_JOBS_PER_SOURCE = 20_000
# Backward-compatible names retained for adapters that record the shared
# listing policy in their scan metadata.
ABSOLUTE_LISTING_REQUEST_CEILING = MAX_LISTING_REQUESTS_PER_SOURCE
ABSOLUTE_LISTING_JOB_CEILING = MAX_RAW_JOBS_PER_SOURCE
# The second stage may consume the remainder of the existing absolute request
# ceiling. The absolute ceiling is still a total-per-source limit, not an
# additional-request allowance.
LISTING_ONLY_SECOND_STAGE_REQUEST_ALLOWANCE = ABSOLUTE_LISTING_REQUEST_CEILING


def record_terminal_evidence(
    metadata: MutableMapping[str, Any],
    evidence_type: str,
    evidence_value: Any,
    *,
    has_more: Optional[bool] = None,
    next_cursor_present: Optional[bool] = None,
    terminal_page_observed: bool = False,
    complete_collection_response: bool = False,
) -> None:
    """Publish adapter-neutral terminal evidence without inventing a total."""
    if not str(evidence_type).strip():
        raise ValueError("terminal evidence type cannot be empty")
    if has_more is not None and not isinstance(has_more, bool):
        raise ValueError("has_more must be boolean or None")
    if next_cursor_present is not None and not isinstance(
        next_cursor_present, bool
    ):
        raise ValueError("next_cursor_present must be boolean or None")
    metadata["terminal_evidence_type"] = str(evidence_type)
    metadata["terminal_evidence_value"] = evidence_value
    if has_more is not None:
        metadata["has_more"] = has_more
    if next_cursor_present is not None:
        metadata["next_cursor_present"] = next_cursor_present
    metadata["terminal_page_observed"] = bool(terminal_page_observed)
    metadata["complete_collection_response"] = bool(
        complete_collection_response
    )


def finish_authoritative_total(
    metadata: MutableMapping[str, Any],
    total_available: int | None,
    results_observed: int,
) -> bool:
    """Let authoritative completion win when it coincides with a safety cap."""
    if not _valid_total(total_available):
        return False
    _validate_progress(0, results_observed)
    assert total_available is not None
    if results_observed != total_available:
        return False
    metadata.update(
        scan_complete=True,
        scan_completeness_reason="reported_total_reached",
        total_available=total_available,
        stop_reason="reported_total_reached",
        safety_limit_reached=False,
    )
    record_terminal_evidence(
        metadata,
        "authoritative_total",
        total_available,
        has_more=False,
        terminal_page_observed=True,
    )
    return True


def html_page_has_more(root: Any) -> Optional[bool]:
    """Read conservative next/terminal state from conventional HTML controls."""
    next_links = root.xpath(
        "//a[@href and (translate(@rel, 'NEXT', 'next')='next' "
        "or contains(translate(@class, 'NEXT', 'next'), 'next') "
        "or contains(translate(@aria-label, 'NEXT', 'next'), 'next') "
        "or normalize-space(translate(string(.), 'NEXT', 'next'))='next')]"
    )
    if next_links:
        return True
    disabled_next = root.xpath(
        "//*[contains(translate(@class, 'NEXTDISABLED', 'nextdisabled'), 'next') "
        "and (contains(translate(@class, 'NEXTDISABLED', 'nextdisabled'), 'disabled') "
        "or @aria-disabled='true')]"
    )
    if disabled_next:
        return False
    pagination_nodes = root.xpath(
        "//*[contains(translate(@class, 'PAGINATION', 'pagination'), 'pagination') "
        "or contains(translate(@class, 'PAGING', 'paging'), 'paging')]"
    )
    text = " ".join(" ".join(node.text_content().split()) for node in pagination_nodes)
    match = re.search(
        r"(?i)(?<!\w)page\s+(\d+)\s*(?:of|/)\s*(\d+)(?!\w)", text
    )
    if match:
        return int(match.group(1)) < int(match.group(2))
    return None


@dataclass
class PageSizeObservation:
    """Track server-selected listing capacity independently of the request.

    ``authoritative_size`` is for response metadata that explicitly reports the
    accepted page size.  Otherwise non-terminal observed rows are the best
    available capacity signal.  The smallest usable capacity is retained so a
    fluctuating source never makes the completion projection optimistic.
    """

    requested_size: int
    observed_size: int = 0
    effective_size: int = 0
    was_clamped: bool = False
    was_expanded: bool = False
    stable_across_pages: bool = True
    observations: int = 0
    _capacity_observations: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _validate_page_size(self.requested_size, "requested page size")
        if not self.effective_size:
            self.effective_size = self.requested_size

    def observe(
        self,
        observed_size: int,
        *,
        authoritative_size: Optional[int] = None,
        terminal: bool = False,
    ) -> int:
        """Record one response and return the conservative effective capacity."""
        if (
            isinstance(observed_size, bool)
            or not isinstance(observed_size, int)
            or observed_size < 0
        ):
            raise ValueError("observed page size must be a non-negative integer")
        if authoritative_size is not None:
            _validate_page_size(authoritative_size, "authoritative page size")
            if observed_size > authoritative_size:
                raise ValueError(
                    "observed page size exceeds the authoritative server page size"
                )

        self.observed_size = observed_size
        self.observations += 1
        capacity = authoritative_size
        if capacity is None and observed_size and (
            not terminal or not self._capacity_observations
        ):
            capacity = observed_size
        if capacity is not None:
            previous = self._capacity_observations
            if previous and capacity != previous[-1]:
                self.stable_across_pages = False
            self._capacity_observations = (*previous, capacity)
            self.effective_size = min(self._capacity_observations)

        comparison = authoritative_size
        if comparison is None and (
            not terminal or observed_size > self.requested_size
        ):
            comparison = observed_size
        if comparison is not None and comparison > 0:
            self.was_clamped = self.was_clamped or comparison < self.requested_size
            self.was_expanded = self.was_expanded or comparison > self.requested_size
        return self.effective_size

    def record(self, metadata: MutableMapping[str, Any]) -> None:
        metadata.update(
            requested_page_size=self.requested_size,
            observed_page_size=self.observed_size,
            effective_server_page_size=self.effective_size,
            page_size_was_clamped=self.was_clamped,
            page_size_was_expanded=self.was_expanded,
            page_size_stable_across_pages=self.stable_across_pages,
        )


@dataclass(frozen=True)
class AdaptiveListingCompletion:
    """Three-level budget: configured normal, adaptive, and absolute safety."""

    normal_request_budget: int
    normal_job_budget: int
    adaptive_request_allowance: int = ADAPTIVE_LISTING_REQUEST_ALLOWANCE
    hard_request_ceiling: int = ABSOLUTE_LISTING_REQUEST_CEILING
    hard_job_ceiling: int = ABSOLUTE_LISTING_JOB_CEILING
    allow_listing_only_second_stage: bool = False
    listing_only_second_stage_request_allowance: int = (
        LISTING_ONLY_SECOND_STAGE_REQUEST_ALLOWANCE
    )

    def __post_init__(self) -> None:
        values = (
            self.normal_request_budget,
            self.normal_job_budget,
            self.hard_request_ceiling,
            self.hard_job_ceiling,
        )
        if any(isinstance(value, bool) or int(value) < 1 for value in values):
            raise ValueError("listing budgets and ceilings must be positive integers")
        if self.adaptive_request_allowance < 0:
            raise ValueError("adaptive listing allowance cannot be negative")
        if self.listing_only_second_stage_request_allowance < 0:
            raise ValueError("second-stage listing allowance cannot be negative")
        if self.normal_request_budget > self.hard_request_ceiling:
            raise ValueError("normal listing budget exceeds the hard request ceiling")
        if self.normal_job_budget > self.hard_job_ceiling:
            raise ValueError("normal job budget exceeds the hard job ceiling")

    def required_requests(self, total_available: int, page_size: int) -> int:
        _validate_total_and_page_size(total_available, page_size)
        return ceil(total_available / page_size) if total_available else 0

    def projected_requests(
        self,
        total_available: int,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
    ) -> int:
        """Project requests from actual listing progress, not nominal page capacity."""
        _validate_total_and_page_size(total_available, page_size)
        _validate_progress(requests_completed, results_observed)
        remaining_results = max(0, total_available - results_observed)
        remaining_requests = (
            ceil(remaining_results / page_size) if remaining_results else 0
        )
        return requests_completed + remaining_requests

    def can_adapt(
        self,
        total_available: int | None,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
    ) -> bool:
        if not _valid_total(total_available) or page_size < 1:
            return False
        assert total_available is not None
        _validate_progress(requests_completed, results_observed)
        projected_requests = self.projected_requests(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
        )
        adaptive_requests = max(
            0, projected_requests - self.normal_request_budget
        )
        return (
            total_available <= self.hard_job_ceiling
            and projected_requests <= self.hard_request_ceiling
            and adaptive_requests <= self.adaptive_request_allowance
        )

    def can_use_listing_only_second_stage(
        self,
        total_available: int | None,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
        collection_changed: bool = False,
        detail_requests_coupled: bool = False,
    ) -> bool:
        """Allow cheap deterministic listings to reach the existing hard ceiling.

        The caller opts in only for an authoritative, deterministic listing
        endpoint. Mutation and per-job detail traffic always fail closed.
        """
        if (
            not self.allow_listing_only_second_stage
            or collection_changed
            or detail_requests_coupled
            or not _valid_total(total_available)
            or page_size < 1
        ):
            return False
        assert total_available is not None
        projected_requests = self.projected_requests(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
        )
        return (
            total_available <= self.hard_job_ceiling
            and projected_requests <= self.hard_request_ceiling
            and max(0, projected_requests - self.normal_request_budget)
            <= self.listing_only_second_stage_request_allowance
        )

    def can_continue(
        self,
        total_available: int | None,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
        collection_changed: bool = False,
        detail_requests_coupled: bool = False,
    ) -> bool:
        return self.can_adapt(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
        ) or self.can_use_listing_only_second_stage(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
            collection_changed=collection_changed,
            detail_requests_coupled=detail_requests_coupled,
        )

    def request_ceiling(
        self,
        total_available: int | None,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
        collection_changed: bool = False,
        detail_requests_coupled: bool = False,
    ) -> int:
        if self.can_continue(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
            collection_changed=collection_changed,
            detail_requests_coupled=detail_requests_coupled,
        ):
            assert total_available is not None
            return max(
                self.normal_request_budget,
                self.projected_requests(
                    total_available,
                    page_size,
                    requests_completed=requests_completed,
                    results_observed=results_observed,
                ),
            )
        return self.normal_request_budget

    def job_ceiling(
        self,
        total_available: int | None,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
        collection_changed: bool = False,
        detail_requests_coupled: bool = False,
    ) -> int:
        if self.can_continue(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
            collection_changed=collection_changed,
            detail_requests_coupled=detail_requests_coupled,
        ):
            assert total_available is not None
            return max(self.normal_job_budget, total_available)
        return self.normal_job_budget

    def complete_collection_job_ceiling(self, total_available: int) -> int:
        """Allow already-downloaded complete collections without more requests."""
        if not _valid_total(total_available):
            return self.normal_job_budget
        return (
            max(self.normal_job_budget, total_available)
            if total_available <= self.hard_job_ceiling
            else self.normal_job_budget
        )

    def record(self, metadata: MutableMapping[str, Any]) -> None:
        metadata.update(
            normal_listing_request_budget=self.normal_request_budget,
            normal_listing_job_budget=self.normal_job_budget,
            adaptive_listing_request_allowance=self.adaptive_request_allowance,
            absolute_listing_request_ceiling=self.hard_request_ceiling,
            absolute_listing_job_ceiling=self.hard_job_ceiling,
            listing_only_second_stage_enabled=self.allow_listing_only_second_stage,
            listing_only_second_stage_request_allowance=(
                self.listing_only_second_stage_request_allowance
            ),
        )

    def record_decision(
        self,
        metadata: MutableMapping[str, Any],
        total_available: int | None,
        page_size: int,
        *,
        requests_completed: int = 0,
        results_observed: int = 0,
        detail_requests_coupled: bool = False,
    ) -> None:
        collection_changed = bool(metadata.get("collection_changed_during_scan"))
        first_stage_allowed = self.can_adapt(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
        )
        second_stage_allowed = self.can_use_listing_only_second_stage(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
            collection_changed=collection_changed,
            detail_requests_coupled=detail_requests_coupled,
        )
        metadata["adaptive_completion_eligible"] = first_stage_allowed
        metadata["listing_only_second_stage_eligible"] = second_stage_allowed
        metadata["detail_requests_coupled"] = detail_requests_coupled
        metadata["effective_listing_request_ceiling"] = self.request_ceiling(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
            collection_changed=collection_changed,
            detail_requests_coupled=detail_requests_coupled,
        )
        metadata["effective_listing_job_ceiling"] = self.job_ceiling(
            total_available,
            page_size,
            requests_completed=requests_completed,
            results_observed=results_observed,
            collection_changed=collection_changed,
            detail_requests_coupled=detail_requests_coupled,
        )
        metadata["listing_requests_completed_at_decision"] = requests_completed
        metadata["listing_results_observed_at_decision"] = results_observed
        if _valid_total(total_available):
            assert total_available is not None
            metadata["remaining_listing_results"] = max(
                0, total_available - results_observed
            )
            metadata["terminal_listing_requests_required"] = self.projected_requests(
                total_available,
                page_size,
                requests_completed=requests_completed,
                results_observed=results_observed,
            )

    def deterministic_terminal_request_ceiling(self) -> int:
        """Bound explicit next-page scans whose total is knowable only at EOF."""
        return min(
            self.hard_request_ceiling,
            self.normal_request_budget + self.adaptive_request_allowance,
        )

    def record_terminal_probe_decision(
        self,
        metadata: MutableMapping[str, Any],
        page_size: int,
        *,
        requests_completed: int,
        results_observed: int,
        deterministic_terminal_contract: bool,
    ) -> None:
        """Record bounded EOF probing for sources without authoritative totals."""
        _validate_page_size(page_size, "page size")
        _validate_progress(requests_completed, results_observed)
        request_ceiling = self.normal_request_budget
        if deterministic_terminal_contract:
            request_ceiling = self.deterministic_terminal_request_ceiling()
        remaining_requests = max(0, request_ceiling - requests_completed)
        job_ceiling = min(
            self.hard_job_ceiling,
            max(
                self.normal_job_budget,
                results_observed + remaining_requests * page_size,
            ),
        )
        metadata["deterministic_terminal_contract"] = bool(
            deterministic_terminal_contract
        )
        metadata["effective_listing_request_ceiling"] = request_ceiling
        metadata["effective_listing_job_ceiling"] = job_ceiling


def _valid_total(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_total_and_page_size(total_available: int, page_size: int) -> None:
    if not _valid_total(total_available):
        raise ValueError("authoritative total must be a non-negative integer")
    _validate_page_size(page_size, "page size")


def _validate_page_size(page_size: int, label: str) -> None:
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError(f"{label} must be a positive integer")


def _validate_progress(requests_completed: int, results_observed: int) -> None:
    for label, value in (
        ("requests completed", requests_completed),
        ("results observed", results_observed),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
