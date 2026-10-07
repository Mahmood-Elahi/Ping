"""Shared request-budget policy for cheap authoritative listing pagination."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
import re
from typing import Any, Mapping, MutableMapping, Optional


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
    *,
    evidence: PaginationEvidence | None = None,
    prior_pagination: bool = False,
) -> bool:
    """Let authoritative completion win when it coincides with a safety cap."""
    if not _valid_total(total_available):
        return False
    _validate_progress(0, results_observed)
    assert total_available is not None
    if results_observed != total_available:
        return False
    if evidence is not None and not pagination_terminal_accepted(
        metadata,
        evidence,
        prior_pagination=prior_pagination,
        additional_terminal_reason="reported_total_reached",
    ):
        return False
    if metadata.get("pagination_evidence_malformed") or metadata.get(
        "pagination_evidence_conflicting"
    ):
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
    disabled_next = root.xpath(
        "//*[(contains(translate(@class, 'NEXTLOADMOR', 'nextloadmor'), 'next') "
        "or contains(translate(@class, 'NEXTLOADMOR', 'nextloadmor'), 'load-more') "
        "or contains(translate(@aria-label, 'NEXTLOADMOR', 'nextloadmor'), 'next') "
        "or contains(translate(@aria-label, 'NEXTLOADMOR', 'nextloadmor'), 'load more') "
        "or normalize-space(translate(string(.), 'NEXTLOADMOR', 'nextloadmor'))='next' "
        "or normalize-space(translate(string(.), 'NEXTLOADMOR', 'nextloadmor'))='load more') "
        "and (@disabled or @aria-disabled='true' "
        "or contains(concat(' ', translate(normalize-space(@class), "
        "'DISABLED', 'disabled'), ' '), ' disabled '))]"
    )
    if disabled_next:
        return False
    next_links = root.xpath(
        "//a[@href and not(@aria-disabled='true') and "
        "not(contains(concat(' ', translate(normalize-space(@class), "
        "'DISABLED', 'disabled'), ' '), ' disabled ')) and "
        "(translate(@rel, 'NEXT', 'next')='next' "
        "or contains(concat(' ', translate(normalize-space(@class), "
        "'NEXT', 'next'), ' '), ' next ') "
        "or contains(translate(@class, 'LOADMORE', 'loadmore'), 'load-more') "
        "or contains(translate(@aria-label, 'NEXT', 'next'), 'next') "
        "or contains(translate(@aria-label, 'LOADMORE', 'loadmore'), 'load more') "
        "or normalize-space(translate(string(.), 'NEXT', 'next'))='next' "
        "or normalize-space(translate(string(.), 'LOADMORE', 'loadmore'))='load more')]"
    )
    if next_links:
        return True
    pagination_nodes = root.xpath(
        "//*[contains(translate(@class, 'PAGINATION', 'pagination'), 'pagination') "
        "or contains(translate(@class, 'PAGING', 'paging'), 'paging')]"
    )
    text = " ".join(
        " ".join(" ".join(node.itertext()).split()) for node in pagination_nodes
    )
    match = re.search(
        r"(?i)(?<!\w)page\s+(\d+)\s*(?:of|/)\s*(\d+)(?!\w)", text
    )
    if match:
        return int(match.group(1)) < int(match.group(2))
    return None


@dataclass(frozen=True)
class PaginationEvidence:
    """Normalized pagination fields evaluated as one fail-closed observation."""

    reported_total: int | None = None
    has_more: bool | None = None
    next_url_present: bool | None = None
    next_cursor_present: bool | None = None
    next_page: int | None = None
    page_count: int | None = None
    current_page: int | None = None
    result_count: int | None = None
    end_index: int | None = None
    disabled_next_control: bool = False
    explicit_final_page: bool = False
    fields: tuple[str, ...] = ()
    malformed_fields: tuple[str, ...] = ()
    conflicting_fields: tuple[str, ...] = ()
    field_diagnostics: tuple[tuple[str, str, Any], ...] = ()

    def continuation_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.has_more is True:
            reasons.append("has_more_true")
        if self.next_url_present is True:
            reasons.append("active_next_url")
        if self.next_cursor_present is True:
            reasons.append("next_cursor_present")
        if self.next_page is not None:
            reasons.append("next_page_present")
        if (
            self.current_page is not None
            and self.page_count is not None
            and self.current_page < self.page_count
        ):
            reasons.append("current_page_below_final_page")
        if (
            self.end_index is not None
            and self.reported_total is not None
            and self.end_index < self.reported_total
        ):
            reasons.append("result_range_below_total")
        return tuple(reasons)

    def terminal_reasons(self, *, prior_pagination: bool = False) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.has_more is False:
            reasons.append("has_more_false")
        if self.disabled_next_control:
            reasons.append("disabled_next_control")
        if self.next_url_present is False:
            reasons.append("next_url_absent")
        if prior_pagination and self.next_cursor_present is False:
            reasons.append("next_cursor_absent_after_pagination")
        if self.explicit_final_page:
            reasons.append("explicit_final_page")
        if (
            self.reported_total is not None
            and self.end_index is not None
            and self.end_index == self.reported_total
        ):
            reasons.append("result_range_reached_total")
        return tuple(reasons)

    def status(
        self,
        *,
        prior_pagination: bool = False,
        additional_terminal_reason: str | None = None,
    ) -> str:
        if self.malformed_fields:
            return "malformed"
        terminal = list(self.terminal_reasons(prior_pagination=prior_pagination))
        if additional_terminal_reason:
            terminal.append(additional_terminal_reason)
        continuation = self.continuation_reasons()
        if self.conflicting_fields or (terminal and continuation):
            return "conflicting"
        if continuation:
            return "continuation"
        if terminal:
            return "terminal"
        return "unproven"

    def terminal(self, *, prior_pagination: bool = False) -> bool:
        return self.status(prior_pagination=prior_pagination) == "terminal"


def html_pagination_evidence(root: Any) -> PaginationEvidence:
    """Extract only conventional, source-visible HTML terminal contracts."""
    has_more = html_page_has_more(root)
    pagination_nodes = root.xpath(
        "//*[contains(translate(@class, 'RESULTPAGINGSUMMARY', "
        "'resultpagingsummary'), 'pagination') "
        "or contains(translate(@class, 'RESULTPAGINGSUMMARY', "
        "'resultpagingsummary'), 'paging') "
        "or contains(translate(@class, 'RESULTPAGINGSUMMARY', "
        "'resultpagingsummary'), 'result-count') "
        "or contains(translate(@class, 'RESULTPAGINGSUMMARY', "
        "'resultpagingsummary'), 'results-count') "
        "or contains(translate(@class, 'RESULTPAGINGSUMMARY', "
        "'resultpagingsummary'), 'job-count') "
        "or contains(translate(@class, 'RESULTPAGINGSUMMARY', "
        "'resultpagingsummary'), 'summary')]"
    )
    text = " ".join(
        " ".join(" ".join(node.itertext()).split()) for node in pagination_nodes
    )
    page_match = re.search(
        r"(?i)(?<!\w)page\s+(\d+)\s*(?:of|/)\s*(\d+)(?!\w)", text
    )
    range_match = re.search(
        r"(?i)(?<!\d)(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s+of\s+"
        r"(\d[\d,]*)\s*(?:matching\s+)?(?:jobs?|results?)?(?!\w)",
        text,
    )
    standalone_total_matches = re.findall(
        r"(?i)(?<![\d-])(\d[\d,]*)\s+(?:matching\s+)?(?:jobs?|results?)\b"
        r"(?!\s+per\s+page\b)",
        text,
    )

    def integer(value: Any) -> int | None:
        cleaned = str(value or "").strip().replace(",", "")
        return int(cleaned) if cleaned.isdigit() else None

    totals: list[tuple[str, int]] = []
    fields: list[str] = []
    malformed: list[str] = []
    conflicts: list[str] = []
    for name in ("data-total", "data-total-count", "data-result-count", "data-results-count"):
        values = [node.get(name) for node in pagination_nodes if node.get(name) is not None]
        for value in values:
            parsed = integer(value)
            if parsed is None:
                malformed.append(name)
            else:
                totals.append((name, parsed))
        if values:
            fields.append(name)
    end_index = None
    if range_match:
        end_index = integer(range_match.group(2))
        range_total = integer(range_match.group(3))
        if range_total is not None:
            totals.append(("result_range", range_total))
        fields.append("result_range")
    for value in standalone_total_matches:
        parsed = integer(value)
        if parsed is not None:
            totals.append(("visible_total", parsed))
    if standalone_total_matches or range_match:
        if standalone_total_matches:
            fields.append("visible_total")
    elif re.search(r"(?i)\b(?:showing\s+)?\S+\s*[-â€“]\s*\S+\s+of\s+\S+", text):
        malformed.append("result_range")
    unique_totals = {value for _name, value in totals}
    total = next(iter(unique_totals)) if len(unique_totals) == 1 else None
    if len(unique_totals) > 1:
        conflicts.extend(name for name, _value in totals)
    current_page = integer(page_match.group(1)) if page_match else None
    page_count = integer(page_match.group(2)) if page_match else None
    if page_match:
        fields.append("page_count")
    if has_more is not None:
        fields.append("next_control")
    active_next = bool(
        root.xpath(
            "//a[@href and not(@aria-disabled='true') and "
            "not(contains(concat(' ', translate(normalize-space(@class), "
            "'DISABLED', 'disabled'), ' '), ' disabled ')) and "
            "(translate(@rel, 'NEXT', 'next')='next' or "
            "contains(concat(' ', translate(normalize-space(@class), 'NEXT', 'next'), ' '), ' next ') or "
            "contains(translate(@class, 'LOADMORE', 'loadmore'), 'load-more') or "
            "contains(translate(@aria-label, 'NEXT', 'next'), 'next') or "
            "contains(translate(@aria-label, 'LOADMORE', 'loadmore'), 'load more') or "
            "normalize-space(translate(string(.), 'NEXT', 'next'))='next' or "
            "normalize-space(translate(string(.), 'LOADMORE', 'loadmore'))='load more')]"
        )
    )
    disabled = bool(
        root.xpath(
            "//*[@disabled or @aria-disabled='true' or "
            "contains(concat(' ', translate(normalize-space(@class), "
            "'DISABLED', 'disabled'), ' '), ' disabled ')]"
            "[contains(translate(@class, 'NEXTLOADMOR', 'nextloadmor'), 'next') or "
            "contains(translate(@class, 'NEXTLOADMOR', 'nextloadmor'), 'load-more') or "
            "contains(translate(@aria-label, 'NEXTLOADMOR', 'nextloadmor'), 'next') or "
            "contains(translate(@aria-label, 'NEXTLOADMOR', 'nextloadmor'), 'load more') or "
            "normalize-space(translate(string(.), 'NEXTLOADMOR', 'nextloadmor'))='next' or "
            "normalize-space(translate(string(.), 'NEXTLOADMOR', 'nextloadmor'))='load more']"
        )
    )
    if active_next:
        has_more = True
    elif disabled:
        has_more = False
    next_url_present = True if active_next else None
    if current_page is not None and page_count is not None:
        if page_count < 1 or current_page < 1 or current_page > page_count:
            malformed.append("page_count")
    if end_index is not None and total is not None and end_index > total:
        malformed.append("result_range")
    return PaginationEvidence(
        reported_total=total,
        has_more=has_more,
        next_url_present=next_url_present,
        page_count=page_count,
        current_page=current_page,
        end_index=end_index,
        disabled_next_control=disabled,
        explicit_final_page=bool(
            current_page is not None
            and page_count is not None
            and current_page >= page_count
        ),
        fields=tuple(fields),
        malformed_fields=tuple(dict.fromkeys(malformed)),
        conflicting_fields=tuple(dict.fromkeys(conflicts)),
    )


_JSON_PAGINATION_FIELDS: Mapping[str, tuple[str, ...]] = {
    "reported_total": ("total", "totalCount", "total_count", "reportedTotal"),
    "has_more": ("hasMore", "has_more", "hasNextPage", "has_next_page"),
    "next_url": ("next", "nextPage", "next_page", "nextUrl", "next_url"),
    "next_cursor": ("nextCursor", "next_cursor"),
    "next_page": (),
    "page_count": ("totalPages", "total_pages", "pageCount", "page_count", "lastPage", "last_page"),
    "current_page": ("currentPage", "current_page", "page", "pageNumber", "page_number"),
    "result_count": ("resultCount", "result_count"),
    "end_index": ("endIndex", "end_index"),
}


def json_pagination_evidence(
    payload: Mapping[str, Any],
    *,
    container_paths: tuple[str, ...] = ("", "pagination", "pageInfo", "page_info", "paging"),
    field_map: Mapping[str, tuple[str, ...]] | None = None,
    numeric_page_index_contract: bool = False,
    expected_current_page: int | None = None,
    expected_page_count: int | None = None,
) -> PaginationEvidence:
    """Read pagination only from explicitly reviewed containers and fields.

    The default contract permits response-root fields and direct, conventionally
    named pagination objects. It deliberately does not recurse into arbitrary
    result, facet, aggregation, analytics, or filter objects.
    """
    configured_fields = dict(_JSON_PAGINATION_FIELDS)
    if numeric_page_index_contract:
        configured_fields["next_url"] = tuple(
            name
            for name in configured_fields["next_url"]
            if name not in {"nextPage", "next_page"}
        )
        configured_fields["next_page"] = ("nextPage", "next_page")
    if field_map:
        for field_name, names in field_map.items():
            if field_name not in configured_fields:
                raise ValueError(f"unsupported pagination field: {field_name}")
            configured_fields[field_name] = tuple(names)

    containers: list[tuple[str, Mapping[str, Any]]] = []
    for path in container_paths:
        current: Any = payload
        if path:
            for component in path.split("."):
                if not isinstance(current, Mapping) or component not in current:
                    current = None
                    break
                current = current[component]
        if isinstance(current, Mapping):
            containers.append((path or "$", current))

    candidates: dict[str, list[tuple[str, Any]]] = {
        name: [] for name in configured_fields
    }
    for path, container in containers:
        for normalized, names in configured_fields.items():
            for name in names:
                if name in container:
                    candidates[normalized].append((f"{path}.{name}", container[name]))

    fields = [name for name, values in candidates.items() if values]
    malformed: list[str] = []
    conflicts: list[str] = []
    field_diagnostics: list[tuple[str, str, Any]] = []

    def sanitized(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:80]
        if isinstance(value, (list, tuple, set, Mapping)):
            return f"<{type(value).__name__} length={len(value)}>"
        return f"<{type(value).__name__}>"

    for values in candidates.values():
        for source, value in values:
            field_diagnostics.append(
                (source, type(value).__name__, sanitized(value))
            )

    def integer(field_name: str) -> int | None:
        values = candidates[field_name]
        parsed: list[int] = []
        for source, value in values:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                parsed.append(value)
            else:
                malformed.append(source)
        if len(set(parsed)) > 1:
            conflicts.extend(source for source, _value in values)
            return None
        return parsed[0] if parsed else None

    def scalar(field_name: str, expected: type) -> Any:
        values = candidates[field_name]
        parsed: list[Any] = []
        for source, value in values:
            if isinstance(value, expected):
                parsed.append(value)
            else:
                malformed.append(source)
        if len({repr(value) for value in parsed}) > 1:
            conflicts.extend(source for source, _value in values)
            return None
        return parsed[0] if parsed else None

    reported_total = integer("reported_total")
    page_count = integer("page_count")
    current_page = integer("current_page")
    result_count = integer("result_count")
    end_index = integer("end_index")
    has_more = scalar("has_more", bool)

    def optional_integer(field_name: str) -> int | None:
        values = candidates[field_name]
        parsed: list[int | None] = []
        for source, value in values:
            if value is None:
                parsed.append(None)
            elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                parsed.append(value)
            else:
                malformed.append(source)
        if len({repr(value) for value in parsed}) > 1:
            conflicts.extend(source for source, _value in values)
            return None
        return parsed[0] if parsed else None

    next_page = optional_integer("next_page")

    def presence(field_name: str, *, cursor: bool = False) -> bool | None:
        values = candidates[field_name]
        parsed: list[bool] = []
        for source, value in values:
            if value is None or value == "":
                parsed.append(False)
            elif isinstance(value, str):
                parsed.append(True)
            elif cursor and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                parsed.append(True)
            else:
                malformed.append(source)
        if len(set(parsed)) > 1:
            conflicts.extend(source for source, _value in values)
            return None
        return parsed[0] if parsed else None

    next_url_present = presence("next_url")
    next_cursor_present = presence("next_cursor", cursor=True)
    if page_count is not None and current_page is not None:
        if page_count < 1 or current_page < 1 or current_page > page_count:
            malformed.append("page_count_relationship")
    if reported_total is not None and end_index is not None and end_index > reported_total:
        malformed.append("result_range_relationship")
    if numeric_page_index_contract:
        for required in ("current_page", "page_count", "next_page"):
            if not candidates[required]:
                malformed.append(f"missing_{required}")
        if expected_current_page is not None and current_page != expected_current_page:
            malformed.append("current_page_sequence")
        if expected_page_count is not None and page_count != expected_page_count:
            conflicts.append("page_count_changed")
        if current_page is not None and page_count is not None:
            if current_page < page_count and next_page != current_page + 1:
                malformed.append("next_page_relationship")
            elif current_page == page_count and next_page is not None:
                malformed.append("terminal_next_page_relationship")
    return PaginationEvidence(
        reported_total=reported_total,
        has_more=has_more,
        next_url_present=next_url_present,
        next_cursor_present=next_cursor_present,
        next_page=next_page,
        page_count=page_count,
        current_page=current_page,
        result_count=result_count,
        end_index=end_index,
        explicit_final_page=bool(
            page_count is not None
            and current_page is not None
            and current_page == page_count
        ),
        fields=tuple(fields),
        malformed_fields=tuple(dict.fromkeys(malformed)),
        conflicting_fields=tuple(dict.fromkeys(conflicts)),
        field_diagnostics=tuple(field_diagnostics),
    )


def publish_pagination_evidence(
    metadata: MutableMapping[str, Any],
    evidence: PaginationEvidence,
    *,
    prior_pagination: bool = False,
) -> None:
    """Persist normalized terminal fields for coverage and offline analysis."""
    metadata.update(
        reported_total=evidence.reported_total,
        has_more=evidence.has_more,
        next_url_present=evidence.next_url_present,
        next_cursor_present=evidence.next_cursor_present,
        next_page=evidence.next_page,
        page_count=evidence.page_count,
        result_count=evidence.result_count,
        end_index=evidence.end_index,
        disabled_next_control=evidence.disabled_next_control,
        explicit_final_page=evidence.explicit_final_page,
        terminal_contract_fields=list(evidence.fields),
        pagination_continuation_reasons=list(evidence.continuation_reasons()),
        pagination_terminal_reasons=list(
            evidence.terminal_reasons(prior_pagination=prior_pagination)
        ),
        pagination_malformed_fields=list(evidence.malformed_fields),
        pagination_conflicting_fields=list(evidence.conflicting_fields),
        pagination_field_diagnostics=[
            {"field": field, "type": value_type, "value": value}
            for field, value_type, value in evidence.field_diagnostics
        ],
    )
    _record_pagination_status(
        metadata,
        evidence,
        prior_pagination=prior_pagination,
    )


def pagination_terminal_accepted(
    metadata: MutableMapping[str, Any],
    evidence: PaginationEvidence,
    *,
    prior_pagination: bool = False,
    additional_terminal_reason: str | None = None,
) -> bool:
    """Evaluate an adapter-specific terminal hint against all observed signals."""
    return _record_pagination_status(
        metadata,
        evidence,
        prior_pagination=prior_pagination,
        additional_terminal_reason=additional_terminal_reason,
    ) == "terminal"


def _record_pagination_status(
    metadata: MutableMapping[str, Any],
    evidence: PaginationEvidence,
    *,
    prior_pagination: bool,
    additional_terminal_reason: str | None = None,
) -> str:
    status = evidence.status(
        prior_pagination=prior_pagination,
        additional_terminal_reason=additional_terminal_reason,
    )
    metadata["pagination_evidence_status"] = status
    if status in {"conflicting", "malformed"}:
        durable_key = f"pagination_evidence_{status}"
        metadata[durable_key] = True
        diagnostics = metadata.setdefault("pagination_evidence_diagnostics", [])
        diagnostic = {
            "status": status,
            "fields": list(evidence.fields),
            "continuation": list(evidence.continuation_reasons()),
            "terminal": list(
                evidence.terminal_reasons(prior_pagination=prior_pagination)
            ) + ([additional_terminal_reason] if additional_terminal_reason else []),
            "malformed": list(evidence.malformed_fields),
            "conflicting": list(evidence.conflicting_fields),
            "field_diagnostics": [
                {"field": field, "type": value_type, "value": value}
                for field, value_type, value in evidence.field_diagnostics
            ],
        }
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
    return status


def enforce_pagination_fail_closed(metadata: MutableMapping[str, Any]) -> bool:
    """Prevent later adapter bookkeeping from erasing a durable bad observation."""
    if metadata.get("pagination_evidence_malformed"):
        reason = "malformed_pagination_evidence"
    elif metadata.get("pagination_evidence_conflicting"):
        reason = "conflicting_pagination_evidence"
    else:
        return False
    metadata["scan_complete"] = False
    metadata["scan_completeness_reason"] = reason
    metadata["stop_reason"] = reason
    metadata["terminal_page_observed"] = False
    return True


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
        """Use the existing hard ceiling when an explicit EOF contract exists."""
        return self.hard_request_ceiling

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
