"""Bounded adapter for Lever's anonymous public postings API."""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, List, Mapping
from urllib.parse import urlencode, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    record_terminal_evidence,
)


ENDPOINT_VARIANT = "postings_api_v0"
PAGE_SIZE = 20
MAX_RESULTS = 500
MAX_PAGES = 25
MAX_RESPONSE_BYTES = 3_000_000
MAX_ATTEMPTS = 2
MAX_TIMEOUT_SECONDS = 60
RETRY_DELAY_SECONDS = 0.1
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)
HEADERS = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}
DEFAULT_ENDPOINT = "https://api.lever.co/v0/postings"
ENDPOINT_HOSTS = {
    "api.lever.co": "jobs.lever.co",
    "api.eu.lever.co": "jobs.eu.lever.co",
}


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    """Enumerate the public board until Lever returns a terminal short page."""
    validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    maximum_results = int(target.max_jobs)
    maximum_pages = max(1, int(target.max_pages))
    endpoint = str(target.endpoint_url or f"{DEFAULT_ENDPOINT}/{target.slug}").rstrip("/")
    allowed_department_values = _clean(target.department_allowlist)
    allowed_departments = {value.casefold() for value in allowed_department_values}
    scan_metadata = SourceScanMetadata(stop_reason="not_started")
    publish_source_scan_metadata(session, scan_metadata)
    listing_budget = AdaptiveListingCompletion(maximum_pages, maximum_results)
    listing_budget.record(scan_metadata)
    listing_budget.record_terminal_probe_decision(
        scan_metadata,
        PAGE_SIZE,
        requests_completed=0,
        results_observed=0,
        deterministic_terminal_contract=True,
    )
    pagination: dict[str, Any] = {
        "endpoint_variant": target.endpoint_variant or ENDPOINT_VARIANT,
        "request_method": "GET",
        "server_result_limit": min(PAGE_SIZE, maximum_results),
        "pages_fetched": 0,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_results,
        "jobs_returned": 0,
        "jobs_normalized": 0,
        "zero_job_contract": False,
        "stop_reason": "not_started",
        "offsets": [],
        "diagnostics": [],
    }
    setattr(session, "lever_pagination", pagination)
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    offset = 0

    try:
        for _page_index in range(
            int(scan_metadata["effective_listing_request_ceiling"])
        ):
            effective_results = int(scan_metadata["effective_listing_job_ceiling"])
            remaining = effective_results - len(jobs)
            if remaining <= 0:
                _mark_safety_limit(scan_metadata, pagination, "maximum_results_reached")
                break
            normal_remaining = maximum_results - len(jobs)
            page_limit = min(
                PAGE_SIZE,
                normal_remaining if normal_remaining > 0 else remaining,
            )
            query: list[tuple[str, str]] = [
                ("mode", "json"),
                ("limit", str(page_limit)),
            ]
            if offset:
                query.append(("skip", str(offset)))
            # Lever applies repeated department parameters as an OR filter.
            query.extend(("department", value) for value in allowed_department_values)
            url = f"{endpoint}?{urlencode(query)}"
            scan_metadata["pages_requested"] += 1
            response = _get(session, url, timeout)
            payload = _payload(response, target)
            scan_metadata["pages_completed"] += 1
            pagination["pages_fetched"] += 1
            pagination["offsets"].append(offset)
            pagination["jobs_returned"] += len(payload)

            page_ids = tuple(_posting_id(item) for item in payload)
            if payload and page_ids in seen_page_signatures:
                scan_metadata["stop_reason"] = "repeated_page"
                scan_metadata["scan_completeness_reason"] = "pagination_failure"
                pagination["stop_reason"] = "repeated_page"
                raise AdapterError("Lever pagination repeated a previously returned page")
            if payload:
                seen_page_signatures.add(page_ids)

            for item in payload[:page_limit]:
                categories = item.get("categories", {}) or {}
                if not isinstance(categories, Mapping):
                    raise AdapterError(
                        "Lever malformed HTTP 200 response: posting categories are not an object"
                    )
                department = str(categories.get("department", "") or "").strip()
                if allowed_departments and department.casefold() not in allowed_departments:
                    continue
                job_id = _posting_id(item)
                title = str(item.get("text", "") or "").strip()
                if not ID_RE.fullmatch(job_id) or not title:
                    raise AdapterError(
                        "Lever posting has no stable job_id/source identifier or title"
                    )
                hosted_url = _official_url(
                    str(item.get("hostedUrl", "") or ""), target, job_id
                )
                folded_id = job_id.casefold()
                if folded_id in seen_ids:
                    pagination["diagnostics"].append(
                        f"Duplicate Lever posting ID: {job_id}"
                    )
                    if offset:
                        scan_metadata["stop_reason"] = "repeated_page"
                        scan_metadata["scan_completeness_reason"] = "pagination_failure"
                        pagination["stop_reason"] = "repeated_page"
                        raise AdapterError(
                            "Lever pagination returned a posting from an earlier page"
                        )
                    continue
                seen_ids.add(folded_id)
                location = categories.get("location", "") or ""
                jobs.append(
                    JobRecord(
                        job_id=job_id,
                        title=title,
                        company=target.name,
                        location=location,
                        url=hosted_url,
                        updated_at=str(item.get("createdAt", "")),
                        source=f"lever:{target.slug}",
                        departments=_clean([department, categories.get("team", "")]),
                        offices=[location] if location else [],
                        description=item.get("descriptionPlain") or item.get("description"),
                        raw_metadata={
                            "platform": "lever",
                            "source_posted_at": item.get("createdAt"),
                            "endpoint_variant": target.endpoint_variant or ENDPOINT_VARIANT,
                            "external_job_id": job_id,
                            "lever_department": department,
                        },
                    )
                )

            # Keep the durable scan metadata current so an independent caller
            # can report valid partial progress if it stops the next request.
            scan_metadata["jobs_normalized"] = len(jobs)
            pagination["jobs_normalized"] = len(jobs)

            if len(payload) > page_limit:
                _mark_safety_limit(scan_metadata, pagination, "maximum_results_reached")
                break
            offset += len(payload)
            if len(payload) < page_limit:
                scan_metadata.update(
                    scan_complete=True,
                    scan_completeness_reason="natural_terminal_page",
                    total_available=offset,
                    stop_reason="natural_terminal_page",
                )
                record_terminal_evidence(
                    scan_metadata,
                    "terminal_page",
                    "short_page",
                    has_more=False,
                    terminal_page_observed=True,
                )
                pagination["stop_reason"] = "natural_terminal_page"
                pagination["zero_job_contract"] = offset == 0
                break
            if len(jobs) >= effective_results:
                _mark_safety_limit(scan_metadata, pagination, "maximum_results_reached")
                break
        else:
            _mark_safety_limit(scan_metadata, pagination, "maximum_pages_reached")
    except requests.RequestException:
        scan_metadata["stop_reason"] = "request_failure"
        scan_metadata["scan_completeness_reason"] = "pagination_failure"
        pagination["stop_reason"] = "request_failure"
        raise
    except AdapterError:
        if scan_metadata["stop_reason"] == "not_started":
            scan_metadata["stop_reason"] = "pagination_failure"
            scan_metadata["scan_completeness_reason"] = "pagination_failure"
            pagination["stop_reason"] = "pagination_failure"
        raise

    if pagination["jobs_returned"] and not jobs and not allowed_departments:
        first = pagination["diagnostics"][0] if pagination["diagnostics"] else "unsupported schema"
        raise AdapterError(f"Lever page contained postings but none were valid: {first}")
    if not jobs and allowed_departments and scan_metadata["scan_complete"]:
        pagination["zero_job_contract"] = True
    pagination["jobs_normalized"] = len(jobs)
    return jobs


