"""BambooHR public careers API adapter."""

from __future__ import annotations

import re
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    AdapterResult,
    CompanyTarget,
    InventoryItem,
    ItemFailure,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    record_terminal_evidence,
)


MAX_JOBS = 20
MAX_DETAIL_REQUESTS = 20
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$", re.IGNORECASE)
ID_RE = re.compile(r"^[1-9][0-9]*$")


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> AdapterResult:
    """Fetch one bounded anonymous BambooHR collection and its public details."""
    contract = validate_target(target)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    metadata = SourceScanMetadata(
        endpoint_variant="complete_public_collection",
        maximum_pages=1,
        maximum_results=int(target.max_jobs),
        pages_requested=1,
        stop_reason="not_started",
    )
    setattr(session, "bamboohr_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    payload = _json_get(session, contract["listing_url"], timeout)
    all_items = _parse_listing(payload)
    metadata["pages_completed"] = 1
    metadata["source_identity_status"] = "validated"
    metadata["total_available"] = len(all_items)
    metadata["detail_requests_coupled"] = False
    record_terminal_evidence(
        metadata,
        "complete_collection_response",
        len(all_items),
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    listing_budget = AdaptiveListingCompletion(1, int(target.max_jobs))
    listing_budget.record(metadata)
    effective_jobs = listing_budget.complete_collection_job_ceiling(len(all_items))
    items = all_items[:effective_jobs]
    jobs: list[JobRecord] = []
    item_failures: list[ItemFailure] = []
    seen_ids: set[str] = set()
    detail_requests = 0
    for item_index, item in enumerate(items):
        job_id = str(item.get("id") or "").strip()
        if not ID_RE.fullmatch(job_id) or job_id in seen_ids:
            raise AdapterError("BambooHR returned a missing or duplicate stable job ID")
        seen_ids.add(job_id)
        if item_index < min(int(target.max_jobs), MAX_DETAIL_REQUESTS):
            detail_url = f"{contract['careers_url']}/{job_id}/detail"
            try:
                detail = _parse_detail(
                    _json_get(session, detail_url, timeout), item, contract, job_id
                )
                detail_requests += 1
                jobs.append(_normalize(item, detail, target, contract, job_id))
            except (AdapterError, requests.RequestException) as exc:
                reason = str(exc)
                deterministic = any(
                    marker in reason.casefold()
                    for marker in (
                        "does not match",
                        "identity changed",
                        "not an open job",
                        "exact job-specific share url",
                    )
                )
                item_failures.append(
                    ItemFailure(
                        item_id=job_id,
                        reason=f"{exc.__class__.__name__}: {exc}",
                        stage="normalization" if deterministic else "hydration",
                        retryable=(
                            isinstance(exc, requests.RequestException)
                            or any(
                                token in reason.casefold()
                                for token in ("http 408", "http 429", "http 5")
                            )
                        ),
                        outcome="rejected" if deterministic else "unreadable",
                    )
                )
                jobs.append(_normalize_summary(item, target, contract, job_id))
        else:
            jobs.append(_normalize_summary(item, target, contract, job_id))
    metadata["jobs_normalized"] = len(jobs)
    metadata["detail_requests"] = detail_requests
    metadata["maximum_detail_requests"] = MAX_DETAIL_REQUESTS
    metadata["zero_job_contract"] = not all_items
    if len(all_items) > effective_jobs:
        metadata["stop_reason"] = "maximum_results_reached"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    else:
        metadata["stop_reason"] = "complete_collection_response"
        metadata["scan_completeness_reason"] = "complete_collection_response"
        metadata["scan_complete"] = True
    metadata["unreadable_items"] = sum(
        failure.outcome == "unreadable" for failure in item_failures
    )
    metadata["rejected_items"] = sum(
        failure.outcome == "rejected" for failure in item_failures
    )
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=tuple(
            InventoryItem(job_id, f"bamboohr:{contract['slug']}")
            for job_id in sorted(seen_ids)
        ),
        item_failures=tuple(item_failures),
        capabilities_used=frozenset(
            {"inventory", "hydration", "partial_progress", "per_item_failure"}
        ),
    )


def _normalize_summary(
    summary: Mapping[str, Any],
    target: CompanyTarget,
    contract: Mapping[str, str],
    job_id: str,
) -> JobRecord:
    """Normalize the complete public list; details are optional enrichment."""
    title = str(summary.get("jobOpeningName") or "").strip()
    if not title:
        raise AdapterError("BambooHR listing job is missing its title")
    location = _location(summary.get("atsLocation")) or _location(
        summary.get("location")
    )
    department = str(summary.get("departmentLabel") or "").strip()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=f"{contract['careers_url']}/{job_id}",
        updated_at=str(summary.get("datePosted") or "").strip(),
        source=f"bamboohr:{contract['slug']}",
        departments=[department] if department else [],
        offices=[location] if location else [],
        description=str(summary.get("description") or "").strip() or None,
        raw_metadata={"platform": "bamboohr"},
    )


