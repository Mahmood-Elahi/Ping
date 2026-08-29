"""Dayforce public Candidate Portal adapter.

Dayforce's public Next.js board exposes tenant and job-board identity in
``__NEXT_DATA__``.  Its JSON search endpoint is protected by the anonymous
NextAuth CSRF token issued to the same public session; no candidate login or
browser state is required.
"""

from __future__ import annotations

from datetime import date
from html import unescape
import json
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


ENDPOINT_VARIANT = "nextjs_jobposting_search"
HOST = "jobs.dayforcehcm.com"
PAGE_SIZE = 25
MAX_PAGES = 5
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_BOOTSTRAP_BYTES = 1_000_000
MAX_CSRF_BYTES = 10_000
MAX_RESPONSE_BYTES = 3_000_000
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch one bounded, deterministic public Dayforce job collection."""
    config = validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)

    shell = session.get(
        config["search_url"],
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        },
        timeout=timeout,
    )
    next_data = _bootstrap_json(shell)
    site_info = _site_info(next_data, config)
    csrf_token = _csrf_token(session, config, timeout)

    metadata: dict[str, Any] = {
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "GET shell + GET CSRF + POST search",
        "page_size": PAGE_SIZE,
        "maximum_pages": int(target.max_pages),
        "maximum_results": int(target.max_jobs),
        "pages": [],
        "jobs_normalized": 0,
        "duplicate_requisitions_skipped": 0,
        "total_available": None,
        "stop_reason": "not_started",
    }
    setattr(session, "dayforce_pagination", metadata)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config["origin"],
        "Referer": config["search_url"],
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        "X-CSRF-TOKEN": csrf_token,
    }
    jobs: list[JobRecord] = []
    seen_posting_ids: set[int] = set()
    seen_requisition_ids: set[int] = set()
    seen_pages: set[tuple[int, ...]] = set()
    observed_total: Optional[int] = None

    for page_index in range(int(target.max_pages)):
        offset = page_index * PAGE_SIZE
        body = {
            "clientNamespace": site_info["clientNamespace"],
            "jobBoardCode": site_info["jobBoardCode"],
            "cultureCode": site_info["cultureCode"],
            "paginationStart": offset,
        }
        response = session.post(
            config["endpoint_url"],
            headers=headers,
            json=body,
            timeout=timeout,
        )
        payload = _search_json(response, target, offset)
        rows = payload["jobPostings"]
        total = payload["maxCount"]
        if observed_total is None:
            observed_total = total
        elif total != observed_total:
            raise AdapterError("Dayforce total job count changed during pagination")
        if total > offset and not rows:
            raise AdapterError("Dayforce results ended before the reported total")

        page_ids = tuple(_positive_int(row, "jobPostingId", "posting") for row in rows)
        if page_ids and page_ids in seen_pages:
            raise AdapterError("Dayforce repeated a result page during pagination")
        seen_pages.add(page_ids)

        for row in rows:
            posting_id = _positive_int(row, "jobPostingId", "posting")
            if posting_id in seen_posting_ids:
                raise AdapterError(f"Dayforce repeated stable posting ID: {posting_id}")
            seen_posting_ids.add(posting_id)
            requisition_id = _positive_int(row, "jobReqId", "requisition")
            if requisition_id in seen_requisition_ids:
                metadata["duplicate_requisitions_skipped"] += 1
                continue
            seen_requisition_ids.add(requisition_id)
            jobs.append(_normalize(row, target, config, site_info))

        metadata["pages"].append(
            {
                "page_number": page_index + 1,
                "offset": offset,
                "jobs_returned": len(rows),
            }
        )
        metadata["jobs_normalized"] = min(len(jobs), int(target.max_jobs))
        if offset + len(rows) >= total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if len(jobs) >= int(target.max_jobs):
            metadata["stop_reason"] = "maximum_results_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"

    metadata["total_available"] = observed_total
    return jobs[: int(target.max_jobs)]


def validate_target(target: CompanyTarget) -> dict[str, str]:
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Dayforce endpoint_variant must be {ENDPOINT_VARIANT}")
    tenant = str(target.tenant or "").strip()
    board = str(target.site_id or "").strip()
    locale = str(target.locale or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", tenant):
        raise AdapterError("Dayforce target needs a valid tenant/client namespace")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", board):
        raise AdapterError("Dayforce target needs a valid site_id/job board code")
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale):
        raise AdapterError("Dayforce target needs an ll-CC locale")

    search = urlparse(str(target.search_url or ""))
    endpoint = urlparse(str(target.endpoint_url or ""))
    expected_search_path = f"/{locale}/{tenant}/{board}"
    expected_endpoint_path = f"/api/geo/{tenant}/jobposting/search"
    if (
        search.scheme != "https"
        or (search.hostname or "").casefold() != HOST
        or search.path.rstrip("/").casefold() != expected_search_path.casefold()
        or search.query
        or search.fragment
    ):
        raise AdapterError("Dayforce search_url must match its locale, tenant, and site_id")
    if (
        endpoint.scheme != "https"
        or (endpoint.hostname or "").casefold() != HOST
        or endpoint.path.casefold() != expected_endpoint_path.casefold()
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError("Dayforce endpoint_url must use the public tenant search path")
    allowed = {str(value).strip().casefold().rstrip(".") for value in target.allowed_apply_hosts}
    if allowed != {HOST}:
        raise AdapterError(f"Dayforce allowed_apply_hosts must contain only {HOST}")
    if int(target.page_size) != PAGE_SIZE:
        raise AdapterError(f"Dayforce page_size must be {PAGE_SIZE}")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Dayforce max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Dayforce max_jobs must be between 1 and {MAX_RESULTS}")
    if int(target.max_jobs) > int(target.max_pages) * PAGE_SIZE:
        raise AdapterError("Dayforce max_jobs exceeds the configured page capacity")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError(f"Dayforce max_response_bytes must be between 1 and {MAX_RESPONSE_BYTES}")
    origin = f"{search.scheme}://{search.netloc}"
    return {
        "tenant": tenant,
        "board": board,
        "locale": locale,
        "origin": origin,
        "search_url": search.geturl(),
        "endpoint_url": endpoint.geturl(),
        "csrf_url": f"{origin}/api/auth/csrf",
    }


def _bootstrap_json(response: Any) -> Mapping[str, Any]:
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce public shell HTTP {status}")
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > MAX_BOOTSTRAP_BYTES:
        raise AdapterError("Dayforce public shell exceeded the byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Dayforce public shell was not HTML")
    matches = NEXT_DATA_RE.findall(str(getattr(response, "text", "") or ""))
    if len(matches) != 1:
        raise AdapterError("Dayforce public shell omitted unambiguous __NEXT_DATA__")
    try:
        value = json.loads(matches[0])
    except (TypeError, ValueError) as exc:
        raise AdapterError("Dayforce __NEXT_DATA__ was invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise AdapterError("Dayforce __NEXT_DATA__ was malformed")
    return value


def _site_info(next_data: Mapping[str, Any], config: Mapping[str, str]) -> dict[str, Any]:
    query = next_data.get("query")
    page_props = next_data.get("props")
    if not isinstance(query, Mapping) or not isinstance(page_props, Mapping):
        raise AdapterError("Dayforce public shell omitted route identity")
    if (
        str(query.get("clientNamespace") or "").casefold() != config["tenant"].casefold()
        or str(query.get("careerSiteXRefCode") or "").casefold() != config["board"].casefold()
    ):
        raise AdapterError("Dayforce public shell crossed tenant or job-board boundaries")
    page = page_props.get("pageProps")
    dehydrated = page.get("dehydratedState") if isinstance(page, Mapping) else None
    queries = dehydrated.get("queries") if isinstance(dehydrated, Mapping) else None
    if not isinstance(queries, list):
        raise AdapterError("Dayforce public shell omitted site-info state")
    matches: list[Mapping[str, Any]] = []
    for item in queries:
        if not isinstance(item, Mapping):
            continue
        query_key = item.get("queryKey")
        state = item.get("state")
        data = state.get("data") if isinstance(state, Mapping) else None
        if isinstance(query_key, list) and query_key[:1] == ["site-info"] and isinstance(data, Mapping):
            matches.append(data)
    if len(matches) != 1:
        raise AdapterError("Dayforce public shell omitted unambiguous site-info state")
    site = matches[0]
    job_board_id = site.get("jobBoardId")
    if (
        str(site.get("clientNamespace") or "").casefold() != config["tenant"].casefold()
        or str(site.get("jobBoardCode") or "").casefold() != config["board"].casefold()
        or str(site.get("cultureCode") or "") != config["locale"]
        or isinstance(job_board_id, bool)
        or not isinstance(job_board_id, int)
        or job_board_id < 1
    ):
        raise AdapterError("Dayforce site-info crossed tenant, board, or locale boundaries")
    return {
        "clientNamespace": str(site["clientNamespace"]),
        "jobBoardCode": str(site["jobBoardCode"]),
        "cultureCode": str(site["cultureCode"]),
        "jobBoardId": job_board_id,
    }


def _csrf_token(
    session: requests.Session,
    config: Mapping[str, str],
    timeout: int,
) -> str:
    response = session.get(
        config["csrf_url"],
        headers={
            "Accept": "application/json",
            "Referer": config["search_url"],
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        },
        timeout=timeout,
    )
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce anonymous CSRF endpoint HTTP {status}")
    if len(bytes(getattr(response, "content", b"") or b"")) > MAX_CSRF_BYTES:
        raise AdapterError("Dayforce anonymous CSRF response exceeded the byte limit")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError("Dayforce anonymous CSRF response was not valid JSON") from exc
    token = str(payload.get("csrfToken") or "") if isinstance(payload, Mapping) else ""
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
        raise AdapterError("Dayforce anonymous CSRF response omitted a valid token")
    return token


def _search_json(response: Any, target: CompanyTarget, offset: int) -> Mapping[str, Any]:
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce HTTP {status} for {target.name} offset={offset}")
    if len(bytes(getattr(response, "content", b"") or b"")) > min(
        int(target.max_response_bytes), MAX_RESPONSE_BYTES
    ):
        raise AdapterError("Dayforce search response exceeded max_response_bytes")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError("Dayforce search response was not JSON")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError("Dayforce search response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobPostings"), list):
        raise AdapterError("Dayforce search response has no jobPostings list")
    total = payload.get("maxCount")
    response_offset = payload.get("offset")
    count = payload.get("count")
    rows = payload["jobPostings"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Dayforce maxCount must be non-negative")
    if isinstance(response_offset, bool) or response_offset != offset:
        raise AdapterError("Dayforce response offset did not match the requested page")
    if isinstance(count, bool) or count != len(rows):
        raise AdapterError("Dayforce response count did not match its jobPostings list")
    if len(rows) > PAGE_SIZE:
        raise AdapterError("Dayforce page exceeded the observed page size")
    if total < offset + len(rows):
        raise AdapterError("Dayforce maxCount was smaller than the returned result range")
    return payload


def _normalize(
    row: Any,
    target: CompanyTarget,
    config: Mapping[str, str],
    site_info: Mapping[str, Any],
) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("Dayforce posting must be a JSON object")
    posting_id = _positive_int(row, "jobPostingId", "posting")
    requisition_id = _positive_int(row, "jobReqId", "requisition")
    if str(row.get("clientNamespace") or "").casefold() != config["tenant"].casefold():
        raise AdapterError("Dayforce posting crossed tenant boundaries")
    if row.get("jobBoardId") != site_info["jobBoardId"]:
        raise AdapterError("Dayforce posting crossed job-board boundaries")
    title = _clean_text(row.get("jobTitle"))
    if not title:
        raise AdapterError("Dayforce posting has no title")

    raw_locations = row.get("postingLocations")
    if raw_locations is None and row.get("hasVirtualLocation") is True:
        raw_locations = []
    if not isinstance(raw_locations, list):
        raise AdapterError("Dayforce postingLocations must be a list")
    locations: list[str] = []
    if row.get("hasVirtualLocation") is True:
        locations.append("Remote")
    for value in raw_locations:
        if not isinstance(value, Mapping):
            raise AdapterError("Dayforce posting location must be an object")
        location = _clean_text(value.get("formattedAddress"))
        if not location:
            location = ", ".join(
                part
                for part in (
                    _clean_text(value.get("cityName")),
                    _clean_text(value.get("stateCode")),
                    _clean_text(value.get("isoCountryCode")),
                )
                if part
            )
        if location and location.casefold() not in {item.casefold() for item in locations}:
            locations.append(location)

    tenant = config["tenant"].casefold()
    board = config["board"].casefold()
    detail_url = (
        f"{config['origin']}/{config['locale']}/{config['tenant']}/{config['board']}"
        f"/jobs/{posting_id}"
    )
    return JobRecord(
        job_id=f"dayforce:{tenant}:{board}:{requisition_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=_posting_date(row.get("postingStartTimestampUTC")),
        source=f"dayforce:{tenant}:{board}",
        offices=locations,
        description=_clean_text(row.get("jobDescription")) or None,
        raw_metadata={
            "platform": "dayforce",
            "endpoint_variant": ENDPOINT_VARIANT,
            "job_posting_id": posting_id,
            "requisition_id": requisition_id,
            "job_board_id": site_info["jobBoardId"],
            "has_virtual_location": row.get("hasVirtualLocation") is True,
            "posting_expiry": _clean_text(row.get("postingExpiryTimestampUTC")),
            "posting_locations": list(raw_locations),
            "detail_url": detail_url,
            "apply_url": detail_url,
        },
    )


def _positive_int(row: Any, field: str, label: str) -> int:
    if not isinstance(row, Mapping):
        raise AdapterError("Dayforce posting must be a JSON object")
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterError(f"Dayforce posting has no stable numeric {label} ID")
    return value


def _posting_date(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise AdapterError("Dayforce postingStartTimestampUTC was invalid") from exc


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()
