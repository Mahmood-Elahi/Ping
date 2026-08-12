"""Reconcile and investigate companies not covered by the production registry.

This research utility is deliberately isolated from scanning and publishing code. It
only reads the workbook, registry, research CSV, and prior fingerprints; performs
bounded anonymous HTTPS GET requests; and writes versioned research artifacts.
"""

from __future__ import annotations

import argparse
import csv
import html
import ipaddress
import json
import re
import socket
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import yaml

try:
    from scripts.company_watchlist_common import (
        _safe_public_https_target,
        canonicalize_url,
        clean_text,
        normalize_company_name as compact_company_name,
    )
    from scripts.enrich_company_watchlist import read_workbook
    from scripts.fingerprint_company_platforms import (
        FINGERPRINT_FIELDS as PREVIOUS_FINGERPRINT_FIELDS,
        FetchEvidence,
        PARENT_SOURCE_RULES,
        PlatformDetection,
        detect_platform,
        parent_source_detection,
    )
except ModuleNotFoundError:
    from company_watchlist_common import (  # type: ignore[no-redef]
        _safe_public_https_target,
        canonicalize_url,
        clean_text,
        normalize_company_name as compact_company_name,
    )
    from enrich_company_watchlist import read_workbook  # type: ignore[no-redef]
    from fingerprint_company_platforms import (  # type: ignore[no-redef]
        FINGERPRINT_FIELDS as PREVIOUS_FINGERPRINT_FIELDS,
        FetchEvidence,
        PARENT_SOURCE_RULES,
        PlatformDetection,
        detect_platform,
        parent_source_detection,
    )


MAX_BATCH_SIZE = 25
MAX_RESPONSE_BYTES = 3_000_000
DEFAULT_INPUT = Path("config/company_watchlist.xlsx")
DEFAULT_CONFIG = Path("config/target_companies.yaml")
DEFAULT_RESEARCH = Path("config/company_watchlist_research.csv")
DEFAULT_PREVIOUS = Path("config/company_platform_fingerprints.csv")
DEFAULT_OUTPUT = Path("config/company_platform_fingerprints_v2.csv")
DEFAULT_CLUSTERS = Path("config/company_platform_clusters_v2.csv")
DEFAULT_INVENTORY = Path("config/company_remaining_inventory_v2.csv")
DEFAULT_PARENTS = Path("config/company_parent_source_candidates_v2.csv")
DEFAULT_JSON = Path("output/company_discovery_v2.json")
DEFAULT_MARKDOWN = Path("output/company_discovery_v2.md")
DEFAULT_FAILURES = Path("output/company_discovery_failures_v2.json")

# These are explicit equivalences, never substring matches. Additions require review.
COMPANY_ALIASES: dict[str, str] = {
    "facebook": "meta",
    "alphabet": "google",
}

ADDITIONAL_PARENT_SOURCE_RULES: dict[str, dict[str, str]] = {
    "x": {
        "parent": "xAI",
        "parameters": "team/business=X; safe structured filter not yet evidenced",
        "notes": "The official careers.x.com URL redirects to x.ai/careers; xAI is already a production source, so source-level deduplication is required.",
    },
}

CORPORATE_SUFFIXES = {
    "co", "company", "corp", "corporation", "inc", "incorporated", "ltd",
    "limited", "llc", "plc", "group", "holdings",
}

EXISTING_ADAPTER_FAMILIES = {
    "adp", "amazon", "ashby", "atlassian", "eightfold", "google", "greenhouse",
    "hawksearch_v2", "hibob", "ibm_search", "lever", "meta", "oracle_recruiting", "phenom",
    "cornerstone_csod", "deel", "sap_successfactors", "shopify", "smartrecruiters",
    "talentbrew", "talentbrew_radancy", "teamtailor", "uber", "workday",
}

FINGERPRINT_FIELDS = [
    "company", "normalized_company", "original_careers_url",
    "current_official_careers_url", "current_job_search_url", "final_url",
    "http_status", "redirect_chain", "page_title", "platform_family",
    "platform_variant", "confidence", "evidence", "endpoint_host",
    "public_search_endpoint", "request_method", "structured_data_type",
    "stable_job_id_evidence", "official_apply_url_evidence",
    "pagination_evidence", "anonymous_access", "cookies_required",
    "csrf_required", "authentication_required", "session_initialization_required",
    "javascript_execution_required", "browser_automation_likely_required",
    "anti_bot_or_access_block_evidence", "parent_company",
    "parent_source_candidate", "duplicate_source_candidate",
    "previous_platform_family", "previous_confidence", "stale_fingerprint",
    "current_classification", "recommended_next_action", "technical_priority",
    "career_relevance_priority", "combined_priority", "urls_tested",
    "investigation_timestamp",
]

INVENTORY_FIELDS = [
    "original_rank", "company", "normalized_company", "original_priority",
    "original_careers_url", "original_notes", "current_official_careers_url",
    "current_job_search_url", "production_covered", "production_name",
    "production_platform", "production_source_identifier", "coverage_relationship",
    "company_specific_collector", "currently_uncovered", "current_platform_family",
    "endpoint_variant", "confidence", "http_status", "final_url",
    "stable_job_id_evidence", "official_apply_url_evidence", "pagination_evidence",
    "anonymous_access", "browser_required", "authentication_required", "blocked",
    "stale_fingerprint", "parent_company", "parent_source_candidate",
    "duplicate_source_candidate", "current_classification",
    "recommended_next_action", "technical_priority", "career_relevance_priority",
    "combined_priority", "evidence", "investigation_timestamp",
]

