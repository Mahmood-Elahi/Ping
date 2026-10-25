"""Bounded adapter for SmartRecruiters' anonymous postings API v1."""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from html import unescape
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from src.scraper.company_careers.incremental import (
    IncrementalCapability,
    IncrementalPolicy,
)
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
from src.scraper.company_careers.listing_completion import (
    finish_authoritative_total,
    record_terminal_evidence,
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    finish_changed,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


API_HOST = "api.smartrecruiters.com"
APPLY_HOST = "jobs.smartrecruiters.com"
ENDPOINT_VARIANT = "companies_postings_v1"
# Batch 3 deterministic evidence includes the BoschGroup board at 4,835
# advertised postings with stable 100-row pages.  Keep the provider ceiling
# narrowly above that observed inventory: 50 pages / 5,000 rows.  Individual
# sources still opt in through their own max_pages/max_jobs configuration, so
# this does not expand any existing production crawl by itself.
MAX_PAGES = 50
MAX_PAGE_SIZE = 100
MAX_RESULTS = MAX_PAGES * MAX_PAGE_SIZE
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5_000_000
PAGE_DELAY_SECONDS = 0.2
MAX_HYDRATION_ATTEMPTS = 2
HYDRATION_RETRY_DELAY_SECONDS = 0.2
TRANSIENT_HYDRATION_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,99}$")
# SmartRecruiters' public v1 API currently returns both legacy shorter numeric
# IDs and newer longer numeric IDs.  The ID must still be an all-numeric,
# job-specific path component corroborated by the listing and detail records.
POSTING_ID_RE = re.compile(r"^[0-9]{6,18}$")
BLOCKED_MARKERS = (
    "captcha",
    "access denied",
    "verify you are human",
    "challenge-platform",
    "sign in to continue",
)
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}

# Provider-contract review: the anonymous postings endpoint exposes a
# release-time filter, but no trustworthy job-level material version, ordered
# update stream, opaque cursor, deletion feed, or conditional representation
# contract.  Keep the adapter full-crawl-only until a later reviewed strategy
# can satisfy the Phase 6 exact-equivalence rules.
INCREMENTAL_POLICY = IncrementalPolicy(
    capability=IncrementalCapability.NONE,
    strategy_version="smartrecruiters_v1",
)
TRUSTED_LISTING_VERSION_FIELD: None = None


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> AdapterResult:
    """Enumerate authoritative inventory, then optionally hydrate details."""
    validate_target(target)
    timeout = min(MAX_TIMEOUT_SECONDS, max(1, int(timeout)))
    return fetch_with_mutation_restart(
        session, lambda: _fetch_jobs_once(session, target, timeout)
    )


