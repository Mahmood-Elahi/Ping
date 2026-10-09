"""Paycor Recruiting public CareerHome adapter."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from lxml import html

from src.scraper.company_careers.models import (
    AdapterError,
    AdapterResult,
    CompanyTarget,
    InventoryItem,
    ItemFailure,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence


MAX_RESULTS = 50
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
SUPPORTED_HOST = "recruitingbypaycor.com"
BLOCKED_MARKERS = ("captcha", "recaptcha", "turnstile", "cf-chl-", "session expired")


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> AdapterResult:
    """Fetch one bounded, anonymous Paycor CareerHome collection."""
    config = validate_target(target)
    response = _get(session, config["endpoint_url"], min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS))
    content = _response_bytes(response)
    lowered = content.decode("utf-8", errors="replace").casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise AdapterError("Paycor page requires CAPTCHA, challenge, or session state")
    try:
        root = html.fromstring(content, base_url=config["endpoint_url"])
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Paycor returned malformed HTML: {exc}") from exc
    page_text = " ".join(root.text_content().split())
    if not re.search(str(target.identity_regex), page_text, re.IGNORECASE):
        raise AdapterError("Paycor page has mixed or ambiguous company identity")
    metadata = SourceScanMetadata(
        endpoint_variant="career_home_complete_html",
        pages_requested=1,
        pages_completed=1,
        maximum_pages=1,
        maximum_results=int(target.max_jobs),
        detail_requests_coupled=True,
        source_identity_status="validated",
    )
    publish_source_scan_metadata(session, metadata)
    cards = root.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), "
        "' gnewtonCareerGroupRowClass ')]"
    )
    if not cards:
        if (
            "no open positions" in page_text.casefold()
            or "no positions available" in page_text.casefold()
            or "currently have no openings" in page_text.casefold()
        ):
            metadata.update(
                scan_complete=True,
                scan_completeness_reason="validated_zero_jobs",
                total_available=0,
                stop_reason="validated_zero_jobs",
            )
            return []
        raise AdapterError("Paycor CareerHome is missing its job-card contract")
    if len(cards) > MAX_RESULTS:
        raise AdapterError("Paycor CareerHome exceeded the hard result limit")
    metadata["total_available"] = len(cards)
    record_terminal_evidence(
        metadata,
        "complete_collection_response",
        len(cards),
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )

    jobs: list[JobRecord] = []
    inventory_items: list[InventoryItem] = []
    item_failures: list[ItemFailure] = []
    seen_ids: set[str] = set()
    for card in cards[: int(target.max_jobs)]:
        links = card.xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), "
            "' gnewtonCareerGroupJobTitleClass ')]//a[@href]"
        )
        if len(links) != 1:
            raise AdapterError("Paycor job card is missing one detail link")
        parsed = urlparse(links[0].get("href") or "")
        query = parse_qs(parsed.query)
        job_id = str((query.get("id") or [""])[0]).strip()
        client_id = str((query.get("clientId") or [""])[0]).strip()
        title = " ".join(links[0].text_content().split())
        if not JOB_ID_RE.fullmatch(job_id) or client_id.casefold() != config["client_id"]:
            raise AdapterError("Paycor job card has an unstable or cross-client job ID")
        if job_id.casefold() in seen_ids:
            raise AdapterError("Paycor returned a duplicate stable job ID")
        if not title:
            raise AdapterError("Paycor job card is missing a title")
        seen_ids.add(job_id.casefold())
        inventory_items.append(
            InventoryItem(job_id, f"paycor:{config['client_id']}")
        )
        locations = card.xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), "
            "' gnewtonCareerGroupJobDescriptionClass ')]"
        )
        location = " ".join(locations[0].text_content().split()) if locations else ""
        detail_url = str(links[0].get("href") or "")
        try:
            detail_response = _get(session, detail_url, timeout)
        except requests.RequestException as exc:
            item_failures.append(
                ItemFailure(
                    job_id,
                    f"{exc.__class__.__name__}: {exc}",
                    retryable=True,
                )
            )
            continue
        except AdapterError as exc:
            if _transient_detail_error(exc):
                item_failures.append(
                    ItemFailure(job_id, str(exc), retryable=True)
                )
                continue
            raise
        detail_content = _response_bytes(detail_response)
        detail_text = detail_content.decode("utf-8", errors="replace")
        if any(marker in detail_text.casefold() for marker in BLOCKED_MARKERS):
            raise AdapterError("Paycor detail requires CAPTCHA, challenge, or session state")
        try:
            detail_root = html.fromstring(detail_content, base_url=detail_url)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Paycor returned malformed detail HTML: {exc}") from exc
        detail_page_text = " ".join(" ".join(detail_root.itertext()).split())
        if not re.search(str(target.identity_regex), detail_page_text, re.IGNORECASE):
            raise AdapterError("Paycor detail has mixed or ambiguous company identity")
        detail_title = _element_text(detail_root, "gnewtonJobPosition")
        detail_location = _element_text(detail_root, "gnewtonJobLocationInfo")
        description = _element_text(detail_root, "gnewtonJobDescriptionText")
        if title.casefold() not in detail_title.casefold() or not description:
            raise AdapterError("Paycor detail does not match its title and description contract")
        if location and detail_location and location.casefold() != detail_location.casefold():
            raise AdapterError("Paycor detail location does not match its listing")
        apply_forms = detail_root.xpath("//form[@id='gravityApplyJob' and @action]")
        if len(apply_forms) != 1:
            raise AdapterError("Paycor detail lacks one official job-specific apply form")
        observed_apply = urlparse(str(apply_forms[0].get("action") or ""))
        observed_query = parse_qs(observed_apply.query)
        if (
            observed_apply.scheme != "https"
            or (observed_apply.hostname or "").casefold() != SUPPORTED_HOST
            or observed_apply.path != "/career/SubmitResume.action"
            or (observed_query.get("clientId") or [""])[0].casefold() != client_id.casefold()
            or (observed_query.get("id") or [""])[0].casefold() != job_id.casefold()
        ):
            raise AdapterError("Paycor detail has an unofficial or cross-job apply form")
        apply_url = urlunparse(
            parsed._replace(
                scheme="https",
                netloc=SUPPORTED_HOST,
                path="/career/SubmitResume.action",
                query=urlencode({"clientId": client_id, "id": job_id}),
                fragment="",
            )
        )
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=target.name,
                location=location,
                url=apply_url,
                updated_at="",
                source=f"paycor:{config['client_id']}",
                offices=[location] if location else [],
                description=description,
                raw_metadata={
                    "platform": "paycor",
                    "client_id": client_id,
                    "detail_url": detail_url,
                },
            )
        )
    if len(cards) > int(target.max_jobs):
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["stop_reason"] = "maximum_results_reached"
        metadata["safety_limit_reached"] = True
    else:
        metadata["scan_complete"] = True
        metadata["scan_completeness_reason"] = "complete_listing_container"
        metadata["stop_reason"] = "complete_listing_container"
    metadata.update(
        unreadable_items=len(item_failures),
        jobs_normalized=len(jobs),
    )
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=tuple(inventory_items),
        item_failures=tuple(item_failures),
        capabilities_used=frozenset({"inventory", "hydration", "per_item_failure"}),
    )


def _transient_detail_error(exc: AdapterError) -> bool:
    return bool(
        re.search(r"HTTP (408|425|429|500|502|503|504)\b", str(exc))
        or "attempts exhausted" in str(exc).casefold()
    )


def validate_target(target: CompanyTarget) -> dict[str, str]:
    if not target.endpoint_url or not target.careers_url or not target.identity_regex:
        raise AdapterError("Paycor target needs endpoint_url, careers_url, and identity_regex")
    endpoint = urlparse(str(target.endpoint_url))
    query = parse_qs(endpoint.query)
    client_id = str((query.get("clientId") or [""])[0]).strip().casefold()
    if (
        endpoint.scheme != "https"
        or (endpoint.hostname or "").casefold() != SUPPORTED_HOST
        or endpoint.path != "/career/CareerHome.action"
        or not CLIENT_ID_RE.fullmatch(client_id)
        or endpoint.fragment
    ):
        raise AdapterError("Paycor endpoint_url is not a supported public CareerHome URL")
    careers = urlparse(str(target.careers_url))
    if careers.scheme != "https" or not careers.hostname:
        raise AdapterError("Paycor careers_url must be an official HTTPS URL")
    try:
        re.compile(str(target.identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"Paycor identity_regex is invalid: {exc}") from exc
    if int(target.max_pages) != 1:
        raise AdapterError("Paycor supports exactly one bounded CareerHome page")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Paycor max_jobs cannot exceed {MAX_RESULTS}")
    normalized_endpoint = urlunparse(endpoint._replace(query=urlencode({"clientId": client_id})))
    return {"client_id": client_id, "endpoint_url": normalized_endpoint}


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
            raise AdapterError(f"Paycor authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"Paycor blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Paycor HTTP {status}")
        return response
    raise AdapterError("Paycor request attempts exhausted")


def _response_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("Paycor response exceeded the byte limit")
    return content


def _element_text(root: Any, element_id: str) -> str:
    nodes = root.xpath(f"//*[@id='{element_id}']")
    if len(nodes) > 1:
        raise AdapterError(f"Paycor detail contains duplicate {element_id} elements")
    return " ".join(" ".join(nodes[0].itertext()).split()) if nodes else ""
