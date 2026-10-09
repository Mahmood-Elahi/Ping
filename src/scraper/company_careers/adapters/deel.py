"""Anonymous Deel ATS schema.org collection adapter."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.adapters import static_html
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


ENDPOINT_VARIANT = "schema_org_itemlist_jobposting_v1"
MAX_PAGES = 1
MAX_RESULTS = 20
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def validate_target(target: CompanyTarget) -> None:
    missing = [name for name, value in (
        ("listing_url", target.listing_url),
        ("tenant", target.tenant),
        ("identity_regex", target.identity_regex),
        ("allowed_apply_hosts", target.allowed_apply_hosts),
    ) if not value]
    if missing:
        raise AdapterError(f"Deel target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Deel unsupported variant: {target.endpoint_variant}")
    static_html._require_https(str(target.listing_url), "listing_url")
    static_html._safe_regex(str(target.identity_regex))
    static_html._validate_hosts(target.allowed_apply_hosts)
    parsed = urlparse(str(target.listing_url))
    if parsed.path.rstrip("/") != f"/{target.tenant}":
        raise AdapterError("Deel listing URL must contain the exact configured tenant")
    if not static_html._allowed_host((parsed.hostname or "").casefold(), target.allowed_apply_hosts):
        raise AdapterError("Deel listing host is not an allowed apply host")
    if int(target.max_pages) != MAX_PAGES:
        raise AdapterError("Deel collection supports exactly one bounded listing page")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Deel result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= static_html.MAX_RESPONSE_BYTES:
        raise AdapterError("Deel response byte limit exceeds the hard maximum")


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 60
) -> AdapterResult:
    """Resolve the public ItemList into bounded public JobPosting detail pages."""
    validate_target(target)
    timeout = min(max(1, int(timeout)), static_html.MAX_TIMEOUT_SECONDS)
    maximum_jobs = min(int(target.max_jobs), MAX_RESULTS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "GET listing + bounded GET details",
        "pages_fetched": 1,
        "detail_pages_fetched": 0,
        "jobs_available": 0,
        "jobs_normalized": 0,
        "maximum_pages": 1,
        "maximum_results": maximum_jobs,
        "stop_reason": "not_started",
        "zero_job_contract": False,
    })
    setattr(session, "deel_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    metadata["pages_requested"] = 1
    response = static_html._get(session, str(target.listing_url), timeout)
    content = static_html._response_bytes(response, target)
    root = static_html._parse_html(content, response)
    metadata["pages_completed"] = 1
    documents = _json_ld(root, content)
    organization = _one_type(documents, "Organization", "listing")
    organization_name = str(organization.get("name") or "").strip()
    if not static_html._safe_regex(str(target.identity_regex)).fullmatch(organization_name):
        raise AdapterError("Deel listing failed exact company attribution")
    canonical = str(organization.get("url") or "").rstrip("/")
    if canonical != str(target.listing_url).rstrip("/"):
        raise AdapterError("Deel organization URL does not match the configured tenant")
    metadata["source_identity_status"] = "validated"
    item_list = _one_type(documents, "ItemList", "listing")
    elements = item_list.get("itemListElement")
    if not isinstance(elements, list):
        raise AdapterError("Deel ItemList is malformed")
    metadata["jobs_available"] = len(elements)
    metadata["total_available"] = len(elements)
    if not elements:
        metadata["zero_job_contract"] = True
        metadata["stop_reason"] = "validated_zero_jobs"
        metadata["scan_complete"] = True
        metadata["scan_completeness_reason"] = "complete_item_list"
        return AdapterResult(
            jobs=[],
            scan=metadata,
            capabilities_used=frozenset({"inventory", "hydration", "per_item_failure"}),
        )

    detail_urls: list[tuple[str, str]] = []
    for index, element in enumerate(elements, start=1):
        if not isinstance(element, dict) or element.get("@type") != "ListItem":
            raise AdapterError("Deel ItemList contains a malformed item")
        if element.get("position") != index:
            raise AdapterError("Deel ItemList positions are not deterministic")
        url = str(element.get("url") or "")
        job_id = _detail_id(url, target)
        detail_urls.append((url, job_id))
    if len({job_id for _, job_id in detail_urls}) != len(detail_urls):
        raise AdapterError("Deel ItemList contains duplicate stable IDs")

    inventory_items = tuple(
        InventoryItem(
            f"deel:{target.tenant}:{job_id}",
            f"deel:{target.tenant}",
        )
        for _url, job_id in detail_urls
    )
    jobs: list[JobRecord] = []
    item_failures: list[ItemFailure] = []
    for url, job_id in detail_urls[:maximum_jobs]:
        inventory_id = f"deel:{target.tenant}:{job_id}"
        try:
            detail_response = static_html._get(session, url, timeout)
            detail_content = static_html._response_bytes(detail_response, target)
            detail_root = static_html._parse_html(detail_content, detail_response)
            posting = _one_type(
                _json_ld(detail_root, detail_content), "JobPosting", "detail"
            )
            jobs.append(_normalize(posting, target, organization_name, url, job_id))
            metadata["detail_pages_fetched"] += 1
        except requests.RequestException as exc:
            item_failures.append(
                ItemFailure(
                    inventory_id,
                    f"{exc.__class__.__name__}: {exc}",
                    retryable=True,
                )
            )
        except AdapterError as exc:
            reason = str(exc)
            retryable = bool(
                re.search(r"HTTP (408|425|429|500|502|503|504)\b", reason)
                or "attempts exhausted" in reason.casefold()
            )
            item_failures.append(
                ItemFailure(
                    inventory_id,
                    reason,
                    retryable=retryable,
                    outcome="unreadable" if retryable else "rejected",
                )
            )
    metadata["jobs_normalized"] = len(jobs)
    metadata["stop_reason"] = (
        "maximum_results_reached" if len(elements) > maximum_jobs else "collection_exhausted"
    )
    if len(elements) > maximum_jobs:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    else:
        metadata["scan_complete"] = True
        metadata["scan_completeness_reason"] = "complete_item_list"
    metadata.update(
        unreadable_items=sum(
            failure.outcome == "unreadable" for failure in item_failures
        ),
        rejected_items=sum(
            failure.outcome == "rejected" for failure in item_failures
        ),
    )
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=inventory_items,
        item_failures=tuple(item_failures),
        capabilities_used=frozenset(
            {"inventory", "hydration", "per_item_failure"}
        ),
    )


def _json_ld(root: Any, content: bytes) -> list[dict[str, Any]]:
    nodes = root.xpath("//script[@type='application/ld+json']")
    if not nodes:
        static_html._reject_non_listing(root, content, False)
    documents: list[dict[str, Any]] = []
    for node in nodes:
        try:
            value = json.loads(str(node.text or ""))
        except json.JSONDecodeError as exc:
            raise AdapterError(f"Deel returned malformed JSON-LD: {exc}") from exc
        if isinstance(value, dict):
            documents.append(value)
    return documents


def _one_type(documents: Iterable[dict[str, Any]], kind: str, page: str) -> dict[str, Any]:
    values = [value for value in documents if value.get("@type") == kind]
    if len(values) != 1:
        raise AdapterError(f"Deel {page} requires exactly one {kind} contract")
    return values[0]


def _detail_id(url: str, target: CompanyTarget) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    expected_host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    match = re.fullmatch(
        rf"/{re.escape(str(target.tenant))}/job-details/([^/]+)/overview/?", parsed.path
    )
    if parsed.scheme != "https" or host != expected_host or not match or not UUID_RE.fullmatch(match.group(1)):
        raise AdapterError("Deel ItemList contains a non-canonical detail URL")
    return match.group(1).casefold()


def _locations(posting: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for place in posting.get("jobLocation") or []:
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        if not isinstance(address, dict):
            continue
        parts = [
            str(address.get(field) or "").strip()
            for field in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
        ]
        value = ", ".join(part for part in parts if part)
        if value and value not in values:
            values.append(value)
    if posting.get("jobLocationType") == "TELECOMMUTE":
        for requirement in posting.get("applicantLocationRequirements") or []:
            if isinstance(requirement, dict) and requirement.get("name"):
                values.append(f"Remote - {str(requirement['name']).strip()}")
        if not values:
            values.append("Remote")
    return values


def _normalize(
    posting: dict[str, Any], target: CompanyTarget, organization_name: str, url: str, job_id: str
) -> JobRecord:
    identifier = posting.get("identifier") or {}
    hiring = posting.get("hiringOrganization") or {}
    title = str(posting.get("title") or "").strip()
    posting_url = str(posting.get("url") or "").rstrip("/")
    locations = _locations(posting)
    if not isinstance(identifier, dict) or str(identifier.get("value") or "").casefold() != job_id:
        raise AdapterError("Deel JobPosting identifier does not match its detail URL")
    if not isinstance(hiring, dict) or str(hiring.get("name") or "").strip() != organization_name:
        raise AdapterError("Deel JobPosting company does not match its exact tenant")
    if not static_html._safe_regex(str(target.identity_regex)).fullmatch(organization_name):
        raise AdapterError("Deel JobPosting failed exact company attribution")
    if posting_url != url.rstrip("/") or _detail_id(posting_url, target) != job_id:
        raise AdapterError("Deel JobPosting URL does not match its stable identifier")
    if posting.get("directApply") is not True:
        raise AdapterError("Deel JobPosting does not expose a direct public application URL")
    if not title or not locations:
        raise AdapterError("Deel JobPosting is missing title or location")
    date_posted = str(posting.get("datePosted") or "").strip()
    return JobRecord(
        job_id=f"deel:{target.tenant}:{job_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=posting_url,
        updated_at=date_posted[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", date_posted) else "",
        source=f"deel:{target.tenant}",
        offices=locations,
        description=str(posting.get("description") or "") or None,
        raw_metadata={
            "platform": "deel",
            "endpoint_variant": ENDPOINT_VARIANT,
            "external_job_id": job_id,
            "employment_type": posting.get("employmentType") or [],
        },
    )
