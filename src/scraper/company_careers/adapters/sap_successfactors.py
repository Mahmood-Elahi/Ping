"""SAP SuccessFactors Recruiting Marketing (RMK/Jobs2Web) adapter.

The supported contracts are the two public HTML layouts observed on the
configured company-owned career domains: a paginated table and a tile list
whose subsequent pages are returned as HTML fragments.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable, List, Mapping, Optional
from urllib.parse import urlencode, parse_qsl, quote, unquote, urljoin, urlparse, urlunparse

import requests

from src.scraper.company_careers.diagnostics import (
    bounded_scalar_sample,
    diagnostic_shape,
    structure_diagnostic,
)
from src.scraper.company_careers.models import (
    AdapterResult,
    AdapterError,
    CompanyTarget,
    InventoryItem,
    ItemFailure,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    finish_changed,
    finish_terminal,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)
from src.scraper.company_careers.student_roles import student_job_signals
from src.scraper.company_careers.canary_diagnostics import contract_error


PAGE_SIZE = 25
MAX_TILE_PAGE_SIZE = 100
MAX_PAGES = 20
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
PAGE_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
TABLE_VARIANT = "rmk_table"
TILE_VARIANT = "rmk_tile"
COLUMNIZED_VARIANT = "rmk_columnized"
SUPPORTED_VARIANTS = {TABLE_VARIANT, TILE_VARIANT, COLUMNIZED_VARIANT}
SEARCH_PATH_SUFFIX = "/search/"
TILE_ENDPOINT = "tile-search-results"
COLUMNIZED_ENDPOINT = "/services/recruiting/v1/jobs"
COLUMNIZED_PAGE_SIZE = 10
MAX_REQUEST_BUDGET = 5_000

# RMK's ``primLocation`` is a display projection, not an opaque location ID.
# Most values are comma-separated city/region/country displays, but tenants also
# legitimately publish country-only values. Keep that scalar exception bounded
# to known country labels/codes so identifiers and arbitrary single tokens do
# not become locations merely because they contain letters.
RMK_COUNTRY_ONLY_LOCATIONS = frozenset(
    value.casefold()
    for value in (
        "Argentina", "Australia", "Austria", "Belgium", "Brazil", "Canada",
        "Chile", "China", "Colombia", "Costa Rica", "Czech Republic", "Denmark",
        "Finland", "France", "Germany", "Hong Kong", "Hungary", "India",
        "Indonesia", "Ireland", "Israel", "Italy", "Japan", "Malaysia", "Mexico",
        "Netherlands", "New Zealand", "Norway", "Philippines", "Poland", "Portugal",
        "Romania", "Singapore", "South Africa", "South Korea", "Spain", "Sweden",
        "Switzerland", "Taiwan", "Thailand", "Turkey", "Ukraine", "United Arab Emirates",
        "United Kingdom", "United States", "United States of America", "Vietnam",
        "AR", "ARG", "AT", "AUT", "AU", "AUS", "BE", "BEL", "BR", "BRA",
        "CA", "CAN", "CH", "CHE", "CL", "CHL", "CN", "CHN", "CO", "COL",
        "CR", "CRI", "CZ", "CZE", "DE", "DEU", "DK", "DNK", "ES", "ESP",
        "FI", "FIN", "FR", "FRA", "GB", "GBR", "HK", "HKG", "HU", "HUN",
        "ID", "IDN", "IE", "IRL", "IL", "ISR", "IN", "IND", "IT", "ITA",
        "JP", "JPN", "KR", "KOR", "MY", "MYS", "MX", "MEX", "NL", "NLD",
        "NO", "NOR", "NZ", "NZL", "PH", "PHL", "PL", "POL", "PT", "PRT",
        "RO", "ROU", "SE", "SWE", "SG", "SGP", "TH", "THA", "TR", "TUR",
        "TW", "TWN", "UA", "UK", "UKR", "US", "USA", "VN", "VNM", "ZA", "ZAF",
    )
)
RMK_SUBNATIONAL_LOCATIONS = frozenset(
    value.casefold()
    for value in (
        "Alberta", "British Columbia", "Manitoba", "New Brunswick",
        "Newfoundland and Labrador", "Nova Scotia", "Ontario",
        "Prince Edward Island", "Quebec", "Saskatchewan", "Northwest Territories",
        "Nunavut", "Yukon", "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
        "ON", "PE", "QC", "SK", "YT",
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
        "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
        "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
        "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
        "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
        "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
        "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
        "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
        "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
        "WI", "WY", "DC",
    )
)
RMK_REGION_LABELS = frozenset(
    value.casefold()
    for value in (
        "Africa", "Americas", "Asia", "Asia Pacific", "Europe", "European Union",
        "Latin America", "Middle East", "North America", "South America",
        "APAC", "EMEA", "LATAM", "Global", "Worldwide", "Remote",
    )
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: Optional["_Node"] = None
    children: list["_Node"] = field(default_factory=list)
    content: list[Any] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return {value.casefold() for value in self.attrs.get("class", "").split()}


class _DomParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        node = _Node(
            tag.casefold(),
            {str(key).casefold(): str(value or "") for key, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)
        self.stack[-1].content.append(node)
        if tag.casefold() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == wanted:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].content.append(data)


class _RequestBudgetExceeded(AdapterError):
    """Raised before a request would exceed the source-wide hard limit."""


class _TransientSuccessFactorsError(AdapterError):
    """A transient upstream response eligible for bounded partial progress."""


@dataclass
class _RequestBudget:
    limit: int
    used: int = 0
    categories: dict[str, int] = field(default_factory=dict)

    def consume(self, category: str) -> None:
        if self.used >= self.limit:
            raise _RequestBudgetExceeded(
                "SAP SuccessFactors source request budget exhausted"
            )
        self.used += 1
        self.categories[category] = self.categories.get(category, 0) + 1

    def record(self, metadata: SourceScanMetadata) -> None:
        metadata["request_budget"] = self.limit
        metadata["requests_used"] = self.used
        metadata["request_counts"] = {
            name: self.categories.get(name, 0)
            for name in ("identity", "listing", "detail", "retry")
        }


@explicit_adapter_result
def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> AdapterResult:
    """Use recovery code only for exact reviewed source contracts."""
    from src.scraper.company_careers.adapters.successfactors_recovery_routing import mode
    if mode(target) == "baseline":
        from src.scraper.company_careers.adapters import sap_successfactors_baseline_84b1655 as baseline
        return baseline.fetch_jobs(session, target, timeout)
    return _fetch_reviewed_jobs(session, target, timeout)


@explicit_adapter_result
def _fetch_reviewed_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> AdapterResult:
    budget = _RequestBudget(int(target.protocol_request_budget))
    return fetch_with_mutation_restart(
        session, lambda: _fetch_jobs_once(session, target, timeout, budget)
    )


def _fetch_jobs_once(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
    budget: Optional[_RequestBudget] = None,
) -> AdapterResult:
    """Fetch a bounded, deterministically ordered public RMK job collection."""
    metadata = _new_scan_metadata(target)
    initialize_mutable_collection(metadata)
    publish_source_scan_metadata(session, metadata)
    budget = budget or _RequestBudget(int(target.protocol_request_budget))
    budget.record(metadata)
    # Keep the adapter-specific diagnostics contract used by registry validation.
    setattr(session, "sap_successfactors_pagination", metadata)
    try:
        validate_target(target)
        timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
        if target.endpoint_variant == COLUMNIZED_VARIANT:
            return _fetch_columnized_jobs(session, target, timeout, metadata, budget)
        return _fetch_html_jobs(session, target, timeout, metadata, budget)
    except requests.RequestException:
        metadata["stop_reason"] = "request_failure"
        metadata["scan_completeness_reason"] = "pagination_failure"
        raise
    except AdapterError as exc:
        _record_adapter_failure(metadata, exc)
        raise


def _new_scan_metadata(target: CompanyTarget) -> SourceScanMetadata:
    page_size = (
        COLUMNIZED_PAGE_SIZE
        if target.endpoint_variant == COLUMNIZED_VARIANT
        else PAGE_SIZE
    )
    maximum_results = (
        int(target.max_jobs)
        if target.endpoint_variant == COLUMNIZED_VARIANT
        else min(MAX_RESULTS, max(1, int(target.max_pages)) * page_size)
    )
    return SourceScanMetadata(
        identity_required=target.endpoint_variant == TABLE_VARIANT,
        endpoint_variant=target.endpoint_variant,
        request_method="POST" if target.endpoint_variant == COLUMNIZED_VARIANT else "GET",
        page_size=page_size,
        maximum_pages=max(1, int(target.max_pages)),
        maximum_results=maximum_results,
        pages_fetched=0,
        jobs_normalized=0,
        offsets=[],
        pages=[],
        diagnostics=[],
        detail_requests=0,
        detail_requests_skipped=0,
    )


def _record_adapter_failure(
    metadata: SourceScanMetadata, exc: AdapterError
) -> None:
    if metadata["stop_reason"] != "not_started":
        return
    message = str(exc).casefold()
    if "destination identity did not match" in message:
        metadata["source_identity_status"] = "failed"
        reason = "identity_failure"
    elif "total" in message or "result metadata" in message:
        reason = "malformed_total"
    elif "http " in message:
        reason = "http_error"
    elif any(
        marker in message
        for marker in ("malformed", "empty http", "missing searchresults", "was not html")
    ):
        reason = "malformed_response"
    elif metadata["pages_completed"]:
        reason = "pagination_failure"
    else:
        reason = "adapter_failure"
    metadata["stop_reason"] = reason
    metadata["scan_completeness_reason"] = reason


def _mark_safety_limit(metadata: SourceScanMetadata, stop_reason: str) -> None:
    metadata["safety_limit_reached"] = True
    if metadata.get("collection_changed_during_scan"):
        finish_changed(metadata, terminal_evidence=stop_reason)
    else:
        metadata["stop_reason"] = stop_reason
        metadata["scan_completeness_reason"] = "safety_limit_reached"


def _mark_complete(metadata: SourceScanMetadata, *, zero: bool = False) -> None:
    finish_terminal(
        metadata,
        complete_reason="reported_total_reached",
        stop_reason="validated_zero_jobs" if zero else "reported_total_reached",
    )


def _mark_columnized_terminal(
    metadata: SourceScanMetadata,
    *,
    actual_total: int,
    stop_reason: str,
) -> None:
    """Record stronger RMK terminal evidence when totalJobs is broader."""
    reported_total = metadata.get("total_available")
    metadata["reported_total"] = reported_total
    metadata["total_available"] = actual_total
    finish_terminal(
        metadata,
        complete_reason="natural_terminal_page",
        stop_reason=stop_reason,
    )
    metadata["diagnostics"].append(
        {
            "warning": "columnized totalJobs exceeded the locale-specific terminal collection",
            "reported_total": reported_total,
            "collected_total": actual_total,
            "terminal_evidence": stop_reason,
        }
    )


def _fetch_html_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
    metadata: SourceScanMetadata,
    budget: _RequestBudget,
) -> AdapterResult:
    max_pages = max(1, int(target.max_pages))
    if max_pages > MAX_PAGES:
        raise AdapterError(f"SAP SuccessFactors max_pages cannot exceed {MAX_PAGES}")
    listing_budget = AdaptiveListingCompletion(
        normal_request_budget=max_pages,
        normal_job_budget=int(metadata["maximum_results"]),
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    duplicate_source_id = False
    current_offset = 0
    next_request: Optional[tuple[str, Optional[dict[str, str]]]] = (
        str(target.search_url),
        None,
    )
    identity_status = "unproven"

    for page_index in range(listing_budget.hard_request_ceiling):
        effective_limit = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if next_request is None or len(jobs) >= effective_limit:
            break
        offset = current_offset
        url, params = next_request
        metadata["pages_requested"] += 1
        try:
            response = _get_page(
                session,
                target,
                url,
                params,
                offset,
                timeout,
                budget,
                "identity" if page_index == 0 else "listing",
            )
        except _RequestBudgetExceeded:
            metadata.update(
                scan_complete=False,
                scan_completeness_reason="successfactors_request_budget_exhausted",
                stop_reason="successfactors_request_budget_exhausted",
                safety_limit_reached=True,
            )
            budget.record(metadata)
            return _successfactors_result(session, metadata, jobs)
        except (requests.RequestException, _TransientSuccessFactorsError) as exc:
            if metadata["pages_completed"] and identity_status == "validated":
                metadata.update(
                    scan_complete=False,
                    scan_completeness_reason="inventory_incomplete",
                    stop_reason="request_failure",
                )
                metadata["diagnostics"].append(
                    {"offset": offset, "error": f"{type(exc).__name__}: {exc}"}
                )
                budget.record(metadata)
                return _successfactors_result(session, metadata, jobs)
            raise
        if page_index == 0:
            identity_status = _validate_html_identity(response, target, metadata)
        page = _parse_page(
            response,
            target,
            offset,
            expected_page_size=(metadata["page_size"] if page_index else None),
        )

        record_total_observation(
            metadata,
            page.total,
            offset=offset,
            source_label="SAP SuccessFactors",
        )
        if page_index == 0:
            metadata["page_size"] = page.page_size
            metadata["maximum_results"] = min(
                MAX_RESULTS,
                max_pages * page.page_size,
            )
            listing_budget = AdaptiveListingCompletion(
                normal_request_budget=max_pages,
                normal_job_budget=int(metadata["maximum_results"]),
                allow_listing_only_second_stage=True,
            )
            listing_budget.record(metadata)
        elif page.page_size != metadata["page_size"]:
            raise AdapterError("SAP SuccessFactors page size changed during pagination")
        listing_budget.record_decision(
            metadata,
            page.total,
            page.page_size,
            requests_completed=int(metadata["pages_requested"]),
            results_observed=offset + len(page.items),
        )
        effective_limit = int(metadata["effective_listing_job_ceiling"])

        source_ids = tuple(_source_item_identity(item) for item in page.items)
        if page.items and (not all(source_ids) or source_ids in seen_page_signatures):
            metadata["stop_reason"] = "repeated_page" if all(source_ids) else "missing_source_identity"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError(
                "SAP SuccessFactors pagination repeated a page"
                if all(source_ids)
                else "SAP SuccessFactors job row has no stable source identity"
            )
        if page.items:
            seen_page_signatures.add(source_ids)

        normalized_count = 0
        page_overlap_count = 0
        for item in page.items:
            source_id = _source_item_identity(item)
            if source_id in seen_source_ids:
                duplicate_source_id = True
                page_overlap_count += 1
                metadata["diagnostics"].append(
                    {"offset": offset, "error": f"Duplicate source job ID: {source_id}"}
                )
                continue
            seen_source_ids.add(source_id)
            try:
                job = _normalize_item(item, target)
            except AdapterError as exc:
                metadata["pagination_evidence_malformed"] = True
                metadata["stop_reason"] = "protocol_contract_invalid"
                metadata["scan_completeness_reason"] = "protocol_contract_invalid"
                metadata["diagnostics"].append(
                    {
                        "offset": offset,
                        "error": str(exc),
                        "item_shape": structure_diagnostic(item),
                    }
                )
                raise
            if job.job_id in seen_ids:
                metadata["diagnostics"].append(
                    {"offset": offset, "error": f"Duplicate stable job ID: {job.job_id}"}
                )
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
            normalized_count += 1
            if len(jobs) >= effective_limit:
                break

        if page.items and normalized_count == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("SAP SuccessFactors pagination returned no new stable jobs")
        if page_overlap_count:
            mark_collection_changed(
                metadata, offset=offset, overlap_count=page_overlap_count
            )

        metadata["pages"].append(
            {
                "offset": offset,
                "url": getattr(response, "url", url),
                "jobs_returned": len(page.items),
                "jobs_normalized": normalized_count,
            }
        )
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["pages_completed"] += 1
        metadata["offsets"].append(offset)
        metadata["jobs_normalized"] = len(jobs)

        effective_total = metadata["total_available"]
        if effective_total and offset < effective_total and not page.items:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="premature_terminal_page")
            else:
                metadata["stop_reason"] = "premature_terminal_page"
                metadata["scan_completeness_reason"] = "premature_terminal_page"
            next_request = None
        elif effective_total == 0:
            _mark_complete(metadata, zero=True)
            next_request = None
        elif effective_total is not None and offset + len(page.items) >= effective_total:
            if len(seen_source_ids) == effective_total:
                _mark_complete(metadata)
            else:
                finish_changed(metadata, terminal_evidence="reported_total_boundary")
            next_request = None
        elif len(page.items) < page.page_size:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="premature_terminal_page")
            else:
                metadata["stop_reason"] = "premature_terminal_page"
                metadata["scan_completeness_reason"] = "premature_terminal_page"
            next_request = None
        else:
            next_request = page.next_request
            if (
                next_request is None
                and target.endpoint_variant == TILE_VARIANT
                and params is not None
            ):
                next_params = dict(params)
                next_params["startrow"] = str(offset + page.page_size)
                next_request = (url, next_params)
            if next_request is None:
                if metadata["collection_changed_during_scan"]:
                    finish_changed(metadata, terminal_evidence="missing_next_page_contract")
                else:
                    metadata["stop_reason"] = "premature_terminal_page"
                    metadata["scan_completeness_reason"] = "premature_terminal_page"

        if next_request is not None and page_index + 1 >= int(
            metadata["effective_listing_request_ceiling"]
        ):
            _mark_safety_limit(metadata, "maximum_pages_reached")
            break
        if next_request is not None and page_index + 1 < listing_budget.hard_request_ceiling:
            current_offset = offset + page.page_size
            time.sleep(PAGE_DELAY_SECONDS)

    if (
        next_request is not None
        and metadata["pages_fetched"] >= listing_budget.hard_request_ceiling
    ):
        _mark_safety_limit(metadata, "maximum_pages_reached")
    elif metadata["stop_reason"] == "not_started" and len(jobs) >= MAX_RESULTS:
        _mark_safety_limit(metadata, "maximum_results_reached")
    if identity_status != "validated":
        metadata.update(
            scan_complete=False,
            scan_completeness_reason="identity_unproven",
            stop_reason="identity_unproven",
        )
    budget.record(metadata)
    return _successfactors_result(session, metadata, jobs)


def _validate_html_identity(
    response: Any, target: CompanyTarget, metadata: SourceScanMetadata
) -> str:
    marker = str(target.identity_regex or "").strip()
    if not marker:
        metadata.update(
            source_identity_status="unproven",
            source_identity_diagnostic=(
                "A responsive RMK host is not employer ownership; no reviewed "
                "identity_regex is configured for the recruiting surface"
            ),
        )
        return "unproven"
    try:
        matched = bool(re.search(marker, str(getattr(response, "text", "") or ""), re.I))
    except re.error as exc:
        raise AdapterError(f"SAP SuccessFactors identity_regex is invalid: {exc}") from exc
    if not matched:
        metadata.update(
            source_identity_status="failed",
            scan_completeness_reason="identity_failure",
            stop_reason="identity_failure",
        )
        raise contract_error(
            "SAP SuccessFactors recruiting surface identity did not match identity_regex",
            code="sf_employer_marker_mismatch",stage="employer_identity"
        )
    metadata.update(
        source_identity_status="validated",
        source_identity_diagnostic=(
            "The exact configured first-party RMK surface matched the reviewed "
            "employer identity marker"
        ),
    )
    return "validated"


def _successfactors_result(
    session: requests.Session,
    metadata: SourceScanMetadata,
    jobs: list[JobRecord],
    inventory_items: Optional[list[InventoryItem]] = None,
    item_failures: Optional[list[ItemFailure]] = None,
) -> AdapterResult:
    inventory = inventory_items or [
        InventoryItem(job.job_id, job.source) for job in jobs
    ]
    failures = item_failures or []
    publish_source_scan_metadata(session, metadata)
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=tuple(inventory),
        item_failures=tuple(failures),
        capabilities_used=frozenset(
            {"inventory_listing", "partial_progress", "per_item_failure"}
        ),
    )


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("careers_url", target.careers_url),
            ("search_url", target.search_url),
            ("endpoint_variant", target.endpoint_variant),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"SAP SuccessFactors target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(
            f"SAP SuccessFactors endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}"
        )
    if not 1 <= int(target.protocol_request_budget) <= MAX_REQUEST_BUDGET:
        raise AdapterError(
            "SAP SuccessFactors protocol_request_budget must be between 1 and "
            f"{MAX_REQUEST_BUDGET}"
        )

    search = urlparse(str(target.search_url))
    if (
        search.scheme != "https"
        or not search.hostname
        or not search.path.endswith(SEARCH_PATH_SUFFIX)
        or search.query
        or search.fragment
    ):
        raise AdapterError("SAP SuccessFactors search_url must be an observed HTTPS /search/ URL")
    careers = urlparse(str(target.careers_url))
    if careers.scheme != "https" or not careers.hostname:
        raise AdapterError("SAP SuccessFactors careers_url must be an official HTTPS URL")
    if target.endpoint_variant == COLUMNIZED_VARIANT:
        endpoint = urlparse(str(target.endpoint_url or ""))
        if (
            endpoint.scheme != "https"
            or (endpoint.hostname or "").casefold() != (search.hostname or "").casefold()
            or endpoint.path != COLUMNIZED_ENDPOINT
            or endpoint.query
            or endpoint.fragment
        ):
            raise AdapterError("SAP SuccessFactors columnized endpoint_url must be the matching public jobs service")
        if not str(target.identity_regex or "").strip():
            raise AdapterError("SAP SuccessFactors columnized target requires identity_regex")
        try:
            re.compile(str(target.identity_regex), re.I)
        except re.error as exc:
            raise AdapterError(f"SAP SuccessFactors identity_regex is invalid: {exc}") from exc
        locale = str(target.locale or "")
        if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", locale):
            raise AdapterError("SAP SuccessFactors columnized target requires an ll_CC locale")
        if int(target.page_size) != COLUMNIZED_PAGE_SIZE:
            raise AdapterError(f"SAP SuccessFactors columnized page_size must be {COLUMNIZED_PAGE_SIZE}")
        if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
            raise AdapterError(f"SAP SuccessFactors max_jobs must be between 1 and {MAX_RESULTS}")
        allowed = {str(value).strip().casefold().rstrip(".") for value in target.allowed_apply_hosts}
        if allowed != {(search.hostname or "").casefold()}:
            raise AdapterError("SAP SuccessFactors columnized allowed_apply_hosts must contain only the search host")


def _fetch_columnized_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
    metadata: SourceScanMetadata,
    budget: _RequestBudget,
) -> AdapterResult:
    """Use the public JSON service defined by the standard columnized RMK client."""
    identity_response = _get_page(
        session,
        target,
        str(target.search_url),
        None,
        0,
        timeout,
        budget,
        "identity",
    )
    identity_html = str(getattr(identity_response, "text", "") or "")
    if not re.search(str(target.identity_regex), identity_html, re.I):
        raise AdapterError("SAP SuccessFactors columnized tenant identity did not match identity_regex")
    if "BS3ColumnizedSearch" not in identity_html or "j2w.searchManager" not in identity_html:
        raise AdapterError("SAP SuccessFactors response is not the standard columnized RMK contract")
    tokens = set(re.findall(r'var\s+CSRFToken\s*=\s*"([^"]+)"', identity_html))
    if len(tokens) != 1 or not re.fullmatch(r"[0-9a-fA-F-]{36}", next(iter(tokens), "")):
        raise AdapterError("SAP SuccessFactors columnized page has no unambiguous CSRF token")
    token = next(iter(tokens))
    metadata.update(
        source_identity_status="validated",
        source_identity_diagnostic=(
            "The exact configured RMK host matched identity_regex, exposed the "
            "standard columnized client contract, and every normalized detail "
            "is revalidated against the same tenant identity"
        ),
    )
    search = urlparse(str(target.search_url))
    origin = f"{search.scheme}://{search.netloc}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": str(target.search_url),
        "User-Agent": HEADERS["User-Agent"],
        "X-CSRF-Token": token,
    }
    limit = int(target.max_jobs)
    max_pages = int(target.max_pages)
    jobs: list[JobRecord] = []
    inventory_items: list[InventoryItem] = []
    item_failures: list[ItemFailure] = []
    seen: set[str] = set()
    seen_fingerprints: dict[str, str] = {}
    duplicate_source_id = False
    seen_page_signatures: set[tuple[str, ...]] = set()
    previous_page_ids: tuple[str, ...] = ()

    for page_index in range(max_pages):
        if len(jobs) >= limit:
            _mark_safety_limit(metadata, "maximum_results_reached")
            break
        offset = page_index * COLUMNIZED_PAGE_SIZE
        # The columnized RMK client bootstraps with pageNumber=0.  Subsequent
        # requests use the one-based number shown by the paginator: the second
        # result page is 2, the third is 3, and so on.  Values 0 and 1 both
        # address the first result page, so a conventional zero-based sequence
        # repeats page one; using result offsets (0, 10, 20, ...) skips pages
        # and eventually produces the service's bounded error response.
        request_page_number = 0 if page_index == 0 else page_index + 1
        metadata["pages_requested"] += 1
        try:
            budget.consume("listing")
            response = session.post(
                str(target.endpoint_url),
                headers=headers,
                json={
                    "keywords": "",
                    "locale": str(target.locale),
                    "location": "",
                    "pageNumber": request_page_number,
                    "sortBy": "recent",
                },
                timeout=timeout,
            )
            payload = _columnized_json(
                response,
                target,
                offset=offset,
                page_number=request_page_number,
            )
        except _RequestBudgetExceeded:
            metadata.update(
                scan_complete=False,
                scan_completeness_reason="successfactors_request_budget_exhausted",
                stop_reason="successfactors_request_budget_exhausted",
                safety_limit_reached=True,
            )
            budget.record(metadata)
            return _successfactors_result(
                session, metadata, jobs, inventory_items, item_failures
            )
        except requests.RequestException as exc:
            metadata["diagnostics"].append(
                {
                    "offset": offset,
                    "page_number": request_page_number,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                }
            )
            metadata["stop_reason"] = "request_failure"
            metadata["scan_completeness_reason"] = "pagination_failure"
            if metadata["pages_completed"]:
                metadata["scan_completeness_reason"] = "inventory_incomplete"
                budget.record(metadata)
                return _successfactors_result(
                    session, metadata, jobs, inventory_items, item_failures
                )
            raise
        except _TransientSuccessFactorsError as exc:
            metadata["diagnostics"].append(
                {
                    "offset": offset,
                    "page_number": request_page_number,
                    "error": str(exc),
                }
            )
            metadata["stop_reason"] = "request_failure"
            metadata["scan_completeness_reason"] = (
                "inventory_incomplete" if metadata["pages_completed"] else "pagination_failure"
            )
            if metadata["pages_completed"]:
                budget.record(metadata)
                return _successfactors_result(
                    session, metadata, jobs, inventory_items, item_failures
                )
            raise
        except AdapterError as exc:
            metadata["diagnostics"].append(
                {
                    "offset": offset,
                    "page_number": request_page_number,
                    "error": str(exc),
                }
            )
            message = str(exc).casefold()
            if "response component totaljobs" in message:
                metadata["stop_reason"] = "malformed_total"
            elif "http " in message:
                metadata["stop_reason"] = "http_error"
            else:
                metadata["stop_reason"] = "malformed_response"
            metadata["scan_completeness_reason"] = metadata["stop_reason"]
            raise
        rows = payload["jobSearchResult"]
        total = payload["totalJobs"]
        record_total_observation(
            metadata,
            total,
            offset=offset,
            source_label="SAP SuccessFactors",
        )
        if (
            total < offset + len(rows)
            and not metadata["collection_changed_during_scan"]
        ):
            metadata["stop_reason"] = "malformed_total"
            metadata["scan_completeness_reason"] = "malformed_total"
            raise AdapterError("SAP SuccessFactors columnized page exceeded the reported total")
        page_ids = tuple(_columnized_row_identity(envelope) for envelope in rows)
        if rows and not all(page_ids):
            metadata["stop_reason"] = "pagination_failure"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("SAP SuccessFactors columnized result envelope is malformed")
        if rows and page_ids in seen_page_signatures:
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("SAP SuccessFactors columnized pagination repeated a complete page")
        if rows:
            seen_page_signatures.add(page_ids)

        page_new_ids = 0
        for envelope in rows:
            row = envelope.get("response") if isinstance(envelope, Mapping) else None
            if not isinstance(row, Mapping):
                raise AdapterError("SAP SuccessFactors columnized result envelope is malformed")
            external_id = str(row.get("id") or "").strip()
            if not re.fullmatch(r"[0-9]{1,18}", external_id):
                raise AdapterError("SAP SuccessFactors columnized job has no stable numeric ID")
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if external_id in seen:
                if seen_fingerprints.get(external_id) != fingerprint:
                    raise AdapterError(f"SAP SuccessFactors conflicting repeated stable job ID: {external_id}")
                duplicate_source_id = True
                metadata.setdefault("duplicate_ids_skipped", []).append(external_id)
                mark_collection_changed(metadata, offset=offset, overlap_count=1)
                continue
            title = _clean_text(row.get("unifiedStandardTitle"))
            url_title = unescape(str(row.get("unifiedUrlTitle") or "").strip())
            locales = row.get("supportedLocales")
            if (
                not title
                or not url_title
                or len(url_title) > 240
                or any(character in url_title for character in "/?#\\")
                or not isinstance(locales, list)
                or str(target.locale) not in locales
            ):
                raise AdapterError("SAP SuccessFactors columnized job is missing title, URL title, or locale")
            detail_url = (
                f"{origin}/job/{quote(unquote(url_title), safe='._~-')}/"
                f"{external_id}-{target.locale}"
            )
            item_id = f"sap_successfactors:{(search.hostname or '').casefold()}:{external_id}"
            inventory_items.append(
                InventoryItem(
                    item_id,
                    f"sap_successfactors:{(search.hostname or '').casefold()}",
                )
            )
            # The validated listing establishes existence independently of
            # detail hydration.  Register it before the detail request so a
            # transient detail failure cannot look like a repeated empty page.
            seen.add(external_id)
            seen_fingerprints[external_id] = fingerprint
            page_new_ids += 1
            try:
                listing_job = _normalize_columnized_listing(
                    row, target, external_id, title, detail_url
                )
                if (
                    target.selective_detail_enrichment
                    and not student_job_signals(listing_job)["matching_rule"]
                ):
                    job = listing_job
                    metadata["detail_requests_skipped"] += 1
                else:
                    detail_response = _get_page(
                        session,
                        target,
                        detail_url,
                        None,
                        offset,
                        timeout,
                        budget,
                        "detail",
                    )
                    metadata["detail_requests"] += 1
                    job = _normalize_columnized_detail(
                        row, detail_response, target, external_id, title, detail_url
                    )
            except _RequestBudgetExceeded:
                metadata.update(
                    scan_complete=False,
                    scan_completeness_reason="successfactors_request_budget_exhausted",
                    stop_reason="successfactors_request_budget_exhausted",
                    safety_limit_reached=True,
                )
                budget.record(metadata)
                return _successfactors_result(
                    session, metadata, jobs, inventory_items, item_failures
                )
            except (requests.RequestException, _TransientSuccessFactorsError) as exc:
                item_failures.append(
                    ItemFailure(
                        item_id,
                        f"{type(exc).__name__}: {exc}",
                        retryable=True,
                    )
                )
                continue
            except AdapterError as exc:
                metadata["diagnostics"].append(
                    {
                        "offset": offset,
                        "page_number": request_page_number,
                        "job_id": external_id,
                        "error": str(exc),
                    }
                )
                raise
            jobs.append(job)
            if len(jobs) >= limit:
                break
        if rows and page_new_ids == 0:
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("SAP SuccessFactors columnized pagination repeated a complete page")
        metadata["pages"].append(
            {
                "page_number": request_page_number,
                "offset": offset,
                "jobs_returned": len(rows),
            }
        )
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["pages_completed"] += 1
        metadata["offsets"].append(offset)
        metadata["jobs_normalized"] = len(jobs)
        previous_page_ids = page_ids
        if offset + len(rows) >= total:
            if len(seen) == total:
                _mark_complete(metadata, zero=total == 0)
            else:
                finish_changed(metadata, terminal_evidence="reported_total_boundary")
            break
        if len(rows) < COLUMNIZED_PAGE_SIZE:
            _mark_columnized_terminal(
                metadata,
                actual_total=len(seen),
                stop_reason="natural_terminal_page",
            )
            break
        if len(jobs) >= limit:
            _mark_safety_limit(metadata, "maximum_results_reached")
            break
    else:
        _mark_safety_limit(metadata, "maximum_pages_reached")
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    budget.record(metadata)
    return _successfactors_result(
        session, metadata, jobs, inventory_items, item_failures
    )


def _columnized_row_identity(envelope: Any) -> str:
    row = envelope.get("response") if isinstance(envelope, Mapping) else None
    if not isinstance(row, Mapping):
        return ""
    value = str(row.get("id") or "").strip()
    return value if re.fullmatch(r"[0-9]{1,18}", value) else ""


def _columnized_json(
    response: Any,
    target: CompanyTarget,
    *,
    offset: int,
    page_number: int,
) -> Mapping[str, Any]:
    request_label = f"offset={offset}, pageNumber={page_number}"
    if response.status_code != 200:
        if response.status_code in TRANSIENT_STATUSES:
            raise _TransientSuccessFactorsError(
                f"SAP SuccessFactors HTTP {response.status_code} for {target.name} ({request_label})"
            )
        raise AdapterError(
            f"SAP SuccessFactors HTTP {response.status_code} for {target.name} ({request_label})"
        )
    if len(bytes(getattr(response, "content", b"") or b"")) > int(target.max_response_bytes):
        raise AdapterError(
            "SAP SuccessFactors columnized response exceeded max_response_bytes "
            f"({request_label})"
        )
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError(
            f"SAP SuccessFactors columnized response was not JSON ({request_label})"
        )
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(
            f"SAP SuccessFactors columnized response was not valid JSON ({request_label})"
        ) from exc
    if not isinstance(payload, Mapping):
        raise AdapterError(
            "SAP SuccessFactors columnized response is missing the top-level object "
            f"({request_label}; observed={type(payload).__name__})"
        )
    rows = payload.get("jobSearchResult")
    if not isinstance(rows, list):
        components = ", ".join(sorted(str(key) for key in payload)[:12]) or "none"
        raise AdapterError(
            "SAP SuccessFactors columnized response component jobSearchResult is missing "
            f"or malformed ({request_label}; observed={type(rows).__name__}; "
            f"components={components})"
        )
    total = payload.get("totalJobs")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError(
            "SAP SuccessFactors columnized response component totalJobs must be a "
            f"non-negative integer ({request_label}; observed={type(total).__name__})"
        )
    if len(rows) > COLUMNIZED_PAGE_SIZE:
        raise AdapterError(
            "SAP SuccessFactors columnized page exceeded the observed page size "
            f"({request_label}; rows={len(rows)})"
        )
    return payload


def _normalize_columnized_detail(
    row: Mapping[str, Any],
    response: Any,
    target: CompanyTarget,
    external_id: str,
    title: str,
    detail_url: str,
) -> JobRecord:
    html = str(getattr(response, "text", "") or "")
    if not re.search(str(target.identity_regex), html, re.I):
        raise AdapterError("SAP SuccessFactors detail tenant identity did not match identity_regex")
    parser = _DomParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise AdapterError(f"SAP SuccessFactors detail returned malformed HTML: {exc}") from exc
    canonical_nodes = [
        node for node in _walk(parser.root)
        if node.tag == "link" and node.attrs.get("rel", "").casefold() == "canonical"
    ]
    if len(canonical_nodes) != 1:
        raise AdapterError("SAP SuccessFactors detail has no unambiguous canonical URL")
    canonical = urlparse(urljoin(detail_url, canonical_nodes[0].attrs.get("href", "")))
    expected = urlparse(detail_url)
    if (
        canonical.scheme != "https"
        or (canonical.hostname or "").casefold() != (expected.hostname or "").casefold()
        or not canonical.path.rstrip("/").endswith(f"/{external_id}-{target.locale}")
    ):
        raise AdapterError("SAP SuccessFactors detail canonical URL did not match the job ID")
    og_titles = [
        node.attrs.get("content", "") for node in _walk(parser.root)
        if node.tag == "meta" and node.attrs.get("property", "").casefold() == "og:title"
    ]
    if len(og_titles) != 1 or _clean_text(og_titles[0]) != title:
        raise AdapterError("SAP SuccessFactors detail title did not match the listing")
    apply_matches = re.findall(r"j2w\.Apply\.init\s*\(\s*\{[^}]*jobID\s*:\s*([0-9]+)", html, re.S)
    if apply_matches != [external_id]:
        raise AdapterError("SAP SuccessFactors detail has no official job-specific apply action")

    detail_fields: dict[str, list[str]] = {}
    for node in _walk(parser.root):
        if node.tag != "li":
            continue
        text = _node_text(node)
        match = re.match(r"^([A-Za-z#%][A-Za-z0-9 #&/%-]{1,40}):\s*(.+)$", text)
        if match:
            detail_fields.setdefault(match.group(1).casefold(), []).append(_clean_text(match.group(2)))
    req_ids = _exact_numeric_ids(
        detail_fields.get("req id", [])
        + detail_fields.get("requisition id", [])
    )
    if not req_ids:
        req_ids = _unique_strings(
            re.findall(r"\bRequisition\s+ID:\s*([0-9]{1,18})\b", _node_text(parser.root), re.I)
        )
    # Some standard RMK detail templates omit the human-readable Req ID field.
    # Canonical URL and j2w.Apply.jobID are already independently required above;
    # when a requisition field is present it remains an additional strict check.
    if req_ids and req_ids != [external_id]:
        raise AdapterError("SAP SuccessFactors detail requisition ID did not match the listing")
    api_locations: list[str] = []
    # These are all explicit RMK location projections.  Keep the concise
    # normalized field first, followed by the full job-location projection and
    # then the standard structured location field used as a displayed search
    # column by some tenants.
    for field_name in ("jobLocationShort", "jobLocation", "location"):
        api_locations = _columnized_locations(row.get(field_name), field_name)
        if api_locations:
            break
    # In the Unified Data Model row contract, primLocation is the scalar
    # primary-location display projection.  Accept it only when it has the
    # documented combined-location shape; a non-empty field name alone is not
    # enough to distinguish a display location from an internal identifier.
    primary_locations = (
        []
        if api_locations
        else _columnized_primary_location(
            row.get("primLocation"), region=row.get("region")
        )
    )
    # RMK also publishes Google Jobs microdata on the already-validated detail
    # page.  Some tenants, including the Seagate-style contract, omit every
    # named location projection from the search row but expose the same stable
    # value as an explicit ``meta[itemprop=jobLocation][content]`` field.
    structured_detail_locations = (
        [] if api_locations or primary_locations else _detail_job_locations(parser.root)
    )
    detail_locations = _unique_strings(detail_fields.get("location", []))
    locations = (
        api_locations
        or primary_locations
        or structured_detail_locations
        or detail_locations
    )
    if not locations:
        raise AdapterError(
            "SAP SuccessFactors columnized job is missing a stable location "
            f"({_columnized_location_diagnostic(row, detail_fields, parser.root)})"
        )
    departments = _unique_strings(detail_fields.get("department", []))
    description_nodes = [
        node for node in _walk(parser.root)
        if node.attrs.get("itemprop", "").casefold() == "description"
    ]
    description = _node_text(description_nodes[0]) if len(description_nodes) == 1 else None
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    posting_date = _normalize_posting_date(row.get("unifiedStandardStart"))
    return JobRecord(
        job_id=f"sap_successfactors:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=posting_date,
        source=f"sap_successfactors:{host}",
        departments=departments,
        offices=locations,
        description=description,
        raw_metadata={
            "platform": "sap_successfactors",
            "endpoint_variant": COLUMNIZED_VARIANT,
            "external_job_id": external_id,
            "detail_url": detail_url,
            "apply_url": detail_url,
            "fields": detail_fields,
        },
    )


def _columnized_locations(value: Any, field_name: str = "location") -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AdapterError(
            f"SAP SuccessFactors columnized location field {field_name} is malformed "
            f"(observed={type(value).__name__})"
        )
    locations: list[str] = []
    for item in value:
        if not isinstance(item, (str, Mapping)):
            raise AdapterError(
                f"SAP SuccessFactors columnized location field {field_name} contains "
                f"a malformed entry (observed={type(item).__name__})"
            )
        raw = item if isinstance(item, str) else item.get("value")
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise AdapterError(
                f"SAP SuccessFactors columnized location field {field_name} contains "
                f"a malformed value (observed={type(raw).__name__})"
            )
        text = _clean_text(_html_to_text(raw))
        if text:
            locations.append(text)
    return _unique_strings(locations)


def _columnized_primary_location(value: Any, *, region: Any = None) -> list[str]:
    """Validate RMK's scalar primary-location display projection."""
    reason = _primary_location_rejection(value, region=region)
    if reason is None:
        text = _clean_text(_html_to_text(value))
        components = [component.strip() for component in text.split(",") if component.strip()]
        if len(components) == 1 and not _recognized_standalone_location(text):
            region_text = _validated_primary_region(region)
            if region_text:
                text = f"{text}, {region_text}"
        return [text]
    if value is not None and not isinstance(value, str):
        raise AdapterError(
            "SAP SuccessFactors columnized location field primLocation is unusable "
            f"(observed={diagnostic_shape(value)}; reason={reason})"
        )
    return []


