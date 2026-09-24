"""Atlassian Careers adapter."""

from __future__ import annotations

import re
from html import unescape
from typing import Any, List

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.mutable_collection import (
    finish_terminal,
    immutable_job_fingerprint,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


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

    metadata = SourceScanMetadata(
        total_available=len(payload),
        pages_requested=1,
        pages_completed=1,
        stop_reason="not_started",
    )
    initialize_mutable_collection(metadata)
    record_total_observation(
        metadata, len(payload), offset=0, source_label="Atlassian Careers"
    )
    publish_source_scan_metadata(session, metadata)

    jobs: List[JobRecord] = []
    seen_ids: set[str] = set()
    seen_fingerprints: dict[str, tuple[Any, ...]] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise AdapterError("Atlassian Careers collection contained a malformed row")
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            raise AdapterError("Atlassian Careers job was missing a stable ID or title")
        locations = _clean_strings(item.get("locations"))
        portal_post = item.get("portalJobPost") if isinstance(item.get("portalJobPost"), dict) else {}
        url = item.get("applyUrl") or portal_post.get("portalUrl") or ""
        job = JobRecord(
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
        fingerprint = immutable_job_fingerprint(job)
        if job_id in seen_ids:
            if seen_fingerprints[job_id] != fingerprint:
                metadata["stop_reason"] = "conflicting_repeated_job_id"
                metadata["scan_completeness_reason"] = "pagination_failure"
                raise AdapterError(
                    "Atlassian Careers collection contained conflicting records for a stable job ID"
                )
            mark_collection_changed(metadata, offset=0, overlap_count=1)
            continue
        seen_ids.add(job_id)
        seen_fingerprints[job_id] = fingerprint
        jobs.append(job)

    metadata["jobs_normalized"] = len(jobs)
    finish_terminal(metadata, complete_reason="complete_collection_response")
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
