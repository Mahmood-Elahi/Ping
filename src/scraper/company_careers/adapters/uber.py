"""Uber's anonymous, server-rendered public careers listing."""

from __future__ import annotations

import re
from typing import List
from urllib.parse import urljoin

import requests
from lxml import html

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


SEARCH_URL = "https://jobs.uber.com/en/jobs/"
PAGE_SIZE = 10
HEADERS = {
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
    "Accept": "text/html,application/xhtml+xml",
}
JOB_PATH_RE = re.compile(r"^/en/jobs/(\d+)/$")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    maximum_jobs = max(1, int(target.max_jobs))

    for page in range(1, max(1, target.max_pages) + 1):
        response = session.get(
            SEARCH_URL,
            params={"page": page, "pagesize": PAGE_SIZE},
            headers=HEADERS,
            timeout=timeout,
        )
        if response.status_code != 200:
            raise AdapterError(f"Uber Careers HTTP {response.status_code}")
        if "html" not in str(getattr(response, "headers", {}).get("content-type", "html")):
            raise AdapterError("Uber Careers listing returned a non-HTML response")

        try:
            root = html.fromstring(response.text)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Uber Careers listing returned malformed HTML: {exc}") from exc

        cards = root.xpath("//*[@data-slot='card' and @data-id]")
        if page == 1 and not cards:
            text = " ".join(root.text_content().split()).casefold()
            if not re.search(r"\b(?:no|0)\s+(?:open\s+)?(?:jobs|roles|results)\b", text):
                raise AdapterError("Uber Careers HTTP 200 response did not prove an empty jobs collection")
            return []

        added = 0
        for card in cards:
            job = _normalize_card(card, target)
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            jobs.append(job)
            added += 1
            if len(jobs) >= maximum_jobs:
                break

        if cards and added == 0:
            raise AdapterError("Uber Careers page repeated without new stable job IDs")
        if len(jobs) >= maximum_jobs or len(cards) < PAGE_SIZE:
            break

    return jobs


def _normalize_card(card: object, target: CompanyTarget) -> JobRecord:
    card_id = str(card.get("data-id") or "").strip()  # type: ignore[attr-defined]
    links = card.xpath(".//a[contains(concat(' ', normalize-space(@class), ' '), ' js-view-job ')]")  # type: ignore[attr-defined]
    if len(links) != 1:
        raise AdapterError("Uber Careers job card must contain one detail link")
    href = str(links[0].get("href") or "").strip()
    match = JOB_PATH_RE.fullmatch(href)
    title = " ".join(links[0].text_content().split())
    if not match or not card_id or match.group(1) != card_id or not title:
        raise AdapterError("Uber Careers job card has an invalid title or stable identifier")

    pills = [
        " ".join(node.text_content().split())
        for node in card.xpath(  # type: ignore[attr-defined]
            ".//*[@data-slot='card-description']"
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' rounded-full ')]"
        )
    ]
    values = [value for value in pills if value]
    location = values[0] if values else ""
    departments = values[1:]
    url = urljoin(SEARCH_URL, href)
    return JobRecord(
        job_id=card_id,
        title=title,
        company=target.name,
        location=location,
        url=url,
        updated_at="",
        source="custom:uber",
        departments=departments,
        offices=[location] if location else [],
        raw_metadata={"platform": "uber", "endpoint_variant": "server_rendered_jobs_v2"},
    )