def _posting_id(item: Any) -> str:
    if not isinstance(item, Mapping):
        raise AdapterError("Lever malformed HTTP 200 response: posting is not an object")
    return str(item.get("id", "") or "").strip()


def _mark_safety_limit(
    scan_metadata: SourceScanMetadata,
    pagination: dict[str, Any],
    stop_reason: str,
) -> None:
    scan_metadata["stop_reason"] = stop_reason
    scan_metadata["scan_completeness_reason"] = "safety_limit_reached"
    scan_metadata["safety_limit_reached"] = True
    pagination["stop_reason"] = stop_reason


def validate_target(target: CompanyTarget) -> None:
    if not target.slug or not SLUG_RE.fullmatch(str(target.slug)):
        raise AdapterError("Lever target is missing a valid slug")
    if target.endpoint_variant and target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Lever unsupported variant: {target.endpoint_variant}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Lever result limit exceeds the hard maximum")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Lever max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Lever response byte limit exceeds the hard maximum")
    hosts = target.allowed_apply_hosts or ["jobs.lever.co"]
    if any(not HOST_RE.fullmatch(str(host)) for host in hosts):
        raise AdapterError("Lever allowed_apply_hosts contains an invalid host")
    if any(not str(value).strip() for value in target.department_allowlist):
        raise AdapterError("Lever department_allowlist contains an empty value")
    endpoint = urlparse(str(target.endpoint_url or f"{DEFAULT_ENDPOINT}/{target.slug}"))
    expected_path = f"/v0/postings/{target.slug}"
    expected_apply_host = ENDPOINT_HOSTS.get((endpoint.hostname or "").casefold())
    if (
        endpoint.scheme != "https"
        or expected_apply_host is None
        or endpoint.path.rstrip("/") != expected_path
        or endpoint.query
        or endpoint.fragment
        or endpoint.username
        or endpoint.password
    ):
        raise AdapterError("Lever endpoint_url must be an approved board-specific postings endpoint")
    if expected_apply_host not in {str(host).casefold() for host in hosts}:
        raise AdapterError("Lever API region does not match the allow-listed apply host")


def _get(session: requests.Session, url: str, timeout: int) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, headers=HEADERS, timeout=timeout)
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
            raise AdapterError(f"Lever authentication required (HTTP {status})")
        if status in {403, 451}:
            raise AdapterError(f"Lever blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Lever HTTP {status}")
        return response
    raise AdapterError("Lever request attempts exhausted")


def _payload(response: Any, target: CompanyTarget) -> list[Any]:
    content = getattr(response, "content", None)
    if content is not None and len(content) > int(target.max_response_bytes):
        raise AdapterError("Lever response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError("Lever malformed HTTP 200 response: expected JSON")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Lever malformed HTTP 200 response: invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise AdapterError("Lever malformed HTTP 200 response: expected a posting list")
    return payload


def _official_url(url: str, target: CompanyTarget, job_id: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    allowed = target.allowed_apply_hosts or ["jobs.lever.co"]
    segments = [value for value in parsed.path.split("/") if value]
    if parsed.scheme != "https" or not parsed.hostname:
        raise AdapterError("Lever posting has no valid apply URL")
    if not any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed):
        raise AdapterError("Lever posting apply host is not allow-listed")
    if (len(segments) < 2 or segments[0].casefold() != str(target.slug).casefold()
            or segments[1].casefold() != str(job_id).casefold()):
        raise AdapterError("Lever posting apply URL does not match its board and stable ID")
    return urlunparse(parsed._replace(fragment=""))


def _clean(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
