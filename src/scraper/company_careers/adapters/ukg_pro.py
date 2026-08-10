"""UKG Pro Recruiting public job-board adapter."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PAGE_SIZE = 50
MAX_PAGES = 2
MAX_RESULTS = 100
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SUPPORTED_HOST_RE = re.compile(r"^recruiting\d*\.ultipro\.(?:com|ca)$", re.IGNORECASE)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
BOARD_PATH_RE = re.compile(
    r"^/(?P<tenant>[^/]+)/JobBoard/(?P<board>[0-9a-f-]{36})/?$",
    re.IGNORECASE,
)
BOARD_JSON_RE = re.compile(
    r"\bjobBoard\s*:\s*(\{.*?\})\s*,\s*isAuthenticated\s*:",
    re.IGNORECASE | re.DOTALL,
)
BLOCKED_MARKERS = (
    "captcha",
    "cf-chl-",
    "cloudflare challenge",
    "session expired",
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch a bounded UKG board using its anonymous LoadSearchResults POST."""
    board = validate_target(target)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    maximum_pages = int(target.max_pages)
    maximum_jobs = int(target.max_jobs)

    board_response = _request(session, "get", board["board_url"], timeout)
    board_config = _parse_board_config(board_response, target, board)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    raw_offset = 0
    endpoint = f"{board['board_url']}/JobBoardView/LoadSearchResults"
    for page_index in range(maximum_pages):
        remaining = maximum_jobs - len(jobs)
        if remaining <= 0:
            break
        top = min(PAGE_SIZE, remaining)
        skip = raw_offset
        response = _request(
            session,
            "post",
            endpoint,
            timeout,
            json={
                "opportunitySearch": {
                    "Top": top,
                    "Skip": skip,
                    "QueryString": "",
                    "OrderBy": [
                        {
                            "Value": "postedDateDesc",
                            "PropertyName": "PostedDate",
                            "Ascending": False,
                        }
                    ],
                    "Filters": [],
                }
            },
        )
        payload = _parse_search(response, top, skip)
        total = payload["totalCount"]
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise AdapterError("UKG Pro totalCount changed during pagination")

        opportunities = payload["opportunities"]
        for item in opportunities:
            if not _has_exact_job_attribution(item, target):
                continue
            job = _normalize(item, target, board, str(board_config["Name"]))
            if job.job_id in seen_ids:
                raise AdapterError("UKG Pro returned a duplicate stable opportunity ID")
            seen_ids.add(job.job_id)
            jobs.append(job)

        raw_offset += len(opportunities)
        if skip + len(opportunities) >= total or len(opportunities) < top:
            break
        if page_index + 1 < maximum_pages:
            time.sleep(RETRY_DELAY_SECONDS)
    return jobs


def validate_target(target: CompanyTarget) -> dict[str, str]:
    if not target.careers_url or not target.identity_regex:
        raise AdapterError("UKG Pro target needs careers_url and identity_regex")
    parsed = urlparse(str(target.careers_url))
    match = BOARD_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not SUPPORTED_HOST_RE.fullmatch(parsed.hostname)
        or parsed.query
        or parsed.fragment
        or not match
    ):
        raise AdapterError("UKG Pro careers_url is not a supported public JobBoard URL")
    if not UUID_RE.fullmatch(match.group("board")):
        raise AdapterError("UKG Pro board ID must be a UUID")
    try:
        re.compile(str(target.identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"UKG Pro identity_regex is invalid: {exc}") from exc
    for field, value in (
        ("job_attribution_regex", target.job_attribution_regex),
        ("job_attribution_exclude_regex", target.job_attribution_exclude_regex),
    ):
        if value:
            try:
                re.compile(str(value), re.IGNORECASE)
            except re.error as exc:
                raise AdapterError(f"UKG Pro {field} is invalid: {exc}") from exc
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"UKG Pro max_pages cannot exceed {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"UKG Pro max_jobs cannot exceed {MAX_RESULTS}")
    board_url = urlunparse(parsed._replace(path=parsed.path.rstrip("/"), query="", fragment=""))
    return {
        "board_url": board_url,
        "host": parsed.hostname.casefold(),
        "tenant": match.group("tenant"),
        "board_id": match.group("board").casefold(),
    }


def _has_exact_job_attribution(item: Mapping[str, Any], target: CompanyTarget) -> bool:
    """Keep only postings whose own fields establish the configured employer."""
    required = str(target.job_attribution_regex or "").strip()
    excluded = str(target.job_attribution_exclude_regex or "").strip()
    if not required and not excluded:
        return True
    text = " ".join(
        str(item.get(field) or "")
        for field in ("Title", "BriefDescription", "JobCategoryName", "RequisitionNumber")
    )
    if required and not re.search(required, text, re.IGNORECASE):
        return False
    return not (excluded and re.search(excluded, text, re.IGNORECASE))


def _request(
    session: requests.Session,
    method: str,
    url: str,
    timeout: int,
    **kwargs: Any,
) -> Any:
    headers = {
        "Accept": "application/json" if method == "post" else "text/html,application/xhtml+xml",
        "User-Agent": "job-radar/1.0",
    }
    if method == "post":
        headers.update({"Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"})
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = getattr(session, method)(url, headers=headers, timeout=timeout, **kwargs)
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
            raise AdapterError(f"UKG Pro authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"UKG Pro blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"UKG Pro HTTP {status}")
        content = getattr(response, "content", b"")
        if content and len(content) > MAX_RESPONSE_BYTES:
            raise AdapterError("UKG Pro response exceeded the byte limit")
        return response
    raise AdapterError("UKG Pro request attempts exhausted")


def _parse_board_config(
    response: Any,
    target: CompanyTarget,
    board: Mapping[str, str],
) -> Mapping[str, Any]:
    text = str(getattr(response, "text", "") or "")
    lowered = text.casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise AdapterError("UKG Pro board requires CAPTCHA, challenge, or session state")
    match = BOARD_JSON_RE.search(text)
    if not match:
        raise AdapterError("UKG Pro board is missing its public jobBoard identity contract")
    try:
        config = json.loads(match.group(1))
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"UKG Pro jobBoard identity is invalid JSON: {exc}") from exc
    if not isinstance(config, Mapping):
        raise AdapterError("UKG Pro jobBoard identity must be a JSON object")
    if str(config.get("Id") or "").casefold() != board["board_id"]:
        raise AdapterError("UKG Pro response board ID does not match configuration")
    name = str(config.get("Name") or "").strip()
    if not name or not re.search(str(target.identity_regex), name, re.IGNORECASE):
        raise AdapterError("UKG Pro board has mixed or ambiguous company identity")
    if config.get("HasThirdPartyPostings") is not False:
        raise AdapterError("UKG Pro board permits mixed third-party postings")
    if str(config.get("OpportunityDetailUrl") or "").strip():
        raise AdapterError("UKG Pro board overrides the official job detail flow")
    return config


