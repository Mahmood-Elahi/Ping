"""Cornerstone CSOD public mobile-career-site adapter.

The supported contract starts at an official company careers page, follows the
exact tenant/site link it publishes, and uses the short-lived anonymous player
token embedded by Cornerstone's public shell.  It never creates a candidate
session or calls an authenticated recruiting-management API.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

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
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion


ENDPOINT_VARIANT = "mobile_career_site_external_search_v1"
SEARCH_PATH = "/rec-job-search/external/jobs"
PAGE_SIZE = 25
MAX_PAGES = 2
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_BOOTSTRAP_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 3_000_000
CHALLENGE_RE = re.compile(
    r"(?:cf-chl-|captcha-container|g-recaptcha|hcaptcha|turnstile|verify you are human)",
    re.IGNORECASE,
)
CONTEXT_RE = re.compile(r"\bcsod\.context\s*=\s*(\{.*?\})\s*;</script>", re.DOTALL)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.casefold() != "a":
            return
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        if values.get("href"):
            self.hrefs.append(values["href"])


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord] | AdapterResult:
    """Fetch a bounded public collection attributed to one tenant and site."""
    config = validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)

    metadata = SourceScanMetadata(**{
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "POST",
        "page_size": PAGE_SIZE,
        "maximum_pages": int(target.max_pages),
        "maximum_results": int(target.max_jobs),
        "configured_careers_url": str(target.careers_url),
        "configured_search_url": str(target.search_url),
        "configured_endpoint_url": str(target.endpoint_url),
        "configured_tenant": str(target.tenant),
        "configured_site": str(target.site_id),
        "pages": [],
        "jobs_normalized": 0,
        "total_available": None,
        "stop_reason": "not_started",
    })
    setattr(session, "cornerstone_csod_pagination", metadata)
    publish_source_scan_metadata(session, metadata)

    official = _get_html(session, str(target.careers_url), timeout, "official careers page")
    metadata["final_url"] = str(getattr(official, "url", target.careers_url) or target.careers_url)
    metadata["final_host"] = (urlparse(metadata["final_url"]).hostname or "").casefold()
    official_html = str(getattr(official, "text", "") or "")
    if not re.search(str(target.identity_regex), official_html, re.IGNORECASE):
        raise AdapterError("Cornerstone CSOD official careers identity did not match identity_regex")
    if not _official_page_links_target(official_html, config):
        metadata["scan_completeness_reason"] = "source_migration_suspected"
        metadata["stop_reason"] = "configured_source_identity_not_corroborated"
        metadata["source_identity_status"] = "failed"
        metadata["source_identity_diagnostic"] = (
            "official careers page did not link the configured tenant and site"
        )
        raise AdapterError("Cornerstone CSOD official careers page did not link the configured tenant and site")

    shell = _get_html(session, str(target.search_url), timeout, "public career-site shell")
    shell_html = str(getattr(shell, "text", "") or "")
    context = _public_context(shell_html, config)
    config["culture_id"] = str(context["cultureID"])
    token = str(context["token"])
    endpoint = str(context["endpoints"]["cloud"]).rstrip("/") + SEARCH_PATH
    if endpoint != config["endpoint_url"]:
        raise AdapterError("Cornerstone CSOD shell changed its configured official search endpoint")
    metadata["source_identity_status"] = "validated"

    listing_budget = AdaptiveListingCompletion(
        int(target.max_pages),
        int(target.max_jobs),
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": config["search_origin"],
        "Referer": str(target.search_url),
        "User-Agent": "job-radar/1.0",
    }
    jobs: list[JobRecord] = []
    seen: set[str] = set()
    observed_total: Optional[int] = None

    for page_number in range(1, listing_budget.hard_request_ceiling + 1):
        metadata["pages_requested"] += 1
        try:
            response = session.post(
                endpoint,
                headers=headers,
                json=_search_body(config, page_number),
                timeout=timeout,
            )
        except requests.RequestException:
            metadata["stop_reason"] = "request_failure"
            metadata["scan_completeness_reason"] = "pagination_failure"
            metadata["total_available"] = observed_total
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
        payload = _search_json(response, target, page_number)
        metadata["pages_completed"] += 1
        data = payload["data"]
        total = data["totalCount"]
        rows = data["requisitions"]
        if observed_total is None:
            observed_total = total
        elif total != observed_total:
            raise AdapterError("Cornerstone CSOD total job count changed during pagination")
        offset = (page_number - 1) * PAGE_SIZE
        listing_budget.record_decision(
            metadata,
            total,
            PAGE_SIZE,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=offset + len(rows),
        )
        effective_jobs = int(metadata["effective_listing_job_ceiling"])
        if total > offset and not rows:
            raise AdapterError("Cornerstone CSOD results ended before the reported total")

        for row in rows:
            job = _normalize(row, target, config)
            if job.job_id in seen:
                raise AdapterError(f"Cornerstone CSOD repeated stable job ID: {job.job_id}")
            seen.add(job.job_id)
            jobs.append(job)
        metadata["pages"].append(
            {"page_number": page_number, "offset": offset, "jobs_returned": len(rows)}
        )
        metadata["jobs_normalized"] = min(len(jobs), effective_jobs)
        if len(jobs) > effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if offset + len(rows) == total:
            metadata["stop_reason"] = "reported_total_reached"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if page_number >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"

    metadata["total_available"] = observed_total
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    jobs.sort(key=lambda job: int(job.raw_metadata["requisition_id"]), reverse=True)
    final_limit = int(
        metadata.get("effective_listing_job_ceiling", listing_budget.normal_job_budget)
    )
    return jobs[:final_limit]


def validate_target(target: CompanyTarget) -> dict[str, str]:
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Cornerstone CSOD endpoint_variant must be {ENDPOINT_VARIANT}")
    if not str(target.identity_regex or "").strip():
        raise AdapterError("Cornerstone CSOD target requires identity_regex")
    try:
        re.compile(str(target.identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"Cornerstone CSOD identity_regex is invalid: {exc}") from exc

    tenant = str(target.tenant or "").strip().casefold()
    site_id = str(target.site_id or "").strip()
    locale = str(target.locale or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", tenant):
        raise AdapterError("Cornerstone CSOD target needs a valid tenant")
    if not re.fullmatch(r"[1-9][0-9]{0,5}", site_id):
        raise AdapterError("Cornerstone CSOD target needs a numeric site_id")
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale):
        raise AdapterError("Cornerstone CSOD target needs an ll-CC locale")

    careers = urlparse(str(target.careers_url or ""))
    search = urlparse(str(target.search_url or ""))
    endpoint = urlparse(str(target.endpoint_url or ""))
    expected_path = f"/ux/ats/careersite/{site_id}/home"
    search_query = parse_qs(search.query)
    if careers.scheme != "https" or not careers.hostname:
        raise AdapterError("Cornerstone CSOD careers_url must be an official HTTPS URL")
    if (
        search.scheme != "https"
        or not (search.hostname or "").casefold().endswith(".csod.com")
        or search.path.rstrip("/") != expected_path
        or search_query.get("c") != [tenant]
        or search_query.get("lang") != [locale]
        or search.fragment
    ):
        raise AdapterError("Cornerstone CSOD search_url must match its tenant, site_id, and locale")
    if (
        endpoint.scheme != "https"
        or not (endpoint.hostname or "").casefold().endswith(".api.csod.com")
        or endpoint.path != SEARCH_PATH
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError("Cornerstone CSOD endpoint_url must use the official public CSOD search path")
    allowed = {str(value).strip().casefold().rstrip(".") for value in target.allowed_apply_hosts}
    if allowed != {(search.hostname or "").casefold()}:
        raise AdapterError("Cornerstone CSOD allowed_apply_hosts must contain only the career-site host")
    if int(target.page_size) != PAGE_SIZE:
        raise AdapterError(f"Cornerstone CSOD page_size must be {PAGE_SIZE}")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Cornerstone CSOD max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Cornerstone CSOD max_jobs must be between 1 and {MAX_RESULTS}")
    return {
        "tenant": tenant,
        "site_id": site_id,
        "locale": locale,
        "search_host": (search.hostname or "").casefold(),
        "search_origin": f"{search.scheme}://{search.netloc}",
        "search_path": expected_path,
        "endpoint_url": endpoint.geturl(),
    }


def _get_html(session: requests.Session, url: str, timeout: int, label: str) -> Any:
    response = session.get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
        timeout=timeout,
    )
    if getattr(response, "status_code", None) != 200:
        raise AdapterError(f"Cornerstone CSOD {label} HTTP {getattr(response, 'status_code', None)}")
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > MAX_BOOTSTRAP_BYTES:
        raise AdapterError(f"Cornerstone CSOD {label} exceeded the byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError(f"Cornerstone CSOD {label} was not HTML")
    text = str(getattr(response, "text", "") or "")
    if CHALLENGE_RE.search(text):
        raise AdapterError(f"Cornerstone CSOD {label} requires CAPTCHA or challenge state")
    return response


def _official_page_links_target(raw_html: str, config: Mapping[str, str]) -> bool:
    parser = _LinkParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception as exc:
        raise AdapterError(f"Cornerstone CSOD official careers page was malformed: {exc}") from exc
    for raw_href in parser.hrefs:
        parsed = urlparse(unescape(raw_href))
        query = parse_qs(parsed.query)
        if (
            parsed.scheme == "https"
            and (parsed.hostname or "").casefold() == config["search_host"]
            and parsed.path.rstrip("/") == config["search_path"]
            and query.get("c") == [config["tenant"]]
        ):
            return True
    return False


def _public_context(raw_html: str, config: Mapping[str, str]) -> Mapping[str, Any]:
    matches = CONTEXT_RE.findall(raw_html)
    if len(matches) != 1:
        raise AdapterError("Cornerstone CSOD shell has no unambiguous public player context")
    try:
        context = json.loads(matches[0])
    except (TypeError, ValueError) as exc:
        raise AdapterError("Cornerstone CSOD public player context was invalid JSON") from exc
    if not isinstance(context, Mapping):
        raise AdapterError("Cornerstone CSOD public player context was malformed")
    token = str(context.get("token") or "")
    endpoints = context.get("endpoints")
    cloud = str(endpoints.get("cloud") or "") if isinstance(endpoints, Mapping) else ""
    if (
        context.get("corp") != config["tenant"]
        or str(context.get("cultureName") or "") != config["locale"]
        or isinstance(context.get("cultureID"), bool)
        or not isinstance(context.get("cultureID"), int)
        or not isinstance(endpoints, Mapping)
        or not cloud
        or not token
        or len(token) > 10_000
    ):
        raise AdapterError("Cornerstone CSOD public player context crossed tenant or locale boundaries")
    claims = _jwt_claims(token)
    routes = str(claims.get("rurls") or "").casefold().split(";")
    if (
        claims.get("corp") != config["tenant"]
        or isinstance(claims.get("sub"), bool)
        or not isinstance(claims.get("sub"), int)
        or claims["sub"] >= 0
        or "rec-job-search/external" not in routes
    ):
        raise AdapterError("Cornerstone CSOD shell did not provide an anonymous external-search token")
    return context


def _jwt_claims(token: str) -> Mapping[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AdapterError("Cornerstone CSOD public player token was malformed")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise AdapterError("Cornerstone CSOD public player token claims were malformed") from exc
    if not isinstance(claims, Mapping):
        raise AdapterError("Cornerstone CSOD public player token claims were malformed")
    return claims


def _search_body(config: Mapping[str, str], page_number: int) -> dict[str, Any]:
    site_id = int(config["site_id"])
    return {
        "careerSiteId": site_id,
        "careerSitePageId": site_id,
        "pageNumber": page_number,
        "pageSize": PAGE_SIZE,
        "cultureId": int(config["culture_id"]),
        "searchText": "",
        "cultureName": config["locale"],
        "states": [],
        "countryCodes": [],
        "cities": [],
        "placeID": "",
        "radius": None,
        "postingsWithinDays": None,
        "customFieldCheckboxKeys": [],
        "customFieldDropdowns": [],
        "customFieldRadios": [],
    }


def _search_json(response: Any, target: CompanyTarget, page_number: int) -> Mapping[str, Any]:
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        raise AdapterError(f"Cornerstone CSOD HTTP {status_code} for {target.name} page={page_number}")
    if len(bytes(getattr(response, "content", b"") or b"")) > min(
        int(target.max_response_bytes), MAX_RESPONSE_BYTES
    ):
        raise AdapterError("Cornerstone CSOD search response exceeded max_response_bytes")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        text = str(getattr(response, "text", "") or "")
        if CHALLENGE_RE.search(text):
            raise AdapterError("Cornerstone CSOD search requires CAPTCHA or challenge state")
        raise AdapterError("Cornerstone CSOD search response was not JSON")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError("Cornerstone CSOD search response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("status") != "Success":
        raise AdapterError("Cornerstone CSOD search response was malformed")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("requisitions"), list):
        raise AdapterError("Cornerstone CSOD search response has no requisitions list")
    total = data.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Cornerstone CSOD totalCount must be non-negative")
    if len(data["requisitions"]) > PAGE_SIZE:
        raise AdapterError("Cornerstone CSOD page exceeded the observed page size")
    if total < len(data["requisitions"]):
        raise AdapterError("Cornerstone CSOD totalCount was smaller than its requisitions list")
    return payload


def _normalize(row: Any, target: CompanyTarget, config: Mapping[str, str]) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("Cornerstone CSOD requisition must be a JSON object")
    requisition_id = row.get("requisitionId")
    if isinstance(requisition_id, bool) or not isinstance(requisition_id, int) or requisition_id < 1:
        raise AdapterError("Cornerstone CSOD requisition has no stable numeric ID")
    title = _clean_text(row.get("displayJobTitle"))
    if not title:
        raise AdapterError("Cornerstone CSOD requisition has no title")
    raw_locations = row.get("locations")
    if not isinstance(raw_locations, list):
        raise AdapterError("Cornerstone CSOD requisition locations must be a list")
    locations: list[str] = []
    for raw_location in raw_locations:
        if not isinstance(raw_location, Mapping):
            raise AdapterError("Cornerstone CSOD requisition location must be an object")
        location = ", ".join(
            value
            for value in (
                _clean_text(raw_location.get("city")),
                _clean_text(raw_location.get("state")),
                _clean_text(raw_location.get("country")),
            )
            if value
        )
        if location and location not in locations:
            locations.append(location)
    query = urlencode({"c": config["tenant"], "lang": config["locale"]})
    detail_url = (
        f"{config['search_origin']}{config['search_path']}/requisition/{requisition_id}?{query}"
    )
    description = _clean_text(row.get("externalDescription")) or None
    return JobRecord(
        job_id=f"cornerstone_csod:{config['search_host']}:{config['site_id']}:{requisition_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=_posting_date(row.get("postingEffectiveDate"), config["locale"]),
        source=f"cornerstone_csod:{config['search_host']}:{config['site_id']}",
        offices=locations,
        description=description,
        raw_metadata={
            "platform": "cornerstone_csod",
            "endpoint_variant": ENDPOINT_VARIANT,
            "requisition_id": requisition_id,
            "posting_expiration_date": _clean_text(row.get("postingExpirationDate")),
            "detail_url": detail_url,
            "apply_url": detail_url,
        },
    )


def _posting_date(value: Any, locale: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    formats = ("%d/%m/%Y", "%m/%d/%Y") if locale == "en-GB" else ("%m/%d/%Y", "%d/%m/%Y")
    for date_format in formats:
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    raise AdapterError("Cornerstone CSOD posting date did not match the configured locale")


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()
