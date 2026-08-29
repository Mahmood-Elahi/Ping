"""Meta Careers adapter."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


SEARCH_URL = "https://www.metacareers.com/jobsearch"
GRAPHQL_URL = "https://www.metacareers.com/api/graphql/"
JOBS_URL = "https://www.metacareers.com/jobs/{job_id}/"
DOC_ID = "29615178951461218"
FRIENDLY_NAME = "CareersJobSearchResultsDataQuery"
DEFAULT_SEARCH_TERMS = [
    "Principal Software Engineer",
    "Staff Software Engineer",
    "Production Engineer",
]
MAX_RESULTS = 20
MAX_PAGES = 3
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 4.0
MAX_RETRY_AFTER_SECONDS = 5.0
REQUEST_PACE_SECONDS = 0.5
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
JSON_CONTENT_TYPES = (
    "application/json",
    "text/json",
    "application/javascript",
    "text/javascript",
    "application/x-javascript",
)
CHALLENGE_MARKERS = (
    "access denied",
    "captcha",
    "challenge-platform",
    "cloudflare challenge",
    "request blocked",
    "verify you are human",
)
UPSTREAM_ERROR_MARKERS = (
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "proxy error",
    "service unavailable",
    "temporarily unavailable",
    "upstream connect error",
    "upstream request timeout",
)


@dataclass
class _RequestPacer:
    last_request_at: Optional[float] = None

    def wait(self) -> None:
        if self.last_request_at is None:
            return
        remaining = REQUEST_PACE_SECONDS - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def mark(self) -> None:
        self.last_request_at = time.monotonic()


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Meta Careers max_jobs must be between 1 and {MAX_RESULTS}")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Meta Careers max_pages must be between 1 and {MAX_PAGES}")
    maximum_jobs = int(target.max_jobs)
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    pacer = _RequestPacer()
    # The anonymous page context is parent-inventory state, not query state.
    # Reuse it for every configured term and for mapped Meta child brands.
    page_context = _fetch_search_context(session, "", timeout, pacer=pacer)

    for term in search_terms:
        if len(jobs) >= maximum_jobs:
            break
        for page in range(1, max(1, target.max_pages) + 1):
            if len(jobs) >= maximum_jobs:
                break
            payload = _fetch_results_page(
                session, term, page, page_context, timeout, pacer=pacer
            )
            page_jobs = _extract_jobs(payload, target.name)
            if not page_jobs:
                break

            for job in page_jobs:
                key = job.job_id or job.url or job.title
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(job)
                if len(jobs) >= maximum_jobs:
                    break

    return jobs


def _fetch_search_context(
    session: requests.Session,
    term: str,
    timeout: int,
    *,
    pacer: Optional[_RequestPacer] = None,
) -> Dict[str, str]:
    referer = f"{SEARCH_URL}?{urlencode({'q': term})}" if term else SEARCH_URL
    response = _request_with_backoff(
        session,
        "get",
        referer,
        pacer=pacer or _RequestPacer(),
        headers={
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise AdapterError(f"Meta Careers search HTTP {response.status_code}")

    lsd = _extract_pattern(response.text, r'\["LSD",\[\],\{"token":"([^"]+)"')
    if not lsd:
        raise AdapterError("Could not find Meta Careers LSD token")

    return {
        "referer": referer,
        "lsd": lsd,
        "client_revision": _extract_pattern(response.text, r'"client_revision":(\d+)') or "0",
        "hsi": _extract_pattern(response.text, r'"hsi":"(\d+)"') or "",
    }


def _fetch_results_page(
    session: requests.Session,
    term: str,
    page: int,
    context: Dict[str, str],
    timeout: int,
    *,
    pacer: Optional[_RequestPacer] = None,
) -> Dict[str, Any]:
    search_input = {
        "q": term,
        "divisions": [],
        "offices": [],
        "roles": [],
        "leadership_levels": [],
        "saved_jobs": [],
        "saved_searches": [],
        "sub_teams": [],
        "teams": [],
        "is_leadership": False,
        "is_remote_only": False,
        "sort_by_new": False,
        "page": page,
    }
    data = {
        "av": "0",
        "__user": "0",
        "__a": "1",
        "__req": str(page),
        "__rev": context["client_revision"],
        "__hsi": context["hsi"],
        "__ccg": "GOOD",
        "__comet_req": "15",
        "lsd": context["lsd"],
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": FRIENDLY_NAME,
        "variables": json.dumps({"search_input": search_input}, separators=(",", ":")),
        "server_timestamps": "true",
        "doc_id": DOC_ID,
    }
    response = _request_with_backoff(
        session,
        "post",
        GRAPHQL_URL,
        pacer=pacer or _RequestPacer(),
        transient_response=_graphql_transient_reason,
        data=data,
        headers={
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.metacareers.com",
            "Referer": context["referer"],
            "X-FB-LSD": context["lsd"],
            "X-FB-Friendly-Name": FRIENDLY_NAME,
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        text = str(getattr(response, "text", "") or "")
        details = _response_details(
            response, _header(response, "content-type"), _body_shape(text)
        )
        raise AdapterError(
            f"Meta Careers GraphQL HTTP {response.status_code} ({details})"
        )

    return _decode_graphql_response(response)


def _request_with_backoff(
    session: requests.Session,
    method: str,
    url: str,
    *,
    pacer: _RequestPacer,
    transient_response: Optional[Callable[[Any], Optional[str]]] = None,
    **kwargs: Any,
) -> Any:
    request = getattr(session, method)
    last_status: Optional[int] = None
    for attempt in range(MAX_ATTEMPTS):
        pacer.wait()
        try:
            response = request(url, **kwargs)
        except requests.RequestException:
            pacer.mark()
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(_exponential_delay(attempt))
            continue
        pacer.mark()
        last_status = getattr(response, "status_code", None)
        response_reason = (
            transient_response(response) if transient_response is not None else None
        )
        should_retry = last_status in TRANSIENT_STATUSES or response_reason is not None
        if not should_retry or attempt + 1 >= MAX_ATTEMPTS:
            return response
        time.sleep(_retry_delay(response, attempt))
    raise AdapterError(f"Meta Careers request attempts exhausted (HTTP {last_status})")


def _retry_delay(response: Any, attempt: int) -> float:
    headers = getattr(response, "headers", {}) or {}
    value = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = _exponential_delay(attempt)
        return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))
    return _exponential_delay(attempt)


def _exponential_delay(attempt: int) -> float:
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2**attempt))


def _extract_jobs(payload: Dict[str, Any], company: str) -> List[JobRecord]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AdapterError("Meta Careers response did not include a data object")
    job_search = data.get("job_search_with_featured_jobs")
    if not isinstance(job_search, dict):
        raise AdapterError("Meta Careers response did not include a job search object")
    items = job_search.get("all_jobs")
    if not isinstance(items, list):
        raise AdapterError("Meta Careers response did not include an all_jobs list")

    jobs: List[JobRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            continue

        locations = _clean_strings(item.get("locations"))
        teams = _clean_strings(item.get("teams"))
        sub_teams = _clean_strings(item.get("sub_teams"))
        description = _summary(teams, sub_teams)
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=company,
                location=", ".join(locations),
                url=JOBS_URL.format(job_id=job_id),
                updated_at="",
                source="custom:meta",
                departments=teams,
                offices=locations,
                description=description,
            )
        )

    return jobs


def _decode_graphql_response(response: Any) -> Dict[str, Any]:
    text = str(getattr(response, "text", "") or "")
    content_type = _header(response, "content-type")
    shape = _body_shape(text)
    details = _response_details(response, content_type, shape)
    redirect_error = _graphql_redirect_error(response)
    if redirect_error:
        raise AdapterError(f"{redirect_error} ({details})")

    if not text.strip():
        raise AdapterError(f"Meta Careers GraphQL returned an empty response ({details})")
    if _contains_marker(text, CHALLENGE_MARKERS):
        raise AdapterError(
            f"Meta Careers GraphQL returned challenge or blocked content ({details})"
        )
    if content_type and not _is_json_content_type(content_type):
        if _contains_marker(text, UPSTREAM_ERROR_MARKERS):
            raise AdapterError(
                f"Meta Careers GraphQL returned upstream-error content ({details})"
            )
        # Meta currently serves its GraphQL object with ``text/html`` on some
        # successful anonymous requests.  Accept only a syntactically valid
        # JSON object after the redirect, challenge, upstream-error, and body
        # shape checks above.  Actual HTML and malformed JSON still fail closed.
        if shape != "json-like":
            raise AdapterError(
                f"Meta Careers GraphQL returned unexpected Content-Type ({details})"
            )

    return _decode_graphql_payload(text, details=details)


def _decode_graphql_payload(
    text: str, *, details: Optional[str] = None
) -> Dict[str, Any]:
    body = text.strip()
    if body.startswith("for (;;);"):
        body = body[len("for (;;);") :].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        suffix = f" ({details})" if details else ""
        raise AdapterError(
            f"Could not decode Meta Careers response: {exc}{suffix}"
        ) from exc
    if not isinstance(payload, dict):
        raise AdapterError("Meta Careers response was not a JSON object")
    if payload.get("errors"):
        raise AdapterError(f"Meta Careers GraphQL error: {payload['errors']}")
    return payload


def _graphql_transient_reason(response: Any) -> Optional[str]:
    if getattr(response, "status_code", None) != 200:
        return None
    if _graphql_redirect_error(response):
        return None

    text = str(getattr(response, "text", "") or "")
    if not text.strip():
        return "empty body"
    if _contains_marker(text, CHALLENGE_MARKERS):
        return None
    if _contains_marker(text, UPSTREAM_ERROR_MARKERS):
        return "upstream-error content"

    content_type = _header(response, "content-type")
    if content_type and not _is_json_content_type(content_type):
        return None
    if not content_type and _body_shape(text) != "json-like":
        return None
    try:
        _decode_graphql_payload(text)
    except AdapterError as exc:
        if "Could not decode" in str(exc):
            return "malformed JSON from JSON endpoint"
    return None


def _graphql_redirect_error(response: Any) -> Optional[str]:
    history = getattr(response, "history", None)
    if history:
        return f"Meta Careers GraphQL followed {len(history)} unexpected redirect(s)"
    value = str(getattr(response, "url", "") or "").strip()
    if not value:
        return None
    expected = urlparse(GRAPHQL_URL)
    actual = urlparse(value)
    if (
        actual.scheme.casefold() != expected.scheme.casefold()
        or (actual.hostname or "").casefold() != (expected.hostname or "").casefold()
        or actual.path.rstrip("/") != expected.path.rstrip("/")
    ):
        return f"Meta Careers GraphQL redirected to an unexpected URL: {value}"
    return None


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value).strip()
    return ""


def _is_json_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    return media_type in JSON_CONTENT_TYPES or media_type.endswith("+json")


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in markers)


def _body_shape(text: str) -> str:
    body = text.lstrip()
    if not body:
        return "empty"
    if _contains_marker(body, CHALLENGE_MARKERS):
        return "challenge"
    if _contains_marker(body, UPSTREAM_ERROR_MARKERS):
        return "upstream-error"
    if body.startswith("for (;;);"):
        body = body[len("for (;;);") :].lstrip()
    if body.startswith(("{", "[")):
        return "json-like"
    if body.startswith("<"):
        return "html"
    return "text"


def _response_details(response: Any, content_type: str, shape: str) -> str:
    status = getattr(response, "status_code", None)
    text = str(getattr(response, "text", "") or "")
    final_url = str(getattr(response, "url", "") or "").strip()
    parts = [
        f"HTTP {status}",
        f"Content-Type {content_type or 'missing'}",
        f"body {shape}, {len(text.encode('utf-8'))} bytes",
    ]
    if final_url:
        parts.append(f"final URL {final_url}")
    history = getattr(response, "history", None)
    if history:
        parts.append(f"redirects {len(history)}")
    return ", ".join(parts)


def _extract_pattern(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _clean_strings(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _summary(teams: List[str], sub_teams: List[str]) -> Optional[str]:
    parts = []
    if teams:
        parts.append(f"Teams: {', '.join(teams)}")
    if sub_teams:
        parts.append(f"Sub-teams: {', '.join(sub_teams)}")
    return "; ".join(parts) if parts else None
