"""Amazon Jobs adapter."""

from __future__ import annotations

from typing import List

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence


HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_SEARCH_TERMS = [
    "Principal Engineer",
    "Senior Principal Engineer",
    "Principal Software Engineer",
    "Senior SDE",
    "Principal SDE",
]


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    base_url = "https://www.amazon.jobs/en/search.json"
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    by_id: dict[str, JobRecord] = {}
    metadata = SourceScanMetadata(
        endpoint_variant="search_terms_offset_v1",
        maximum_pages=max(1, target.max_pages),
    )
    publish_source_scan_metadata(session, metadata)
    safety_limited = False

    for term in search_terms:
        term_complete = False
        for page in range(max(1, target.max_pages)):
            offset = page * 100
            metadata["pages_requested"] += 1
            response = session.get(
                base_url,
                params={
                    "base_query": term,
                    "result_limit": 100,
                    "offset": offset,
                    "sort": "recent",
                },
                headers=HEADERS,
                timeout=timeout,
            )
            if response.status_code != 200:
                raise AdapterError(f"Amazon HTTP {response.status_code}")

            items = response.json().get("jobs", []) or []
            metadata["pages_completed"] += 1
            if not items:
                term_complete = True
                break

            for item in items:
                job_id = str(item.get("id", "") or item.get("id_icims", ""))
                if not job_id:
                    raise AdapterError("Amazon returned a job without a stable ID")
                location = item.get("normalized_location", "") or item.get("location", "")
                job = JobRecord(
                        job_id=job_id,
                        title=item.get("title", ""),
                        company=target.name,
                        location=location,
                        url="https://www.amazon.jobs" + item.get("job_path", ""),
                        updated_at=item.get("posted_date", "") or item.get("updated_time", ""),
                        source="custom:amazon",
                        departments=[item.get("business_category", "")]
                        if item.get("business_category")
                        else [],
                        offices=[location] if location else [],
                        description=item.get("description"),
                    )
                if job_id in seen:
                    existing = by_id[job_id]
                    if (
                        existing.title != job.title
                        or existing.url != job.url
                        or existing.location != job.location
                        or existing.updated_at != job.updated_at
                    ):
                        raise AdapterError("Amazon returned a conflicting duplicate stable ID")
                    continue
                seen.add(job_id)
                by_id[job_id] = job
                jobs.append(job)

            if len(items) < 100:
                term_complete = True
                break
        if not term_complete:
            safety_limited = True

    metadata["total_available"] = len(jobs) if not safety_limited else None
    metadata["scan_complete"] = not safety_limited
    metadata["scan_completeness_reason"] = (
        "safety_limit_reached" if safety_limited else "all_search_terms_exhausted"
    )
    metadata["stop_reason"] = (
        "maximum_pages_reached" if safety_limited else "all_search_terms_exhausted"
    )
    metadata["safety_limit_reached"] = safety_limited
    record_terminal_evidence(
        metadata,
        "search_terms_exhausted" if not safety_limited else "search_terms_incomplete",
        not safety_limited,
        has_more=safety_limited,
        terminal_page_observed=not safety_limited,
    )
    return jobs
