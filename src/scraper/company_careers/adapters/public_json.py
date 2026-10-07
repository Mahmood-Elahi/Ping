"""Conservative adapters for reviewed, same-origin public JSON collections."""

from __future__ import annotations

from collections import Counter
from html import unescape
import json
from math import ceil
import re
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.parse import urljoin

import requests
from lxml import html

from src.scraper.company_careers.listing_completion import (
    MAX_LISTING_REQUESTS_PER_SOURCE,
    MAX_RAW_JOBS_PER_SOURCE,
    record_terminal_evidence,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
    source_scan_metadata,
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    initialize_mutable_collection,
    record_total_observation,
)


AURORA_JOBS_INDEX = "aurora_jobs_index_v1"
WORDPRESS_ROLES = "wordpress_roles_v1"
SHAZAMME_JOB_RESULTS = "shazamme_job_results_v1"
OPTIVER_JOBS = "optiver_jobs_v1"
UNITREE_JOBS = "unitree_jobs_v1"
VALVE_JOBS = "valve_jobs_v1"
ROBLOX_JOBS = "roblox_jobs_v1"
DYSON_JOBS = "dyson_jobs_v1"
SUPPORTED_VARIANTS = {
    AURORA_JOBS_INDEX,
    WORDPRESS_ROLES,
    SHAZAMME_JOB_RESULTS,
    OPTIVER_JOBS,
    UNITREE_JOBS,
    VALVE_JOBS,
    ROBLOX_JOBS,
    DYSON_JOBS,
}
ROBLOX_ENDPOINT = "https://d32kbl9jppd7az.cloudfront.net/careers/jobs.json"
ROBLOX_MAX_JOBS = 500
DYSON_ENDPOINT = "https://careers.dyson.com/en-gb/api/search/jobs/"
DYSON_PAGE_SIZE = 10
DYSON_MAX_PAGES = 50
DYSON_MAX_JOBS = 500
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
SHAZAMME_ATOMIC_COLLECTIONS = {
    "a5bcebfa-981b-46cd-b46f-ad91a50505b8": {
        "careers_host": "www.careers.zoom.us",
        "endpoint_host": "shazamme.io",
        "source_job_host": "careers.zoom.com",
        "advertiser": "Zoom",
        "site_name": "Zoom Careers",
    }
}
SHAZAMME_PAGINATION_HEADERS = {
    "content-range",
    "link",
    "x-has-more",
    "x-limit",
    "x-next-page",
    "x-offset",
    "x-page",
    "x-page-count",
    "x-per-page",
    "x-total-pages",
}
SHAZAMME_TOTAL_HEADERS = ("x-total-count", "x-total")
SHAZAMME_PAGINATION_FIELDS = {
    "count",
    "cursor",
    "hasmore",
    "limit",
    "next",
    "nextcursor",
    "nextpage",
    "offset",
    "page",
    "pagecount",
    "pagesize",
    "total",
    "totalcount",
    "totalpages",
}


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    validate_target(target)
    if target.endpoint_variant == OPTIVER_JOBS:
        jobs = _fetch_optiver_collection(session, target, timeout)
    elif target.endpoint_variant == UNITREE_JOBS:
        jobs = _fetch_unitree_collection(session, target, timeout)
    elif target.endpoint_variant == VALVE_JOBS:
        jobs = _fetch_valve_collection(session, target, timeout)
    elif target.endpoint_variant == ROBLOX_JOBS:
        jobs = _fetch_roblox_collection(session, target, timeout)
    elif target.endpoint_variant == DYSON_JOBS:
        jobs = _fetch_dyson_collection(session, target, timeout)
    elif target.endpoint_variant == SHAZAMME_JOB_RESULTS:
        jobs = _fetch_shazamme_collection(session, target, timeout)
    elif target.endpoint_variant == WORDPRESS_ROLES:
        jobs = fetch_with_mutation_restart(
            session, lambda: _fetch_wordpress_once(session, target, timeout)
        )
    else:
        jobs = _fetch_aurora_index(session, target, timeout)
    _record_collection_integrity(source_scan_metadata(session), jobs)
    return jobs


def validate_target(target: CompanyTarget) -> None:
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"Public JSON unsupported variant: {target.endpoint_variant}")
    if not target.endpoint_url or not target.allowed_apply_hosts:
        raise AdapterError("Public JSON target requires endpoint_url and allowed_apply_hosts")
    endpoint = urlparse(str(target.endpoint_url))
    careers = urlparse(str(target.careers_url or ""))
    if target.endpoint_variant == SHAZAMME_JOB_RESULTS:
        _validate_shazamme_target(target, endpoint, careers)
        return
    if target.endpoint_variant == OPTIVER_JOBS:
        _validate_optiver_target(target, endpoint, careers)
        return
    if target.endpoint_variant == UNITREE_JOBS:
        _validate_unitree_target(target, endpoint, careers)
        return
    if target.endpoint_variant == VALVE_JOBS:
        _validate_valve_target(target, endpoint, careers)
        return
    if target.endpoint_variant == ROBLOX_JOBS:
        _validate_roblox_target(target, endpoint, careers)
        return
    if target.endpoint_variant == DYSON_JOBS:
        _validate_dyson_target(target, endpoint, careers)
        return
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or careers.scheme != "https"
        or endpoint.hostname.casefold() != (careers.hostname or "").casefold()
    ):
        raise AdapterError("Public JSON endpoint must be anonymous, same-origin HTTPS")
    expected_path = (
        "/api/jobs-index"
        if target.endpoint_variant == AURORA_JOBS_INDEX
        else "/wp-json/wp/v2/roles"
    )
    if endpoint.path.rstrip("/") != expected_path:
        raise AdapterError("Public JSON endpoint path does not match its reviewed variant")


def _validate_optiver_target(target: CompanyTarget, endpoint: Any, careers: Any) -> None:
    allowed_source = {str(value).casefold() for value in target.allowed_source_hosts}
    allowed_apply = {str(value).casefold() for value in target.allowed_apply_hosts}
    if (
        endpoint.scheme != "https"
        or endpoint.username
        or endpoint.password
        or endpoint.hostname != "www.optiver.com"
        or endpoint.path != "/en/api/v1/jobs"
        or endpoint.params
        or endpoint.query
        or endpoint.fragment
        or careers.scheme != "https"
        or careers.username
        or careers.password
        or careers.hostname != "www.optiver.com"
        or allowed_source != {"www.optiver.com"}
        or allowed_apply != {"www.optiver.com"}
        or target.page_size != 16
        or not target.identity_regex
    ):
        raise AdapterError("Optiver target does not bind the reviewed same-origin jobs API")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"Optiver identity regex is invalid: {exc}") from exc


def _validate_unitree_target(target: CompanyTarget, endpoint: Any, careers: Any) -> None:
    if (
        endpoint.scheme != "https"
        or endpoint.username
        or endpoint.password
        or endpoint.hostname != "api.unitree.com"
        or endpoint.path != "/website/job/list"
        or endpoint.query != "perPage=50"
        or endpoint.fragment
        or careers.scheme != "https"
        or careers.hostname != "www.unitree.com"
        or careers.path.rstrip("/") != "/position"
        or {str(value).casefold() for value in target.allowed_source_hosts}
        != {"api.unitree.com"}
        or {str(value).casefold() for value in target.allowed_apply_hosts}
        != {"www.unitree.com"}
    ):
        raise AdapterError("Unitree target does not bind the reviewed official jobs API")