def _primary_location_rejection(value: Any, *, region: Any = None) -> Optional[str]:
    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "expected scalar string"
    text = _clean_text(_html_to_text(value))
    if not text:
        return "empty"
    if len(text) > 240:
        return "exceeds display length"
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        return "URL is not a display location"
    if text[0] in "[{" or text[-1] in "]}":
        return "serialized structure is not a display location"
    components = [component.strip() for component in text.split(",") if component.strip()]
    allowed_punctuation = frozenset(" .'-\u2019()/&")
    for component in components:
        if not any(character.isalpha() for character in component):
            return "display component has no letters"
        if any(
            not character.isalnum() and character not in allowed_punctuation
            for character in component
        ):
            return "display component has identifier punctuation"
    if len(components) > 1:
        return None
    if _recognized_standalone_location(text):
        return None
    if re.fullmatch(r"[A-Z0-9_-]{2,32}", text) and any(
        character.isalpha() for character in text
    ):
        return "opaque code-like token"
    # Multi-word localized labels are distinguishable from opaque IDs and are
    # a legitimate RMK display projection even when no country is appended.
    if len(re.findall(r"[^\W\d_]+", text, re.UNICODE)) >= 2:
        return None
    if _validated_primary_region(region):
        return None
    return "not a recognized country-only or combined display location"


