"""Bounded, configuration-driven collector for server-rendered job lists."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

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
    enforce_pagination_fail_closed,
    finish_authoritative_total,
    html_pagination_evidence,
    pagination_terminal_accepted,
    publish_pagination_evidence,
    record_terminal_evidence,
)


MAX_PAGES = 2
MAX_RESULTS = 20
MAX_ATTEMPTS = 2
MAX_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 3_000_000
MAX_STREAM_CHUNK_BYTES = 64_000
MAX_STRUCTURED_SCRIPTS = 20
MAX_STRUCTURED_SCRIPT_BYTES = 1_000_000
MAX_RECORD_DIAGNOSTICS = 10
MAX_DIAGNOSTIC_CANDIDATES = 5
RETRY_DELAY_SECONDS = 0.1
TRANSIENT_STATUSES = {202, 408, 425, 500, 502, 503, 504}
HEADERS = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"}
SUPPORTED_VARIANTS = {"server_rendered_cards", "server_rendered_links"}
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_ATTR_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_.:-]*)(?:([*^$]?=)[\"']?([^\"']*)[\"']?)?$")
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "gclsrc", "mc_cid", "mc_eid", "ref", "referrer",
    "source", "trk", "tracking_id",
}


@dataclass(frozen=True)
class _ListingRecord:
    scope: Any
    configured_card: Any
    links: tuple[Any, ...]


class _ListingRecordError(AdapterError):
    def __init__(
        self,
        message: str,
        diagnostic: Mapping[str, Any],
        *,
        identity_known: bool,
    ) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic)
        self.identity_known = identity_known


def _failure(code: str, message: str) -> AdapterError:
    return AdapterError(f"Static source {code}: {message}")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 60) -> list[JobRecord]:
    """Fetch a deterministic HTML listing without browser or session state."""
    validate_target(target)
    maximum_pages = min(int(target.max_pages), MAX_PAGES)
    maximum_jobs = min(int(target.max_jobs), MAX_RESULTS)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "pages": [],
        "pages_fetched": 0,
        "bytes_inspected": 0,
        "jobs_normalized": 0,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "stop_reason": "not_started",
        "repeated_page": False,
        "zero_job_contract": False,
        "diagnostics": [],
        "warnings": [],
        "html_truncated": False,
        "structured_contract": None,
    })
    setattr(session, "static_html_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(maximum_pages, maximum_jobs)
    listing_budget.record(metadata)
    explicit_pagination = bool(
        target.pagination_url_template or target.next_page_selector
    )
    request_ceiling = (
        listing_budget.deterministic_terminal_request_ceiling()
        if explicit_pagination
        else 1
    )
    jobs: list[JobRecord] = []
    seen_jobs: dict[str, JobRecord] = {}
    seen_pages: set[tuple[str, ...]] = set()
    next_url = str(target.listing_url)
    raw_records_observed = 0

    for page_index in range(request_ceiling):
        if target.pagination_url_template:
            next_url = str(target.pagination_url_template).format(page=page_index + 1)
        metadata["pages_requested"] += 1
        response = _get(session, next_url, timeout)
        content = _response_bytes(response, target)
        truncated = bool(getattr(response, "_job_radar_html_truncated", False))
        metadata["html_truncated"] = metadata["html_truncated"] or truncated
        metadata["bytes_inspected"] += len(content)
        root = _parse_html(content, response)
        pagination_evidence = html_pagination_evidence(root)
        publish_pagination_evidence(
            metadata,
            pagination_evidence,
            prior_pagination=page_index > 0,
        )
        if pagination_evidence.reported_total is not None:
            metadata["total_available"] = pagination_evidence.reported_total
            metadata["authoritative_total_present"] = True
        structured = _json_ld_contract(root, target, next_url)
        if structured is not None:
            structured_jobs, complete_collection = structured
            effective_jobs = (
                listing_budget.complete_collection_job_ceiling(len(structured_jobs))
                if complete_collection
                else maximum_jobs
            )
            metadata["structured_contract"] = "json_ld_job_posting"
            metadata["pages_completed"] += 1
            metadata["pages_fetched"] = metadata["pages_completed"]
            metadata["jobs_normalized"] = min(len(structured_jobs), effective_jobs)
            metadata["total_available"] = (
                len(structured_jobs) if complete_collection else None
            )
            metadata["zero_job_contract"] = complete_collection and not structured_jobs
            structured_capped = len(structured_jobs) > effective_jobs
            metadata["scan_complete"] = (
                complete_collection and not truncated and not structured_capped
            )
            metadata["scan_completeness_reason"] = (
                "proven_structured_collection"
                if metadata["scan_complete"]
                else (
                    "safety_limit_reached"
                    if structured_capped or truncated
                    else "structured_collection_without_total"
                )
            )
            metadata["stop_reason"] = metadata["scan_completeness_reason"]
            metadata["safety_limit_reached"] = truncated or structured_capped
            return structured_jobs[:effective_jobs]
        if not select(root, str(target.listing_selector)):
            if truncated:
                metadata["safety_limit_reached"] = True
                metadata["stop_reason"] = "html_too_large"
                metadata["scan_completeness_reason"] = "html_too_large"
                raise _failure(
                    "html_too_large",
                    "bounded HTML prefix did not contain a supported structured or listing contract",
                )
            _reject_non_listing(root, content, False)
        listing_containers = select(root, str(target.listing_selector))
        cards = [
            card
            for card in select(root, str(target.job_card_selector))
            if any(_contains(container, card) for container in listing_containers)
        ]
        empty = bool(target.empty_selector and select(root, str(target.empty_selector)))
        if not cards:
            terminal_empty = empty or pagination_evidence.terminal(
                prior_pagination=page_index > 0
            )
            _reject_non_listing(
                root,
                content,
                terminal_empty or (page_index > 0 and explicit_pagination),
                listing_contract_found=True,
            )
            metadata["zero_job_contract"] = empty and page_index == 0
            if empty and page_index == 0:
                if pagination_terminal_accepted(
                    metadata,
                    pagination_evidence,
                    additional_terminal_reason="validated_zero_jobs",
                ):
                    metadata["stop_reason"] = "validated_zero_jobs"
                else:
                    metadata["stop_reason"] = "pagination_evidence_not_terminal"
            elif empty:
                if pagination_terminal_accepted(
                    metadata,
                    pagination_evidence,
                    prior_pagination=page_index > 0,
                    additional_terminal_reason="configured_empty_state",
                ):
                    metadata["stop_reason"] = "validated_terminal_empty_page"
                    record_terminal_evidence(
                        metadata,
                        "configured_empty_state",
                        str(target.empty_selector),
                        has_more=False,
                        terminal_page_observed=True,
                    )
                else:
                    metadata["stop_reason"] = "pagination_evidence_not_terminal"
            elif pagination_terminal_accepted(
                metadata,
                pagination_evidence,
                prior_pagination=page_index > 0,
            ):
                metadata["stop_reason"] = "explicit_terminal_page"
                record_terminal_evidence(
                    metadata,
                    "html_pagination_contract",
                    list(pagination_evidence.fields),
                    has_more=False,
                    terminal_page_observed=True,
                )
            else:
                metadata["stop_reason"] = "empty_page_without_terminal_contract"
            metadata["pages"].append({"page": page_index + 1, "url": next_url, "jobs_returned": 0, "jobs_normalized": 0})
            metadata["pages_fetched"] = len(metadata["pages"])
            metadata["pages_completed"] += 1
            break

        records = _listing_records(listing_containers, cards, target, next_url)
        raw_records_observed += len(records)
        if not records:
            metadata["zero_job_contract"] = page_index == 0
            metadata["stop_reason"] = (
                "validated_zero_jobs"
                if page_index == 0
                else "validated_terminal_empty_page"
            )
            metadata["pages"].append(
                {
                    "page": page_index + 1,
                    "url": next_url,
                    "jobs_returned": 0,
                    "jobs_normalized": 0,
                }
            )
            metadata["pages_fetched"] = len(metadata["pages"])
            metadata["pages_completed"] += 1
            metadata["raw_results_observed"] = raw_records_observed
            record_terminal_evidence(
                metadata,
                "structural_non_job_rows",
                len(cards),
                has_more=False,
                terminal_page_observed=True,
            )
            break
        if not explicit_pagination:
            metadata["total_available"] = len(records)
        effective_jobs = (
            listing_budget.complete_collection_job_ceiling(len(records))
            if not explicit_pagination
            else listing_budget.hard_job_ceiling
        )
        identities = tuple(_record_page_identity(record, target, next_url) for record in records)
        page_record = {"page": page_index + 1, "url": next_url, "jobs_returned": len(records), "jobs_normalized": 0}
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["pages_completed"] += 1
        if identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            break
        seen_pages.add(identities)

        new_ids = 0
        parsed = 0
        for record_index, record in enumerate(records):
            try:
                job = _normalize(record, target, next_url)
            except _ListingRecordError as exc:
                diagnostic = {
                    "page": page_index + 1,
                    "record": record_index + 1,
                    "error": str(exc),
                    **exc.diagnostic,
                }
                _append_record_diagnostic(metadata, diagnostic)
                if not exc.identity_known:
                    metadata["stop_reason"] = "malformed_identity"
                    metadata["scan_completeness_reason"] = "malformed_identity"
                    raise
                metadata["malformed_records"] = int(metadata.get("malformed_records", 0)) + 1
                continue
            except AdapterError as exc:
                _append_record_diagnostic(
                    metadata,
                    {
                        "page": page_index + 1,
                        "record": record_index + 1,
                        "listing_card_selector": str(target.job_card_selector),
                        "listing_container_selector": str(target.listing_selector),
                        "error": str(exc),
                        "reason": str(exc)[:240],
                    },
                )
                continue
            parsed += 1
            previous = seen_jobs.get(job.job_id)
            if previous is not None:
                if _jobs_conflict(previous, job):
                    metadata["stop_reason"] = "conflicting_stable_id"
                    metadata["scan_completeness_reason"] = "conflicting_stable_id"
                    raise _failure(
                        "identity_conflict",
                        f"duplicate stable ID exposed conflicting listing data: {job.job_id}",
                    )
                metadata["equivalent_duplicate_records"] = int(
                    metadata.get("equivalent_duplicate_records", 0)
                ) + 1
                _merge_optional_job_data(previous, job)
                continue
            seen_jobs[job.job_id] = job
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= effective_jobs:
                break
        if parsed == 0:
            if metadata["diagnostics"]:
                raise AdapterError(str(metadata["diagnostics"][0]["error"]))
            raise AdapterError("Static HTML listing contained cards but none satisfied the reviewed contract")
        page_record["jobs_normalized"] = new_ids
        metadata["jobs_normalized"] = len(jobs)
        metadata["raw_results_observed"] = raw_records_observed
        if finish_authoritative_total(
            metadata,
            pagination_evidence.reported_total,
            len(jobs),
            evidence=pagination_evidence,
            prior_pagination=page_index > 0,
        ):
            break
        if pagination_terminal_accepted(
            metadata,
            pagination_evidence,
            prior_pagination=page_index > 0,
        ):
            metadata["stop_reason"] = "explicit_terminal_page"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "validated_html_terminal"
            metadata["total_available"] = len(jobs)
            record_terminal_evidence(
                metadata,
                "html_pagination_contract",
                list(pagination_evidence.fields),
                has_more=False,
                terminal_page_observed=True,
            )
            break
        if new_ids == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            break
        if len(jobs) >= effective_jobs and (
            explicit_pagination or len(records) > effective_jobs
        ):
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if not target.pagination_url_template:
            if not target.next_page_selector:
                metadata["stop_reason"] = "no_pagination_configured"
                break
            links = select(root, str(target.next_page_selector))
            href = links[0].get("href") if links else None
            if not href:
                if pagination_terminal_accepted(
                    metadata,
                    pagination_evidence,
                    prior_pagination=page_index > 0,
                    additional_terminal_reason="configured_next_link_absent",
                ):
                    metadata["stop_reason"] = "no_next_page"
                    record_terminal_evidence(
                        metadata,
                        "configured_next_link_absent",
                        str(target.next_page_selector),
                        has_more=False,
                        terminal_page_observed=True,
                    )
                else:
                    metadata["stop_reason"] = "pagination_evidence_not_terminal"
                break
            record_terminal_evidence(
                metadata,
                "configured_next_link",
                href,
                has_more=True,
            )
            proposed = urljoin(next_url, href)
            _require_listing_host(proposed, target)
            if proposed == next_url:
                metadata["repeated_page"] = True
                metadata["stop_reason"] = "repeated_page"
                break
            next_url = proposed
        if page_index + 1 < request_ceiling:
            time.sleep(RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    complete_reasons = {
        "validated_zero_jobs",
        "validated_terminal_empty_page",
        "explicit_terminal_page",
        "no_next_page",
        "reported_total_reached",
    }
    metadata["scan_complete"] = (
        metadata["stop_reason"] in complete_reasons
        and not metadata["html_truncated"]
        and not metadata.get("malformed_records")
    )
    metadata["scan_completeness_reason"] = (
        "validated_html_terminal"
        if metadata["scan_complete"]
        else (
            "bounded_html_prefix_only"
            if metadata["html_truncated"]
            else (
                "malformed_listing_records"
                if metadata.get("malformed_records")
                else metadata["stop_reason"]
            )
        )
    )
    metadata["safety_limit_reached"] = bool(
        metadata["safety_limit_reached"]
        or metadata["html_truncated"]
        or metadata["stop_reason"] in {"maximum_pages_reached", "maximum_results_reached"}
    )
    if metadata["scan_complete"]:
        metadata["total_available"] = len(jobs)
    elif metadata["safety_limit_reached"]:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
    enforce_pagination_fail_closed(metadata)
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [name for name, value in (
        ("listing_url", target.listing_url), ("endpoint_variant", target.endpoint_variant),
        ("listing_selector", target.listing_selector), ("job_card_selector", target.job_card_selector),
        ("title_selector", target.title_selector), ("detail_link_selector", target.detail_link_selector),
        ("allowed_apply_hosts", target.allowed_apply_hosts),
    ) if not value]
    if missing:
        raise AdapterError(f"Static HTML target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"Static HTML unsupported variant: {target.endpoint_variant}")
    _require_https(str(target.listing_url), "listing_url")
    if target.pagination_url_template:
        if str(target.pagination_url_template).count("{page}") != 1:
            raise AdapterError("Static HTML pagination_url_template must contain one {page} placeholder")
        _require_https(str(target.pagination_url_template).replace("{page}", "1"), "pagination_url_template")
    for value in (target.listing_selector, target.job_card_selector, target.title_selector,
                  target.location_selector, target.detail_link_selector, target.date_selector,
                  target.department_selector, target.next_page_selector, target.empty_selector,
                  target.external_id_selector, target.job_attribution_selector):
        if value:
            selector_to_xpath(str(value))
    if target.fixed_location and len(str(target.fixed_location)) > 200:
        raise AdapterError("Static HTML fixed_location exceeds the hard length limit")
    if target.external_id_attribute and not _ATTR_RE.fullmatch(str(target.external_id_attribute)):
        raise AdapterError("Static HTML external_id_attribute is invalid")
    if target.reference_url_attribute and not _ATTR_RE.fullmatch(str(target.reference_url_attribute)):
        raise AdapterError("Static HTML reference_url_attribute is invalid")
    if target.external_id_regex:
        _safe_regex(str(target.external_id_regex))
    if (not target.external_id_attribute and not target.external_id_selector
            and not target.external_id_regex and not target.allow_url_id_fallback):
        raise AdapterError("Static HTML target requires an identifier extraction contract")
    if bool(target.job_attribution_selector) != bool(target.job_attribution_regex):
        raise AdapterError(
            "Static HTML per-card attribution requires both selector and regex"
        )
    if target.job_attribution_regex:
        _safe_regex(str(target.job_attribution_regex))
    if target.job_attribution_exclude_regex:
        _safe_regex(str(target.job_attribution_exclude_regex))
    _validate_hosts(target.allowed_apply_hosts)
    if not 1 <= int(target.max_pages) <= MAX_PAGES or not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Static HTML page or result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Static HTML response byte limit exceeds the hard maximum")


def select(node: Any, selector: str) -> list[Any]:
    if selector == ":self":
        return [node]
    return list(node.xpath(selector_to_xpath(selector)))


def selector_to_xpath(selector: str) -> str:
    """Compile a deliberately small, non-executable CSS selector subset."""
    text = " ".join(str(selector).split())
    if text == ":self":
        return "."
    selector_groups = [item.strip() for item in text.split(",")]
    if not text or len(selector_groups) > 8 or any(not item for item in selector_groups):
        raise AdapterError("Static HTML selector uses an unsupported construct")
    compiled_groups = []
    for group in selector_groups:
        parts = group.split(" ")
        without_supported_pseudo = " ".join(
            re.sub(r":(?:first-child|last-child|nth-child\([1-9][0-9]*\))$", "", part)
            for part in parts
        )
        if any(token in without_supported_pseudo for token in (">", "(", ")", "|", "\\")):
            raise AdapterError("Static HTML selector uses an unsupported construct")
        compiled_groups.append(".//" + "//".join(_simple_selector(part) for part in parts))
    return " | ".join(compiled_groups)


def _simple_selector(part: str) -> str:
    pseudo = None
    pseudo_match = re.search(r":(first-child|last-child|nth-child\(([1-9][0-9]*)\))$", part)
    if pseudo_match:
        pseudo = pseudo_match.group(1)
        part = part[:pseudo_match.start()]
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*|\*)", part)
    tag = tag_match.group(1) if tag_match else "*"
    rest = part[tag_match.end():] if tag_match else part
    predicates: list[str] = []
    while rest:
        if rest.startswith(".") or rest.startswith("#"):
            marker, rest = rest[0], rest[1:]
            match = re.match(r"[A-Za-z0-9_-]+", rest)
            if not match:
                raise AdapterError("Static HTML selector token is invalid")
            value, rest = match.group(0), rest[match.end():]
            if marker == ".":
                predicates.append(f"contains(concat(' ', normalize-space(@class), ' '), ' {value} ')")
            else:
                predicates.append(f"@id={_xpath_literal(value)}")
        elif rest.startswith("["):
            end = rest.find("]")
            if end < 0:
                raise AdapterError("Static HTML selector attribute is unterminated")
            expression, rest = rest[1:end], rest[end + 1:]
            match = _ATTR_RE.fullmatch(expression)
            if not match:
                raise AdapterError("Static HTML selector attribute is invalid")
            name, operator, value = match.groups()
            if operator is None:
                predicates.append(f"@{name}")
            elif operator == "=":
                predicates.append(f"@{name}={_xpath_literal(value)}")
            elif operator == "*=":
                predicates.append(f"contains(@{name}, {_xpath_literal(value)})")
            elif operator == "^=":
                predicates.append(f"starts-with(@{name}, {_xpath_literal(value)})")
            else:
                predicates.append(f"substring(@{name}, string-length(@{name}) - {len(value) - 1})={_xpath_literal(value)}")
        else:
            raise AdapterError("Static HTML selector token is invalid")
    if pseudo == "first-child":
        predicates.append("not(preceding-sibling::*)")
    elif pseudo == "last-child":
        predicates.append("not(following-sibling::*)")
    elif pseudo:
        predicates.append(f"count(preceding-sibling::*)={int(pseudo_match.group(2)) - 1}")
    return tag + "".join(f"[{item}]" for item in predicates)


def _xpath_literal(value: str) -> str:
    if "'" in value or '"' in value:
        raise AdapterError("Static HTML selector values cannot contain quotes")
    return f"'{value}'"


def _get(session: requests.Session, url: str, timeout: int) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, headers=HEADERS, timeout=timeout, stream=True)
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
            raise AdapterError(f"Static HTML authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise _failure("access_blocked", f"HTTP {status}")
        if status != 200:
            raise AdapterError(f"Static HTML HTTP {status}")
        return response
    raise AdapterError("Static HTML request attempts exhausted")


def _response_bytes(response: Any, target: CompanyTarget) -> bytes:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Static HTML returned a non-HTML response")
    limit = int(target.max_response_bytes)
    truncated = False
    iterator = getattr(response, "iter_content", None)
    chunk_iterator = None
    if callable(iterator):
        try:
            chunk_iterator = iter(iterator(chunk_size=MAX_STREAM_CHUNK_BYTES))
        except TypeError:
            chunk_iterator = None
    if chunk_iterator is not None:
        chunks: list[bytes] = []
        retained = 0
        for chunk in chunk_iterator:
            if not chunk:
                continue
            raw = bytes(chunk)
            remaining = limit + 1 - retained
            if remaining > 0:
                chunks.append(raw[:remaining])
                retained += min(len(raw), remaining)
            if retained > limit:
                truncated = True
                break
        content = b"".join(chunks)[:limit]
        if truncated:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    else:
        raw_content = getattr(response, "content", None)
        if raw_content is None:
            raw_content = str(getattr(response, "text", "") or "").encode("utf-8")
        raw_content = bytes(raw_content)
        truncated = len(raw_content) > limit
        content = raw_content[:limit]
    setattr(response, "_job_radar_html_truncated", truncated)
    if not content:
        raise AdapterError("Static HTML returned an empty HTTP 200 response")
    return bytes(content)


def _parse_html(content: bytes, response: Any) -> Any:
    try:
        return html.fromstring(content, base_url=getattr(response, "url", None))
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Static HTML malformed HTTP 200 response: {exc}") from exc


def _reject_non_listing(
    root: Any,
    content: bytes,
    valid_empty: bool,
    *,
    listing_contract_found: bool = False,
) -> None:
    if valid_empty:
        return
    text = " ".join(root.text_content().split()).casefold()
    title = " ".join(root.xpath("string(//title)").split()).casefold()
    if any(token in text for token in ("cloudflare challenge", "access denied", "request blocked", "captcha")):
        raise _failure("access_blocked", "blocked or challenge response")
    if "consent required" in text or "cookie consent required" in text:
        raise AdapterError("Static HTML consent page is unsupported")
    if "enable javascript to continue" in text or "javascript is required" in text:
        raise AdapterError("Static HTML browser required")
    if re.search(r"\b(session required|session expired)\b", title):
        raise AdapterError("Static HTML session required")
    if re.search(r"\b(sign in|log in|login)\b", title):
        raise AdapterError("Static HTML authentication required")
    if listing_contract_found:
        raise _failure(
            "unproven_empty_collection",
            "HTTP 200 listing container had no jobs and no explicit empty contract",
        )
    raise _failure(
        "unsupported_listing_contract",
        "HTTP 200 response did not contain a supported listing contract",
    )


def _json_ld_contract(
    root: Any, target: CompanyTarget, page_url: str
) -> Optional[tuple[list[JobRecord], bool]]:
    """Return bounded JSON-LD JobPosting records and collection proof when present."""
    scripts = root.xpath(
        "//script[translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='application/ld+json']"
    )
    if not scripts:
        return None
    if len(scripts) > min(int(target.max_scripts), MAX_STRUCTURED_SCRIPTS):
        raise _failure(
            "structured_payload_malformed", "JSON-LD script-count bound exceeded"
        )

    postings: list[Mapping[str, Any]] = []
    complete_collection = False
    for script in scripts:
        if script.get("src"):
            raise _failure(
                "structured_payload_malformed", "external JSON-LD scripts are unsupported"
            )
        body = script.text or ""
        if len(body.encode("utf-8")) > min(
            int(target.max_script_bytes), MAX_STRUCTURED_SCRIPT_BYTES
        ):
            raise _failure(
                "structured_payload_malformed", "JSON-LD script byte bound exceeded"
            )
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise _failure(
                "structured_payload_malformed", "JSON-LD was not valid JSON"
            ) from exc
        found, collection_proven = _json_ld_postings(payload)
        postings.extend(found)
        complete_collection = complete_collection or collection_proven

    if not postings and not complete_collection:
        return None
    jobs: list[JobRecord] = []
    seen: dict[str, JobRecord] = {}
    for posting in postings:
        job = _normalize_json_ld(posting, target, page_url)
        previous = seen.get(job.job_id)
        if previous is not None:
            if _jobs_conflict(previous, job):
                raise _failure(
                    "identity_conflict",
                    f"duplicate structured stable ID exposed conflicting data: {job.job_id}",
                )
            _merge_optional_job_data(previous, job)
            continue
        seen[job.job_id] = job
        jobs.append(job)
    return jobs, complete_collection


def _json_ld_postings(payload: Any) -> tuple[list[Mapping[str, Any]], bool]:
    postings: list[Mapping[str, Any]] = []
    complete = False
    stack: list[Any] = [payload]
    visited = 0
    while stack:
        value = stack.pop()
        visited += 1
        if visited > 10_000:
            raise _failure(
                "structured_payload_malformed", "JSON-LD node bound exceeded"
            )
        if isinstance(value, list):
            stack.extend(reversed(value))
            continue
        if not isinstance(value, Mapping):
            continue
        type_value = value.get("@type")
        types = type_value if isinstance(type_value, list) else [type_value]
        normalized_types = {str(item).casefold() for item in types if item is not None}
        if "jobposting" in normalized_types:
            postings.append(value)
            continue
        if "itemlist" in normalized_types and "itemListElement" in value:
            elements = value.get("itemListElement")
            count = value.get("numberOfItems")
            if not isinstance(elements, list) or isinstance(count, bool) or not isinstance(count, int):
                raise _failure(
                    "structured_payload_malformed",
                    "JSON-LD ItemList lacked a numeric count and list",
                )
            if count != len(elements):
                raise _failure(
                    "structured_payload_malformed", "JSON-LD ItemList count conflicted"
                )
            complete = True
            stack.extend(reversed(elements))
            continue
        stack.extend(reversed(list(value.values())))
    return postings, complete


def _normalize_json_ld(
    item: Mapping[str, Any], target: CompanyTarget, page_url: str
) -> JobRecord:
    title_values = _unique(
        str(item.get(field) or "") for field in ("title", "name") if item.get(field)
    )
    if len(title_values) != 1:
        raise _failure("identity_missing", "JSON-LD JobPosting title is missing or ambiguous")
    raw_url = item.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise _failure("identity_missing", "JSON-LD JobPosting has no canonical URL")
    url = _official_url(urljoin(page_url, raw_url), target)

    structured_ids = _json_ld_identifier_values(item.get("identifier"))
    url_ids: list[str] = []
    if target.external_id_regex:
        match = _safe_regex(str(target.external_id_regex)).search(url)
        if match:
            url_ids.append(match.group(1) if match.groups() else match.group(0))
    identities = _unique(structured_ids + url_ids)
    if len(identities) > 1:
        raise _failure("identity_missing", "JSON-LD JobPosting identities conflicted")
    if identities:
        external_id = identities[0]
    else:
        canonical = _canonical_job_url(url)
        external_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    url = _canonical_job_url(url)

    locations = _json_ld_locations(item.get("jobLocation"))
    remote = str(item.get("jobLocationType") or "").strip()
    if remote:
        locations = _unique([*locations, remote])
    departments = _json_ld_text_values(item.get("occupationalCategory"))
    date_posted = str(item.get("datePosted") or "").strip()
    host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    source_namespace = str(target.slug or host).strip().casefold()
    return JobRecord(
        job_id=f"static_html:{source_namespace}:{external_id}",
        title=title_values[0],
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=date_posted,
        source=f"static_html:{source_namespace}",
        departments=departments,
        offices=locations,
        raw_metadata={
            "platform": "static_html",
            "endpoint_variant": "json_ld_job_posting",
            "external_job_id": external_id,
            "work_modes": [
                value
                for value in locations
                if re.search(r"(?i)\b(remote|hybrid|telecommute)\b", value)
            ],
        },
    )


def _json_ld_identifier_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, Mapping):
            item = item.get("value")
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            raise _failure(
                "structured_payload_malformed", "JSON-LD identifier was malformed"
            )
        text = str(item).strip()
        if not text or len(text) > 200 or not re.fullmatch(r"[A-Za-z0-9._:-]+", text):
            raise _failure(
                "structured_payload_malformed", "JSON-LD identifier was malformed"
            )
        result.append(text)
    return _unique(result)


def _json_ld_locations(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    locations: list[str] = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, str):
            locations.append(item)
            continue
        if not isinstance(item, Mapping):
            raise _failure(
                "structured_payload_malformed", "JSON-LD jobLocation was malformed"
            )
        address = item.get("address", item)
        if isinstance(address, str):
            locations.append(address)
            continue
        if not isinstance(address, Mapping):
            raise _failure(
                "structured_payload_malformed", "JSON-LD address was malformed"
            )
        parts = [
            str(address.get(field) or "").strip()
            for field in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
        ]
        display = ", ".join(part for part in parts if part)
        if display:
            locations.append(display)
    return _unique(locations)


def _json_ld_text_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return _unique(
        str(item.get("name") or item.get("value") or "")
        if isinstance(item, Mapping)
        else str(item or "")
        for item in values
    )


def _listing_records(
    listing_containers: list[Any],
    configured_cards: list[Any],
    target: CompanyTarget,
    page_url: str,
) -> list[_ListingRecord]:
    """Bind canonical links to the smallest useful record inside a listing."""
    link_origins: list[tuple[Any, Any]] = []
    missing_link_cards: list[Any] = []
    ambiguous_link_cards: list[_ListingRecord] = []
    seen_links: set[int] = set()
    for card in configured_cards:
        links = select(card, str(target.detail_link_selector))
        if not links:
            if not _is_structural_non_job_card(card, target):
                missing_link_cards.append(card)
            continue
        link_keys = _unique(
            _candidate_link_key(link, target, page_url) for link in links
        )
        if len(link_keys) > 1 and not any(
            card is container for container in listing_containers
        ):
            ambiguous_link_cards.append(_ListingRecord(card, card, tuple(links)))
            continue
        for link in links:
            marker = id(link)
            if marker not in seen_links:
                seen_links.add(marker)
                link_origins.append((link, card))

    all_links = [link for link, _card in link_origins]
    records: list[_ListingRecord] = list(ambiguous_link_cards)
    seen_records: set[tuple[int, tuple[int, ...]]] = set()
    for link, configured_card in link_origins:
        containers = [container for container in listing_containers if _contains(container, link)]
        if not containers:
            continue
        boundary = max(containers, key=_node_depth)
        link_key = _candidate_link_key(link, target, page_url)
        configured_card_keys = {
            _candidate_link_key(candidate, target, page_url)
            for candidate in all_links
            if _contains(configured_card, candidate)
        }
        configured_card_keys.discard("")
        narrow_link_contract = boundary is link and configured_card is link
        if narrow_link_contract:
            boundary = link.getroottree().getroot()
        elif configured_card is not link and len(configured_card_keys) <= 1:
            boundary = configured_card
        scope = link
        node = link
        configured_title_found = _scope_has_configured_title(scope, target)
        while node is not boundary and not (narrow_link_contract and configured_title_found):
            parent = node.getparent()
            if parent is None or not _contains(boundary, parent):
                break
            parent_keys = {
                _candidate_link_key(candidate, target, page_url)
                for candidate in all_links
                if _contains(parent, candidate)
            }
            parent_keys.discard("")
            if parent_keys - {link_key}:
                break
            scope = parent
            node = parent
            configured_title_found = _scope_has_configured_title(scope, target)
        grouped_links = tuple(
            candidate
            for candidate in all_links
            if _contains(scope, candidate)
            and _candidate_link_key(candidate, target, page_url) == link_key
        ) or (link,)
        marker = (id(scope), tuple(id(candidate) for candidate in grouped_links))
        if marker not in seen_records:
            seen_records.add(marker)
            records.append(_ListingRecord(scope, configured_card, grouped_links))

    records.extend(_ListingRecord(card, card, ()) for card in missing_link_cards)
    return records


def _is_structural_non_job_card(card: Any, target: CompanyTarget) -> bool:
    """Return true only when a linkless configured card has no job signal."""
    if _scope_has_configured_title(card, target):
        return False
    semantic_markers = {
        "job",
        "job-card",
        "job_title",
        "job-title",
        "opening",
        "position",
        "requisition",
        "role",
        "title",
        "vacancy",
    }
    if any(
        marker.casefold() in semantic_markers
        for node in card.iter()
        for attribute in ("class", "id")
        for marker in str(node.get(attribute) or "").split()
    ):
        return False
    if target.external_id_attribute:
        attribute = str(target.external_id_attribute)
        if any(str(node.get(attribute) or "").strip() for node in card.iter()):
            return False
    if target.external_id_selector and any(
        _normalized_text(_text(node))
        for node in select(card, str(target.external_id_selector))
    ):
        return False
    if target.external_id_regex:
        pattern = _safe_regex(str(target.external_id_regex))
        evidence = [_text(card)]
        evidence.extend(
            str(value)
            for node in card.iter()
            for value in node.attrib.values()
            if str(value).strip()
        )
        if any(pattern.search(value) for value in evidence):
            return False
    if target.allow_url_id_fallback:
        attribute = str(target.reference_url_attribute or "href")
        for node in card.iter():
            reference = str(node.get(attribute) or "").strip()
            if reference and _generic_url_identity(_canonical_job_url(reference)):
                return False
    return True


def _scope_has_configured_title(scope: Any, target: CompanyTarget) -> bool:
    if str(target.title_selector) == ":self":
        return bool(_normalized_text(_text(scope)))
    return any(
        _normalized_text(_text(node))
        for node in select(scope, str(target.title_selector))
    )


def _candidate_link_key(node: Any, target: CompanyTarget, page_url: str) -> str:
    reference = _link_reference(node, target)
    if not reference:
        return ""
    return _canonical_job_url(urljoin(page_url, reference))


def _record_page_identity(
    record: _ListingRecord, target: CompanyTarget, page_url: str
) -> str:
    links = _unique(
        _candidate_link_key(node, target, page_url) for node in record.links
    )
    if links:
        return "|".join(links)
    return _text(record.scope)[:200]


def _contains(ancestor: Any, node: Any) -> bool:
    current = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.getparent()
    return False


def _node_depth(node: Any) -> int:
    depth = 0
    while node is not None:
        depth += 1
        node = node.getparent()
    return depth


def _normalize(record: Any, target: CompanyTarget, page_url: str) -> JobRecord:
    if not isinstance(record, _ListingRecord):
        card = record
        record = _ListingRecord(
            card,
            card,
            tuple(select(card, str(target.detail_link_selector))),
        )
    scope = record.scope
    _require_card_attribution(scope, target)

    diagnostic: dict[str, Any] = {
        "candidate_title_count": 0,
        "normalized_equivalent_title_count": 0,
        "candidate_link_count": len(record.links),
        "stable_identity_candidates": [],
        "listing_card_selector": str(target.job_card_selector),
        "listing_container_selector": str(target.listing_selector),
    }
    try:
        link_urls = _unique(
            _canonical_job_url(
                _official_url(urljoin(page_url, _link_reference(node, target)), target)
            )
            for node in record.links
            if _link_reference(node, target)
        )
    except (AdapterError, ValueError) as exc:
        diagnostic["reason"] = "canonical_link_invalid"
        raise _ListingRecordError(
            str(exc), diagnostic, identity_known=False
        ) from exc
    if len(link_urls) != 1:
        diagnostic["reason"] = (
            "canonical_link_missing" if not link_urls else "canonical_link_ambiguous"
        )
        raise _ListingRecordError(
            "Static source identity_missing: listing record did not bind one canonical job URL",
            diagnostic,
            identity_known=False,
        )
    url = link_urls[0]

    explicit_title_nodes = (
        list(record.links)
        if str(target.title_selector) == ":self"
        else select(scope, str(target.title_selector))
    )
    title_levels = [
        [_text(node) for node in explicit_title_nodes],
        [_text(node) for node in record.links],
        [
            str(node.get(attribute) or "")
            for node in record.links
            for attribute in ("data-job-title", "aria-label", "title")
        ],
    ]

    identity_candidates: list[str] = []
    if target.external_id_attribute:
        for node in (record.configured_card, scope):
            value = str(node.get(str(target.external_id_attribute)) or "").strip()
            if value:
                identity_candidates.append(value)
    if target.external_id_selector:
        selector_identities = _unique(
            _text(node) for node in select(scope, str(target.external_id_selector))
        )
        if len(selector_identities) != 1:
            diagnostic["stable_identity_candidates"] = selector_identities[
                :MAX_DIAGNOSTIC_CANDIDATES
            ]
            diagnostic["reason"] = "configured_stable_identity_selector_not_unique"
            raise _ListingRecordError(
                "Static HTML stable identifier selector must match exactly once per card",
                diagnostic,
                identity_known=False,
            )
        identity_candidates.extend(selector_identities)
    if target.external_id_regex:
        match = _safe_regex(str(target.external_id_regex)).search(url)
        value = match.group(1) if match and match.groups() else match.group(0) if match else ""
        if value:
            identity_candidates.append(value)
    identities = _unique(identity_candidates)
    configured_identity_contract = bool(
        target.external_id_attribute
        or target.external_id_selector
        or target.external_id_regex
    )
    if not identities and not configured_identity_contract:
        generic_url_id = _generic_url_identity(url)
        if generic_url_id:
            identities.append(generic_url_id)
    diagnostic["stable_identity_candidates"] = identities[:MAX_DIAGNOSTIC_CANDIDATES]
    if len(identities) > 1:
        diagnostic["reason"] = "conflicting_stable_identity_candidates"
        raise _ListingRecordError(
            "Static source identity_missing: job exposed conflicting stable identifiers",
            diagnostic,
            identity_known=False,
        )
    external_id = identities[0] if identities else ""
    if not external_id and target.allow_url_id_fallback:
        external_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    title = ""
    for level_name, candidates in zip(
        ("configured_title_selector", "canonical_link_text", "structured_link_attribute"),
        title_levels,
    ):
        normalized = _unique(candidates)
        if not normalized:
            continue
        diagnostic["candidate_title_count"] = len(
            [value for value in candidates if _normalized_text(value)]
        )
        diagnostic["normalized_equivalent_title_count"] = len(normalized)
        diagnostic["title_precedence"] = level_name
        if len(normalized) > 1:
            diagnostic["reason"] = "conflicting_titles_within_precedence"
            raise _ListingRecordError(
                "Static source identity_missing: title selector was ambiguous",
                diagnostic,
                identity_known=bool(external_id),
            )
        title = normalized[0]
        break
    if not title and external_id:
        title = _canonical_url_title(url, external_id)
        if title:
            diagnostic["title_precedence"] = "canonical_url_slug"
    if not title or not external_id:
        diagnostic["reason"] = "title_missing" if external_id else "stable_identity_missing"
        raise _ListingRecordError(
            "Static source identity_missing: job is missing a title or stable identifier",
            diagnostic,
            identity_known=bool(external_id),
        )

    locations = _texts(scope, target.location_selector)
    if not locations and target.fixed_location:
        locations = _unique([str(target.fixed_location)])
    departments = _texts(scope, target.department_selector)
    dates = _texts(scope, target.date_selector)
    work_modes = [value for value in locations if re.search(r"(?i)\b(remote|hybrid)\b", value)]
    host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    # Some hosted ATS products (notably Jobvite) serve many unrelated company
    # boards from one hostname.  A reviewed slug gives those boards separate
    # source namespaces while preserving the historical host-only contract for
    # every existing static target.
    source_namespace = str(target.slug or host).strip().casefold()
    return JobRecord(
        job_id=f"static_html:{source_namespace}:{external_id}", title=title, company=target.name,
        location=" | ".join(locations), url=url, updated_at=dates[0] if dates else "",
        source=f"static_html:{source_namespace}", departments=departments, offices=locations,
        raw_metadata={"platform": "static_html", "endpoint_variant": target.endpoint_variant,
                      "external_job_id": external_id, "work_modes": work_modes,
                      "listing_resolution": diagnostic},
    )


def _canonical_url_title(url: str, external_id: str) -> str:
    """Read a title only from a canonical ``title-stable-id`` path segment."""
    segment = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    suffix = f"-{external_id}"
    if not segment.casefold().endswith(suffix.casefold()):
        return ""
    slug = segment[: -len(suffix)]
    if not slug or len(slug) > 240 or not re.fullmatch(r"[\w%+.-]+", slug):
        return ""
    words = " ".join(part for part in re.split(r"[-_+]+", slug) if part)
    return words[:1].upper() + words[1:] if words else ""


def _canonical_job_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    host = (parsed.hostname or "").casefold()
    netloc = host
    if parsed.port and not (
        (parsed.scheme.casefold() == "https" and parsed.port == 443)
        or (parsed.scheme.casefold() == "http" and parsed.port == 80)
    ):
        netloc = f"{host}:{parsed.port}"
    kept_query = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc=netloc,
            path=path,
            query=urlencode(kept_query, doseq=True),
            fragment="",
        )
    )


def _generic_url_identity(url: str) -> str:
    """Return a conservative numeric/alphanumeric ID from a job URL suffix."""
    path = unquote(urlparse(url).path).rstrip("/")
    if not path:
        return ""
    segment = path.rsplit("/", 1)[-1]
    candidates = [*reversed(re.split(r"[-_]", segment)), segment]
    for candidate in candidates:
        value = candidate.strip()
        if not 3 <= len(value) <= 100 or not re.fullmatch(r"[A-Za-z0-9.:-]+", value):
            continue
        if value.isdigit() or (re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)):
            return value
    return ""


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip(" |").casefold()


def _append_record_diagnostic(
    metadata: SourceScanMetadata, diagnostic: Mapping[str, Any]
) -> None:
    diagnostics = metadata.setdefault("diagnostics", [])
    if len(diagnostics) < MAX_RECORD_DIAGNOSTICS:
        diagnostics.append(dict(diagnostic))
    else:
        metadata["diagnostics_dropped"] = int(metadata.get("diagnostics_dropped", 0)) + 1


def _jobs_conflict(existing: JobRecord, candidate: JobRecord) -> bool:
    comparisons = (
        (_normalized_text(existing.title), _normalized_text(candidate.title)),
        (_canonical_job_url(existing.url), _canonical_job_url(candidate.url)),
        (_normalized_text(existing.location), _normalized_text(candidate.location)),
        (_normalized_text(existing.updated_at), _normalized_text(candidate.updated_at)),
    )
    return any(left and right and left != right for left, right in comparisons)


def _merge_optional_job_data(existing: JobRecord, candidate: JobRecord) -> None:
    if not existing.location:
        existing.location = candidate.location
    if not existing.updated_at:
        existing.updated_at = candidate.updated_at
    existing.departments = _unique([*existing.departments, *candidate.departments])
    existing.offices = _unique([*existing.offices, *candidate.offices])


def _texts(card: Any, selector: Optional[str]) -> list[str]:
    if not selector:
        return []
    return _unique(_text(node) for node in select(card, selector))


def _text(node: Any) -> str:
    return " ".join(node.text_content().split())


def _card_identity(card: Any, target: CompanyTarget) -> str:
    links = select(card, str(target.detail_link_selector))
    reference = _link_reference(links[0], target) if links else ""
    if reference:
        return reference
    if target.external_id_attribute and card.get(str(target.external_id_attribute)):
        return str(card.get(str(target.external_id_attribute)))
    if target.external_id_selector:
        nodes = select(card, str(target.external_id_selector))
        if len(nodes) == 1:
            return _text(nodes[0])
    return _text(card)[:200]


def _require_card_attribution(card: Any, target: CompanyTarget) -> None:
    """Fail closed when a mixed-brand listing card is not the configured brand."""
    if not target.job_attribution_selector:
        return
    values = _texts(card, target.job_attribution_selector)
    if len(values) != 1:
        raise AdapterError(
            "Static HTML attribution selector must match exactly once per card"
        )
    include = _safe_regex(str(target.job_attribution_regex))
    exclude = (
        _safe_regex(str(target.job_attribution_exclude_regex))
        if target.job_attribution_exclude_regex else None
    )
    if not include.fullmatch(values[0]) or (exclude and exclude.search(values[0])):
        raise AdapterError("Static HTML job failed exact per-card attribution")


def _official_url(url: str, target: CompanyTarget) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    listing_host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    if parsed.scheme == "http" and host == listing_host and _allowed_host(host, target.allowed_apply_hosts):
        parsed = parsed._replace(scheme="https")
    if parsed.scheme != "https" or not _allowed_host(host, target.allowed_apply_hosts):
        raise AdapterError("Static HTML job has no official allowed-host apply URL")
    return urlunparse(parsed._replace(fragment=""))


def _link_reference(node: Any, target: CompanyTarget) -> str:
    attribute = str(target.reference_url_attribute or "href")
    return str(node.get(attribute) or "").strip()


def _require_listing_host(url: str, target: CompanyTarget) -> None:
    expected = (urlparse(str(target.listing_url)).hostname or "").casefold()
    actual = (urlparse(url).hostname or "").casefold()
    if actual != expected:
        raise AdapterError("Static HTML pagination left the reviewed listing host")


def _require_https(url: str, field: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise AdapterError(f"Static HTML {field} must be an anonymous HTTPS URL")
    if any(token in parsed.path.casefold() for token in ("/login", "/signin", "/session")):
        raise AdapterError(f"Static HTML {field} cannot require authentication or session state")


def _safe_regex(value: str) -> re.Pattern[str]:
    if len(value) > 240 or any(token in value for token in ("(?=", "(?<", "(?!", "(?P", "\\1", "\\g")):
        raise AdapterError("Static HTML identifier regex uses an unsafe construct")
    try:
        return re.compile(value)
    except re.error as exc:
        raise AdapterError(f"Static HTML identifier regex is invalid: {exc}") from exc


def _validate_hosts(hosts: Iterable[str]) -> None:
    for host in hosts:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", str(host)):
            raise AdapterError("Static HTML allowed_apply_hosts contains an invalid host")


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip(" |")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
