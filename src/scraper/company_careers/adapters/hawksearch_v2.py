"""Generic bounded adapter for anonymous HawkSearch v2 career indexes."""

from __future__ import annotations

import math
import re
import time
from datetime import date, datetime
from html import unescape
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


ENDPOINT_VARIANT = "hawksearch_v2_search"
MAX_PAGES = 5
MAX_PAGE_SIZE = 100
MAX_RESULTS = MAX_PAGES * MAX_PAGE_SIZE
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5_000_000
PAGE_DELAY_SECONDS = 0.2
CLIENT_GUID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
JOB_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{5,63}$")
HAWKSEARCH_HOST_RE = re.compile(
    r"^[a-z0-9-]+\.searchapi(?:-[a-z0-9-]+)?\.hawksearch\.com$", re.I
)
BLOCKED_MARKERS = (
    "captcha",
    "access denied",
    "verify you are human",
    "challenge-platform",
    "sign in to continue",
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    validate_target(target)
    max_pages = int(target.max_pages)
    page_size = int(target.page_size)
    requested_limit = min(MAX_RESULTS, int(target.max_jobs))
    timeout = min(MAX_TIMEOUT_SECONDS, max(1, int(timeout)))
    client_guid = str(target.client_guid)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        "X-HawkSearch-ClientGuid": client_guid,
    }
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    observed_total: int | None = None
    observed_pages: int | None = None
    metadata: dict[str, Any] = {
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "POST",
        "page_size": page_size,
        "maximum_pages": max_pages,
        "maximum_results": requested_limit,
        "pages": [],
        "jobs_normalized": 0,
        "stop_reason": "not_started",
    }
    setattr(session, "hawksearch_v2_pagination", metadata)

    _validate_identity_page(session, target, headers, timeout)

    for page_no in range(1, max_pages + 1):
        if len(jobs) >= requested_limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        payload = {
            "ClientData": {
                "UserAgent": None,
                "VisitId": None,
                "VisitorId": None,
                "Custom": {"custom": target.locale or "en"},
            },
            "Keyword": "",
            "FacetSelections": {},
            "PageNo": page_no,
            "IndexName": target.index_name,
            "IgnoreSpellcheck": False,
            "IsInPreview": True,
            "ClientGuid": client_guid,
            "Is100CoverageTurnedOn": True,
            "MaxPerPage": page_size,
        }
        response = session.post(
            str(target.endpoint_url),
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        body = _parse_response(response, page_no)
        results, pagination = _parse_page(body, page_no, page_size)
        total = pagination["NofResults"]
        total_pages = pagination["NofPages"]
        if observed_total is None:
            observed_total = total
            observed_pages = total_pages
            metadata["initial_total_available"] = total
        elif total != observed_total or total_pages != observed_pages:
            raise AdapterError(
                "HawkSearch result bounds changed during pagination: "
                f"{observed_total}/{observed_pages} -> {total}/{total_pages}"
            )

        normalized_on_page = 0
        for result in results:
            job = _normalize(result, target)
            if job.job_id in seen_ids:
                raise AdapterError(f"HawkSearch repeated stable job ID: {job.job_id}")
            seen_ids.add(job.job_id)
            jobs.append(job)
            normalized_on_page += 1
            if len(jobs) >= requested_limit:
                break

        metadata["pages"].append(
            {
                "page_no": page_no,
                "jobs_returned": len(results),
                "jobs_normalized": normalized_on_page,
                "reported_total": total,
                "reported_pages": total_pages,
            }
        )
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["jobs_normalized"] = len(jobs)
        metadata["total_available"] = total

        if total == 0:
            metadata["stop_reason"] = "validated_zero_jobs"
            break
        if not results:
            raise AdapterError("HawkSearch page ended before its reported result bounds")
        if page_no >= total_pages:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if len(results) < page_size:
            raise AdapterError("HawkSearch returned a short page before NofPages")
        if len(jobs) >= requested_limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if page_no < max_pages:
            time.sleep(PAGE_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"

    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("careers_url", target.careers_url),
            ("endpoint_url", target.endpoint_url),
            ("endpoint_variant", target.endpoint_variant),
            ("client_guid", target.client_guid),
            ("identity_regex", target.identity_regex),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"HawkSearch target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"HawkSearch endpoint_variant must be {ENDPOINT_VARIANT}")
    if not CLIENT_GUID_RE.fullmatch(str(target.client_guid)):
        raise AdapterError("HawkSearch client_guid must be the public 32-hex client GUID")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"HawkSearch identity_regex is invalid: {exc}") from exc
    endpoint = urlparse(str(target.endpoint_url))
    endpoint_host = (endpoint.hostname or "").casefold()
    if (
        endpoint.scheme != "https"
        or not HAWKSEARCH_HOST_RE.fullmatch(endpoint_host)
        or endpoint.path != "/api/v2/search/"
        or endpoint.params
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError("HawkSearch endpoint_url must be an HTTPS v2 search endpoint")
    careers = urlparse(str(target.careers_url))
    careers_host = (careers.hostname or "").casefold()
    if careers.scheme != "https" or not careers_host:
        raise AdapterError("HawkSearch careers_url must be an observed HTTPS URL")
    source_hosts = _hosts(target.allowed_source_hosts)
    if source_hosts != {careers_host, endpoint_host}:
        raise AdapterError(
            "HawkSearch allowed_source_hosts must exactly bind the careers and API hosts"
        )
    if not _hosts(target.allowed_apply_hosts):
        raise AdapterError("HawkSearch target requires explicit allowed_apply_hosts")
    if any(not _host_name(host) for host in target.allowed_apply_hosts):
        raise AdapterError("HawkSearch allowed_apply_hosts contains an invalid host")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"HawkSearch page_size cannot exceed {MAX_PAGE_SIZE}")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"HawkSearch max_pages cannot exceed {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"HawkSearch max_jobs cannot exceed {MAX_RESULTS}")


def _validate_identity_page(
    session: requests.Session,
    target: CompanyTarget,
    headers: Mapping[str, str],
    timeout: int,
) -> None:
    response = session.get(
        str(target.careers_url),
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": headers["User-Agent"]},
        timeout=timeout,
    )
    status = getattr(response, "status_code", None)
    if status in {401, 407}:
        raise AdapterError("HawkSearch official careers page requires authentication")
    if status in {403, 429, 451}:
        raise AdapterError(f"HawkSearch official careers page is blocked (HTTP {status})")
    if status != 200:
        raise AdapterError(f"HawkSearch official careers page HTTP {status}")
    content = getattr(response, "content", b"")
    if not content or len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("HawkSearch official careers page is empty or oversized")
    requested = urlparse(str(target.careers_url))
    actual = urlparse(str(getattr(response, "url", "") or target.careers_url))
    if (
        actual.scheme != "https"
        or (actual.hostname or "").casefold() != (requested.hostname or "").casefold()
        or actual.path.rstrip("/") != requested.path.rstrip("/")
    ):
        raise AdapterError("HawkSearch official careers page redirected outside its identity URL")
    text = str(getattr(response, "text", "") or "")
    lowered = text.casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise AdapterError("HawkSearch official careers page returned a CAPTCHA, WAF, or login page")
    if not re.search(str(target.identity_regex), text, re.I):
        raise AdapterError("HawkSearch official careers page has ambiguous company identity")
    if str(target.client_guid).casefold() not in lowered:
        raise AdapterError("HawkSearch official careers page does not expose the configured client GUID")
    if str(target.endpoint_url).casefold() not in lowered:
        raise AdapterError("HawkSearch official careers page does not expose the configured endpoint")


def _parse_response(response: Any, page_no: int) -> Mapping[str, Any]:
    status = getattr(response, "status_code", None)
    if status in {401, 407}:
        raise AdapterError("HawkSearch authentication required")
    if status in {403, 429, 451}:
        raise AdapterError(f"HawkSearch endpoint blocked the request (HTTP {status})")
    if status != 200:
        raise AdapterError(f"HawkSearch HTTP {status} for page {page_no}")
    content = getattr(response, "content", b"")
    if content and len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("HawkSearch response exceeded the byte limit")
    text = str(getattr(response, "text", "") or "")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if "html" in content_type.casefold() or text.lstrip().startswith("<"):
        lowered = text.casefold()
        if any(marker in lowered for marker in BLOCKED_MARKERS):
            raise AdapterError("HawkSearch returned a CAPTCHA, WAF, or login page")
        raise AdapterError("HawkSearch returned HTML instead of JSON")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"HawkSearch returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("HawkSearch response must be a JSON object")
    return payload


def _parse_page(
    payload: Mapping[str, Any], page_no: int, page_size: int
) -> tuple[list[Mapping[str, Any]], Mapping[str, int]]:
    results = payload.get("Results")
    pagination = payload.get("Pagination")
    if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
        raise AdapterError("HawkSearch Results must be a result-object list")
    if not isinstance(pagination, Mapping):
        raise AdapterError("HawkSearch response is missing Pagination")
    for field in ("NofResults", "CurrentPage", "MaxPerPage", "NofPages"):
        value = pagination.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AdapterError(f"HawkSearch Pagination.{field} must be a non-negative integer")
    if pagination["CurrentPage"] != page_no:
        raise AdapterError("HawkSearch response did not honor PageNo")
    if pagination["MaxPerPage"] != page_size:
        raise AdapterError("HawkSearch response did not honor MaxPerPage")
    expected_pages = math.ceil(pagination["NofResults"] / page_size)
    if pagination["NofPages"] != expected_pages:
        raise AdapterError("HawkSearch NofPages is inconsistent with its result count")
    if len(results) > page_size:
        raise AdapterError("HawkSearch page exceeded MaxPerPage")
    if pagination["NofResults"] == 0 and results:
        raise AdapterError("HawkSearch zero-result response contained jobs")
    return list(results), pagination  # type: ignore[return-value]


def _normalize(result: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    document = result.get("Document")
    if not isinstance(document, Mapping):
        raise AdapterError("HawkSearch result is missing its Document object")
    job_id = _single(document, "jobrequisitionid")
    if not JOB_ID_RE.fullmatch(job_id):
        raise AdapterError("HawkSearch result is missing a stable requisition ID")
    doc_id = _clean(result.get("DocId"))
    if doc_id != job_id:
        raise AdapterError("HawkSearch DocId does not match jobrequisitionid")
    title = _single(document, "title")
    if not title:
        raise AdapterError("HawkSearch result is missing its title")
    locations = _strings(document.get("location"))
    if not locations:
        primary = _single(document, "primarylocation", required=False)
        locations = [primary] if primary else []
    if not locations:
        raise AdapterError("HawkSearch result is missing its location")
    detail_url = _job_url(
        _single(document, "link"),
        _hosts(target.allowed_source_hosts),
        job_id,
        "detail",
    )
    careers_host = (urlparse(str(target.careers_url)).hostname or "").casefold()
    if (urlparse(detail_url).hostname or "").casefold() != careers_host:
        raise AdapterError("HawkSearch detail URL is not on the configured careers host")
    apply_url = _job_url(
        _single(document, "externalapplyurl"),
        _hosts(target.allowed_apply_hosts),
        job_id,
        "apply",
    )
    category = _single(document, "category", required=False)
    family = _single(document, "jobfamily", required=False)
    description = _single(document, "jobdescription", required=False)
    endpoint_host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=apply_url,
        updated_at=_iso_date(_single(document, "created_moment", required=False)),
        source=f"hawksearch_v2:{endpoint_host}:{str(target.client_guid).casefold()}",
        departments=_unique([category, family]),
        offices=locations,
        description=_html_text(description) or None,
        raw_metadata={
            "platform": "hawksearch_v2",
            "endpoint_variant": ENDPOINT_VARIANT,
            "client_guid": target.client_guid,
            "detail_url": detail_url,
            "apply_url": apply_url,
            "doc_id": doc_id,
        },
    )


def _job_url(value: str, allowed_hosts: set[str], job_id: str, kind: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in allowed_hosts
        or job_id.casefold() not in parsed.path.casefold()
        or parsed.params
        or parsed.fragment
    ):
        raise AdapterError(f"HawkSearch result has no official job-specific {kind} URL")
    return urlunparse(parsed)


def _single(document: Mapping[str, Any], field: str, *, required: bool = True) -> str:
    value = document.get(field)
    if not isinstance(value, list) or len(value) != 1:
        if not required and (value is None or value == []):
            return ""
        raise AdapterError(f"HawkSearch Document.{field} must contain exactly one value")
    cleaned = _clean(value[0])
    if required and not cleaned:
        raise AdapterError(f"HawkSearch Document.{field} cannot be empty")
    return cleaned


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _unique([_clean(item) for item in value])


def _iso_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise AdapterError(f"HawkSearch created_moment is not ISO formatted: {value}") from exc


def _html_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:li|p|h[1-6]|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _hosts(values: list[str]) -> set[str]:
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _host_name(value: str) -> bool:
    return bool(re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", str(value), re.I))


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value.casefold() not in {item.casefold() for item in result}:
            result.append(value)
    return result
