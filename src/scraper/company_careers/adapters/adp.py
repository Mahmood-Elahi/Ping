"""Bounded adapters for ADP's two anonymous public recruiting contracts."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from html import unescape
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from src.scraper.company_careers.diagnostics import structure_diagnostic
from src.scraper.company_careers.models import (
    AdapterError,
    AdapterResult,
    CompanyTarget,
    InventoryItem,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    finish_authoritative_total,
)


WORKFORCE_NOW = "workforce_now_requisitions_v1"
MYJOBS = "myjobs_requisitions_v1"
SUPPORTED_VARIANTS = {WORKFORCE_NOW, MYJOBS}
MAX_PAGES = 5
MAX_PAGE_SIZE = 20
MAX_RESULTS = 100
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5_000_000
WFN_PAGE_SIZE = 10
WFN_LOCALES = {"en_CA", "en_US"}
WFN_PATH = "/mascsr/default/mdf/recruitment/recruitment.html"
WFN_API_PATH = "/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
WFN_IDENTITY_PATH = "/mascsr/default/careercenter/public/events/staffing/v1/content-links/career-center"
MYJOBS_HOST = "myjobs.adp.com"
MYJOBS_API = "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
BLOCKED_MARKERS = ("captcha", "access denied", "verify you are human", "challenge-platform")
MAX_REJECTED_RECORD_IDS = 20


class _MissingStableLocationError(AdapterError):
    """Raised only when a syntactically valid location shape has no stable display."""


class _WfnIdentityValidatedMissingLocationError(AdapterError):
    """Raised after a Workforce Now record's identity contract is fully validated."""

    def __init__(self, item_id: str) -> None:
        super().__init__(
            f"ADP Workforce Now record {item_id} has no stable usable location"
        )
        self.item_id = item_id


