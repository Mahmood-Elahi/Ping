"""Amazon Jobs adapter."""

from __future__ import annotations

from typing import List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


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

    for term in search_terms:
        for page in range(max(1, target.max_pages)):
            offset = page * 100
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
            if not items:
                break

            for item in items:
                job_id = str(item.get("id", "") or item.get("id_icims", ""))
                if job_id in seen:
                    continue
                seen.add(job_id)
                location = item.get("normalized_location", "") or item.get("location", "")
                jobs.append(
                    JobRecord(
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
                )

            if len(items) < 100:
                break

    return jobs
