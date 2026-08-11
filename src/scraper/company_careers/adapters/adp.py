"""Bounded adapters for ADP's two anonymous public recruiting contracts."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from html import unescape
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


WORKFORCE_NOW = "workforce_now_requisitions_v1"
MYJOBS = "myjobs_requisitions_v1"
SUPPORTED_VARIANTS = {WORKFORCE_NOW, MYJOBS}
MAX_PAGES = 5
MAX_PAGE_SIZE = 20
MAX_RESULTS = 100
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5_000_000
WFN_PAGE_SIZE = 10
WFN_PATH = "/mascsr/default/mdf/recruitment/recruitment.html"
WFN_API_PATH = "/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
WFN_IDENTITY_PATH = "/mascsr/default/careercenter/public/events/staffing/v1/content-links/career-center"
MYJOBS_HOST = "myjobs.adp.com"
MYJOBS_API = "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
BLOCKED_MARKERS = ("captcha", "access denied", "verify you are human", "challenge-platform")


def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> list[JobRecord]:
    """Fetch a deterministic public ADP prefix and fail on contract drift."""
    validate_target(target)
    timeout = min(MAX_TIMEOUT_SECONDS, max(1, int(timeout)))
    if target.endpoint_variant == WORKFORCE_NOW:
        return _fetch_workforce_now(session, target, timeout)
    return _fetch_myjobs(session, target, timeout)


def validate_target(target: CompanyTarget) -> None:
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"ADP endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}")
    for field in ("careers_url", "endpoint_url", "identity_regex"):
        if not str(getattr(target, field) or "").strip():
            raise AdapterError(f"ADP target is missing {field}")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"ADP max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"ADP max_jobs must be between 1 and {MAX_RESULTS}")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"ADP identity_regex is invalid: {exc}") from exc

    careers = urlparse(str(target.careers_url))
    endpoint = urlparse(str(target.endpoint_url))
    allowed = {_host(value) for value in target.allowed_apply_hosts}
    if target.endpoint_variant == WORKFORCE_NOW:
        _validate_wfn_target(target, careers, endpoint, allowed)
    else:
        _validate_myjobs_target(target, careers, endpoint, allowed)


def _validate_wfn_target(target: CompanyTarget, careers: Any, endpoint: Any, allowed: set[str]) -> None:
    host = (careers.hostname or "").casefold()
    if (
        careers.scheme != "https"
        or host not in {"workforcenow.adp.com", "workforcenow.cloud.adp.com"}
        or careers.path != WFN_PATH
        or careers.fragment
    ):
        raise AdapterError("ADP Workforce Now careers_url must be the observed public recruitment URL")
    query = parse_qs(careers.query, keep_blank_values=True)
    if set(query) != {"cid", "ccId", "lang"} or any(len(values) != 1 for values in query.values()):
        raise AdapterError("ADP Workforce Now careers_url must contain only one cid, ccId, and lang")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", query["cid"][0]):
        raise AdapterError("ADP Workforce Now cid must be a UUID")
    if not SAFE_ID.fullmatch(query["ccId"][0]) or query["lang"][0] != "en_US":
        raise AdapterError("ADP Workforce Now ccId or language is invalid")
    if (
        endpoint.scheme != "https"
        or (endpoint.hostname or "").casefold() != host
        or endpoint.path != WFN_API_PATH
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError("ADP Workforce Now endpoint_url must be the matching public requisitions endpoint")
    if int(target.page_size) != WFN_PAGE_SIZE:
        raise AdapterError(f"ADP Workforce Now page_size must be {WFN_PAGE_SIZE}")
    if allowed != {host}:
        raise AdapterError("ADP Workforce Now allowed_apply_hosts must contain only the recruitment host")


def _validate_myjobs_target(target: CompanyTarget, careers: Any, endpoint: Any, allowed: set[str]) -> None:
    slug = str(target.slug or "")
    if not SAFE_SLUG.fullmatch(slug):
        raise AdapterError("ADP MyJobs target requires a lowercase career-site slug")
    if (
        careers.scheme != "https"
        or (careers.hostname or "").casefold() != MYJOBS_HOST
        or careers.path.rstrip("/") != f"/{slug}/cx"
        or careers.query
        or careers.fragment
    ):
        raise AdapterError("ADP MyJobs careers_url must be the matching public /<slug>/cx URL")
    if urlunparse(endpoint) != MYJOBS_API:
        raise AdapterError("ADP MyJobs endpoint_url must be the observed public requisitions endpoint")
    if not SAFE_ID.fullmatch(str(target.organization_id or "")):
        raise AdapterError("ADP MyJobs target requires organization_id")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"ADP MyJobs page_size must be between 1 and {MAX_PAGE_SIZE}")
    if allowed != {MYJOBS_HOST}:
        raise AdapterError("ADP MyJobs allowed_apply_hosts must contain only myjobs.adp.com")


def _fetch_workforce_now(
    session: requests.Session, target: CompanyTarget, timeout: int
) -> list[JobRecord]:
    careers = urlparse(str(target.careers_url))
    query = {key: values[0] for key, values in parse_qs(careers.query).items()}
    base = urlunparse(careers._replace(path="", query="", fragment="")).rstrip("/")
    common = {"cid": query["cid"], "ccId": query["ccId"], "lang": "en_US", "locale": "en_US"}
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en_US",
        "Content-Type": "application/json",
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        "X-Requested-With": "XMLHttpRequest",
        "locale": "en_US",
        "x-forwarded-host": careers.hostname or "",
    }
    identity = _get_json(
        session, base + WFN_IDENTITY_PATH, "Workforce Now career-center identity",
        timeout, params=common, headers=headers,
    )
    links = identity.get("contentLinks") if isinstance(identity, Mapping) else None
    if not isinstance(links, list) or not links:
        raise AdapterError("ADP Workforce Now identity response is missing contentLinks")
    identity_text = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    if not re.search(str(target.identity_regex), identity_text, re.I):
        raise AdapterError("ADP Workforce Now tenant identity did not match identity_regex")

    limit = int(target.max_jobs)
    page_size = int(target.page_size)
    jobs: list[JobRecord] = []
    seen: set[str] = set()
    total: int | None = None
    metadata = _metadata(target, "GET", page_size, limit)
    setattr(session, "adp_pagination", metadata)
    for page in range(int(target.max_pages)):
        if len(jobs) >= limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        offset = page * page_size
        params = {**common, "$skip": offset, "$top": page_size, "userQuery": ""}
        payload = _get_json(session, str(target.endpoint_url), f"Workforce Now offset={offset}", timeout, params=params, headers=headers)
        rows, current_total = _wfn_listing(payload, offset, page_size)
        total = _stable_total(total, current_total, "ADP Workforce Now")
        for row in rows:
            item_id = _stable_id(row.get("itemID"), "ADP Workforce Now itemID")
            if item_id in seen:
                raise AdapterError(f"ADP Workforce Now repeated stable job ID: {item_id}")
            detail_url = f"{target.endpoint_url}/{item_id}"
            detail = _get_json(session, detail_url, f"Workforce Now detail {item_id}", timeout, params=common, headers=headers)
            job = _normalize_wfn(row, detail, target, query, detail_url)
            seen.add(item_id)
            jobs.append(job)
            if len(jobs) >= limit:
                break
        metadata["pages"].append({"offset": offset, "jobs_returned": len(rows)})
        metadata["jobs_normalized"] = len(jobs)
        if offset + len(rows) >= current_total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if not rows:
            raise AdapterError("ADP Workforce Now ended before its reported total")
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    metadata["total_available"] = total
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    return jobs


def _fetch_myjobs(session: requests.Session, target: CompanyTarget, timeout: int) -> list[JobRecord]:
    slug = str(target.slug)
    config_url = f"https://{MYJOBS_HOST}/public/staffing/v1/career-site/{slug}"
    config = _get_json(
        session, config_url, "MyJobs career-site configuration", timeout,
        headers={"Accept": "application/json", "Referer": f"https://{MYJOBS_HOST}/"},
    )
    if not isinstance(config, Mapping):
        raise AdapterError("ADP MyJobs career-site configuration must be an object")
    if config.get("domain") != slug or config.get("orgoid") != target.organization_id:
        raise AdapterError("ADP MyJobs career-site identity or organization changed")
    if config.get("active") is not True or not re.fullmatch(str(target.identity_regex), str(config.get("clientName") or ""), re.I):
        raise AdapterError("ADP MyJobs client identity did not match the active career site")
    settings = config.get("settings")
    token = config.get("myJobsToken")
    if not isinstance(settings, Mapping) or settings.get("careerSiteType") != "External" or not isinstance(token, str) or len(token) < 20:
        raise AdapterError("ADP MyJobs public configuration is malformed")

    headers = {
        "Accept": "application/json",
        "Origin": f"https://{MYJOBS_HOST}",
        "Referer": f"https://{MYJOBS_HOST}/",
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        "myjobstoken": token,
        "orgoid": str(target.organization_id),
    }
    select = (
        "reqId,jobTitle,publishedJobTitle,type,jobDescription,jobQualifications,"
        "workLevelCode,clientRequisitionID,postingDate,requisitionLocations,"
        "easyApplyEnabled,careerSiteDomains,canApply"
    )
    page_size = int(target.page_size)
    limit = int(target.max_jobs)
    jobs: list[JobRecord] = []
    seen: set[str] = set()
    total: int | None = None
    metadata = _metadata(target, "GET", page_size, limit)
    setattr(session, "adp_pagination", metadata)
    for page in range(int(target.max_pages)):
        if len(jobs) >= limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        offset = page * page_size
        params = {
            "$orderby": "postingDate desc", "$select": select, "$top": page_size,
            "$skip": offset, "tz": "UTC",
        }
        payload = _get_json(session, str(target.endpoint_url), f"MyJobs offset={offset}", timeout, params=params, headers=headers)
        rows, current_total = _myjobs_listing(payload, page_size, offset)
        total = _stable_total(total, current_total, "ADP MyJobs")
        for row in rows:
            req_id = _stable_id(row.get("reqId"), "ADP MyJobs reqId")
            if req_id in seen:
                raise AdapterError(f"ADP MyJobs repeated stable job ID: {req_id}")
            jobs.append(_normalize_myjobs(row, target))
            seen.add(req_id)
            if len(jobs) >= limit:
                break
        metadata["pages"].append({"offset": offset, "jobs_returned": len(rows)})
        metadata["jobs_normalized"] = len(jobs)
        if offset + len(rows) >= current_total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if not rows:
            raise AdapterError("ADP MyJobs ended before its reported total")
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    metadata["total_available"] = total
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    return jobs


def _wfn_listing(payload: Any, offset: int, page_size: int) -> tuple[list[Mapping[str, Any]], int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobRequisitions"), list) or not isinstance(payload.get("meta"), Mapping):
        raise AdapterError("ADP Workforce Now listing response is malformed")
    meta = payload["meta"]
    total = _nonnegative_int(meta.get("totalNumber"), "ADP Workforce Now totalNumber")
    if meta.get("startSequence") != offset:
        raise AdapterError("ADP Workforce Now startSequence did not match the requested offset")
    rows = payload["jobRequisitions"]
    if len(rows) > page_size or any(not isinstance(row, Mapping) for row in rows):
        raise AdapterError("ADP Workforce Now page exceeded its bound or contained a malformed row")
    return rows, total


def _myjobs_listing(payload: Any, page_size: int, offset: int) -> tuple[list[Mapping[str, Any]], int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobRequisitions"), list):
        raise AdapterError("ADP MyJobs listing response is malformed")
    total = _nonnegative_int(payload.get("count"), "ADP MyJobs count")
    rows = payload["jobRequisitions"]
    if len(rows) > page_size or any(not isinstance(row, Mapping) for row in rows):
        raise AdapterError("ADP MyJobs page exceeded its bound or contained a malformed row")
    if total > offset and not rows:
        raise AdapterError("ADP MyJobs returned an empty page before its reported total")
    return rows, total


def _normalize_wfn(
    listing: Mapping[str, Any], detail: Any, target: CompanyTarget,
    query: Mapping[str, str], api_detail_url: str,
) -> JobRecord:
    if not isinstance(detail, Mapping):
        raise AdapterError("ADP Workforce Now detail response must be an object")
    item_id = _stable_id(listing.get("itemID"), "ADP Workforce Now itemID")
    if detail.get("itemID") != item_id:
        raise AdapterError("ADP Workforce Now detail itemID did not match the listing")
    title = _required_text(listing.get("requisitionTitle"), "ADP Workforce Now title")
    if _required_text(detail.get("requisitionTitle"), "ADP Workforce Now detail title") != title:
        raise AdapterError("ADP Workforce Now detail title did not match the listing")
    external_id = _wfn_external_id(listing)
    if _wfn_external_id(detail) != external_id:
        raise AdapterError("ADP Workforce Now external job ID changed in the detail response")
    location = _locations(listing.get("requisitionLocations"))
    if _locations(detail.get("requisitionLocations")) != location:
        raise AdapterError("ADP Workforce Now detail locations did not match the listing")
    apply_query = urlencode({"cid": query["cid"], "ccId": query["ccId"], "jobId": external_id, "lang": "en_US"})
    careers = urlparse(str(target.careers_url))
    apply_url = urlunparse(careers._replace(query=apply_query, fragment=""))
    return JobRecord(
        job_id=f"adp:{(careers.hostname or '').casefold()}:{query['cid'].casefold()}:{item_id}",
        title=title, company=target.name, location=location, url=apply_url,
        updated_at=_date_value(listing.get("postDate")),
        source=f"adp:{(careers.hostname or '').casefold()}:{query['cid'].casefold()}",
        description=_html_text(detail.get("requisitionDescription")),
        raw_metadata={
            "platform": "adp", "endpoint_variant": WORKFORCE_NOW,
            "item_id": item_id, "external_job_id": external_id,
            "detail_url": apply_url, "apply_url": apply_url,
            "api_detail_url": api_detail_url,
        },
    )


def _normalize_myjobs(row: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    req_id = _stable_id(row.get("reqId"), "ADP MyJobs reqId")
    title = _required_text(row.get("publishedJobTitle") or row.get("jobTitle"), "ADP MyJobs title")
    domains = row.get("careerSiteDomains")
    if not isinstance(domains, list) or domains != [target.slug]:
        raise AdapterError("ADP MyJobs row crossed or ambiguously attributed career-site domains")
    if row.get("canApply") is not True:
        raise AdapterError("ADP MyJobs row did not expose an official apply action")
    location = _locations(row.get("requisitionLocations"))
    detail_url = f"https://{MYJOBS_HOST}/{target.slug}/cx/job-details?{urlencode({'reqId': req_id})}"
    return JobRecord(
        job_id=f"adp:{str(target.organization_id).casefold()}:{req_id}",
        title=title, company=target.name, location=location, url=detail_url,
        updated_at=_date_value(row.get("postingDate")),
        source=f"adp:{str(target.organization_id).casefold()}:{str(target.slug).casefold()}",
        description=_html_text(row.get("jobDescription")),
        raw_metadata={
            "platform": "adp", "endpoint_variant": MYJOBS,
            "req_id": req_id, "client_requisition_id": str(row.get("clientRequisitionID") or ""),
            "detail_url": detail_url, "apply_url": detail_url,
        },
    )


def _get_json(session: requests.Session, url: str, label: str, timeout: int, **kwargs: Any) -> Any:
    response = session.get(url, timeout=timeout, **kwargs)
    if response.status_code != 200:
        raise AdapterError(f"ADP {label} returned HTTP {response.status_code}")
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError(f"ADP {label} exceeded the response-size bound")
    text = str(getattr(response, "text", "") or "")
    lowered = text.casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise AdapterError(f"ADP {label} returned CAPTCHA or blocked content")
    try:
        return response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"ADP {label} was not valid JSON") from exc


def _locations(value: Any) -> str:
    if not isinstance(value, list) or not value:
        raise AdapterError("ADP job is missing requisition locations")
    locations: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AdapterError("ADP requisition location is malformed")
        name = item.get("nameCode")
        text = str(name.get("longName") or name.get("shortName") or "").strip() if isinstance(name, Mapping) else ""
        if not text:
            address = item.get("address")
            if not isinstance(address, Mapping):
                raise AdapterError("ADP requisition location has no stable display value")
            subdivision = address.get("countrySubdivisionLevel1")
            country = address.get("country")
            parts = [
                address.get("cityName"),
                subdivision.get("longName") or subdivision.get("codeValue") if isinstance(subdivision, Mapping) else None,
                country.get("longName") or country.get("codeValue") if isinstance(country, Mapping) else None,
            ]
            text = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        if not text:
            raise AdapterError("ADP requisition location has no stable display value")
        if text not in locations:
            locations.append(text)
    return " | ".join(locations)


def _wfn_external_id(item: Mapping[str, Any]) -> str:
    group = item.get("customFieldGroup")
    fields = group.get("stringFields") if isinstance(group, Mapping) else None
    matches: list[str] = []
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, Mapping):
                continue
            name = field.get("nameCode")
            if isinstance(name, Mapping) and name.get("codeValue") == "ExternalJobID":
                matches.append(str(field.get("stringValue") or "").strip())
    if len(matches) != 1 or not SAFE_ID.fullmatch(matches[0]):
        raise AdapterError("ADP Workforce Now job has no unambiguous ExternalJobID")
    return matches[0]


def _metadata(target: CompanyTarget, method: str, page_size: int, limit: int) -> dict[str, Any]:
    return {
        "endpoint_variant": target.endpoint_variant, "request_method": method,
        "page_size": page_size, "maximum_pages": int(target.max_pages),
        "maximum_results": limit, "pages": [], "jobs_normalized": 0,
        "stop_reason": "not_started",
    }


def _stable_total(previous: int | None, current: int, label: str) -> int:
    if previous is not None and previous != current:
        raise AdapterError(f"{label} total changed during pagination: {previous} -> {current}")
    return current


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(f"{label} must be a non-negative integer")
    return value


def _stable_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise AdapterError(f"{label} is missing or invalid")
    return text


def _required_text(value: Any, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise AdapterError(f"{label} is missing")
    return text


def _date_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError as exc:
            raise AdapterError("ADP posting date is malformed") from exc


def _html_text(value: Any) -> str | None:
    text = re.sub(r"<[^>]+>", " ", unescape(str(value or "")))
    cleaned = " ".join(text.split())
    return cleaned or None


def _host(value: Any) -> str:
    text = str(value or "").strip().casefold().rstrip(".")
    return (urlparse(text).hostname or text).casefold().rstrip(".")