def _recognized_standalone_location(value: str) -> bool:
    key = value.casefold()
    return key in (
        RMK_COUNTRY_ONLY_LOCATIONS | RMK_SUBNATIONAL_LOCATIONS | RMK_REGION_LABELS
    )


def _validated_primary_region(value: Any) -> str:
    """Return only an explicit, bounded geographic region display."""
    if not isinstance(value, str):
        return ""
    text = _clean_text(_html_to_text(value))
    if not text or len(text) > 80 or "," in text:
        return ""
    if _recognized_standalone_location(text):
        return text
    # Localized multi-word region labels are safe when the field itself is the
    # RMK ``region`` projection; single opaque tokens remain rejected.
    if (
        len(re.findall(r"[^\W\d_]+", text, re.UNICODE)) >= 2
        and not any(character in text for character in "{}[]:=")
    ):
        return text
    return ""


def _primary_location_scalar_diagnostic(value: Any) -> str:
    """Describe the requested RMK scalar characteristics without dumping it."""
    if not isinstance(value, str):
        return f"shape={diagnostic_shape(value)}"
    raw = value
    text = _clean_text(_html_to_text(value))
    upper_code_like = bool(
        re.fullmatch(r"[A-Z0-9_-]{2,32}", text) and any(char.isalpha() for char in text)
    )
    return (
        f"shape=str,length={len(raw)},comma_count={raw.count(',')},"
        f"all_upper_code_like={str(upper_code_like).lower()},"
        f"known_country={str(text.casefold() in RMK_COUNTRY_ONLY_LOCATIONS).lower()},"
        f"contains_delimiters={str(bool(re.search(r'[,|;/]', raw))).lower()},"
        f"sample=\"{bounded_scalar_sample(raw)}\""
    )


