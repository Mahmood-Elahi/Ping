"""Bounded anonymous collector for the public BeeSite GJB search contract."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse, urlunparse

import requests

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    PageSizeObservation,
)


ENDPOINT_VARIANT = "gjb_search_v1"
MAX_PAGES = 2
MAX_RESULTS = 20
MAX_PAGE_SIZE = 20
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 3_000_000
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
DESCRIPTORS = [
    "ID",
    "PositionID",
    "PositionTitle",
    "PositionURI",
    "ApplyURI",
    "PublicationStartDate",
    "ParentOrganizationName",
    "OrganizationName",
    "PositionLocation.CityName",
    "PositionLocation.CountryName",
    "PositionLocation.CountryCode",
    "PositionLocation.DisplayName",
]


def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> list[JobRecord]:
    validate_target(target)
    maximum_pages = int(target.max_pages)
    maximum_jobs = int(target.max_jobs)
    configured_page_size = int(target.page_size)
    page_size = MAX_PAGE_SIZE if configured_page_size == 10 else configured_page_size
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "page_size": page_size,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "total_available": None,
        "pages": [],
        "pages_fetched": 0,
        "jobs_normalized": 0,
        "stop_reason": "not_started",
        "repeated_page": False,
        "zero_job_contract": False,
        "diagnostics": [],
        "warnings": [],
    })
    setattr(session, "beesite_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(
        maximum_pages,
        maximum_jobs,
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)
    page_sizes = PageSizeObservation(page_size)
    page_sizes.record(metadata)
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    positions_seen = 0

    for page_index in range(listing_budget.hard_request_ceiling):
        effective_jobs = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        first_item = positions_seen + 1
        payload = _request_payload(target, first_item, page_size)
        metadata["pages_requested"] += 1
        response = _get(session, target, payload, timeout)
        items, total, zero_contract = _parse_response(response, target)
        metadata["pages_completed"] += 1
        if metadata["total_available"] is None:
            metadata["total_available"] = total
        elif metadata["total_available"] != total:
            raise AdapterError("BeeSite total job count changed during pagination")
        next_positions_seen = positions_seen + len(items)
        if next_positions_seen > total:
            raise AdapterError("BeeSite returned jobs beyond its reported total")
        page_sizes.observe(
            len(items), terminal=next_positions_seen >= total
        )
        page_sizes.record(metadata)
        listing_budget.record_decision(
            metadata,
            total,
            page_sizes.effective_size,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=next_positions_seen,
        )
        effective_jobs = int(metadata["effective_listing_job_ceiling"])

        identities = tuple(_descriptor(item).get("ID", "") for item in items)
        page_record = {
            "page": page_index + 1,
            "first_item": first_item,
            "url": str(getattr(response, "url", target.endpoint_url)),
            "jobs_returned": len(items),
            "jobs_normalized": 0,
        }
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])
        if items and identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            break
        seen_pages.add(identities)

        new_ids = 0
        for item in items:
            job = _normalize(item, target)
            if job.job_id in seen_ids:
                raise AdapterError("BeeSite returned a duplicate stable job ID")
            seen_ids.add(job.job_id)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= effective_jobs:
                break
        page_record["jobs_normalized"] = new_ids
        metadata["jobs_normalized"] = len(jobs)
        positions_seen = next_positions_seen

        if page_index == 0 and not items:
            if not zero_contract:
                raise AdapterError("BeeSite response did not prove an empty jobs collection")
            metadata["zero_job_contract"] = True
            metadata["stop_reason"] = "validated_zero_jobs"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
        if not items:
            metadata["stop_reason"] = "empty_page_before_reported_total"
            metadata["scan_completeness_reason"] = "pagination_failure"
            break
        if len(jobs) == total:
            metadata["stop_reason"] = "reported_total_reached"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if page_index + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
        if page_index + 1 < listing_budget.hard_request_ceiling:
            time.sleep(RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("endpoint_url", target.endpoint_url),
            ("endpoint_variant", target.endpoint_variant),
            ("locale", target.locale),
            ("job_attribution_regex", target.job_attribution_regex),
            ("allowed_source_hosts", target.allowed_source_hosts),
            ("allowed_apply_hosts", target.allowed_apply_hosts),
        )
        if not value
    ]
    if missing:
        raise AdapterError(f"BeeSite target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"BeeSite endpoint_variant must be {ENDPOINT_VARIANT}")
    _require_https(str(target.endpoint_url), "endpoint_url")
    static_html._validate_hosts(target.allowed_source_hosts)
    static_html._validate_hosts(target.allowed_apply_hosts)
    endpoint_host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    if not _allowed_host(endpoint_host, target.allowed_source_hosts):
        raise AdapterError("BeeSite endpoint_url is outside allowed_source_hosts")
    if not re.fullmatch(r"[A-Za-z]{2}(?:[-_][A-Za-z]{2})?", str(target.locale)):
        raise AdapterError("BeeSite locale is invalid")
    static_html._safe_regex(str(target.job_attribution_regex))
    if target.job_attribution_exclude_regex:
        static_html._safe_regex(str(target.job_attribution_exclude_regex))
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"BeeSite max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"BeeSite max_jobs must be between 1 and {MAX_RESULTS}")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"BeeSite page_size must be between 1 and {MAX_PAGE_SIZE}")


def _request_payload(target: CompanyTarget, first_item: int, count_item: int) -> str:
    return json.dumps(
        {
            "LanguageCode": str(target.locale).upper(),
            "SearchParameters": {
                "FirstItem": first_item,
                "CountItem": count_item,
                "Sort": [
                    {"Criterion": "PublicationStartDate", "Direction": "DESC"}
                ],
                "MatchedObjectDescriptor": DESCRIPTORS,
            },
            "SearchCriteria": [],
        },
        separators=(",", ":"),
    )


def _get(
    session: requests.Session, target: CompanyTarget, payload: str, timeout: int
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                str(target.endpoint_url),
                params={"data": payload},
                headers=HEADERS,
                timeout=timeout,
            )
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
            raise AdapterError(f"BeeSite authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"BeeSite endpoint access was blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(f"BeeSite HTTP {status} for {target.name}")
        _validate_final_url(response, target)
        return response
    raise AdapterError("BeeSite request attempts exhausted")


def _parse_response(
    response: Any, target: CompanyTarget
) -> tuple[list[Mapping[str, Any]], int, bool]:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if not content:
        raise AdapterError("BeeSite returned an empty HTTP 200 response")
    if len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("BeeSite response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError("BeeSite returned a non-JSON response")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"BeeSite returned malformed JSON: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("LanguageCode") is None:
        raise AdapterError("BeeSite response is missing its language contract")
    result = payload.get("SearchResult")
    if not isinstance(result, Mapping):
        raise AdapterError("BeeSite response is missing SearchResult")
    items = result.get("SearchResultItems")
    total = result.get("SearchResultCountAll")
    returned = result.get("SearchResultCount")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise AdapterError("BeeSite SearchResultItems is not a job-object list")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("BeeSite SearchResultCountAll is invalid")
    if isinstance(returned, bool) or not isinstance(returned, int) or returned != len(items):
        raise AdapterError("BeeSite SearchResultCount does not match returned jobs")
    if total < len(items):
        raise AdapterError("BeeSite total count is smaller than the returned collection")
    zero_contract = total == 0 and returned == 0 and not items
    return list(items), total, zero_contract


def _normalize(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    descriptor = _descriptor(item)
    external_id = _text(descriptor.get("ID"))
    if _text(item.get("MatchedObjectId")) != external_id:
        raise AdapterError("BeeSite job ID does not match MatchedObjectId")
    title = _text(descriptor.get("PositionTitle"))
    if not external_id or not title:
        raise AdapterError("BeeSite job is missing a stable ID or title")
    attribution_text = " | ".join(
        _text(descriptor.get(key))
        for key in ("ParentOrganizationName", "OrganizationName")
        if _text(descriptor.get(key))
    )
    attribution = static_html._safe_regex(str(target.job_attribution_regex))
    exclusion = (
        static_html._safe_regex(str(target.job_attribution_exclude_regex))
        if target.job_attribution_exclude_regex
        else None
    )
    if not attribution_text or not attribution.search(attribution_text):
        raise AdapterError("BeeSite job failed exact source attribution")
    if exclusion and exclusion.search(attribution_text):
        raise AdapterError("BeeSite job matched excluded attribution")
    url = _official_url(descriptor.get("PositionURI"), target)
    locations = _locations(descriptor.get("PositionLocation"))
    updated = _text(descriptor.get("PublicationStartDate"))
    host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    work_modes = [
        value
        for value in locations
        if re.search(r"(?i)\b(remote|hybrid|telecommute)\b", value)
    ]
    return JobRecord(
        job_id=f"beesite:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=updated,
        source=f"beesite:{host}",
        departments=[],
        offices=locations,
        raw_metadata={
            "platform": "beesite",
            "endpoint_variant": target.endpoint_variant,
            "external_job_id": external_id,
            "position_id": _text(descriptor.get("PositionID")),
            "parent_organization": _text(descriptor.get("ParentOrganizationName")),
            "organization": _text(descriptor.get("OrganizationName")),
            "work_modes": work_modes,
        },
    )


def _descriptor(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("MatchedObjectDescriptor")
    if not isinstance(value, Mapping):
        raise AdapterError("BeeSite job is missing MatchedObjectDescriptor")
    return value


def _locations(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else []
    result: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise AdapterError("BeeSite job contains an invalid location")
        display = _text(row.get("DisplayName"))
        if display:
            result.append(display)
            continue
        parts = [
            _text(row.get(key))
            for key in ("CityName", "CountryName")
            if _text(row.get(key))
        ]
        if parts:
            result.append(", ".join(parts))
    return _unique(result)


def _official_url(value: Any, target: CompanyTarget) -> str:
    url = _text(value)
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not _allowed_host(host, target.allowed_apply_hosts):
        raise AdapterError("BeeSite job has no official allowed-host URL")
    if not parsed.path:
        raise AdapterError("BeeSite job URL has no path")
    return urlunparse(parsed._replace(fragment=""))


def _validate_final_url(response: Any, target: CompanyTarget) -> None:
    final_url = str(getattr(response, "url", "") or target.endpoint_url or "")
    parsed = urlparse(final_url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not _allowed_host(host, target.allowed_source_hosts):
        raise AdapterError("BeeSite endpoint redirected outside allowed source hosts")


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(
        host == str(value).casefold() or host.endswith("." + str(value).casefold())
        for value in allowed
    )


def _require_https(url: str, field: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise AdapterError(f"BeeSite {field} must be an anonymous HTTPS URL")
    if any(token in parsed.path.casefold() for token in ("/login", "/signin", "/session")):
        raise AdapterError(f"BeeSite {field} cannot require authentication or session state")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value).strip(" |")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