CLUSTER_FIELDS = [
    "cluster_rank", "platform_family", "endpoint_variant", "company_count",
    "companies", "confidence_summary", "existing_adapter_available",
    "existing_adapter_extension_possible", "new_adapter_required",
    "parent_mapping_possible", "browser_required_count", "anonymous_endpoint_count",
    "stable_id_count", "official_apply_url_count", "pagination_model",
    "estimated_complexity", "estimated_companies_unlocked", "technical_priority",
    "career_relevance_priority", "combined_priority", "recommended_action", "supporting_evidence",
]

PARENT_FIELDS = [
    "company", "parent_company", "parent_source_candidate", "official_careers_url",
    "official_job_search_url", "filter_evidence", "filter_deterministic",
    "duplicate_job_risk", "keep_brand_visible", "mapping_safety", "confidence",
    "evidence", "recommended_action",
]


def normalize_company_name(value: Any) -> str:
    """Readable deterministic normalization used in artifacts."""
    text = clean_text(value).casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def suffix_normalized_key(value: Any) -> str:
    tokens = normalize_company_name(value).split()
    while tokens and tokens[-1] in CORPORATE_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def source_identifier(entry: Mapping[str, Any]) -> str:
    platform = clean_text(entry.get("platform"))
    if platform in {"greenhouse", "lever", "ashby"}:
        return clean_text(entry.get("slug"))
    if platform == "workday":
        return "/".join(clean_text(entry.get(key)) for key in ("tenant", "wd_subdomain", "site_id"))
    if platform in {"oracle_recruiting", "phenom", "eightfold", "sap_successfactors"}:
        return clean_text(entry.get("api_url") or entry.get("base_url") or entry.get("careers_url") or entry.get("site_id"))
    return clean_text(entry.get("source_url") or entry.get("careers_url") or platform)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{key: clean_text(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean_text(row.get(field)) for field in fields})
    temporary.replace(path)


@dataclass(frozen=True)
class MappingReport:
    original_count: int
    covered_count: int
    uncovered_count: int
    production_only: list[str]
    ambiguous: list[dict[str, Any]]


def reconcile_companies(
    workbook_rows: list[Mapping[str, Any]], registry_entries: list[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], MappingReport]:
    """Map exact/explicit/suffix-safe names and surface collisions as ambiguous."""
    registry_exact: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    registry_suffix: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in registry_entries:
        registry_exact[compact_company_name(entry.get("name"))].append(entry)
        registry_suffix[suffix_normalized_key(entry.get("name"))].append(entry)

    represented: set[str] = set()
    ambiguous: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for original in workbook_rows:
        name = clean_text(original.get("Company"))
        exact_key = compact_company_name(name)
        candidates = list(registry_exact.get(exact_key, []))
        relationship = "direct"
        alias_key = COMPANY_ALIASES.get(exact_key)
        if not candidates and alias_key:
            candidates = list(registry_exact.get(alias_key, []))
            relationship = "explicit_alias"
        if not candidates:
            suffix_candidates = registry_suffix.get(suffix_normalized_key(name), [])
            if len(suffix_candidates) == 1:
                candidates = list(suffix_candidates)
                relationship = "corporate_suffix_variant"
            elif len(suffix_candidates) > 1:
                ambiguous.append({"company": name, "candidates": [clean_text(x.get("name")) for x in suffix_candidates]})
        if len(candidates) > 1:
            ambiguous.append({"company": name, "candidates": [clean_text(x.get("name")) for x in candidates]})
            candidates = []
        production = candidates[0] if candidates else None
        if production:
            represented.add(compact_company_name(production.get("name")))
        rows.append({
            **dict(original),
            "_production": production,
            "_coverage_relationship": relationship if production else "uncovered",
        })
    production_only = sorted(
        clean_text(entry.get("name")) for entry in registry_entries
        if compact_company_name(entry.get("name")) not in represented
    )
    covered = sum(row["_production"] is not None for row in rows)
    return rows, MappingReport(len(rows), covered, len(rows) - covered, production_only, ambiguous)


@dataclass
class DetailedEvidence:
    requested_url: str
    final_url: str = ""
    status_code: int = 0
    redirect_chain: list[dict[str, Any]] | None = None
    content_type: str = ""
    text: str = ""
    title: str = ""
    note: str = ""
    headers: dict[str, str] | None = None

    def base(self) -> FetchEvidence:
        return FetchEvidence(
            self.requested_url, self.final_url, self.status_code, self.content_type,
            self.text, self.note, self.headers or {},
        )