def _detail_job_locations(root: _Node) -> list[str]:
    """Read only SAP RMK's explicit JobPosting location microdata."""
    nodes = [
        node
        for node in _walk(root)
        if node.tag == "meta"
        and "joblocation"
        in {token.casefold() for token in node.attrs.get("itemprop", "").split()}
    ]
    if not nodes:
        return []
    locations: list[str] = []
    for node in nodes:
        raw = node.attrs.get("content")
        text = _clean_text(_html_to_text(raw)) if raw is not None else ""
        if not text:
            raise AdapterError(
                "SAP SuccessFactors detail structured jobLocation metadata is malformed"
            )
        locations.append(text)
    return _unique_strings(locations)


def _columnized_location_diagnostic(
    row: Mapping[str, Any],
    detail_fields: Mapping[str, list[str]],
    detail_root: _Node,
) -> str:
    """Describe bounded field identities and shapes, never whole job data."""
    shapes: list[str] = []
    for field_name in (
        "jobLocationShort",
        "jobLocation",
        "location",
        "primLocation",
        "city",
        "state",
        "country",
        "facility",
    ):
        if field_name not in row:
            continue
        value = row[field_name]
        shape = diagnostic_shape(value)
        if field_name == "primLocation":
            reason = _primary_location_rejection(value, region=row.get("region"))
            scalar = _primary_location_scalar_diagnostic(value)
            shape += (
                f"(usable; {scalar})"
                if reason is None
                else f"(rejected={reason}; {scalar})"
            )
        shapes.append(f"{field_name}={shape}")
    fields = ", ".join(shapes) if shapes else "none"
    row_shapes = ", ".join(
        f"{_diagnostic_token(key)}={diagnostic_shape(value)}"
        for key, value in sorted(row.items(), key=lambda item: str(item[0]).casefold())[:20]
    ) or "none"
    descriptors = ", ".join(_columnized_descriptors(row)) or "none"
    itemprops = _unique_strings(
        token.casefold()
        for node in _walk(detail_root)
        for token in node.attrs.get("itemprop", "").split()
        if token
    )[:12]
    return (
        f"location fields present: {fields}; "
        f"detail location entries={len(detail_fields.get('location', []))}; "
        f"row fields: {row_shapes}; descriptors: {descriptors}; "
        f"detail itemprops: {', '.join(itemprops) if itemprops else 'none'}"
    )


