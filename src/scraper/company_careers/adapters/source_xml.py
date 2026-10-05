"""Bounded production adapter for first-party ``source/job`` XML feeds."""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.listing_completion import (
    PaginationEvidence,
    enforce_pagination_fail_closed,
    finish_authoritative_total,
    publish_pagination_evidence,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.source_health import (
    assess_source_response,
    record_source_response_assessment,
)
from src.scraper.company_careers.xml_security import (
    direct_children,
    first_child_text,
    local_name,
    safe_xml_root,
)


ENDPOINT_VARIANT = "source_jobs_v1"
MAX_RESPONSE_BYTES = 8_000_000
MAX_JOBS = 20_000
MAX_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 2
STREAM_CHUNK_BYTES = 64 * 1024
RETRY_DELAY_SECONDS = 0.1
TRANSIENT_STATUSES = {202, 408, 425, 500, 502, 503, 504}
HEADERS = {
    "Accept": "application/xml,text/xml,application/rss+xml",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
LISTING_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": HEADERS["User-Agent"],
}
PUBLICATION_FIELDS = ("date", "pubdate", "published", "updated")


def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 60
) -> list[JobRecord]:
    """Fetch exactly one source-backed XML document and validate every job."""
    validate_target(target)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    endpoint = str(target.endpoint_url)
    metadata = SourceScanMetadata(
        endpoint_variant=target.endpoint_variant,
        request_method="GET",
        maximum_pages=1,
        maximum_results=int(target.max_jobs),
        maximum_response_bytes=int(target.max_response_bytes),
        pages=[],
        pages_fetched=0,
        jobs_normalized=0,
        records_observed=0,
        bytes_inspected=0,
        response_fully_read=False,
        response_truncated=False,
        diagnostics=[],
        warnings=[],
        original_configured_url=endpoint,
    )
    publish_source_scan_metadata(session, metadata)

    listing_before: dict[str, Any] | None = None
    if target.listing_total_regex:
        metadata["listing_corroboration_required"] = True
        listing_before = _listing_snapshot(session, target, timeout, metadata)

    metadata["pages_requested"] = 1
    response = _get(session, endpoint, timeout, headers=HEADERS)
    try:
        content = _response_bytes(response, target, metadata)
    except AdapterError:
        raise

    assessment = assess_source_response(
        configured_url=endpoint,
        final_url=str(getattr(response, "url", "") or endpoint),
        status_code=int(getattr(response, "status_code", 0) or 0),
        allowed_hosts=target.allowed_source_hosts,
        body_prefix=content[:20_000].decode("utf-8", errors="replace"),
        redirect_count=len(getattr(response, "history", ()) or ()),
        required_markers=("<source",),
    )
    record_source_response_assessment(metadata, assessment)
    metadata["source_identity_status"] = "unproven"
    if not assessment.accepted:
        metadata["stop_reason"] = assessment.health_code.value
        metadata["scan_completeness_reason"] = assessment.health_code.value
        raise AdapterError(f"Source XML response rejected: {assessment.reason}")

    try:
        root = safe_xml_root(content)
    except AdapterError as exc:
        message = str(exc)
        if any(
            token in message.casefold()
            for token in ("premature end", "expected '>'", "no element found")
        ):
            metadata["response_truncated"] = True
            metadata["stop_reason"] = "truncated_xml_response"
            metadata["scan_completeness_reason"] = "truncated_xml_response"
        else:
            metadata["stop_reason"] = "malformed_xml_response"
            metadata["scan_completeness_reason"] = "malformed_xml_response"
        raise
    if local_name(root).casefold() != "source":
        metadata["stop_reason"] = "unsupported_xml_root"
        metadata["scan_completeness_reason"] = "unsupported_xml_root"
        raise AdapterError("Source XML document has an unsupported root element")

    publisher_nodes = direct_children(root, "publisher")
    publisher_values = _unique(_element_text(node) for node in publisher_nodes)
    publisher = publisher_values[0] if len(publisher_values) == 1 else ""
    metadata["publisher"] = publisher
    if len(publisher_nodes) != 1 or len(publisher_values) != 1:
        metadata["source_identity_status"] = "failed"
        metadata["stop_reason"] = "publisher_identity_ambiguous"
        metadata["scan_completeness_reason"] = "publisher_identity_ambiguous"
        raise AdapterError("Source XML publisher identity must occur exactly once")
    identity = static_html._safe_regex(str(target.identity_regex))
    if (
        publisher.casefold() != _text(target.name).casefold()
        or identity.fullmatch(publisher) is None
    ):
        metadata["source_identity_status"] = "failed"
        metadata["stop_reason"] = "publisher_identity_mismatch"
        metadata["scan_completeness_reason"] = "publisher_identity_mismatch"
        raise AdapterError(
            f"Source XML publisher identity mismatch for {target.name}"
        )
    metadata["source_identity_status"] = "validated"
    metadata["source_identity_diagnostic"] = {
        "field": "publisher",
        "expected_company": target.name,
        "observed_publisher": publisher,
        "match": "exact",
    }
    metadata["last_build_date"] = first_child_text(root, ("lastBuildDate",))
    metadata["xml_root_metadata"] = [
        {"field": local_name(child), "value": _element_text(child)[:500]}
        for child in root
        if local_name(child).casefold() != "job"
    ]

    job_nodes = direct_children(root, "job")
    metadata["records_observed"] = len(job_nodes)
    if len(job_nodes) > int(target.max_jobs):
        metadata["safety_limit_reached"] = True
        metadata["stop_reason"] = "maximum_results_reached"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        raise AdapterError(
            f"Source XML safety limit exceeded: {len(job_nodes)} jobs is above "
            f"the {int(target.max_jobs)}-job limit"
        )

    total, total_evidence = _authoritative_total(root, target)
    pagination = _pagination_evidence(root, target, total, total_evidence)
    publish_pagination_evidence(metadata, pagination)
    metadata["total_metadata"] = total_evidence
    if total is not None:
        metadata["total_available"] = total
        metadata["authoritative_total_present"] = True

    jobs: list[JobRecord] = []
    diagnostics: list[dict[str, Any]] = []
    for index, node in enumerate(job_nodes, start=1):
        try:
            jobs.append(_normalize_job(node, target, publisher))
        except AdapterError as exc:
            diagnostics.append({"record": index, "error": str(exc)[:500]})

    id_counts = Counter(job.job_id for job in jobs)
    url_counts = Counter(job.url for job in jobs)
    duplicate_ids = sorted(value for value, count in id_counts.items() if count > 1)
    duplicate_urls = sorted(value for value, count in url_counts.items() if count > 1)
    metadata.update(
        valid_records_parsed=len(jobs),
        rejected_record_count=len(diagnostics),
        diagnostics=diagnostics[:20],
        duplicate_ids=duplicate_ids,
        duplicate_canonical_urls=duplicate_urls,
        unique_stable_ids=len(id_counts),
        unique_canonical_urls=len(url_counts),
    )
    if diagnostics:
        metadata["stop_reason"] = "malformed_required_records"
        metadata["scan_completeness_reason"] = "malformed_required_records"
        raise AdapterError(
            f"Source XML contained {len(diagnostics)} malformed required record(s)"
        )
    if duplicate_ids or duplicate_urls:
        metadata["rejected_record_count"] = len(duplicate_ids) + len(duplicate_urls)
        metadata["stop_reason"] = "duplicate_job_records"
        metadata["scan_completeness_reason"] = "duplicate_job_records"
        raise AdapterError(
            "Source XML contained duplicate stable IDs or canonical job URLs"
        )

    metadata["jobs_normalized"] = len(jobs)
    metadata["pages_completed"] = 1
    metadata["pages_fetched"] = 1
    metadata["pages"] = [
        {
            "page": 1,
            "url": endpoint,
            "jobs_returned": len(job_nodes),
            "jobs_normalized": len(jobs),
            "bytes": len(content),
        }
    ]
    metadata["terminal_page_fully_processed"] = True

    listing_total: int | None = None
    if listing_before is not None:
        listing_after = _listing_snapshot(session, target, timeout, metadata)
        metadata["listing_corroboration"] = {
            "before": listing_before,
            "after": listing_after,
        }
        before_total = listing_before.get("total")
        after_total = listing_after.get("total")
        if before_total is None or after_total is None:
            metadata["listing_total_present"] = False
        elif before_total != after_total:
            metadata["total_count_changed"] = True
            metadata["collection_changed_during_scan"] = True
        else:
            listing_total = int(before_total)
            metadata["listing_total_present"] = True
            metadata["listing_total"] = listing_total

    continuation = bool(pagination.continuation_reasons())
    if pagination.malformed_fields or pagination.conflicting_fields:
        enforce_pagination_fail_closed(metadata)
    elif metadata.get("collection_changed_during_scan"):
        metadata["stop_reason"] = "collection_changed_during_scan"
        metadata["scan_completeness_reason"] = "collection_changed_during_scan"
    elif continuation:
        metadata["stop_reason"] = "unsupported_pagination_continuation"
        metadata["scan_completeness_reason"] = "unsupported_pagination_continuation"
    elif total is not None and listing_total is not None and total != listing_total:
        metadata["pagination_evidence_conflicting"] = True
        metadata["stop_reason"] = "conflicting_total_metadata"
        metadata["scan_completeness_reason"] = "conflicting_total_metadata"
    elif total is None and listing_total is None:
        reason = (
            "empty_collection_without_authoritative_total"
            if not jobs
            else "collection_completeness_not_proven"
        )
        metadata["stop_reason"] = reason
        metadata["scan_completeness_reason"] = reason
    elif len(jobs) != (total if total is not None else listing_total):
        expected_total = total if total is not None else listing_total
        assert expected_total is not None
        reason = (
            "conflicting_total_metadata"
            if len(jobs) > expected_total
            else "authoritative_total_not_reached"
        )
        metadata["pagination_evidence_conflicting"] = len(jobs) > expected_total
        metadata["total_available"] = expected_total
        metadata["authoritative_total_present"] = True
        metadata["stop_reason"] = reason
        metadata["scan_completeness_reason"] = reason
    else:
        expected_total = total if total is not None else listing_total
        assert expected_total is not None
        metadata["total_available"] = expected_total
        metadata["authoritative_total_present"] = True
        completion_fields = list(pagination.fields)
        if listing_total is not None:
            completion_fields.append("official_unfiltered_listing_total")
        completion_evidence = PaginationEvidence(
            reported_total=expected_total,
            fields=tuple(completion_fields),
            malformed_fields=pagination.malformed_fields,
            conflicting_fields=pagination.conflicting_fields,
            has_more=pagination.has_more,
            next_url_present=pagination.next_url_present,
            next_page=pagination.next_page,
            page_count=pagination.page_count,
            current_page=pagination.current_page,
            explicit_final_page=pagination.explicit_final_page,
            field_diagnostics=pagination.field_diagnostics,
        )
        finish_authoritative_total(
            metadata, expected_total, len(jobs), evidence=completion_evidence
        )
        if metadata.get("scan_complete") and listing_total is not None:
            metadata["authoritative_total_source"] = (
                "same_run_official_unfiltered_listing_corroboration"
            )
        if not metadata["scan_complete"]:
            metadata["stop_reason"] = "conflicting_pagination_evidence"
            metadata["scan_completeness_reason"] = "conflicting_pagination_evidence"
    enforce_pagination_fail_closed(metadata)
    return jobs


