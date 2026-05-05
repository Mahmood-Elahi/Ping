"""Atlassian Careers adapter."""

from __future__ import annotations

import re
from html import unescape
from typing import Any, List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


LISTINGS_URL = "https://www.atlassian.com/endpoint/careers/listings"


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    response = session.get(
        LISTINGS_URL,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise AdapterError(f"Atlassian Careers HTTP {response.status_code}")

    payload = response.json()
    if not isinstance(payload, list):
        raise AdapterError("Atlassian Careers response was not a list")

    jobs: List[JobRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            continue

        locations = _clean_strings(item.get("locations"))
        portal_post = item.get("portalJobPost") if isinstance(item.get("portalJobPost"), dict) else {}
        url = item.get("applyUrl") or portal_post.get("portalUrl") or ""
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=target.name,
                location=", ".join(locations),
                url=str(url),
                updated_at=str(portal_post.get("updatedDate") or ""),
                source="custom:atlassian",
                departments=[str(item.get("category"))] if item.get("category") else [],
                offices=locations,
                description=_description(item),
            )
        )

    return jobs


def _description(item: dict) -> str:
    parts = [item.get("overview"), item.get("responsibilities"), item.get("qualifications")]
    return "\n\n".join(_html_to_text(part) for part in parts if part).strip()


def _html_to_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:li|p|h[1-6]|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _clean_strings(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]
