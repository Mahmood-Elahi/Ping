"""Atlassian Careers adapter."""

from __future__ import annotations

import re
from html import unescape
from typing import Any, List
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterResult,
    AdapterError,
    CompanyTarget,
    InventoryItem,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence
from src.scraper.company_careers.mutable_collection import immutable_job_fingerprint


LISTINGS_URL = "https://www.atlassian.com/endpoint/careers/listings"
APPLY_HOSTS = {
    "careers-americas.icims.com",
    "careers-apac-atlassian.icims.com",
}


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> AdapterResult:
    if target.name != "Atlassian" or target.platform != "atlassian":
        raise AdapterError("Atlassian adapter requires the exact Atlassian target")
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
    response_bytes = len(bytes(getattr(response, "content", b"") or b""))
    if response_bytes > int(target.max_response_bytes):
        raise AdapterError("Atlassian Careers response exceeded max_response_bytes")
    if not payload:
        raise AdapterError(
            "Atlassian Careers returned an empty atomic collection without validated-zero evidence"
        )

    metadata = SourceScanMetadata(
        total_available=len(payload),
        # This is a raw array length, not a separately declared provider total.
        authoritative_total_present=False,
        pages_requested=1,
        pages_completed=1,
        stop_reason="not_started",
        endpoint_variant="atomic_json_collection",
        source_identity_status="validated",
        source_identity_diagnostic=(
            "The exact Atlassian target was read from the fixed first-party "
            "Atlassian careers listings endpoint"
        ),
        bytes_inspected=response_bytes,
        maximum_response_bytes=int(target.max_response_bytes),
        response_fully_read=True,
    )
    publish_source_scan_metadata(session, metadata)

    jobs: List[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_fingerprints: dict[str, tuple[Any, ...]] = {}
    duplicate_representations = 0
    for item in payload:
        if not isinstance(item, dict):
            raise AdapterError("Atlassian Careers collection contained a malformed row")
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            raise AdapterError("Atlassian Careers job was missing a stable ID or title")
        locations = _clean_strings(item.get("locations"))
        portal_post = item.get("portalJobPost") if isinstance(item.get("portalJobPost"), dict) else {}
        url = _validated_job_url(
            job_id, item.get("applyUrl") or portal_post.get("portalUrl")
        )
        job = JobRecord(
            job_id=job_id,
            title=title,
            company=target.name,
            location=", ".join(locations),
            url=url,
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
                metadata["scan_completeness_reason"] = "malformed_atomic_collection"
                raise AdapterError(
                    "Atlassian Careers collection contained conflicting records for a stable job ID"
                )
            duplicate_representations += 1
            continue
        seen_ids.add(job_id)
        seen_urls.add(url)
        seen_fingerprints[job_id] = fingerprint
        jobs.append(job)

    metadata["jobs_normalized"] = len(jobs)
    metadata["unique_inventory_items"] = len(jobs)
    metadata["unique_canonical_urls"] = len(seen_urls)
    metadata["duplicate_representations"] = duplicate_representations
    metadata["duplicate_representations_accounted"] = True
    metadata["terminal_accounted_items"] = len(payload)
    metadata["scan_complete"] = True
    metadata["scan_completeness_reason"] = "complete_collection_response"
    metadata["stop_reason"] = "complete_collection_response"
    metadata["complete_collection_response"] = True
    record_terminal_evidence(
        metadata,
        "complete_collection_response",
        "atomic_json_collection",
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=tuple(
            InventoryItem(item_id=job.job_id, source=job.source) for job in jobs
        ),
    )


def _validated_job_url(job_id: str, value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or host not in APPLY_HOSTS
        or len(path_parts) < 2
        or path_parts[0].casefold() != "jobs"
        or path_parts[1] != job_id
        or parsed.fragment
    ):
        raise AdapterError(
            "Atlassian Careers job URL did not match the observed iCIMS host and stable-ID path contract"
        )
    return url


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