class BoundedResearchClient:
    """Sequential, anonymous, GET-only client with explicit limits."""

    def __init__(self, *, request_delay: float, timeout: int, max_redirects: int,
                 retries: int = 1, session: Optional[requests.Session] = None,
                 sleep_fn: Any = time.sleep) -> None:
        if request_delay < 0:
            raise ValueError("request_delay cannot be negative")
        if not 1 <= timeout <= 60:
            raise ValueError("timeout must be between 1 and 60")
        if not 0 <= max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")
        if not 0 <= retries <= 2:
            raise ValueError("retries must be between 0 and 2")
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.retries = retries
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self.requests_made = 0

    def fetch(self, url: str) -> DetailedEvidence:
        requested = request_url(url)
        current = requested
        chain: list[dict[str, Any]] = []
        if not current:
            return DetailedEvidence(url, note="Missing careers URL", redirect_chain=[])
        for redirect_index in range(self.max_redirects + 1):
            safe, reason = safe_research_target(current)
            if not safe:
                return DetailedEvidence(requested, current, redirect_chain=chain, note=reason)
            try:
                response = self._get(current)
            except requests.RequestException as exc:
                return DetailedEvidence(requested, current, redirect_chain=chain,
                                        note=f"Request failed: {exc.__class__.__name__}: {exc}")
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    chain.append({"url": current, "status": status, "location": location})
                    if not location:
                        return DetailedEvidence(requested, current, status, chain, note="Redirect without Location", headers=dict(response.headers))
                    if redirect_index == self.max_redirects:
                        return DetailedEvidence(requested, current, status, chain, note="Too many redirects", headers=dict(response.headers))
                    current = request_url(urljoin(current, location))
                    continue
                try:
                    raw = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
                except Exception as exc:  # urllib3 can raise after requests returned a streamed response.
                    chain.append({"url": current, "status": status})
                    return DetailedEvidence(
                        requested, current, status, chain,
                        response.headers.get("Content-Type", ""), note=
                        f"Response read failed: {exc.__class__.__name__}: {exc}",
                        headers=dict(response.headers),
                    )
                if len(raw) > MAX_RESPONSE_BYTES:
                    return DetailedEvidence(requested, current, status, chain, note="Response exceeded 3 MB", headers=dict(response.headers))
                text = raw.decode(response.encoding or "utf-8", errors="replace")
                title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
                title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:300] if title_match else ""
                chain.append({"url": current, "status": status})
                return DetailedEvidence(
                    requested, current, status, chain, response.headers.get("Content-Type", ""),
                    text, title, f"HTTP {status}", dict(response.headers),
                )
            finally:
                response.close()
        return DetailedEvidence(requested, current, redirect_chain=chain, note="Too many redirects")

    def _get(self, url: str) -> requests.Response:
        last_error: Optional[requests.RequestException] = None
        for attempt in range(self.retries + 1):
            if self.requests_made:
                self.sleep_fn(self.request_delay)
            self.requests_made += 1
            try:
                response = self.session.get(
                    url, allow_redirects=False, timeout=self.timeout, stream=True,
                    headers={
                        "User-Agent": "JobRadarCompanyResearch/2.0",
                        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
                    },
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    response.close()
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


def request_url(value: Any) -> str:
    """Normalize a request target without erasing a meaningful trailing slash."""
    text = clean_text(value)
    if not text:
        return ""
    parsed = urlsplit(text if urlsplit(text).scheme else "https://" + text)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        return text
    try:
        port = parsed.port
    except ValueError:
        return text
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def safe_research_target(url: str) -> tuple[bool, str]:
    """Apply common SSRF checks, allowing LinkedIn's official corporate careers host."""
    allowed, note = _safe_public_https_target(url)
    host = (urlsplit(url).hostname or "").casefold()
    if allowed or host != "careers.linkedin.com" or "job-board" not in note:
        return allowed, note
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        return False, f"Hostname resolution failed: {exc}"
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        return False, "Private or non-global network targets are not allowed"
    return True, ""


def endpoint_variant(detection: PlatformDetection) -> str:
    parsed = urlsplit(detection.endpoint_url)
    host = parsed.hostname or ""
    if detection.family == "workday":
        return "wday_cxs_v1"
    if detection.family == "oracle_recruiting":
        return "hcmRestApi_recruitingCEJobRequisitions"
    if detection.family == "greenhouse":
        return "boards_api_v1"
    if detection.family == "lever":
        return "postings_api_v0"
    if detection.family == "ashby":
        return "posting_api_job_board"
    if detection.family == "smartrecruiters":
        return "companies_postings_v1"
    if detection.family == "eightfold":
        return "pcsx_search"
    if detection.family == "phenom":
        return "phenom_search_api"
    if detection.family == "sap_successfactors":
        return "career_search"
    if detection.family == "icims":
        return "jobs_search"
    if detection.family == "avature":
        return "portal_search"
    if detection.family == "oracle_taleo":
        return "careersection_jobsearch"
    if detection.family == "parent_company_source":
        return f"shared_{compact_company_name(detection.vendor)}_source"
    if detection.family in {"static_html", "javascript_embedded_data", "browser_required", "unknown"}:
        return detection.family
    return host or detection.family


def structured_type(family: str) -> str:
    if family == "custom_graphql":
        return "GraphQL JSON"
    if family in {"custom_rest_api", "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "oracle_recruiting", "eightfold", "phenom"}:
        return "JSON API"
    if family == "javascript_embedded_data":
        return "embedded JSON/JSON-LD"
    if family == "static_html":
        return "server-rendered HTML"
    return "unknown"


def classify_access(evidence: DetailedEvidence, detection: PlatformDetection) -> tuple[str, str, str]:
    body = evidence.text.casefold()
    invalid_note_markers = (
        "missing careers url", "only https careers urls are allowed", "has no hostname",
        "third-party job-board urls are prohibited", "credentials in careers urls",
    )
    if not evidence.requested_url or (evidence.status_code == 0 and any(marker in evidence.note.casefold() for marker in invalid_note_markers)):
        return "invalid_careers_url", "unknown", "false"
    if evidence.status_code == 0:
        return "inaccessible", "unknown", "false"
    if evidence.status_code == 401 or (evidence.status_code == 403 and re.search(r"(?:sign in|log in).{0,80}(?:to view|required)", body)):
        return "authentication_required", "false", "false"
    if evidence.status_code in {403, 429} or detection.anti_bot == "true":
        return "blocked", "false", "true"
    if evidence.status_code in {404, 410}:
        return "invalid_careers_url", "false", "false"
    inactive_markers = sum(marker in body for marker in (
        "company has ceased operations", "no longer in business", "has shut down",
        "company is closed", "operations have ceased",
    ))
    if inactive_markers >= 1:
        return "inactive_company", "true", "false"
    if detection.family == "parent_company_source":
        return "parent_company_source", "true", "false"
    if detection.family == "browser_required":
        return "browser_required", "true" if evidence.status_code < 400 else "false", "false"
    if detection.family == "unknown":
        return "unknown", "true" if 200 <= evidence.status_code < 300 else "false", "false"
    return detection.family, "true" if 200 <= evidence.status_code < 400 else "false", "false"


def previous_detection(previous: Mapping[str, Any]) -> str:
    return clean_text(previous.get("Detected Platform Family") or previous.get("platform_family"))


def technical_score(detection: PlatformDetection, anonymous: str, classification: str) -> int:
    score = {"high": 50, "medium": 38, "low": 20}.get(detection.confidence, 15)
    if detection.family in EXISTING_ADAPTER_FAMILIES:
        score += 25
    if anonymous == "true":
        score += 10
    if detection.stable_job_id == "true":
        score += 6
    if detection.apply_url == "true":
        score += 5
    if detection.pagination_method not in {"", "unknown"}:
        score += 4
    if classification in {"browser_required", "blocked", "authentication_required"}:
        score -= 30
    if detection.family == "parent_company_source":
        score += 10
    return max(0, min(100, score))


def career_score(source: Mapping[str, Any]) -> int:
    try:
        rank = int(clean_text(source.get("Rank")))
    except ValueError:
        rank = 246
    score = 100 - round((rank - 1) * 45 / 245)
    priority = clean_text(source.get("Priority")).casefold()
    score += {"high": 8, "medium": 4, "low": -4}.get(priority, 0)
    text = f"{source.get('Company', '')} {source.get('Notes', '')}".casefold()
    if any(term in text for term in ("robot", "semiconductor", "software", "technology", " ai", "energy", "automotive")):
        score += 4
    return max(0, min(100, score))


def recommendation(detection: PlatformDetection, classification: str) -> str:
    family = detection.family
    if classification == "parent_company_source":
        return f"Validate deterministic {detection.vendor} filter and deduplicate before mapping"
    if family in EXISTING_ADAPTER_FAMILIES:
        return f"Validate identifiers and reuse existing {family} adapter"
    if family in {"static_html", "javascript_embedded_data"}:
        return f"Validate schema stability; plan a simple {family} collector"
    if family in {"custom_rest_api", "custom_graphql"}:
        return f"Validate endpoint contract and pagination; design a reusable {family} adapter"
    if classification == "browser_required":
        return "Manually inspect browser network activity; do not automate during this milestone"
    if classification in {"blocked", "authentication_required"}:
        return "Manual access review; no bypass or authenticated collection"
    if classification == "inaccessible":
        return "Retry the official URL later and manually verify DNS/TLS availability"
    if classification in {"invalid_careers_url", "inactive_company"}:
        return "Verify corporate status and locate a current official careers source manually"
    return "Manual official-site investigation with recorded network evidence"


def build_record(source: Mapping[str, Any], evidence: DetailedEvidence,
                 previous: Mapping[str, Any], timestamp: str) -> dict[str, str]:
    company = clean_text(source.get("Company"))
    job_url = clean_text(source.get("Official Job Search URL") or source.get("Official Careers URL") or source.get("Careers URL"))
    parent = research_parent_detection(company, job_url, evidence)
    detection = parent or detect_platform(evidence.base())
    classification, anonymous, blocked = classify_access(evidence, detection)
    previous_family = previous_detection(previous)
    stale = "unknown"
    if previous_family:
        stale = "true" if previous_family != detection.family or classification == "invalid_careers_url" else "false"
    cookies = "unknown"
    if evidence.headers is not None:
        cookies = "true" if evidence.headers.get("Set-Cookie") and anonymous != "true" else "false"
    auth = "true" if classification == "authentication_required" else clean_text(detection.authentication_required or "unknown")
    browser = "true" if classification in {"browser_required", "blocked"} else "false"
    tech = technical_score(detection, anonymous, classification)
    career = career_score(source)
    combined = round(tech * 0.65 + career * 0.35)
    parent_rule = parent_rule_for(company)
    evidence_parts = [
        detection.evidence_marker,
        f"{evidence.note} at {evidence.final_url or evidence.requested_url}",
    ]
    if previous_family:
        evidence_parts.append(f"previous={previous_family}/{clean_text(previous.get('Confidence'))}")
    return {
        "company": company,
        "normalized_company": normalize_company_name(company),
        "original_careers_url": clean_text(source.get("Careers URL")),
        "current_official_careers_url": clean_text(source.get("Official Careers URL") or source.get("Careers URL")),
        "current_job_search_url": job_url,
        "final_url": evidence.final_url,
        "http_status": str(evidence.status_code) if evidence.status_code else "",
        "redirect_chain": json.dumps(evidence.redirect_chain or [], ensure_ascii=False, sort_keys=True),
        "page_title": evidence.title,
        "platform_family": detection.family,
        "platform_variant": endpoint_variant(detection),
        "confidence": detection.confidence,
        "evidence": "; ".join(item for item in evidence_parts if item),
        "endpoint_host": urlsplit(detection.endpoint_url).hostname or "",
        "public_search_endpoint": detection.endpoint_url,
        "request_method": detection.endpoint_method or "GET",
        "structured_data_type": structured_type(detection.family),
        "stable_job_id_evidence": detection.stable_job_id,
        "official_apply_url_evidence": detection.apply_url,
        "pagination_evidence": detection.pagination_method,
        "anonymous_access": anonymous,
        "cookies_required": cookies,
        "csrf_required": "unknown" if detection.endpoint_method in {"POST", "GET/POST", "GET/POST (verify)"} else "false",
        "authentication_required": auth,
        "session_initialization_required": "unknown" if cookies == "true" else "false",
        "javascript_execution_required": detection.javascript_required,
        "browser_automation_likely_required": browser,
        "anti_bot_or_access_block_evidence": detection.evidence_marker if blocked == "true" else "",
        "parent_company": clean_text(parent_rule.get("parent")),
        "parent_source_candidate": detection.endpoint_url if parent else "",
        "duplicate_source_candidate": clean_text(parent_rule.get("parent")) if parent else "",
        "previous_platform_family": previous_family,
        "previous_confidence": clean_text(previous.get("Confidence")),
        "stale_fingerprint": stale,
        "current_classification": classification,
        "recommended_next_action": recommendation(detection, classification),
        "technical_priority": str(tech),
        "career_relevance_priority": str(career),
        "combined_priority": str(combined),
        "urls_tested": job_url,
        "investigation_timestamp": timestamp,
    }


def parent_rule_for(company: Any) -> Mapping[str, str]:
    key = compact_company_name(company)
    return PARENT_SOURCE_RULES.get(key) or ADDITIONAL_PARENT_SOURCE_RULES.get(key, {})


def research_parent_detection(company: str, job_url: str,
                              evidence: DetailedEvidence) -> Optional[PlatformDetection]:
    existing = parent_source_detection(company, job_url)
    if existing:
        return existing
    key = compact_company_name(company)
    rule = ADDITIONAL_PARENT_SOURCE_RULES.get(key)
    if not rule:
        return None
    # The new mapping is evidence-gated to the observed official redirect.
    if key == "x" and (urlsplit(evidence.final_url).hostname or "").casefold() != "x.ai":
        return None
    return PlatformDetection(
        "parent_company_source", rule["parent"], "official shared-source redirect",
        f"official redirect={job_url} -> {evidence.final_url}; parent={rule['parent']}",
        "medium", evidence.final_url, "GET", rule["parameters"],
        "parent adapter pagination", "true", "true", "false", "false", "false",
    )


def merge_batch(sources: list[Mapping[str, Any]], completed: list[Mapping[str, Any]],
                previous_by_name: Mapping[str, Mapping[str, Any]], client: Any, *,
                start: int = 0, limit: Optional[int] = None, batch_size: int = 25,
                resume: bool = False, timestamp: Optional[str] = None) -> list[dict[str, str]]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if start < 0:
        raise ValueError("start must be non-negative")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    by_name = {compact_company_name(row.get("company")): dict(row) for row in completed}
    selected = sources[start:]
    if limit is not None:
        selected = selected[:limit]
    run_timestamp = timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset:offset + batch_size]
        for source in batch:
            key = compact_company_name(source.get("Company"))
            if resume and key in by_name and clean_text(by_name[key].get("current_classification")):
                prior_result = by_name[key]
                x_mapping_upgrade = (
                    key == "x"
                    and (urlsplit(clean_text(prior_result.get("final_url"))).hostname or "").casefold() == "x.ai"
                    and clean_text(prior_result.get("current_classification")) != "parent_company_source"
                )
                if not x_mapping_upgrade:
                    continue
            job_url = clean_text(source.get("Official Job Search URL") or source.get("Official Careers URL") or source.get("Careers URL"))
            evidence = client.fetch(job_url)
            by_name[key] = build_record(source, evidence, previous_by_name.get(key, {}), run_timestamp)
    return [by_name[compact_company_name(source.get("Company"))] for source in sources if compact_company_name(source.get("Company")) in by_name]


def refresh_record_metadata(records: list[Mapping[str, Any]],
                            previous_by_name: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Apply evidence metadata upgrades without repeating completed HTTP requests."""
    refreshed: list[dict[str, Any]] = []
    for source in records:
        row = dict(source)
        previous = previous_by_name.get(compact_company_name(row.get("company")), {})
        prior_family = previous_detection(previous)
        if prior_family and not clean_text(row.get("previous_platform_family")):
            row["previous_platform_family"] = prior_family
            row["previous_confidence"] = clean_text(previous.get("Confidence"))
            row["evidence"] = f"{clean_text(row.get('evidence'))}; previous={prior_family}/{clean_text(previous.get('Confidence'))}".strip("; ")
        if prior_family:
            row["stale_fingerprint"] = str(
                prior_family != clean_text(row.get("platform_family"))
                or clean_text(row.get("current_classification")) == "invalid_careers_url"
            ).lower()
        refreshed.append(row)
    return refreshed


def build_clusters(records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        groups[(clean_text(row.get("platform_family")) or "unknown", clean_text(row.get("platform_variant")) or "unknown")].append(row)
    clusters: list[dict[str, str]] = []
    for (family, variant), members in groups.items():
        confidences = Counter(clean_text(row.get("confidence")) or "low" for row in members)
        existing = family in EXISTING_ADAPTER_FAMILIES
        parent = family == "parent_company_source"
        browser_count = sum(clean_text(row.get("browser_automation_likely_required")) == "true" for row in members)
        anonymous_count = sum(clean_text(row.get("anonymous_access")) == "true" for row in members)
        stable_count = sum(clean_text(row.get("stable_job_id_evidence")) == "true" for row in members)
        apply_count = sum(clean_text(row.get("official_apply_url_evidence")) == "true" for row in members)
        actionable_count = sum(
            clean_text(row.get("current_classification")) not in {
                "unknown", "browser_required", "blocked", "authentication_required",
                "inaccessible", "invalid_careers_url", "inactive_company",
            }
            for row in members
        )
        average_tech = round(sum(int(clean_text(row.get("technical_priority")) or 0) for row in members) / len(members))
        average_career = round(sum(int(clean_text(row.get("career_relevance_priority")) or 0) for row in members) / len(members))
        if existing:
            complexity, action = "low", f"Validate source identifiers; reuse existing {family} adapter"
        elif parent:
            complexity, action = "low-medium", "Validate filters and deduplication; map to parent source"
        elif family in {"static_html", "javascript_embedded_data"}:
            complexity, action = "medium", f"Prototype a bounded reusable {family} collector"
        elif family in {"custom_rest_api", "custom_graphql", "smartrecruiters", "icims", "avature", "jobvite", "dayforce", "adp", "oracle_taleo"}:
            complexity, action = "medium-high", f"Design and validate a reusable {family} adapter"
        elif browser_count:
            complexity, action = "high", "Manual browser-network research; defer automation"
        else:
            complexity, action = "unknown", "Manual official-source investigation"
        evidence_samples = sorted({clean_text(row.get("evidence")) for row in members if clean_text(row.get("evidence"))})[:3]
        clusters.append({
            "cluster_rank": "",
            "platform_family": family,
            "endpoint_variant": variant,
            "company_count": str(len(members)),
            "companies": "; ".join(sorted(clean_text(row.get("company")) for row in members)),
            "confidence_summary": "; ".join(f"{key}={confidences[key]}" for key in ("high", "medium", "low")),
            "existing_adapter_available": str(existing).lower(),
            "existing_adapter_extension_possible": str(existing and family in {"eightfold", "oracle_recruiting", "phenom", "sap_successfactors", "workday"}).lower(),
            "new_adapter_required": str(not existing and not parent and family not in {"unknown", "browser_required"}).lower(),
            "parent_mapping_possible": str(parent).lower(),
            "browser_required_count": str(browser_count),
            "anonymous_endpoint_count": str(anonymous_count),
            "stable_id_count": str(stable_count),
            "official_apply_url_count": str(apply_count),
            "pagination_model": "; ".join(sorted({clean_text(row.get("pagination_evidence")) for row in members})),
            "estimated_complexity": complexity,
            "estimated_companies_unlocked": str(actionable_count),
            "technical_priority": str(average_tech),
            "career_relevance_priority": str(average_career),
            "combined_priority": str(round(average_tech * 0.65 + average_career * 0.35)),
            "recommended_action": action,
            "supporting_evidence": " | ".join(evidence_samples),
        })
    clusters.sort(key=lambda row: (-int(row["technical_priority"]), -int(row["company_count"]), row["platform_family"], row["endpoint_variant"]))
    for index, row in enumerate(clusters, 1):
        row["cluster_rank"] = str(index)
    return clusters


def parent_candidates(records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for record in records:
        parent = clean_text(record.get("parent_company"))
        if not parent:
            continue
        key = compact_company_name(record.get("company"))
        rule = parent_rule_for(record.get("company"))
        parameters = clean_text(rule.get("parameters"))
        deterministic = not any(term in parameters for term in ("validate", "not yet evidenced", "free-text"))
        rows.append({
            "company": clean_text(record.get("company")),
            "parent_company": parent,
            "parent_source_candidate": clean_text(record.get("parent_source_candidate")),
            "official_careers_url": clean_text(record.get("current_official_careers_url")),
            "official_job_search_url": clean_text(record.get("current_job_search_url")),
            "filter_evidence": parameters,
            "filter_deterministic": str(deterministic).lower(),
            "duplicate_job_risk": "high; shared source must deduplicate by parent source and stable job ID",
            "keep_brand_visible": "manual_review" if not deterministic else "true",
            "mapping_safety": "safe" if deterministic else "manual_review",
            "confidence": clean_text(record.get("confidence")),
            "evidence": clean_text(record.get("evidence")),
            "recommended_action": clean_text(record.get("recommended_next_action")),
        })
    return sorted(rows, key=lambda row: compact_company_name(row["company"]))


def build_inventory(reconciled: list[Mapping[str, Any]], records: list[Mapping[str, Any]],
                    research_by_name: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    discovered = {compact_company_name(row.get("company")): row for row in records}
    rows: list[dict[str, str]] = []
    for source in reconciled:
        name = clean_text(source.get("Company"))
        key = compact_company_name(name)
        production = source.get("_production")
        record = discovered.get(key, {})
        research = research_by_name.get(key, {})
        covered = production is not None
        platform = clean_text(production.get("platform")) if production else ""
        rows.append({
            "original_rank": clean_text(source.get("Rank")),
            "company": name,
            "normalized_company": normalize_company_name(name),
            "original_priority": clean_text(source.get("Priority")),
            "original_careers_url": clean_text(source.get("Careers URL")),
            "original_notes": clean_text(source.get("Notes")),
            "current_official_careers_url": clean_text(record.get("current_official_careers_url") or research.get("Official Careers URL")),
            "current_job_search_url": clean_text(record.get("current_job_search_url") or research.get("Official Job Search URL")),
            "production_covered": str(covered).lower(),
            "production_name": clean_text(production.get("name")) if production else "",
            "production_platform": platform,
            "production_source_identifier": source_identifier(production) if production else "",
            "coverage_relationship": clean_text(source.get("_coverage_relationship")),
            "company_specific_collector": str(covered and platform in {"amazon", "atlassian", "google", "ibm_search", "meta", "shopify", "uber"}).lower(),
            "currently_uncovered": str(not covered).lower(),
            "current_platform_family": clean_text(record.get("platform_family") or platform),
            "endpoint_variant": clean_text(record.get("platform_variant")),
            "confidence": clean_text(record.get("confidence") or ("high" if covered else "")),
            "http_status": clean_text(record.get("http_status")),
            "final_url": clean_text(record.get("final_url")),
            "stable_job_id_evidence": clean_text(record.get("stable_job_id_evidence")),
            "official_apply_url_evidence": clean_text(record.get("official_apply_url_evidence")),
            "pagination_evidence": clean_text(record.get("pagination_evidence")),
            "anonymous_access": clean_text(record.get("anonymous_access")),
            "browser_required": clean_text(record.get("browser_automation_likely_required") or "false"),
            "authentication_required": clean_text(record.get("authentication_required") or "false"),
            "blocked": str(clean_text(record.get("current_classification")) == "blocked").lower(),
            "stale_fingerprint": clean_text(record.get("stale_fingerprint")),
            "parent_company": clean_text(record.get("parent_company")),
            "parent_source_candidate": clean_text(record.get("parent_source_candidate")),
            "duplicate_source_candidate": clean_text(record.get("duplicate_source_candidate")),
            "current_classification": clean_text(record.get("current_classification") or ("production_covered" if covered else "unknown")),
            "recommended_next_action": clean_text(record.get("recommended_next_action") or ("No research action; currently covered" if covered else "Investigate")),
            "technical_priority": clean_text(record.get("technical_priority") or ("0" if covered else "")),
            "career_relevance_priority": clean_text(record.get("career_relevance_priority") or str(career_score(source))),
            "combined_priority": clean_text(record.get("combined_priority") or "0"),
            "evidence": clean_text(record.get("evidence") or (f"Production registry entry: {production.get('name')}" if covered else "")),
            "investigation_timestamp": clean_text(record.get("investigation_timestamp")),
        })
    return sorted(rows, key=lambda row: (int(row["original_rank"] or 999999), compact_company_name(row["company"])))


def report_payload(mapping: MappingReport, records: list[Mapping[str, Any]],
                   clusters: list[Mapping[str, Any]], inventory: list[Mapping[str, Any]]) -> dict[str, Any]:
    families = Counter(clean_text(row.get("platform_family")) or "unknown" for row in records)
    classifications = Counter(clean_text(row.get("current_classification")) or "unknown" for row in records)
    technical_ceiling = sum(
        clean_text(row.get("current_classification")) not in {"browser_required", "blocked", "authentication_required", "inaccessible", "invalid_careers_url", "inactive_company", "unknown"}
        for row in records
    )
    browser_ceiling = technical_ceiling + sum(clean_text(row.get("current_classification")) == "browser_required" for row in records)
    effort_weights = {"low": 1.0, "low-medium": 1.5, "medium": 2.0, "medium-high": 3.0, "high": 5.0, "unknown": 6.0}

    def milestone_key(row: Mapping[str, Any]) -> tuple[float, int, int, str, str]:
        unlocked = int(clean_text(row.get("estimated_companies_unlocked")) or 0)
        weight = effort_weights.get(clean_text(row.get("estimated_complexity")), 6.0)
        return (
            -(unlocked / weight),
            -int(clean_text(row.get("technical_priority")) or 0),
            -unlocked,
            clean_text(row.get("platform_family")),
            clean_text(row.get("endpoint_variant")),
        )

    milestones = sorted(
        [row for row in clusters if int(clean_text(row.get("estimated_companies_unlocked")) or 0) > 0],
        key=milestone_key,
    )[:10]
    milestones = [dict(row) | {
        "coverage_to_effort_ratio": round(
            int(clean_text(row.get("estimated_companies_unlocked")) or 0)
            / effort_weights.get(clean_text(row.get("estimated_complexity")), 6.0), 2
        )
    } for row in milestones]
    return {
        "generated_at": max((clean_text(row.get("investigation_timestamp")) for row in records), default=""),
        "authoritative_sources": [
            str(DEFAULT_INPUT), str(DEFAULT_CONFIG), str(DEFAULT_RESEARCH), str(DEFAULT_PREVIOUS),
            "scripts/company_watchlist_common.py", "scripts/fingerprint_company_platforms.py",
            "src/scraper/company_careers/adapters/",
        ],
        "inventory": asdict(mapping),
        "companies_investigated": len(records),
        "platform_family_distribution": dict(sorted(families.items())),
        "classification_distribution": dict(sorted(classifications.items())),
        "realistic_additional_coverage_without_browser": technical_ceiling,
        "realistic_total_original_coverage_without_browser": mapping.covered_count + technical_ceiling,
        "realistic_additional_coverage_with_browser": browser_ceiling,
        "realistic_total_original_coverage_with_browser": mapping.covered_count + browser_ceiling,
        "top_milestones": milestones,
        "clusters": clusters,
        "records": records,
        "inventory_records": inventory,
    }


def markdown_report(payload: Mapping[str, Any]) -> str:
    inventory = payload["inventory"]
    lines = [
        "# Remaining-company discovery v2", "",
        f"Generated: {payload.get('generated_at') or 'partial run'}", "",
        "## Inventory reconciliation", "",
        f"- Original companies: {inventory['original_count']}",
        f"- Currently covered: {inventory['covered_count']}",
        f"- Currently uncovered: {inventory['uncovered_count']}",
        f"- Production-only companies: {len(inventory['production_only'])} ({'; '.join(inventory['production_only'])})",
        f"- Ambiguous mappings: {len(inventory['ambiguous'])}", "",
        "## Investigation", "",
        f"- Companies investigated: {payload['companies_investigated']}",
        f"- Additional coverage ceiling without browser automation: {payload['realistic_additional_coverage_without_browser']}",
        f"- Total original-list ceiling without browser automation: {payload['realistic_total_original_coverage_without_browser']}",
        f"- Additional coverage ceiling with browser automation: {payload['realistic_additional_coverage_with_browser']}",
        f"- Total original-list ceiling with browser automation: {payload['realistic_total_original_coverage_with_browser']}", "",
        "## Platform-family distribution", "",
        "| Family | Companies |", "|---|---:|",
    ]
    for family, count in payload["platform_family_distribution"].items():
        lines.append(f"| {family} | {count} |")
    lines.extend(["", "## Top future milestones", "", "| Rank | Family / variant | Companies unlocked | Coverage / effort | Technical priority | Action |", "|---:|---|---:|---:|---:|---|"])
    for index, cluster in enumerate(payload["top_milestones"], 1):
        lines.append(
            f"| {index} | {cluster['platform_family']} / {cluster['endpoint_variant']} | "
            f"{cluster['estimated_companies_unlocked']} | {cluster['coverage_to_effort_ratio']} | "
            f"{cluster['technical_priority']} | {cluster['recommended_action']} |"
        )
    lines.extend(["", "## Evidence policy", "", "All requests were anonymous, bounded, rate-limited HTTPS GETs. Unknown records retain tested URLs, status, examined evidence, and a manual next step. No browser automation, login, publishing, database, or production-registry writes were performed.", ""])
    return "\n".join(lines)


def persist_all(args: argparse.Namespace, mapping: MappingReport,
                reconciled: list[Mapping[str, Any]], research_by_name: Mapping[str, Mapping[str, Any]],
                records: list[Mapping[str, Any]]) -> None:
    clusters = build_clusters(records)
    inventory = build_inventory(reconciled, records, research_by_name)
    parents = parent_candidates(records)
    payload = report_payload(mapping, records, clusters, inventory)
    failures = [row for row in records if clean_text(row.get("current_classification")) in {
        "unknown", "browser_required", "blocked", "authentication_required",
        "inaccessible", "invalid_careers_url", "inactive_company",
    }]
    write_csv(args.output_csv, records, FINGERPRINT_FIELDS)
    write_csv(args.clusters_output, clusters, CLUSTER_FIELDS)
    write_csv(args.inventory_output, inventory, INVENTORY_FIELDS)
    write_csv(args.parent_output, parents, PARENT_FIELDS)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown_output.write_text(markdown_report(payload), encoding="utf-8")
    args.failures_output.write_text(json.dumps(failures, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def load_sources(args: argparse.Namespace) -> tuple[list[dict[str, Any]], MappingReport, list[dict[str, Any]], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    workbook = read_workbook(args.input)
    registry_data = yaml.safe_load(args.current_config.read_text(encoding="utf-8")) or {}
    registry = registry_data.get("target_companies") or registry_data.get("companies") or []
    reconciled, mapping = reconcile_companies(workbook, registry)
    research_rows = read_csv(args.research_file)
    research_by_name = {compact_company_name(row.get("Company")): row for row in research_rows}
    sources = []
    for row in reconciled:
        if row.get("_production") is not None:
            continue
        research = research_by_name.get(compact_company_name(row.get("Company")), {})
        sources.append({**dict(row), **research, "Company": clean_text(row.get("Company"))})
    previous = read_csv(args.previous_fingerprints)
    previous_by_name = {compact_company_name(row.get("Company")): row for row in previous}
    for key, research in research_by_name.items():
        if key in previous_by_name or not clean_text(research.get("Detected Platform")):
            continue
        previous_by_name[key] = {
            "Detected Platform Family": clean_text(research.get("Detected Platform")),
            "Confidence": "high" if clean_text(research.get("Reviewed")).casefold() in {"true", "1", "yes"} else "low",
        }
    return reconciled, mapping, sources, research_by_name, previous_by_name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--current-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--research-file", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--previous-fingerprints", type=Path, default=DEFAULT_PREVIOUS)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clusters-output", type=Path, default=DEFAULT_CLUSTERS)
    parser.add_argument("--inventory-output", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--parent-output", type=Path, default=DEFAULT_PARENTS)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--failures-output", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--request-delay", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-redirects", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    reconciled, mapping, sources, research_by_name, previous_by_name = load_sources(args)
    selected = sources[args.start:]
    if args.limit is not None:
        selected = selected[:args.limit]
    if args.dry_run:
        print(json.dumps({
            "dry_run": True, "original": mapping.original_count, "covered": mapping.covered_count,
            "uncovered": mapping.uncovered_count, "selected": [row["Company"] for row in selected],
            "production_only": mapping.production_only, "ambiguous": mapping.ambiguous,
        }, indent=2, ensure_ascii=False))
        return 0
    completed = refresh_record_metadata(read_csv(args.output_csv), previous_by_name) if args.resume else []
    client = BoundedResearchClient(
        request_delay=args.request_delay, timeout=args.timeout,
        max_redirects=args.max_redirects,
    )
    # Persist each internal batch so interruption loses at most one in-flight request.
    total = len(selected)
    records = completed
    for offset in range(0, total, args.batch_size):
        batch_limit = min(args.batch_size, total - offset)
        records = merge_batch(
            sources, records, previous_by_name, client,
            start=args.start + offset, limit=batch_limit, batch_size=args.batch_size,
            resume=args.resume,
        )
        persist_all(args, mapping, reconciled, research_by_name, records)
        print(f"Persisted batch {offset // args.batch_size + 1}: {batch_limit} selected; {len(records)}/{len(sources)} complete", flush=True)
    if total == 0:
        persist_all(args, mapping, reconciled, research_by_name, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
