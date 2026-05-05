"""Ashby job board adapter."""

from __future__ import annotations

from typing import List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.slug:
        raise AdapterError("Ashby target is missing slug")

    url = f"https://api.ashbyhq.com/posting-api/job-board/{target.slug}?includeCompensation=false"
    response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    if response.status_code != 200:
        raise AdapterError(f"Ashby HTTP {response.status_code} for {target.slug}")

    data = response.json()
    jobs = []
    for item in data.get("jobs", []):
        location = item.get("location", "") or ""
        jobs.append(
            JobRecord(
                job_id=item.get("id", ""),
                title=item.get("title", ""),
                company=target.name,
                location=location,
                url=item.get("jobUrl", ""),
                updated_at=item.get("publishedDate", ""),
                source=f"ashby:{target.slug}",
                departments=[item.get("department", "")] if item.get("department") else [],
                offices=[location] if location else [],
                description=item.get("descriptionPlain") or item.get("description"),
            )
        )
    return jobs
