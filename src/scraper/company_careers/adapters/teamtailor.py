"""Anonymous Teamtailor server-rendered jobs adapter."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)


ENDPOINT_VARIANT = "server_rendered_jobs_v1"
MAX_PAGES = 2
MAX_RESULTS = 20


def validate_target(target: CompanyTarget) -> None:
    missing = [name for name, value in (
        ("listing_url", target.listing_url),
        ("identity_regex", target.identity_regex),
        ("allowed_apply_hosts", target.allowed_apply_hosts),
    ) if not value]
    if missing:
        raise AdapterError(f"Teamtailor target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Teamtailor unsupported variant: {target.endpoint_variant}")
    static_html._require_https(str(target.listing_url), "listing_url")
    static_html._safe_regex(str(target.identity_regex))
    static_html._validate_hosts(target.allowed_apply_hosts)
    listing_host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    if not static_html._allowed_host(listing_host, target.allowed_apply_hosts):
        raise AdapterError("Teamtailor listing host is not an allowed apply host")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError("Teamtailor page limit exceeds the hard maximum")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Teamtailor result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= static_html.MAX_RESPONSE_BYTES:
        raise AdapterError("Teamtailor response byte limit exceeds the hard maximum")


def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 60
) -> list[JobRecord]:
    """Collect at most two deterministic public Teamtailor HTML pages."""
    validate_target(target)
    maximum_pages = min(int(target.max_pages), MAX_PAGES)
    maximum_jobs = min(int(target.max_jobs), MAX_RESULTS)
    timeout = min(max(1, int(timeout)), static_html.MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "GET",
        "pages": [],
        "pages_fetched": 0,
        "jobs_normalized": 0,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "stop_reason": "not_started",
        "zero_job_contract": False,
    })
    setattr(session, "teamtailor_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    next_url = str(target.listing_url)

    for page_index in range(maximum_pages):
        if next_url in seen_urls:
            metadata["stop_reason"] = "repeated_page"
            break
        seen_urls.add(next_url)
        metadata["pages_requested"] += 1
        response = static_html._get(session, next_url, timeout)
        content = static_html._response_bytes(response, target)
        root = static_html._parse_html(content, response)
        _require_identity(root, target)
        cards = root.xpath("//ul[@id='jobs_list_container']/li")
        page_row = {
            "page": page_index + 1,
            "url": next_url,
            "jobs_returned": len(cards),
            "jobs_normalized": 0,
        }
        metadata["pages"].append(page_row)
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["pages_completed"] += 1
        if not cards:
            static_html._reject_non_listing(root, content, False)

        new_jobs = 0
        for card in cards:
            job = _normalize(card, target, next_url)
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
            new_jobs += 1
            if len(jobs) >= maximum_jobs:
                break
        page_row["jobs_normalized"] = new_jobs
        metadata["jobs_normalized"] = len(jobs)
        if len(jobs) >= maximum_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if new_jobs == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            break
        links = root.xpath("//a[@rel='next' and @href]")
        if not links:
            metadata["stop_reason"] = "no_next_page"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "explicit_next_page_exhausted"
            metadata["total_available"] = len(jobs)
            break
        proposed = urljoin(next_url, str(links[0].get("href")))
        static_html._require_listing_host(proposed, target)
        next_url = proposed
        if page_index + 1 < maximum_pages:
            time.sleep(static_html.RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    elif not metadata["scan_complete"]:
        metadata["scan_completeness_reason"] = "pagination_failure"
    return jobs


def _require_identity(root: Any, target: CompanyTarget) -> None:
    text = " ".join(root.text_content().split())
    if not static_html._safe_regex(str(target.identity_regex)).search(text):
        static_html._reject_non_listing(root, text.encode("utf-8"), False)
        raise AdapterError("Teamtailor listing failed exact company attribution")


def _normalize(card: Any, target: CompanyTarget, page_url: str) -> JobRecord:
    links = card.xpath(".//a[@href and contains(@href, '/jobs/')]")
    if len(links) != 1:
        raise AdapterError("Teamtailor card has no unique detail link")
    link = links[0]
    title = " ".join(link.text_content().split())
    url = static_html._official_url(urljoin(page_url, str(link.get("href"))), target)
    match = re.search(r"/jobs/([1-9][0-9]{0,19})(?:-|$)", urlparse(url).path)
    if not title or not match:
        raise AdapterError("Teamtailor job is missing title or stable numeric ID")
    values = [
        " ".join(node.text_content().split())
        for node in card.xpath(
            ".//span[contains(concat(' ', normalize-space(@class), ' '), ' text-base ')]/span"
            "[not(contains(concat(' ', normalize-space(@class), ' '), ' mx-[2px] '))]"
        )
        if " ".join(node.text_content().split()) not in {"", "·"}
    ]
    department = values[:1]
    locations = values[1:]
    if not locations:
        raise AdapterError("Teamtailor job is missing its location")
    host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    external_id = match.group(1)
    return JobRecord(
        job_id=f"teamtailor:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at="",
        source=f"teamtailor:{host}",
        departments=department,
        offices=locations,
        raw_metadata={
            "platform": "teamtailor",
            "endpoint_variant": ENDPOINT_VARIANT,
            "external_job_id": external_id,
        },
    )