def _fetch_jobs_once(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> AdapterResult:
    """Traverse one SmartRecruiters snapshot without unioning mutable pages."""
    max_pages = max(1, int(target.max_pages))
    configured_page_size = max(1, int(target.page_size))
    page_size = (
        MAX_PAGE_SIZE if configured_page_size == 20 else configured_page_size
    )
    requested_limit = min(MAX_RESULTS, max(1, int(target.max_jobs)))
    endpoint = str(target.endpoint_url)
    identifier = str(target.slug)
    listings: list[tuple[str, Mapping[str, Any], str]] = []
    seen_ids: set[str] = set()
    observed_total: int | None = None

    metadata = SourceScanMetadata(
        endpoint_variant=ENDPOINT_VARIANT,
        request_method="GET",
        page_size=page_size,
        maximum_pages=max_pages,
        maximum_results=requested_limit,
        pages=[],
        jobs_normalized=0,
        detail_requests_coupled=False,
        inventory_hydration_separated=True,
    )
    setattr(session, "smartrecruiters_pagination", metadata)
    initialize_mutable_collection(metadata)
    publish_source_scan_metadata(session, metadata)

    try:
        for page_index in range(max_pages):
            if len(listings) >= requested_limit:
                _mark_safety_limit(metadata, "maximum_results_reached")
                break
            offset = page_index * page_size
            metadata["pages_requested"] += 1
            response = session.get(
                endpoint,
                params={"limit": page_size, "offset": offset},
                headers=HEADERS,
                timeout=timeout,
            )
            payload = _parse_response(response, f"postings offset={offset}")
            content, total = _parse_listing(payload, identifier, target, page_size, offset)
            if offset == 0 and len(content) == total:
                record_terminal_evidence(
                    metadata,
                    "complete_collection_response",
                    total,
                    has_more=False,
                    terminal_page_observed=True,
                    complete_collection_response=True,
                )
            metadata["pages_completed"] += 1
            total_changed = record_total_observation(
                metadata,
                total,
                offset=offset,
                source_label="SmartRecruiters",
            )
            if observed_total is None:
                observed_total = total
            if total_changed:
                finish_changed(
                    metadata, terminal_evidence="collection_changed_during_scan"
                )
                break

            # Preserve mutation restart above, then validate the entire raw
            # page before max_jobs can conceal contradictory accounting.
            if offset + len(content) > total:
                raise AdapterError("SmartRecruiters page exceeds its advertised total")

            inventory_on_page = 0
            page_ids: set[str] = set()
            page_overlap = 0
            for item in content:
                list_id = _posting_id(item.get("id"))
                if list_id in page_ids:
                    metadata["stop_reason"] = "repeated_job_id"
                    metadata["scan_completeness_reason"] = "pagination_failure"
                    raise AdapterError(
                        f"SmartRecruiters repeated stable job ID within one page: {list_id}"
                    )
                page_ids.add(list_id)
                if list_id in seen_ids:
                    page_overlap += 1
                    continue
                detail_ref = _detail_ref(item.get("ref"), identifier, list_id)
                seen_ids.add(list_id)
                listings.append((list_id, item, detail_ref))
                inventory_on_page += 1
                if len(listings) >= requested_limit:
                    break

            if page_overlap:
                mark_collection_changed(
                    metadata, offset=offset, overlap_count=page_overlap
                )

            metadata["pages"].append(
                {
                    "offset": offset,
                    "jobs_returned": len(content),
                    "inventory_items": inventory_on_page,
                    "reported_total": total,
                }
            )
            metadata["inventory_items_collected"] = len(listings)
            metadata["pages_fetched"] = len(metadata["pages"])

            if page_overlap:
                finish_changed(
                    metadata, terminal_evidence="collection_changed_during_scan"
                )
                break

            if len(listings) == total:
                finish_authoritative_total(metadata, total, len(listings))
                break
            if len(listings) >= requested_limit:
                _mark_safety_limit(metadata, "maximum_results_reached")
                break
            if page_index + 1 < max_pages:
                time.sleep(PAGE_DELAY_SECONDS)
        else:
            _mark_safety_limit(metadata, "maximum_pages_reached")
    except requests.RequestException:
        metadata["stop_reason"] = "request_failure"
        metadata["scan_completeness_reason"] = "pagination_failure"
        if not listings:
            raise
    except AdapterError:
        if metadata["stop_reason"] == "not_started":
            metadata["stop_reason"] = "adapter_failure"
            metadata["scan_completeness_reason"] = "adapter_failure"
        raise

    if listings:
        metadata.update(
            source_identity_status="validated",
            source_identity_diagnostic=(
                "Every listing row matched the configured company identifier/name, "
                "a stable posting ID, and the exact company-scoped detail endpoint"
            ),
        )
    elif metadata.get("scan_complete"):
        metadata.update(
            source_identity_status="validated",
            source_identity_diagnostic=(
                "The exact configured company endpoint returned a validated empty "
                "SmartRecruiters collection contract"
            ),
        )

    inventory_items = tuple(
        InventoryItem(item_id, f"smartrecruiters:{identifier.casefold()}")
        for item_id, _item, _detail_ref_value in listings
    )
    jobs: list[JobRecord] = []
    item_failures: list[ItemFailure] = []
    detail_requests = 0
    if target.selective_detail_enrichment and metadata.get("scan_complete"):
        for item_id, item, detail_ref in listings:
            job, failure, attempts = _hydrate_listing(
                session, item, target, identifier, item_id, detail_ref, timeout
            )
            detail_requests += attempts
            if job is not None:
                jobs.append(job)
            if failure is not None:
                item_failures.append(failure)
    else:
        for item_id, item, _detail_ref_value in listings:
            try:
                jobs.append(_normalize_listing(item, target, identifier, item_id))
            except AdapterError as exc:
                item_failures.append(
                    ItemFailure(
                        item_id=item_id,
                        reason=str(exc),
                        stage="normalization",
                        outcome="rejected",
                    )
                )
    metadata.update(
        jobs_normalized=len(jobs),
        detail_requests_planned=(
            len(listings) if target.selective_detail_enrichment else 0
        ),
        detail_requests=detail_requests,
        unreadable_items=sum(
            failure.outcome == "unreadable" for failure in item_failures
        ),
        rejected_items=sum(
            failure.outcome == "rejected" for failure in item_failures
        ),
    )
    publish_source_scan_metadata(session, metadata)
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=inventory_items,
        item_failures=tuple(item_failures),
        capabilities_used=frozenset(
            {"inventory", "partial_progress", "per_item_failure"}
            | ({"hydration"} if target.selective_detail_enrichment else set())
        ),
    )


