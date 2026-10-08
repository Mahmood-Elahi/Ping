"""Fail-closed adapter for Jobvite's counted public tenant search page."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from lxml import html

from src.scraper.company_careers.listing_completion import record_terminal_evidence
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)


ENDPOINT_VARIANT = "server_search_total_v1"
MAX_RESULTS = 500
MAX_RESPONSE_BYTES = 4_000_000
MAX_TIMEOUT_SECONDS = 60
HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
RANGE_RE = re.compile(
    r"(?:displaying\s+)?(\d+)\s*(?:to|[-\u2010-\u2015\u2212])\s*(\d+)\s+of\s+(\d+)",
    re.I,
)
RESULT_RANGE_XPATH = (
    "//*[contains(concat(' ', normalize-space(@class), ' '), "
    "' jv-pagination-text ')]"
)
JOB_TITLE_ANCHOR_XPATH = (
    "//table[contains(concat(' ', normalize-space(@class), ' '), "
    "' jv-job-list ')]//td[contains(concat(' ', normalize-space(@class), ' '), "
    "' jv-job-list-name ')]//a"
)


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch one tenant-bound page only when its authoritative range is final."""
    _validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(
        endpoint_variant=ENDPOINT_VARIANT,
        request_method="GET",
        jobs_normalized=0,
        rejected_record_count=0,
        stop_reason="not_started",
    )
    publish_source_scan_metadata(session, metadata)

    metadata["pages_requested"] = 1
    identity_response, identity_body = _get_html(
        session, str(target.careers_url), timeout, int(target.max_response_bytes)
    )
    identity_url = urlparse(
        str(getattr(identity_response, "url", "") or target.careers_url)
    )
    configured_careers_url = urlparse(str(target.careers_url))
    try:
        identity_root = html.fromstring(identity_body)
        identity_visible_text = " ".join(identity_root.text_content().split())
    except (TypeError, ValueError) as exc:
        raise AdapterError("Jobvite official careers page returned malformed HTML") from exc
    if (
        identity_url.scheme != "https"
        or identity_url.hostname != configured_careers_url.hostname
        or identity_url.username
        or identity_url.password
        or not re.search(str(target.identity_regex), identity_visible_text, re.I)
        or not _has_exact_tenant_link(
            identity_root, str(target.careers_url), str(target.slug)
        )
    ):
        raise AdapterError("Jobvite official careers page did not bind the configured tenant")
    metadata["pages_completed"] = 1
    metadata["source_identity_status"] = "validated"

    metadata["pages_requested"] += 1
    listing_response, listing_body = _get_html(
        session, str(target.listing_url), timeout, int(target.max_response_bytes)
    )
    final_url = urlparse(
        str(getattr(listing_response, "url", "") or target.listing_url)
    )
    if (
        final_url.scheme != "https"
        or final_url.hostname != "jobs.jobvite.com"
        or final_url.username
        or final_url.password
        or final_url.path.rstrip("/") != f"/{target.slug}/search"
        or final_url.params
        or final_url.query
        or final_url.fragment
    ):
        raise AdapterError("Jobvite search redirected outside the configured tenant")
    metadata["pages_completed"] += 1

    try:
        root = html.fromstring(listing_body)
    except (TypeError, ValueError) as exc:
        raise AdapterError("Jobvite search returned malformed HTML") from exc
    range_elements = root.xpath(RESULT_RANGE_XPATH)
    if len(range_elements) != 1:
        raise AdapterError("Jobvite search did not expose one authoritative result range")
    range_text = " ".join(range_elements[0].text_content().split())
    range_match = RANGE_RE.fullmatch(range_text)
    if range_match is None:
        raise AdapterError("Jobvite authoritative result range is malformed")
    start, end, total = (int(value) for value in range_match.groups())
    if total > int(target.max_jobs) or total > MAX_RESULTS:
        metadata.update(
            total_available=total,
            authoritative_total_present=True,
            safety_limit_reached=True,
            stop_reason="maximum_results_reached",
            scan_completeness_reason="safety_limit_reached",
        )
        return []
    if total == 0:
        if (start, end) not in {(0, 0), (1, 0)}:
            raise AdapterError("Jobvite empty result range is malformed")
        if root.xpath(JOB_TITLE_ANCHOR_XPATH):
            raise AdapterError("Jobvite empty result range conflicts with job rows")
        metadata.update(
            scan_complete=True,
            scan_completeness_reason="validated_zero_jobs",
            total_available=0,
            authoritative_total_present=True,
            stop_reason="validated_zero_jobs",
            terminal_page_observed=True,
            complete_collection_response=True,
            has_more=False,
        )
        record_terminal_evidence(
            metadata,
            "authoritative_result_range",
            "0 of 0",
            has_more=False,
            terminal_page_observed=True,
            complete_collection_response=True,
        )
        return []
    if start != 1 or end != total:
        raise AdapterError("Jobvite result range does not prove the final complete page")

    anchors = root.xpath(JOB_TITLE_ANCHOR_XPATH)
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for anchor in anchors:
        try:
            href = str(anchor.get("href") or "").strip()
            absolute = urljoin(str(target.listing_url), href)
            job = _normalize_jobvite(anchor, absolute, target)
        except (AdapterError, ValueError) as exc:
            metadata["rejected_record_count"] += 1
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError(
                "Jobvite row lacks a stable tenant URL, title, or location"
            ) from exc
        if job.job_id in seen_ids or job.url in seen_urls:
            metadata["duplicate_ids"] = True
            raise AdapterError("Jobvite search repeated a stable ID or canonical URL")
        seen_ids.add(job.job_id)
        seen_urls.add(job.url)
        jobs.append(job)
    if len(jobs) != total:
        raise AdapterError("Jobvite normalized row count conflicts with its result range")

    metadata.update(
        scan_complete=True,
        scan_completeness_reason="reported_total_reached",
        total_available=total,
        authoritative_total_present=True,
        jobs_normalized=len(jobs),
        stop_reason="reported_total_reached",
        has_more=False,
        terminal_page_observed=True,
    )
    record_terminal_evidence(
        metadata,
        "authoritative_result_range",
        f"1-{end} of {total}",
        has_more=False,
        terminal_page_observed=True,
    )
    return jobs