def _validate_valve_target(target: CompanyTarget, endpoint: Any, careers: Any) -> None:
    if (
        endpoint.scheme != "https"
        or endpoint.username
        or endpoint.password
        or endpoint.hostname != "www.valvesoftware.com"
        or endpoint.path != "/en/jobs/job-search"
        or endpoint.query != "search="
        or endpoint.fragment
        or careers.scheme != "https"
        or careers.hostname != "www.valvesoftware.com"
        or careers.path.rstrip("/") != "/en"
        or {str(value).casefold() for value in target.allowed_source_hosts}
        != {"www.valvesoftware.com"}
        or {str(value).casefold() for value in target.allowed_apply_hosts}
        != {"www.valvesoftware.com"}
    ):
        raise AdapterError("Valve target does not bind the reviewed official jobs API")


def _validate_roblox_target(target: CompanyTarget, endpoint: Any, careers: Any) -> None:
    if (
        str(target.endpoint_url) != ROBLOX_ENDPOINT
        or endpoint.scheme != "https"
        or endpoint.username
        or endpoint.password
        or endpoint.hostname != "d32kbl9jppd7az.cloudfront.net"
        or endpoint.path != "/careers/jobs.json"
        or endpoint.query
        or endpoint.fragment
        or careers.scheme != "https"
        or careers.username
        or careers.password
        or careers.hostname != "careers.roblox.com"
        or careers.path.rstrip("/") != "/jobs"
        or {str(value).casefold() for value in target.allowed_source_hosts}
        != {"d32kbl9jppd7az.cloudfront.net"}
        or {str(value).casefold() for value in target.allowed_apply_hosts}
        != {"careers.roblox.com"}
        or int(target.max_pages) != 1
        or int(target.max_jobs) != ROBLOX_MAX_JOBS
        or not 1 <= int(target.max_scripts) <= 20
        or not 1 <= int(target.max_script_bytes) <= 1_000_000
        or not target.identity_regex
    ):
        raise AdapterError("Roblox target does not bind the reviewed official jobs feed")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"Roblox identity regex is invalid: {exc}") from exc


def _validate_dyson_target(target: CompanyTarget, endpoint: Any, careers: Any) -> None:
    if (
        str(target.endpoint_url) != DYSON_ENDPOINT
        or endpoint.scheme != "https"
        or endpoint.username
        or endpoint.password
        or endpoint.hostname != "careers.dyson.com"
        or endpoint.path != "/en-gb/api/search/jobs/"
        or endpoint.query
        or endpoint.fragment
        or careers.scheme != "https"
        or careers.username
        or careers.password
        or careers.hostname != "careers.dyson.com"
        or careers.path.rstrip("/") != "/en-gb/search-results"
        or {str(value).casefold() for value in target.allowed_source_hosts}
        != {"careers.dyson.com"}
        or {str(value).casefold() for value in target.allowed_apply_hosts}
        != {"careers.dyson.com"}
        or int(target.page_size) != DYSON_PAGE_SIZE
        or not 1 <= int(target.max_pages) <= DYSON_MAX_PAGES
        or not 1 <= int(target.max_jobs) <= DYSON_MAX_JOBS
        or not 1 <= int(target.max_scripts) <= 5
        or not 1 <= int(target.max_script_bytes) <= 1_000_000
        or not target.identity_regex
    ):
        raise AdapterError("Dyson target does not bind the reviewed official jobs API")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"Dyson identity regex is invalid: {exc}") from exc


