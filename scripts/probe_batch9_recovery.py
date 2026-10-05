#!/usr/bin/env python3
"""Bounded, read-only source diagnostics for the five Batch 9 recovery targets.

This utility is intentionally narrower than the normal scanner.  It accepts
only an explicit allowlist, never loads any other company into an execution
plan, counts redirects against a per-company request ceiling, and writes only
sanitized diagnostic artifacts under ``output/batch9_recovery``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.adapters import smartrecruiters, static_html
from src.scraper.company_careers.listing_completion import html_pagination_evidence
from src.scraper.company_careers.models import AdapterError, CompanyTarget, SourceScanMetadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.xml_security import (
    direct_children as _direct_children,
    first_child_text as _first_child_text,
    local_name as _local_name,
    safe_xml_root as _safe_xml_root,
)


ALLOWED_COMPANIES = (
    "Bosch",
    "SGS",
    "General Motors",
    "Caterpillar",
    "Coupang",
)
SMARTRECRUITERS_COMPANIES = frozenset({"Bosch", "SGS"})
STATIC_COMPANIES = frozenset({"General Motors", "Caterpillar", "Coupang"})
PROBE_MODES = (
    "listing",
    "xml",
    "smartrecruiters-partition",
    "smartrecruiters-reconcile",
)
OUTPUT_ROOT = ROOT / "output" / "batch9_recovery"
DEFAULT_REQUEST_LIMIT = 3
MAX_REQUEST_LIMIT = 8
DEFAULT_TIMEOUT = 15
MAX_TIMEOUT = 30
MAX_STATIC_RESPONSE_BYTES = 3_000_000
MAX_JSON_RESPONSE_BYTES = 5_000_000
MAX_XML_RESPONSE_BYTES = 2_000_000
MAX_XML_RESPONSE_OVERRIDE_BYTES = 8_000_000
XML_INITIAL_INSPECTION_BYTES = 4_096
MAX_XML_REQUESTS = 2
MAX_RESUME_ARTIFACT_BYTES = 12_000_000
MAX_FIXTURE_RECORDS = 10
MAX_REJECTION_SAMPLES = 10
STREAM_CHUNK_BYTES = 64 * 1024
USER_AGENT = "job-radar-batch9-probe/1.0 (read-only bounded diagnostic)"
SENSITIVE_QUERY_NAMES = frozenset(
    {"access_token", "api_key", "apikey", "authorization", "cookie", "key", "password", "secret", "session", "token"}
)
BLOCKED_MARKERS = (
    "access denied",
    "captcha",
    "challenge-platform",
    "verify you are human",
    "sign in to continue",
)
XML_IDENTITY_PATTERNS = {
    "General Motors": r"(?:General Motors|\bGM\b)",
    "Caterpillar": r"\bCaterpillar\b",
    "Coupang": r"\bCoupang\b",
}
SMARTRECRUITERS_PARTITION_DIMENSIONS = frozenset({"country", "department"})
SMARTRECRUITERS_POSTING_DOC = (
    "https://developers.smartrecruiters.com/reference/v1listpostings"
)


class RequestCeilingReached(AdapterError):
    """Raised before a request would exceed a company's hard ceiling."""


