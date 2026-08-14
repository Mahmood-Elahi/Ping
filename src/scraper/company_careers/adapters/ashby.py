"""Bounded adapter for Ashby's anonymous public job-board API."""

from __future__ import annotations

import re
from typing import Any, List, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.student_roles import (
    classify_student_location,
    student_job_signals,
)


MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 3_000_000
MAX_TIMEOUT_SECONDS = 60
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
HEADERS = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    _validate_target(target)
    url = (
        "https://api.ashbyhq.com/posting-api/job-board/"
        f"{target.slug}?includeCompensation=false"
    )
    response = session.get(
        url,
        headers=HEADERS,
        timeout=min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS),
    )
    if response.status_code != 200:
        raise AdapterError(f"Ashby HTTP {response.status_code} for {target.slug}")
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if content and len(content) > int(target.max_response_bytes):
        raise AdapterError("Ashby response exceeded the byte limit")
    try:
        data = response.json()
    except (TypeError, ValueError, requests.RequestException) as exc:
        raise AdapterError(f"Ashby response is malformed JSON: {exc}") from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
        raise AdapterError("Ashby response is missing its job collection")

    normalized = [_normalize(item, target) for item in data["jobs"]]
    if len({job.job_id.casefold() for job in normalized}) != len(normalized):
        raise AdapterError("Ashby response contains duplicate stable job IDs")
    prioritized, candidate_count = _prioritized(normalized, int(target.max_jobs))
    jobs = prioritized[: int(target.max_jobs)]
    setattr(
        session,
        "ashby_pagination",
        {
            "pages_fetched": 1,
            "maximum_pages": 1,
            "maximum_results": int(target.max_jobs),
            "jobs_returned": len(normalized),
            "jobs_scanned": len(normalized),
            "candidate_jobs": candidate_count,
            "jobs_normalized": len(jobs),
            "zero_job_contract": not normalized,
            "stop_reason": (
                "validated_zero_jobs"
                if not normalized
                else "maximum_results_reached"
                if len(normalized) > len(jobs)
                else "bounded_collection"
            ),
        },
    )
    return jobs


def _validate_target(target: CompanyTarget) -> None:
    if not target.slug or not SLUG_RE.fullmatch(str(target.slug)):
        raise AdapterError("Ashby target is missing a valid slug")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Ashby max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Ashby response byte limit exceeds the hard maximum")
    if target.allowed_apply_hosts and {
        str(host).casefold() for host in target.allowed_apply_hosts
    } != {"jobs.ashbyhq.com"}:
        raise AdapterError(
            "Ashby allowed_apply_hosts must contain only jobs.ashbyhq.com"
        )


def _normalize(item: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(item, Mapping):
        raise AdapterError("Ashby job must be a JSON object")
    job_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    job_url = str(item.get("jobUrl") or "").strip()
    parsed = urlparse(job_url)
    parts = [part for part in parsed.path.split("/") if part]
    if not ID_RE.fullmatch(job_id) or not title:
        raise AdapterError("Ashby job is missing a stable ID or title")
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "jobs.ashbyhq.com"
        or len(parts) != 2
        or parts[0].casefold() != str(target.slug).casefold()
        or parts[1].casefold() != job_id.casefold()
    ):
        raise AdapterError("Ashby job has no exact board-owned stable URL")
    location = str(item.get("location") or "").strip()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=job_url,
        updated_at=str(item.get("publishedDate") or ""),
        source=f"ashby:{target.slug}",
        departments=[str(item.get("department"))] if item.get("department") else [],
        offices=[location] if location else [],
        description=item.get("descriptionPlain") or item.get("description"),
        raw_metadata={"platform": "ashby", "external_job_id": job_id},
    )


def _prioritized(
    jobs: list[JobRecord], maximum_results: int
) -> tuple[list[JobRecord], int]:
    if len(jobs) <= maximum_results:
        return jobs, len(jobs)
    candidates = [(job, student_job_signals(job)) for job in jobs]
    candidates = [pair for pair in candidates if pair[1]["matching_rule"]]
    candidates.sort(
        key=lambda pair: (
            0
            if pair[1].get("strong_title") or pair[1].get("structured_strong")
            else 1,
            _location_priority(pair[0]),
        )
    )
    prioritized = [job for job, _signals in candidates]
    ids = {job.job_id.casefold() for job in prioritized}
    prioritized.extend(job for job in jobs if job.job_id.casefold() not in ids)
    return prioritized, len(candidates)


def _location_priority(job: JobRecord) -> int:
    status = classify_student_location(job)
    if status.startswith("eligible_"):
        return 0
    if status.startswith("manual_review_"):
        return 1
    return 2