def validate_target(target: CompanyTarget) -> None:
    required = {
        "endpoint_url": target.endpoint_url,
        "endpoint_variant": target.endpoint_variant,
        "identity_regex": target.identity_regex,
        "id_field": target.id_field,
        "title_field": target.title_field,
        "url_field": target.url_field,
        "allowed_source_hosts": target.allowed_source_hosts,
        "allowed_apply_hosts": target.allowed_apply_hosts,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise AdapterError(f"Source XML target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"Source XML unsupported variant: {target.endpoint_variant}")
    static_html._require_https(str(target.endpoint_url), "endpoint_url")
    static_html._validate_hosts(target.allowed_source_hosts)
    static_html._validate_hosts(target.allowed_apply_hosts)
    endpoint_host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    if not static_html._allowed_host(endpoint_host, target.allowed_source_hosts):
        raise AdapterError("Source XML endpoint host is not approved")
    pattern = str(target.identity_regex)
    if not (pattern.startswith("^") and pattern.endswith("$")):
        raise AdapterError("Source XML identity_regex must be exact and anchored")
    static_html._safe_regex(pattern)
    for field_name in (
        target.id_field,
        target.title_field,
        target.url_field,
        target.date_field,
        target.complete_collection_total_field,
        *target.location_fields,
    ):
        if field_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", str(field_name)):
            raise AdapterError("Source XML field names must be simple XML local names")
    if target.external_id_regex:
        pattern = static_html._safe_regex(str(target.external_id_regex))
        if pattern.groups != 1:
            raise AdapterError("Source XML external_id_regex must contain one capture group")
    corroborates_listing = bool(target.listing_total_regex or target.listing_identity_regex)
    if corroborates_listing:
        if not (
            target.listing_url
            and target.listing_total_regex
            and target.listing_identity_regex
        ):
            raise AdapterError(
                "Source XML listing corroboration requires listing_url, "
                "listing_total_regex, and listing_identity_regex"
            )
        static_html._require_https(str(target.listing_url), "listing_url")
        listing = urlparse(str(target.listing_url))
        if listing.query or listing.fragment:
            raise AdapterError("Source XML corroborating listing must be unfiltered")
        if not static_html._allowed_host(
            (listing.hostname or "").casefold(), target.allowed_source_hosts
        ):
            raise AdapterError("Source XML listing host is not approved")
        listing_identity = static_html._safe_regex(str(target.listing_identity_regex))
        listing_total = static_html._safe_regex(str(target.listing_total_regex))
        if listing_identity.groups or listing_total.groups != 1:
            raise AdapterError(
                "Source XML listing identity must have no captures and total regex one"
            )
    if not target.complete_collection_total_field and not target.listing_total_regex:
        raise AdapterError(
            "Source XML target needs root total metadata or listing corroboration"
        )
    if int(target.max_pages) != 1:
        raise AdapterError("Source XML does not support pagination assumptions; max_pages must be 1")
    if not 1 <= int(target.max_jobs) <= MAX_JOBS:
        raise AdapterError(f"Source XML max_jobs cannot exceed {MAX_JOBS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError(
            f"Source XML max_response_bytes cannot exceed {MAX_RESPONSE_BYTES}"
        )


def _get(
    session: requests.Session,
    url: str,
    timeout: int,
    *,
    headers: dict[str, str],
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                url, headers=headers, timeout=timeout, stream=True
            )
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        status = int(getattr(response, "status_code", 0) or 0)
        if status in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if status in {401, 407}:
            raise AdapterError(f"Source XML authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"Source XML access blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Source XML HTTP {status}")
        return response
    raise AdapterError("Source XML request attempts exhausted")


def _response_bytes(
    response: Any, target: CompanyTarget, metadata: SourceScanMetadata
) -> bytes:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and not any(
        marker in content_type.casefold() for marker in ("xml", "text/plain")
    ):
        metadata["stop_reason"] = "non_xml_response"
        metadata["scan_completeness_reason"] = "non_xml_response"
        raise AdapterError("Source XML endpoint returned a non-XML content type")
    limit = int(target.max_response_bytes)
    raw_length = str(
        getattr(response, "headers", {}).get("content-length", "") or ""
    ).strip()
    declared_length = int(raw_length) if raw_length.isascii() and raw_length.isdigit() else None
    metadata["declared_content_length"] = declared_length
    if declared_length is not None and declared_length > limit:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        metadata["safety_limit_reached"] = True
        metadata["stop_reason"] = "response_size_limit_exceeded"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        raise AdapterError(
            f"Source XML safety limit exceeded: Content-Length is above {limit} bytes"
        )

    chunks: list[bytes] = []
    retained = 0
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        try:
            stream = iterator(chunk_size=STREAM_CHUNK_BYTES)
        except TypeError:
            stream = None
    else:
        stream = None
    if stream is None:
        raw = getattr(response, "content", None)
        if raw is None:
            raw = str(getattr(response, "text", "") or "").encode("utf-8")
        stream = (bytes(raw),)
    for chunk in stream:
        if not chunk:
            continue
        raw = bytes(chunk)
        retained += len(raw)
        if retained > limit:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            metadata["bytes_inspected"] = min(retained, limit + 1)
            metadata["safety_limit_reached"] = True
            metadata["stop_reason"] = "response_size_limit_exceeded"
            metadata["scan_completeness_reason"] = "safety_limit_reached"
            raise AdapterError(
                f"Source XML safety limit exceeded: response exceeded {limit} bytes"
            )
        chunks.append(raw)
    content = b"".join(chunks)
    metadata["bytes_inspected"] = len(content)
    if not content:
        metadata["stop_reason"] = "empty_http_response"
        metadata["scan_completeness_reason"] = "empty_http_response"
        raise AdapterError("Source XML returned an empty HTTP 200 response")
    if (
        declared_length is not None
        and not getattr(response, "headers", {}).get("content-encoding")
        and len(content) < declared_length
    ):
        metadata["response_truncated"] = True
        metadata["stop_reason"] = "truncated_xml_response"
        metadata["scan_completeness_reason"] = "truncated_xml_response"
        raise AdapterError("Source XML response was truncated before Content-Length")
    metadata["response_fully_read"] = True
    return content


def _listing_snapshot(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
    metadata: SourceScanMetadata,
) -> dict[str, Any]:
    """Read one exact unfiltered official listing count around the XML read."""
    configured_url = str(target.listing_url)
    response = _get(
        session, configured_url, timeout, headers=LISTING_HEADERS
    )
    content = _listing_response_bytes(response, target, metadata)
    final_url = str(getattr(response, "url", "") or configured_url)
    configured = static_html._canonical_job_url(configured_url)
    final = static_html._canonical_job_url(final_url)
    parsed_final = urlparse(final_url)
    if final != configured or parsed_final.query or parsed_final.fragment:
        metadata["listing_population_status"] = "filtered_or_redirected"
        raise AdapterError(
            "Source XML corroborating listing did not remain the exact unfiltered URL"
        )
    assessment = assess_source_response(
        configured_url=configured_url,
        final_url=final_url,
        status_code=int(getattr(response, "status_code", 0) or 0),
        allowed_hosts=target.allowed_source_hosts,
        body_prefix=content[:20_000].decode("utf-8", errors="replace"),
        redirect_count=len(getattr(response, "history", ()) or ()),
    )
    if not assessment.accepted:
        metadata["listing_identity_status"] = "failed"
        raise AdapterError(
            f"Source XML corroborating listing rejected: {assessment.reason}"
        )
    root = static_html._parse_html(content, response)
    title = " ".join(root.xpath("string(//title)").split())
    identity = static_html._safe_regex(str(target.listing_identity_regex))
    if identity.fullmatch(title) is None:
        metadata["listing_identity_status"] = "failed"
        raise AdapterError("Source XML corroborating listing identity mismatch")
    metadata["listing_identity_status"] = "validated"
    visible = " ".join(
        " ".join(str(value).split())
        for value in root.xpath(
            "//body//text()[not(ancestor::script) and not(ancestor::style) "
            "and not(ancestor::template)]"
        )
        if str(value).strip()
    )
    pattern = static_html._safe_regex(str(target.listing_total_regex))
    raw_values = [match.group(1) for match in pattern.finditer(visible)]
    totals = {
        int(value.replace(",", ""))
        for value in raw_values
        if re.fullmatch(r"[0-9][0-9,]*", value)
    }
    total = next(iter(totals)) if len(totals) == 1 else None
    metadata["listing_population_status"] = (
        "validated_unfiltered" if total is not None else "count_unproven"
    )
    return {
        "url": final,
        "title": title,
        "total": total,
        "count_matches": len(raw_values),
        "bytes": len(content),
    }


def _listing_response_bytes(
    response: Any, target: CompanyTarget, metadata: SourceScanMetadata
) -> bytes:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and not any(
        marker in content_type.casefold() for marker in ("text/html", "application/xhtml")
    ):
        raise AdapterError("Source XML corroborating listing returned non-HTML content")
    limit = int(target.max_response_bytes)
    raw_length = str(
        getattr(response, "headers", {}).get("content-length", "") or ""
    ).strip()
    declared = int(raw_length) if raw_length.isascii() and raw_length.isdigit() else None
    if declared is not None and declared > limit:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        metadata["safety_limit_reached"] = True
        raise AdapterError("Source XML corroborating listing exceeded response size limit")
    chunks: list[bytes] = []
    retained = 0
    iterator = getattr(response, "iter_content", None)
    stream = iterator(chunk_size=STREAM_CHUNK_BYTES) if callable(iterator) else None
    if stream is None:
        raw = getattr(response, "content", b"")
        stream = (bytes(raw),)
    for chunk in stream:
        if not chunk:
            continue
        raw = bytes(chunk)
        retained += len(raw)
        if retained > limit:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            metadata["safety_limit_reached"] = True
            raise AdapterError("Source XML corroborating listing exceeded response size limit")
        chunks.append(raw)
    content = b"".join(chunks)
    if not content:
        raise AdapterError("Source XML corroborating listing was empty")
    if (
        declared is not None
        and not getattr(response, "headers", {}).get("content-encoding")
        and len(content) < declared
    ):
        metadata["response_truncated"] = True
        raise AdapterError("Source XML corroborating listing response was truncated")
    return content


def _normalize_job(node: Any, target: CompanyTarget, publisher: str) -> JobRecord:
    external_id = first_child_text(node, (str(target.id_field),))
    title = first_child_text(node, (str(target.title_field),))
    raw_url = first_child_text(node, (str(target.url_field),))
    missing = [
        name
        for name, value in (
            (str(target.id_field), external_id),
            (str(target.title_field), title),
            (str(target.url_field), raw_url),
        )
        if not value
    ]
    if missing:
        raise AdapterError(
            "Source XML job is missing required " + ", ".join(missing)
        )
    if len(external_id) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", external_id):
        raise AdapterError("Source XML job has an invalid stable requisition ID")
    url = _canonical_official_url(raw_url, target)
    posting_id = external_id
    identity_source = "requisition_id"
    if target.external_id_regex:
        match = static_html._safe_regex(str(target.external_id_regex)).search(url)
        if match is None or not match.group(1):
            raise AdapterError(
                "Source XML canonical job URL is missing its configured posting identity"
            )
        posting_id = match.group(1)
        if len(posting_id) > 200 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]*", posting_id
        ):
            raise AdapterError("Source XML canonical URL has an invalid posting identity")
        query_ids = parse_qs(urlparse(url).query).get("gh_jid", [])
        if query_ids and any(value != posting_id for value in query_ids):
            raise AdapterError("Source XML canonical URL has conflicting posting identities")
        identity_source = "canonical_url"

    location_values: list[str] = []
    configured_locations = tuple(target.location_fields or ())
    if configured_locations:
        direct_location = (
            _child_texts(node, "location")
            if any(str(field).casefold() == "location" for field in configured_locations)
            else []
        )
        if direct_location:
            location_values.extend(direct_location)
        else:
            for field in configured_locations:
                if str(field).casefold() != "location":
                    location_values.extend(_child_texts(node, str(field)))
    elif target.location_field:
        location_values.extend(_child_texts(node, str(target.location_field)))
    else:
        direct_location = _child_texts(node, "location")
        location_values.extend(
            direct_location
            or [
                value
                for field in ("city", "state", "country", "postalcode")
                for value in _child_texts(node, field)
            ]
        )
    locations = _unique(location_values)
    publication = first_child_text(
        node,
        (str(target.date_field),) if target.date_field else PUBLICATION_FIELDS,
    )
    host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    source = f"source_xml:{host}"
    return JobRecord(
        job_id=f"{source}:{posting_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=publication,
        source=source,
        offices=locations,
        raw_metadata={
            "platform": "source_xml",
            "endpoint_variant": target.endpoint_variant,
            "external_job_id": external_id,
            "requisition_id": external_id,
            "posting_id": posting_id,
            "posting_identity_source": identity_source,
            "publisher": publisher,
            "publication_date": publication,
        },
    )


def _canonical_official_url(value: str, target: CompanyTarget) -> str:
    parsed = urlparse(_text(value))
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username
        or parsed.password
        or not static_html._allowed_host(host, target.allowed_apply_hosts)
    ):
        raise AdapterError("Source XML job URL is not HTTPS on an approved host")
    canonical = static_html._canonical_job_url(_text(value))
    canonical_parsed = urlparse(canonical)
    configured = {
        static_html._canonical_job_url(str(url))
        for url in (target.careers_url, target.listing_url, target.endpoint_url)
        if url
    }
    if canonical in configured or canonical_parsed.path.rstrip("/") == "":
        raise AdapterError("Source XML job URL is not a canonical job-specific URL")
    return canonical


