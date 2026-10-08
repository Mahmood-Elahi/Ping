"""Anonymous M-Cloud career-web-site Google Jobs search adapter."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urljoin, urlparse, urlunparse

import requests

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    PageSizeObservation,
)
from src.scraper.company_careers.mutable_collection import (
    finish_changed,
    finish_terminal,
    immutable_job_fingerprint,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


ENDPOINT_VARIANT = "google_jobs_v1"
MAX_PAGES = 2
MAX_RESULTS = 20
MAX_PAGE_SIZE = 20
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 3_000_000
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
JSON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
ORG_RE = re.compile(r"^companies/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> list[JobRecord]:
    validate_target(target)
    maximum_pages = int(target.max_pages)
    maximum_jobs = int(target.max_jobs)
    configured_page_size = int(target.page_size)
    page_size = MAX_PAGE_SIZE if configured_page_size == 10 else configured_page_size
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "organization_id": target.organization_id,
        "page_size": page_size,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "total_available": None,
        "initial_total_available": None,
        "final_total_available": None,
        "observed_total_counts": [],
        "total_count_changed": False,
        "pages": [],
        "pages_fetched": 0,
        "jobs_normalized": 0,
        "stop_reason": "not_started",
        "repeated_page": False,
        "zero_job_contract": False,
        "identity_binding_verified": False,
        "job_detail_path": None,
        "declared_ats_hosts": [],
        "diagnostics": [],
        "warnings": [],
    })
    setattr(session, "mcloud_cws_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    initialize_mutable_collection(metadata)
    listing_budget = AdaptiveListingCompletion(
        maximum_pages,
        maximum_jobs,
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)
    page_sizes = PageSizeObservation(page_size)
    page_sizes.record(metadata)
    job_detail_path, declared_ats_hosts = _verify_identity_binding(
        session, target, timeout
    )
    metadata["identity_binding_verified"] = True
    metadata["job_detail_path"] = job_detail_path
    metadata["declared_ats_hosts"] = sorted(declared_ats_hosts)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_fingerprints: dict[str, tuple[Any, ...]] = {}
    seen_pages: set[tuple[str, ...]] = set()
    positions_seen = 0
    for page_index in range(listing_budget.hard_request_ceiling):
        effective_jobs = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        offset = positions_seen
        metadata["pages_requested"] += 1
        response = _get(
            session,
            str(target.endpoint_url),
            headers=JSON_HEADERS,
            timeout=timeout,
            params={
                "companyName": str(target.organization_id),
                "pageSize": page_size,
                "offset": offset,
                "orderBy": "posting_publish_time desc",
            },
        )
        _validate_final_url(response, target.allowed_source_hosts, "search endpoint")
        items, total, zero_contract = _parse_response(response, target)
        metadata["pages_completed"] += 1
        _record_total_observation(metadata, offset, total)
        next_positions_seen = positions_seen + len(items)
        if (
            not metadata["collection_changed_during_scan"]
            and next_positions_seen > total
        ):
            raise AdapterError("M-Cloud result counts exceed the reported total")
        page_sizes.observe(
            len(items), terminal=next_positions_seen >= total
        )
        page_sizes.record(metadata)
        listing_budget.record_decision(
            metadata,
            total,
            page_sizes.effective_size,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=next_positions_seen,
        )
        effective_jobs = int(metadata["effective_listing_job_ceiling"])
        identities = tuple(sorted(_job_id(item) for item in items))
        page_record = {
            "page": page_index + 1,
            "offset": offset,
            "url": str(getattr(response, "url", target.endpoint_url)),
            "jobs_returned": len(items),
            "jobs_normalized": 0,
            "reported_total": total,
        }
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])
        if items and identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("M-Cloud pagination repeated a complete page")
        seen_pages.add(identities)

        new_ids = 0
        for item in items:
            job = _normalize(
                item,
                target,
                job_detail_path=job_detail_path,
                declared_ats_hosts=declared_ats_hosts,
            )
            if job.job_id in seen_ids:
                if seen_fingerprints[job.job_id] != immutable_job_fingerprint(job):
                    metadata["stop_reason"] = "conflicting_repeated_job_id"
                    metadata["scan_completeness_reason"] = "pagination_failure"
                    raise AdapterError(
                        "M-Cloud returned conflicting records for a stable job ID"
                    )
                mark_collection_changed(metadata, offset=offset, overlap_count=1)
                continue
            seen_ids.add(job.job_id)
            seen_fingerprints[job.job_id] = immutable_job_fingerprint(job)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= effective_jobs:
                break
        page_record["jobs_normalized"] = new_ids
        metadata["jobs_normalized"] = len(jobs)
        positions_seen = next_positions_seen

        if metadata["collection_changed_during_scan"]:
            finish_changed(metadata, terminal_evidence="collection_changed_during_scan")
            break

        if items and new_ids == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("M-Cloud pagination returned no new stable job IDs")

        if page_index == 0 and not items:
            if not zero_contract:
                raise AdapterError("M-Cloud response did not prove an empty jobs collection")
            metadata["zero_job_contract"] = True
            finish_terminal(
                metadata,
                complete_reason="reported_total_reached",
                stop_reason="validated_zero_jobs",
            )
            break
        if not items:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="empty_page_before_reported_total")
            else:
                metadata["stop_reason"] = "empty_page_before_reported_total"
                metadata["scan_completeness_reason"] = "pagination_failure"
            break
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        latest_total = metadata["final_total_available"]
        if latest_total is not None and positions_seen >= latest_total:
            finish_terminal(
                metadata,
                complete_reason="reported_total_reached",
                stop_reason="total_jobs_reached",
            )
            break
        if page_index + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
        if page_index + 1 < listing_budget.hard_request_ceiling:
            time.sleep(RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["safety_limit_reached"] = True
        if metadata["collection_changed_during_scan"]:
            finish_changed(metadata, terminal_evidence=metadata["stop_reason"])
        else:
            metadata["scan_completeness_reason"] = "safety_limit_reached"
    return jobs


def _record_total_observation(
    metadata: SourceScanMetadata, offset: int, total: int
) -> None:
    record_total_observation(
        metadata, total, offset=offset, source_label="M-Cloud"
    )


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("careers_url", target.careers_url),
            ("endpoint_url", target.endpoint_url),
            ("endpoint_variant", target.endpoint_variant),
            ("organization_id", target.organization_id),
            ("identity_regex", target.identity_regex),
            ("job_attribution_regex", target.job_attribution_regex),
            ("allowed_source_hosts", target.allowed_source_hosts),
            ("allowed_apply_hosts", target.allowed_apply_hosts),
        )
        if not value
    ]
    if missing:
        raise AdapterError(f"M-Cloud target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"M-Cloud endpoint_variant must be {ENDPOINT_VARIANT}")
    _require_https(str(target.careers_url), "careers_url")
    _require_https(str(target.endpoint_url), "endpoint_url")
    static_html._validate_hosts(target.allowed_source_hosts)
    static_html._validate_hosts(target.allowed_apply_hosts)
    endpoint_host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    careers_host = (urlparse(str(target.careers_url)).hostname or "").casefold()
    if not _allowed_host(endpoint_host, target.allowed_source_hosts):
        raise AdapterError("M-Cloud endpoint_url is outside allowed_source_hosts")
    if not _allowed_host(careers_host, target.allowed_apply_hosts):
        raise AdapterError("M-Cloud careers_url is outside allowed_apply_hosts")
    if not ORG_RE.fullmatch(str(target.organization_id)):
        raise AdapterError("M-Cloud organization_id is invalid")
    static_html._safe_regex(str(target.identity_regex))
    static_html._safe_regex(str(target.job_attribution_regex))
    if target.job_attribution_exclude_regex:
        static_html._safe_regex(str(target.job_attribution_exclude_regex))
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"M-Cloud max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"M-Cloud max_jobs must be between 1 and {MAX_RESULTS}")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"M-Cloud page_size must be between 1 and {MAX_PAGE_SIZE}")


def _verify_identity_binding(
    session: requests.Session, target: CompanyTarget, timeout: int
) -> tuple[str, frozenset[str]]:
    response = _get(
        session,
        str(target.careers_url),
        headers=HTML_HEADERS,
        timeout=timeout,
        params=None,
    )
    _validate_final_url(response, target.allowed_apply_hosts, "careers page")
    content = _content(response, expected="html")
    text = content.decode("utf-8", errors="replace")
    lowered = text.casefold()
    if any(
        token in lowered
        for token in (
            "cloudflare challenge",
            "security checkpoint",
            "access denied",
            "request blocked",
        )
    ):
        raise AdapterError("M-Cloud careers page returned an access challenge")
    if not static_html._safe_regex(str(target.identity_regex)).search(text):
        raise AdapterError("M-Cloud careers page failed exact company identity")
    config_match = re.search(r"var\s+cws_opts\s*=\s*(\{.*?\});", text, re.DOTALL)
    if not config_match:
        raise AdapterError("M-Cloud careers page no longer publishes cws_opts")
    try:
        config = json.loads(config_match.group(1))
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"M-Cloud careers page contains malformed cws_opts: {exc}") from exc
    if not isinstance(config, Mapping) or config.get("org") != str(target.organization_id):
        raise AdapterError("M-Cloud careers page no longer publishes the configured organization")
    endpoint_host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    configured_api_host = (
        urlparse(str(config.get("api") or "")).hostname or ""
    ).casefold()
    job_detail_path = str(config.get("job_detail_path") or "")
    if configured_api_host != endpoint_host or job_detail_path != "/job":
        raise AdapterError("M-Cloud careers page no longer publishes the configured API host")
    return job_detail_path, _declared_ats_hosts(config)


def _declared_ats_hosts(config: Mapping[str, Any]) -> frozenset[str]:
    raw = config.get("ATS_params")
    if raw in (None, ""):
        return frozenset()
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"M-Cloud careers page contains malformed ATS_params: {exc}") from exc
    rows = payload.get("CONFIG") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise AdapterError("M-Cloud careers page contains malformed ATS_params")
    hosts: set[str] = set()
    for row in rows:
        host = _text(row.get("ATS_HOST_NAME")).casefold().rstrip(".")
        if not host:
            continue
        static_html._validate_hosts([host])
        hosts.add(host)
    return frozenset(hosts)


def _get(
    session: requests.Session,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: int,
    params: Mapping[str, Any] | None,
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                url,
                headers=dict(headers),
                timeout=timeout,
                **({"params": dict(params)} if params is not None else {}),
            )
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
            raise AdapterError(f"M-Cloud authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"M-Cloud endpoint access was blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(f"M-Cloud HTTP {status}")
        return response
    raise AdapterError("M-Cloud request attempts exhausted")


def _parse_response(
    response: Any, target: CompanyTarget
) -> tuple[list[Mapping[str, Any]], int, bool]:
    content = _content(response, expected="json")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"M-Cloud returned malformed JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("M-Cloud response root is not an object")
    total = payload.get("totalHits")
    items = payload.get("searchResults")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("M-Cloud totalHits is invalid")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise AdapterError("M-Cloud searchResults is not a job-object list")
    if total < len(items):
        raise AdapterError("M-Cloud result count exceeds the reported total")
    return list(items), total, total == 0 and not items


def _normalize(
    item: Mapping[str, Any],
    target: CompanyTarget,
    *,
    job_detail_path: str,
    declared_ats_hosts: frozenset[str],
) -> JobRecord:
    job = item.get("job")
    if not isinstance(job, Mapping):
        raise AdapterError("M-Cloud result is missing its job object")
    external_id = _text(job.get("id"))
    title = _text(job.get("title"))
    if not external_id or not title or not external_id.isdigit():
        raise AdapterError("M-Cloud job is missing a stable numeric ID or title")
    attribution_text = " | ".join(
        _text(job.get(key))
        for key in ("company_name", "brand")
        if _text(job.get(key))
    )
    attribution = static_html._safe_regex(str(target.job_attribution_regex))
    exclusion = (
        static_html._safe_regex(str(target.job_attribution_exclude_regex))
        if target.job_attribution_exclude_regex
        else None
    )
    if not attribution_text or not attribution.search(attribution_text):
        raise AdapterError("M-Cloud job failed exact source attribution")
    if exclusion and exclusion.search(attribution_text):
        raise AdapterError("M-Cloud job matched excluded attribution")
    url = _official_url(
        job.get("url"),
        target,
        external_id=external_id,
        reference=_text(job.get("ref") or job.get("clientid")),
        job_detail_path=job_detail_path,
        declared_ats_hosts=declared_ats_hosts,
    )
    locations = _locations(job)
    departments = _unique(
        _text(job.get(key))
        for key in ("primary_category", "parent_category", "department")
        if _text(job.get(key)) and _text(job.get(key)).casefold() != "na"
    )
    host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"mcloud_cws:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=_text(job.get("open_date")),
        source=f"mcloud_cws:{host}:{target.organization_id}",
        departments=departments,
        offices=locations,
        raw_metadata={
            "platform": "mcloud_cws",
            "endpoint_variant": target.endpoint_variant,
            "organization_id": target.organization_id,
            "external_job_id": external_id,
            "reference": _text(job.get("ref")),
            "company_name": _text(job.get("company_name")),
            "brand": _text(job.get("brand")),
            "source_job_url": _text(job.get("url")),
        },
    )


def _job_id(item: Mapping[str, Any]) -> str:
    job = item.get("job")
    return _text(job.get("id")) if isinstance(job, Mapping) else ""


def _locations(job: Mapping[str, Any]) -> list[str]:
    primary = [
        _text(job.get(key))
        for key in ("primary_city", "primary_state", "primary_country")
        if _text(job.get(key))
    ]
    result = [", ".join(primary)] if primary else []
    additional = job.get("addtnl_locations")
    if isinstance(additional, list):
        for row in additional:
            if isinstance(row, Mapping):
                parts = [
                    _text(row.get(key))
                    for key in ("city", "state", "country")
                    if _text(row.get(key))
                ]
                if parts:
                    result.append(", ".join(parts))
    return _unique(result)


def _official_url(
    value: Any,
    target: CompanyTarget,
    *,
    external_id: str,
    reference: str,
    job_detail_path: str,
    declared_ats_hosts: frozenset[str],
) -> str:
    url = _text(value)
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise AdapterError("M-Cloud job has no official allowed-host URL")
    expected_path = rf"^{re.escape(job_detail_path.rstrip('/'))}/{re.escape(external_id)}(?:/|$)"
    if _allowed_host(host, target.allowed_apply_hosts):
        if not re.search(expected_path, parsed.path):
            raise AdapterError("M-Cloud job URL has no matching stable job path")
        return urlunparse(parsed._replace(fragment=""))
    if host not in declared_ats_hosts:
        raise AdapterError("M-Cloud job has no official allowed-host URL")
    if not reference or not _url_contains_reference(parsed, reference):
        raise AdapterError("M-Cloud declared ATS URL has no matching stable job reference")

    official_url = urljoin(
        str(target.careers_url),
        f"{job_detail_path.rstrip('/')}/{external_id}",
    )
    official = urlparse(official_url)
    official_host = (official.hostname or "").casefold()
    if official.scheme != "https" or not _allowed_host(
        official_host, target.allowed_apply_hosts
    ):
        raise AdapterError("M-Cloud canonical job route is outside allowed_apply_hosts")
    return urlunparse(official._replace(query="", fragment=""))


def _url_contains_reference(parsed: Any, reference: str) -> bool:
    values = [unquote(part) for part in parsed.path.split("/") if part]
    values.extend(unquote(value) for _key, value in parse_qsl(parsed.query))
    return reference in values


def _content(response: Any, *, expected: str) -> bytes:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if not content:
        raise AdapterError("M-Cloud returned an empty HTTP 200 response")
    if len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("M-Cloud response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", "")).casefold()
    if content_type and expected not in content_type:
        raise AdapterError(f"M-Cloud endpoint returned a non-{expected.upper()} response")
    return bytes(content)


def _validate_final_url(response: Any, allowed_hosts: Iterable[str], label: str) -> None:
    parsed = urlparse(str(getattr(response, "url", "") or ""))
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not _allowed_host(host, allowed_hosts):
        raise AdapterError(f"M-Cloud {label} redirected outside allowed hosts")


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(
        host == str(value).casefold() or host.endswith("." + str(value).casefold())
        for value in allowed
    )


def _require_https(url: str, field: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise AdapterError(f"M-Cloud {field} must be an anonymous HTTPS URL")
    if any(token in parsed.path.casefold() for token in ("/login", "/signin", "/session")):
        raise AdapterError(f"M-Cloud {field} cannot require authentication or session state")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value).strip(" |")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
