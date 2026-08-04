"""Oracle Fusion Cloud Recruiting Candidate Experience adapter."""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, List, Mapping, Optional
from urllib.parse import quote, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PAGE_SIZE = 24
MAX_PAGES = 5
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
PAGE_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
EXPECTED_ENDPOINT_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
EXPAND = "requisitionList.secondaryLocations"
SORT_BY = "POSTING_DATES_DESC"


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    """Fetch a bounded, deterministically paginated Oracle job collection."""
    validate_target(target)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    max_pages = max(1, int(target.max_pages))
    if max_pages > MAX_PAGES:
        raise AdapterError(f"Oracle Recruiting max_pages cannot exceed {MAX_PAGES}")

    jobs: List[JobRecord] = []
    seen_ids: set[str] = set()
    expected_total: Optional[int] = None

    for page_index in range(max_pages):
        offset = page_index * PAGE_SIZE
        if len(jobs) >= MAX_RESULTS:
            break
        response = _get_page(session, target, offset, timeout)
        envelope = _parse_envelope(response, target, offset)
        page_jobs = envelope["requisitionList"]
        total = envelope["TotalJobsCount"]
        if expected_total is None:
            expected_total = total
        elif total < 0:
            raise AdapterError("Oracle Recruiting TotalJobsCount cannot be negative")

        for item in page_jobs:
            job = _normalize_job(item, target)
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
            if len(jobs) >= MAX_RESULTS:
                break

        if offset + len(page_jobs) >= total or len(page_jobs) < PAGE_SIZE:
            break
        if page_index + 1 < max_pages:
            time.sleep(PAGE_DELAY_SECONDS)

    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [
        field
        for field, value in (
            ("careers_url", target.careers_url),
            ("endpoint_url", target.endpoint_url),
            ("site_number", target.site_number),
            ("locale", target.locale),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"Oracle Recruiting target is missing {', '.join(missing)}")

    endpoint = urlparse(str(target.endpoint_url))
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or not endpoint.hostname.casefold().endswith(".oraclecloud.com")
        or endpoint.path.rstrip("/") != EXPECTED_ENDPOINT_PATH
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError("Oracle Recruiting endpoint_url is not a supported public Oracle endpoint")

    careers = urlparse(str(target.careers_url))
    path_parts = [part for part in careers.path.split("/") if part]
    locale = str(target.locale).strip()
    if (
        careers.scheme != "https"
        or not careers.hostname
        or len(path_parts) < 4
        or path_parts[-1] not in {"jobs", "requisitions"}
        or "sites" not in path_parts
        or locale not in path_parts
    ):
        raise AdapterError("Oracle Recruiting careers_url must be an observed locale/site jobs URL")


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    offset: int,
    timeout: int,
) -> Any:
    finder_parts = [
        f"siteNumber={target.site_number}",
        "facetsList=NONE",
        f"limit={PAGE_SIZE}",
        f"offset={offset}",
        f"sortBy={SORT_BY}",
    ]
    if target.organization_id:
        finder_parts.append(f"organizationId={target.organization_id}")
    params = {
        "onlyData": "true",
        "expand": EXPAND,
        "finder": "findReqs;" + ",".join(finder_parts),
    }
    locale = str(target.locale)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Accept-Language": locale,
        "Ora-Irc-Language": locale,
    }

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                str(target.endpoint_url),
                params=params,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code != 200:
            raise AdapterError(
                f"Oracle Recruiting HTTP {response.status_code} for {target.name} "
                f"site={target.site_number} offset={offset}"
            )
        return response
    raise AdapterError(f"Oracle Recruiting request attempts exhausted for {target.name}")


def _parse_envelope(response: Any, target: CompanyTarget, offset: int) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"Oracle Recruiting returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("Oracle Recruiting response must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
        raise AdapterError("Oracle Recruiting response must contain one search envelope in items")
    envelope = items[0]
    requisitions = envelope.get("requisitionList")
    if not isinstance(requisitions, list):
        raise AdapterError("Oracle Recruiting search envelope is missing requisitionList")
    for field in ("TotalJobsCount", "Offset", "Limit", "SiteNumber"):
        if field not in envelope:
            raise AdapterError(f"Oracle Recruiting search envelope is missing {field}")
    if not isinstance(envelope["TotalJobsCount"], int) or envelope["TotalJobsCount"] < 0:
        raise AdapterError("Oracle Recruiting TotalJobsCount must be a non-negative integer")
    if envelope["Offset"] != offset:
        raise AdapterError(
            f"Oracle Recruiting returned offset {envelope['Offset']} for requested offset {offset}"
        )
    if envelope["Limit"] != PAGE_SIZE or not 0 <= len(requisitions) <= PAGE_SIZE:
        raise AdapterError("Oracle Recruiting page exceeded the configured result limit")
    if str(envelope["SiteNumber"]) != str(target.site_number):
        raise AdapterError("Oracle Recruiting response site number does not match configuration")
    if envelope["TotalJobsCount"] == 0 and requisitions:
        raise AdapterError("Oracle Recruiting zero-job envelope unexpectedly contained requisitions")
    if envelope["TotalJobsCount"] > offset and not requisitions:
        raise AdapterError("Oracle Recruiting response ended before its reported total job count")
    return envelope


def _normalize_job(item: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(item, Mapping):
        raise AdapterError("Oracle Recruiting requisition must be a JSON object")
    job_id = str(item.get("Id") or "").strip()
    title = str(item.get("Title") or "").strip()
    if not job_id or not title:
        raise AdapterError("Oracle Recruiting requisition is missing stable Id or Title")

    locations = _locations(item)
    departments = _unique_strings(
        item.get(field) for field in ("Department", "JobFunction", "JobFamily", "Organization")
    )
    descriptions = _unique_strings(
        item.get(field)
        for field in ("ShortDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
    )
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=_apply_url(str(target.careers_url), job_id),
        updated_at=_normalize_posted_date(item.get("PostedDate")),
        source=_source(target),
        departments=departments,
        offices=locations,
        description="\n\n".join(descriptions) or None,
        raw_metadata={"platform": "oracle_recruiting", "requisition": dict(item)},
    )


def _locations(item: Mapping[str, Any]) -> List[str]:
    values: list[Any] = [item.get("PrimaryLocation")]
    secondary = item.get("secondaryLocations") or []
    if not isinstance(secondary, list):
        raise AdapterError("Oracle Recruiting secondaryLocations must be a list")
    for location in secondary:
        if isinstance(location, Mapping):
            values.append(location.get("Name"))
        elif isinstance(location, str):
            values.append(location)
        else:
            raise AdapterError("Oracle Recruiting secondary location has an unsupported shape")
    return _unique_strings(values)


def _unique_strings(values: Any) -> List[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _normalize_posted_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise AdapterError(f"Oracle Recruiting PostedDate is not ISO formatted: {text}") from exc


def _apply_url(careers_url: str, job_id: str) -> str:
    parsed = urlparse(careers_url)
    parts = parsed.path.rstrip("/").split("/")
    if not parts or parts[-1] not in {"jobs", "requisitions"}:
        raise AdapterError("Oracle Recruiting careers_url cannot construct an apply URL")
    parts[-1:] = ["job", quote(job_id, safe="")]
    return urlunparse(parsed._replace(path="/".join(parts), query="", fragment=""))


def _source(target: CompanyTarget) -> str:
    host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    return f"oracle_recruiting:{host}:{str(target.site_number).casefold()}"