def _authoritative_total(
    root: Any, target: CompanyTarget
) -> tuple[int | None, list[dict[str, Any]]]:
    field = str(target.complete_collection_total_field or "").strip()
    if not field:
        return None, []
    nodes = direct_children(root, field)
    evidence: list[dict[str, Any]] = []
    values: list[int] = []
    malformed = False
    for node in nodes:
        raw = _element_text(node)
        value = int(raw) if raw.isascii() and raw.isdigit() else None
        evidence.append(
            {"field": local_name(node), "raw": raw[:100], "value": value}
        )
        if value is None:
            malformed = True
        else:
            values.append(value)
    unique = sorted(set(values))
    if malformed or len(unique) > 1:
        return None, evidence
    return (unique[0] if unique else None), evidence


def _pagination_evidence(
    root: Any,
    target: CompanyTarget,
    total: int | None,
    total_evidence: list[dict[str, Any]],
) -> PaginationEvidence:
    fields: list[str] = []
    malformed: list[str] = []
    conflicts: list[str] = []
    diagnostics: list[tuple[str, str, Any]] = []
    total_values = {
        item["value"] for item in total_evidence if item.get("value") is not None
    }
    if any(item.get("value") is None for item in total_evidence):
        malformed.append("authoritative_total")
    if len(total_values) > 1:
        conflicts.append("authoritative_total")
    if total_evidence:
        fields.append("authoritative_total")
        diagnostics.extend(
            (str(item["field"]), "integer", item.get("value"))
            for item in total_evidence
        )

    has_more_values: list[bool] = []
    next_urls: list[str] = []
    next_page_values: list[int] = []
    page_count_values: list[int] = []
    current_page_values: list[int] = []
    for child in root:
        name = local_name(child).casefold()
        if name == "job":
            continue
        raw = _element_text(child)
        if name in {"hasmore", "has_more"}:
            fields.append("has_more")
            if raw.casefold() in {"true", "1"}:
                has_more_values.append(True)
            elif raw.casefold() in {"false", "0"}:
                has_more_values.append(False)
            else:
                malformed.append("has_more")
        if name in {"next", "nexturl", "next_url"} or (
            name == "link" and str(child.get("rel") or "").casefold() == "next"
        ):
            fields.append("next_url")
            value = str(child.get("href") or raw).strip()
            if not value:
                malformed.append("next_url")
            else:
                resolved = urljoin(str(target.endpoint_url), value)
                parsed = urlparse(resolved)
                if (
                    parsed.scheme != "https"
                    or not static_html._allowed_host(
                        (parsed.hostname or "").casefold(),
                        target.allowed_source_hosts,
                    )
                ):
                    malformed.append("next_url")
                else:
                    next_urls.append(resolved)
        if name in {"nextpage", "next_page"}:
            fields.append("next_page")
            if raw.isascii() and raw.isdigit():
                next_page_values.append(int(raw))
            else:
                malformed.append("next_page")
        if name in {"pagecount", "page_count"}:
            fields.append("page_count")
            if raw.isascii() and raw.isdigit():
                page_count_values.append(int(raw))
            else:
                malformed.append("page_count")
        if name in {"currentpage", "current_page"}:
            fields.append("current_page")
            if raw.isascii() and raw.isdigit():
                current_page_values.append(int(raw))
            else:
                malformed.append("current_page")

    def one(values: list[Any], field: str) -> Any:
        unique = list(dict.fromkeys(values))
        if len(unique) > 1:
            conflicts.append(field)
            return None
        return unique[0] if unique else None

    has_more = one(has_more_values, "has_more")
    next_page = one(next_page_values, "next_page")
    page_count = one(page_count_values, "page_count")
    current_page = one(current_page_values, "current_page")
    if len(set(next_urls)) > 1:
        conflicts.append("next_url")
    next_present = True if next_urls else None
    if has_more is False and (next_present is True or next_page is not None):
        conflicts.append("terminal_and_continuation")
    if has_more is True and total is not None:
        # A complete one-response count cannot simultaneously advertise more.
        diagnostics.append(("has_more", "boolean", True))
    if page_count is not None and current_page is not None:
        if page_count < 1 or current_page < 1 or current_page > page_count:
            malformed.append("page_range")
    return PaginationEvidence(
        reported_total=total,
        has_more=has_more,
        next_url_present=next_present,
        next_page=next_page,
        page_count=page_count,
        current_page=current_page,
        explicit_final_page=bool(
            page_count is not None
            and current_page is not None
            and page_count == current_page
        ),
        fields=tuple(dict.fromkeys(fields)),
        malformed_fields=tuple(dict.fromkeys(malformed)),
        conflicting_fields=tuple(dict.fromkeys(conflicts)),
        field_diagnostics=tuple(diagnostics),
    )


def _child_texts(node: Any, name: str) -> list[str]:
    return [_element_text(child) for child in direct_children(node, name) if _element_text(child)]


def _element_text(node: Any) -> str:
    return " ".join("".join(node.itertext()).split())


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _text(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result