def _normalize_columnized_listing(
    row: Mapping[str, Any],
    target: CompanyTarget,
    external_id: str,
    title: str,
    detail_url: str,
) -> JobRecord:
    """Normalize listing metadata before deciding whether detail is useful."""
    locations: list[str] = []
    for field_name in ("jobLocationShort", "jobLocation", "location"):
        locations = _columnized_locations(row.get(field_name), field_name)
        if locations:
            break
    if not locations:
        locations = _columnized_primary_location(
            row.get("primLocation"), region=row.get("region")
        )
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"sap_successfactors:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=_normalize_posting_date(row.get("unifiedStandardStart")),
        source=f"sap_successfactors:{host}",
        offices=locations,
        raw_metadata={
            "platform": "sap_successfactors",
            "endpoint_variant": COLUMNIZED_VARIANT,
            "external_job_id": external_id,
            "detail_url": detail_url,
            "apply_url": detail_url,
            "listing": dict(row),
            "detail_enrichment": "not_requested",
        },
    )


def _columnized_descriptors(row: Mapping[str, Any]) -> list[str]:
    """Return bounded identifier/label pairs from shallow structured fields."""
    identifier_keys = ("fieldid", "fieldname", "columnid", "columnname", "key")
    label_keys = ("label", "fieldlabel", "columnlabel", "displaylabel")
    descriptors: list[str] = []
    candidates: list[Mapping[str, Any]] = []
    for value in row.values():
        if isinstance(value, Mapping):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, Mapping))
    for candidate in candidates[:24]:
        normalized = {str(key).casefold(): value for key, value in candidate.items()}
        identifier = next(
            (
                _diagnostic_token(normalized[key])
                for key in identifier_keys
                if isinstance(normalized.get(key), str) and normalized[key].strip()
            ),
            "",
        )
        label = next(
            (
                _diagnostic_label(normalized[key])
                for key in label_keys
                if isinstance(normalized.get(key), str) and normalized[key].strip()
            ),
            "",
        )
        if identifier or label:
            descriptors.append(
                "/".join(
                    part
                    for part in (
                        f"id={identifier}" if identifier else "",
                        f"label={label}" if label else "",
                    )
                    if part
                )
            )
    return _unique_strings(descriptors)[:12]


