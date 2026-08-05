"""Bounded adapter for Lever's anonymous public postings API."""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, List, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


ENDPOINT_VARIANT = "postings_api_v0"
MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 3_000_000
MAX_ATTEMPTS = 2
MAX_TIMEOUT_SECONDS = 60
RETRY_DELAY_SECONDS = 0.1
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)
HEADERS = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    """Fetch one public board response and normalize at most ``max_jobs`` records."""
    validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    maximum_results = min(MAX_RESULTS, max(1, int(target.max_jobs)))
    url = f"https://api.lever.co/v0/postings/{target.slug}?mode=json"
    response = _get(session, url, timeout)
    payload = _payload(response, target)
    metadata: dict[str, Any] = {
        "endpoint_variant": target.endpoint_variant or ENDPOINT_VARIANT,
        "request_method": "GET",
        "pages_fetched": 1,
        "maximum_pages": 1,
        "maximum_results": maximum_results,
        "jobs_returned": len(payload),
        "jobs_normalized": 0,
        "zero_job_contract": not payload,
        "stop_reason": "validated_zero_jobs" if not payload else "not_started",
        "diagnostics": [],
    }
    setattr(session, "lever_pagination", metadata)
    allowed_departments = {value.casefold() for value in _clean(target.department_allowlist)}
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise AdapterError("Lever malformed HTTP 200 response: posting is not an object")
        categories = item.get("categories", {}) or {}
        if not isinstance(categories, Mapping):
            raise AdapterError("Lever malformed HTTP 200 response: posting categories are not an object")
        department = str(categories.get("department", "") or "").strip()
        if allowed_departments and department.casefold() not in allowed_departments:
            continue
        job_id = str(item.get("id", "") or "").strip()
        title = str(item.get("text", "") or "").strip()
        if not ID_RE.fullmatch(job_id) or not title:
            raise AdapterError("Lever posting has no stable job_id/source identifier or title")
        hosted_url = _official_url(str(item.get("hostedUrl", "") or ""), target, job_id)
        if job_id.casefold() in seen_ids:
            metadata["diagnostics"].append(f"Duplicate Lever posting ID: {job_id}")
            continue
        seen_ids.add(job_id.casefold())
        location = categories.get("location", "") or ""
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=target.name,
                location=location,
                url=hosted_url,
                updated_at=str(item.get("createdAt", "")),
                source=f"lever:{target.slug}",
                departments=_clean([department, categories.get("team", "")]),
                offices=[location] if location else [],
                description=item.get("descriptionPlain") or item.get("description"),
                raw_metadata={
                    "platform": "lever",
                    "endpoint_variant": target.endpoint_variant or ENDPOINT_VARIANT,
                    "external_job_id": job_id,
                    "lever_department": department,
                },
            )
        )
        if len(jobs) >= maximum_results:
            metadata["stop_reason"] = "maximum_results_reached"
            break
    if payload and not jobs:
        if allowed_departments:
            metadata["stop_reason"] = "validated_zero_jobs"
            metadata["zero_job_contract"] = True
        else:
            first = metadata["diagnostics"][0] if metadata["diagnostics"] else "unsupported schema"
            raise AdapterError(f"Lever page contained postings but none were valid: {first}")
    elif jobs and metadata["stop_reason"] == "not_started":
        metadata["stop_reason"] = "bounded_collection"
    metadata["jobs_normalized"] = len(jobs)
    return jobs


def validate_target(target: CompanyTarget) -> None:
    if not target.slug or not SLUG_RE.fullmatch(str(target.slug)):
        raise AdapterError("Lever target is missing a valid slug")
    if target.endpoint_variant and target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Lever unsupported variant: {target.endpoint_variant}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Lever result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Lever response byte limit exceeds the hard maximum")
    hosts = target.allowed_apply_hosts or ["jobs.lever.co"]
    if any(not HOST_RE.fullmatch(str(host)) for host in hosts):
        raise AdapterError("Lever allowed_apply_hosts contains an invalid host")
    if any(not str(value).strip() for value in target.department_allowlist):
        raise AdapterError("Lever department_allowlist contains an empty value")


def _get(session: requests.Session, url: str, timeout: int) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, headers=HEADERS, timeout=timeout)
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
            raise AdapterError(f"Lever authentication required (HTTP {status})")
        if status in {403, 451}:
            raise AdapterError(f"Lever blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Lever HTTP {status}")
        return response
    raise AdapterError("Lever request attempts exhausted")


def _payload(response: Any, target: CompanyTarget) -> list[Any]:
    content = getattr(response, "content", None)
    if content is not None and len(content) > int(target.max_response_bytes):
        raise AdapterError("Lever response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError("Lever malformed HTTP 200 response: expected JSON")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Lever malformed HTTP 200 response: invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise AdapterError("Lever malformed HTTP 200 response: expected a posting list")
    return payload


def _official_url(url: str, target: CompanyTarget, job_id: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    allowed = target.allowed_apply_hosts or ["jobs.lever.co"]
    segments = [value for value in parsed.path.split("/") if value]
    if parsed.scheme != "https" or not parsed.hostname:
        raise AdapterError("Lever posting has no valid apply URL")
    if not any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed):
        raise AdapterError("Lever posting apply host is not allow-listed")
    if (len(segments) < 2 or segments[0].casefold() != str(target.slug).casefold()
            or segments[1].casefold() != str(job_id).casefold()):
        raise AdapterError("Lever posting apply URL does not match its board and stable ID")
    return urlunparse(parsed._replace(fragment=""))


def _clean(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
