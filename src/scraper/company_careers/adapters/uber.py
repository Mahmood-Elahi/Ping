"""Uber Careers adapter."""

from __future__ import annotations

from typing import Any, List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


SEARCH_URL = "https://www.uber.com/api/loadSearchJobsResults"
REFERER = "https://www.uber.com/us/en/careers/list/"
PAGE_LIMIT = 100


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    seen: set[str] = set()

    for page in range(max(1, target.max_pages)):
        response = session.post(
            SEARCH_URL,
            params={"localeCode": "en"},
            json={
                "limit": PAGE_LIMIT,
                "page": page,
                "params": {
                    "department": [],
                    "lineOfBusinessName": [],
                    "location": [],
                    "programAndPlatform": [],
                    "team": [],
                },
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": REFERER,
                "x-csrf-token": "x",
                "x-uber-sites-page-edge-cache-enabled": "true",
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            raise AdapterError(f"Uber Careers HTTP {response.status_code}")

        payload = response.json()
        if payload.get("status") != "success":
            raise AdapterError(f"Uber Careers unsuccessful response: {payload.get('status')}")
        items = ((payload.get("data") or {}).get("results") or [])
        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id") or "").strip()
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            locations = _locations(item)
            jobs.append(
                JobRecord(
                    job_id=job_id,
                    title=str(item.get("title") or ""),
                    company=target.name,
                    location=", ".join(locations),
                    url=f"https://www.uber.com/us/en/careers/list/{job_id}/",
                    updated_at=str(item.get("updatedDate") or item.get("creationDate") or ""),
                    source="custom:uber",
                    departments=[str(item.get("department"))] if item.get("department") else [],
                    offices=locations,
                    description=item.get("description"),
                )
            )

        if len(items) < PAGE_LIMIT:
            break

    return jobs


def _locations(item: dict) -> List[str]:
    values = item.get("allLocations")
    if not isinstance(values, list) or not values:
        values = [item.get("location")]
    locations = []
    for value in values:
        if not isinstance(value, dict):
            continue
        parts = [value.get("city"), value.get("region"), value.get("countryName") or value.get("country")]
        location = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        if location:
            locations.append(location)
    return locations