def _diagnostic_token(value: Any) -> str:
    token = re.sub(r"[^A-Za-z0-9_.:-]+", "?", str(value or "").strip())
    return token[:48] or "?"


def _diagnostic_label(value: Any) -> str:
    label = _clean_text(value)
    return re.sub(r"[^A-Za-z0-9 #&/().,_-]+", "?", label)[:60] or "?"


def _exact_numeric_ids(values: Iterable[Any]) -> list[str]:
    """Keep only complete RMK identifiers from possibly nested field wrappers.

    Some columnized detail templates nest the real field list inside another
    ``li``.  The outer node begins with ``Req ID`` but its text also contains
    every following field, while the leaf node contains the exact ID.  Treating
    the wrapper as a second identifier creates a false mismatch.  Requiring the
    entire extracted value to be one numeric ID preserves strict identity
    validation and ignores only presentation containers.
    """
    identifiers: list[str] = []
    for value in values:
        candidate = _clean_text(value)
        if re.fullmatch(r"[0-9]{1,18}", candidate):
            identifiers.append(candidate)
    return _unique_strings(identifiers)


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    url: str,
    params: Optional[dict[str, str]],
    offset: int,
    timeout: int,
    budget: _RequestBudget,
    category: str,
) -> Any:
    if target.endpoint_variant == TABLE_VARIANT:
        _validate_table_destination(url, str(target.search_url), offset, diagnostic_stage="request_destination")
    for attempt in range(MAX_ATTEMPTS):
        try:
            budget.consume(category if attempt == 0 else "retry")
            response = session.get(url, params=params, headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code != 200:
            if response.status_code in TRANSIENT_STATUSES:
                raise _TransientSuccessFactorsError(
                    f"SAP SuccessFactors HTTP {response.status_code} for "
                    f"{target.name} offset={offset}"
                )
            raise AdapterError(
                f"SAP SuccessFactors HTTP {response.status_code} for {target.name} offset={offset}"
            )
        if target.endpoint_variant == TABLE_VARIANT:
            _validate_table_destination(str(getattr(response, "url", None) or url),
                                        str(target.search_url), offset, diagnostic_stage="response_destination")
        content_type = str(getattr(response, "headers", {}).get("content-type", ""))
        if content_type and "html" not in content_type.casefold():
            raise AdapterError("SAP SuccessFactors response was not HTML")
        return response
    raise AdapterError(f"SAP SuccessFactors request attempts exhausted for {target.name}")


@dataclass
class _ParsedPage:
    items: list[dict[str, Any]]
    total: Optional[int]
    page_size: int
    next_request: Optional[tuple[str, Optional[dict[str, str]]]]


def _parse_page(
    response: Any,
    target: CompanyTarget,
    offset: int,
    expected_page_size: Optional[int] = None,
) -> _ParsedPage:
    text = str(getattr(response, "text", "") or "")
    if not text.strip():
        raise AdapterError("SAP SuccessFactors returned an empty HTTP 200 response")
    parser = _DomParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise AdapterError(f"SAP SuccessFactors returned malformed HTML: {exc}") from exc

    if target.endpoint_variant == TABLE_VARIANT:
        return _parse_table_page(parser.root, str(target.search_url), offset, expected_page_size)
    return _parse_tile_page(
        parser.root,
        text,
        str(target.search_url),
        offset,
        expected_page_size,
    )


def _parse_table_page(
    root: _Node,
    search_url: str,
    offset: int,
    expected_page_size: Optional[int],
) -> _ParsedPage:
    tables = [node for node in _walk(root) if node.attrs.get("id", "").casefold() == "searchresults"]
    explicit_zero = bool(re.search(r"\b0\s+most\s+recent\s+jobs\b", _node_text(root), re.I))
    if not tables:
        if explicit_zero:
            result_range = _table_result_range(root)
            if result_range != (0, 0, 0) or any("jobtitle-link" in n.classes for n in _walk(root)):
                raise AdapterError("SAP SuccessFactors zero-job response has contradictory result metadata")
            next_request = _table_next_request(root, search_url, offset, PAGE_SIZE, 0, 0)
            return _ParsedPage([], 0, PAGE_SIZE, next_request)
        raise AdapterError("SAP SuccessFactors table response is missing searchresults")

    if len(tables) != 1:
        raise AdapterError("SAP SuccessFactors table response has duplicate searchresults")
    table = tables[0]
    header_rows = [n for n in _walk(table) if n.tag == "tr" and any(c.tag == "th" for c in n.children)]
    if len(header_rows) != 1:
        raise AdapterError("SAP SuccessFactors table response has malformed header rows")
    header_cells = [c for c in header_rows[0].children if c.tag in {"td", "th"}]
    has_layout = bool(header_cells and _empty_table_layout_cell(header_cells[-1]))
    if any(c.tag != "th" for c in (header_cells[:-1] if has_layout else header_cells)):
        raise AdapterError("SAP SuccessFactors table response has malformed layout header")
    header_nodes = [node for node in _walk(table) if node.tag == "th"]
    header_ids = [node.attrs.get("id", "").casefold() for node in header_nodes]
    header_labels = [_clean_text(_node_text(node)) for node in header_nodes]
    if (
        not header_nodes
        or any(not value for value in header_ids)
        or len(header_ids) != len(set(header_ids))
        or any(not value for value in header_labels)
        or len({value.casefold() for value in header_labels}) != len(header_labels)
    ):
        raise AdapterError(
            "SAP SuccessFactors table response has malformed or duplicate column headers"
        )
    headers = dict(zip(header_ids, header_labels))
    row_nodes = [node for node in _walk(table) if "data-row" in node.classes]
    items = [_table_item(row, headers, has_layout=has_layout) for row in row_nodes]
    items = [item for item in items if item]
    result_range = _table_result_range(root)
    if result_range is None:
        raise AdapterError("SAP SuccessFactors table response is missing total-result metadata")
    result_start, result_end, total = result_range
    expected_start = 0 if total == 0 else offset + 1
    if (
        result_start != expected_start
        or result_end != offset + len(items)
        or result_end > total
        or (total > offset and not items)
    ):
        raise AdapterError("SAP SuccessFactors table response has impossible result metadata")
    page_size = expected_page_size or (
        PAGE_SIZE
        if total == 0
        else (len(items) if total <= len(items) else result_end - offset)
    )
    if page_size < 1 or page_size > PAGE_SIZE:
        raise AdapterError("SAP SuccessFactors table response has an unsupported page size")
    if total == 0 and items:
        raise AdapterError("SAP SuccessFactors zero-job response unexpectedly contained jobs")
    if len(items) > page_size:
        raise AdapterError("SAP SuccessFactors table page exceeded the observed page size")

    next_request = _table_next_request(root, search_url, offset, page_size, result_end, total)
    return _ParsedPage(items, total, page_size, next_request)


# Observed first-party rejected CURRENT-OFFSET anchors only. This does not
# authorize locale-bearing requests or a locale-specific inventory scope.
_REVIEWED_SELF_LOCALE_ANCHORS = {
    "https://careers.pcl.com/search/": frozenset({"en_US"}),
    "https://jobs.exxonmobil.com/search/": frozenset({"de_DE"}),
}


def _ignored_current_locale_anchor(node: _Node, candidate: str, search_url: str,
                                   current_offset: int, explicit_next: bool) -> bool:
    approved = _REVIEWED_SELF_LOCALE_ANCHORS.get(search_url)
    if not approved or explicit_next:
        return False
    # A language-like hint must never conceal an explicit continuation control.
    descendants = (node, *tuple(_walk(node)))
    # Indirect labels/event handlers are not evidenced language controls. Do
    # not let an ignored anchor conceal navigation expressed outside its text.
    if any(key in {"aria-labelledby", "aria-describedby"} or key.startswith("on")
           for child in descendants for key in child.attrs):
        return False
    labels = " ".join([_node_text(node)] + [
        child.attrs.get(key, "") for child in descendants
        for key in ("aria-label", "title", "alt", "class", "id", "rel")
    ])
    if re.search(r"next|[›»→▶⏭]", labels, re.I) or _node_text(node).strip() in {">", ">>"}:
        return False
    try:
        parts = urlparse(candidate)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        values = dict(pairs)
        if (len(values) != len(pairs) or values.get("locale") not in approved
                or values.get("startrow") != str(current_offset)):
            return False
        clean = urlunparse(parts._replace(query=urlencode([(k,v) for k,v in pairs if k != "locale"])))
        _validate_table_destination(clean, search_url, current_offset)
        return True
    except (ValueError, AdapterError):
        return False


def _table_next_request(root: _Node, search_url: str, offset: int, page_size: int,
                        result_end: int, total: int) -> Optional[tuple[str, None]]:
    next_request = None
    wanted_offset = offset + page_size
    for node in _walk(root):
        if node.tag != "a":
            continue
        explicit_next = ("next" in node.attrs.get("rel", "").casefold().split()
                         or _node_text(node).casefold() in {"next", "next page"}
                         or node.attrs.get("aria-label", "").casefold() in {"next", "next page"})
        if not node.attrs.get("href"):
            if explicit_next and node.attrs.get("aria-disabled") != "true":
                raise AdapterError("SAP SuccessFactors pagination has an unreviewed next-page continuation")
            continue
        candidate = urljoin(search_url, unescape(node.attrs["href"]))
        pairs = parse_qsl(urlparse(candidate).query, keep_blank_values=True)
        values = dict(pairs)
        if "startrow" not in values:
            if explicit_next:
                raise AdapterError("SAP SuccessFactors pagination has an unreviewed next-page continuation")
            continue
        if len(pairs) != len(values) or not re.fullmatch(r"0|[1-9][0-9]*", values["startrow"]):
            raise AdapterError("SAP SuccessFactors pagination has malformed or duplicate startrow")
        if _ignored_current_locale_anchor(node, candidate, search_url, offset, explicit_next):
            continue
        _validate_table_destination(candidate, search_url, int(values["startrow"]), diagnostic_stage="pagination_anchor")
        if result_end >= total and (int(values["startrow"]) > offset
                                    or explicit_next):
            raise AdapterError("SAP SuccessFactors terminal total contradicts next-page continuation")
        if values.get("startrow") == str(wanted_offset):
            _validate_table_destination(candidate, search_url, wanted_offset, diagnostic_stage="pagination_anchor")
            if next_request is not None and next_request[0] != candidate:
                raise AdapterError("SAP SuccessFactors pagination has conflicting next destinations")
            next_request = (candidate, None)
    return next_request


def _parse_tile_page(
    root: _Node,
    raw_html: str,
    search_url: str,
    offset: int,
    expected_page_size: Optional[int],
) -> _ParsedPage:
    tiles = [node for node in _walk(root) if node.tag == "li" and "job-tile" in node.classes]
    items = [_tile_item(tile) for tile in tiles]
    items = [item for item in items if item]

    if offset:
        if expected_page_size is None or len(items) > expected_page_size:
            raise AdapterError("SAP SuccessFactors tile fragment exceeded the observed page size")
        return _ParsedPage(items, None, expected_page_size, None)

    lists = [node for node in _walk(root) if node.attrs.get("id", "").casefold() == "job-tile-list"]
    total_match = re.search(r'jobRecordsFound\s*:\s*parseInt\("(\d+)"\)', raw_html)
    size_match = re.search(r'jobRecordsPerPage\s*:\s*parseInt\("(\d+)"\)', raw_html)
    if not lists or not total_match or not size_match:
        raise AdapterError("SAP SuccessFactors tile response is missing result metadata")
    returned_value = lists[0].attrs.get("data-record-returned", "")
    if not returned_value.isdigit() or int(returned_value) != len(items):
        raise AdapterError("SAP SuccessFactors tile response has impossible result metadata")
    page_size = int(size_match.group(1))
    if not 1 <= page_size <= MAX_TILE_PAGE_SIZE:
        raise AdapterError("SAP SuccessFactors tile response has an unsupported page size")
    if len(items) > page_size:
        raise AdapterError("SAP SuccessFactors tile page exceeded the observed page size")
    total = int(total_match.group(1))
    if total == 0 and items:
        raise AdapterError("SAP SuccessFactors zero-job response unexpectedly contained jobs")
    if total and not items:
        raise AdapterError("SAP SuccessFactors tile response reported jobs without job tiles")

    endpoint_match = re.search(r'apiEndpoint\s*:\s*"([^"]+)"', raw_html)
    query_match = re.search(r'searchQuery\s*:\s*"([^"]*)"', raw_html)
    next_request = None
    if total > len(items):
        if not endpoint_match or endpoint_match.group(1) != TILE_ENDPOINT or not query_match:
            raise AdapterError("SAP SuccessFactors tile response is missing pagination contract")
        query = unescape(query_match.group(1))
        parsed_query = urlparse(query)
        params = dict(parse_qsl(parsed_query.query, keep_blank_values=True))
        params["startrow"] = str(page_size)
        base = urlparse(search_url)
        endpoint_url = urlunparse(base._replace(path=f"/{TILE_ENDPOINT}/", query="", fragment=""))
        next_request = (endpoint_url, params)
    return _ParsedPage(items, total, page_size, next_request)


def _empty_table_layout_cell(cell: _Node) -> bool:
    """Only the observed trailing, empty hidden-phone TD is a layout cell."""
    return (cell.tag == "td" and cell.attrs == {"class": "hidden-phone"}
            and not cell.children and not _node_text(cell))


def _validate_table_destination(url: str, search_url: str, offset: int, *, diagnostic_stage: str = "destination_validation") -> None:
    search, destination = urlparse(search_url), urlparse(url)
    pairs = parse_qsl(destination.query, keep_blank_values=True)
    values = dict(pairs)
    if (destination.scheme != "https" or destination.hostname != search.hostname
            or destination.netloc != search.netloc or destination.path != search.path
            or destination.fragment or destination.username or destination.password
            or len(pairs) != len(values)
            or set(values) - {"startrow", "q", "locationsearch", "sortColumn", "sortDirection"}
            or values.get("q", "") or values.get("locationsearch", "")
            or values.get("startrow", str(offset)) != str(offset)
            or (offset and "startrow" not in values)):
        raise contract_error("SAP SuccessFactors table destination identity did not match reviewed search_url",
                             code="sf_table_destination_rejected",stage=diagnostic_stage,
                             url=url,reviewed_url=search_url,offset=offset)


def _table_item(row: _Node, headers: Mapping[str, str], *, has_layout: bool = False) -> Optional[dict[str, Any]]:
    link = _first_descendant(row, lambda node: node.tag == "a" and "jobtitle-link" in node.classes)
    if link is None:
        raise AdapterError("SAP SuccessFactors table row is missing its title link")
    cells = [cell for cell in row.children if cell.tag == "td"]
    if has_layout:
        if len(cells) != len(headers) + 1 or not _empty_table_layout_cell(cells[-1]):
            raise AdapterError("SAP SuccessFactors table row length does not match its schema: malformed layout cell")
        cells = cells[:-1]
    links = [n for n in _walk(row) if n.tag == "a" and "jobtitle-link" in n.classes]
    if any(n.attrs.get("href", "") != link.attrs.get("href", "") for n in links):
        raise contract_error("SAP SuccessFactors repeated stable title links did not match",
                             code="sf_title_link_disagreement",stage="record_identity")
    if len(cells) != len(headers):
        raise AdapterError("SAP SuccessFactors table row length does not match its schema")
    fields: dict[str, list[str]] = {}
    seen_headers: set[str] = set()
    for cell in cells:
        references = cell.attrs.get("headers", "").split()
        if len(references) != 1:
            raise AdapterError(
                "SAP SuccessFactors table cell has malformed column attribution"
            )
        header_id = references[0].casefold()
        label = headers.get(header_id, "")
        if not label or header_id in seen_headers:
            raise AdapterError(
                "SAP SuccessFactors table row has unknown or duplicate column attribution"
            )
        seen_headers.add(header_id)
        value = _node_text(cell)
        if label and value:
            fields.setdefault(label, []).append(value)
    return {"title": _node_text(link), "href": link.attrs.get("href", ""), "fields": fields}


def _tile_item(tile: _Node) -> Optional[dict[str, Any]]:
    link = _first_descendant(tile, lambda node: node.tag == "a" and "jobtitle-link" in node.classes)
    if link is None:
        return None
    fields: dict[str, list[str]] = {}
    for section in _walk(tile):
        if "section-field" not in section.classes:
            continue
        label_node = _first_descendant(section, lambda node: "section-label" in node.classes)
        label = _node_text(label_node) if label_node else _label_from_classes(section.classes)
        value_nodes = [
            node
            for node in _walk(section)
            if node.attrs.get("id", "").casefold().endswith("-value")
        ]
        value = _node_text(value_nodes[0]) if value_nodes else ""
        if label and value:
            fields.setdefault(label, []).append(value)
    return {"title": _node_text(link), "href": link.attrs.get("href", ""), "fields": fields}


def _normalize_item(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    title = _clean_text(item.get("title"))
    apply_url = _official_apply_url(str(target.search_url), item.get("href"))
    url_identifier = _identifier_from_url(apply_url)
    fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
    normalized_fields = {
        _clean_text(label): _unique_strings(values if isinstance(values, list) else [values])
        for label, values in fields.items()
    }
    external_id = _first_field(normalized_fields, ("requisition id", "job id")) or url_identifier
    if not title or not external_id:
        raise AdapterError("SAP SuccessFactors job is missing title or stable requisition ID")

    locations: list[str] = []
    departments: list[str] = []
    descriptions: list[str] = []
    posting_date = ""
    for label, values in normalized_fields.items():
        key = label.casefold()
        if "location" in key or key in {"city", "city/state", "address"}:
            for value in values:
                locations.extend(re.split(r"\s*[|;]\s*", value))
        if any(token in key for token in ("department", "category", "function", "functional area", "job family")):
            departments.extend(values)
        if "date" in key and not posting_date:
            posting_date = _normalize_posting_date(values[0])
        if "description" in key or "summary" in key:
            descriptions.extend(_html_to_text(value) for value in values)

    locations = _unique_strings(locations)
    departments = _unique_strings(departments)
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"sap_successfactors:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=apply_url,
        updated_at=posting_date,
        source=f"sap_successfactors:{host}",
        departments=departments,
        offices=locations,
        description="\n\n".join(value for value in descriptions if value) or None,
        raw_metadata={
            "platform": "sap_successfactors",
            "endpoint_variant": target.endpoint_variant,
            "external_job_id": external_id,
            "url_job_id": url_identifier,
            "fields": normalized_fields,
        },
    )


def _source_item_identity(item: Mapping[str, Any]) -> str:
    """Return the source-level row identity independently of normalization."""
    path = urlparse(unescape(str(item.get("href") or ""))).path
    match = re.search(r"/([0-9]+)/?$", path)
    if match:
        return match.group(1)
    fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
    for label, values in fields.items():
        if _clean_text(label).casefold() not in {"requisition id", "job id"}:
            continue
        candidates = values if isinstance(values, list) else [values]
        for value in candidates:
            candidate = _clean_text(value)
            if candidate and len(candidate) <= 80:
                return candidate.casefold()
    return ""


def _official_apply_url(search_url: str, href: Any) -> str:
    url = urljoin(search_url, str(href or "").strip())
    search = urlparse(search_url)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() != (search.hostname or "").casefold()
        or "/job/" not in parsed.path
        or not _identifier_from_url(url)
    ):
        raise contract_error("SAP SuccessFactors job has no official stable apply URL",
                             code="sf_apply_url_rejected",stage="apply_identity",url=url,reviewed_url=search_url)
    return urlunparse(parsed._replace(fragment=""))


