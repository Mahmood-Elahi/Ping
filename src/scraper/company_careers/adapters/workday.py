"""Workday career-site adapter."""

from __future__ import annotations

from typing import Dict, List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.tenant or not target.wd_subdomain or not (target.site_id or target.site_ids):
        raise AdapterError("Workday target needs tenant, wd_subdomain, and site_id/site_ids")

    site_ids = _candidate_site_ids(target)
    last_error = None

    for site_id in site_ids:
        api_url = (
            f"https://{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
            f"/wday/cxs/{target.tenant}/{site_id}/jobs"
        )
        for body_index in range(len(_body_variants(0))):
            try:
                return _fetch_pages(session, target, api_url, site_id, body_index, timeout)
            except AdapterError as exc:
                last_error = exc
                continue

    raise AdapterError(str(last_error) if last_error else f"Workday failed for {target.name}")


def _candidate_site_ids(target: CompanyTarget) -> List[str]:
    site_ids = []
    if target.site_id:
        site_ids.append(target.site_id)
    site_ids.extend(target.site_ids)
    return list(dict.fromkeys(site_ids))


def _body_variants(offset: int) -> List[Dict[str, object]]:
    return [
        {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
        {"limit": 20, "offset": offset},
        {"limit": 20, "offset": offset, "searchText": ""},
    ]


def _fetch_pages(
    session: requests.Session,
    target: CompanyTarget,
    api_url: str,
    site_id: str,
    body_index: int,
    timeout: int,
) -> List[JobRecord]:
    base_url = f"https://{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
    jobs: List[JobRecord] = []

    for page in range(max(1, target.max_pages)):
        offset = page * 20
        body = _body_variants(offset)[body_index]
        response = session.post(api_url, json=body, headers=HEADERS, timeout=timeout)
        if response.status_code != 200:
            if page == 0:
                raise AdapterError(
                    f"Workday HTTP {response.status_code} for {target.name} site={site_id} body={body_index}"
                )
            break

        items = response.json().get("jobPostings", [])
        if not items:
            break

        for item in items:
            external_path = item.get("externalPath", "") or ""
            location = item.get("locationsText", "") or ""
            bullet_fields = item.get("bulletFields") or []
            job_id = str(bullet_fields[0]) if bullet_fields else external_path.split("/")[-1]
            jobs.append(
                JobRecord(
                    job_id=job_id,
                    title=item.get("title", ""),
                    company=target.name,
                    location=location,
                    url=f"{base_url}/en-US/{site_id}{external_path}",
                    updated_at=item.get("postedOn", ""),
                    source=f"workday:{target.tenant}",
                    departments=[],
                    offices=[location] if location else [],
                )
            )

        if len(items) < 20:
            break

    return jobs
