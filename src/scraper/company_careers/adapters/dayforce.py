"""Dayforce public Candidate Portal adapter.

Dayforce's public Next.js board exposes tenant and job-board identity in
``__NEXT_DATA__``.  Its JSON search endpoint is protected by the anonymous
NextAuth CSRF token issued to the same public session; no candidate login or
browser state is required.
"""

from __future__ import annotations

from datetime import date
from html import unescape
import json
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.diagnostics import structure_diagnostic
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)


ENDPOINT_VARIANT = "nextjs_jobposting_search"
HOST = "jobs.dayforcehcm.com"
PAGE_SIZE = 25
MAX_PAGES = 5
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_BOOTSTRAP_BYTES = 1_000_000
MAX_CSRF_BYTES = 10_000
MAX_RESPONSE_BYTES = 3_000_000
MAX_DETAIL_REQUESTS = 10
MAX_REJECTED_RECORD_IDS = 20
MAX_DETAIL_NODES = 10_000
MAX_DETAIL_DEPTH = 24
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


class _IdentityValidatedMissingStructuredLocationError(AdapterError):
    """Raised only after Dayforce listing/detail identity has been validated."""

    def __init__(self, posting_id: int, requisition_id: int) -> None:
        super().__init__(
            "Dayforce identity-validated posting has no structured location: "
            f"posting={posting_id} requisition={requisition_id}"
        )
        self.posting_id = posting_id
        self.requisition_id = requisition_id


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch one bounded, deterministic public Dayforce job collection."""
    config = validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)

    shell = session.get(
        config["search_url"],
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        },
        timeout=timeout,
    )
    next_data = _bootstrap_json(shell)
    site_info = _site_info(next_data, config)
    csrf_token = _csrf_token(session, config, timeout)

    metadata = SourceScanMetadata(**{
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "GET shell + GET CSRF + POST search",
        "page_size": PAGE_SIZE,
        "maximum_pages": int(target.max_pages),
        "maximum_results": int(target.max_jobs),
        "pages": [],
        "jobs_normalized": 0,
        "duplicate_requisitions_skipped": 0,
        "duplicate_posting_ids_skipped": [],
        "duplicate_requisition_ids": [],
        "detail_requests": 0,
        "maximum_detail_requests": MAX_DETAIL_REQUESTS,
        "rejected_record_count": 0,
        "rejected_record_ids": [],
        "rejected_posting_ids": [],
        "rejected_requisition_ids": [],
        "rejected_record_reason": None,
        "malformed_records": 0,
        "total_available": None,
        "stop_reason": "not_started",
    })
    setattr(session, "dayforce_pagination", metadata)
    publish_source_scan_metadata(session, metadata)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config["origin"],
        "Referer": config["search_url"],
        "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        "X-CSRF-TOKEN": csrf_token,
    }
    jobs: list[JobRecord] = []
    seen_posting_ids: set[int] = set()
    seen_requisition_ids: set[int] = set()
    seen_pages: set[tuple[int, ...]] = set()
    observed_total: Optional[int] = None

    for page_index in range(int(target.max_pages)):
        offset = page_index * PAGE_SIZE
        body = {
            "clientNamespace": site_info["clientNamespace"],
            "jobBoardCode": site_info["jobBoardCode"],
            "cultureCode": site_info["cultureCode"],
            "paginationStart": offset,
        }
        metadata["pages_requested"] += 1
        response = session.post(
            config["endpoint_url"],
            headers=headers,
            json=body,
            timeout=timeout,
        )
        payload = _search_json(response, target, offset)
        rows = payload["jobPostings"]
        total = payload["maxCount"]
        if observed_total is None:
            observed_total = total
        elif total != observed_total:
            raise AdapterError("Dayforce total job count changed during pagination")
        if total > offset and not rows:
            raise AdapterError("Dayforce results ended before the reported total")

        page_ids = tuple(_positive_int(row, "jobPostingId", "posting") for row in rows)
        if page_ids and page_ids in seen_pages:
            raise AdapterError("Dayforce repeated a result page during pagination")
        seen_pages.add(page_ids)

        for row in rows:
            posting_id = _positive_int(row, "jobPostingId", "posting")
            if posting_id in seen_posting_ids:
                raise AdapterError(f"Dayforce repeated stable posting ID: {posting_id}")
            seen_posting_ids.add(posting_id)
            requisition_id = _positive_int(row, "jobReqId", "requisition")
            if requisition_id in seen_requisition_ids:
                metadata["duplicate_requisitions_skipped"] += 1
                if (
                    len(metadata["duplicate_requisition_ids"])
                    < MAX_REJECTED_RECORD_IDS
                ):
                    metadata["duplicate_posting_ids_skipped"].append(posting_id)
                    metadata["duplicate_requisition_ids"].append(requisition_id)
                continue
            seen_requisition_ids.add(requisition_id)
            normalized_row = row
            if _needs_detail_location(row):
                if metadata["detail_requests"] >= MAX_DETAIL_REQUESTS:
                    metadata["stop_reason"] = "detail_enrichment_limit_reached"
                    metadata["scan_completeness_reason"] = (
                        "missing_location_detail_enrichment_limit_reached"
                    )
                    metadata["safety_limit_reached"] = True
                    raise AdapterError(
                        "Dayforce missing-location detail enrichment exceeded "
                        f"the {MAX_DETAIL_REQUESTS}-request bound"
                    )
                metadata["detail_requests"] += 1
                detail = _fetch_detail_posting(
                    session, row, target, config, site_info, timeout
                )
                try:
                    normalized_row = _merge_detail_location(row, detail)
                except _IdentityValidatedMissingStructuredLocationError as exc:
                    rejected_count = int(metadata["rejected_record_count"]) + 1
                    metadata["rejected_record_count"] = rejected_count
                    metadata["malformed_records"] = rejected_count
                    if len(metadata["rejected_record_ids"]) < MAX_REJECTED_RECORD_IDS:
                        metadata["rejected_record_ids"].append(
                            {
                                "posting_id": exc.posting_id,
                                "requisition_id": exc.requisition_id,
                            }
                        )
                        metadata["rejected_posting_ids"].append(exc.posting_id)
                        metadata["rejected_requisition_ids"].append(
                            exc.requisition_id
                        )
                    metadata["rejected_record_reason"] = (
                        "missing_structured_location"
                    )
                    continue
            jobs.append(_normalize(normalized_row, target, config, site_info))

        metadata["pages"].append(
            {
                "page_number": page_index + 1,
                "offset": offset,
                "jobs_returned": len(rows),
            }
        )
        metadata["pages_completed"] += 1
        metadata["jobs_normalized"] = min(len(jobs), int(target.max_jobs))
        if offset + len(rows) >= total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if len(jobs) >= int(target.max_jobs):
            metadata["stop_reason"] = "maximum_results_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"

    metadata["total_available"] = observed_total
    if metadata["stop_reason"] == "total_jobs_reached":
        metadata["scan_complete"] = True
        metadata["scan_completeness_reason"] = "reported_total_enumerated"
    else:
        metadata["scan_complete"] = False
        metadata["safety_limit_reached"] = metadata["stop_reason"] in {
            "maximum_results_reached",
            "maximum_pages_reached",
        }
        metadata["scan_completeness_reason"] = metadata["stop_reason"]
    if metadata["rejected_record_count"]:
        metadata["scan_complete"] = False
        metadata["scan_completeness_reason"] = "malformed_listing_records"
        if not jobs:
            raise AdapterError(
                "Dayforce had no valid jobs after rejecting identity-validated "
                "postings with no structured location"
            )
    return jobs[: int(target.max_jobs)]


def validate_target(target: CompanyTarget) -> dict[str, str]:
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Dayforce endpoint_variant must be {ENDPOINT_VARIANT}")
    tenant = str(target.tenant or "").strip()
    board = str(target.site_id or "").strip()
    locale = str(target.locale or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", tenant):
        raise AdapterError("Dayforce target needs a valid tenant/client namespace")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", board):
        raise AdapterError("Dayforce target needs a valid site_id/job board code")
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale):
        raise AdapterError("Dayforce target needs an ll-CC locale")

    search = urlparse(str(target.search_url or ""))
    endpoint = urlparse(str(target.endpoint_url or ""))
    expected_search_path = f"/{locale}/{tenant}/{board}"
    expected_endpoint_path = f"/api/geo/{tenant}/jobposting/search"
    if (
        search.scheme != "https"
        or (search.hostname or "").casefold() != HOST
        or search.path.rstrip("/").casefold() != expected_search_path.casefold()
        or search.query
        or search.fragment
    ):
        raise AdapterError("Dayforce search_url must match its locale, tenant, and site_id")
    if (
        endpoint.scheme != "https"
        or (endpoint.hostname or "").casefold() != HOST
        or endpoint.path.casefold() != expected_endpoint_path.casefold()
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError("Dayforce endpoint_url must use the public tenant search path")
    allowed = {str(value).strip().casefold().rstrip(".") for value in target.allowed_apply_hosts}
    if allowed != {HOST}:
        raise AdapterError(f"Dayforce allowed_apply_hosts must contain only {HOST}")
    if int(target.page_size) != PAGE_SIZE:
        raise AdapterError(f"Dayforce page_size must be {PAGE_SIZE}")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Dayforce max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Dayforce max_jobs must be between 1 and {MAX_RESULTS}")
    if int(target.max_jobs) > int(target.max_pages) * PAGE_SIZE:
        raise AdapterError("Dayforce max_jobs exceeds the configured page capacity")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError(f"Dayforce max_response_bytes must be between 1 and {MAX_RESPONSE_BYTES}")
    origin = f"{search.scheme}://{search.netloc}"
    return {
        "tenant": tenant,
        "board": board,
        "locale": locale,
        "origin": origin,
        "search_url": search.geturl(),
        "endpoint_url": endpoint.geturl(),
        "csrf_url": f"{origin}/api/auth/csrf",
    }


def _bootstrap_json(response: Any) -> Mapping[str, Any]:
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce public shell HTTP {status}")
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > MAX_BOOTSTRAP_BYTES:
        raise AdapterError("Dayforce public shell exceeded the byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Dayforce public shell was not HTML")
    matches = NEXT_DATA_RE.findall(str(getattr(response, "text", "") or ""))
    if len(matches) != 1:
        raise AdapterError("Dayforce public shell omitted unambiguous __NEXT_DATA__")
    try:
        value = json.loads(matches[0])
    except (TypeError, ValueError) as exc:
        raise AdapterError("Dayforce __NEXT_DATA__ was invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise AdapterError("Dayforce __NEXT_DATA__ was malformed")
    return value


def _site_info(next_data: Mapping[str, Any], config: Mapping[str, str]) -> dict[str, Any]:
    query = next_data.get("query")
    page_props = next_data.get("props")
    if not isinstance(query, Mapping) or not isinstance(page_props, Mapping):
        raise AdapterError("Dayforce public shell omitted route identity")
    if (
        str(query.get("clientNamespace") or "").casefold() != config["tenant"].casefold()
        or str(query.get("careerSiteXRefCode") or "").casefold() != config["board"].casefold()
    ):
        raise AdapterError("Dayforce public shell crossed tenant or job-board boundaries")
    page = page_props.get("pageProps")
    dehydrated = page.get("dehydratedState") if isinstance(page, Mapping) else None
    queries = dehydrated.get("queries") if isinstance(dehydrated, Mapping) else None
    if not isinstance(queries, list):
        raise AdapterError("Dayforce public shell omitted site-info state")
    matches: list[Mapping[str, Any]] = []
    for item in queries:
        if not isinstance(item, Mapping):
            continue
        query_key = item.get("queryKey")
        state = item.get("state")
        data = state.get("data") if isinstance(state, Mapping) else None
        if isinstance(query_key, list) and query_key[:1] == ["site-info"] and isinstance(data, Mapping):
            matches.append(data)
    if len(matches) != 1:
        raise AdapterError("Dayforce public shell omitted unambiguous site-info state")
    site = matches[0]
    job_board_id = site.get("jobBoardId")
    if (
        str(site.get("clientNamespace") or "").casefold() != config["tenant"].casefold()
        or str(site.get("jobBoardCode") or "").casefold() != config["board"].casefold()
        or str(site.get("cultureCode") or "") != config["locale"]
        or isinstance(job_board_id, bool)
        or not isinstance(job_board_id, int)
        or job_board_id < 1
    ):
        raise AdapterError("Dayforce site-info crossed tenant, board, or locale boundaries")
    return {
        "clientNamespace": str(site["clientNamespace"]),
        "jobBoardCode": str(site["jobBoardCode"]),
        "cultureCode": str(site["cultureCode"]),
        "jobBoardId": job_board_id,
    }


def _csrf_token(
    session: requests.Session,
    config: Mapping[str, str],
    timeout: int,
) -> str:
    response = session.get(
        config["csrf_url"],
        headers={
            "Accept": "application/json",
            "Referer": config["search_url"],
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        },
        timeout=timeout,
    )
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce anonymous CSRF endpoint HTTP {status}")
    if len(bytes(getattr(response, "content", b"") or b"")) > MAX_CSRF_BYTES:
        raise AdapterError("Dayforce anonymous CSRF response exceeded the byte limit")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError("Dayforce anonymous CSRF response was not valid JSON") from exc
    token = str(payload.get("csrfToken") or "") if isinstance(payload, Mapping) else ""
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
        raise AdapterError("Dayforce anonymous CSRF response omitted a valid token")
    return token


def _search_json(response: Any, target: CompanyTarget, offset: int) -> Mapping[str, Any]:
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce HTTP {status} for {target.name} offset={offset}")
    if len(bytes(getattr(response, "content", b"") or b"")) > min(
        int(target.max_response_bytes), MAX_RESPONSE_BYTES
    ):
        raise AdapterError("Dayforce search response exceeded max_response_bytes")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError("Dayforce search response was not JSON")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError("Dayforce search response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobPostings"), list):
        raise AdapterError("Dayforce search response has no jobPostings list")
    total = payload.get("maxCount")
    response_offset = payload.get("offset")
    count = payload.get("count")
    rows = payload["jobPostings"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Dayforce maxCount must be non-negative")
    if isinstance(response_offset, bool) or response_offset != offset:
        raise AdapterError("Dayforce response offset did not match the requested page")
    if isinstance(count, bool) or count != len(rows):
        raise AdapterError("Dayforce response count did not match its jobPostings list")
    if len(rows) > PAGE_SIZE:
        raise AdapterError("Dayforce page exceeded the observed page size")
    if total < offset + len(rows):
        raise AdapterError("Dayforce maxCount was smaller than the returned result range")
    return payload


def _normalize(
    row: Any,
    target: CompanyTarget,
    config: Mapping[str, str],
    site_info: Mapping[str, Any],
) -> JobRecord:
    if not isinstance(row, Mapping):
        raise AdapterError("Dayforce posting must be a JSON object")
    posting_id = _positive_int(row, "jobPostingId", "posting")
    requisition_id = _positive_int(row, "jobReqId", "requisition")
    if str(row.get("clientNamespace") or "").casefold() != config["tenant"].casefold():
        raise AdapterError("Dayforce posting crossed tenant boundaries")
    if row.get("jobBoardId") != site_info["jobBoardId"]:
        raise AdapterError("Dayforce posting crossed job-board boundaries")
    title = _clean_text(row.get("jobTitle"))
    if not title:
        raise AdapterError("Dayforce posting has no title")

    try:
        raw_locations = _posting_locations(
            row.get("postingLocations"),
            has_virtual_location=row.get("hasVirtualLocation") is True,
            posting=row,
        )
    except AdapterError as exc:
        raise AdapterError(f"{exc} ({_dayforce_location_diagnostic(row)})") from exc
    locations: list[str] = []
    if row.get("hasVirtualLocation") is True:
        locations.append("Remote")
    for value in raw_locations:
        location = _posting_location_display(value)
        if location and location.casefold() not in {item.casefold() for item in locations}:
            locations.append(location)

    tenant = config["tenant"].casefold()
    board = config["board"].casefold()
    detail_url = _detail_url(config, posting_id)
    return JobRecord(
        job_id=f"dayforce:{tenant}:{board}:{requisition_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=_posting_date(row.get("postingStartTimestampUTC")),
        source=f"dayforce:{tenant}:{board}",
        offices=locations,
        description=_clean_text(row.get("jobDescription")) or None,
        raw_metadata={
            "platform": "dayforce",
            "endpoint_variant": ENDPOINT_VARIANT,
            "job_posting_id": posting_id,
            "requisition_id": requisition_id,
            "job_board_id": site_info["jobBoardId"],
            "has_virtual_location": row.get("hasVirtualLocation") is True,
            "posting_expiry": _clean_text(row.get("postingExpiryTimestampUTC")),
            "posting_locations": list(raw_locations),
            "detail_url": detail_url,
            "apply_url": detail_url,
        },
    )


def _needs_detail_location(row: Mapping[str, Any]) -> bool:
    """Return true only when a non-virtual listing has no location projection."""
    if row.get("postingLocations") is not None or row.get("hasVirtualLocation") is True:
        return False
    return not _alternate_posting_locations(row)


def _detail_url(config: Mapping[str, str], posting_id: int) -> str:
    return (
        f"{config['origin']}/{config['locale']}/{config['tenant']}/{config['board']}"
        f"/jobs/{posting_id}"
    )


def _fetch_detail_posting(
    session: requests.Session,
    listing: Mapping[str, Any],
    target: CompanyTarget,
    config: Mapping[str, str],
    site_info: Mapping[str, Any],
    timeout: int,
) -> Mapping[str, Any]:
    """Fetch one public detail shell and return its identity-checked posting."""
    posting_id = _positive_int(listing, "jobPostingId", "posting")
    requisition_id = _positive_int(listing, "jobReqId", "requisition")
    response = session.get(
        _detail_url(config, posting_id),
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Referer": config["search_url"],
            "User-Agent": "job-radar/1.0 (read-only public careers scan)",
        },
        timeout=timeout,
    )
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AdapterError(f"Dayforce job detail HTTP {status}")
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > min(int(target.max_response_bytes), MAX_RESPONSE_BYTES):
        raise AdapterError("Dayforce job detail exceeded max_response_bytes")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Dayforce job detail was not HTML")
    matches = NEXT_DATA_RE.findall(str(getattr(response, "text", "") or ""))
    if len(matches) != 1:
        raise AdapterError("Dayforce job detail omitted unambiguous __NEXT_DATA__")
    try:
        next_data = json.loads(matches[0])
    except (TypeError, ValueError) as exc:
        raise AdapterError("Dayforce job detail __NEXT_DATA__ was invalid JSON") from exc

    detail = _identity_checked_detail(
        next_data,
        listing,
        posting_id=posting_id,
        requisition_id=requisition_id,
    )
    detail_tenant = detail.get("clientNamespace")
    if (
        detail_tenant is not None
        and str(detail_tenant).casefold() != config["tenant"].casefold()
    ):
        raise AdapterError("Dayforce listing/detail tenant identity mismatch")
    detail_board = detail.get("jobBoardId")
    if detail_board is not None and detail_board != site_info["jobBoardId"]:
        raise AdapterError("Dayforce listing/detail job-board identity mismatch")
    detail_title = _clean_text(detail.get("jobTitle"))
    if detail_title and detail_title != _clean_text(listing.get("jobTitle")):
        raise AdapterError("Dayforce listing/detail title identity mismatch")
    return detail


_DETAIL_LOCATION_FIELDS = frozenset(
    {
        "postingLocations",
        "primaryLocation",
        "jobLocation",
        "workLocation",
        "siteLocation",
        "locationDetails",
        "primaryLocationName",
        "jobLocationName",
        "workLocationName",
        "locationName",
        "formattedAddress",
        "address",
        "addressLineOne",
        "addressLine1",
        "cityName",
        "city",
        "stateProvince",
        "stateName",
        "stateCode",
        "state",
        "countryName",
        "country",
        "countryCode",
        "isoCountryCode",
    }
)
_DETAIL_IDENTITY_FIELDS = {
    "posting": ("jobPostingId", "postingId", "postingID", "jobId"),
    "requisition": (
        "jobReqId",
        "requisitionId",
        "requisitionID",
        "reqId",
    ),
    "reference": (
        "referenceNumber",
        "requisitionNumber",
        "jobRequisitionNumber",
    ),
}
_DETAIL_URL_FIELDS = (
    "canonicalUrl",
    "canonicalPostingUrl",
    "jobPostingUrl",
    "jobUrl",
    "applyUrl",
    "url",
)
_IDENTIFIER_CONTAINER_FIELDS = (
    "identifier",
    "identifiers",
    "postingIdentifier",
    "requisitionIdentifier",
    "jobIdentifier",
)


def _identity_checked_detail(
    next_data: Mapping[str, Any],
    listing: Mapping[str, Any],
    *,
    posting_id: int,
    requisition_id: int,
) -> Mapping[str, Any]:
    """Select one detail payload only after strict, multi-shape identity proof."""
    expected = {
        "posting": str(posting_id),
        "requisition": str(requisition_id),
    }
    listing_references = {
        _identity_scalar(listing[field], f"listing {field}")
        for field in _DETAIL_IDENTITY_FIELDS["reference"]
        if field in listing
    }
    if len(listing_references) > 1:
        raise AdapterError("Dayforce listing exposed ambiguous reference identities")
    if listing_references:
        expected["reference"] = next(iter(listing_references))
    valid: list[tuple[Mapping[str, Any], dict[str, set[str]]]] = []
    conflicts: list[dict[str, set[str]]] = []
    observed: list[Mapping[str, Any]] = []
    listing_title = _clean_text(listing.get("jobTitle"))

    for node in _detail_mapping_nodes(next_data):
        evidence = _detail_identity_evidence(node)
        if not evidence:
            continue
        observed.append(node)
        ambiguous = any(len(values) != 1 for values in evidence.values())
        mismatched = any(
            group not in expected or next(iter(values), "") != expected[group]
            for group, values in evidence.items()
        )
        title = _clean_text(node.get("jobTitle") or node.get("title"))
        posting_payload = bool(_DETAIL_LOCATION_FIELDS.intersection(node)) or bool(title)
        if ambiguous:
            if posting_payload:
                raise AdapterError("Dayforce job detail exposed ambiguous structured identities")
            continue
        if mismatched:
            if posting_payload and (not title or title == listing_title):
                conflicts.append(evidence)
            continue
        if posting_payload:
            valid.append((node, evidence))

    if conflicts:
        raise AdapterError("Dayforce listing/detail identity mismatch")
    if not valid:
        route_identity = _detail_route_identity(next_data)
        if route_identity:
            if route_identity != str(posting_id):
                raise AdapterError("Dayforce listing/detail canonical route identity mismatch")
            route_candidates = [
                node
                for node in _detail_mapping_nodes(next_data)
                if _DETAIL_LOCATION_FIELDS.intersection(node)
                and (
                    not _clean_text(node.get("jobTitle") or node.get("title"))
                    or _clean_text(node.get("jobTitle") or node.get("title"))
                    == listing_title
                )
            ]
            if route_candidates:
                score = max(
                    len(_DETAIL_LOCATION_FIELDS.intersection(node))
                    + (2 if _clean_text(node.get("jobTitle") or node.get("title")) else 0)
                    for node in route_candidates
                )
                strongest_route_candidates = [
                    node
                    for node in route_candidates
                    if len(_DETAIL_LOCATION_FIELDS.intersection(node))
                    + (2 if _clean_text(node.get("jobTitle") or node.get("title")) else 0)
                    == score
                ]
                if len(strongest_route_candidates) == 1:
                    return strongest_route_candidates[0]
                raise AdapterError(
                    "Dayforce job detail exposed ambiguous route-identified location payloads"
                )
        diagnostic = structure_diagnostic(observed[:3])
        raise AdapterError(
            "Dayforce job detail did not expose one structured posting identity "
            f"(identity_shape: {diagnostic})"
        )

    with_locations = [item for item in valid if _DETAIL_LOCATION_FIELDS.intersection(item[0])]
    candidates = with_locations or valid
    strongest = max(sum(len(values) for values in evidence.values()) for _, evidence in candidates)
    strongest_candidates = [
        node
        for node, evidence in candidates
        if sum(len(values) for values in evidence.values()) == strongest
    ]
    unique_candidates: list[Mapping[str, Any]] = []
    seen_shapes: set[str] = set()
    for node in strongest_candidates:
        signature = json.dumps(node, sort_keys=True, separators=(",", ":"), default=str)
        if signature not in seen_shapes:
            seen_shapes.add(signature)
            unique_candidates.append(node)
    if len(unique_candidates) != 1:
        raise AdapterError("Dayforce job detail exposed ambiguous matching posting identities")
    return unique_candidates[0]


def _detail_route_identity(next_data: Mapping[str, Any]) -> str:
    """Read one canonical posting identifier from Next.js route state."""
    query = next_data.get("query")
    if not isinstance(query, Mapping):
        return ""
    values: set[str] = set()
    for field in ("jobPostingId", "postingId", "jobId", "id"):
        if field in query:
            values.add(_identity_scalar(query[field], f"detail route {field}"))
    if len(values) > 1:
        raise AdapterError("Dayforce job detail exposed ambiguous canonical route identities")
    return next(iter(values), "")


def _detail_identity_evidence(value: Mapping[str, Any]) -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = {}
    for group, fields in _DETAIL_IDENTITY_FIELDS.items():
        for field in fields:
            if field in value:
                normalized = _identity_scalar(value[field], f"detail {field}")
                if normalized:
                    evidence.setdefault(group, set()).add(normalized)

    for field in _DETAIL_URL_FIELDS:
        if field not in value:
            continue
        url = value[field]
        if not isinstance(url, str):
            raise AdapterError(f"Dayforce detail {field} identity must be text")
        match = re.search(r"/jobs/([A-Za-z0-9._:-]+)(?:[/?#]|$)", url)
        if match:
            evidence.setdefault("posting", set()).add(match.group(1))

    for field in _IDENTIFIER_CONTAINER_FIELDS:
        if field not in value:
            continue
        containers = value[field] if isinstance(value[field], list) else [value[field]]
        if len(containers) > 16:
            raise AdapterError("Dayforce detail identifiers exceeded the collection bound")
        for container in containers:
            if not isinstance(container, Mapping):
                raise AdapterError(f"Dayforce detail {field} must contain objects")
            for group, fields in _DETAIL_IDENTITY_FIELDS.items():
                for identity_field in fields:
                    if identity_field in container:
                        normalized = _identity_scalar(
                            container[identity_field], f"detail {field}.{identity_field}"
                        )
                        if normalized:
                            evidence.setdefault(group, set()).add(normalized)
            kind = _clean_text(
                container.get("type")
                or container.get("name")
                or container.get("key")
                or container.get("identifierType")
            ).casefold()
            scalar = next(
                (
                    container[key]
                    for key in ("value", "codeValue", "id")
                    if key in container
                ),
                None,
            )
            if scalar is not None:
                if "post" in kind or kind in {"jobid", "job id"}:
                    group = "posting"
                elif "reference" in kind:
                    group = "reference"
                elif "req" in kind:
                    group = "requisition"
                else:
                    continue
                normalized = _identity_scalar(scalar, f"detail {field}.{kind or 'value'}")
                if normalized:
                    evidence.setdefault(group, set()).add(normalized)
    return evidence


def _identity_scalar(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AdapterError(f"Dayforce {label} identity must be a scalar")
    text = str(value).strip()
    if not text or len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", text):
        raise AdapterError(f"Dayforce {label} identity is malformed")
    return text


def _detail_mapping_nodes(value: Any) -> list[Mapping[str, Any]]:
    """Walk a bounded JSON tree; interpretation remains posting-contract-specific."""
    nodes: list[Mapping[str, Any]] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > MAX_DETAIL_NODES:
            raise AdapterError("Dayforce job detail exceeded the structured-node bound")
        if depth > MAX_DETAIL_DEPTH:
            raise AdapterError("Dayforce job detail exceeded the structured-depth bound")
        if isinstance(current, Mapping):
            nodes.append(current)
            stack.extend((item, depth + 1) for item in reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in reversed(current))
    return nodes


def _merge_detail_location(
    listing: Mapping[str, Any], detail: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Copy only explicit structured location projections from detail data."""
    posting_id = _positive_int(listing, "jobPostingId", "posting")
    requisition_id = _positive_int(listing, "jobReqId", "requisition")
    merged = dict(listing)
    fields = (
        "postingLocations",
        "primaryLocation",
        "jobLocation",
        "workLocation",
        "siteLocation",
        "locationDetails",
        "primaryLocationName",
        "jobLocationName",
        "workLocationName",
        "locationName",
        "formattedAddress",
        "address",
        "addressLineOne",
        "addressLine1",
        "cityName",
        "city",
        "stateProvince",
        "stateName",
        "stateCode",
        "state",
        "countryName",
        "country",
        "countryCode",
        "isoCountryCode",
    )
    copied = False
    for field in fields:
        if field in detail:
            merged[field] = detail[field]
            copied = True
    if not copied:
        raise _IdentityValidatedMissingStructuredLocationError(
            posting_id, requisition_id
        )
    if merged.get("postingLocations") is None and not _alternate_posting_locations(merged):
        raise _IdentityValidatedMissingStructuredLocationError(
            posting_id, requisition_id
        )
    try:
        locations = _posting_locations(
            merged.get("postingLocations"),
            has_virtual_location=False,
            posting=merged,
        )
        if not locations:
            raise _IdentityValidatedMissingStructuredLocationError(
                posting_id, requisition_id
            )
        for location in locations:
            _posting_location_display(location)
    except _IdentityValidatedMissingStructuredLocationError:
        raise
    except AdapterError as exc:
        raise AdapterError(f"Dayforce job detail location is invalid: {exc}") from exc
    return merged


