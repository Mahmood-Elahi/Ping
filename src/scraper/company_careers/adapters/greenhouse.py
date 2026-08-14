"""Bounded Greenhouse job board adapter."""

from __future__ import annotations

import json
import re
from typing import Any, List, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.student_roles import (
    classify_student_location,
    student_job_signals,
)


MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 5_000_000
MAX_DETAIL_RESPONSE_BYTES = 1_000_000
READ_CHUNK_BYTES = 64 * 1024
HEADERS = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}


def _secure_apply_url(value: object) -> str:
    """Canonicalize legacy Greenhouse HTTP links to their HTTPS endpoint."""
    parsed = urlparse(str(value or ""))
    if parsed.scheme.casefold() == "http" and parsed.hostname:
        return urlunparse(parsed._replace(scheme="https"))
    return str(value or "")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    """Fetch a compact board index and enrich at most ``max_jobs`` postings."""
    if not target.slug:
        raise AdapterError("Greenhouse target is missing slug")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Greenhouse max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Greenhouse response byte limit exceeds the hard maximum")
    if bool(target.job_attribution_field) != bool(target.job_attribution_regex):
        raise AdapterError(
            "Greenhouse per-job attribution requires both field and regex"
        )
    attribution_regex: re.Pattern[str] | None = None
    if target.job_attribution_regex:
        try:
            attribution_regex = re.compile(str(target.job_attribution_regex), re.IGNORECASE)
        except re.error as exc:
            raise AdapterError(f"Greenhouse attribution regex is invalid: {exc}") from exc
    exclusion_regex: re.Pattern[str] | None = None
    if target.job_attribution_exclude_regex:
        try:
            exclusion_regex = re.compile(
                str(target.job_attribution_exclude_regex), re.IGNORECASE
            )
        except re.error as exc:
            raise AdapterError(
                f"Greenhouse attribution exclusion regex is invalid: {exc}"
            ) from exc

    base_url = f"https://boards-api.greenhouse.io/v1/boards/{target.slug}"
    data, listing_bytes, listing_streamed = _get_json(
        session,
        f"{base_url}/jobs",
        timeout,
        int(target.max_response_bytes),
        target.slug,
        "response",
    )
    if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
        raise AdapterError("Greenhouse response is missing its stable job_id collection")

    allowed_departments = {
        str(value).strip().casefold()
        for value in target.department_allowlist
        if str(value).strip()
    }
    allowed_job_ids: set[str] | None = None
    department_bytes = 0
    requests_fetched = 1
    expanded_listing = not listing_streamed
    if allowed_departments and not expanded_listing:
        department_data, department_bytes, _ = _get_json(
            session,
            f"{base_url}/departments",
            timeout,
            int(target.max_response_bytes),
            target.slug,
            "department response",
        )
        requests_fetched += 1
        allowed_job_ids = _department_job_ids(department_data, allowed_departments)

    maximum_results = int(target.max_jobs)
    detail_limit = min(int(target.max_response_bytes), MAX_DETAIL_RESPONSE_BYTES)
    summaries: list[tuple[Mapping[str, Any], JobRecord]] = []
    attribution_filtered = 0
    for item in data["jobs"]:
        summary = _listing_record(item, target)
        if allowed_job_ids is not None and summary.job_id not in allowed_job_ids:
            continue
        if attribution_regex and not _has_exact_attribution(
            summary,
            str(target.job_attribution_field),
            attribution_regex,
            exclusion_regex,
        ):
            attribution_filtered += 1
            continue
        summaries.append((item, summary))

    prioritized_summaries, candidate_count = _prioritized_summaries(
        summaries, maximum_results
    )
    selected_summaries = prioritized_summaries[:maximum_results]
    jobs: list[JobRecord] = []
    detail_requests = 0
    detail_bytes = 0
    for item, summary in selected_summaries:
        detail = _embedded_detail(item, assume_embedded=expanded_listing)
        if detail is None:
            detail, response_bytes, _ = _get_json(
                session,
                f"{base_url}/jobs/{summary.job_id}",
                timeout,
                detail_limit,
                target.slug,
                "detail response",
            )
            detail_requests += 1
            requests_fetched += 1
            detail_bytes += response_bytes
        if not isinstance(detail, Mapping):
            raise AdapterError("Greenhouse job detail must be a JSON object")
        detail_job_id = str(detail.get("id") or "").strip()
        if detail_job_id != summary.job_id:
            raise AdapterError("Greenhouse job detail does not match its stable job ID")

        departments = _names(detail.get("departments"))
        if allowed_departments and not any(
            department.casefold() in allowed_departments for department in departments
        ):
            continue
        offices = _names(detail.get("offices"))
        jobs.append(
            JobRecord(
                job_id=summary.job_id,
                title=summary.title,
                company=summary.company,
                location=summary.location,
                url=summary.url,
                updated_at=summary.updated_at,
                source=summary.source,
                departments=departments,
                offices=offices,
                description=detail.get("content"),
                raw_metadata=summary.raw_metadata,
            )
        )

    setattr(
        session,
        "greenhouse_pagination",
        {
            "pages_fetched": 1,
            "maximum_pages": 1,
            "maximum_results": maximum_results,
            "jobs_returned": len(data["jobs"]),
            "jobs_scanned": len(data["jobs"]),
            "candidate_jobs": candidate_count,
            "candidates_selected": len(selected_summaries),
            "jobs_normalized": len(jobs),
            "department_allowlist": list(target.department_allowlist),
            "job_attribution_field": target.job_attribution_field,
            "attribution_filtered": attribution_filtered,
            "zero_job_contract": not data["jobs"],
            "stop_reason": (
                "maximum_results_reached"
                if len(jobs) >= maximum_results
                else "bounded_collection"
            ),
            "listing_requests": 1,
            "department_requests": int(bool(allowed_departments) and not expanded_listing),
            "detail_requests": detail_requests,
            "requests_fetched": requests_fetched,
            "listing_bytes": listing_bytes,
            "department_bytes": department_bytes,
            "detail_bytes": detail_bytes,
        },
    )
    return jobs


