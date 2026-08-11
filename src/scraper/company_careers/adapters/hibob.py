"""Bounded adapter for HiBob's anonymous public career-site API."""

from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import UUID

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


ENDPOINT_VARIANT = "career_site_job_ads_v1"
MAX_JOBS = 100
MAX_COLLECTION_SIZE = 1_000
MAX_TIMEOUT_SECONDS = 30
BLOCKED_MARKERS = ("captcha", "access denied", "verify you are human", "challenge-platform")


def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> list[JobRecord]:
    """Fetch, validate, deterministically sort, and bound a public HiBob board."""
    validate_target(target)
    timeout = min(MAX_TIMEOUT_SECONDS, max(1, int(timeout)))
    base = str(target.careers_url).rstrip("/")
    headers = {
        "Accept": "application/json",
        "Referer": base + "/",
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
    }
    identity = _get_json(session, base + "/api/career-site", "career-site identity", target, timeout, headers)
    if not isinstance(identity, Mapping):
        raise AdapterError("HiBob career-site identity must be an object")
    if identity.get("isPublished") is not True or identity.get("isBobCareerSite") is not True:
        raise AdapterError("HiBob career site is not an active published Bob career site")
    company_name = str(identity.get("companyName") or "").strip()
    if not re.fullmatch(str(target.identity_regex), company_name, re.I):
        raise AdapterError("HiBob company identity did not match identity_regex")

    payload = _get_json(session, str(target.endpoint_url), "job-ad collection", target, timeout, headers)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobAdDetails"), list):
        raise AdapterError("HiBob job-ad response is malformed")
    rows = payload["jobAdDetails"]
    if len(rows) > MAX_COLLECTION_SIZE:
        raise AdapterError("HiBob job-ad collection exceeded the safety bound")
    jobs = [_normalize(row, target) for row in rows]
    ids = [job.job_id for job in jobs]
    if len(ids) != len(set(ids)):
        raise AdapterError("HiBob job-ad collection repeated a stable job ID")
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    limit = int(target.max_jobs)
    metadata = {
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "GET",
        "total_available": len(jobs),
        "maximum_results": limit,
        "jobs_normalized": min(len(jobs), limit),
        "stop_reason": "maximum_results_reached" if len(jobs) > limit else "collection_exhausted",
    }
    setattr(session, "hibob_pagination", metadata)
    return jobs[:limit]


def validate_target(target: CompanyTarget) -> None:
    for field in ("slug", "careers_url", "endpoint_url", "identity_regex"):
        if not str(getattr(target, field) or "").strip():
            raise AdapterError(f"HiBob target is missing {field}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"HiBob endpoint_variant must be {ENDPOINT_VARIANT}")
    slug = str(target.slug)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
        raise AdapterError("HiBob slug is invalid")
    careers = urlparse(str(target.careers_url))
    endpoint = urlparse(str(target.endpoint_url))
    host = f"{slug}.careers.hibob.com"
    if (
        careers.scheme != "https" or (careers.hostname or "").casefold() != host
        or careers.path not in {"", "/"} or careers.query or careers.fragment
    ):
        raise AdapterError("HiBob careers_url must be the matching public tenant root")
    if (
        endpoint.scheme != "https" or (endpoint.hostname or "").casefold() != host
        or endpoint.path != "/api/job-ad" or endpoint.query or endpoint.fragment
    ):
        raise AdapterError("HiBob endpoint_url must be the matching public /api/job-ad endpoint")
    allowed = {str(value).strip().casefold().rstrip(".") for value in target.allowed_apply_hosts}
    if allowed != {host}:
        raise AdapterError("HiBob allowed_apply_hosts must contain only the tenant career host")
    if int(target.max_pages) != 1:
        raise AdapterError("HiBob max_pages must be 1 for its non-paginated collection")
    if not 1 <= int(target.max_jobs) <= MAX_JOBS:
        raise AdapterError(f"HiBob max_jobs must be between 1 and {MAX_JOBS}")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"HiBob identity_regex is invalid: {exc}") from exc


def _normalize(value: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(value, Mapping):
        raise AdapterError("HiBob job-ad row must be an object")
    raw_id = str(value.get("id") or "").strip()
    try:
        job_uuid = str(UUID(raw_id))
    except (ValueError, AttributeError) as exc:
        raise AdapterError("HiBob job-ad row has no stable UUID") from exc
    if job_uuid != raw_id.casefold():
        raise AdapterError("HiBob job-ad UUID is not canonical")
    title = " ".join(str(value.get("title") or "").split())
    site = " ".join(str(value.get("site") or "").split())
    country = " ".join(str(value.get("country") or "").split())
    if not title or not site or not country:
        raise AdapterError("HiBob job-ad row is missing title or location")
    location = site if site.casefold() == country.casefold() else f"{site}, {country}"
    posted = str(value.get("publishedAt") or "").strip()
    try:
        updated_at = datetime.fromisoformat(posted.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise AdapterError("HiBob job-ad row has a malformed publishedAt") from exc
    base = str(target.careers_url).rstrip("/")
    detail_url = f"{base}/jobs/{job_uuid}"
    apply_url = f"{detail_url}/apply"
    department = " ".join(str(value.get("department") or "").split())
    description = _html_text(value.get("description"))
    return JobRecord(
        job_id=f"hibob:{str(target.slug).casefold()}:{job_uuid}",
        title=title,
        company=target.name,
        location=location,
        url=apply_url,
        updated_at=updated_at,
        source=f"hibob:{str(target.slug).casefold()}",
        departments=[department] if department else [],
        offices=[site],
        description=description,
        raw_metadata={
            "platform": "hibob",
            "endpoint_variant": ENDPOINT_VARIANT,
            "id": job_uuid,
            "detail_url": detail_url,
            "apply_url": apply_url,
            "employment_type": value.get("employmentType"),
            "workspace_type": value.get("workspaceType"),
        },
    )


def _get_json(
    session: requests.Session, url: str, label: str, target: CompanyTarget,
    timeout: int, headers: Mapping[str, str],
) -> Any:
    response = session.get(url, headers=dict(headers), timeout=timeout)
    if response.status_code != 200:
        raise AdapterError(f"HiBob {label} returned HTTP {response.status_code}")
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > int(target.max_response_bytes):
        raise AdapterError(f"HiBob {label} exceeded max_response_bytes")
    text = str(getattr(response, "text", "") or "")
    if any(marker in text.casefold() for marker in BLOCKED_MARKERS):
        raise AdapterError(f"HiBob {label} returned CAPTCHA or blocked content")
    try:
        return response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"HiBob {label} was not valid JSON") from exc


def _html_text(value: Any) -> str | None:
    text = re.sub(r"<[^>]+>", " ", unescape(str(value or "")))
    cleaned = " ".join(text.split())
    return cleaned or None