def _hydrate_listing(
    session: requests.Session,
    listing: Mapping[str, Any],
    target: CompanyTarget,
    identifier: str,
    item_id: str,
    detail_ref: str,
    timeout: int,
) -> tuple[JobRecord | None, ItemFailure | None, int]:
    """Hydrate one listed item without converting detail failure into absence."""
    for attempt in range(MAX_HYDRATION_ATTEMPTS):
        try:
            response = session.get(detail_ref, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            if attempt + 1 < MAX_HYDRATION_ATTEMPTS:
                time.sleep(HYDRATION_RETRY_DELAY_SECONDS)
                continue
            return None, ItemFailure(
                item_id=item_id,
                reason=f"{exc.__class__.__name__}: {exc}",
                retryable=True,
            ), attempt + 1
        status = getattr(response, "status_code", None)
        if status in TRANSIENT_HYDRATION_STATUSES:
            if attempt + 1 < MAX_HYDRATION_ATTEMPTS:
                time.sleep(HYDRATION_RETRY_DELAY_SECONDS)
                continue
            return None, ItemFailure(
                item_id=item_id,
                reason=f"SmartRecruiters transient detail HTTP {status}",
                retryable=True,
            ), attempt + 1
        try:
            detail = _parse_response(response, f"posting detail {item_id}")
            return (
                _normalize(detail, listing, target, identifier, detail_ref),
                None,
                attempt + 1,
            )
        except AdapterError as exc:
            reason = str(exc)
            deterministic = any(
                marker in reason.casefold()
                for marker in (
                    "does not match",
                    "crossed company",
                    "mixed or ambiguous",
                    "official job-specific apply",
                    "reference changed",
                    "stable numeric id",
                )
            )
            return None, ItemFailure(
                item_id=item_id,
                reason=reason,
                stage="normalization" if deterministic else "hydration",
                retryable=False,
                outcome="rejected" if deterministic else "unreadable",
            ), attempt + 1
    raise AssertionError("bounded SmartRecruiters hydration loop exhausted")


def _normalize_listing(
    listing: Mapping[str, Any],
    target: CompanyTarget,
    identifier: str,
    job_id: str,
) -> JobRecord:
    """Normalize the fields guaranteed by the public inventory contract."""
    title = _clean(listing.get("name"))
    if not title:
        raise AdapterError("SmartRecruiters listing is missing its title")
    location = _location(listing.get("location"))
    department = _label(listing.get("department"))
    function = _label(listing.get("function"))
    apply_url = f"https://{APPLY_HOST}/{identifier}/{job_id}"
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=apply_url,
        updated_at=_iso_date(listing.get("releasedDate")),
        source=f"smartrecruiters:{identifier.casefold()}",
        departments=_unique([department, function]),
        offices=[location] if location else [],
        raw_metadata={
            "platform": "smartrecruiters",
            "endpoint_variant": ENDPOINT_VARIANT,
            "company_identifier": identifier,
            "company_name": _clean((listing.get("company") or {}).get("name")),
            "detail_url": apply_url,
            "apply_url": apply_url,
            "api_detail_url": _detail_ref(listing.get("ref"), identifier, job_id),
            "ref_number": _clean(listing.get("refNumber")),
            "hydration": "inventory_only",
        },
    )