def _has_exact_attribution(
    job: JobRecord,
    field: str,
    include: re.Pattern[str],
    exclude: re.Pattern[str] | None,
) -> bool:
    """Accept one job only when its reviewed Greenhouse metadata is exact."""
    metadata = job.raw_metadata.get("greenhouse_metadata")
    if not isinstance(metadata, Mapping):
        raise AdapterError("Greenhouse job metadata attribution must be an object")
    field_name = field.removeprefix("greenhouse_metadata.")
    value = metadata.get(field_name)
    values = value if isinstance(value, list) else [value]
    cleaned = [str(item).strip() for item in values if str(item or "").strip()]
    return bool(cleaned) and any(include.fullmatch(item) for item in cleaned) and not any(
        exclude.search(item) for item in cleaned if exclude
    )


def _get_json(
    session: requests.Session,
    url: str,
    timeout: int,
    byte_limit: int,
    slug: str,
    label: str,
) -> tuple[Any, int, bool]:
    """Read one JSON response without consuming more than its byte budget."""
    response = session.get(url, headers=HEADERS, timeout=timeout, stream=True)
    if response.status_code != 200:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise AdapterError(f"Greenhouse HTTP {response.status_code} for {slug}")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        body = bytearray()
        try:
            for chunk in iter_content(chunk_size=READ_CHUNK_BYTES):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > byte_limit:
                    raise AdapterError(f"Greenhouse {label} exceeded the byte limit")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        try:
            return json.loads(bytes(body)), len(body), True
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AdapterError(f"Greenhouse {label} is malformed JSON: {exc}") from exc

    content = getattr(response, "content", None)
    if content is not None:
        body = bytes(content or b"")
        if len(body) > byte_limit:
            raise AdapterError(f"Greenhouse {label} exceeded the byte limit")
        try:
            return response.json(), len(body), False
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Greenhouse {label} is malformed JSON: {exc}") from exc

    text = getattr(response, "text", "") or ""
    encoded_size = len(str(text).encode("utf-8"))
    if encoded_size > byte_limit:
        raise AdapterError(f"Greenhouse {label} exceeded the byte limit")
    try:
        return response.json(), encoded_size, False
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Greenhouse {label} is malformed JSON: {exc}") from exc


def _summary_identity(item: Any) -> tuple[str, str]:
    if not isinstance(item, Mapping):
        raise AdapterError("Greenhouse job must be a JSON object")
    job_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    if not job_id or not title:
        raise AdapterError("Greenhouse job is missing a stable ID or title")
    return job_id, title


