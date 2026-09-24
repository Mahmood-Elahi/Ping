"""Bounded adapter for Apple's first-party careers search API."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)


SEARCH_ENDPOINT = "https://jobs.apple.com/api/v1/search"
ENDPOINT_VARIANT = "apple_search_api_v1"
SOURCE = "browser_rendered_html:jobs.apple.com"
JOB_ID_PREFIX = f"{SOURCE}:"
PAGE_SIZE = 20
MAX_PAGES = 3
MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 3_000_000
READ_CHUNK_BYTES = 64 * 1024
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "job-radar/1.0",
}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")
SAFE_SLUG_RE = re.compile(r"^(?:[a-z0-9-]|%[0-9A-Fa-f]{2}){1,240}$")
SAFE_LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")
SAFE_TEAM_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")


def validate_target(target: CompanyTarget) -> None:
    """Restrict this company-specific protocol to Apple's reviewed endpoint."""
    if target.name.strip().casefold() != "apple":
        raise AdapterError("Apple adapter cannot be used for another employer")
    if target.endpoint_url != SEARCH_ENDPOINT:
        raise AdapterError("Apple target must use the reviewed first-party search endpoint")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError("Apple target has an unsupported endpoint variant")
    endpoint = urlparse(str(target.endpoint_url))
    if endpoint.scheme != "https" or endpoint.hostname != "jobs.apple.com":
        raise AdapterError("Apple endpoint must remain on jobs.apple.com over HTTPS")
    allowed_hosts = {str(host).strip().casefold() for host in target.allowed_apply_hosts}
    if allowed_hosts != {"jobs.apple.com"}:
        raise AdapterError("Apple apply URLs must be restricted to jobs.apple.com")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Apple max_pages cannot exceed {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Apple max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Apple response byte limit exceeds the hard maximum")
    locale = str(target.locale or "en-us").casefold()
    if not SAFE_LOCALE_RE.fullmatch(locale):
        raise AdapterError("Apple locale is invalid")


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch and normalize a bounded slice of Apple's public search results."""
    validate_target(target)
    maximum_pages = int(target.max_pages)
    maximum_jobs = int(target.max_jobs)
    locale = str(target.locale or "en-us").casefold()
    metadata = SourceScanMetadata(**{
        "endpoint_variant": ENDPOINT_VARIANT,
        "pages": [],
        "pages_fetched": 0,
        "bytes_inspected": 0,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "jobs_normalized": 0,
        "total_records": None,
        "repeated_page": False,
        "zero_job_contract": False,
        "stop_reason": "not_started",
    })
    setattr(session, "apple_pagination", metadata)
    publish_source_scan_metadata(session, metadata)

    jobs: list[JobRecord] = []
    by_stable_id: dict[str, JobRecord] = {}
    seen_pages: set[tuple[str, ...]] = set()
    records_seen = 0
    expected_total: int | None = None

    for page in range(1, maximum_pages + 1):
        body = {
            "query": "",
            "filters": {
                "locations": ["postLocation-USA", "postLocation-CAN"],
            },
            "page": page,
            "locale": locale,
            "sort": "newest",
            "format": {
                "longDate": "MMMM D, YYYY",
                "mediumDate": "MMM D, YYYY",
            },
        }
        metadata["pages_requested"] += 1
        payload, response_bytes = _post_json(
            session,
            str(target.endpoint_url),
            body,
            timeout,
            int(target.max_response_bytes),
        )
        metadata["bytes_inspected"] += response_bytes
        items, total = _search_page(payload)
        metadata["pages_completed"] += 1
        metadata["total_available"] = total
        if expected_total is None:
            expected_total = total
            metadata["total_records"] = total
        elif total != expected_total:
            raise AdapterError("Apple pagination totalRecords changed between pages")

        normalized_page = [_normalize(item, target, locale) for item in items]
        signature = tuple(
            json.dumps(job.to_dict(), sort_keys=True, separators=(",", ":"))
            for job in normalized_page
        )
        if signature and signature in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            raise AdapterError("Apple pagination repeated a previously observed page")
        if signature:
            seen_pages.add(signature)

        new_jobs = 0
        for job in normalized_page:
            existing = by_stable_id.get(job.job_id)
            if existing is not None:
                if existing.to_dict() != job.to_dict():
                    raise AdapterError("Apple returned a conflicting duplicate stable ID")
                continue
            by_stable_id[job.job_id] = job
            if len(jobs) < maximum_jobs:
                jobs.append(job)
                new_jobs += 1

        records_seen += len(items)
        metadata["pages"].append(
            {
                "page": page,
                "jobs_returned": len(items),
                "jobs_normalized": new_jobs,
            }
        )
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["jobs_normalized"] = len(jobs)

        if not items:
            if page == 1 and total == 0:
                metadata["zero_job_contract"] = True
                metadata["stop_reason"] = "validated_zero_jobs"
                metadata["scan_complete"] = True
                metadata["scan_completeness_reason"] = "reported_total_reached"
                break
            if records_seen < total:
                raise AdapterError("Apple pagination ended before totalRecords was satisfied")
            metadata["stop_reason"] = "total_records_reached"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
        if len(jobs) >= maximum_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if records_seen >= total:
            metadata["stop_reason"] = "total_records_reached"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"

    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True

    return jobs


def _post_json(
    session: requests.Session,
    url: str,
    body: Mapping[str, Any],
    timeout: int,
    byte_limit: int,
) -> tuple[Any, int]:
    response = session.post(
        url,
        json=dict(body),
        headers=HEADERS,
        timeout=timeout,
        stream=True,
    )
    if response.status_code != 200:
        _close(response)
        raise AdapterError(f"Apple Careers HTTP {response.status_code}")
    content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
    if "application/json" not in content_type.casefold():
        _close(response)
        raise AdapterError("Apple blocked response or unexpected Content-Type")

    body_bytes = bytearray()
    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        try:
            for chunk in iter_content(chunk_size=READ_CHUNK_BYTES):
                if not chunk:
                    continue
                body_bytes.extend(chunk)
                if len(body_bytes) > byte_limit:
                    raise AdapterError("Apple response exceeded the byte limit")
        finally:
            _close(response)
    else:
        content = getattr(response, "content", None)
        if content is None:
            content = str(getattr(response, "text", "") or "").encode("utf-8")
        body_bytes.extend(content)
        _close(response)
        if len(body_bytes) > byte_limit:
            raise AdapterError("Apple response exceeded the byte limit")
    try:
        return json.loads(bytes(body_bytes)), len(body_bytes)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AdapterError(f"Apple response is malformed JSON: {exc}") from exc


def _search_page(payload: object) -> tuple[list[Mapping[str, Any]], int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("res"), Mapping):
        raise AdapterError("Apple response is missing its search result envelope")
    result = payload["res"]
    items = result.get("searchResults")
    total = result.get("totalRecords")
    if not isinstance(items, list):
        raise AdapterError("Apple response is missing its searchResults list")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Apple totalRecords must be a non-negative integer")
    if len(items) > PAGE_SIZE:
        raise AdapterError("Apple searchResults exceeded the reviewed page size")
    if total < len(items):
        raise AdapterError("Apple totalRecords is smaller than the returned collection")
    if not items and total > 0:
        raise AdapterError("Apple returned an invalid empty-looking response")
    if any(not isinstance(item, Mapping) for item in items):
        raise AdapterError("Apple searchResults must contain job objects")
    return items, total


def _normalize(
    item: Mapping[str, Any],
    target: CompanyTarget,
    default_locale: str,
) -> JobRecord:
    raw_id = str(item.get("id") or "").strip()
    req_id = str(item.get("reqId") or "").strip()
    title = " ".join(str(item.get("postingTitle") or "").split())
    slug = str(item.get("transformedPostingTitle") or "").strip()
    if not raw_id or req_id != raw_id or not SAFE_ID_RE.fullmatch(raw_id):
        raise AdapterError("Apple job is missing a corroborated stable requisition ID")
    if not title or not SAFE_SLUG_RE.fullmatch(slug):
        raise AdapterError("Apple job is missing a title or canonical URL slug")
    if item.get("postExternal") is not True:
        raise AdapterError("Apple job is not an externally posted role")

    canonical_id = raw_id
    if raw_id.startswith("PIPE-"):
        position_id = str(item.get("positionId") or "").strip()
        if not SAFE_ID_RE.fullmatch(position_id):
            raise AdapterError("Apple pipeline role is missing its canonical position ID")
        canonical_id = position_id

    locale_info = item.get("localeInfo")
    locale = default_locale
    if isinstance(locale_info, Mapping) and locale_info.get("defaultLocaleCode"):
        locale = str(locale_info["defaultLocaleCode"]).strip().casefold()
    if not SAFE_LOCALE_RE.fullmatch(locale):
        raise AdapterError("Apple job contains an invalid locale")

    team = item.get("team")
    if not isinstance(team, Mapping):
        raise AdapterError("Apple job is missing its structured team identity")
    team_name = " ".join(str(team.get("teamName") or "").split())
    team_code = str(team.get("teamCode") or "").strip()
    if not team_name or not SAFE_TEAM_RE.fullmatch(team_code):
        raise AdapterError("Apple job has malformed team metadata")

    raw_locations = item.get("locations")
    if not isinstance(raw_locations, list) or not raw_locations:
        raise AdapterError("Apple job is missing its structured locations")
    locations: list[str] = []
    for location in raw_locations:
        if not isinstance(location, Mapping):
            raise AdapterError("Apple job location must be an object")
        value = " ".join(str(location.get("name") or "").split())
        if not value:
            raise AdapterError("Apple job location is missing its display name")
        if value.casefold() not in {item.casefold() for item in locations}:
            locations.append(value)

    query = urlencode({"team": team_code})
    canonical_url = (
        f"https://jobs.apple.com/{locale}/details/{canonical_id}/{slug}?{query}"
    )
    return JobRecord(
        job_id=f"{JOB_ID_PREFIX}{canonical_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=canonical_url,
        updated_at=str(item.get("postDateInGMT") or item.get("postingDate") or ""),
        source=SOURCE,
        departments=[team_name],
        offices=locations,
        description=str(item.get("jobSummary") or "") or None,
        raw_metadata={
            "platform": "apple_search_api",
            "requisition_id": req_id,
            "position_id": str(item.get("positionId") or ""),
            "team_code": team_code,
            "job_type": str(item.get("type") or ""),
            "home_office": item.get("homeOffice"),
        },
    )


def _close(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