def _posting_locations(
    value: Any,
    *,
    has_virtual_location: bool,
    posting: Optional[Mapping[str, Any]] = None,
) -> list[Mapping[str, Any]]:
    """Normalize only Dayforce's equivalent location collection shapes."""
    if value is None:
        if has_virtual_location:
            return []
        alternates = _alternate_posting_locations(posting or {})
        if alternates:
            return alternates
        raise AdapterError("Dayforce postingLocations is null for a non-virtual posting")
    if isinstance(value, Mapping):
        # Some Candidate Portal tenants project the sole location as the same
        # location object used inside the normal array. Validate it before
        # normalizing so arbitrary wrappers are never coerced into a list.
        _posting_location_display(value)
        return [value]
    if not isinstance(value, list):
        raise AdapterError("Dayforce postingLocations must be a list or location object")
    normalized: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AdapterError("Dayforce posting location must be an object")
        _posting_location_display(item)
        normalized.append(item)
    return normalized


def _posting_location_display(value: Mapping[str, Any]) -> str:
    formatted = _location_text(value.get("formattedAddress"), "formattedAddress")
    if formatted:
        return formatted
    named = next(
        (
            _location_text(value.get(field), field)
            for field in ("locationName", "displayName", "name")
            if value.get(field) is not None
        ),
        "",
    )
    if named:
        return named
    address = value.get("address")
    if address is not None and not isinstance(address, Mapping):
        raise AdapterError("Dayforce posting location address must be an object")
    source = address if isinstance(address, Mapping) else value
    line_one = _first_dayforce_component(
        source, ("addressLineOne", "addressLine1", "lineOne")
    )
    city = _first_dayforce_component(source, ("cityName", "city"))
    subdivision = _first_dayforce_component(
        source, ("stateProvince", "stateName", "stateCode", "state")
    )
    country = _first_dayforce_component(
        source, ("countryName", "country", "countryCode", "isoCountryCode")
    )
    parts = [line_one, city, subdivision, country]
    display = ", ".join(part for part in parts if part)
    if not display:
        raise AdapterError("Dayforce posting location has no stable display value")
    return display


