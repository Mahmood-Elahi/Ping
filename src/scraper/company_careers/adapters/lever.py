"""Lever postings adapter."""

from __future__ import annotations

from typing import List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.slug:
        raise AdapterError("Lever target is missing slug")

    url = f"https://api.lever.co/v0/postings/{target.slug}?mode=json"
    response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    if response.status_code != 200:
        raise AdapterError(f"Lever HTTP {response.status_code} for {target.slug}")

    jobs = []
    for item in response.json():
        categories = item.get("categories", {}) or {}
        location = categories.get("location", "") or ""
        jobs.append(
            JobRecord(
                job_id=item.get("id", ""),
                title=item.get("text", ""),
                company=target.name,
                location=location,
                url=item.get("hostedUrl", ""),
                updated_at=str(item.get("createdAt", "")),
                source=f"lever:{target.slug}",
                departments=[categories.get("team", "")] if categories.get("team") else [],
                offices=[location] if location else [],
                description=item.get("descriptionPlain") or item.get("description"),
            )
        )
    return jobs