def _identifier_from_url(url: str) -> str:
    match = re.search(r"/([0-9]+)/?$", urlparse(url).path)
    return match.group(1) if match else ""


def _table_result_range(root: _Node) -> Optional[tuple[int, int, int]]:
    ranges = []
    for node in _walk(root):
        if "paginationlabel" not in node.classes:
            continue
        match = re.search(
            r"Results\s+([0-9][0-9,]*)\s*[–-]\s*([0-9][0-9,]*)\s+of\s+([0-9][0-9,]*)",
            _node_text(node), re.I,
        )
        if not match:
            raise AdapterError("SAP SuccessFactors table response has malformed total-result metadata")
        ranges.append(tuple(int(value.replace(",", "")) for value in match.groups()))
    if len(set(ranges)) > 1:
        raise AdapterError("SAP SuccessFactors table response has contradictory total-result metadata")
    explicit_zero = bool(re.search(r"\b0\s+most\s+recent\s+jobs\b", _node_text(root), re.I))
    if ranges:
        if explicit_zero and ranges[0] != (0, 0, 0):
            raise AdapterError("SAP SuccessFactors table response has contradictory zero-result metadata")
        return ranges[0]
    return (0, 0, 0) if explicit_zero else None


def _first_field(fields: Mapping[str, list[str]], names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    for label, values in fields.items():
        if label.casefold() in wanted and values:
            return values[0]
    return ""


def _label_from_classes(classes: Iterable[str]) -> str:
    joined = " ".join(classes).casefold()
    for token, label in (
        ("location", "Location"),
        ("date", "Date"),
        ("department", "Department"),
        ("category", "Category"),
        ("function", "Function"),
        ("description", "Description"),
    ):
        if token in joined:
            return label
    return ""


def _normalize_posting_date(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return ""


def _html_to_text(value: Any) -> str:
    # Keep this aligned with the existing lightweight Atlassian HTML cleanup.
    text = unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:li|p|h[1-6]|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _walk(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        yield child
        yield from _walk(child)


def _node_text(node: Optional[_Node]) -> str:
    if node is None:
        return ""
    values = [
        _node_text(value) if isinstance(value, _Node) else str(value)
        for value in node.content
    ]
    return _clean_text(" ".join(values))


def _first_descendant(node: _Node, predicate: Any) -> Optional[_Node]:
    for candidate in _walk(node):
        if predicate(candidate):
            return candidate
    return None
