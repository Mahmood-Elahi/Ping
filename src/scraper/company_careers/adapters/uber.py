"""Uber's anonymous same-origin public careers search API."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from math import ceil
from typing import Any, List, Mapping
from urllib.parse import urljoin, urlparse

import requests
from lxml import html

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    finish_authoritative_total,
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    finish_changed,
    immutable_job_fingerprint,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


SEARCH_URL = "https://jobs.uber.com/en/jobs/"
SEARCH_API_URL = "https://jobs.uber.com/api/jobs/search/"
PAGE_SIZE = 10
HEADERS = {
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
    "Accept": "application/json",
}
JOB_PATH_RE = re.compile(r"^/en/jobs/(\d+)/$")
RSC_PREFIX = "self.__next_f.push("
MAX_RESPONSE_BYTES = 3_000_000
PAGE_DELAY_SECONDS = 0.1


@explicit_adapter_result
def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    """Fetch one internally consistent traversal of Uber's public job index."""
    return fetch_with_mutation_restart(
        session, lambda: _fetch_jobs_once(session, target, timeout)
    )


def _fetch_jobs_once(
    session: requests.Session, target: CompanyTarget, timeout: int
) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    seen_fingerprints: dict[str, tuple[Any, ...]] = {}
    maximum_jobs = max(1, int(target.max_jobs))
    metadata = SourceScanMetadata(
        endpoint_variant="same_origin_search_api_v1",
        maximum_pages=max(1, int(target.max_pages)),
        maximum_results=maximum_jobs,
        pages=[],
    )
    initialize_mutable_collection(metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(
        normal_request_budget=max(1, int(target.max_pages)),
        normal_job_budget=maximum_jobs,
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)

    for page in range(1, listing_budget.hard_request_ceiling + 1):
        metadata["pages_requested"] += 1
        response = session.get(
            SEARCH_API_URL,
            params={"page": page, "pagesize": PAGE_SIZE},
            headers=HEADERS,
            timeout=timeout,
        )
        if response.status_code in {403, 429, 451}:
            raise static_html._failure(
                "access_blocked", f"Uber Careers HTTP {response.status_code}"
            )
        if response.status_code != 200:
            raise AdapterError(f"Uber Careers HTTP {response.status_code}")
        content = bytes(getattr(response, "content", b"") or b"")
        if not content or len(content) > MAX_RESPONSE_BYTES:
            raise AdapterError("Uber Careers API response exceeded its byte bound")
        if "json" not in str(
            getattr(response, "headers", {}).get("content-type", "")
        ).casefold():
            raise AdapterError("Uber Careers API returned a non-JSON response")
        try:
            payload = response.json()
        except (TypeError, ValueError, requests.RequestException) as exc:
            raise AdapterError(f"Uber Careers API returned malformed JSON: {exc}") from exc

        envelope = _job_envelope(payload, page)
        if envelope:
            rows, total, total_pages, page_size = envelope
            metadata["pages_completed"] += 1
            total_changed = record_total_observation(
                metadata,
                total,
                offset=(page - 1) * page_size,
                source_label="Uber Careers",
            )
            listing_budget.record_decision(
                metadata,
                total,
                page_size,
                requests_completed=int(metadata["pages_completed"]),
                results_observed=(page - 1) * page_size + len(rows),
            )
            page_ids: set[str] = set()
            page_overlap = 0
            added = 0
            for row in rows:
                job = _normalize_api_job(row, target)
                if job.job_id in page_ids:
                    raise AdapterError(
                        f"Uber Careers repeated stable job ID within one page: {job.job_id}"
                    )
                page_ids.add(job.job_id)
                fingerprint = immutable_job_fingerprint(job)
                if job.job_id in seen:
                    if seen_fingerprints[job.job_id] != fingerprint:
                        raise AdapterError(
                            "Uber Careers returned conflicting records for stable job ID: "
                            f"{job.job_id}"
                        )
                    page_overlap += 1
                    continue
                seen.add(job.job_id)
                seen_fingerprints[job.job_id] = fingerprint
                jobs.append(job)
                added += 1
            if page_overlap:
                mark_collection_changed(
                    metadata,
                    offset=(page - 1) * page_size,
                    overlap_count=page_overlap,
                )
            metadata["jobs_normalized"] = len(jobs)
            metadata["pages"].append(
                {
                    "page": page,
                    "jobs_returned": len(rows),
                    "jobs_normalized": added,
                    "reported_total": total,
                    "reported_total_pages": total_pages,
                }
            )
            if total_changed or page_overlap:
                finish_changed(
                    metadata, terminal_evidence="collection_changed_during_scan"
                )
                break
            if finish_authoritative_total(metadata, total, len(jobs)):
                break
            if page >= total_pages:
                metadata["stop_reason"] = "terminal_page_count_mismatch"
                metadata["scan_completeness_reason"] = "pagination_failure"
                raise AdapterError(
                    "Uber Careers terminal page did not reach its reported total"
                )
            if page >= int(metadata["effective_listing_request_ceiling"]):
                metadata["stop_reason"] = "maximum_pages_reached"
                metadata["scan_completeness_reason"] = "safety_limit_reached"
                metadata["safety_limit_reached"] = True
                break
            time.sleep(PAGE_DELAY_SECONDS)
            continue

    return jobs


def _job_envelope(
    envelope: Any, expected_page: int
) -> tuple[list[Mapping[str, Any]], int, int, int]:
    """Validate one anonymous response from Uber's same-origin search API."""
    if not isinstance(envelope, Mapping):
        raise AdapterError("Uber Careers API response must be a JSON object")
    rows = envelope.get("jobs")
    total = envelope.get("totalJobs")
    total_pages = envelope.get("totalPages")
    page = envelope.get("page")
    page_size = envelope.get("pageSize")
    integers = (total, total_pages, page, page_size)
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, Mapping) for row in rows)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in integers)
        or total < 0
        or total_pages < 0
        or page != expected_page
        or page_size != PAGE_SIZE
        or total_pages != (ceil(total / page_size) if total else 0)
        or len(rows) > page_size
        or (page < total_pages and len(rows) != page_size)
        or (page == total_pages and (page - 1) * page_size + len(rows) != total)
    ):
        raise AdapterError("Uber Careers API violated its pagination contract")
    return list(rows), total, total_pages, page_size


