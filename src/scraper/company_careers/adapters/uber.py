"""Uber's anonymous, server-rendered public careers listing."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import List
from urllib.parse import urljoin

import requests
from lxml import html

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)


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
    metadata = SourceScanMetadata(
        endpoint_variant="server_rendered_or_json_ld_jobs_v3",
        maximum_pages=max(1, int(target.max_pages)),
        maximum_results=maximum_jobs,
    )
    publish_source_scan_metadata(session, metadata)

    for page in range(1, max(1, target.max_pages) + 1):
        metadata["pages_requested"] += 1
        response = session.get(
            SEARCH_URL,
            params={"page": page, "pagesize": PAGE_SIZE},
            headers=HEADERS,
            timeout=timeout,
            stream=True,
        )
        if response.status_code in {403, 429, 451}:
            raise static_html._failure(
                "access_blocked", f"Uber Careers HTTP {response.status_code}"
            )
        if response.status_code != 200:
            raise AdapterError(f"Uber Careers HTTP {response.status_code}")
        if "html" not in str(getattr(response, "headers", {}).get("content-type", "html")):
            raise AdapterError("Uber Careers listing returned a non-HTML response")

        static_target = replace(
            target,
            platform="static_html",
            listing_url=SEARCH_URL,
            endpoint_variant="server_rendered_cards",
            allowed_apply_hosts=["jobs.uber.com"],
            external_id_regex=r"/en/jobs/(\d+)/",
        )
        content = static_html._response_bytes(response, static_target)
        try:
            root = html.fromstring(content)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Uber Careers listing returned malformed HTML: {exc}") from exc

        structured = static_html._json_ld_contract(root, static_target, SEARCH_URL)
        if structured is not None:
            structured_jobs, collection_proven = structured
            for job in structured_jobs[:maximum_jobs]:
                external_id = str(job.raw_metadata.get("external_job_id") or "")
                job.job_id = external_id or job.job_id
                job.source = "custom:uber"
                job.raw_metadata["platform"] = "uber"
                job.raw_metadata["endpoint_variant"] = "json_ld_job_posting"
            metadata["pages_completed"] += 1
            metadata["scan_complete"] = bool(collection_proven)
            metadata["scan_completeness_reason"] = (
                "proven_structured_collection"
                if collection_proven
                else "structured_collection_without_total"
            )
            metadata["stop_reason"] = metadata["scan_completeness_reason"]
            metadata["total_available"] = (
                len(structured_jobs) if collection_proven else None
            )
            metadata["safety_limit_reached"] = len(structured_jobs) > maximum_jobs
            return structured_jobs[:maximum_jobs]

        cards = root.xpath("//*[@data-slot='card' and @data-id]")
        if page == 1 and not cards:
            text = " ".join(root.text_content().split()).casefold()
            if not re.search(r"\b(?:no|0)\s+(?:open\s+)?(?:jobs|roles|results)\b", text):
                metadata["stop_reason"] = "unproven_empty_collection"
                metadata["scan_completeness_reason"] = "unproven_empty_collection"
                raise static_html._failure(
                    "unproven_empty_collection",
                    "Uber Careers HTTP 200 response did not prove an empty jobs collection",
                )
            metadata["pages_completed"] += 1
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "proven_zero_job_collection"
            metadata["stop_reason"] = "proven_zero_job_collection"
            metadata["total_available"] = 0
            return []

        metadata["pages_completed"] += 1

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
            metadata["scan_complete"] = len(cards) < PAGE_SIZE
            metadata["scan_completeness_reason"] = (
                "natural_terminal_page" if len(cards) < PAGE_SIZE else "maximum_results_reached"
            )
            metadata["stop_reason"] = metadata["scan_completeness_reason"]
            metadata["safety_limit_reached"] = len(jobs) >= maximum_jobs and len(cards) >= PAGE_SIZE
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
