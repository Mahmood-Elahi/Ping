"""Conservative adapters for reviewed, same-origin public JSON collections."""

from __future__ import annotations

from html import unescape
import json
from math import ceil
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

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
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    initialize_mutable_collection,
    record_total_observation,
)


AURORA_JOBS_INDEX = "aurora_jobs_index_v1"
WORDPRESS_ROLES = "wordpress_roles_v1"
SUPPORTED_VARIANTS = {AURORA_JOBS_INDEX, WORDPRESS_ROLES}
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    validate_target(target)
    if target.endpoint_variant == WORDPRESS_ROLES:
        return fetch_with_mutation_restart(
            session, lambda: _fetch_wordpress_once(session, target, timeout)
        )
    return _fetch_aurora_index(session, target, timeout)


def validate_target(target: CompanyTarget) -> None:
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"Public JSON unsupported variant: {target.endpoint_variant}")
    if not target.endpoint_url or not target.allowed_apply_hosts:
        raise AdapterError("Public JSON target requires endpoint_url and allowed_apply_hosts")
    endpoint = urlparse(str(target.endpoint_url))
    careers = urlparse(str(target.careers_url or ""))
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
