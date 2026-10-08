"""Amazon Jobs adapter."""

from __future__ import annotations

from dataclasses import replace
from typing import List

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    enforce_pagination_fail_closed,
    json_pagination_evidence,
    pagination_terminal_accepted,
    publish_pagination_evidence,
    record_terminal_evidence,
)


HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_SEARCH_TERMS = [
    "Principal Engineer",
    "Senior Principal Engineer",
    "Principal Software Engineer",
    "Senior SDE",
    "Principal SDE",
]


@explicit_adapter_result
def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    base_url = "https://www.amazon.jobs/en/search.json"
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    by_id: dict[str, JobRecord] = {}
    metadata = SourceScanMetadata(
        endpoint_variant="search_terms_offset_v1",
        maximum_pages=max(1, target.max_pages),
    )
    publish_source_scan_metadata(session, metadata)
    normal_request_budget = max(1, target.max_pages) * max(1, len(search_terms))
    try:
        listing_budget = AdaptiveListingCompletion(
            normal_request_budget,
            max(1, int(target.max_jobs)),
            allow_listing_only_second_stage=True,
        )
    except ValueError as exc:
        raise AdapterError(f"Amazon search-term budget is invalid: {exc}") from exc
    listing_budget.record(metadata)
    term_states: list[dict[str, object]] = []
    query_totals: dict[str, int] = {}
    raw_results_observed = 0

    for term in search_terms:
        raw_term_count = 0
        observed_total: int | None = None
        term_state: dict[str, object] = {
            "term": term,
            "pages_requested": 0,
            "pages_completed": 0,
            "rows_collected": 0,
            "reported_total": None,
            "status": "unproven",
            "stop_reason": "not_started",
            "safety_limit_reached": False,
            "pagination_evidence_conflicting": False,
            "pagination_evidence_malformed": False,
        }
        term_states.append(term_state)
        term_seen_ids: set[str] = set()
        term_page_signatures: set[tuple[str, ...]] = set()
        deterministic_terminal_contract = False
        for page in range(listing_budget.hard_request_ceiling):
            if page >= max(1, target.max_pages) and not deterministic_terminal_contract:
                term_state["safety_limit_reached"] = True
                term_state["stop_reason"] = "maximum_pages_reached"
                break
            if (
                int(metadata["pages_requested"])
                >= int(
                    metadata.get(
                        "effective_listing_request_ceiling",
                        listing_budget.normal_request_budget,
                    )
                )
                or raw_results_observed
                >= int(
                    metadata.get(
                        "effective_listing_job_ceiling",
                        listing_budget.normal_job_budget,
                    )
                )
            ):
                term_state["safety_limit_reached"] = True
                term_state["stop_reason"] = (
                    "maximum_results_reached"
                    if raw_results_observed
                    >= int(
                        metadata.get(
                            "effective_listing_job_ceiling",
                            listing_budget.normal_job_budget,
                        )
                    )
                    else "maximum_pages_reached"
                )
                break
            offset = page * 100
            metadata["pages_requested"] += 1
            term_state["pages_requested"] = int(term_state["pages_requested"]) + 1
            response = session.get(
                base_url,
                params={
                    "base_query": term,
                    "result_limit": 100,
                    "offset": offset,
                    "sort": "recent",
                },
                headers=HEADERS,
                timeout=timeout,
            )
            if response.status_code != 200:
                raise AdapterError(f"Amazon HTTP {response.status_code}")

            payload = response.json()
            if not isinstance(payload, dict):
                raise AdapterError("Amazon response is missing its search result object")
            evidence = json_pagination_evidence(
                payload,
                container_paths=("",),
                field_map={"reported_total": ("hits", "total")},
            )
            items = payload.get("jobs")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise AdapterError("Amazon jobs must be a list of job objects")
            if len(items) > 100:
                raise AdapterError("Amazon jobs exceeded the requested page size")
            projected_term_count = raw_term_count + len(items)
            if evidence.reported_total is not None:
                if observed_total is not None and evidence.reported_total != observed_total:
                    evidence = replace(
                        evidence,
                        reported_total=None,
                        conflicting_fields=tuple(
                            dict.fromkeys(
                                (*evidence.conflicting_fields, "reported_total_changed")
                            )
                        ),
                    )
                else:
                    observed_total = evidence.reported_total
                    query_totals[term] = evidence.reported_total
                    term_state["reported_total"] = evidence.reported_total
                evidence = replace(evidence, end_index=projected_term_count)
                if (
                    not evidence.conflicting_fields
                    and sum(query_totals.values()) <= listing_budget.hard_job_ceiling
                ):
                    deterministic_terminal_contract = True
                    listing_budget.record_terminal_probe_decision(
                        metadata,
                        100,
                        requests_completed=int(metadata["pages_completed"]) + 1,
                        results_observed=raw_results_observed + len(items),
                        deterministic_terminal_contract=True,
                    )
            publish_pagination_evidence(
                term_state, evidence, prior_pagination=page > 0
            )
            publish_pagination_evidence(metadata, evidence, prior_pagination=page > 0)
            metadata["pages_completed"] += 1
            term_state["pages_completed"] = int(term_state["pages_completed"]) + 1
            if raw_results_observed + len(items) > int(
                metadata.get(
                    "effective_listing_job_ceiling",
                    listing_budget.normal_job_budget,
                )
            ):
                term_state["safety_limit_reached"] = True
                term_state["stop_reason"] = "maximum_results_reached"
                break
            if not items:
                terminal = pagination_terminal_accepted(
                    term_state,
                    evidence,
                    prior_pagination=page > 0,
                    additional_terminal_reason="empty_page",
                )
                term_state["status"] = term_state.get(
                    "pagination_evidence_status", "unproven"
                )
                term_state["stop_reason"] = (
                    "empty_page" if terminal else "pagination_evidence_not_terminal"
                )
                break
            raw_term_count += len(items)
            raw_results_observed += len(items)
            term_state["rows_collected"] = raw_term_count

            signature = tuple(
                str(item.get("id", "") or item.get("id_icims", ""))
                for item in items
            )
            if signature in term_page_signatures:
                raise AdapterError("Amazon pagination repeated a page for one search term")
            term_page_signatures.add(signature)
            prior_term_ids = set(term_seen_ids)

            for item in items:
                job_id = str(item.get("id", "") or item.get("id_icims", ""))
                if not job_id:
                    raise AdapterError("Amazon returned a job without a stable ID")
                if job_id in prior_term_ids:
                    raise AdapterError(
                        "Amazon pagination repeated a stable ID for one search term"
                    )
                term_seen_ids.add(job_id)
                location = item.get("normalized_location", "") or item.get("location", "")
                job = JobRecord(
                        job_id=job_id,
                        title=item.get("title", ""),
                        company=target.name,
                        location=location,
                        url="https://www.amazon.jobs" + item.get("job_path", ""),
                        updated_at=item.get("posted_date", "") or item.get("updated_time", ""),
                        source="custom:amazon",
                        departments=[item.get("business_category", "")]
                        if item.get("business_category")
                        else [],
                        offices=[location] if location else [],
                        description=item.get("description"),
                    )
                if job_id in seen:
                    existing = by_id[job_id]
                    if (
                        existing.title != job.title
                        or existing.url != job.url
                        or existing.location != job.location
                        or existing.updated_at != job.updated_at
                    ):
                        raise AdapterError("Amazon returned a conflicting duplicate stable ID")
                    continue
                seen.add(job_id)
                by_id[job_id] = job
                jobs.append(job)
            metadata["jobs_normalized"] = len(jobs)

            if len(items) < 100:
                terminal = pagination_terminal_accepted(
                    term_state,
                    evidence,
                    prior_pagination=page > 0,
                    additional_terminal_reason="short_page",
                )
                term_state["status"] = term_state.get(
                    "pagination_evidence_status", "unproven"
                )
                term_state["stop_reason"] = (
                    "short_page" if terminal else "pagination_evidence_not_terminal"
                )
                break
            if (
                evidence.reported_total is not None
                and raw_term_count >= evidence.reported_total
            ):
                terminal = pagination_terminal_accepted(
                    term_state,
                    evidence,
                    prior_pagination=page > 0,
                    additional_terminal_reason="reported_total_reached",
                )
                term_state["status"] = term_state.get(
                    "pagination_evidence_status", "unproven"
                )
                if terminal:
                    term_state["stop_reason"] = "reported_total_reached"
                    break
            if pagination_terminal_accepted(
                term_state, evidence, prior_pagination=page > 0
            ):
                term_state["status"] = "terminal"
                term_state["stop_reason"] = "explicit_terminal_evidence"
                break
            term_state["status"] = term_state.get(
                "pagination_evidence_status", "unproven"
            )
        else:
            term_state["safety_limit_reached"] = True
            term_state["stop_reason"] = "maximum_pages_reached"

        if term_state.get("status") != "terminal" and term_state.get(
            "stop_reason"
        ) == "not_started":
            term_state["stop_reason"] = "pagination_evidence_not_terminal"

        if term_state.get("pagination_evidence_malformed"):
            metadata["pagination_evidence_malformed"] = True
        if term_state.get("pagination_evidence_conflicting"):
            metadata["pagination_evidence_conflicting"] = True

    all_terms_terminal = bool(term_states) and all(
        state.get("status") == "terminal"
        and not state.get("pagination_evidence_conflicting")
        and not state.get("pagination_evidence_malformed")
        and not state.get("safety_limit_reached")
        for state in term_states
    )
    safety_limited = any(
        bool(state.get("safety_limit_reached")) for state in term_states
    )
    malformed_fields = list(
        dict.fromkeys(
            field
            for state in term_states
            for field in state.get("pagination_malformed_fields", [])
        )
    )
    conflicting_fields = list(
        dict.fromkeys(
            field
            for state in term_states
            for field in state.get("pagination_conflicting_fields", [])
        )
    )
    metadata["pagination_malformed_fields"] = malformed_fields
    metadata["pagination_conflicting_fields"] = conflicting_fields
    metadata["pagination_evidence_status"] = (
        "malformed"
        if metadata.get("pagination_evidence_malformed")
        else "conflicting"
        if metadata.get("pagination_evidence_conflicting")
        else "terminal"
        if all_terms_terminal
        else "continuation"
        if safety_limited
        or any(state.get("status") == "continuation" for state in term_states)
        else "unproven"
    )

    metadata["total_available"] = len(jobs) if all_terms_terminal else None
    metadata["scan_complete"] = all_terms_terminal
    metadata["scan_completeness_reason"] = (
        "all_search_terms_terminal"
        if all_terms_terminal
        else "safety_limit_reached"
        if safety_limited
        else "search_term_pagination_incomplete"
    )
    metadata["stop_reason"] = (
        "all_search_terms_terminal"
        if all_terms_terminal
        else "maximum_pages_reached"
        if safety_limited
        else "search_term_pagination_incomplete"
    )
    metadata["safety_limit_reached"] = safety_limited
    metadata["query_reported_totals"] = query_totals
    metadata["query_pagination"] = term_states
    metadata["raw_results_observed"] = raw_results_observed
    record_terminal_evidence(
        metadata,
        "search_terms_terminal" if all_terms_terminal else "search_terms_incomplete",
        [
            {
                "term": state["term"],
                "status": state["status"],
                "stop_reason": state["stop_reason"],
            }
            for state in term_states
        ],
        has_more=not all_terms_terminal,
        terminal_page_observed=all_terms_terminal,
    )
    enforce_pagination_fail_closed(metadata)
    return jobs