def _fetch_optiver_collection(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    """Enumerate Optiver's official counted API through its exact terminal total."""
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)

    metadata["pages_requested"] = 1
    identity = session.get(
        str(target.careers_url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if identity.status_code != 200:
        raise AdapterError(f"Optiver official careers identity HTTP {identity.status_code}")
    identity_content = bytes(getattr(identity, "content", b"") or b"")
    if not identity_content:
        identity_content = str(getattr(identity, "text", "") or "").encode("utf-8")
    if not identity_content or len(identity_content) > int(target.max_response_bytes):
        raise AdapterError("Optiver official careers identity response exceeded its bound")
    identity_url = urlparse(str(getattr(identity, "url", "") or target.careers_url))
    identity_text = identity_content.decode("utf-8", errors="replace")
    try:
        identity_visible_text = " ".join(
            html.fromstring(identity_content).text_content().split()
        )
    except (TypeError, ValueError) as exc:
        raise AdapterError("Optiver official careers page returned malformed HTML") from exc
    if (
        identity_url.scheme != "https"
        or identity_url.hostname != "www.optiver.com"
        or "/en/api/v1/jobs" not in identity_text.replace("\\/", "/")
        or not re.search(str(target.identity_regex), identity_visible_text, re.I)
    ):
        raise AdapterError("Optiver official page did not bind the reviewed same-origin API")
    metadata["pages_completed"] = 1
    metadata["source_identity_status"] = "validated"

    page_size = 16
    maximum_pages = min(max(1, int(target.max_pages)), MAX_LISTING_REQUESTS_PER_SOURCE)
    maximum_jobs = min(max(1, int(target.max_jobs)), MAX_RAW_JOBS_PER_SOURCE)
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_signatures: set[tuple[str, ...]] = set()
    expected_total: int | None = None

    for page_index in range(maximum_pages):
        offset = page_index * page_size
        metadata["pages_requested"] += 1
        response = _get(
            session,
            str(target.endpoint_url),
            timeout,
            params={"from": str(offset), "size": str(page_size)},
        )
        final = urlparse(str(getattr(response, "url", "") or target.endpoint_url))
        if final.scheme != "https" or final.hostname != "www.optiver.com" or final.path != "/en/api/v1/jobs":
            raise AdapterError("Optiver API redirected outside its reviewed endpoint")
        payload = _json(response, target)
        metadata["pages_completed"] += 1
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise AdapterError("Optiver API response is missing its items collection")
        total = _nonnegative_int(payload.get("totalCount"), "totalCount")
        rows = payload["items"]
        if total > maximum_jobs or total > MAX_RAW_JOBS_PER_SOURCE:
            metadata.update(
                total_available=total,
                authoritative_total_present=True,
                safety_limit_reached=True,
                stop_reason="maximum_results_reached",
                scan_completeness_reason="safety_limit_reached",
            )
            return jobs
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            metadata["collection_changed_during_scan"] = True
            raise AdapterError("Optiver totalCount changed during pagination")
        expected_count = min(page_size, max(0, total - offset))
        if len(rows) != expected_count:
            raise AdapterError("Optiver page length conflicts with totalCount and offset")

        page_ids: list[str] = []
        normalized: list[JobRecord] = []
        for row in rows:
            try:
                job = _normalize_optiver(row, target)
            except AdapterError:
                metadata["rejected_record_count"] += 1
                raise
            page_ids.append(job.job_id)
            if job.job_id in seen_ids or job.url in seen_urls:
                metadata["duplicate_ids"] = True
                raise AdapterError("Optiver API repeated a stable ID or canonical URL")
            seen_ids.add(job.job_id)
            seen_urls.add(job.url)
            normalized.append(job)
        signature = tuple(page_ids)
        if signature and signature in seen_signatures:
            metadata["repeated_page"] = True
            raise AdapterError("Optiver API repeated a page")
        if signature:
            seen_signatures.add(signature)
        jobs.extend(normalized)
        metadata["jobs_normalized"] = len(jobs)
        metadata["total_available"] = total
        metadata["authoritative_total_present"] = True

        if len(jobs) == total:
            metadata.update(
                scan_complete=True,
                scan_completeness_reason="reported_total_reached",
                stop_reason="reported_total_reached",
                terminal_evidence_type="authoritative_total",
                terminal_evidence_value=total,
                terminal_page_observed=True,
                has_more=False,
            )
            return jobs

    metadata.update(
        safety_limit_reached=True,
        stop_reason="maximum_pages_reached",
        scan_completeness_reason="safety_limit_reached",
    )
    return jobs


def _normalize_optiver(row: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("Optiver job row must be an object")
    component_id = row.get("componentID")
    title = " ".join(str(row.get("title") or "").split())
    href = str(row.get("href") or "").strip()
    if not isinstance(component_id, int) or isinstance(component_id, bool) or component_id <= 0 or not title or not href:
        raise AdapterError("Optiver job is missing a stable componentID, title, or href")
    url = _official_url(urljoin(str(target.careers_url), href), target)
    parsed = urlparse(url)
    if (
        parsed.hostname != "www.optiver.com"
        or not re.fullmatch(r"/join-us/jobs/[A-Za-z0-9][A-Za-z0-9/_-]*/?", parsed.path)
        or "//" in parsed.path
        or "/../" in parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise AdapterError("Optiver job href is not one canonical first-party job URL")
    location = " ".join(str(row.get("location") or "").split())
    departments = [
        value
        for value in (" ".join(str(row.get("domain") or "").split()),)
        if value
    ]
    return JobRecord(
        job_id=f"public_json:www.optiver.com:{component_id}",
        title=title,
        company=target.name,
        location=location,
        url=url,
        updated_at="",
        source="public_json:www.optiver.com:optiver_jobs_v1",
        departments=departments,
        offices=[location] if location else [],
        raw_metadata={
            "external_job_id": str(component_id),
            "platform": "public_json",
            "endpoint_variant": OPTIVER_JOBS,
        },
    )


def _validate_shazamme_target(
    target: CompanyTarget, endpoint: Any, careers: Any
) -> None:
    site_id = str(target.site_id or "").strip()
    contract = SHAZAMME_ATOMIC_COLLECTIONS.get(site_id.casefold())
    allowed_source = {str(value).casefold() for value in target.allowed_source_hosts}
    allowed_apply = {str(value).casefold() for value in target.allowed_apply_hosts}
    if (
        contract is None
        or endpoint.username
        or endpoint.password
        or endpoint.scheme != "https"
        or (endpoint.hostname or "").casefold() != contract["endpoint_host"]
        or endpoint.path != f"/job-results/{site_id}"
        or endpoint.params
        or endpoint.query
        or endpoint.fragment
        or not UUID_RE.fullmatch(site_id)
        or careers.scheme != "https"
        or (careers.hostname or "").casefold() != contract["careers_host"]
        or allowed_source != {contract["endpoint_host"]}
        or allowed_apply != {contract["careers_host"]}
    ):
        raise AdapterError("Shazamme target does not bind one reviewed site and official host")
    if not target.identity_regex or not target.job_attribution_regex:
        raise AdapterError("Shazamme target requires page and per-job identity contracts")
    try:
        re.compile(str(target.identity_regex), re.I)
        re.compile(str(target.job_attribution_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"Shazamme identity regex is invalid: {exc}") from exc
    template = str(target.apply_url_template or "")
    parsed_template = urlparse(template.replace("{path}", "/job"))
    if (
        template.count("{path}") != 1
        or parsed_template.scheme != "https"
        or (parsed_template.hostname or "").casefold() not in allowed_apply
        or parsed_template.query
        or parsed_template.fragment
    ):
        raise AdapterError("Shazamme apply_url_template must bind {path} to an official host")


def _metadata(target: CompanyTarget) -> SourceScanMetadata:
    metadata = SourceScanMetadata(
        endpoint_variant=target.endpoint_variant,
        request_method="GET",
        pages=[],
        jobs_normalized=0,
        diagnostics=[],
        warnings=[],
        rejected_record_count=0,
    )
    initialize_mutable_collection(metadata)
    return metadata


def _official_identity_page(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
    *,
    required_fragments: tuple[str, ...],
) -> str:
    response = session.get(
        str(target.careers_url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if response.status_code != 200:
        raise AdapterError(
            f"Public JSON official careers identity HTTP {response.status_code}"
        )
    content = bytes(getattr(response, "content", b"") or b"")
    if not content or len(content) > int(target.max_response_bytes):
        raise AdapterError("Public JSON official careers identity response exceeded its bound")
    final = urlparse(str(getattr(response, "url", target.careers_url)))
    configured = urlparse(str(target.careers_url))
    if (
        final.scheme != "https"
        or (final.hostname or "").casefold() != (configured.hostname or "").casefold()
    ):
        raise AdapterError("Public JSON official careers page redirected outside its host")
    text = content.decode("utf-8", errors="replace")
    if not all(fragment in text for fragment in required_fragments):
        raise AdapterError("Public JSON official careers page did not bind the reviewed endpoint")
    return text


def _finish_atomic_collection(
    metadata: SourceScanMetadata,
    jobs: list[JobRecord],
    *,
    evidence_type: str,
    evidence_value: Any,
    pages_completed: int = 2,
) -> list[JobRecord]:
    if len(jobs) > MAX_RAW_JOBS_PER_SOURCE:
        raise AdapterError("Public JSON collection exceeded the hard job ceiling")
    metadata["jobs_normalized"] = len(jobs)
    _record_collection_integrity(metadata, jobs)
    total = len(jobs)
    metadata.update(
        jobs_normalized=total,
        total_available=total,
        pages_completed=pages_completed,
        scan_complete=True,
        scan_completeness_reason=evidence_type,
        stop_reason=evidence_type,
        complete_collection_response=True,
        atomic_collection_contract=True,
        source_identity_status="verified",
    )
    record_terminal_evidence(
        metadata,
        evidence_type,
        evidence_value,
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    return jobs


def _record_collection_integrity(
    metadata: SourceScanMetadata,
    jobs: list[JobRecord],
) -> None:
    """Publish and enforce stable-ID and canonical-URL uniqueness."""
    id_counts = Counter(job.job_id for job in jobs)
    url_counts = Counter(job.url for job in jobs)
    duplicate_ids = sorted(
        value for value, count in id_counts.items() if count > 1
    )
    duplicate_urls = sorted(
        value for value, count in url_counts.items() if count > 1
    )
    metadata.update(
        collection_integrity_contract=True,
        unique_stable_ids=len(id_counts),
        unique_canonical_urls=len(url_counts),
        duplicate_ids=duplicate_ids[:20],
        duplicate_canonical_urls=duplicate_urls[:20],
        stable_ids_unique=not duplicate_ids,
        canonical_urls_unique=not duplicate_urls,
    )
    if duplicate_ids:
        raise AdapterError("Public JSON collection repeated a stable job ID")
    if duplicate_urls:
        raise AdapterError("Public JSON collection repeated a canonical job URL")


def _fetch_unitree_collection(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)
    metadata["pages_requested"] = 1
    _official_identity_page(
        session,
        target,
        timeout,
        required_fragments=(
            "Unitree Robotics",
            'apiBase:"https://api.unitree.com/website"',
        ),
    )
    metadata["pages_requested"] += 1
    response = _get(session, str(target.endpoint_url), timeout)
    payload = _json(response, target)
    if str(getattr(response, "url", "")) != str(target.endpoint_url):
        raise AdapterError("Unitree jobs API redirected outside its reviewed endpoint")
    if not isinstance(payload, Mapping) or payload.get("code") != 100:
        raise AdapterError("Unitree jobs API did not return its success envelope")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise AdapterError("Unitree jobs API is missing its data object")
    rows = data.get("items")
    total = _nonnegative_int(data.get("count"), "Unitree count")
    if (
        not isinstance(rows, list)
        or total != len(rows)
        or total > 50
        or not all(isinstance(row, Mapping) for row in rows)
    ):
        raise AdapterError("Unitree jobs API count does not prove one complete page")
    jobs = [_normalize_unitree(row, target) for row in rows]
    record_total_observation(metadata, total, offset=0, source_label="Unitree jobs API")
    return _finish_atomic_collection(
        metadata,
        jobs,
        evidence_type="unitree_counted_atomic_collection",
        evidence_value=total,
    )


def _normalize_unitree(row: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    external_id = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not external_id.isdigit() or not title:
        raise AdapterError("Unitree job is missing a stable numeric ID or title")
    url = _official_url(
        f"https://www.unitree.com/position/{external_id}", target
    )
    category = str(row.get("categoryId") or "").strip()
    return JobRecord(
        job_id=f"public_json:api.unitree.com:{external_id}",
        title=title,
        company=target.name,
        location="",
        url=url,
        updated_at=str(row.get("postTime") or ""),
        source="public_json:api.unitree.com:website-job-list",
        departments=[category] if category else [],
        raw_metadata={
            "external_job_id": external_id,
            "city_id": str(row.get("cityId") or ""),
            "group_id": str(row.get("groupId") or ""),
            "platform": "public_json",
            "endpoint_variant": UNITREE_JOBS,
        },
    )


def _fetch_roblox_collection(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    """Read the atomic feed bound by Roblox's current official Next.js app."""
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)
    metadata["pages_requested"] = 1
    response = session.get(
        str(target.careers_url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if response.status_code != 200:
        raise AdapterError(f"Roblox official careers page HTTP {response.status_code}")
    content = bytes(getattr(response, "content", b"") or b"")
    if not content or len(content) > int(target.max_response_bytes):
        raise AdapterError("Roblox official careers page exceeded its response bound")
    final = urlparse(str(getattr(response, "url", target.careers_url)))
    if (
        final.scheme != "https"
        or final.hostname != "careers.roblox.com"
        or final.path.rstrip("/") != "/jobs"
    ):
        raise AdapterError("Roblox official careers page redirected outside its reviewed route")
    try:
        root = html.fromstring(content)
    except (TypeError, ValueError) as exc:
        raise AdapterError("Roblox official careers page returned malformed HTML") from exc
    visible = " ".join(root.text_content().split())
    if not re.search(str(target.identity_regex), visible, re.I):
        raise AdapterError("Roblox official careers identity did not match")

    script_urls: list[str] = []
    for value in root.xpath("//script[@src]/@src"):
        script_url = urljoin(str(target.careers_url), str(value))
        parsed = urlparse(script_url)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "careers.roblox.com"
            and re.fullmatch(r"/_next/static/chunks/[^/?#]+\.js", parsed.path)
            and not parsed.query
            and not parsed.fragment
        ):
            script_urls.append(script_url)
    script_urls = list(dict.fromkeys(script_urls))
    if not script_urls or len(script_urls) > int(target.max_scripts):
        raise AdapterError("Roblox official page exposed an invalid script set")

    binding_found = False
    for script_url in script_urls:
        metadata["pages_requested"] += 1
        script = _get(session, script_url, timeout)
        script_content = bytes(getattr(script, "content", b"") or b"")
        if not script_content or len(script_content) > int(target.max_script_bytes):
            raise AdapterError("Roblox application script exceeded its response bound")
        if str(getattr(script, "url", "")) != script_url:
            raise AdapterError("Roblox application script redirected outside its reviewed URL")
        metadata["pages_completed"] += 1
        source = script_content.decode("utf-8", errors="replace").replace("\\/", "/")
        if ROBLOX_ENDPOINT in source or (
            "d32kbl9jppd7az.cloudfront.net/careers" in source
            and "/jobs.json" in source
        ):
            binding_found = True
            break
    if not binding_found:
        raise AdapterError("Roblox official application did not bind the reviewed jobs feed")
    metadata["source_identity_status"] = "validated"

    metadata["pages_requested"] += 1
    feed = _get(session, str(target.endpoint_url), timeout)
    if str(getattr(feed, "url", "")) != str(target.endpoint_url):
        raise AdapterError("Roblox jobs feed redirected outside its reviewed endpoint")
    payload = _json(feed, target)
    metadata["pages_completed"] += 1
    if not isinstance(payload, list) or len(payload) > ROBLOX_MAX_JOBS:
        raise AdapterError("Roblox jobs feed is not one bounded atomic collection")
    jobs = [_normalize_roblox(row, target) for row in payload]
    return _finish_atomic_collection(
        metadata,
        jobs,
        evidence_type="roblox_official_atomic_feed",
        evidence_value=len(jobs),
        pages_completed=int(metadata["pages_completed"]),
    )


def _normalize_roblox(row: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("Roblox jobs feed contained a malformed row")
    external_id = row.get("id")
    title = " ".join(str(row.get("title") or "").split())
    location = " ".join(str(row.get("location") or "").split())
    employment_type = row.get("employment_type")
    groups = row.get("groups")
    departments = row.get("department")
    early_career = row.get("early_career")
    if (
        not isinstance(external_id, int)
        or isinstance(external_id, bool)
        or external_id <= 0
        or not title
        or not location
        or not isinstance(employment_type, str)
        or not isinstance(groups, list)
        or not isinstance(departments, list)
        or not isinstance(early_career, bool)
        or not all(isinstance(value, str) and value.strip() for value in groups + departments)
    ):
        raise AdapterError("Roblox job is missing its stable ID or required typed fields")
    labels = list(
        dict.fromkeys(
            " ".join(value.split()) for value in departments + groups if value.strip()
        )
    )
    return JobRecord(
        job_id=f"public_json:d32kbl9jppd7az.cloudfront.net:{external_id}",
        title=title,
        company=target.name,
        location=location,
        url=_official_url(f"https://careers.roblox.com/jobs/{external_id}", target),
        updated_at="",
        source="public_json:d32kbl9jppd7az.cloudfront.net:roblox_jobs_v1",
        departments=labels,
        offices=[location],
        raw_metadata={
            "external_job_id": str(external_id),
            "employment_type": employment_type.strip(),
            "early_career": early_career,
            "platform": "public_json",
            "endpoint_variant": ROBLOX_JOBS,
        },
    )


def _fetch_dyson_collection(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    """Enumerate Dyson's counted first-party JSON search collection."""
    metadata = _metadata(target)
    metadata.update(
        page_size=DYSON_PAGE_SIZE,
        maximum_pages=int(target.max_pages),
        maximum_results=int(target.max_jobs),
    )
    publish_source_scan_metadata(session, metadata)

    metadata["pages_requested"] = 1
    identity = session.get(
        str(target.careers_url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if identity.status_code != 200:
        raise AdapterError(f"Dyson official careers page HTTP {identity.status_code}")
    content = bytes(getattr(identity, "content", b"") or b"")
    if not content or len(content) > int(target.max_response_bytes):
        raise AdapterError("Dyson official careers page exceeded its response bound")
    final = urlparse(str(getattr(identity, "url", target.careers_url)))
    if (
        final.scheme != "https"
        or final.hostname != "careers.dyson.com"
        or final.path.rstrip("/") != "/en-gb/search-results"
    ):
        raise AdapterError("Dyson official careers page redirected outside its reviewed route")
    try:
        root = html.fromstring(content)
    except (TypeError, ValueError) as exc:
        raise AdapterError("Dyson official careers page returned malformed HTML") from exc
    if not re.search(str(target.identity_regex), " ".join(root.text_content().split()), re.I):
        raise AdapterError("Dyson official careers identity did not match")
    script_urls = list(
        dict.fromkeys(
            urljoin(str(target.careers_url), str(value))
            for value in root.xpath("//script[@src]/@src")
            if re.fullmatch(r"/js/search-[A-Za-z0-9]+\.min\.js", urlparse(str(value)).path)
        )
    )
    if len(script_urls) != 1:
        raise AdapterError("Dyson official careers page did not identify one search script")
    metadata["pages_completed"] = 1

    metadata["pages_requested"] += 1
    script_url = script_urls[0]
    script = _get(session, script_url, timeout)
    script_final = urlparse(str(getattr(script, "url", script_url)))
    script_content = bytes(getattr(script, "content", b"") or b"")
    if (
        script_final.scheme != "https"
        or script_final.hostname != "careers.dyson.com"
        or not re.fullmatch(r"/js/search-[A-Za-z0-9]+\.min\.js", script_final.path)
        or not script_content
        or len(script_content) > int(target.max_script_bytes)
        or b"/api/search/jobs/" not in script_content
    ):
        raise AdapterError("Dyson search script did not bind the reviewed jobs API")
    metadata["pages_completed"] += 1
    metadata["source_identity_status"] = "validated"

    jobs: list[JobRecord] = []
    expected_total: int | None = None

    for page in range(1, int(target.max_pages) + 1):
        metadata["pages_requested"] += 1
        response = _get(
            session,
            str(target.endpoint_url),
            timeout,
            params={
                "q": "",
                "pagesize": str(DYSON_PAGE_SIZE),
                "page": str(page),
                "minpage": str(page),
                "maxpage": str(page),
                "sort": "Score",
                "earlycareers": "0",
            },
        )
        response_url = urlparse(str(getattr(response, "url", target.endpoint_url)))
        if (
            response_url.scheme != "https"
            or response_url.hostname != "careers.dyson.com"
            or response_url.path != "/en-gb/api/search/jobs/"
        ):
            raise AdapterError("Dyson jobs API redirected outside its reviewed endpoint")
        payload = _json(response, target)
        metadata["pages_completed"] += 1
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            raise AdapterError("Dyson jobs API did not return its success envelope")
        rows = payload.get("items")
        total = _nonnegative_int(payload.get("totalItems"), "Dyson totalItems")
        original_query = payload.get("originalQuery")
        if (
            not isinstance(rows, list)
            or not all(isinstance(row, Mapping) for row in rows)
            or not isinstance(original_query, Mapping)
            or original_query.get("page") != page
            or original_query.get("minPage") != page
            or original_query.get("maxPage") != page
        ):
            raise AdapterError("Dyson jobs API returned a malformed page contract")
        if expected_total is None:
            expected_total = total
            required_pages = ceil(total / DYSON_PAGE_SIZE) if total else 1
            if total > int(target.max_jobs) or required_pages > int(target.max_pages):
                metadata.update(
                    total_available=total,
                    authoritative_total_present=True,
                    safety_limit_reached=True,
                    stop_reason="hard_safety_limit_exceeded",
                    scan_completeness_reason="safety_limit_reached",
                )
                return []
        elif total != expected_total:
            metadata["total_count_changed"] = True
            metadata["collection_changed_during_scan"] = True
            raise AdapterError("Dyson totalItems changed during pagination")
        record_total_observation(
            metadata,
            total,
            offset=(page - 1) * DYSON_PAGE_SIZE,
            source_label="Dyson jobs API",
        )
        remaining = max(0, total - len(jobs))
        expected_count = min(DYSON_PAGE_SIZE, remaining)
        if len(rows) != expected_count:
            raise AdapterError("Dyson page length conflicts with totalItems")
        if payload.get("isFirst") is not (page == 1):
            raise AdapterError("Dyson isFirst conflicts with the requested page")
        final_page = len(jobs) + len(rows) == total
        if payload.get("isLast") is not final_page:
            raise AdapterError("Dyson isLast conflicts with totalItems")

        normalized = [_normalize_dyson(row, target) for row in rows]
        jobs.extend(normalized)
        metadata["jobs_normalized"] = len(jobs)
        _record_collection_integrity(metadata, jobs)
        metadata["pages"].append(
            {
                "page": page,
                "jobs_returned": len(rows),
                "jobs_normalized": len(normalized),
            }
        )
        if final_page:
            break

    if expected_total is None or len(jobs) != expected_total:
        raise AdapterError("Dyson jobs API ended before its authoritative total")
    metadata.update(
        total_available=expected_total,
        authoritative_total_present=True,
        scan_complete=True,
        scan_completeness_reason="reported_total_reached",
        stop_reason="reported_total_reached",
        source_identity_status="verified",
    )
    record_terminal_evidence(
        metadata,
        "authoritative_total",
        expected_total,
        has_more=False,
        terminal_page_observed=True,
    )
    return jobs


def _normalize_dyson(row: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    external_id = " ".join(str(row.get("id") or "").split())
    title = " ".join(str(row.get("title") or "").split())
    location = " ".join(str(row.get("locationsText") or "").split())
    department = " ".join(str(row.get("teamsText") or "").split())
    raw_url = str(row.get("url") or "").strip()
    parsed = urlparse(raw_url)
    if (
        not re.fullmatch(r"JR[0-9]+", external_id)
        or not title
        or not location
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname != "careers.dyson.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(
            rf"/en-gb/job-description/[^/]+/{re.escape(external_id)}/?",
            parsed.path,
        )
    ):
        raise AdapterError("Dyson job is missing its stable ID or canonical fields")
    canonical_url = parsed._replace(scheme="https").geturl()
    canonical_url = _official_url(canonical_url, target)
    return JobRecord(
        job_id=f"public_json:careers.dyson.com:{external_id}",
        title=title,
        company=target.name,
        location=location,
        url=canonical_url,
        updated_at=str(row.get("postedDateIso") or row.get("postedDate") or ""),
        source="public_json:careers.dyson.com:dyson_jobs_v1",
        departments=[department] if department else [],
        offices=[location],
        description=" ".join(str(row.get("description") or "").split()) or None,
        raw_metadata={
            "external_job_id": external_id,
            "platform": "public_json",
            "endpoint_variant": DYSON_JOBS,
        },
    )


def _fetch_valve_collection(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)
    metadata["pages_requested"] = 1
    identity = _official_identity_page(
        session,
        target,
        timeout,
        required_fragments=(
            "Valve Corporation",
            "/en/public/javascript/jobs.js",
        ),
    )
    metadata["pages_requested"] += 1
    script_match = re.search(
        r'''src=["']([^"']*/en/public/javascript/jobs\.js(?:\?[^"']*)?)["']''',
        identity,
        re.I,
    )
    if not script_match:
        raise AdapterError("Valve official careers page did not identify its jobs script")
    script_url = urljoin(str(target.careers_url), unescape(script_match.group(1)))
    script_response = _get(session, script_url, timeout)
    script_final = urlparse(str(getattr(script_response, "url", script_url)))
    script_content = bytes(getattr(script_response, "content", b"") or b"")
    if (
        script_final.scheme != "https"
        or script_final.hostname != "www.valvesoftware.com"
        or script_final.path != "/en/public/javascript/jobs.js"
        or not script_content
        or len(script_content) > int(target.max_script_bytes)
        or b"https://www.valvesoftware.com/en/jobs/job-search?search="
        not in script_content
    ):
        raise AdapterError("Valve official jobs script did not bind the reviewed endpoint")
    metadata["pages_requested"] += 1
    response = _get(session, str(target.endpoint_url), timeout)
    payload = _json(response, target)
    if str(getattr(response, "url", "")) != str(target.endpoint_url):
        raise AdapterError("Valve jobs API redirected outside its reviewed endpoint")
    if not isinstance(payload, Mapping):
        raise AdapterError("Valve unfiltered jobs API must return an ID-keyed object")
    jobs: list[JobRecord] = []
    for key, row in payload.items():
        if not isinstance(row, Mapping) or str(row.get("reqid") or "") != str(key):
            raise AdapterError("Valve jobs API key conflicts with its stable requisition ID")
        jobs.append(_normalize_valve(row, target))
    return _finish_atomic_collection(
        metadata,
        jobs,
        evidence_type="valve_unfiltered_atomic_collection",
        evidence_value=str(target.endpoint_url),
        pages_completed=3,
    )


def _normalize_valve(row: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    external_id = str(row.get("reqid") or "").strip()
    title = str(row.get("name") or "").strip()
    if not external_id.isdigit() or not title:
        raise AdapterError("Valve job is missing a stable numeric ID or title")
    url = _official_url(
        f"https://www.valvesoftware.com/en/jobs?job_id={external_id}", target
    )
    return JobRecord(
        job_id=f"public_json:www.valvesoftware.com:{external_id}",
        title=title,
        company=target.name,
        location="Bellevue, Washington, United States",
        url=url,
        updated_at="",
        source="public_json:www.valvesoftware.com:job-search",
        description=_html_text(row.get("description")) or None,
        offices=["Bellevue, Washington, United States"],
        raw_metadata={
            "external_job_id": external_id,
            "platform": "public_json",
            "endpoint_variant": VALVE_JOBS,
        },
    )


def _fetch_shazamme_collection(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    """Fetch a complete client-side collection after proving its official embed."""
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)
    metadata["pages_requested"] = 1
    identity_response = session.get(
        str(target.careers_url),
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if identity_response.status_code != 200:
        raise AdapterError(
            f"Shazamme official careers identity HTTP {identity_response.status_code}"
        )
    identity_content = bytes(getattr(identity_response, "content", b"") or b"")
    if not identity_content or len(identity_content) > int(target.max_response_bytes):
        raise AdapterError("Shazamme official careers identity response exceeded its bound")
    final_host = (
        urlparse(str(getattr(identity_response, "url", target.careers_url))).hostname
        or ""
    ).casefold()
    if final_host not in {str(value).casefold() for value in target.allowed_apply_hosts}:
        raise AdapterError("Shazamme official careers page redirected outside allowed hosts")
    identity_text = identity_content.decode("utf-8", errors="replace")
    if (
        str(target.site_id) not in identity_text
        or not re.search(str(target.identity_regex), identity_text, re.I)
    ):
        raise AdapterError("Shazamme official careers page did not bind the configured site")

    metadata["pages_requested"] += 1
    response = _get(session, str(target.endpoint_url), timeout)
    payload = _json(response, target)
    metadata["pages_completed"] = 2
    authoritative_total = _validate_shazamme_atomic_response(response, payload, target)
    if len(payload) > MAX_RAW_JOBS_PER_SOURCE:
        raise AdapterError("Shazamme job results exceeded the hard job ceiling")

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in payload:
        job = _normalize_shazamme(row, target)
        if job.job_id in seen_ids or job.url in seen_urls:
            raise AdapterError("Shazamme job results repeated a stable ID or canonical URL")
        seen_ids.add(job.job_id)
        seen_urls.add(job.url)
        jobs.append(job)
    total = len(jobs)
    if authoritative_total is not None:
        record_total_observation(
            metadata,
            authoritative_total,
            offset=0,
            source_label="Shazamme job results",
        )
    metadata.update(
        jobs_normalized=total,
        total_available=total,
        scan_complete=True,
        scan_completeness_reason="shazamme_atomic_collection_contract",
        stop_reason="shazamme_atomic_collection_contract",
        complete_collection_response=True,
        atomic_collection_contract=True,
        source_identity_status="verified",
    )
    record_terminal_evidence(
        metadata,
        "shazamme_atomic_collection_contract",
        str(target.endpoint_url),
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    return jobs


def _normalize_shazamme(row: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    contract = SHAZAMME_ATOMIC_COLLECTIONS[str(target.site_id).casefold()]
    data = row.get("data")
    if not isinstance(data, Mapping):
        raise AdapterError("Shazamme row is missing its data object")
    external_id = str(data.get("jobID") or "").strip()
    reference = str(data.get("referenceNumber") or "").strip()
    title = _html_text(data.get("jobName"))
    if not UUID_RE.fullmatch(external_id) or not reference or not title:
        raise AdapterError("Shazamme job is missing a stable UUID, requisition, or title")
    if data.get("activeStatus") is not True:
        raise AdapterError("Shazamme complete collection contained a non-active job")
    if str(data.get("siteID") or "").casefold() != str(target.site_id).casefold():
        raise AdapterError("Shazamme job crossed configured site identity")
    attribution = str(data.get("advertiserName") or "").strip()
    if (
        attribution.casefold() != contract["advertiser"].casefold()
        or not re.fullmatch(str(target.job_attribution_regex), attribution, re.I)
    ):
        raise AdapterError("Shazamme job failed exact advertiser attribution")
    site_name = str(data.get("siteName") or "").strip()
    if (
        site_name.casefold() != contract["site_name"].casefold()
        or not re.search(str(target.identity_regex), site_name, re.I)
    ):
        raise AdapterError("Shazamme job failed exact site attribution")
    source_url = urlparse(str(data.get("jobURL") or ""))
    path = source_url.path
    if (
        source_url.scheme != "https"
        or (source_url.hostname or "").casefold() != contract["source_job_host"]
        or not re.fullmatch(r"/job-details/[a-z0-9-]+-jobs-[0-9]+", path, re.I)
        or source_url.query
        or source_url.fragment
    ):
        raise AdapterError("Shazamme job did not expose one canonical detail path")
    url = str(target.apply_url_template).replace("{path}", path)
    url = _official_url(url, target)
    locations = _unique_strings(
        data.get(field) for field in ("city", "state", "country")
    )
    category = str(data.get("category") or "").strip()
    return JobRecord(
        job_id=f"public_json:shazamme.io:{external_id}",
        title=title,
        company=target.name,
        location=", ".join(locations),
        url=url,
        updated_at=str(data.get("changedOnUTC") or data.get("postedDate") or ""),
        source=f"public_json:shazamme.io:{str(target.site_id).casefold()}",
        departments=[category] if category else [],
        offices=locations,
        description=_html_text(data.get("fullDescription")) or None,
        raw_metadata={
            "external_job_id": external_id,
            "requisition_id": reference,
            "site_id": str(target.site_id),
            "advertiser": attribution,
            "platform": "public_json",
            "endpoint_variant": SHAZAMME_JOB_RESULTS,
        },
    )


def _validate_shazamme_atomic_response(
    response: Any, payload: Any, target: CompanyTarget
) -> int | None:
    """Validate the one reviewed Shazamme full-collection response contract."""
    final_url = str(getattr(response, "url", "") or target.endpoint_url or "")
    if final_url != str(target.endpoint_url):
        raise AdapterError("Shazamme atomic collection redirected outside its reviewed endpoint")
    headers = {
        str(name).casefold(): str(value).strip()
        for name, value in dict(getattr(response, "headers", {}) or {}).items()
    }
    signaled = sorted(
        name for name in SHAZAMME_PAGINATION_HEADERS if headers.get(name)
    )
    if signaled:
        raise AdapterError(
            "Shazamme atomic collection exposed pagination or truncation signals: "
            + ", ".join(signaled)
        )
    if not isinstance(payload, list) or not all(
        isinstance(row, Mapping) and set(row) == {"data"} for row in payload
    ):
        raise AdapterError(
            "Shazamme atomic collection must be one unwrapped array of data rows"
        )
    for row in payload:
        data = row["data"]
        if not isinstance(data, Mapping):
            raise AdapterError("Shazamme atomic collection row is missing its data object")
        pagination_fields = sorted(
            str(name)
            for name in data
            if str(name).casefold() in SHAZAMME_PAGINATION_FIELDS
        )
        if pagination_fields:
            raise AdapterError(
                "Shazamme atomic collection row exposed pagination metadata: "
                + ", ".join(pagination_fields)
            )
    totals: list[int] = []
    for name in SHAZAMME_TOTAL_HEADERS:
        if not headers.get(name):
            continue
        value = headers[name]
        if not value.isdigit():
            raise AdapterError(f"Shazamme {name} must be a non-negative integer")
        totals.append(int(value))
    if totals and (len(set(totals)) != 1 or totals[0] != len(payload)):
        raise AdapterError("Shazamme source-provided total conflicts with returned rows")
    return totals[0] if totals else None


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def _fetch_aurora_index(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)
    metadata["pages_requested"] = 1
    response = _get(session, str(target.endpoint_url), timeout)
    payload = _json(response, target)
    metadata["pages_completed"] = 1
    if not isinstance(payload, Mapping):
        raise AdapterError("Aurora jobs index must be a JSON object")
    rows = payload.get("jobs")
    raw_index = payload.get("index")
    if not isinstance(rows, list) or not isinstance(raw_index, str):
        raise AdapterError("Aurora jobs index is missing jobs or index")
    try:
        index = json.loads(raw_index)
    except (TypeError, ValueError) as exc:
        raise AdapterError("Aurora jobs index metadata is malformed") from exc
    if not isinstance(index, Mapping):
        raise AdapterError("Aurora jobs index metadata must be an object")
    total = _nonnegative_int(index.get("documentCount"), "documentCount")
    next_id = _nonnegative_int(index.get("nextId"), "nextId")
    document_ids = index.get("documentIds")
    if not isinstance(document_ids, Mapping):
        raise AdapterError("Aurora jobs index documentIds must be an object")
    indexed_ids = [str(value or "").strip() for value in document_ids.values()]
    row_ids = [str(row.get("id") or "").strip() if isinstance(row, Mapping) else "" for row in rows]
    if (
        total != len(rows)
        or next_id != total
        or len(indexed_ids) != total
        or len(set(row_ids)) != len(row_ids)
        or set(indexed_ids) != set(row_ids)
        or total > MAX_RAW_JOBS_PER_SOURCE
    ):
        raise AdapterError("Aurora jobs index did not prove one complete unique collection")

    jobs = [_normalize_aurora(row, target) for row in rows]
    record_total_observation(metadata, total, offset=0, source_label="Aurora jobs index")
    metadata.update(
        jobs_normalized=len(jobs),
        total_available=total,
        scan_complete=True,
        scan_completeness_reason="complete_collection_response",
        stop_reason="complete_collection_response",
        complete_collection_response=True,
    )
    record_terminal_evidence(
        metadata,
        "authoritative_index_document_count",
        total,
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    return jobs


def _fetch_wordpress_once(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    metadata = _metadata(target)
    publish_source_scan_metadata(session, metadata)
    page_size = max(1, min(100, int(target.page_size or 20)))
    maximum_pages = max(
        1, min(int(target.max_pages or 1), MAX_LISTING_REQUESTS_PER_SOURCE)
    )
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    expected_pages: int | None = None
    for page in range(1, maximum_pages + 1):
        metadata["pages_requested"] += 1
        response = _get(
            session,
            str(target.endpoint_url),
            timeout,
            params={
                "_adjust_ids": "true",
                "locations_slug": "",
                "teams_slug": "",
                "order": "asc",
                "orderby": "title",
                "per_page": str(page_size),
                "page": str(page),
            },
        )
        rows = _json(response, target)
        metadata["pages_completed"] += 1
        if not isinstance(rows, list):
            raise AdapterError("WordPress roles response must be a JSON list")
        total = _header_int(response, "x-wp-total")
        total_pages = _header_int(response, "x-wp-totalpages")
        record_total_observation(
            metadata,
            total,
            offset=(page - 1) * page_size,
            source_label="WordPress roles",
        )
        if total > MAX_RAW_JOBS_PER_SOURCE:
            raise AdapterError("WordPress roles total exceeds the hard job ceiling")
        calculated_pages = max(1, ceil(total / page_size))
        if total_pages != calculated_pages:
            raise AdapterError("WordPress roles page count conflicts with its total")
        if expected_pages is None:
            expected_pages = total_pages
        elif total_pages != expected_pages:
            metadata["collection_changed_during_scan"] = True
        if total_pages > maximum_pages:
            metadata["safety_limit_reached"] = True
            metadata["stop_reason"] = "maximum_pages_reached"
            metadata["scan_completeness_reason"] = "safety_limit_reached"
            return jobs
        page_ids: set[str] = set()
        normalized: list[JobRecord] = []
        for row in rows:
            try:
                job = _normalize_wordpress(row, target)
            except AdapterError as exc:
                metadata["rejected_record_count"] += 1
                if len(metadata["diagnostics"]) < 10:
                    metadata["diagnostics"].append({"page": page, "error": str(exc)})
                continue
            if job.job_id in page_ids or job.job_id in seen_ids:
                metadata["repeated_stable_ids"] = True
                metadata["stop_reason"] = "repeated_stable_ids"
                metadata["scan_completeness_reason"] = "pagination_failure"
                return jobs
            page_ids.add(job.job_id)
            normalized.append(job)
        jobs.extend(normalized)
        seen_ids.update(page_ids)
        metadata["jobs_normalized"] = len(jobs)
        metadata["pages"].append(
            {"page": page, "jobs_returned": len(rows), "jobs_normalized": len(normalized)}
        )
        if page < total_pages and len(rows) != page_size:
            metadata["stop_reason"] = "short_page_before_reported_total"
            metadata["scan_completeness_reason"] = "pagination_failure"
            return jobs
        if page == total_pages:
            if len(jobs) != total:
                metadata["stop_reason"] = "terminal_page_count_mismatch"
                metadata["scan_completeness_reason"] = "pagination_failure"
                return jobs
            metadata.update(
                total_available=total,
                scan_complete=not metadata["collection_changed_during_scan"],
                scan_completeness_reason=(
                    "reported_total_reached"
                    if not metadata["collection_changed_during_scan"]
                    else "collection_changed_during_scan"
                ),
                stop_reason=(
                    "reported_total_reached"
                    if not metadata["collection_changed_during_scan"]
                    else "collection_changed_during_scan"
                ),
                terminal_page_fully_processed=(
                    metadata["rejected_record_count"] == 0
                ),
            )
            record_terminal_evidence(
                metadata,
                "wordpress_total_pages",
                total_pages,
                has_more=False,
                terminal_page_observed=True,
            )
            if metadata["rejected_record_count"]:
                metadata["scan_complete"] = False
                metadata["scan_completeness_reason"] = "record_validation_failure"
                metadata["stop_reason"] = "terminal_reached_with_rejected_records"
            return jobs
    metadata["safety_limit_reached"] = True
    metadata["stop_reason"] = "maximum_pages_reached"
    metadata["scan_completeness_reason"] = "safety_limit_reached"
    return jobs


def _normalize_aurora(row: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("Aurora jobs index contained a malformed row")
    external_id = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    locations = row.get("locations")
    if not UUID_RE.fullmatch(external_id) or not title or not isinstance(locations, list):
        raise AdapterError("Aurora job is missing a stable UUID, title, or locations")
    clean_locations = [str(value).strip() for value in locations if str(value).strip()]
    url = _official_url(str(row.get("applyLink") or ""), target)
    return JobRecord(
        job_id=f"public_json:{urlparse(str(target.endpoint_url)).hostname}:{external_id}",
        title=title,
        company=target.name,
        location=", ".join(clean_locations),
        url=url,
        updated_at=str(row.get("updatedAt") or row.get("publishedDate") or ""),
        source=f"public_json:{urlparse(str(target.endpoint_url)).hostname}",
        departments=[str(row.get("category"))] if row.get("category") else [],
        offices=clean_locations,
        raw_metadata={
            "external_job_id": external_id,
            "ats_job_id": str(row.get("jobId") or ""),
            "employment_type": str(row.get("employmentType") or ""),
            "is_remote": bool(row.get("isRemote")),
            "platform": "public_json",
        },
    )


def _normalize_wordpress(row: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("WordPress roles collection contained a malformed row")
    value = row.get("id")
    external_id = str(value) if isinstance(value, int) and not isinstance(value, bool) else ""
    title_node = row.get("title")
    title = _html_text(title_node.get("rendered")) if isinstance(title_node, Mapping) else ""
    meta = row.get("meta")
    if not external_id or not title or not isinstance(meta, Mapping):
        raise AdapterError("WordPress role is missing an integer ID, title, or metadata")
    location = next(
        (
            str(meta.get(field) or "").strip()
            for field in ("ats_location", "ashby_location", "greenhouse_location")
            if str(meta.get(field) or "").strip()
        ),
        "",
    )
    url = _official_url(str(row.get("link") or ""), target)
    return JobRecord(
        job_id=f"public_json:{urlparse(str(target.endpoint_url)).hostname}:{external_id}",
        title=title,
        company=target.name,
        location=location,
        url=url,
        updated_at=str(row.get("modified_gmt") or row.get("modified") or ""),
        source=f"public_json:{urlparse(str(target.endpoint_url)).hostname}",
        offices=[location] if location else [],
        raw_metadata={
            "external_job_id": external_id,
            "ats_req_id": str(meta.get("ats_req_id") or meta.get("ashby_req_id") or ""),
            "ats_platform": str(meta.get("ats_platform") or ""),
            "is_remote": bool(
                meta.get("ats_remote") or meta.get("ashby_remote") or meta.get("greenhouse_remote")
            ),
            "platform": "public_json",
        },
    )


def _get(session: requests.Session, url: str, timeout: int, **kwargs: Any) -> Any:
    response = session.get(url, headers=HEADERS, timeout=min(max(1, int(timeout)), 30), **kwargs)
    if response.status_code in {401, 407}:
        raise AdapterError(f"Public JSON authentication required (HTTP {response.status_code})")
    if response.status_code in {403, 429, 451}:
        raise AdapterError(f"Public JSON endpoint blocked (HTTP {response.status_code})")
    if response.status_code != 200:
        raise AdapterError(f"Public JSON HTTP {response.status_code}")
    return response


def _json(response: Any, target: CompanyTarget) -> Any:
    content = bytes(getattr(response, "content", b"") or b"")
    if not content:
        text = str(getattr(response, "text", "") or "")
        content = text.encode("utf-8")
    if not content or len(content) > int(target.max_response_bytes):
        raise AdapterError("Public JSON response was empty or exceeded the byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if "json" not in content_type.casefold():
        raise AdapterError("Public JSON endpoint returned a non-JSON response")
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise AdapterError("Public JSON endpoint returned malformed JSON") from exc


def _official_url(value: str, target: CompanyTarget) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    allowed = any(
        host == str(candidate).casefold()
        or host.endswith("." + str(candidate).casefold())
        for candidate in target.allowed_apply_hosts
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise AdapterError("Public JSON job URL is not an approved anonymous HTTPS URL")
    return value


def _header_int(response: Any, name: str) -> int:
    value = str(getattr(response, "headers", {}).get(name, "")).strip()
    if not value.isdigit():
        raise AdapterError(f"WordPress roles response is missing valid {name}")
    return int(value)


def _nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterError(f"Public JSON {name} must be a non-negative integer")
    return value


def _html_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(str(value or "")))
    return " ".join(text.split())
