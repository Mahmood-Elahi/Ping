"""Anonymous public iCIMS career-search adapter.

Supported variants are limited to the observed Jibe JSON API, Radancy
server-rendered search pages, and the legacy iCIMS server-rendered portal.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Iterable, List, Mapping, Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
from lxml import html

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    html_page_has_more,
    record_terminal_evidence,
)
from src.scraper.company_careers.mutable_collection import (
    finish_changed,
    finish_terminal,
    immutable_job_fingerprint,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


JIBE_API = "icims_jibe_api"
RADANCY_HTML = "icims_radancy_html"
LEGACY_HTML = "icims_legacy_portal_html"
SUPPORTED_VARIANTS = {JIBE_API, RADANCY_HTML, LEGACY_HTML}
MAX_PAGES = 2
MAX_RESULTS = 20
MAX_PAGE_SIZE = 50
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
PAGE_DELAY_SECONDS = 0.2
MAX_REJECTED_RECORD_DIAGNOSTICS = 5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
HEADERS_JSON = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
HEADERS_HTML = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    validate_target(target)
    maximum_pages = max(1, int(target.max_pages))
    if maximum_pages > MAX_PAGES:
        raise AdapterError(f"iCIMS max_pages cannot exceed {MAX_PAGES}")
    maximum_jobs = max(1, int(target.max_jobs))
    if maximum_jobs > MAX_RESULTS:
        raise AdapterError(f"iCIMS max_jobs cannot exceed {MAX_RESULTS}")
    page_size = max(1, int(target.page_size))
    if page_size > MAX_PAGE_SIZE:
        raise AdapterError(f"iCIMS page_size cannot exceed {MAX_PAGE_SIZE}")
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    listing_budget = AdaptiveListingCompletion(
        normal_request_budget=maximum_pages,
        normal_job_budget=maximum_jobs,
        allow_listing_only_second_stage=True,
    )
    metadata = SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "page_size": page_size,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "total_available": None,
        "pages": [],
        "pages_fetched": 0,
        "jobs_normalized": 0,
        "stop_reason": "not_started",
        "repeated_page": False,
        "zero_job_contract": False,
        "rejected_record_count": 0,
        "rejected_record_ids": [],
        "rejected_record_reason": None,
        "rejected_record_diagnostics": [],
        "diagnostics": [],
        "warnings": [],
    })
    setattr(session, "icims_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget.record(metadata)
    initialize_mutable_collection(metadata)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_fingerprints: dict[str, tuple[Any, ...]] = {}
    seen_pages: set[tuple[str, ...]] = set()
    contiguous_nonempty_pages: list[int] = []
    for page_index in range(listing_budget.hard_request_ceiling):
        effective_jobs = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        params = _page_params(target, page_index)
        metadata["pages_requested"] += 1
        response = _get_page(session, target, params, timeout)
        items, total, zero_contract, has_more, listing_contract = _parse_page(
            response, target, page_index
        )
        metadata["pages_completed"] += 1
        record_total_observation(
            metadata,
            total,
            offset=page_index * page_size,
            source_label="iCIMS",
        )
        listing_budget.record_decision(
            metadata,
            total,
            page_size,
            requests_completed=int(metadata["pages_completed"]),
            results_observed=page_index * page_size + len(items),
        )
        if total is None and has_more is not None:
            record_terminal_evidence(
                metadata,
                "explicit_has_more",
                has_more,
                has_more=has_more,
                terminal_page_observed=not has_more,
            )
            listing_budget.record_terminal_probe_decision(
                metadata,
                max(1, len(items)),
                requests_completed=int(metadata["pages_completed"]),
                results_observed=len(jobs) + len(items),
                deterministic_terminal_contract=True,
            )
        effective_jobs = int(metadata["effective_listing_job_ceiling"])

        identities = tuple(
            sorted(_item_identity(item, target.endpoint_variant) for item in items)
        )
        page_record = {
            "page": page_index + 1,
            "params": params,
            "url": getattr(response, "url", _endpoint(target)),
            "jobs_returned": len(items),
            "jobs_normalized": 0,
        }
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])
        if items and identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("iCIMS pagination repeated a complete page")
        seen_pages.add(identities)

        parsed_on_page = 0
        new_ids = 0
        rejected_on_page = 0
        for item in items:
            try:
                job = _normalize_item(item, target)
            except AdapterError as exc:
                rejected_on_page += 1
                _record_rejected_item(metadata, item, target, page_index, exc)
                continue
            parsed_on_page += 1
            if job.job_id in seen_ids:
                if seen_fingerprints[job.job_id] != immutable_job_fingerprint(job):
                    metadata["stop_reason"] = "conflicting_repeated_job_id"
                    metadata["scan_completeness_reason"] = "pagination_failure"
                    raise AdapterError(
                        f"iCIMS conflicting repeated stable job ID: {job.job_id}"
                    )
                mark_collection_changed(
                    metadata, offset=page_index * page_size, overlap_count=1
                )
                continue
            seen_ids.add(job.job_id)
            seen_fingerprints[job.job_id] = immutable_job_fingerprint(job)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= effective_jobs:
                break
        page_record["jobs_normalized"] = new_ids
        page_record["jobs_rejected"] = rejected_on_page
        metadata["jobs_normalized"] = len(jobs)
        if items and parsed_on_page == 0:
            metadata["stop_reason"] = "all_records_rejected"
            metadata["scan_completeness_reason"] = "record_validation_failure"
            if target.endpoint_variant == JIBE_API and jobs:
                break
            raise AdapterError("iCIMS page contained jobs but none were valid")

        if page_index == 0 and not items:
            if not zero_contract:
                raise AdapterError("iCIMS HTTP 200 response did not prove an empty jobs collection")
            metadata["zero_job_contract"] = True
            finish_terminal(
                metadata,
                complete_reason="validated_zero_jobs",
                stop_reason="validated_zero_jobs",
            )
            break
        if not items:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="empty_terminal_page")
            elif total is not None and len(jobs) == total:
                finish_terminal(metadata, complete_reason="reported_total_reached")
            elif has_more is False:
                metadata["total_available"] = len(jobs)
                finish_terminal(
                    metadata,
                    complete_reason="explicit_next_page_exhausted",
                )
            elif _legacy_terminal_empty_page(
                response,
                target,
                page_index,
                has_more=has_more,
                listing_contract=listing_contract,
                contiguous_nonempty_pages=contiguous_nonempty_pages,
                rejected_record_count=int(metadata["rejected_record_count"]),
            ):
                metadata["total_available"] = len(jobs)
                finish_terminal(
                    metadata,
                    complete_reason="validated_terminal_empty_page",
                )
            else:
                metadata["stop_reason"] = "empty_page_without_terminal_contract"
                metadata["scan_completeness_reason"] = "pagination_failure"
            break
        if new_ids == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("iCIMS pagination returned no new stable job IDs")
        contiguous_nonempty_pages.append(page_index)
        if total is not None and len(jobs) == total:
            finish_terminal(metadata, complete_reason="reported_total_reached")
            break
        if has_more is False:
            metadata["total_available"] = len(jobs)
            finish_terminal(
                metadata,
                complete_reason="explicit_next_page_exhausted",
            )
            break
        if len(items) < page_size and has_more is None:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="short_terminal_page")
            else:
                metadata["stop_reason"] = "short_page_without_terminal_contract"
                metadata["scan_completeness_reason"] = "adapter_completeness_not_proven"
            break
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if page_index + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
        if page_index + 1 < listing_budget.hard_request_ceiling:
            time.sleep(PAGE_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["safety_limit_reached"] = True
        if metadata["collection_changed_during_scan"]:
            finish_changed(metadata, terminal_evidence=metadata["stop_reason"])
        else:
            metadata["scan_completeness_reason"] = "safety_limit_reached"
    if metadata["rejected_record_count"]:
        metadata["scan_complete"] = False
        metadata["scan_completeness_reason"] = "record_validation_failure"
        if metadata["stop_reason"] in {
            "reported_total_reached",
            "explicit_next_page_exhausted",
        }:
            metadata["stop_reason"] = "terminal_reached_with_rejected_records"
    return jobs


def _record_rejected_item(
    metadata: SourceScanMetadata,
    item: Any,
    target: CompanyTarget,
    page_index: int,
    error: AdapterError,
) -> None:
    """Retain bounded row diagnostics without treating rejection as exhaustion."""
    record_id = _item_identity(item, target.endpoint_variant) or "<unknown>"
    reason = str(error)
    metadata["rejected_record_count"] += 1
    if len(metadata["rejected_record_ids"]) < MAX_REJECTED_RECORD_DIAGNOSTICS:
        metadata["rejected_record_ids"].append(record_id)
    diagnostics = metadata["rejected_record_diagnostics"]
    if len(diagnostics) < MAX_REJECTED_RECORD_DIAGNOSTICS:
        diagnostic = {
            "page": page_index + 1,
            "record_id": record_id,
            "error": reason,
        }
        diagnostics.append(diagnostic)
        metadata["diagnostics"].append(diagnostic)
    existing = metadata.get("rejected_record_reason")
    if existing is None:
        metadata["rejected_record_reason"] = reason
    elif existing != reason:
        metadata["rejected_record_reason"] = "multiple_record_validation_errors"


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("endpoint_variant", target.endpoint_variant),
            ("tenant", target.tenant),
            ("board_id", target.board_id),
            ("allowed_apply_hosts", target.allowed_apply_hosts),
        )
        if not value
    ]
    if target.endpoint_variant == JIBE_API and not target.endpoint_url:
        missing.append("endpoint_url")
    if target.endpoint_variant in {RADANCY_HTML, LEGACY_HTML} and not target.search_url:
        missing.append("search_url")
    if missing:
        raise AdapterError(f"iCIMS target is missing {', '.join(dict.fromkeys(missing))}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"iCIMS endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}")
    parsed = urlparse(_endpoint(target))
    if parsed.scheme != "https" or not parsed.hostname or parsed.fragment:
        raise AdapterError("iCIMS endpoint must be an observed public HTTPS URL")
    if target.endpoint_variant == JIBE_API and parsed.path.rstrip("/") != "/api/jobs":
        raise AdapterError("iCIMS Jibe endpoint_url must use the public /api/jobs contract")
    if target.endpoint_variant == LEGACY_HTML and not parsed.path.rstrip("/").endswith("/jobs/search"):
        raise AdapterError("iCIMS legacy search_url must use the public /jobs/search contract")
    if any(token in parsed.path.casefold() for token in ("login", "signin", "session")):
        raise AdapterError("iCIMS endpoint cannot be an authentication or session URL")
    for value, label in ((target.tenant, "tenant"), (target.board_id, "board_id")):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(value)):
            raise AdapterError(f"iCIMS {label} is invalid")
    for host in target.allowed_apply_hosts:
        if not HOST_RE.fullmatch(str(host)):
            raise AdapterError("iCIMS allowed_apply_hosts contains an invalid host")
    if target.apply_url_id_query_param:
        if target.endpoint_variant != JIBE_API:
            raise AdapterError("iCIMS apply_url_id_query_param is supported only for Jibe feeds")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(target.apply_url_id_query_param)):
            raise AdapterError("iCIMS apply_url_id_query_param is invalid")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"iCIMS page_size must be between 1 and {MAX_PAGE_SIZE}")


def _endpoint(target: CompanyTarget) -> str:
    return str(target.endpoint_url if target.endpoint_variant == JIBE_API else target.search_url)


def _page_params(target: CompanyTarget, page_index: int) -> dict[str, str]:
    if target.endpoint_variant == JIBE_API:
        return {"page": str(page_index + 1)}
    if target.endpoint_variant == RADANCY_HTML:
        return {"p": str(page_index + 1)}
    return {
        "ss": "1",
        "in_iframe": "1",
        "searchRelation": "keyword_all",
        "pr": str(page_index),
    }


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    params: Mapping[str, str],
    timeout: int,
) -> Any:
    headers = HEADERS_JSON if target.endpoint_variant == JIBE_API else HEADERS_HTML
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(_endpoint(target), params=dict(params), headers=headers, timeout=timeout)
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
            raise AdapterError(f"iCIMS authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"iCIMS endpoint access was blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(f"iCIMS HTTP {status} for {target.name}")
        return response
    raise AdapterError(f"iCIMS request attempts exhausted for {target.name}")


def _parse_page(
    response: Any,
    target: CompanyTarget,
    page_index: int = 0,
) -> tuple[list[Any], Optional[int], bool, Optional[bool], bool]:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    text = str(getattr(response, "text", "") or "")
    if target.endpoint_variant == JIBE_API:
        _reject_bad_json_success(text, content_type)
        try:
            payload = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise AdapterError(f"iCIMS Jibe returned invalid JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise AdapterError("iCIMS Jibe response must be a JSON object")
        items = payload.get("jobs")
        total = payload.get("totalCount")
        count = payload.get("count")
        if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
            raise AdapterError("iCIMS Jibe jobs must be a list of objects")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise AdapterError("iCIMS Jibe totalCount must be a non-negative integer")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AdapterError("iCIMS Jibe count must be a non-negative integer")
        if total == 0 and items:
            raise AdapterError("iCIMS Jibe zero-job response unexpectedly contained jobs")
        return (
            items,
            total,
            total == 0 and count == 0 and not items,
            (page_index + 1) * int(target.page_size) < total,
            True,
        )

    _reject_bad_html_success(text, content_type)
    try:
        root = html.fromstring(text)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"iCIMS returned malformed HTML: {exc}") from exc
    if target.endpoint_variant == RADANCY_HTML:
        items = root.xpath(
            "//li[contains(@class, 'search-results-list__list-item') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' job-card ') "
            "or @data-intuit-jobid]"
        )
        items = _dedupe_items(
            [item for item in items if item.xpath(".//a[@data-job-id and contains(@href, '/job/')]")],
            target.endpoint_variant,
        )
        total = _integer_attr(root, "data-results-count")
        zero = total == 0 or _zero_text(root)
        has_more = (
            (page_index + 1) * int(target.page_size) < total
            if total is not None
            else html_page_has_more(root)
        )
        listing_contract = bool(
            items
            or root.xpath(
                "//*[contains(concat(' ', normalize-space(@class), ' '), "
                "' search-results-list ')]"
            )
        )
    else:
        items = root.xpath(
            "//li[contains(concat(' ', normalize-space(@class), ' '), ' iCIMS_JobCardItem ')]"
        )
        total = None
        zero = _zero_text(root) or bool(
            root.xpath("//*[contains(@class, 'iCIMS_JobSearchNoResults')]")
        )
        has_more = html_page_has_more(root)
        listing_contract = bool(
            items
            or root.xpath(
                "//*[contains(concat(' ', normalize-space(@class), ' '), "
                "' iCIMS_JobsList ') or contains(concat(' ', "
                "normalize-space(@class), ' '), ' iCIMS_JobsTable ') "
                "or contains(concat(' ', normalize-space(@class), ' '), "
                "' iCIMS_JobSearchResults ')]"
            )
        )
    return items, total, zero, has_more, listing_contract


def _legacy_terminal_empty_page(
    response: Any,
    target: CompanyTarget,
    page_index: int,
    *,
    has_more: Optional[bool],
    listing_contract: bool,
    contiguous_nonempty_pages: list[int],
    rejected_record_count: int,
) -> bool:
    """Accept only a correctly indexed empty legacy result collection as EOF."""
    if (
        target.endpoint_variant != LEGACY_HTML
        or page_index < 1
        or has_more is not None
        or not listing_contract
        or rejected_record_count
        or contiguous_nonempty_pages != list(range(page_index))
    ):
        return False
    query = parse_qs(urlparse(str(getattr(response, "url", "") or "")).query)
    requested_pages = query.get("pr")
    return requested_pages == [str(page_index)]


def _reject_bad_json_success(text: str, content_type: str) -> None:
    lowered = text.casefold()
    if "html" in content_type.casefold() or text.lstrip().startswith("<"):
        _reject_bad_html_success(text, content_type)
        raise AdapterError("iCIMS Jibe returned HTML instead of its JSON jobs response")
    if any(token in lowered for token in ("captcha", "access denied", "request blocked")):
        raise AdapterError("iCIMS returned a blocked response")


def _reject_bad_html_success(text: str, content_type: str) -> None:
    lowered = text.casefold()
    if not text.strip():
        raise AdapterError("iCIMS returned an empty HTTP 200 response")
    if "html" not in content_type.casefold() and not text.lstrip().startswith("<"):
        raise AdapterError("iCIMS returned a non-HTML response")
    if any(token in lowered for token in ("captcha", "access denied", "request blocked", "cloudflare challenge")):
        raise AdapterError("iCIMS returned a blocked page")
    if any(token in lowered for token in ("enable javascript to continue", "javascript is required")):
        raise AdapterError("iCIMS browser required by successful HTML response")
    if "session required" in lowered or "session has expired" in lowered:
        raise AdapterError("iCIMS session required by successful HTML response")
    if re.search(r"<title[^>]*>\s*(?:sign in|log in|login)", lowered):
        raise AdapterError("iCIMS authentication required by successful HTML response")


def _normalize_item(item: Any, target: CompanyTarget) -> JobRecord:
    if target.endpoint_variant == JIBE_API:
        return _normalize_jibe(item, target)
    if target.endpoint_variant == RADANCY_HTML:
        return _normalize_radancy(item, target)
    return _normalize_legacy(item, target)


def _normalize_jibe(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    data = item.get("data")
    if not isinstance(data, Mapping):
        raise AdapterError("iCIMS Jibe job must contain a data object")
    client_code = _clean(data.get("client_code"))
    if client_code and client_code.casefold() != str(target.tenant).casefold():
        raise AdapterError("iCIMS Jibe client_code does not match the configured tenant")
    external_id = _clean(data.get("slug")) or _clean(data.get("req_id"))
    title = _clean(data.get("title"))
    if not external_id or not title:
        raise AdapterError("iCIMS Jibe job is missing title or stable identifier")
    url, url_id = _official_url(data.get("apply_url"), target)
    if url_id and url_id != external_id:
        raise AdapterError("iCIMS Jibe apply URL identifier does not match the job")
    locations = _jibe_locations(data)
    work_modes = _jibe_work_modes(data, locations)
    display_locations = list(locations)
    for mode in work_modes:
        if not any(mode.casefold() in value.casefold() for value in display_locations):
            display_locations.append(mode)
    categories = _category_values(data.get("categories"))
    categories.extend(_strings(data.get("category")))
    department = _clean(data.get("department"))
    if department:
        categories.append(department)
    host = (urlparse(_endpoint(target)).hostname or "").casefold()
    return JobRecord(
        job_id=f"icims:{target.tenant}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(_unique(display_locations)),
        url=url,
        updated_at=_normalize_date(data.get("posted_date")),
        source=f"icims:{host}:{target.board_id}",
        departments=_unique(categories),
        offices=_unique(locations),
        description=_clean(data.get("description")) or None,
        raw_metadata={
            "platform": "icims",
            "endpoint_variant": target.endpoint_variant,
            "tenant": target.tenant,
            "board_id": target.board_id,
            "external_job_id": external_id,
            "work_modes": work_modes,
            "job": dict(data),
        },
    )


def _normalize_radancy(item: Any, target: CompanyTarget) -> JobRecord:
    links = item.xpath(".//a[@data-job-id and contains(@href, '/job/')]")
    if not links:
        raise AdapterError("iCIMS Radancy job is missing its detail link")
    link = links[0]
    url, url_id = _official_url(link.get("href"), target)
    external_id = _clean(link.get("data-job-id")) or url_id
    title_nodes = link.xpath(".//h2")
    title = _text(title_nodes[0]) if title_nodes else _text(link).split(" Save job")[0]
    locations = _texts_by_class(link, ("job-location", "location"))
    categories = _texts_by_class(link, ("category",))
    posted_values = _texts_by_class(link, ("job-date-posted",))
    posted = _normalize_date(re.sub(r"(?i)^posted\s*:?[ ]*", "", posted_values[0])) if posted_values else ""
    return _html_job(target, external_id, title, url, locations, posted, categories, item)


def _normalize_legacy(item: Any, target: CompanyTarget) -> JobRecord:
    links = item.xpath(".//a[contains(@href, '/jobs/') and contains(@href, '/job')]")
    if not links:
        raise AdapterError("iCIMS legacy job is missing its detail link")
    link = links[0]
    url, url_id = _official_url(link.get("href"), target)
    fields: dict[str, str] = {}
    for group in item.xpath(".//*[contains(@class, 'iCIMS_JobHeaderTag')]"):
        labels = group.xpath(".//dt")
        values = group.xpath(".//dd")
        if labels and values:
            fields[_text(labels[0]).casefold()] = _text(values[0])
    external_id = fields.get("id") or url_id
    title_nodes = link.xpath(".//h3")
    title = _text(title_nodes[0]) if title_nodes else _text(link)
    location_nodes = item.xpath(".//*[contains(@class, 'header') and contains(@class, 'left')]")
    locations = [re.sub(r"^\s*\|\s*", "", _text(node)) for node in location_nodes if _text(node)]
    categories = [fields["category"]] if fields.get("category") else []
    return _html_job(target, external_id, title, url, locations, "", categories, item)


def _html_job(
    target: CompanyTarget,
    external_id: str,
    title: str,
    url: str,
    locations: Iterable[str],
    posted: str,
    categories: Iterable[str],
    item: Any,
) -> JobRecord:
    if not external_id or not title:
        raise AdapterError("iCIMS job is missing title or stable identifier")
    locations = _unique(locations)
    work_modes = [value for value in locations if re.search(r"(?i)remote|hybrid", value)]
    host = (urlparse(_endpoint(target)).hostname or "").casefold()
    return JobRecord(
        job_id=f"icims:{target.tenant}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=posted,
        source=f"icims:{host}:{target.board_id}",
        departments=_unique(categories),
        offices=locations,
        raw_metadata={
            "platform": "icims",
            "endpoint_variant": target.endpoint_variant,
            "tenant": target.tenant,
            "board_id": target.board_id,
            "external_job_id": external_id,
            "work_modes": work_modes,
            "listing_html": html.tostring(item, encoding="unicode")[:10000],
        },
    )


def _official_url(value: Any, target: CompanyTarget) -> tuple[str, str]:
    url = urljoin(_endpoint(target), str(value or "").strip())
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not _allowed_host(host, target.allowed_apply_hosts):
        raise AdapterError("iCIMS job has no official allowed-host apply URL")
    match = re.search(r"/jobs/(\d+)(?:/|$)", parsed.path)
    if not match and target.endpoint_variant == RADANCY_HTML:
        matches = re.findall(r"/(\d+)(?:/)?$", parsed.path)
        identifier = matches[-1] if matches else ""
    else:
        identifier = match.group(1) if match else ""
    if not identifier and target.apply_url_id_query_param:
        values = parse_qs(parsed.query, keep_blank_values=True).get(
            str(target.apply_url_id_query_param), []
        )
        identifier = _clean(values[0]) if len(values) == 1 else ""
        if identifier and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identifier):
            raise AdapterError("iCIMS apply URL query identifier is invalid")
    if not identifier:
        raise AdapterError("iCIMS apply URL has no stable job identifier")
    return urlunparse(parsed._replace(fragment="")), identifier


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _jibe_locations(data: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    full = _clean(data.get("full_location"))
    if full:
        values.extend(part.strip() for part in full.split(";") if part.strip())
    if not values:
        values.extend(_strings(data.get("location_name")))
    additional = data.get("additional_locations")
    if isinstance(additional, list):
        for location in additional:
            if isinstance(location, Mapping):
                display = ", ".join(
                    value
                    for value in (
                        _clean(location.get("city")),
                        _clean(location.get("state")),
                        _clean(location.get("country")),
                    )
                    if value
                ) or _clean(location.get("location_name"))
                if display:
                    values.append(display)
    return _unique(values)


def _jibe_work_modes(data: Mapping[str, Any], locations: Iterable[str]) -> list[str]:
    candidates = list(locations)
    for field in ("location_type", "tags1", "tags2", "tags3", "tags4", "tags5"):
        candidates.extend(_strings(data.get(field)))
    result = [value for value in candidates if re.search(r"(?i)remote|hybrid", value)]
    if str(data.get("location_type") or "").casefold() == "any" and not any(
        "remote" in value.casefold() for value in result
    ):
        result.append("Remote")
    return _unique(result)


def _category_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item.get("name")) for item in value if isinstance(item, Mapping) and _clean(item.get("name"))]


def _item_identity(item: Any, variant: Optional[str]) -> str:
    if variant == JIBE_API and isinstance(item, Mapping):
        data = item.get("data")
        if isinstance(data, Mapping):
            return _clean(data.get("slug")) or _clean(data.get("req_id"))
    values = item.xpath(".//a[@data-job-id]/@data-job-id | .//a[contains(@href, '/jobs/')]/@href")
    return _clean(values[0]) if values else _text(item)[:100]


def _dedupe_items(items: Iterable[Any], variant: Optional[str]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        identity = _item_identity(item, variant)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def _integer_attr(root: Any, name: str) -> Optional[int]:
    values = root.xpath(f"//*[@{name}]/@{name}")
    if not values:
        return None
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"iCIMS {name} must be a non-negative integer") from exc
    if value < 0:
        raise AdapterError(f"iCIMS {name} must be a non-negative integer")
    return value


def _zero_text(root: Any) -> bool:
    text = " ".join(root.text_content().split()).casefold()
    return bool(re.search(r"\b(?:0|no)\s+(?:jobs|results|positions)(?:\s+found)?\b", text))


def _texts_by_class(item: Any, names: Iterable[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        for node in item.xpath(f".//*[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"):
            value = re.sub(r"(?i)^(?:category|posted|job id)\s*:\s*", "", _text(node))
            if value:
                result.append(value)
    return _unique(result)


def _normalize_date(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [_clean(item) for item in values if _clean(item)]


def _text(node: Any) -> str:
    return " ".join(node.text_content().split())


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value).strip(" |")
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
