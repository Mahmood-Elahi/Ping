"""Bounded Greenhouse job board adapter."""

from __future__ import annotations

from typing import Any, List, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 5_000_000


def _secure_apply_url(value: object) -> str:
    """Canonicalize legacy Greenhouse HTTP links to their HTTPS endpoint."""
    parsed = urlparse(str(value or ""))
    if parsed.scheme.casefold() == "http" and parsed.hostname:
        return urlunparse(parsed._replace(scheme="https"))
    return str(value or "")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.slug:
        raise AdapterError("Greenhouse target is missing slug")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Greenhouse max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Greenhouse response byte limit exceeds the hard maximum")

    url = f"https://boards-api.greenhouse.io/v1/boards/{target.slug}/jobs?content=true"
    response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    if response.status_code != 200:
        raise AdapterError(f"Greenhouse HTTP {response.status_code} for {target.slug}")

    content = bytes(getattr(response, "content", b"") or b"")
    if content and len(content) > int(target.max_response_bytes):
        raise AdapterError("Greenhouse response exceeded the byte limit")

    data = response.json()
    if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
        raise AdapterError("Greenhouse response is missing its stable job_id collection")
    allowed_departments = {
        str(value).strip().casefold()
        for value in target.department_allowlist
        if str(value).strip()
    }
    jobs = []
    for item in data["jobs"]:
        if not isinstance(item, Mapping):
            raise AdapterError("Greenhouse job must be a JSON object")
        departments = _names(item.get("departments"))
        if allowed_departments and not any(
            department.casefold() in allowed_departments for department in departments
        ):
            continue
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            raise AdapterError("Greenhouse job is missing a stable ID or title")
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=target.name,
                location=(item.get("location") or {}).get("name", "") or "",
                url=_secure_apply_url(item.get("absolute_url")),
                updated_at=item.get("updated_at", ""),
                source=f"greenhouse:{target.slug}",
                departments=departments,
                offices=_names(item.get("offices")),
                description=item.get("content"),
            )
        )
        if len(jobs) >= int(target.max_jobs):
            break
    setattr(
        session,
        "greenhouse_pagination",
        {
            "pages_fetched": 1,
            "maximum_pages": 1,
            "maximum_results": int(target.max_jobs),
            "jobs_returned": len(data["jobs"]),
            "jobs_normalized": len(jobs),
            "department_allowlist": list(target.department_allowlist),
            "zero_job_contract": not data["jobs"],
            "stop_reason": (
                "maximum_results_reached"
                if len(jobs) >= int(target.max_jobs)
                else "bounded_collection"
            ),
        },
    )
    return jobs


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