def _mark_safety_limit(metadata: SourceScanMetadata, stop_reason: str) -> None:
    metadata["scan_complete"] = False
    metadata["scan_completeness_reason"] = "safety_limit_reached"
    metadata["stop_reason"] = stop_reason
    metadata["safety_limit_reached"] = True


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("slug", target.slug),
            ("careers_url", target.careers_url),
            ("endpoint_url", target.endpoint_url),
            ("endpoint_variant", target.endpoint_variant),
            ("identity_regex", target.identity_regex),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"SmartRecruiters target is missing {', '.join(missing)}")
    identifier = str(target.slug)
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise AdapterError("SmartRecruiters slug must be a public company identifier")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(
            f"SmartRecruiters endpoint_variant must be {ENDPOINT_VARIANT}"
        )
    try:
        re.compile(str(target.identity_regex), re.I)
    except re.error as exc:
        raise AdapterError(f"SmartRecruiters identity_regex is invalid: {exc}") from exc

    endpoint = urlparse(str(target.endpoint_url))
    expected_path = f"/v1/companies/{identifier}/postings"
    if (
        endpoint.scheme != "https"
        or (endpoint.hostname or "").casefold() != API_HOST
        or endpoint.path.casefold() != expected_path.casefold()
        or endpoint.params
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError(
            "SmartRecruiters endpoint_url must be the exact public company postings endpoint"
        )
    careers = urlparse(str(target.careers_url))
    if careers.scheme != "https" or not careers.hostname:
        raise AdapterError("SmartRecruiters careers_url must be an observed HTTPS URL")
    apply_hosts = _hosts(target.allowed_apply_hosts)
    if apply_hosts != {APPLY_HOST}:
        raise AdapterError(
            "SmartRecruiters allowed_apply_hosts must contain only jobs.smartrecruiters.com"
        )
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(
            f"SmartRecruiters page_size must be between 1 and {MAX_PAGE_SIZE}"
        )
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"SmartRecruiters max_pages cannot exceed {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"SmartRecruiters max_jobs cannot exceed {MAX_RESULTS}")


def _parse_response(response: Any, context: str) -> Mapping[str, Any]:
    status = getattr(response, "status_code", None)
    if status in {401, 407}:
        raise AdapterError(f"SmartRecruiters authentication required for {context}")
    if status in {403, 429, 451}:
        raise AdapterError(f"SmartRecruiters blocked response for {context} (HTTP {status})")
    if status != 200:
        raise AdapterError(f"SmartRecruiters HTTP {status} for {context}")
    content = getattr(response, "content", b"")
    if content and len(content) > MAX_RESPONSE_BYTES:
        raise AdapterError("SmartRecruiters response exceeded the byte limit")
    text = str(getattr(response, "text", "") or "")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if "html" in content_type.casefold() or text.lstrip().startswith("<"):
        lowered = text.casefold()
        if any(marker in lowered for marker in BLOCKED_MARKERS):
            raise AdapterError("SmartRecruiters returned a CAPTCHA, WAF, or login page")
        raise AdapterError("SmartRecruiters returned HTML instead of JSON")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"SmartRecruiters returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("SmartRecruiters response must be a JSON object")
    return payload


def _parse_listing(
    payload: Mapping[str, Any],
    identifier: str,
    target: CompanyTarget,
    limit: int,
    offset: int,
) -> tuple[list[Mapping[str, Any]], int]:
    content = payload.get("content")
    total = payload.get("totalFound")
    if payload.get("limit") != limit or payload.get("offset") != offset:
        raise AdapterError("SmartRecruiters response did not honor limit/offset")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("SmartRecruiters totalFound must be a non-negative integer")
    if not isinstance(content, list) or not all(isinstance(item, Mapping) for item in content):
        raise AdapterError("SmartRecruiters content must be a posting-object list")
    if len(content) > limit:
        raise AdapterError("SmartRecruiters page exceeded its requested limit")
    if total > offset and not content:
        raise AdapterError("SmartRecruiters response ended before totalFound")
    for item in content:
        _company_identity(item.get("company"), identifier, target)
    return list(content), total


def _normalize(
    detail: Mapping[str, Any],
    listing: Mapping[str, Any],
    target: CompanyTarget,
    identifier: str,
    detail_ref: str,
) -> JobRecord:
    job_id = _posting_id(detail.get("id"))
    if job_id != _posting_id(listing.get("id")):
        raise AdapterError("SmartRecruiters detail ID does not match its listing ID")
    _company_identity(detail.get("company"), identifier, target)
    title = _clean(detail.get("name"))
    list_title = _clean(listing.get("name"))
    if not title or title != list_title:
        raise AdapterError("SmartRecruiters detail title does not match its listing title")
    location = _location(detail.get("location"))
    if location != _location(listing.get("location")):
        raise AdapterError("SmartRecruiters detail location does not match its listing location")
    apply_url = _apply_url(detail.get("applyUrl"), identifier, job_id, target)
    detail_self = _detail_ref(detail.get("ref") or detail_ref, identifier, job_id)
    if detail_self != detail_ref:
        raise AdapterError("SmartRecruiters detail reference changed across requests")
    department = _label(detail.get("department"))
    function = _label(detail.get("function"))
    departments = _unique([department, function])
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=apply_url,
        updated_at=_iso_date(detail.get("releasedDate")),
        source=f"smartrecruiters:{identifier.casefold()}",
        departments=departments,
        offices=[location] if location else [],
        description=_description(detail.get("jobAd")) or None,
        raw_metadata={
            "platform": "smartrecruiters",
            "endpoint_variant": ENDPOINT_VARIANT,
            "company_identifier": identifier,
            "company_name": _clean((detail.get("company") or {}).get("name")),
            "detail_url": apply_url,
            "apply_url": apply_url,
            "api_detail_url": detail_ref,
            "ref_number": _clean(detail.get("refNumber")),
            **_optional_detail_metadata(detail),
        },
    )


