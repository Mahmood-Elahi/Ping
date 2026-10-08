"""Strict adapter for ByteDance's reviewed first-party career-search API."""

from __future__ import annotations

import math
import re
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.listing_completion import record_terminal_evidence
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


BYTEDANCE_SUPPLIER = "bytedance_supplier_search_v1"
SUPPORTED_VARIANTS = {BYTEDANCE_SUPPLIER}
PAGE_SIZE = 100
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.25
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
HARD_LIMITS = {BYTEDANCE_SUPPLIER: (20, 2_000)}
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    return fetch_with_mutation_restart(
        session, lambda: _fetch_jobs_once(session, target, timeout)
    )


def _fetch_jobs_once(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> list[JobRecord]:
    validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    identity = _get(session, str(target.careers_url), timeout)
    _validate_identity(identity, target)

    metadata = SourceScanMetadata(
        endpoint_variant=target.endpoint_variant,
        request_method="POST",
        page_size=int(target.page_size),
        maximum_pages=int(target.max_pages),
        maximum_results=int(target.max_jobs),
        pages=[],
        source_identity_status="validated",
        original_configured_url=str(target.careers_url),
        final_url=str(getattr(identity, "url", "") or target.careers_url),
        final_host=(
            urlparse(str(getattr(identity, "url", "") or target.careers_url)).hostname
            or ""
        ).casefold(),
        host_remained_approved=True,
        diagnostics=[],
        warnings=[],
    )
    initialize_mutable_collection(metadata)
    publish_source_scan_metadata(session, metadata)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: dict[str, str] = {}
    total: int | None = None
    page_size = int(target.page_size)

    for page_index in range(int(target.max_pages)):
        offset = page_index * page_size
        metadata["pages_requested"] += 1
        payload = _request_payload(target, offset, page_size)
        response = _post(session, str(target.endpoint_url), payload, timeout)
        parsed = _json(response, target)
        rows, observed_total = _collection(parsed, target, offset, page_size)
        metadata["pages_completed"] += 1
        record_total_observation(
            metadata,
            observed_total,
            offset=offset,
            source_label="First-party careers API",
        )
        if total is None:
            total = observed_total
        elif total != observed_total:
            total = observed_total

        page_record = {
            "page": page_index + 1,
            "offset": offset,
            "jobs_returned": len(rows),
            "jobs_normalized": 0,
        }
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])

        if page_index == 0:
            required_pages = math.ceil(observed_total / page_size) if observed_total else 0
            if (
                observed_total > int(target.max_jobs)
                or required_pages > int(target.max_pages)
            ):
                metadata["safety_limit_reached"] = True
                metadata["stop_reason"] = "hard_safety_limit_exceeded"
                metadata["scan_completeness_reason"] = "safety_limit_reached"
                return []

        for row in rows:
            job = _normalize(row, target)
            canonical_url = job.url.rstrip("/").casefold()
            if job.job_id in seen_ids:
                mark_collection_changed(metadata, offset=offset, overlap_count=1)
                continue
            prior_id = seen_urls.get(canonical_url)
            if prior_id is not None and prior_id != job.job_id:
                raise AdapterError(
                    "First-party careers API reused one canonical URL for distinct stable IDs"
                )
            seen_ids.add(job.job_id)
            seen_urls[canonical_url] = job.job_id
            jobs.append(job)
            page_record["jobs_normalized"] += 1

        metadata["jobs_normalized"] = len(jobs)
        if len(rows) > page_size:
            raise AdapterError("First-party careers API exceeded its requested page size")
        if observed_total == 0:
            if rows:
                raise AdapterError("First-party careers API returned jobs with a zero total")
            break
        if offset + len(rows) < observed_total and len(rows) != page_size:
            raise AdapterError("First-party careers API returned a short page before its total")
        if offset + len(rows) >= observed_total:
            break
        time.sleep(RETRY_DELAY_SECONDS)

    if total is None:
        raise AdapterError("First-party careers API did not return an authoritative total")
    if metadata.get("collection_changed_during_scan"):
        metadata["stop_reason"] = "collection_changed_during_scan"
        metadata["scan_completeness_reason"] = "collection_changed_during_scan"
        return jobs
    if len(jobs) != total:
        raise AdapterError(
            f"First-party careers API normalized {len(jobs)} jobs but reported {total}"
        )
    record_terminal_evidence(
        metadata,
        "authoritative_total",
        total,
        has_more=False,
        terminal_page_observed=True,
    )
    metadata["scan_complete"] = True
    metadata["scan_completeness_reason"] = (
        "validated_zero_jobs" if total == 0 else "reported_total_reached"
    )
    metadata["stop_reason"] = metadata["scan_completeness_reason"]
    return jobs