def _parse_search(response: Any, top: int, skip: int) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"UKG Pro returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("UKG Pro response must be a JSON object")
    opportunities = payload.get("opportunities")
    total = payload.get("totalCount")
    if not isinstance(opportunities, list) or not all(isinstance(item, Mapping) for item in opportunities):
        raise AdapterError("UKG Pro response is missing an opportunity-object list")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("UKG Pro totalCount must be a non-negative integer")
    if len(opportunities) > top:
        raise AdapterError("UKG Pro page exceeded the requested result limit")
    if total > skip and not opportunities:
        raise AdapterError("UKG Pro response ended before its reported totalCount")
    return payload


def _normalize(
    item: Mapping[str, Any],
    target: CompanyTarget,
    board: Mapping[str, str],
    board_name: str,
) -> JobRecord:
    job_id = str(item.get("Id") or "").strip()
    title = str(item.get("Title") or "").strip()
    if not UUID_RE.fullmatch(job_id) or not title:
        raise AdapterError("UKG Pro opportunity is missing a stable UUID or title")
    locations = _locations(item.get("Locations"))
    detail_url = f"{board['board_url']}/OpportunityDetail?{urlencode({'opportunityId': job_id})}"
    query = parse_qs(urlparse(detail_url).query)
    if query.get("opportunityId") != [job_id]:
        raise AdapterError("UKG Pro opportunity is missing a job-specific detail URL")
    category = str(item.get("JobCategoryName") or "").strip()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=_iso_date(item.get("PostedDate")),
        source=f"ukg_pro:{board['host']}:{board['tenant'].casefold()}:{board['board_id']}",
        departments=[category] if category else [],
        offices=locations,
        description=str(item.get("BriefDescription") or "").strip() or None,
        raw_metadata={
            "platform": "ukg_pro",
            "board_name": board_name,
            "requisition_number": str(item.get("RequisitionNumber") or "").strip(),
            "full_time": item.get("FullTime"),
            "job_location_type": item.get("JobLocationType"),
        },
    )


def _locations(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise AdapterError("UKG Pro opportunity Locations must be a list")
    results: list[str] = []
    for location in value:
        if not isinstance(location, Mapping):
            raise AdapterError("UKG Pro opportunity location must be an object")
        address = location.get("Address") or {}
        if not isinstance(address, Mapping):
            raise AdapterError("UKG Pro opportunity address must be an object")
        state = address.get("State") or {}
        country = address.get("Country") or {}
        if not isinstance(state, Mapping) or not isinstance(country, Mapping):
            raise AdapterError("UKG Pro opportunity state and country must be objects")
        address_text = ", ".join(
            text
            for text in (
                str(address.get("City") or "").strip(),
                str(state.get("Name") or state.get("Code") or "").strip(),
                str(country.get("Name") or country.get("Code") or "").strip(),
            )
            if text
        )
        candidates: list[Any] = []
        if location.get("DisplayName") is True:
            candidates.append(location.get("LocalizedName"))
        if location.get("DisplayLocationId") is True:
            candidates.append(location.get("LocalizedLocationId"))
        if location.get("DisplayAddress") is True:
            candidates.append(address_text)
        if location.get("DisplayDescription") is True:
            candidates.append(location.get("LocalizedDescription"))
        candidates.extend(
            [location.get("LocalizedName"), location.get("LocalizedLocationId"), address_text]
        )
        selected = next((" ".join(str(item).split()) for item in candidates if str(item or "").strip()), "")
        if selected and selected.casefold() not in {item.casefold() for item in results}:
            results.append(selected)
    return results


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise AdapterError(f"UKG Pro PostedDate is not ISO formatted: {text}") from exc