def _listing_record(item: Any, target: CompanyTarget) -> JobRecord:
    """Validate and normalize the fields available on the compact listing."""
    job_id, title = _summary_identity(item)
    location = item.get("location") or {}
    if not isinstance(location, Mapping):
        raise AdapterError("Greenhouse job location must be an object")
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location.get("name", "") or "",
        url=_secure_apply_url(item.get("absolute_url")),
        updated_at=item.get("updated_at", ""),
        source=f"greenhouse:{target.slug}",
        raw_metadata=_listing_metadata(item),
    )


def _prioritized_summaries(
    summaries: list[tuple[Mapping[str, Any], JobRecord]], maximum_results: int
) -> tuple[list[tuple[Mapping[str, Any], JobRecord]], int]:
    """Search a full large board and prioritize likely in-scope student roles."""
    if len(summaries) <= maximum_results:
        return summaries, len(summaries)

    candidates: list[tuple[tuple[Mapping[str, Any], JobRecord], Mapping[str, Any]]] = []
    for pair in summaries:
        signals = student_job_signals(pair[1])
        if signals["matching_rule"]:
            candidates.append((pair, signals))
    candidates.sort(
        key=lambda candidate: (
            _candidate_signal_priority(candidate[1]),
            _candidate_location_priority(candidate[0][1]),
        )
    )
    prioritized = [pair for pair, _signals in candidates]
    candidate_ids = {pair[1].job_id for pair in prioritized}
    # Preserve a useful bounded source when no internship is open today.
    # Explicit student roles stay first; ordinary roles only backfill capacity.
    prioritized.extend(pair for pair in summaries if pair[1].job_id not in candidate_ids)
    return prioritized, len(candidates)


def _candidate_signal_priority(signals: Mapping[str, Any]) -> int:
    """Prefer explicit student markers over contextual temporary/seasonal signals."""
    if signals.get("strong_title") or signals.get("structured_strong"):
        return 0
    return 1


def _candidate_location_priority(job: JobRecord) -> int:
    """Keep source order within eligible, ambiguous, and out-of-scope groups."""
    status = classify_student_location(job)
    if status.startswith("eligible_"):
        return 0
    if status.startswith("manual_review_"):
        return 1
    return 2


def _listing_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose Greenhouse custom metadata under its field names for shared signals."""
    raw_metadata = dict(item)
    metadata = item.get("metadata")
    if metadata is None:
        raw_metadata["greenhouse_metadata"] = {}
        return raw_metadata
    if not isinstance(metadata, list):
        raise AdapterError("Greenhouse job metadata must be a list")

    named_metadata: dict[str, Any] = {}
    for field in metadata:
        if not isinstance(field, Mapping):
            raise AdapterError("Greenhouse job metadata field must be an object")
        name = str(field.get("name") or "").strip()
        if not name:
            raise AdapterError("Greenhouse job metadata field is missing its name")
        value = field.get("value")
        if name not in named_metadata:
            named_metadata[name] = value
        else:
            existing = named_metadata[name]
            named_metadata[name] = (
                [*existing, value] if isinstance(existing, list) else [existing, value]
            )
    raw_metadata["greenhouse_metadata"] = named_metadata
    return raw_metadata


def _embedded_detail(
    item: Mapping[str, Any], *, assume_embedded: bool = False
) -> Mapping[str, Any] | None:
    """Retain compatibility with already-expanded fixtures and cached responses."""
    detail_fields = ("content", "departments", "offices")
    for field in ("departments", "offices"):
        if field in item:
            _names(item.get(field))
    if assume_embedded or all(field in item for field in detail_fields):
        return item
    return None


def _department_job_ids(data: Any, allowed_departments: set[str]) -> set[str]:
    if not isinstance(data, Mapping) or not isinstance(data.get("departments"), list):
        raise AdapterError("Greenhouse department response is missing its department collection")
    job_ids: set[str] = set()
    for department in data["departments"]:
        if not isinstance(department, Mapping):
            raise AdapterError("Greenhouse department must be a JSON object")
        name = str(department.get("name") or "").strip()
        department_jobs = department.get("jobs")
        if not isinstance(department_jobs, list):
            raise AdapterError("Greenhouse department jobs must be a list")
        if name.casefold() not in allowed_departments:
            continue
        for item in department_jobs:
            job_id, _ = _summary_identity(item)
            job_ids.add(job_id)
    return job_ids


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AdapterError("Greenhouse departments/offices must be lists")
    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AdapterError("Greenhouse department/office must be an object")
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names