class XMLInspectionError(AdapterError):
    """An XML response was rejected after retaining bounded safe metadata."""

    def __init__(self, message: str, inspection: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.inspection = dict(inspection)


class BoundedProbeSession(requests.Session):
    """Count every HTTP transaction, including redirects, and restrict hosts."""

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        request_limit: int,
        phase_limits: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__()
        self.allowed_hosts = {str(host).casefold() for host in allowed_hosts}
        self.request_limit = int(request_limit)
        self.requests_started = 0
        self.request_log: list[dict[str, Any]] = []
        self.host_remained_approved = True
        self.phase = "default"
        self.phase_limits = {
            str(name): int(limit) for name, limit in (phase_limits or {}).items()
        }
        self.phase_requests_started: Counter[str] = Counter()
        self.headers.update({"User-Agent": USER_AGENT})

    def set_phase(self, phase: str) -> None:
        self.phase = str(phase)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        parsed = urlparse(str(request.url))
        host = (parsed.hostname or "").casefold()
        if request.method != "GET":
            raise AdapterError("Batch 9 diagnostics permit GET requests only")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise AdapterError("Batch 9 diagnostics require anonymous HTTPS URLs")
        if host not in self.allowed_hosts:
            self.host_remained_approved = False
            raise AdapterError(f"Batch 9 diagnostic request left the approved source host: {host}")
        lowered_headers = {str(name).casefold() for name in request.headers}
        if {"authorization", "proxy-authorization"} & lowered_headers:
            raise AdapterError("Batch 9 diagnostics refuse credential-bearing requests")
        if kwargs.get("verify") is False:
            raise AdapterError("Batch 9 diagnostics cannot disable TLS verification")
        if self.requests_started >= self.request_limit:
            raise RequestCeilingReached(
                f"hard request ceiling reached ({self.request_limit})"
            )
        phase_limit = self.phase_limits.get(self.phase)
        if (
            phase_limit is not None
            and self.phase_requests_started[self.phase] >= phase_limit
        ):
            raise RequestCeilingReached(
                f"hard {self.phase} request ceiling reached ({phase_limit})"
            )
        self.requests_started += 1
        self.phase_requests_started[self.phase] += 1
        entry = {
            "number": self.requests_started,
            "method": "GET",
            "url": sanitize_url(str(request.url)),
            "status": None,
            "phase": self.phase,
        }
        self.request_log.append(entry)
        response = super().send(request, **kwargs)
        entry["status"] = int(response.status_code)
        entry["final_url"] = sanitize_url(str(response.url))
        return response


def sanitize_url(value: str) -> str:
    """Remove fragments, user info, and secret-like query values from a URL."""
    parsed = urlparse(str(value))
    host = (parsed.hostname or "").casefold()
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    query = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        redacted = "<redacted>" if name.casefold() in SENSITIVE_QUERY_NAMES else item
        query.append((name, redacted))
    return urlunparse(
        parsed._replace(netloc=netloc, query=urlencode(query, doseq=True), fragment="")
    )


def parse_request_limits(values: Iterable[str]) -> dict[str, int]:
    """Parse repeatable ``COMPANY=COUNT`` arguments without accepting aliases."""
    result: dict[str, int] = {}
    for value in values:
        company, separator, raw_count = str(value).partition("=")
        if not separator or company not in ALLOWED_COMPANIES:
            raise ValueError(
                "--request-limit must be one of "
                + ", ".join(f"{name}=N" for name in ALLOWED_COMPANIES)
            )
        if company in result:
            raise ValueError(f"duplicate request limit for {company}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"request limit for {company} must be an integer") from exc
        if not 1 <= count <= MAX_REQUEST_LIMIT:
            raise ValueError(
                f"request limit for {company} must be between 1 and {MAX_REQUEST_LIMIT}"
            )
        result[company] = count
    return result


def parse_xml_response_limits(values: Iterable[str]) -> dict[str, int]:
    """Parse explicit, company-scoped XML byte-limit overrides."""
    result: dict[str, int] = {}
    for value in values:
        company, separator, raw_count = str(value).partition("=")
        if not separator or company not in STATIC_COMPANIES:
            raise ValueError(
                "--xml-response-limit must be General Motors=N, Caterpillar=N, or Coupang=N"
            )
        if company in result:
            raise ValueError(f"duplicate XML response limit for {company}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"XML response limit for {company} must be an integer") from exc
        if not MAX_XML_RESPONSE_BYTES < count <= MAX_XML_RESPONSE_OVERRIDE_BYTES:
            raise ValueError(
                f"XML response limit for {company} must be greater than "
                f"{MAX_XML_RESPONSE_BYTES} and at most {MAX_XML_RESPONSE_OVERRIDE_BYTES}"
            )
        result[company] = count
    return result


def _approved_source_hosts(target: CompanyTarget) -> set[str]:
    if target.platform == "smartrecruiters":
        return {smartrecruiters.API_HOST}
    parsed = urlparse(str(target.listing_url))
    return {(parsed.hostname or "").casefold()}


def _checked_response(response: requests.Response, *, maximum_bytes: int) -> bytes:
    status = int(response.status_code)
    if status in {401, 407}:
        raise AdapterError(f"authentication required (HTTP {status})")
    if status in {403, 429, 451}:
        raise AdapterError(f"source blocked the bounded diagnostic (HTTP {status})")
    if status != 200:
        raise AdapterError(f"unexpected HTTP status {status}")
    chunks: list[bytes] = []
    retained = 0
    for chunk in response.iter_content(chunk_size=STREAM_CHUNK_BYTES):
        if not chunk:
            continue
        raw = bytes(chunk)
        if retained + len(raw) > maximum_bytes:
            response.close()
            raise AdapterError(f"response exceeded the {maximum_bytes}-byte diagnostic limit")
        chunks.append(raw)
        retained += len(raw)
    content = b"".join(chunks)
    response._content = content
    response._content_consumed = True
    if not content:
        raise AdapterError("source returned an empty HTTP 200 response")
    lowered = content[:200_000].decode("utf-8", errors="ignore").casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise AdapterError("source returned a CAPTCHA, WAF, or login response")
    return content


def _response_content_length(response: requests.Response) -> tuple[int | None, str | None]:
    raw = str(response.headers.get("content-length") or "").strip()
    if not raw:
        return None, None
    if raw.isascii() and raw.isdigit():
        return int(raw), raw
    return None, raw[:100]


def _initial_content_classification(content: bytes) -> str:
    """Classify only supported XML-feed roots; a 200 or MIME type is insufficient."""
    normalized = content.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    normalized = re.sub(br"^(?:<\?xml\b[^>]*>\s*)?", b"", normalized, flags=re.I)
    normalized = re.sub(br"^(?:<!--.*?-->\s*)*", b"", normalized, flags=re.I | re.S)
    if re.match(br"<(?:rss|feed|(?:[a-z_][\w.-]*:)?rdf)(?:\s|>)", normalized, re.I):
        return "confirmed_xml_feed"
    if re.match(br"<(?:!doctype\s+html|html)(?:\s|>)", normalized, re.I):
        return "html"
    if normalized.startswith((b"{", b"[")):
        return "json"
    if normalized.startswith(b"<"):
        return "other_markup"
    return "unknown"


def _xml_inspection_sample(content: bytes) -> str:
    text = content[:512].decode("utf-8", errors="replace")
    return " ".join(text.replace("\x00", "").split())[:512]


def _inspect_xml_response(
    response: requests.Response,
    *,
    response_limit: int = MAX_XML_RESPONSE_BYTES,
) -> tuple[bytes, dict[str, Any]]:
    """Inspect headers and a small prefix before accepting a bounded XML body."""
    status = int(response.status_code)
    content_length, raw_content_length = _response_content_length(response)
    content_type = str(response.headers.get("content-type") or "")[:300]
    override_requested = response_limit > MAX_XML_RESPONSE_BYTES
    inspection: dict[str, Any] = {
        **_response_outcome(response),
        "content_type": content_type,
        "content_length": content_length,
        "raw_content_length": raw_content_length,
        "initial_inspection_limit": XML_INITIAL_INSPECTION_BYTES,
        "default_response_limit": MAX_XML_RESPONSE_BYTES,
        "effective_response_limit": response_limit,
        "override_requested": override_requested,
        "override_accepted": False,
        "complete_body_downloaded": False,
    }
    if not MAX_XML_RESPONSE_BYTES <= response_limit <= MAX_XML_RESPONSE_OVERRIDE_BYTES:
        raise XMLInspectionError(
            f"XML response limit must be between {MAX_XML_RESPONSE_BYTES} and "
            f"{MAX_XML_RESPONSE_OVERRIDE_BYTES} bytes",
            inspection,
        )
    if status in {401, 407}:
        raise XMLInspectionError(f"authentication required (HTTP {status})", inspection)
    if status in {403, 429, 451}:
        raise XMLInspectionError(
            f"source blocked the bounded diagnostic (HTTP {status})", inspection
        )
    if status != 200:
        raise XMLInspectionError(f"unexpected HTTP status {status}", inspection)

    chunks: list[bytes] = []
    retained = 0
    iterator = response.iter_content(chunk_size=XML_INITIAL_INSPECTION_BYTES)
    try:
        first = next(iterator)
    except StopIteration:
        first = b""
    prefix = bytes(first or b"")[:XML_INITIAL_INSPECTION_BYTES]
    classification = _initial_content_classification(prefix)
    inspection.update(
        {
            "initial_bytes_read": len(prefix),
            "initial_bytes_sha256": hashlib.sha256(prefix).hexdigest(),
            "initial_content_sample": _xml_inspection_sample(prefix),
            "content_classification": classification,
        }
    )
    if not first:
        response.close()
        raise XMLInspectionError("source returned an empty HTTP 200 response", inspection)
    if classification != "confirmed_xml_feed":
        response.close()
        raise XMLInspectionError(
            f"source-discovered feed was not confirmed XML ({classification})",
            inspection,
        )
    inspection["override_accepted"] = override_requested
    if content_length is not None and content_length > response_limit:
        response.close()
        inspection["limit_exceeded"] = True
        raise XMLInspectionError(
            f"confirmed XML response Content-Length {content_length} exceeds the "
            f"{response_limit}-byte diagnostic limit",
            inspection,
        )

    raw_first = bytes(first)
    if len(raw_first) > response_limit:
        response.close()
        inspection["limit_exceeded"] = True
        raise XMLInspectionError(
            f"confirmed XML response exceeded the {response_limit}-byte diagnostic limit",
            inspection,
        )
    chunks.append(raw_first)
    retained = len(raw_first)
    for chunk in iterator:
        if not chunk:
            continue
        raw = bytes(chunk)
        if retained + len(raw) > response_limit:
            response.close()
            inspection["bytes_read_before_rejection"] = retained + len(raw)
            inspection["limit_exceeded"] = True
            raise XMLInspectionError(
                f"confirmed XML response exceeded the {response_limit}-byte diagnostic limit",
                inspection,
            )
        chunks.append(raw)
        retained += len(raw)
    content = b"".join(chunks)
    response._content = content
    response._content_consumed = True
    lowered = content[:200_000].decode("utf-8", errors="ignore").casefold()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        raise XMLInspectionError("source returned a CAPTCHA, WAF, or login response", inspection)
    inspection.update(
        {
            "complete_body_downloaded": True,
            "body_bytes": len(content),
            "body_sha256": hashlib.sha256(content).hexdigest(),
            "limit_exceeded": False,
        }
    )
    return content, inspection


def _response_outcome(response: requests.Response) -> dict[str, Any]:
    return {
        "status": int(response.status_code),
        "requested_url": sanitize_url(str(response.request.url)),
        "final_url": sanitize_url(str(response.url)),
        "redirects": [
            {
                "status": int(item.status_code),
                "url": sanitize_url(str(item.url)),
                "location": sanitize_url(str(item.headers.get("location") or "")),
            }
            for item in response.history
        ],
    }


def _pagination_dict(evidence: Any) -> dict[str, Any]:
    value = asdict(evidence)
    value["status"] = evidence.status()
    value["continuation_reasons"] = list(evidence.continuation_reasons())
    value["terminal_reasons"] = list(evidence.terminal_reasons())
    return value


def _active_next_urls(root: Any, page_url: str) -> list[str]:
    nodes = root.xpath(
        "//a[@href and not(@aria-disabled='true') and "
        "not(contains(concat(' ', translate(normalize-space(@class), "
        "'DISABLED', 'disabled'), ' '), ' disabled ')) and "
        "(translate(@rel, 'NEXT', 'next')='next' or "
        "contains(concat(' ', translate(normalize-space(@class), 'NEXT', 'next'), ' '), ' next ') or "
        "contains(translate(@class, 'LOADMORE', 'loadmore'), 'load-more') or "
        "contains(translate(@aria-label, 'NEXT', 'next'), 'next') or "
        "contains(translate(@aria-label, 'LOADMORE', 'loadmore'), 'load more') or "
        "normalize-space(translate(string(.), 'NEXT', 'next'))='next' or "
        "normalize-space(translate(string(.), 'LOADMORE', 'loadmore'))='load more')]"
    )
    current_host = (urlparse(page_url).hostname or "").casefold()
    values: list[str] = []
    for node in nodes:
        proposed = urljoin(page_url, str(node.get("href") or "").strip())
        parsed = urlparse(proposed)
        if (
            parsed.scheme == "https"
            and (parsed.hostname or "").casefold() == current_host
            and not parsed.username
            and not parsed.password
            and not any(
                name.casefold() in SENSITIVE_QUERY_NAMES
                for name, _value in parse_qsl(parsed.query, keep_blank_values=True)
            )
        ):
            cleaned = sanitize_url(proposed)
            if cleaned not in values:
                values.append(cleaned)
    return values


def _approved_discovered_url(value: str, page_url: str) -> str | None:
    """Return a safe same-host URL or ``None`` without relaxing source scope."""
    proposed = urljoin(page_url, str(value or "").strip())
    parsed = urlparse(proposed)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold()
        != (urlparse(page_url).hostname or "").casefold()
        or parsed.username
        or parsed.password
        or any(
            name.casefold() in SENSITIVE_QUERY_NAMES
            for name, _item in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        return None
    return sanitize_url(proposed)


def _source_discovered_feed_links(root: Any, page_url: str) -> list[dict[str, Any]]:
    """Extract actual feed links from HTML elements; never infer a URL template."""
    links: list[dict[str, Any]] = []
    for node in root.xpath("//a[@href] | //link[@href]"):
        href = str(node.get("href") or "").strip()
        media_type = " ".join(str(node.get("type") or "").split()).casefold()
        rel = " ".join(str(node.get("rel") or "").split()).casefold()
        lowered = href.casefold()
        if not (
            any(token in media_type for token in ("rss", "atom", "xml"))
            or any(token in lowered for token in ("/xml/", ".xml", "rss=", "atom", "/feed"))
        ):
            continue
        approved = _approved_discovered_url(href, page_url)
        if approved is None:
            continue
        item = {
            "url": approved,
            "element": str(getattr(node, "tag", "")).casefold(),
            "rel": rel,
            "type": media_type,
            "text": " ".join(node.xpath("string(.)").split())[:300],
            "provenance": "html_link_element",
        }
        if approved not in {existing["url"] for existing in links}:
            links.append(item)
    return links


def _structured_endpoint_candidates(content: bytes, page_url: str) -> list[str]:
    """Return unverified same-host job/API URL evidence without requesting it."""
    text = content[:500_000].decode("utf-8", errors="ignore")
    host = (urlparse(page_url).hostname or "").casefold()
    expressions = (
        r"https://[^\s\"'<>]+",
        r"[\"']((?:/[^\"']*)?(?:api|jobs?|search)[^\"']*)[\"']",
    )
    candidates: list[str] = []
    for expression in expressions:
        for match in re.finditer(expression, text, flags=re.I):
            value = match.group(1) if match.lastindex else match.group(0)
            value = value.replace("\\/", "/").rstrip(",);]")
            proposed = urljoin(page_url, value)
            parsed = urlparse(proposed)
            lowered = proposed.casefold()
            if (
                parsed.scheme == "https"
                and (parsed.hostname or "").casefold() == host
                and any(token in lowered for token in ("/api", "/job", "/search"))
            ):
                cleaned = sanitize_url(proposed)
                if cleaned not in candidates and cleaned != sanitize_url(page_url):
                    candidates.append(cleaned)
            if len(candidates) >= 20:
                return candidates
    return candidates


def analyze_static_page(
    content: bytes,
    response: requests.Response,
    target: CompanyTarget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the production static parser contracts to one captured page."""
    root = static_html._parse_html(content, response)
    evidence = html_pagination_evidence(root)
    containers = static_html.select(root, str(target.listing_selector))
    cards = [
        card
        for card in static_html.select(root, str(target.job_card_selector))
        if any(static_html._contains(container, card) for container in containers)
    ]
    metadata = SourceScanMetadata(diagnostics=[])
    records = static_html._listing_records(
        containers,
        cards,
        target,
        str(response.url),
        metadata=metadata,
        page_number=1,
    ) if containers else []
    jobs = []
    rejected = []
    for index, record in enumerate(records):
        try:
            jobs.append(static_html._normalize(record, target, str(response.url)))
        except (AdapterError, ValueError) as exc:
            if len(rejected) < MAX_REJECTION_SAMPLES:
                rejected.append({"record": index + 1, "reason": safe_error(exc)})
    ids = [job.job_id for job in jobs]
    urls = [static_html._canonical_job_url(job.url) for job in jobs]
    identity_text = " ".join(
        [
            " ".join(root.xpath("string(//title)").split()),
            " ".join(root.xpath("string(//body)").split())[:100_000],
        ]
    )
    if target.identity_regex:
        source_identity = {
            "status": "validated" if re.search(str(target.identity_regex), identity_text, re.I) else "failed",
            "contract": str(target.identity_regex),
        }
    else:
        source_identity = {
            "status": "unproven",
            "contract": None,
            "reason": "effective target has no source identity regex",
        }
    next_urls = _active_next_urls(root, str(response.url))
    discovered_feed_links = _source_discovered_feed_links(root, str(response.url))
    page = {
        **_response_outcome(response),
        "bytes": len(content),
        "cards": len(cards),
        "records": len(records),
        "normalized_records": len(jobs),
        "stable_ids": ids,
        "titles": [job.title for job in jobs],
        "canonical_urls": urls,
        "duplicate_ids_within_page": sorted(
            value for value, count in Counter(ids).items() if count > 1
        ),
        "rejected_records": rejected,
        "pagination": _pagination_dict(evidence),
        "source_advertised_next_urls": next_urls,
        "source_discovered_feed_links": discovered_feed_links,
        "unverified_structured_endpoint_candidates": _structured_endpoint_candidates(
            content, str(response.url)
        ),
        "source_identity": source_identity,
    }
    fixture = {
        "url": sanitize_url(str(response.url)),
        "title": " ".join(root.xpath("string(//title)").split())[:300],
        "pagination": page["pagination"],
        "source_advertised_next_urls": next_urls,
        "source_discovered_feed_links": discovered_feed_links,
        "records": [
            {"stable_id": job.job_id, "title": job.title[:300], "canonical_url": url}
            for job, url in list(zip(jobs, urls))[:MAX_FIXTURE_RECORDS]
        ],
        "record_count": len(jobs),
        "response_sha256": hashlib.sha256(content).hexdigest(),
    }
    return page, fixture


def summarize_static_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    all_ids = [value for page in pages for value in page["stable_ids"]]
    all_urls = [value for page in pages for value in page["canonical_urls"]]
    id_counts = Counter(all_ids)
    url_to_ids: dict[str, set[str]] = {}
    observations: dict[str, tuple[str, str]] = {}
    mutation = False
    for page in pages:
        for record_id, title, url in zip(
            page["stable_ids"], page["titles"], page["canonical_urls"]
        ):
            current = (url, " ".join(str(title).split()).casefold())
            if record_id in observations and observations[record_id] != current:
                mutation = True
            observations.setdefault(record_id, current)
            url_to_ids.setdefault(url, set()).add(record_id)
    fingerprints = [tuple(page["stable_ids"]) for page in pages]
    repeated_page = len(fingerprints) != len(set(fingerprints))
    totals = [
        page["pagination"]["reported_total"]
        for page in pages
        if page["pagination"]["reported_total"] is not None
    ]
    contradictory_totals = len(set(totals)) > 1
    collisions = {
        url: sorted(ids) for url, ids in url_to_ids.items() if len(ids) > 1
    }
    rejected = sum(len(page["rejected_records"]) for page in pages)
    final_pagination = pages[-1]["pagination"] if pages else {}
    terminal = bool(final_pagination.get("status") == "terminal")
    if repeated_page:
        failure = "repeated_page"
    elif collisions:
        failure = "canonical_url_collision"
    elif contradictory_totals:
        failure = "contradictory_totals"
    elif mutation:
        failure = "collection_changed_during_scan"
    elif any(count > 1 for count in id_counts.values()):
        failure = "duplicate_ids_across_pages"
    elif rejected:
        failure = "rejected_records"
    elif not terminal:
        failure = "missing_authoritative_terminal_evidence"
    else:
        failure = None
    return {
        "retrieved_records": len(all_ids),
        "unique_stable_ids": len(id_counts),
        "duplicate_ids": sorted(value for value, count in id_counts.items() if count > 1),
        "canonical_url_collisions": collisions,
        "advertised_totals": totals,
        "contradictory_totals": contradictory_totals,
        "repeated_page": repeated_page,
        "collection_mutation": mutation,
        "rejected_record_count": rejected,
        "terminal_evidence": final_pagination,
        "failure_reason": failure,
    }


def probe_static(
    session: BoundedProbeSession,
    target: CompanyTarget,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    static_html.validate_target(target)
    pages: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    next_url = str(target.listing_url)
    seen_urls: set[str] = set()
    while session.requests_started < session.request_limit:
        normalized_url = sanitize_url(next_url)
        if normalized_url in seen_urls:
            break
        seen_urls.add(normalized_url)
        response = session.get(
            next_url,
            headers={"Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            stream=True,
        )
        content = _checked_response(response, maximum_bytes=MAX_STATIC_RESPONSE_BYTES)
        content_type = str(response.headers.get("content-type") or "")
        if content_type and "html" not in content_type.casefold():
            raise AdapterError("static source returned a non-HTML response")
        page, fixture = analyze_static_page(content, response, target)
        pages.append(page)
        fixtures.append(fixture)
        candidates = page["source_advertised_next_urls"]
        if not candidates:
            break
        if len(candidates) != 1:
            break
        proposed = str(candidates[0])
        if proposed == normalized_url:
            break
        next_url = proposed
    summary = summarize_static_pages(pages)
    if (
        session.requests_started >= session.request_limit
        and pages
        and pages[-1]["pagination"].get("status") == "continuation"
        and summary["failure_reason"] == "missing_authoritative_terminal_evidence"
    ):
        summary["failure_reason"] = "hard_request_ceiling_before_terminal_evidence"
    identities = [page["source_identity"] for page in pages]
    identity_status = "failed" if any(item["status"] == "failed" for item in identities) else (
        "validated" if identities and all(item["status"] == "validated" for item in identities) else "unproven"
    )
    diagnostic = {
        "pages": pages,
        **summary,
        "source_identity_evidence": {"status": identity_status, "pages": identities},
        "structured_endpoint_candidates_are_unverified": True,
    }
    fixture = {"company": target.name, "adapter": target.platform, "pages": fixtures}
    return diagnostic, fixture


def _xml_item_url(item: Any, feed_format: str, page_url: str) -> str:
    if feed_format == "atom":
        candidates = [
            child
            for child in item
            if _local_name(child).casefold() == "link"
            and str(child.get("rel") or "alternate").casefold() in {"", "alternate"}
        ]
        raw = str(candidates[0].get("href") or "") if candidates else ""
    else:
        links = _direct_children(item, "link")
        raw = "".join(links[0].itertext()).strip() if links else ""
    return _approved_discovered_url(raw, page_url) or ""


def _xml_next_urls(root: Any, page_url: str) -> list[str]:
    values: list[str] = []
    for node in root.iter():
        if (
            _local_name(node).casefold() == "link"
            and str(node.get("rel") or "").casefold() == "next"
        ):
            approved = _approved_discovered_url(str(node.get("href") or ""), page_url)
            if approved and approved not in values:
                values.append(approved)
    return values


def _xml_total_metadata(container: Any) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for child in container:
        name = _local_name(child)
        if name.casefold() not in {"totalresults", "totalfound", "total", "count"}:
            continue
        text = " ".join("".join(child.itertext()).split())
        try:
            parsed: int | None = int(text)
        except ValueError:
            parsed = None
        values.append(
            {
                "field": name,
                "value": parsed,
                "raw": text[:100],
                "authoritative": name.casefold() in {"totalresults", "totalfound"},
            }
        )
    return values


def analyze_xml_document(
    content: bytes,
    response: requests.Response,
    target: CompanyTarget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inspect a bounded RSS/Atom document without treating no-next as atomic."""
    root = _safe_xml_root(content)
    root_name = _local_name(root).casefold()
    if root_name == "rss":
        channels = _direct_children(root, "channel")
        if len(channels) != 1:
            raise AdapterError("RSS document must contain exactly one channel")
        container = channels[0]
        items = _direct_children(container, "item")
        feed_format = "rss"
    elif root_name == "feed":
        container = root
        items = _direct_children(container, "entry")
        feed_format = "atom"
    elif root_name in {"rdf", "rdf:rdf"}:
        container = root
        items = [node for node in root if _local_name(node).casefold() == "item"]
        feed_format = "rss-rdf"
    else:
        raise AdapterError(f"unsupported XML job-feed root element: {root_name or '<none>'}")

    page_url = str(response.url)
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        stable_id = _first_child_text(item, ("guid", "id"))
        title = _first_child_text(item, ("title",))
        canonical_url = _xml_item_url(item, feed_format, page_url)
        publication = {
            _local_name(child): " ".join("".join(child.itertext()).split())[:300]
            for child in item
            if _local_name(child).casefold()
            in {"pubdate", "published", "updated", "date", "issued", "modified"}
        }
        reasons = []
        if not stable_id:
            reasons.append("missing_stable_id")
        if not title:
            reasons.append("missing_title")
        if not canonical_url:
            reasons.append("missing_or_unapproved_canonical_url")
        if reasons:
            rejected.append({"record": index + 1, "reasons": reasons})
            continue
        records.append(
            {
                "stable_id": stable_id,
                "title": title,
                "canonical_url": canonical_url,
                "publication_fields": publication,
            }
        )

    ids = [item["stable_id"] for item in records]
    urls = [item["canonical_url"] for item in records]
    url_to_ids: dict[str, set[str]] = {}
    for record in records:
        url_to_ids.setdefault(record["canonical_url"], set()).add(record["stable_id"])
    metadata_text = " ".join(
        filter(
            None,
            (
                _first_child_text(container, ("title",)),
                _first_child_text(container, ("subtitle", "description")),
                _first_child_text(container, ("generator", "rights")),
            ),
        )
    )
    identity_pattern = XML_IDENTITY_PATTERNS.get(target.name) or target.identity_regex
    if not identity_pattern:
        identity = {
            "status": "unproven",
            "contract": None,
            "reason": "no reviewed XML identity pattern",
        }
    else:
        identity = {
            "status": (
                "validated"
                if re.search(str(identity_pattern), metadata_text, re.I)
                and all(urls)
                else "failed"
            ),
            "contract": str(identity_pattern),
            "feed_metadata": metadata_text[:500],
        }
    totals = _xml_total_metadata(container)
    next_urls = _xml_next_urls(root, page_url)
    content_type = str(response.headers.get("content-type") or "")
    page = {
        **_response_outcome(response),
        "bytes": len(content),
        "content_type": content_type,
        "format": feed_format,
        "root_element": _local_name(root),
        "item_count": len(items),
        "normalized_records": len(records),
        "stable_ids": ids,
        "titles": [item["title"] for item in records],
        "canonical_urls": urls,
        "publication_fields_present": sorted(
            {name for item in records for name in item["publication_fields"]}
        ),
        "duplicate_ids_within_page": sorted(
            value for value, count in Counter(ids).items() if count > 1
        ),
        "canonical_url_collisions": {
            url: sorted(values) for url, values in url_to_ids.items() if len(values) > 1
        },
        "rejected_records": rejected[:MAX_REJECTION_SAMPLES],
        "rejected_record_count": len(rejected),
        "source_identity": identity,
        "continuation_urls": next_urls,
        "total_metadata": totals,
        "response_sha256": hashlib.sha256(content).hexdigest(),
    }
    fixture = {
        "url": sanitize_url(page_url),
        "content_type": content_type,
        "format": feed_format,
        "root_element": _local_name(root),
        "item_count": len(items),
        "records": records[:MAX_FIXTURE_RECORDS],
        "rejected_records": rejected[:MAX_REJECTION_SAMPLES],
        "source_identity": identity,
        "continuation_urls": next_urls,
        "total_metadata": totals,
        "response_sha256": page["response_sha256"],
    }
    return page, fixture


def summarize_xml_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [value for page in pages for value in page["stable_ids"]]
    counts = Counter(ids)
    urls: dict[str, set[str]] = {}
    observations: dict[str, tuple[str, str]] = {}
    mutation = False
    for page in pages:
        for stable_id, title, url in zip(
            page["stable_ids"], page["titles"], page["canonical_urls"]
        ):
            value = (url, " ".join(str(title).split()).casefold())
            if stable_id in observations and observations[stable_id] != value:
                mutation = True
            observations.setdefault(stable_id, value)
            urls.setdefault(url, set()).add(stable_id)
    authoritative_totals = [
        item["value"]
        for page in pages
        for item in page["total_metadata"]
        if item["authoritative"] and item["value"] is not None
    ]
    total = (
        authoritative_totals[0]
        if authoritative_totals and len(set(authoritative_totals)) == 1
        else None
    )
    repeated_page = len(pages) > 1 and len(
        {tuple(page["stable_ids"]) for page in pages}
    ) != len(pages)
    collisions = {url: sorted(values) for url, values in urls.items() if len(values) > 1}
    rejected = sum(page["rejected_record_count"] for page in pages)
    identities = [page["source_identity"]["status"] for page in pages]
    identity_status = (
        "failed"
        if "failed" in identities
        else "validated" if identities and all(value == "validated" for value in identities)
        else "unproven"
    )
    next_present = bool(pages and pages[-1]["continuation_urls"])
    terminal = (
        total is not None
        and len(counts) == total
        and not next_present
        and not repeated_page
        and not rejected
    )
    if not pages:
        failure = "no_xml_documents"
    elif identity_status != "validated":
        failure = "source_identity_" + identity_status
    elif len(set(authoritative_totals)) > 1:
        failure = "conflicting_totals"
    elif repeated_page:
        failure = "repeated_page"
    elif mutation:
        failure = "collection_changed_during_scan"
    elif collisions:
        failure = "canonical_url_collision"
    elif any(count > 1 for count in counts.values()):
        failure = "duplicate_ids"
    elif rejected:
        failure = "rejected_records"
    elif not terminal:
        failure = "missing_authoritative_completeness_evidence"
    else:
        failure = None
    return {
        "retrieved_records": len(ids),
        "unique_stable_ids": len(counts),
        "duplicate_ids": sorted(value for value, count in counts.items() if count > 1),
        "canonical_url_collisions": collisions,
        "advertised_totals": authoritative_totals,
        "collection_mutation": mutation,
        "repeated_page": repeated_page,
        "rejected_record_count": rejected,
        "source_identity_evidence": {"status": identity_status},
        "terminal_evidence": {
            "observed": terminal,
            "type": "authoritative_total" if terminal else None,
            "value": total if terminal else None,
            "absence_of_next_is_not_atomic": True,
        },
        "feed_appears_truncated": (
            True if next_present or (total is not None and len(counts) < total) else None
        ),
        "failure_reason": failure,
    }


def probe_xml(
    session: BoundedProbeSession,
    target: CompanyTarget,
    timeout: int,
    response_limit: int = MAX_XML_RESPONSE_BYTES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Discover a feed from source HTML, then inspect at most two XML requests."""
    static_html.validate_target(target)
    session.set_phase("discovery")
    response = session.get(
        str(target.listing_url),
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
        stream=True,
    )
    html_content = _checked_response(response, maximum_bytes=MAX_STATIC_RESPONSE_BYTES)
    content_type = str(response.headers.get("content-type") or "")
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("XML discovery source returned a non-HTML response")
    html_root = static_html._parse_html(html_content, response)
    discovered = _source_discovered_feed_links(html_root, str(response.url))
    if not discovered:
        raise AdapterError("source HTML exposed no approved XML/RSS link element")

    selected = discovered[0]
    pages: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    response_inspections: list[dict[str, Any]] = []

    def failed_xml_probe(reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
        summary = summarize_xml_pages(pages)
        summary["failure_reason"] = reason
        diagnostic = {
            "discovery": {
                **_response_outcome(response),
                "bytes": len(html_content),
                "source_discovered_feed_links": discovered,
                "selected_feed": selected,
                "unselected_feed_links": discovered[1:],
                "selection_contract": "first approved HTML link element in document order",
            },
            "pages": pages,
            "xml_response_inspections": response_inspections,
            **summary,
            "xml_request_ceiling": MAX_XML_REQUESTS,
            "xml_requests_started": session.phase_requests_started["xml"],
            "xml_response_limit": response_limit,
            "source_discovered_only": True,
            "feed_activated_for_production": False,
        }
        fixture = {
            "company": target.name,
            "adapter": "xml_diagnostic",
            "discovery": {
                "url": sanitize_url(str(response.url)),
                "source_discovered_feed_links": discovered,
                "response_sha256": hashlib.sha256(html_content).hexdigest(),
            },
            "pages": fixtures,
        }
        return diagnostic, fixture

    next_url = str(selected["url"])
    seen_urls: set[str] = set()
    session.set_phase("xml")
    while session.phase_requests_started["xml"] < MAX_XML_REQUESTS:
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)
        xml_response = session.get(
            next_url,
            headers={"Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml"},
            timeout=timeout,
            stream=True,
        )
        try:
            content, inspection = _inspect_xml_response(
                xml_response, response_limit=response_limit
            )
        except XMLInspectionError as exc:
            response_inspections.append(exc.inspection)
            return failed_xml_probe(safe_error(exc))
        response_inspections.append(inspection)
        try:
            page, fixture = analyze_xml_document(content, xml_response, target)
        except AdapterError as exc:
            return failed_xml_probe(safe_error(exc))
        pages.append(page)
        fixtures.append(fixture)
        candidates = page["continuation_urls"]
        if len(candidates) != 1:
            break
        next_url = str(candidates[0])

    summary = summarize_xml_pages(pages)
    diagnostic = {
        "discovery": {
            **_response_outcome(response),
            "bytes": len(html_content),
            "source_discovered_feed_links": discovered,
            "selected_feed": selected,
            "unselected_feed_links": discovered[1:],
            "selection_contract": "first approved HTML link element in document order",
        },
        "pages": pages,
        "xml_response_inspections": response_inspections,
        **summary,
        "xml_request_ceiling": MAX_XML_REQUESTS,
        "xml_requests_started": session.phase_requests_started["xml"],
        "xml_response_limit": response_limit,
        "source_discovered_only": True,
        "feed_activated_for_production": False,
    }
    fixture = {
        "company": target.name,
        "adapter": "xml_diagnostic",
        "discovery": {
            "url": sanitize_url(str(response.url)),
            "source_discovered_feed_links": discovered,
            "response_sha256": hashlib.sha256(html_content).hexdigest(),
        },
        "pages": fixtures,
    }
    return diagnostic, fixture


def _smartrecruiters_listing_fixture(
    payload: Mapping[str, Any], content: list[Mapping[str, Any]], offset: int
) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": payload.get("limit"),
        "totalFound": payload.get("totalFound"),
        "top_level_fields": sorted(str(key) for key in payload),
        "records": [
            {
                "id": item.get("id"),
                "name": str(item.get("name") or "")[:300],
                "ref": sanitize_url(str(item.get("ref") or "")),
                "company": item.get("company"),
                "location": item.get("location"),
                "department": item.get("department"),
                "function": item.get("function"),
                "publication_fields": {
                    str(key): value
                    for key, value in item.items()
                    if any(token in str(key).casefold() for token in ("status", "publish", "release"))
                    and isinstance(value, (str, int, float, bool, type(None)))
                },
            }
            for item in content[:MAX_FIXTURE_RECORDS]
        ],
        "record_count": len(content),
    }


def summarize_smartrecruiters_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [value for page in pages for value in page["stable_ids"]]
    counts = Counter(ids)
    totals = [page["advertised_total"] for page in pages]
    total = totals[0] if totals and len(set(totals)) == 1 else None
    mutation = len(set(totals)) > 1
    retrieved = len(ids)
    unique = len(counts)
    terminal = total is not None and unique == total
    if mutation:
        failure = "contradictory_totals"
    elif any(page["duplicate_ids_within_page"] for page in pages):
        failure = "duplicate_ids_within_page"
    elif unique != retrieved:
        failure = "duplicate_ids_across_offsets"
    elif not terminal:
        failure = "authoritative_total_not_reached"
    else:
        failure = None
    return {
        "advertised_totals": totals,
        "retrieved_records": retrieved,
        "unique_stable_ids": unique,
        "duplicate_ids": sorted(value for value, count in counts.items() if count > 1),
        "canonical_url_collisions": {},
        "terminal_evidence": {
            "type": "authoritative_total" if terminal else None,
            "value": total if terminal else None,
            "observed": terminal,
        },
        "collection_mutation": mutation,
        "rejected_record_count": 0,
        "failure_reason": failure,
    }


def probe_smartrecruiters(
    session: BoundedProbeSession,
    target: CompanyTarget,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    smartrecruiters.validate_target(target)
    page_size = smartrecruiters.MAX_PAGE_SIZE
    listing_budget = session.request_limit - 1 if session.request_limit > 1 else 1
    pages: list[dict[str, Any]] = []
    fixture_pages: list[dict[str, Any]] = []
    first_item: Mapping[str, Any] | None = None
    for page_index in range(listing_budget):
        offset = page_index * page_size
        response = session.get(
            str(target.endpoint_url),
            params={"limit": page_size, "offset": offset},
            headers=smartrecruiters.HEADERS,
            timeout=timeout,
            stream=True,
        )
        raw = _checked_response(response, maximum_bytes=MAX_JSON_RESPONSE_BYTES)
        payload = smartrecruiters._parse_response(response, f"diagnostic offset={offset}")
        content, total = smartrecruiters._parse_listing(
            payload, str(target.slug), target, page_size, offset
        )
        ids = [smartrecruiters._posting_id(item.get("id")) for item in content]
        pages.append(
            {
                **_response_outcome(response),
                "offset": offset,
                "requested_limit": page_size,
                "returned_records": len(content),
                "advertised_total": total,
                "stable_ids": ids,
                "duplicate_ids_within_page": sorted(
                    value for value, count in Counter(ids).items() if count > 1
                ),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        fixture_pages.append(_smartrecruiters_listing_fixture(payload, content, offset))
        if first_item is None and content:
            first_item = content[0]
        if len(content) < page_size or len(set(ids)) + offset >= total:
            break

    detail_evidence: dict[str, Any] = {"status": "not_requested"}
    if first_item is not None and session.requests_started < session.request_limit:
        job_id = smartrecruiters._posting_id(first_item.get("id"))
        detail_url = smartrecruiters._detail_ref(
            first_item.get("ref"), str(target.slug), job_id
        )
        response = session.get(
            detail_url,
            headers=smartrecruiters.HEADERS,
            timeout=timeout,
            stream=True,
        )
        raw = _checked_response(response, maximum_bytes=MAX_JSON_RESPONSE_BYTES)
        detail = smartrecruiters._parse_response(response, f"diagnostic detail {job_id}")
        job = smartrecruiters._normalize(
            detail, first_item, target, str(target.slug), detail_url
        )
        detail_evidence = {
            "status": "validated",
            "stable_id": job.job_id,
            "canonical_url": sanitize_url(job.url),
            "company": job.company,
            "source": job.source,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
        }

    summary = summarize_smartrecruiters_pages(pages)
    diagnostic = {
        "pages": pages,
        **summary,
        "count_interpretation": "totalFound is authoritative only when stable unique IDs reach it",
        "source_identity_evidence": {
            "status": "validated" if pages else "unproven",
            "company_identifier": target.slug,
            "detail_sample": detail_evidence,
        },
        "server_side_population_filter": {
            "status": "unproven",
            "reason": "no filter was sent without a reviewed official query contract",
        },
    }
    fixture = {
        "company": target.name,
        "adapter": target.platform,
        "pages": fixture_pages,
        "detail_evidence": detail_evidence,
    }
    return diagnostic, fixture


def parse_population_filter(value: str) -> tuple[str, str]:
    dimension, separator, population = str(value).partition("=")
    dimension = dimension.strip().casefold()
    population = population.strip()
    if not separator or dimension not in SMARTRECRUITERS_PARTITION_DIMENSIONS:
        raise ValueError("--population-filter must be country=VALUE or department=VALUE")
    if not population or len(population) > 200 or any(char.isspace() for char in population):
        raise ValueError("population filter value must be a non-empty source value without whitespace")
    return dimension, population


def _posting_partition_value(item: Mapping[str, Any], dimension: str) -> str | None:
    if dimension == "country":
        location = item.get("location")
        value = location.get("country") if isinstance(location, Mapping) else None
    else:
        department = item.get("department")
        value = department.get("id") if isinstance(department, Mapping) else None
    text = " ".join(str(value or "").split())
    return text or None


def summarize_population_partitions(
    partitions: list[dict[str, Any]],
    *,
    baseline_total: int,
    anchors_match: bool,
    observed_dimension_values: Iterable[str],
    absent_dimension_observed: bool,
    snapshots_consistent: bool = True,
    partition_values_validated: bool = True,
) -> dict[str, Any]:
    """Validate a non-overlapping partition union without assuming exhaustiveness."""
    observed = {str(value) for value in observed_dimension_values}
    requested = {str(item["value"]) for item in partitions}
    id_to_values: dict[str, set[str]] = {}
    conflicting_totals = False
    incomplete: list[str] = []
    unexpected_values: dict[str, list[str]] = {}
    aggregate_ids: set[str] = set()
    for partition in partitions:
        totals = list(partition.get("advertised_totals") or [])
        if len(set(totals)) > 1:
            conflicting_totals = True
        if partition.get("failure_reason") is not None:
            incomplete.append(str(partition["value"]))
        unexpected = sorted(
            str(item) for item in (partition.get("unexpected_partition_values") or [])
        )
        if unexpected or int(partition.get("absent_partition_value_count") or 0):
            unexpected_values[str(partition["value"])] = unexpected + (
                ["<absent>"] if int(partition.get("absent_partition_value_count") or 0) else []
            )
        for stable_id in partition.get("stable_ids") or []:
            stable_id = str(stable_id)
            id_to_values.setdefault(stable_id, set()).add(str(partition["value"]))
            aggregate_ids.add(stable_id)
    overlaps = {
        stable_id: sorted(values)
        for stable_id, values in id_to_values.items()
        if len(values) > 1
    }
    missing_observed = sorted(observed - requested)
    if not anchors_match or not snapshots_consistent:
        failure = "collection_changed_during_partition_traversal"
    elif conflicting_totals:
        failure = "conflicting_partition_totals"
    elif overlaps:
        failure = "population_overlap"
    elif unexpected_values:
        failure = "unexpected_or_absent_partition_values"
    elif incomplete:
        failure = "partial_partition_collection"
    elif missing_observed:
        failure = "missing_observed_population_partitions"
    elif absent_dimension_observed:
        failure = "unfilterable_absent_dimension_records"
    elif not partition_values_validated:
        failure = "partition_value_validation_unproven"
    elif len(aggregate_ids) != baseline_total:
        failure = "aggregate_total_mismatch"
    else:
        failure = None
    return {
        "baseline_total": baseline_total,
        "aggregate_unique_ids": len(aggregate_ids),
        "population_overlap": overlaps,
        "unexpected_or_absent_partition_values": unexpected_values,
        "conflicting_partition_totals": conflicting_totals,
        "incomplete_partitions": incomplete,
        "missing_observed_population_partitions": missing_observed,
        "absent_dimension_observed": absent_dimension_observed,
        "anchors_match": anchors_match,
        "snapshots_consistent": snapshots_consistent,
        "partition_values_validated": partition_values_validated,
        "global_population_complete": failure is None,
        "failure_reason": failure,
        "observed_values_are_exhaustive_list": False,
        "exhaustive_coverage_basis": (
            "stable_authoritative_total_equals_non_overlapping_unique_partition_union"
            if failure is None
            else None
        ),
    }


def _smartrecruiters_page(
    session: BoundedProbeSession,
    target: CompanyTarget,
    timeout: int,
    *,
    offset: int,
    filters: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[Mapping[str, Any]], Mapping[str, Any]]:
    page_size = smartrecruiters.MAX_PAGE_SIZE
    params: dict[str, Any] = {"limit": page_size, "offset": offset}
    params.update(filters or {})
    response = session.get(
        str(target.endpoint_url),
        params=params,
        headers=smartrecruiters.HEADERS,
        timeout=timeout,
        stream=True,
    )
    raw = _checked_response(response, maximum_bytes=MAX_JSON_RESPONSE_BYTES)
    payload = smartrecruiters._parse_response(response, f"partition offset={offset}")
    content, total = smartrecruiters._parse_listing(
        payload, str(target.slug), target, page_size, offset
    )
    ids = [smartrecruiters._posting_id(item.get("id")) for item in content]
    page = {
        **_response_outcome(response),
        "offset": offset,
        "requested_limit": page_size,
        "filters": dict(filters or {}),
        "returned_records": len(content),
        "advertised_total": total,
        "stable_ids": ids,
        "duplicate_ids_within_page": sorted(
            value for value, count in Counter(ids).items() if count > 1
        ),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return page, content, payload


def _anchor_signature(page: Mapping[str, Any]) -> str:
    material = {
        "advertised_total": page.get("advertised_total"),
        "stable_ids": list(page.get("stable_ids") or []),
    }
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_diagnostic_artifact(path: Path) -> dict[str, Any]:
    """Load one bounded local diagnostic artifact without accepting oversized state."""
    resolved = path.resolve(strict=True)
    size = resolved.stat().st_size
    if size > MAX_RESUME_ARTIFACT_BYTES:
        raise ValueError(
            f"diagnostic artifact exceeds the {MAX_RESUME_ARTIFACT_BYTES}-byte local limit"
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read diagnostic artifact: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("diagnostic artifact must contain one JSON object")
    return payload


def _validated_resume_pages(
    payload: Mapping[str, Any],
    *,
    company: str,
    dimension: str,
    value: str,
) -> tuple[list[dict[str, Any]], str]:
    """Validate immutable prior pages before any resumed network request."""
    if payload.get("company") != company or payload.get("mode") != "smartrecruiters-partition":
        raise AdapterError("resume artifact company or mode does not match this probe")
    if payload.get("status") != "diagnostic_complete":
        raise AdapterError("resume artifact did not complete its bounded diagnostic")
    identity = payload.get("source_identity_evidence")
    if not isinstance(identity, Mapping) or identity.get("status") != "validated":
        raise AdapterError("resume artifact does not contain validated source identity")
    partition = payload.get("partition")
    anchors = payload.get("anchor_pages")
    if not isinstance(partition, Mapping) or not isinstance(anchors, list) or len(anchors) != 2:
        raise AdapterError("resume artifact is missing its partition or bracketing anchors")
    if partition.get("dimension") != dimension or partition.get("value") != value:
        raise AdapterError("resume artifact partition does not match the requested filter")
    if _anchor_signature(anchors[0]) != _anchor_signature(anchors[1]):
        raise AdapterError("resume artifact has inconsistent bracketing anchors")
    pages = partition.get("pages")
    if not isinstance(pages, list) or not pages:
        raise AdapterError("resume artifact has no completed partition pages")
    validated: list[dict[str, Any]] = []
    for index, raw_page in enumerate(pages):
        if not isinstance(raw_page, Mapping):
            raise AdapterError("resume artifact contains a malformed partition page")
        page = deepcopy(dict(raw_page))
        ids = list(page.get("stable_ids") or [])
        if (
            page.get("offset") != index * smartrecruiters.MAX_PAGE_SIZE
            or page.get("requested_limit") != smartrecruiters.MAX_PAGE_SIZE
            or page.get("filters") != {dimension: value}
            or page.get("returned_records") != len(ids)
            or len(ids) != len(set(str(item) for item in ids))
        ):
            raise AdapterError("resume artifact pages are not contiguous, filtered, and unique")
        validated.append(page)
    summary = summarize_smartrecruiters_pages(validated)
    if list(partition.get("stable_ids") or []) != [
        stable_id for page in validated for stable_id in page["stable_ids"]
    ]:
        raise AdapterError("resume artifact stable-ID summary contradicts its pages")
    if summary["terminal_evidence"]["observed"]:
        raise AdapterError(
            "resume artifact already records a complete partition; no HTTP requests are needed"
        )
    if summary["failure_reason"] != "authoritative_total_not_reached":
        raise AdapterError("resume artifact partition pages are internally inconsistent")
    if len(validated[-1]["stable_ids"]) != smartrecruiters.MAX_PAGE_SIZE:
        raise AdapterError("resume artifact stopped mid-page without terminal evidence")
    return validated, _anchor_signature(anchors[0])


def _partition_value_evidence(
    content: Iterable[Mapping[str, Any]], dimension: str, expected: str
) -> tuple[list[str], int]:
    values = [_posting_partition_value(item, dimension) for item in content]
    unexpected = sorted({str(item) for item in values if item is not None and item != expected})
    return unexpected, sum(item is None for item in values)


def probe_smartrecruiters_partition(
    session: BoundedProbeSession,
    target: CompanyTarget,
    timeout: int,
    population_filter: tuple[str, str],
    resume_payload: Mapping[str, Any] | None = None,
    resume_label: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enumerate or safely resume one population between stable anchor reads."""
    smartrecruiters.validate_target(target)
    dimension, value = population_filter
    prior_pages: list[dict[str, Any]] = []
    prior_anchor_signature: str | None = None
    if resume_payload is not None:
        prior_pages, prior_anchor_signature = _validated_resume_pages(
            resume_payload,
            company=target.name,
            dimension=dimension,
            value=value,
        )
    session.set_phase("anchor")
    first_anchor, first_content, first_payload = _smartrecruiters_page(
        session, target, timeout, offset=0
    )
    observed_values = sorted(
        {
            observed
            for item in first_content
            if (observed := _posting_partition_value(item, dimension)) is not None
        }
    )
    absent_observed = any(
        _posting_partition_value(item, dimension) is None for item in first_content
    )
    if value not in observed_values:
        raise AdapterError(
            f"requested {dimension} population was not observed in the current anchor page"
        )

    current_anchor_signature = _anchor_signature(first_anchor)
    if prior_anchor_signature is not None and current_anchor_signature != prior_anchor_signature:
        partition = {
            "dimension": dimension,
            "value": value,
            "pages": prior_pages,
            **summarize_smartrecruiters_pages(prior_pages),
            "stable_ids": [item for page in prior_pages for item in page["stable_ids"]],
        }
        return {
            "official_filter_contract": {
                "documentation": SMARTRECRUITERS_POSTING_DOC,
                "supported_dimensions": sorted(SMARTRECRUITERS_PARTITION_DIMENSIONS),
                "tested_filter": {dimension: value},
            },
            "anchor_pages": [first_anchor],
            "anchor_total_stable": False,
            "anchor_identity_stable": False,
            "source_observed_dimension_values": observed_values,
            "absent_dimension_observed_in_anchor": absent_observed,
            "partition": partition,
            "population_summary": {
                "global_population_complete": False,
                "failure_reason": "resume_snapshot_mismatch",
            },
            "retrieved_records": partition["retrieved_records"],
            "unique_stable_ids": partition["unique_stable_ids"],
            "duplicate_ids": partition["duplicate_ids"],
            "canonical_url_collisions": {},
            "terminal_evidence": partition["terminal_evidence"],
            "source_identity_evidence": {"status": "validated", "company_identifier": target.slug},
            "collection_mutation": True,
            "rejected_record_count": 0,
            "failure_reason": "resume_snapshot_mismatch",
            "resume": {
                "used": True,
                "artifact": resume_label,
                "skipped_completed_offsets": [page["offset"] for page in prior_pages],
            },
        }, {"company": target.name, "adapter": target.platform, "pages": []}

    session.set_phase("partition")
    pages: list[dict[str, Any]] = list(prior_pages)
    fixture_pages: list[dict[str, Any]] = []
    while session.requests_started < session.request_limit - 1:
        offset = len(pages) * smartrecruiters.MAX_PAGE_SIZE
        page, content, payload = _smartrecruiters_page(
            session,
            target,
            timeout,
            offset=offset,
            filters={dimension: value},
        )
        unexpected, absent_count = _partition_value_evidence(content, dimension, value)
        page["unexpected_partition_values"] = unexpected
        page["absent_partition_value_count"] = absent_count
        page["partition_value_validated"] = not unexpected and absent_count == 0
        pages.append(page)
        fixture_pages.append(_smartrecruiters_listing_fixture(payload, content, offset))
        if len(content) < smartrecruiters.MAX_PAGE_SIZE or offset + len(content) >= page["advertised_total"]:
            break

    session.set_phase("anchor")
    final_anchor, final_content, final_payload = _smartrecruiters_page(
        session, target, timeout, offset=0
    )
    anchors_match = (
        first_anchor["advertised_total"] == final_anchor["advertised_total"]
        and first_anchor["stable_ids"] == final_anchor["stable_ids"]
        and (
            prior_anchor_signature is None
            or _anchor_signature(final_anchor) == prior_anchor_signature
        )
    )
    partition_summary = summarize_smartrecruiters_pages(pages)
    unexpected_values = sorted(
        {
            item
            for page in pages
            for item in (page.get("unexpected_partition_values") or [])
        }
    )
    absent_partition_values = sum(
        int(page.get("absent_partition_value_count") or 0) for page in pages
    )
    partition_values_validated = all(
        page.get("partition_value_validated") is True for page in pages
    )
    partition = {
        "dimension": dimension,
        "value": value,
        "pages": pages,
        **partition_summary,
        "stable_ids": [stable_id for page in pages for stable_id in page["stable_ids"]],
        "unexpected_partition_values": unexpected_values,
        "absent_partition_value_count": absent_partition_values,
        "partition_values_validated": partition_values_validated,
    }
    population_summary = summarize_population_partitions(
        [partition],
        baseline_total=first_anchor["advertised_total"],
        anchors_match=anchors_match,
        observed_dimension_values=observed_values,
        absent_dimension_observed=absent_observed,
        partition_values_validated=partition_values_validated,
    )
    diagnostic = {
        "official_filter_contract": {
            "documentation": SMARTRECRUITERS_POSTING_DOC,
            "supported_dimensions": sorted(SMARTRECRUITERS_PARTITION_DIMENSIONS),
            "tested_filter": {dimension: value},
        },
        "anchor_pages": [first_anchor, final_anchor],
        "anchor_total_stable": first_anchor["advertised_total"] == final_anchor["advertised_total"],
        "anchor_identity_stable": first_anchor["stable_ids"] == final_anchor["stable_ids"],
        "source_observed_dimension_values": observed_values,
        "absent_dimension_observed_in_anchor": absent_observed,
        "partition": partition,
        "population_summary": population_summary,
        "retrieved_records": partition_summary["retrieved_records"],
        "unique_stable_ids": partition_summary["unique_stable_ids"],
        "duplicate_ids": partition_summary["duplicate_ids"],
        "canonical_url_collisions": {},
        "terminal_evidence": partition_summary["terminal_evidence"],
        "source_identity_evidence": {
            "status": "validated",
            "company_identifier": target.slug,
        },
        "collection_mutation": not anchors_match,
        "rejected_record_count": 0,
        "failure_reason": population_summary["failure_reason"],
        "resume": {
            "used": resume_payload is not None,
            "artifact": resume_label,
            "skipped_completed_offsets": [page["offset"] for page in prior_pages],
            "new_offsets": [page["offset"] for page in pages[len(prior_pages):]],
        },
        "detail_enrichment": {
            "status": "not_requested",
            "reason": "listing enumeration is intentionally separate from optional detail enrichment",
        },
    }
    fixture = {
        "company": target.name,
        "adapter": target.platform,
        "mode": "smartrecruiters-partition",
        "filter": {dimension: value},
        "anchors": [
            _smartrecruiters_listing_fixture(first_payload, first_content, 0),
            _smartrecruiters_listing_fixture(final_payload, final_content, 0),
        ],
        "pages": fixture_pages,
    }
    return diagnostic, fixture


def reconcile_partition_diagnostics(
    payloads: Iterable[Mapping[str, Any]], *, company: str
) -> dict[str, Any]:
    """Offline-only reconciliation of immutable partition diagnostics."""
    diagnostics = list(payloads)
    partitions: list[dict[str, Any]] = []
    observed_values: set[str] = set()
    baseline_totals: set[int] = set()
    anchor_signatures: set[str] = set()
    dimensions: set[str] = set()
    anchors_match = True
    absent_observed = False
    validation_complete = True
    artifact_errors: list[str] = []
    for index, payload in enumerate(diagnostics, start=1):
        if (
            payload.get("company") != company
            or payload.get("mode") != "smartrecruiters-partition"
            or payload.get("status") != "diagnostic_complete"
            or not isinstance(payload.get("source_identity_evidence"), Mapping)
            or payload["source_identity_evidence"].get("status") != "validated"
        ):
            artifact_errors.append(f"artifact_{index}_identity_or_status")
            continue
        partition = payload.get("partition")
        anchors = payload.get("anchor_pages")
        if not isinstance(partition, Mapping) or not isinstance(anchors, list) or len(anchors) != 2:
            artifact_errors.append(f"artifact_{index}_missing_partition_or_anchors")
            continue
        copied = deepcopy(dict(partition))
        dimension = str(copied.get("dimension") or "")
        value = str(copied.get("value") or "")
        pages = copied.get("pages")
        if not isinstance(pages, list) or not pages:
            artifact_errors.append(f"artifact_{index}_missing_partition_pages")
            continue
        recomputed = summarize_smartrecruiters_pages(pages)
        flattened_ids = [
            stable_id for page in pages for stable_id in (page.get("stable_ids") or [])
        ]
        pages_well_formed = all(
            isinstance(page, Mapping)
            and page.get("offset") == page_index * smartrecruiters.MAX_PAGE_SIZE
            and page.get("requested_limit") == smartrecruiters.MAX_PAGE_SIZE
            and page.get("filters") == {dimension: value}
            and page.get("returned_records") == len(page.get("stable_ids") or [])
            for page_index, page in enumerate(pages)
        )
        if (
            not pages_well_formed
            or recomputed["failure_reason"] is not None
            or not recomputed["terminal_evidence"]["observed"]
            or list(copied.get("stable_ids") or []) != flattened_ids
        ):
            artifact_errors.append(f"artifact_{index}_partition_not_terminal_or_inconsistent")
            continue
        partitions.append(copied)
        dimensions.add(dimension)
        observed_values.update(
            str(value) for value in (payload.get("source_observed_dimension_values") or [])
        )
        absent_observed = absent_observed or bool(
            payload.get("absent_dimension_observed_in_anchor")
        )
        validation_complete = validation_complete and (
            copied.get("partition_values_validated") is True
        )
        first_signature = _anchor_signature(anchors[0])
        final_signature = _anchor_signature(anchors[1])
        anchors_match = anchors_match and first_signature == final_signature
        anchor_signatures.update((first_signature, final_signature))
        for anchor in anchors:
            total = anchor.get("advertised_total")
            if isinstance(total, int) and not isinstance(total, bool):
                baseline_totals.add(total)
            else:
                artifact_errors.append(f"artifact_{index}_invalid_global_total")

    values = [str(partition.get("value") or "") for partition in partitions]
    duplicate_population_values = sorted(
        value for value, count in Counter(values).items() if value and count > 1
    )
    snapshots_consistent = (
        not artifact_errors
        and anchors_match
        and len(anchor_signatures) == 1
        and len(baseline_totals) == 1
    )
    baseline_total = next(iter(baseline_totals), 0)
    summary = summarize_population_partitions(
        partitions,
        baseline_total=baseline_total,
        anchors_match=anchors_match,
        observed_dimension_values=observed_values,
        absent_dimension_observed=absent_observed,
        snapshots_consistent=snapshots_consistent,
        partition_values_validated=validation_complete,
    )
    if artifact_errors:
        summary["failure_reason"] = "invalid_partition_artifacts"
    elif len(dimensions) != 1 or "" in dimensions:
        summary["failure_reason"] = "mixed_partition_dimensions"
    elif len(baseline_totals) != 1:
        summary["failure_reason"] = "aggregate_total_contradiction"
    elif duplicate_population_values:
        summary["failure_reason"] = "duplicate_population_partitions"
    summary["global_population_complete"] = summary["failure_reason"] is None
    if summary["global_population_complete"]:
        summary["exhaustive_coverage_basis"] = (
            "stable_authoritative_total_equals_non-overlapping_unique_partition_union"
        )
    else:
        summary["exhaustive_coverage_basis"] = None
    summary.update(
        {
            "company": company,
            "artifact_count": len(diagnostics),
            "accepted_partition_count": len(partitions),
            "partition_dimension": next(iter(dimensions)) if len(dimensions) == 1 else None,
            "partition_values": sorted(set(values)),
            "duplicate_population_partitions": duplicate_population_values,
            "artifact_errors": artifact_errors,
            "anchor_snapshot_signatures": sorted(anchor_signatures),
            "observed_values_are_exhaustive_list": False,
        }
    )
    return summary


def safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())[:1000]
    text = re.sub(r"(?i)(authorization|cookie|token|secret|password)=?\S*", r"\1=<redacted>", text)
    return text


def _artifact_directory() -> Path:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for _attempt in range(10):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = OUTPUT_ROOT / f"{stamp}_{secrets.token_hex(3)}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a non-overwriting diagnostic directory")


def _write_new_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True, sort_keys=True)
        handle.write("\n")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def run_company(
    target: CompanyTarget,
    *,
    request_limit: int,
    timeout: int,
    mode: str = "listing",
    population_filter: tuple[str, str] | None = None,
    xml_response_limit: int = MAX_XML_RESPONSE_BYTES,
    resume_payload: Mapping[str, Any] | None = None,
    resume_label: str | None = None,
    partition_payloads: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = BoundedProbeSession(
        allowed_hosts=_approved_source_hosts(target),
        request_limit=request_limit,
        phase_limits={"xml": MAX_XML_REQUESTS},
    )
    started = datetime.now(timezone.utc).isoformat()
    base = {
        "schema_version": 3,
        "company": target.name,
        "mode": mode,
        "adapter": target.platform,
        "endpoint_variant": target.endpoint_variant,
        "official_source_endpoint": sanitize_url(
            str(target.endpoint_url or target.listing_url)
        ),
        "request_limit": request_limit,
        "timeout_seconds": timeout,
        "started_at": started,
    }
    fixture: dict[str, Any] = {"company": target.name, "adapter": target.platform}
    try:
        if mode == "xml":
            if target.name not in STATIC_COMPANIES:
                raise AdapterError("XML mode is limited to the three static Batch 9 companies")
            evidence, fixture = probe_xml(
                session, target, timeout, response_limit=xml_response_limit
            )
        elif mode == "smartrecruiters-partition":
            if target.name not in SMARTRECRUITERS_COMPANIES:
                raise AdapterError("partition mode is limited to Bosch and SGS")
            if population_filter is None:
                raise AdapterError("partition mode requires one population filter")
            if request_limit < 3:
                raise AdapterError("partition mode requires at least three requests")
            evidence, fixture = probe_smartrecruiters_partition(
                session,
                target,
                timeout,
                population_filter,
                resume_payload=resume_payload,
                resume_label=resume_label,
            )
        elif mode == "smartrecruiters-reconcile":
            if target.name not in SMARTRECRUITERS_COMPANIES:
                raise AdapterError("reconciliation mode is limited to Bosch and SGS")
            payloads = list(partition_payloads or [])
            if not payloads:
                raise AdapterError("reconciliation mode requires partition artifacts")
            population_summary = reconcile_partition_diagnostics(
                payloads, company=target.name
            )
            evidence = {
                "population_summary": population_summary,
                "retrieved_records": population_summary["aggregate_unique_ids"],
                "unique_stable_ids": population_summary["aggregate_unique_ids"],
                "duplicate_ids": sorted(population_summary["population_overlap"]),
                "canonical_url_collisions": {},
                "terminal_evidence": {
                    "observed": population_summary["global_population_complete"],
                    "type": (
                        "stable_global_authoritative_total"
                        if population_summary["global_population_complete"]
                        else None
                    ),
                    "value": (
                        population_summary["baseline_total"]
                        if population_summary["global_population_complete"]
                        else None
                    ),
                },
                "source_identity_evidence": {
                    "status": "validated" if not population_summary["artifact_errors"] else "failed",
                    "company_identifier": target.slug,
                },
                "collection_mutation": not population_summary["snapshots_consistent"],
                "rejected_record_count": 0,
                "failure_reason": population_summary["failure_reason"],
                "offline_reconciliation": True,
                "feed_activated_for_production": False,
            }
            fixture = {
                "company": target.name,
                "adapter": target.platform,
                "mode": "smartrecruiters-reconcile",
                "partition_count": len(payloads),
            }
        elif target.name in SMARTRECRUITERS_COMPANIES:
            evidence, fixture = probe_smartrecruiters(session, target, timeout)
        elif target.name in STATIC_COMPANIES:
            evidence, fixture = probe_static(session, target, timeout)
        else:
            raise AdapterError("company is outside the Batch 9 diagnostic allowlist")
        status = "diagnostic_complete"
        error = None
    except Exception as exc:
        evidence = {
            "failure_reason": safe_error(exc),
            "retrieved_records": 0,
            "unique_stable_ids": 0,
            "duplicate_ids": [],
            "canonical_url_collisions": {},
            "terminal_evidence": {},
            "source_identity_evidence": {"status": "unproven"},
            "collection_mutation": False,
            "rejected_record_count": 0,
        }
        status = "failed"
        error = f"{type(exc).__name__}: {safe_error(exc)}"
    finally:
        session.close()
    diagnostic = {
        **base,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "http_statuses": [item.get("status") for item in session.request_log],
        "requests_started": session.requests_started,
        "request_log": session.request_log,
        "phase_requests_started": dict(session.phase_requests_started),
        "approved_source_hosts": sorted(session.allowed_hosts),
        "host_remained_approved": session.host_remained_approved,
        "error": error,
        **evidence,
    }
    return diagnostic, fixture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--company",
        action="append",
        required=True,
        choices=ALLOWED_COMPANIES,
        help="Explicit Batch 9 company allowlist entry; repeat to select more than one.",
    )
    parser.add_argument(
        "--mode",
        choices=PROBE_MODES,
        default="listing",
        help="Diagnostic contract to execute (default: listing).",
    )
    parser.add_argument(
        "--population-filter",
        help="For partition mode only: one documented country=VALUE or department=VALUE filter.",
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="For partition mode only: one prior incomplete diagnostic artifact.",
    )
    parser.add_argument(
        "--partition-artifact",
        action="append",
        default=[],
        type=Path,
        help="For offline reconciliation only; repeat for each completed country partition.",
    )
    parser.add_argument(
        "--request-limit",
        action="append",
        default=[],
        metavar="COMPANY=COUNT",
        help=f"Per-company hard ceiling (default {DEFAULT_REQUEST_LIMIT}, maximum {MAX_REQUEST_LIMIT}).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-request timeout in seconds (1-{MAX_TIMEOUT}).",
    )
    parser.add_argument(
        "--xml-response-limit",
        action="append",
        default=[],
        metavar="COMPANY=BYTES",
        help=(
            "XML-only confirmed-feed override; greater than "
            f"{MAX_XML_RESPONSE_BYTES} and at most {MAX_XML_RESPONSE_OVERRIDE_BYTES} bytes."
        ),
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= MAX_TIMEOUT:
        parser.error(f"--timeout must be between 1 and {MAX_TIMEOUT}")
    try:
        configured_limits = parse_request_limits(args.request_limit)
        xml_response_limits = parse_xml_response_limits(args.xml_response_limit)
    except ValueError as exc:
        parser.error(str(exc))
    selected = list(dict.fromkeys(args.company))
    population_filter: tuple[str, str] | None = None
    resume_payload: Mapping[str, Any] | None = None
    partition_payloads: list[Mapping[str, Any]] = []
    if args.mode == "xml" and not set(selected).issubset(STATIC_COMPANIES):
        parser.error("--mode xml accepts only General Motors, Caterpillar, and Coupang")
    if args.mode == "smartrecruiters-partition":
        if len(selected) != 1 or selected[0] not in SMARTRECRUITERS_COMPANIES:
            parser.error("partition mode accepts exactly one of Bosch or SGS")
        if not args.population_filter:
            parser.error("partition mode requires --population-filter")
        try:
            population_filter = parse_population_filter(args.population_filter)
        except ValueError as exc:
            parser.error(str(exc))
        if args.partition_artifact:
            parser.error("--partition-artifact is valid only in smartrecruiters-reconcile mode")
        if args.resume_from:
            try:
                resume_payload = load_diagnostic_artifact(args.resume_from)
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
    elif args.mode == "smartrecruiters-reconcile":
        if len(selected) != 1 or selected[0] not in SMARTRECRUITERS_COMPANIES:
            parser.error("reconciliation mode accepts exactly one of Bosch or SGS")
        if not args.partition_artifact:
            parser.error("reconciliation mode requires at least one --partition-artifact")
        if args.population_filter or args.resume_from:
            parser.error("reconciliation mode does not accept a population filter or resume artifact")
        if args.request_limit:
            parser.error("offline reconciliation does not accept --request-limit")
        try:
            partition_payloads = [
                load_diagnostic_artifact(path) for path in args.partition_artifact
            ]
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    elif args.population_filter:
        parser.error("--population-filter is valid only in smartrecruiters-partition mode")
    elif args.resume_from or args.partition_artifact:
        parser.error("resume/partition artifacts require a SmartRecruiters diagnostic mode")
    if args.mode != "xml" and xml_response_limits:
        parser.error("--xml-response-limit is valid only in XML mode")
    invalid_xml_limits = sorted(set(xml_response_limits) - set(selected))
    if invalid_xml_limits:
        parser.error(f"XML response limits supplied for unselected companies: {invalid_xml_limits}")
    unused_limits = sorted(set(configured_limits) - set(selected))
    if unused_limits:
        parser.error(f"request limits supplied for unselected companies: {unused_limits}")

    configured = {
        target.name: target
        for target in load_target_companies(apply_reliability_repairs=True)
        if target.name in set(selected)
    }
    missing = sorted(set(selected) - configured.keys())
    if missing:
        parser.error(f"effective registry is missing requested companies: {missing}")

    output_dir = _artifact_directory()
    manifest = {
        "schema_version": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "companies": [],
        "production_state_written": False,
    }
    failures = 0
    for name in selected:
        limit = configured_limits.get(name, DEFAULT_REQUEST_LIMIT)
        diagnostic, fixture = run_company(
            configured[name],
            request_limit=limit,
            timeout=args.timeout,
            mode=args.mode,
            population_filter=population_filter,
            xml_response_limit=xml_response_limits.get(name, MAX_XML_RESPONSE_BYTES),
            resume_payload=resume_payload,
            resume_label=args.resume_from.name if args.resume_from else None,
            partition_payloads=partition_payloads,
        )
        slug = _slug(name)
        diagnostic_path = output_dir / f"{slug}.diagnostic.json"
        fixture_path = output_dir / f"{slug}.fixture.json"
        _write_new_json(diagnostic_path, diagnostic)
        _write_new_json(fixture_path, fixture)
        manifest["companies"].append(
            {
                "company": name,
                "diagnostic": diagnostic_path.name,
                "fixture": fixture_path.name,
                "status": diagnostic["status"],
                "requests_started": diagnostic["requests_started"],
            }
        )
        failures += diagnostic["status"] != "diagnostic_complete"
    _write_new_json(output_dir / "manifest.json", manifest)
    print(str(output_dir.relative_to(ROOT)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
