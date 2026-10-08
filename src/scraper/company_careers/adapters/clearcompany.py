"""ClearCompany public career-site API adapter."""

from __future__ import annotations

import re
import time
from typing import Any, List, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion


PAGE_SIZE = 20
MAX_PAGES = 2
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SITE_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    """Fetch a bounded ClearCompany collection with exact brand attribution."""
    validate_target(target)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    max_pages = max(1, int(target.max_pages))
    if max_pages > MAX_PAGES:
        raise AdapterError(f"ClearCompany max_pages cannot exceed {MAX_PAGES}")

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    endpoint = f"https://careers-api.clearcompany.com/v1/{target.site_id}"
    metadata = SourceScanMetadata(
        endpoint_variant="public_total_count_api",
        page_size=PAGE_SIZE,
        maximum_pages=max_pages,
        maximum_results=int(target.max_jobs),
    )
    listing_budget = AdaptiveListingCompletion(
        max_pages, int(target.max_jobs), allow_listing_only_second_stage=True
    )
    listing_budget.record(metadata)
    publish_source_scan_metadata(session, metadata)

    for page_index in range(listing_budget.hard_request_ceiling):
        metadata["pages_requested"] += 1
        response = _get_page(session, endpoint, page_index, timeout)
        payload = _parse_page(response, page_index)
        metadata["pages_completed"] += 1
        total = payload["totalCount"]
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise AdapterError("ClearCompany total job count changed during pagination")
        listing_budget.record_decision(
            metadata,
            total,
            PAGE_SIZE,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=page_index * PAGE_SIZE + len(payload["results"]),
        )
        effective_jobs = int(metadata["effective_listing_job_ceiling"])

        for item in payload["results"]:
            job = _normalize(item, target)
            if job.job_id in seen_ids:
                raise AdapterError("ClearCompany returned a duplicate stable job ID")
            seen_ids.add(job.job_id)
            jobs.append(job)
            if len(jobs) >= effective_jobs:
                break

        metadata["total_available"] = total
        if len(jobs) == total:
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            metadata["stop_reason"] = "reported_total_reached"
            break
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if len(payload["results"]) < PAGE_SIZE:
            metadata["stop_reason"] = "short_page_before_reported_total"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("ClearCompany returned a short page before totalCount")
        if page_index + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
    if not metadata["scan_complete"] and metadata["stop_reason"] == "not_started":
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    return jobs


def validate_target(target: CompanyTarget) -> None:
    if not target.careers_url or not target.site_id:
        raise AdapterError("ClearCompany target needs careers_url and site_id")
    careers = urlparse(str(target.careers_url))
    if careers.scheme != "https" or not careers.hostname:
        raise AdapterError("ClearCompany careers_url must be an anonymous HTTPS URL")
    if not SITE_ID_RE.fullmatch(str(target.site_id)):
        raise AdapterError("ClearCompany site_id must be a UUID")


def _get_page(
    session: requests.Session,
    endpoint: str,
    page_index: int,
    timeout: int,
) -> Any:
    params = {"pageIndex": page_index, "pageSize": PAGE_SIZE, "source": ""}
    headers = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(endpoint, params=params, headers=headers, timeout=timeout)
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code in {401, 407}:
            raise AdapterError(f"ClearCompany authentication required (HTTP {response.status_code})")
        if response.status_code in {403, 429, 451}:
            raise AdapterError(f"ClearCompany blocked response (HTTP {response.status_code})")
        if response.status_code != 200:
            raise AdapterError(f"ClearCompany HTTP {response.status_code}")
        return response
    raise AdapterError("ClearCompany request attempts exhausted")


def _parse_page(response: Any, page_index: int) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"ClearCompany returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("ClearCompany response must be a JSON object")
    results = payload.get("results")
    if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
        raise AdapterError("ClearCompany response is missing a job-object results list")
    if len(results) > PAGE_SIZE:
        raise AdapterError("ClearCompany page exceeded the configured result limit")
    if payload.get("currentPageIndex") != page_index:
        raise AdapterError("ClearCompany response page index does not match the request")
    if payload.get("currentPageCount") != len(results):
        raise AdapterError("ClearCompany current page count does not match its results")
    total = payload.get("totalCount")
    if not isinstance(total, int) or total < 0:
        raise AdapterError("ClearCompany totalCount must be a non-negative integer")
    if total > page_index * PAGE_SIZE and not results:
        raise AdapterError("ClearCompany response ended before its reported total job count")
    return payload


def _normalize(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    job_id = str(item.get("id") or "").strip()
    title = str(item.get("positionTitle") or "").strip()
    brand = str(item.get("brandName") or "").strip()
    apply_url = str(item.get("applyLink") or "").strip()
    if not SITE_ID_RE.fullmatch(job_id) or not title:
        raise AdapterError("ClearCompany job is missing a stable UUID or title")
    if brand.casefold() != target.name.casefold():
        raise AdapterError("ClearCompany job has mixed or ambiguous company identity")
    parsed = urlparse(apply_url)
    if parsed.scheme != "https" or not parsed.hostname or job_id.casefold() not in parsed.path.casefold():
        raise AdapterError("ClearCompany job is missing a job-specific HTTPS apply URL")
    location = str(item.get("location") or "").strip()
    department = str(item.get("departmentName") or "").strip()
    office = str(item.get("officeName") or "").strip()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=apply_url,
        updated_at=str(item.get("postedDate") or item.get("openDate") or ""),
        source=f"clearcompany:{target.site_id}",
        departments=[department] if department else [],
        offices=[office] if office else ([location] if location else []),
        description=str(item.get("description") or "") or None,
        raw_metadata={"platform": "clearcompany", "brand_name": brand},
    )