def _optional_detail_metadata(detail: Mapping[str, Any]) -> dict[str, str]:
    """Retain provider content without changing normalized job/source identity."""
    result: dict[str, str] = {}
    for source, destination, field in (
        ("typeOfEmployment", "employment_type", "label"),
        ("experienceLevel", "experience_level", "label"),
        ("language", "language", "code"),
    ):
        value = detail.get(source)
        if isinstance(value, Mapping) and isinstance(value.get(field), str):
            text = _clean(value[field])
            if text:
                result[destination] = text
    location = detail.get("location")
    if isinstance(location, Mapping):
        remote = location.get("remote") is True
        hybrid = location.get("hybrid") is True
        if remote and not hybrid:
            result["job_location_type"] = "remote"
        elif hybrid and not remote:
            result["job_location_type"] = "hybrid"
    return result


def _company_identity(value: Any, identifier: str, target: CompanyTarget) -> None:
    if not isinstance(value, Mapping):
        raise AdapterError("SmartRecruiters posting is missing company identity")
    actual_identifier = _clean(value.get("identifier"))
    company_name = _clean(value.get("name"))
    if actual_identifier.casefold() != identifier.casefold():
        raise AdapterError("SmartRecruiters posting crossed company identifiers")
    if not company_name or not re.fullmatch(str(target.identity_regex), company_name, re.I):
        raise AdapterError("SmartRecruiters posting has mixed or ambiguous company identity")


def _posting_id(value: Any) -> str:
    job_id = _clean(value)
    if not POSTING_ID_RE.fullmatch(job_id):
        raise AdapterError("SmartRecruiters posting is missing a stable numeric ID")
    return job_id


def _detail_ref(value: Any, identifier: str, job_id: str) -> str:
    parsed = urlparse(_clean(value))
    expected_path = f"/v1/companies/{identifier}/postings/{job_id}"
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != API_HOST
        or parsed.path.casefold() != expected_path.casefold()
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AdapterError("SmartRecruiters posting has no stable API detail URL")
    return urlunparse(parsed)


def _apply_url(value: Any, identifier: str, job_id: str, target: CompanyTarget) -> str:
    parsed = urlparse(_clean(value))
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in _hosts(target.allowed_apply_hosts)
        or len(parts) != 2
        or parts[0].casefold() != identifier.casefold()
        or not (parts[1] == job_id or parts[1].startswith(job_id + "-"))
        or parsed.params
        or parsed.fragment
    ):
        raise AdapterError("SmartRecruiters posting has no official job-specific apply URL")
    return urlunparse(parsed)


def _location(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise AdapterError("SmartRecruiters posting is missing its location object")
    location = _clean(value.get("fullLocation"))
    if not location:
        location = ", ".join(
            part
            for part in (
                _clean(value.get("city")),
                _clean(value.get("region")),
                _clean(value.get("country")),
            )
            if part
        )
    if not location:
        raise AdapterError("SmartRecruiters posting is missing a stable location")
    return location


def _description(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    sections = value.get("sections")
    if not isinstance(sections, Mapping):
        return ""
    parts: list[str] = []
    for section in sections.values():
        if isinstance(section, Mapping) and section.get("text"):
            body = _html_text(section.get("text"))
            if body:
                parts.append(body)
    return "\n\n".join(part for part in parts if part)


def _html_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:li|p|h[1-6]|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _iso_date(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise AdapterError(f"SmartRecruiters releasedDate is not ISO formatted: {text}") from exc


def _label(value: Any) -> str:
    return _clean(value.get("label")) if isinstance(value, Mapping) else ""


def _hosts(values: list[str]) -> set[str]:
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value.casefold() not in {item.casefold() for item in result}:
            result.append(value)
    return result