def validate_target(target: CompanyTarget) -> dict[str, str]:
    slug = str(target.slug or "").strip().casefold()
    if not SLUG_RE.fullmatch(slug):
        raise AdapterError("BambooHR target needs a safe tenant slug")
    if int(target.max_pages) != 1:
        raise AdapterError("BambooHR supports exactly one bounded collection page")
    if not 1 <= int(target.max_jobs) <= MAX_JOBS:
        raise AdapterError(f"BambooHR max_jobs cannot exceed {MAX_JOBS}")
    expected_listing = f"https://{slug}.bamboohr.com/careers/list"
    if str(target.listing_url or "").rstrip("/") != expected_listing:
        raise AdapterError("BambooHR listing_url does not match the configured tenant")
    return {
        "slug": slug,
        "host": f"{slug}.bamboohr.com",
        "careers_url": f"https://{slug}.bamboohr.com/careers",
        "listing_url": expected_listing,
    }


def _json_get(session: requests.Session, url: str, timeout: int) -> Mapping[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        status = getattr(response, "status_code", None)
        if status in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if status in {401, 407}:
            raise AdapterError(f"BambooHR authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"BambooHR blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"BambooHR HTTP {status}")
        content = getattr(response, "content", b"")
        if content and len(content) > MAX_RESPONSE_BYTES:
            raise AdapterError("BambooHR response exceeded the byte limit")
        try:
            payload = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise AdapterError(f"BambooHR returned invalid JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise AdapterError("BambooHR response must be a JSON object")
        return payload
    raise AdapterError("BambooHR request attempts exhausted")


def _parse_listing(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    meta = payload.get("meta")
    items = payload.get("result")
    if not isinstance(meta, Mapping) or not isinstance(items, list):
        raise AdapterError("BambooHR listing is missing its public collection contract")
    if not all(isinstance(item, Mapping) for item in items):
        raise AdapterError("BambooHR listing contains a non-object job")
    total = meta.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != len(items):
        raise AdapterError("BambooHR totalCount does not match its result collection")
    return items


def _parse_detail(
    payload: Mapping[str, Any],
    summary: Mapping[str, Any],
    contract: Mapping[str, str],
    job_id: str,
) -> Mapping[str, Any]:
    result = payload.get("result")
    detail = result.get("jobOpening") if isinstance(result, Mapping) else None
    if not isinstance(detail, Mapping):
        raise AdapterError("BambooHR detail is missing its jobOpening object")
    expected_share = f"{contract['careers_url']}/{job_id}"
    share_url = str(detail.get("jobOpeningShareUrl") or "").rstrip("/")
    if share_url != expected_share:
        raise AdapterError("BambooHR job is missing its exact job-specific share URL")
    if str(detail.get("jobOpeningStatus") or "").casefold() != "open":
        raise AdapterError("BambooHR detail is not an open job")
    title = str(summary.get("jobOpeningName") or "").strip()
    if not title or str(detail.get("jobOpeningName") or "").strip() != title:
        raise AdapterError("BambooHR list/detail title identity changed")
    return detail


def _normalize(
    summary: Mapping[str, Any],
    detail: Mapping[str, Any],
    target: CompanyTarget,
    contract: Mapping[str, str],
    job_id: str,
) -> JobRecord:
    location = _location(detail.get("atsLocation") or summary.get("atsLocation"))
    if not location:
        location = _location(detail.get("location") or summary.get("location"))
    department = str(detail.get("departmentLabel") or summary.get("departmentLabel") or "").strip()
    share_url = f"{contract['careers_url']}/{job_id}"
    return JobRecord(
        job_id=job_id,
        title=str(detail["jobOpeningName"]).strip(),
        company=target.name,
        location=location,
        url=share_url,
        updated_at=str(detail.get("datePosted") or "").strip(),
        source=f"bamboohr:{contract['slug']}",
        departments=[department] if department else [],
        offices=[location] if location else [],
        description=str(detail.get("description") or "").strip() or None,
        raw_metadata={
            "platform": "bamboohr",
            "employment_status": str(detail.get("employmentStatusLabel") or "").strip(),
        },
    )


def _location(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    parts = [
        str(value.get("city") or "").strip(),
        str(value.get("state") or value.get("province") or "").strip(),
        str(value.get("country") or value.get("addressCountry") or "").strip(),
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part))