def validate_target(target: CompanyTarget) -> dict[str, Any]:
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError("First-party JSON endpoint_variant is unsupported")
    required = {
        "careers_url": target.careers_url,
        "endpoint_url": target.endpoint_url,
        "identity_regex": target.identity_regex,
        "allowed_source_hosts": target.allowed_source_hosts,
        "allowed_apply_hosts": target.allowed_apply_hosts,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise AdapterError(f"First-party JSON target is missing {', '.join(missing)}")
    for field_name in ("careers_url", "endpoint_url"):
        parsed = urlparse(str(getattr(target, field_name)))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise AdapterError(f"First-party JSON {field_name} must be anonymous HTTPS")
    try:
        re.compile(str(target.identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"First-party JSON identity_regex is invalid: {exc}") from exc
    source_hosts = _hosts(target.allowed_source_hosts, "source")
    apply_hosts = _hosts(target.allowed_apply_hosts, "apply")
    endpoint = urlparse(str(target.endpoint_url))
    if (endpoint.hostname or "").casefold() not in source_hosts:
        raise AdapterError("First-party JSON endpoint host is not allowlisted")
    max_pages, max_jobs = HARD_LIMITS[str(target.endpoint_variant)]
    if not 1 <= int(target.max_pages) <= max_pages:
        raise AdapterError(f"First-party JSON max_pages cannot exceed {max_pages}")
    if not 1 <= int(target.max_jobs) <= max_jobs:
        raise AdapterError(f"First-party JSON max_jobs cannot exceed {max_jobs}")
    if not 1 <= int(target.page_size) <= PAGE_SIZE:
        raise AdapterError(f"First-party JSON page_size cannot exceed {PAGE_SIZE}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("First-party JSON response byte limit exceeds the hard maximum")

    if str(target.endpoint_url) != (
        "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts"
    ):
        raise AdapterError("ByteDance supplier endpoint must match the reviewed public route")
    if str(target.apply_url_template) != "https://joinbytedance.com/search/{id}":
        raise AdapterError("ByteDance apply URL template is not the reviewed official route")
    return {
        "source_hosts": source_hosts,
        "apply_hosts": apply_hosts,
        "variant": target.endpoint_variant,
    }


def _request_payload(target: CompanyTarget, offset: int, limit: int) -> dict[str, Any]:
    return {
        "recruitment_id_list": [],
        "job_category_id_list": [],
        "subject_id_list": [],
        "location_code_list": [],
        "keyword": "",
        "limit": limit,
        "offset": offset,
    }


def _collection(
    payload: Mapping[str, Any],
    target: CompanyTarget,
    offset: int,
    limit: int,
) -> tuple[list[Mapping[str, Any]], int]:
    if payload.get("code") != 0 or payload.get("message") != "ok":
        raise AdapterError("ByteDance supplier API returned an application error")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise AdapterError("ByteDance supplier API omitted its data object")
    rows, total = data.get("job_post_list"), data.get("count")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise AdapterError("First-party careers API returned a malformed job collection")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("First-party careers API returned an invalid authoritative total")
    return list(rows), total


def _normalize(row: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    external_id = _text(row.get("id"))
    if not re.fullmatch(r"[1-9][0-9]{10,24}", external_id):
        raise AdapterError("ByteDance supplier job has an invalid stable ID")
    title = _text(row.get("title"))
    url = str(target.apply_url_template).format(id=external_id)
    locations = _location_chain(row.get("city_info"))
    departments = _named_values(row.get("job_category"))
    updated_at = ""
    description_parts = [
        _text(row.get("description")),
        _text(row.get("requirement")),
    ]
    description = "\n\n".join(value for value in description_parts if value) or None
    parsed = urlparse(url)
    allowed = {str(host).casefold() for host in target.allowed_apply_hosts}
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in allowed:
        raise AdapterError("First-party careers API returned an off-domain apply URL")
    if not title:
        raise AdapterError("First-party careers API job is missing a title")
    endpoint_host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    source = f"first_party_json:{endpoint_host}"
    return JobRecord(
        job_id=f"{source}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=updated_at,
        source=source,
        departments=departments,
        offices=locations,
        description=description,
        raw_metadata={
            "platform": "first_party_json",
            "endpoint_variant": target.endpoint_variant,
            "external_job_id": external_id,
        },
    )


def _get(session: requests.Session, url: str, timeout: int) -> Any:
    return _request(session, "get", url, timeout=timeout, headers={"User-Agent": HEADERS["User-Agent"]})


def _post(
    session: requests.Session,
    url: str,
    payload: Mapping[str, Any],
    timeout: int,
) -> Any:
    headers = dict(HEADERS)
    headers.update(
        {
            "Accept-Language": "en-US",
            "Website-Path": "en",
            "X-Tt-Env": "boe_epam_api",
        }
    )
    return _request(session, "post", url, timeout=timeout, headers=headers, json=dict(payload))


def _request(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = getattr(session, method)(url, **kwargs)
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
            raise AdapterError(f"First-party careers API authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"First-party careers API blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"First-party careers API HTTP {status}")
        return response
    raise AdapterError("First-party careers API request attempts exhausted")


def _json(response: Any, target: CompanyTarget) -> Mapping[str, Any]:
    final = urlparse(str(getattr(response, "url", "") or target.endpoint_url))
    allowed = {str(host).casefold() for host in target.allowed_source_hosts}
    if final.scheme != "https" or (final.hostname or "").casefold() not in allowed:
        raise AdapterError("First-party careers API redirected outside allowed source hosts")
    content = getattr(response, "content", b"")
    if content and len(content) > int(target.max_response_bytes):
        raise AdapterError("First-party careers API response exceeded the byte limit")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise AdapterError("First-party careers API returned malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("First-party careers API returned a non-object JSON response")
    return payload


def _validate_identity(response: Any, target: CompanyTarget) -> None:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if len(content) > int(target.max_response_bytes):
        raise AdapterError("First-party careers identity page exceeded the byte limit")
    text = bytes(content).decode("utf-8", errors="replace")
    if not re.search(str(target.identity_regex), text, re.IGNORECASE):
        raise AdapterError("First-party careers identity page did not match the company")
    final = urlparse(str(getattr(response, "url", "") or target.careers_url))
    original = urlparse(str(target.careers_url))
    allowed = {
        (original.hostname or "").casefold(),
        *(str(host).casefold() for host in target.allowed_apply_hosts),
    }
    if final.scheme != "https" or (final.hostname or "").casefold() not in allowed:
        raise AdapterError("First-party careers identity page redirected off-domain")


def _hosts(values: list[str], label: str) -> set[str]:
    hosts = {str(value).strip().casefold() for value in values if str(value).strip()}
    if not hosts or any("/" in host or ":" in host or "." not in host for host in hosts):
        raise AdapterError(f"First-party JSON {label} hosts must be hostname-only")
    return hosts


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _location_chain(value: Any) -> list[str]:
    result: list[str] = []
    current = value
    while isinstance(current, Mapping):
        name = _text(current.get("i18n_name") or current.get("en_name") or current.get("name"))
        if name and name.casefold() not in {item.casefold() for item in result}:
            result.append(name)
        current = current.get("parent")
    return result


def _named_values(value: Any) -> list[str]:
    return _location_chain(value)