def _location_text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AdapterError(f"Dayforce posting location {field_name} must be text")
    return _clean_text(value)


def _first_dayforce_component(
    value: Mapping[str, Any], fields: tuple[str, ...]
) -> str:
    for field in fields:
        component = value.get(field)
        if component is None:
            continue
        if isinstance(component, str):
            text = _clean_text(component)
        elif isinstance(component, Mapping):
            text = next(
                (
                    _clean_text(component.get(key))
                    for key in ("longName", "shortName", "name", "codeValue")
                    if isinstance(component.get(key), str) and _clean_text(component.get(key))
                ),
                "",
            )
        else:
            raise AdapterError(f"Dayforce posting location {field} must be text or an object")
        if text:
            return text
    return ""


def _alternate_posting_locations(
    posting: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Read only explicitly named Dayforce location projections."""
    for field in (
        "primaryLocation",
        "jobLocation",
        "workLocation",
        "siteLocation",
        "locationDetails",
    ):
        value = posting.get(field)
        candidates = value if isinstance(value, list) else [value]
        if value is None:
            continue
        normalized: list[Mapping[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise AdapterError(f"Dayforce alternate location field {field} must be an object")
            _posting_location_display(candidate)
            normalized.append(candidate)
        if normalized:
            return normalized

    for field in ("primaryLocationName", "jobLocationName", "workLocationName", "locationName"):
        value = posting.get(field)
        if value is None:
            continue
        text = _location_text(value, field)
        if text:
            return [{"locationName": text}]

    explicit = {
        field: posting[field]
        for field in (
            "formattedAddress",
            "address",
            "addressLineOne",
            "addressLine1",
            "cityName",
            "city",
            "stateProvince",
            "stateName",
            "stateCode",
            "state",
            "countryName",
            "country",
            "countryCode",
            "isoCountryCode",
        )
        if field in posting
    }
    if explicit:
        _posting_location_display(explicit)
        return [explicit]
    return []


def _dayforce_location_diagnostic(posting: Mapping[str, Any]) -> str:
    location_fields = {
        str(key): value
        for key, value in posting.items()
        if any(
            token in str(key).casefold()
            for token in ("location", "city", "state", "province", "country", "address", "site")
        )
    }
    names = ",".join(sorted(location_fields, key=str.casefold)) or "none"
    return (
        f"virtual={str(posting.get('hasVirtualLocation') is True).lower()}; "
        f"location_fields=[{names}]; "
        f"postingLocations: {structure_diagnostic(posting.get('postingLocations'))}; "
        f"alternate_fields: {structure_diagnostic(location_fields)}"
    )


def _positive_int(row: Any, field: str, label: str) -> int:
    if not isinstance(row, Mapping):
        raise AdapterError("Dayforce posting must be a JSON object")
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterError(f"Dayforce posting has no stable numeric {label} ID")
    return value


def _posting_date(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise AdapterError("Dayforce postingStartTimestampUTC was invalid") from exc


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()