@explicit_adapter_result
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
    if not SAFE_ID.fullmatch(query["ccId"][0]) or query["lang"][0] not in WFN_LOCALES:
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
    locale = query["lang"]
    common = {"cid": query["cid"], "ccId": query["ccId"], "lang": locale, "locale": locale}
    headers = {
        "Accept": "application/json",
        "Accept-Language": locale,
        "Content-Type": "application/json",
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        "X-Requested-With": "XMLHttpRequest",
        "locale": locale,
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
    metadata.update(
        rejected_record_count=0,
        rejected_record_ids=[],
        rejected_record_reason=None,
    )
    setattr(session, "adp_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(int(target.max_pages), limit)
    listing_budget.record(metadata)
    for page in range(listing_budget.hard_request_ceiling):
        effective_limit = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        offset = page * page_size
        params = {**common, "$skip": offset, "$top": page_size, "userQuery": ""}
        metadata["pages_requested"] += 1
        payload = _get_json(session, str(target.endpoint_url), f"Workforce Now offset={offset}", timeout, params=params, headers=headers)
        rows, current_total = _wfn_listing(payload, offset, page_size)
        metadata["pages_completed"] += 1
        total = _stable_total(total, current_total, "ADP Workforce Now")
        listing_budget.record_decision(
            metadata,
            current_total,
            page_size,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=offset + len(rows),
            detail_requests_coupled=True,
        )
        effective_limit = int(metadata["effective_listing_job_ceiling"])
        for row in rows:
            item_id = _stable_id(row.get("itemID"), "ADP Workforce Now itemID")
            if item_id in seen:
                raise AdapterError(f"ADP Workforce Now repeated stable job ID: {item_id}")
            seen.add(item_id)
            detail_url = f"{target.endpoint_url}/{item_id}"
            detail = _get_json(session, detail_url, f"Workforce Now detail {item_id}", timeout, params=common, headers=headers)
            try:
                job = _normalize_wfn(row, detail, target, query, detail_url)
            except _WfnIdentityValidatedMissingLocationError as exc:
                rejected_count = int(metadata["rejected_record_count"]) + 1
                metadata["rejected_record_count"] = rejected_count
                metadata["malformed_records"] = rejected_count
                rejected_ids = metadata["rejected_record_ids"]
                if len(rejected_ids) < MAX_REJECTED_RECORD_IDS:
                    rejected_ids.append(exc.item_id)
                metadata["rejected_record_reason"] = "missing_stable_location"
                continue
            jobs.append(job)
            if len(jobs) >= effective_limit:
                break
        metadata["pages"].append({"offset": offset, "jobs_returned": len(rows)})
        metadata["jobs_normalized"] = len(jobs)
        if offset + len(rows) >= current_total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if not rows:
            raise AdapterError("ADP Workforce Now ended before its reported total")
        if page + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    metadata["total_available"] = total
    metadata["authoritative_total_present"] = total is not None
    _finalize_metadata(metadata)
    if metadata["rejected_record_count"] and not jobs:
        raise AdapterError(
            "ADP Workforce Now had no valid jobs after rejecting "
            "identity-validated records without stable locations"
        )
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    return jobs


def _fetch_myjobs(
    session: requests.Session, target: CompanyTarget, timeout: int
) -> list[JobRecord] | AdapterResult:
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
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(
        int(target.max_pages),
        limit,
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)
    for page in range(listing_budget.hard_request_ceiling):
        effective_limit = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        offset = page * page_size
        params = {
            "$orderby": "postingDate desc", "$select": select, "$top": page_size,
            "$skip": offset, "tz": "UTC",
        }
        metadata["pages_requested"] += 1
        try:
            payload = _get_json(
                session,
                str(target.endpoint_url),
                f"MyJobs offset={offset}",
                timeout,
                params=params,
                headers=headers,
            )
        except requests.RequestException:
            metadata["stop_reason"] = "request_failure"
            metadata["scan_completeness_reason"] = "pagination_failure"
            metadata["source_identity_status"] = "validated"
            metadata["total_available"] = total
            if jobs:
                return AdapterResult(
                    jobs=jobs,
                    scan=metadata,
                    inventory_items=tuple(
                        InventoryItem(job.job_id, job.source) for job in jobs
                    ),
                    capabilities_used=frozenset({"inventory", "partial_progress"}),
                )
            raise
        rows, current_total = _myjobs_listing(payload, page_size, offset)
        metadata["pages_completed"] += 1
        total = _stable_total(total, current_total, "ADP MyJobs")
        listing_budget.record_decision(
            metadata,
            current_total,
            page_size,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=offset + len(rows),
        )
        effective_limit = int(metadata["effective_listing_job_ceiling"])
        for row in rows:
            req_id = _stable_id(row.get("reqId"), "ADP MyJobs reqId")
            if req_id in seen:
                raise AdapterError(f"ADP MyJobs repeated stable job ID: {req_id}")
            jobs.append(_normalize_myjobs(row, target))
            seen.add(req_id)
            if len(jobs) >= effective_limit:
                break
        metadata["pages"].append({"offset": offset, "jobs_returned": len(rows)})
        metadata["jobs_normalized"] = len(jobs)
        if offset + len(rows) >= current_total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if not rows:
            raise AdapterError("ADP MyJobs ended before its reported total")
        if page + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    metadata["total_available"] = total
    metadata["authoritative_total_present"] = total is not None
    _finalize_metadata(metadata)
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    return jobs


def _wfn_listing(payload: Any, offset: int, page_size: int) -> tuple[list[Mapping[str, Any]], int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobRequisitions"), list):
        raise AdapterError("ADP Workforce Now listing response is malformed")
    rows = payload["jobRequisitions"]
    # Workforce Now omits ``meta`` for the exact zero-inventory response used
    # by some public tenants.  Accept only the unambiguous first-page envelope;
    # non-empty or later pages still require the normal total/offset metadata.
    if not isinstance(payload.get("meta"), Mapping):
        if offset == 0 and not rows and set(payload) == {"jobRequisitions"}:
            return [], 0
        raise AdapterError("ADP Workforce Now listing response is malformed")
    meta = payload["meta"]
    total = _nonnegative_int(meta.get("totalNumber"), "ADP Workforce Now totalNumber")
    if meta.get("startSequence") != offset:
        raise AdapterError("ADP Workforce Now startSequence did not match the requested offset")
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
    apply_query = urlencode(
        {"cid": query["cid"], "ccId": query["ccId"], "jobId": external_id, "lang": query["lang"]}
    )
    careers = urlparse(str(target.careers_url))
    apply_url = urlunparse(careers._replace(query=apply_query, fragment=""))
    if (
        urlparse(apply_url).scheme != "https"
        or _host(apply_url) not in {_host(value) for value in target.allowed_apply_hosts}
    ):
        raise AdapterError("ADP Workforce Now official apply URL is not allow-listed")
    locations = _matching_wfn_locations(listing, detail, item_id)
    location = " | ".join(locations)
    return JobRecord(
        job_id=f"adp:{(careers.hostname or '').casefold()}:{query['cid'].casefold()}:{item_id}",
        title=title, company=target.name, location=location, url=apply_url,
        updated_at=_date_value(listing.get("postDate")),
        source=f"adp:{(careers.hostname or '').casefold()}:{query['cid'].casefold()}",
        offices=locations,
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
    locations = _location_values(row.get("requisitionLocations"))
    location = " | ".join(locations)
    detail_url = f"https://{MYJOBS_HOST}/{target.slug}/cx/job-details?{urlencode({'reqId': req_id})}"
    return JobRecord(
        job_id=f"adp:{str(target.organization_id).casefold()}:{req_id}",
        title=title, company=target.name, location=location, url=detail_url,
        updated_at=_date_value(row.get("postingDate")),
        source=f"adp:{str(target.organization_id).casefold()}:{str(target.slug).casefold()}",
        offices=locations,
        description=_html_text(row.get("jobDescription")),
        raw_metadata={
            "platform": "adp", "endpoint_variant": MYJOBS,
            "req_id": req_id, "client_requisition_id": str(row.get("clientRequisitionID") or ""),
            "detail_url": detail_url, "apply_url": detail_url,
        },
    )


def _get_json(session: requests.Session, url: str, label: str, timeout: int, **kwargs: Any) -> Any:
    response = session.get(url, timeout=timeout, **kwargs)
    # Legacy Workforce Now tenants can be redirected by ADP's edge to a
    # branded maintenance page that deliberately returns HTTP 200.  This is
    # neither a career-center configuration variant nor malformed tenant JSON;
    # identify only the exact observed ADP-owned maintenance contract so the
    # scanner can report a transient upstream failure without accepting HTML.
    requested_host = (urlparse(url).hostname or "").casefold()
    resolved = urlparse(str(getattr(response, "url", "") or ""))
    if (
        requested_host.endswith(".adp.com")
        and (resolved.hostname or "").casefold() == "sorry.adp.com"
        and re.fullmatch(r"/maintenance/[A-Za-z0-9]+/?", resolved.path)
    ):
        raise AdapterError(f"ADP {label} returned transient upstream maintenance redirect")
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
    return " | ".join(_location_values(value))


def _location_values(value: Any) -> list[str]:
    """Return ADP's stable location displays in deterministic precedence order."""
    if value is None or value == []:
        raise _MissingStableLocationError(
            "ADP job is missing requisition locations "
            f"({_adp_location_diagnostic(value)})"
        )
    if not isinstance(value, list):
        raise AdapterError(
            "ADP requisition locations are malformed "
            f"({_adp_location_diagnostic(value)})"
        )
    locations: list[str] = []
    try:
        for item in value:
            if not isinstance(item, Mapping):
                raise AdapterError("ADP requisition location is malformed")
            name = item.get("nameCode")
            if name is not None and not isinstance(name, Mapping):
                raise AdapterError("ADP requisition location nameCode is malformed")
            text = _first_text(name, ("longName", "shortName"), "nameCode")
            if not text:
                text = _first_text(
                    item,
                    ("formattedAddress", "displayName", "locationName"),
                    "location display",
                )
            if not text:
                text = _structured_adp_location(item, "location")
            if not text and item.get("workLocation") is not None:
                work_location = item.get("workLocation")
                if not isinstance(work_location, Mapping):
                    raise AdapterError("ADP requisition workLocation is malformed")
                text = _first_text(
                    work_location,
                    ("formattedAddress", "displayName", "locationName", "name"),
                    "workLocation",
                ) or _structured_adp_location(work_location, "workLocation")
            # Some Workforce Now tenants append empty location placeholders after
            # one or more real locations. Ignore only those empty objects; the
            # collection still fails closed below if no stable location remains.
            if not text:
                continue
            if text.casefold() not in {location.casefold() for location in locations}:
                locations.append(text)
    except AdapterError as exc:
        raise AdapterError(f"{exc} ({_adp_location_diagnostic(value)})") from exc
    if not locations:
        raise _MissingStableLocationError(
            "ADP requisition location has no stable display value "
            f"({_adp_location_diagnostic(value)})"
        )
    return locations


def _matching_wfn_locations(
    listing: Mapping[str, Any], detail: Mapping[str, Any], item_id: str
) -> list[str]:
    """Validate listing/detail location agreement before classifying locationless rows."""
    listing_locations: list[str] | None
    detail_locations: list[str] | None
    try:
        listing_locations = _location_values(listing.get("requisitionLocations"))
    except _MissingStableLocationError:
        listing_locations = None
    try:
        detail_locations = _location_values(detail.get("requisitionLocations"))
    except _MissingStableLocationError:
        detail_locations = None
    if listing_locations != detail_locations:
        raise AdapterError("ADP Workforce Now detail locations did not match the listing")
    if listing_locations is None:
        if listing.get("requisitionLocations") != detail.get("requisitionLocations"):
            raise AdapterError("ADP Workforce Now detail locations did not match the listing")
        raise _WfnIdentityValidatedMissingLocationError(item_id)
    return listing_locations


def _structured_adp_location(value: Mapping[str, Any], label: str) -> str:
    """Build a display from only semantically explicit ADP geography fields."""
    address = value.get("address")
    if address is not None and not isinstance(address, Mapping):
        raise AdapterError(f"ADP requisition {label}.address is malformed")
    source = address if isinstance(address, Mapping) else value
    line_one = _first_text_value(
        source, ("addressLineOne", "addressLine1", "lineOne"), f"{label}.address line"
    )
    city = _first_geographic_component(
        source, ("cityName", "city"), f"{label}.city"
    )
    subdivision_field, subdivision_value = next(
        (
            (field, source.get(field))
            for field in ("countrySubdivisionLevel1", "stateProvince", "stateCode", "state")
            if source.get(field) is not None
        ),
        ("countrySubdivisionLevel1", None),
    )
    subdivision = _geographic_component(
        subdivision_value, f"{label}.{subdivision_field}"
    )
    postal_value = next(
        (
            source.get(field)
            for field in ("postalCode", "zipCode")
            if source.get(field) is not None
        ),
        None,
    )
    postal = _geographic_component(postal_value, f"{label}.postalCode")
    country_value = next(
        (
            source.get(field)
            for field in ("country", "countryCode", "isoCountryCode")
            if source.get(field) is not None
        ),
        None,
    )
    country = _geographic_component(country_value, f"{label}.country")
    parts = [part for part in (line_one, city, subdivision) if part]
    if postal:
        if parts:
            parts[-1] = f"{parts[-1]} {postal}"
        else:
            parts.append(postal)
    if country:
        parts.append(country)
    return ", ".join(parts)


def _first_text_value(
    value: Mapping[str, Any], fields: tuple[str, ...], label: str
) -> str:
    for field in fields:
        text = _optional_text(value.get(field), f"{label}.{field}")
        if text:
            return text
    return ""


def _first_geographic_component(
    value: Mapping[str, Any], fields: tuple[str, ...], label: str
) -> str:
    for field in fields:
        text = _geographic_component(value.get(field), f"{label}.{field}")
        if text:
            return text
    return ""


def _adp_location_diagnostic(value: Any) -> str:
    return "location_shape: " + structure_diagnostic(value)


def _first_text(value: Any, fields: tuple[str, ...], label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, Mapping):
        raise AdapterError(f"ADP requisition {label} is malformed")
    for field in fields:
        text = _optional_text(value.get(field), f"{label}.{field}")
        if text:
            return text
    return ""


def _optional_text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AdapterError(f"ADP requisition {label} must be text")
    return " ".join(value.split())


def _geographic_component(value: Any, label: str, *, depth: int = 0) -> str:
    """Read only explicitly geographic ADP address components."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _explicit_geographic_text(value, label, is_code=False)
    if isinstance(value, list):
        if depth >= 5:
            raise AdapterError(f"ADP requisition {label} exceeded the geographic nesting bound")
        if len(value) > 8:
            raise AdapterError(f"ADP requisition {label} exceeded the geographic list bound")
        displays: list[str] = []
        for index, item in enumerate(value):
            display = _geographic_component(
                item, f"{label}[{index}]", depth=depth + 1
            )
            if display and display.casefold() not in {
                existing.casefold() for existing in displays
            }:
                displays.append(display)
        if len(displays) > 1:
            raise AdapterError(
                f"ADP requisition {label} contains ambiguous geographic display values"
            )
        return displays[0] if displays else ""
    if not isinstance(value, Mapping):
        raise AdapterError(f"ADP requisition {label} is malformed")
    if depth >= 5:
        raise AdapterError(f"ADP requisition {label} exceeded the geographic nesting bound")
    for field in ("longName", "shortName", "name"):
        field_value = value.get(field)
        if field_value is None:
            continue
        if not isinstance(field_value, str):
            raise AdapterError(f"ADP requisition {label}.{field} must be text")
        text = _explicit_geographic_text(field_value, label, is_code=False)
        if text:
            return text
    for field in ("displayValue", "value", "nameCode"):
        nested = value.get(field)
        if nested is None:
            continue
        text = _geographic_component(nested, f"{label}.{field}", depth=depth + 1)
        if text:
            return text
    for field in ("codeValue", "code", "isoCode"):
        field_value = value.get(field)
        if field_value is None:
            continue
        if not isinstance(field_value, str):
            raise AdapterError(f"ADP requisition {label}.{field} must be text")
        text = _explicit_geographic_text(field_value, label, is_code=True)
        if text:
            return text
    return ""


def _explicit_geographic_text(value: str, label: str, *, is_code: bool) -> str:
    text = " ".join(value.split())
    if not text:
        return ""
    if len(text) > 200 or any(ord(character) < 32 for character in text):
        return ""
    if text[:1] in "[{" or text[-1:] in "]}":
        try:
            if isinstance(json.loads(text), (Mapping, list)):
                return ""
        except (TypeError, ValueError):
            # A malformed object-looking scalar is not a geographic display.
            return ""
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return ""
    if re.fullmatch(r"(?i)(?:loc(?:ation)?|id|code)[-_ ]?\d{2,}", text):
        return ""
    if re.fullmatch(
        r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        text,
    ):
        return ""
    if not is_code:
        return text
    if "country" in label.casefold():
        return text if re.fullmatch(r"[A-Za-z]{2,3}", text) else ""
    if "state" in label.casefold() or "subdivision" in label.casefold():
        return text if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,3})?", text) else ""
    if "postal" in label.casefold():
        return text if re.fullmatch(r"(?=.*\d)[A-Za-z0-9 -]{3,12}", text) else ""
    return ""


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


def _metadata(
    target: CompanyTarget, method: str, page_size: int, limit: int
) -> SourceScanMetadata:
    return SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant, "request_method": method,
        "page_size": page_size, "maximum_pages": int(target.max_pages),
        "maximum_results": limit, "pages": [], "jobs_normalized": 0,
        "detail_requests_coupled": target.endpoint_variant == WORKFORCE_NOW,
        "stop_reason": "not_started",
    })


def _finalize_metadata(metadata: SourceScanMetadata) -> None:
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"} and finish_authoritative_total(
        metadata, metadata.get("total_available"), int(metadata.get("jobs_normalized") or 0)
    ):
        return
    if metadata["stop_reason"] == "total_jobs_reached":
        metadata["scan_complete"] = True
        metadata["scan_completeness_reason"] = "reported_total_reached"
        metadata["stop_reason"] = "total_jobs_reached"
    elif metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    if metadata.get("rejected_record_count"):
        metadata["scan_complete"] = False
        metadata["scan_completeness_reason"] = "malformed_listing_records"


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
