"""StartDate/HireGround anonymous active-jobs adapter."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion


MAX_JOBS = 20
SOURCE_SPECIFIC_MAX_JOBS = {"tricanwellservice.startdate.ca": 50}
MAX_COLLECTION_ITEMS = 500
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.startdate\.ca$", re.IGNORECASE)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch a bounded tenant-scoped collection using the client's public Origin contract."""
    contract = validate_target(target)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    response = _get(session, contract["endpoint_url"], contract["origin"], timeout)
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"StartDate returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
        raise AdapterError("StartDate response must be a job-object list")
    if len(payload) > MAX_COLLECTION_ITEMS:
        raise AdapterError("StartDate response exceeded the collection item limit")
    if any(not _has_exact_attribution(item, str(target.job_attribution_regex)) for item in payload):
        raise AdapterError("StartDate collection has mixed or ambiguous per-job attribution")

    listing_budget = AdaptiveListingCompletion(1, int(target.max_jobs))
    effective_jobs = listing_budget.complete_collection_job_ceiling(len(payload))
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    for item in payload[:effective_jobs]:
        job = _normalize(item, target, contract)
        if job.job_id in seen_ids:
            raise AdapterError("StartDate returned a duplicate stable job ID")
        seen_ids.add(job.job_id)
        jobs.append(job)
    limited = len(payload) > effective_jobs
    metadata = SourceScanMetadata(
        scan_complete=not limited,
        scan_completeness_reason=(
            "safety_limit_reached" if limited else "complete_collection_response"
        ),
        total_available=len(payload),
        pages_requested=1,
        pages_completed=1,
        stop_reason=("maximum_results_reached" if limited else "complete_collection_response"),
        safety_limit_reached=limited,
        zero_job_contract=not payload,
    )
    setattr(session, "startdate_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget.record(metadata)
    return jobs


def validate_target(target: CompanyTarget) -> dict[str, str]:
    careers = urlparse(str(target.careers_url or ""))
    endpoint = urlparse(str(target.endpoint_url or ""))
    if (
        careers.scheme != "https"
        or not careers.hostname
        or not HOST_RE.fullmatch(careers.hostname)
        or careers.path not in {"", "/"}
        or careers.query
        or careers.fragment
    ):
        raise AdapterError("StartDate careers_url must be an anonymous tenant root")
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "api.startdate.ca"
        or endpoint.path != "/jobs/active"
        or parse_qs(endpoint.query) != {"page": ["candidate_dashboard"]}
        or endpoint.fragment
    ):
        raise AdapterError("StartDate endpoint_url is not the reviewed public active-jobs endpoint")
    if int(target.max_pages) != 1:
        raise AdapterError("StartDate supports exactly one bounded collection page")
    source_max_jobs = SOURCE_SPECIFIC_MAX_JOBS.get(
        careers.hostname.casefold(), MAX_JOBS
    )
    if not 1 <= int(target.max_jobs) <= source_max_jobs:
        raise AdapterError(
            f"StartDate max_jobs cannot exceed {source_max_jobs} for {target.name}"
        )
    if not target.job_attribution_regex:
        raise AdapterError("StartDate requires exact per-job attribution")
    try:
        re.compile(str(target.job_attribution_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"StartDate job_attribution_regex is invalid: {exc}") from exc
    origin = f"https://{careers.hostname.casefold()}"
    return {
        "origin": origin,
        "host": careers.hostname.casefold(),
        "endpoint_url": str(target.endpoint_url),
    }


def _get(session: requests.Session, url: str, origin: str, timeout: int) -> Any:
    headers = {"Accept": "application/json", "Origin": origin, "User-Agent": "job-radar/1.0"}
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
            raise AdapterError(f"StartDate authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"StartDate blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"StartDate HTTP {status}")
        content = getattr(response, "content", b"")
        if content and len(content) > MAX_RESPONSE_BYTES:
            raise AdapterError("StartDate response exceeded the byte limit")
        return response
    raise AdapterError("StartDate request attempts exhausted")


def _localized(value: Any, locale: str = "en") -> str:
    if isinstance(value, Mapping):
        return str(value.get(locale) or value.get("en") or "").strip()
    return str(value or "").strip()


def _has_exact_attribution(item: Mapping[str, Any], pattern: str) -> bool:
    divisions = item.get("category_division")
    if not isinstance(divisions, list) or not all(isinstance(value, Mapping) for value in divisions):
        return False
    text = " ".join(
        [_localized(item.get("summary")), _localized(item.get("description"))]
        + [_localized(value.get("name")) for value in divisions]
    )
    return bool(re.search(pattern, text, re.IGNORECASE))


def _normalize(
    item: Mapping[str, Any],
    target: CompanyTarget,
    contract: Mapping[str, str],
) -> JobRecord:
    job_id = str(item.get("id") or "").strip()
    title = _localized(item.get("name"), str(target.locale or "en"))
    if not job_id.isdigit() or int(job_id) <= 0 or not title:
        raise AdapterError("StartDate job is missing a stable numeric ID or title")
    if str(item.get("status") or "").casefold() != "active" or item.get("is_internal") is not False:
        raise AdapterError("StartDate collection contains a non-public active job")
    location_value = item.get("location")
    location = ""
    if isinstance(location_value, Mapping):
        location = ", ".join(
            value
            for value in (
                str(location_value.get("city") or "").strip(),
                str(location_value.get("province") or "").strip(),
            )
            if value
        )
    locale = str(target.locale or "en")
    apply_url = f"{contract['origin']}/#/career/info/job-list/{job_id}?language={locale}"
    categories = item.get("category_display")
    departments = (
        [_localized(value.get("name"), locale) for value in categories if _localized(value.get("name"), locale)]
        if isinstance(categories, list) and all(isinstance(value, Mapping) for value in categories)
        else []
    )
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=apply_url,
        updated_at=_iso_date(item.get("start_date")),
        source=f"startdate:{contract['host']}",
        departments=departments,
        offices=[location] if location else [],
        description=_localized(item.get("description"), locale) or None,
        raw_metadata={
            "platform": "startdate",
            "requisition_number": str(item.get("requisition_number") or "").strip(),
        },
    )


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise AdapterError(f"StartDate start_date is not ISO formatted: {text}") from exc
