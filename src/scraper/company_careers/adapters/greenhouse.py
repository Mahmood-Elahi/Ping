"""Greenhouse job board adapter."""

from __future__ import annotations

from typing import List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.slug:
        raise AdapterError("Greenhouse target is missing slug")

    url = f"https://boards-api.greenhouse.io/v1/boards/{target.slug}/jobs?content=true"
    response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    if response.status_code != 200:
        raise AdapterError(f"Greenhouse HTTP {response.status_code} for {target.slug}")

    data = response.json()
    jobs = []
    for item in data.get("jobs", []):
        jobs.append(
            JobRecord(
                job_id=str(item.get("id", "")),
                title=item.get("title", ""),
                company=target.name,
                location=(item.get("location") or {}).get("name", "") or "",
                url=item.get("absolute_url", ""),
                updated_at=item.get("updated_at", ""),
                source=f"greenhouse:{target.slug}",
                departments=[dept.get("name", "") for dept in item.get("departments", [])],
                offices=[office.get("name", "") for office in item.get("offices", [])],
                description=item.get("content"),
            )
        )
    return jobs
