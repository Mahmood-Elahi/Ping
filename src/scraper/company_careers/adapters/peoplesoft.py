"""Oracle PeopleSoft public external-careers adapter."""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from lxml import html

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence


MAX_RESULTS = 20
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
JOB_ID_RE = re.compile(r"^[1-9][0-9]{2,19}$")
BLOCKED_MARKERS = (
    "cf-chl-",
    "session expired",
    "verify you are human",
    "captcha challenge",
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch public PeopleSoft job links and bounded anonymous detail pages."""
    config = validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    listing_response = _get(session, str(target.listing_url), timeout)
    listing_root, listing_text = _parse_page(listing_response, "listing")
    if not re.search(str(target.identity_regex), listing_text, re.IGNORECASE):
        raise AdapterError("PeopleSoft listing has mixed or ambiguous company identity")

    candidates: list[tuple[str, str, str, str]] = []
    seen_ids: set[str] = set()
    for link in listing_root.xpath("//a[@href]"):
        proposed = urljoin(str(target.listing_url), str(link.get("href") or ""))
        parsed = urlparse(proposed)
        query = {key.casefold(): values for key, values in parse_qs(parsed.query).items()}
        job_id = str((query.get("jobopeningid") or [""])[0]).strip()
        page = str((query.get("page") or [""])[0]).strip().casefold()
        site_id = str((query.get("siteid") or [""])[0]).strip()
        posting_sequence = str((query.get("postingseq") or [""])[0]).strip()
        if (parsed.hostname or "").casefold() not in config["allowed_hosts"]:
            continue
        if page != "hrs_app_jbpst_fl" or not JOB_ID_RE.fullmatch(job_id):
            continue
        if site_id != config["site_id"] or not posting_sequence.isdigit():
            raise AdapterError("PeopleSoft job link has a mixed site or unstable posting sequence")
        if job_id in seen_ids:
            raise AdapterError("PeopleSoft listing returned a duplicate stable JobOpeningId")
        seen_ids.add(job_id)
        candidates.append((job_id, " ".join(link.text_content().split()), proposed, posting_sequence))
    if not candidates:
        if "no current" in listing_text.casefold() or "no job" in listing_text.casefold():
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=True,
                    scan_completeness_reason="validated_zero_jobs",
                    total_available=0,
                    pages_requested=1,
                    pages_completed=1,
                    stop_reason="validated_zero_jobs",
                ),
            )
            return []
        raise AdapterError("PeopleSoft listing has no stable public job-specific links")
    total_candidates = len(candidates)
    candidates = candidates[: int(target.max_jobs)]

    jobs: list[JobRecord] = []
    for job_id, listing_title, detail_url, posting_sequence in candidates[: int(target.max_jobs)]:
        detail_response = _get(session, detail_url, timeout)
        route_state = _detail_route_state(detail_response, detail_url, config)
        root, page_text = _parse_page(detail_response, "detail")
        if not re.search(str(target.identity_regex), page_text, re.IGNORECASE):
            raise AdapterError("PeopleSoft detail has mixed or ambiguous company identity")
        if route_state == "stale":
            if (
                _field_text(root, "HRS_SCH_WRK2_HRS_JOB_OPENING_ID")
                or _field_text(root, "HRS_SCH_WRK2_POSTING_TITLE")
                or root.xpath("//a[normalize-space()='Apply for Job']")
            ):
                raise AdapterError("PeopleSoft stale-detail redirect contains ambiguous job detail")
            continue
        returned_id = _field_text(root, "HRS_SCH_WRK2_HRS_JOB_OPENING_ID")
        title = _field_text(root, "HRS_SCH_WRK2_POSTING_TITLE")
        location = _field_text(root, "HRS_SCH_WRK_HRS_DESCRLONG")
        if returned_id != job_id:
            raise AdapterError("PeopleSoft detail does not match its stable JobOpeningId")
        if not title or _normalized_title(title) != _normalized_title(listing_title):
            raise AdapterError("PeopleSoft detail title does not match its listing title")
        if not root.xpath("//a[normalize-space()='Apply for Job']"):
            raise AdapterError("PeopleSoft detail lacks an official job-specific apply flow")
        descriptions = [
            " ".join(node.text_content().split())
            for node in root.xpath("//*[starts-with(@id, 'HRS_SCH_PSTDSC_DESCRLONG')]")
            if " ".join(node.text_content().split())
        ]
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=target.name,
                location=location,
                url=detail_url,
                updated_at="",
                source=f"peoplesoft:{urlparse(detail_url).hostname.casefold()}:{config['site_id']}",
                offices=[location] if location else [],
                description="\n\n".join(dict.fromkeys(descriptions)) or None,
                raw_metadata={
                    "platform": "peoplesoft",
                    "site_id": config["site_id"],
                    "posting_sequence": posting_sequence,
                },
            )
        )
    limited = total_candidates > int(target.max_jobs)
    metadata = SourceScanMetadata(
            scan_complete=not limited,
            scan_completeness_reason=(
                "safety_limit_reached" if limited else "complete_listing_container"
            ),
            total_available=total_candidates,
            pages_requested=1,
            pages_completed=1,
            stop_reason=("maximum_results_reached" if limited else "complete_listing_container"),
            safety_limit_reached=limited,
            detail_requests_coupled=True,
        )
    record_terminal_evidence(
        metadata,
        "complete_collection_response",
        total_candidates,
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    publish_source_scan_metadata(session, metadata)
    return jobs


def validate_target(target: CompanyTarget) -> dict[str, Any]:
    if not target.listing_url or not target.careers_url or not target.identity_regex:
        raise AdapterError("PeopleSoft target needs listing_url, careers_url, and identity_regex")
    listing = urlparse(str(target.listing_url))
    careers = urlparse(str(target.careers_url))
    if listing.scheme != "https" or not listing.hostname or careers.scheme != "https" or not careers.hostname:
        raise AdapterError("PeopleSoft listing and careers URLs must use HTTPS")
    allowed_hosts = {str(host).strip().casefold() for host in target.allowed_apply_hosts if str(host).strip()}
    if not allowed_hosts or any("/" in host or ":" in host for host in allowed_hosts):
        raise AdapterError("PeopleSoft target needs hostname-only allowed_apply_hosts")
    try:
        re.compile(str(target.identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"PeopleSoft identity_regex is invalid: {exc}") from exc
    if int(target.max_pages) != 1:
        raise AdapterError("PeopleSoft supports exactly one bounded public listing page")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"PeopleSoft max_jobs cannot exceed {MAX_RESULTS}")
    return {
        "allowed_hosts": allowed_hosts,
        "site_id": str(target.site_id or "1"),
    }


def _get(session: requests.Session, url: str, timeout: int) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        status = getattr(response, "status_code", None)
        if status in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if status in {401, 407}:
            raise AdapterError(f"PeopleSoft authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"PeopleSoft blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"PeopleSoft HTTP {status}")
        return response
    raise AdapterError("PeopleSoft request attempts exhausted")


def _parse_page(response: Any, page_type: str) -> tuple[Any, str]:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("PeopleSoft response exceeded the byte limit")
    text = content.decode("utf-8", errors="replace")
    lowered = text.casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise AdapterError("PeopleSoft page requires CAPTCHA, challenge, or session state")
    try:
        root = html.fromstring(content, base_url=str(getattr(response, "url", "") or ""))
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"PeopleSoft returned malformed {page_type} HTML: {exc}") from exc
    return root, " ".join(" ".join(root.itertext()).split())


def _field_text(root: Any, field_id: str) -> str:
    nodes = root.xpath(f"//*[@id='{field_id}']")
    if len(nodes) > 1:
        raise AdapterError(f"PeopleSoft detail contains duplicate {field_id} fields")
    return " ".join(nodes[0].text_content().split()) if nodes else ""


def _detail_route_state(response: Any, requested_url: str, config: dict[str, Any]) -> str:
    """Classify only the two anonymous detail outcomes exposed by PeopleSoft."""
    requested = urlparse(requested_url)
    returned = urlparse(str(getattr(response, "url", "") or ""))
    returned_host = (returned.hostname or "").casefold()
    if returned.scheme != "https" or returned_host not in config["allowed_hosts"]:
        raise AdapterError("PeopleSoft detail redirected outside its official HTTPS host")
    if returned.path != requested.path:
        raise AdapterError("PeopleSoft detail redirected to an unexpected official route")

    query = {key.casefold(): values for key, values in parse_qs(returned.query).items()}
    page = str((query.get("page") or [""])[0]).strip().casefold()
    if page == "hrs_app_schjob_fl":
        action = str((query.get("action") or [""])[0]).strip().casefold()
        if action == "u":
            return "stale"
        raise AdapterError("PeopleSoft detail redirected to an ambiguous job-search route")
    if page != "hrs_app_jbpst_fl":
        raise AdapterError("PeopleSoft detail redirected to an unexpected official route")

    requested_query = {
        key.casefold(): values for key, values in parse_qs(requested.query).items()
    }
    for field in ("jobopeningid", "postingseq", "siteid"):
        expected = str((requested_query.get(field) or [""])[0]).strip()
        actual = str((query.get(field) or [""])[0]).strip()
        if actual != expected:
            raise AdapterError("PeopleSoft detail route changed its stable job identity")
    return "detail"


def _normalized_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