def _validate_target(target: CompanyTarget) -> None:
    slug = str(target.slug or "").strip().casefold()
    listing = urlparse(str(target.listing_url or ""))
    careers = urlparse(str(target.careers_url or ""))
    allowed = {str(value).casefold() for value in target.allowed_apply_hosts}
    if (
        target.endpoint_variant != ENDPOINT_VARIANT
        or not SLUG_RE.fullmatch(slug)
        or target.slug != slug
        or listing.scheme != "https"
        or listing.hostname != "jobs.jobvite.com"
        or listing.username
        or listing.password
        or listing.path.rstrip("/") != f"/{slug}/search"
        or listing.params
        or listing.query
        or listing.fragment
        or careers.scheme != "https"
        or not careers.hostname
        or careers.username
        or careers.password
        or allowed != {"jobs.jobvite.com"}
        or not target.identity_regex
        or not 1 <= int(target.max_jobs) <= MAX_RESULTS
        or not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES
    ):
        raise AdapterError("Jobvite target does not bind one reviewed public tenant")
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"Jobvite identity regex is invalid: {exc}") from exc


def validate_target(target: CompanyTarget) -> None:
    """Public registry-validation entry point."""
    _validate_target(target)


def _has_exact_tenant_link(root: Any, base_url: str, slug: str) -> bool:
    tenant_root = f"/{slug}".casefold()
    for anchor in root.xpath("//a[@href]"):
        try:
            parsed = urlparse(
                urljoin(base_url, str(anchor.get("href") or "").strip())
            )
            path = parsed.path.rstrip("/").casefold()
            if (
                parsed.scheme == "https"
                and parsed.hostname == "jobs.jobvite.com"
                and not parsed.username
                and not parsed.password
                and (path == tenant_root or path.startswith(f"{tenant_root}/"))
            ):
                return True
        except ValueError:
            continue
    return False


def _normalize_jobvite(anchor: Any, absolute_url: str, target: CompanyTarget) -> JobRecord:
    parsed = urlparse(absolute_url)
    match = re.fullmatch(
        rf"/{re.escape(str(target.slug))}/job/([^/?#]+)/?", parsed.path, re.I
    )
    title = " ".join(anchor.text_content().split())
    row = anchor.xpath("ancestor::tr[1]")
    locations = row[0].xpath(
        ".//td[contains(concat(' ', normalize-space(@class), ' '), "
        "' jv-job-list-location ')]"
    ) if row else []
    location = " ".join(locations[0].text_content().split()) if locations else ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != "jobs.jobvite.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or match is None
        or not title
        or not location
    ):
        raise AdapterError("Jobvite row lacks a stable tenant URL, title, or location")
    external_id = match.group(1)
    canonical = urlunparse(parsed._replace(query="", fragment=""))
    return JobRecord(
        job_id=f"jobvite:{target.slug}:{external_id}",
        title=title,
        company=target.name,
        location=location,
        url=canonical,
        updated_at="",
        source=f"jobvite:{target.slug}",
        offices=[location],
        raw_metadata={
            "external_job_id": external_id,
            "platform": "jobvite",
            "endpoint_variant": ENDPOINT_VARIANT,
        },
    )


def _get_html(
    session: requests.Session,
    url: str,
    timeout: int,
    byte_limit: int,
) -> tuple[Any, bytes]:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    if response.status_code in {401, 407}:
        raise AdapterError(f"Jobvite authentication required (HTTP {response.status_code})")
    if response.status_code in {403, 429, 451}:
        raise AdapterError(f"Jobvite source blocked (HTTP {response.status_code})")
    if response.status_code != 200:
        raise AdapterError(f"Jobvite HTTP {response.status_code}")
    body = bytes(getattr(response, "content", b"") or b"")
    if not body:
        body = str(getattr(response, "text", "") or "").encode("utf-8")
    if not body or len(body) > byte_limit:
        raise AdapterError("Jobvite response was empty or exceeded its byte limit")
    return response, body