def _rsc_job_envelope(
    root: Any, expected_page: int
) -> tuple[list[Mapping[str, Any]], int, int, int] | None:
    """Extract the one reviewed job collection from Next's RSC bootstrap."""
    candidates: list[Mapping[str, Any]] = []
    for script in root.xpath("//script[not(@src)]"):
        body = str(script.text or "").strip()
        if "totalJobs" not in body or not body.startswith(RSC_PREFIX) or not body.endswith(")"):
            continue
        try:
            pushed = json.loads(body[len(RSC_PREFIX) : -1])
        except (TypeError, ValueError) as exc:
            raise AdapterError("Uber Careers RSC bootstrap was malformed") from exc
        if (
            not isinstance(pushed, list)
            or len(pushed) != 2
            or not isinstance(pushed[1], str)
        ):
            raise AdapterError("Uber Careers RSC bootstrap had an unexpected shape")
        value = pushed[1]
        marker = value.find('{"jobs":')
        if marker < 0:
            continue
        try:
            candidate, _end = json.JSONDecoder().raw_decode(value[marker:])
        except (TypeError, ValueError) as exc:
            raise AdapterError("Uber Careers job envelope was malformed") from exc
        if isinstance(candidate, Mapping):
            candidates.append(candidate)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise AdapterError("Uber Careers exposed an ambiguous job collection")
    envelope = candidates[0]
    rows = envelope.get("jobs")
    total = envelope.get("totalJobs")
    total_pages = envelope.get("totalPages")
    page = envelope.get("page")
    page_size = envelope.get("pageSize")
    integers = (total, total_pages, page, page_size)
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, Mapping) for row in rows)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in integers)
        or total < 0
        or total_pages < 0
        or page != expected_page
        or page_size != PAGE_SIZE
        or total_pages != (ceil(total / page_size) if total else 0)
        or len(rows) > page_size
        or (page < total_pages and len(rows) != page_size)
        or (page == total_pages and (page - 1) * page_size + len(rows) != total)
    ):
        raise AdapterError("Uber Careers job envelope violated its pagination contract")
    return list(rows), total, total_pages, page_size


def _normalize_api_job(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    job_id = str(item.get("Id") or "").strip()
    reference = str(item.get("Reference") or "").strip()
    title = " ".join(str(item.get("Title") or "").split())
    if not job_id.isdigit() or reference != job_id or not title:
        raise AdapterError("Uber Careers job is missing a stable ID or title")
    raw_urls = item.get("Urls")
    if not isinstance(raw_urls, list):
        raise AdapterError("Uber Careers job is missing its URL collection")
    defaults = [
        value
        for value in raw_urls
        if isinstance(value, Mapping) and value.get("IsDefault") is True
    ]
    if len(defaults) != 1:
        raise AdapterError("Uber Careers job must expose one canonical URL")
    path = str(defaults[0].get("Url") or "")
    match = JOB_PATH_RE.fullmatch(path)
    if not match or match.group(1) != job_id:
        raise AdapterError("Uber Careers canonical URL did not bind its stable ID")
    url = urljoin(SEARCH_URL, path)
    if (urlparse(url).hostname or "").casefold() != "jobs.uber.com":
        raise AdapterError("Uber Careers canonical URL left the official host")
    raw_locations = item.get("Locations")
    if not isinstance(raw_locations, list):
        raise AdapterError("Uber Careers job locations must be a list")
    locations: list[str] = []
    for row in raw_locations:
        if not isinstance(row, Mapping):
            raise AdapterError("Uber Careers job contains a malformed location")
        value = " ".join(str(row.get("Address") or "").split())
        if value and value not in locations:
            locations.append(value)
    teams = item.get("Teams")
    if not isinstance(teams, list) or any(not isinstance(value, str) for value in teams):
        raise AdapterError("Uber Careers job teams must be a string list")
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=str(item.get("DisplayDate") or ""),
        source="custom:uber",
        departments=[" ".join(value.split()) for value in teams if value.strip()],
        offices=locations,
        raw_metadata={
            "platform": "uber",
            "endpoint_variant": "same_origin_search_api_v1",
            "external_job_id": job_id,
        },
    )


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
