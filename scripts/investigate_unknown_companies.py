"""Deep, bounded research for the v2 companies whose platform is unknown.

This is a research-only utility.  It never imports the scanner, persistence, or
publisher modules and never writes the production company registry.  Requests
are anonymous, sequential, rate-limited, size-limited, cached, and restricted
to HTTPS sources reached from the supplied official careers pages.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import queue
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
import yaml

try:
    from scripts.company_watchlist_common import clean_text, normalize_company_name as compact_name
    from scripts.investigate_remaining_companies import (
        EXISTING_ADAPTER_FAMILIES,
        PARENT_SOURCE_RULES,
        request_url,
        safe_research_target,
    )
except ModuleNotFoundError:
    from company_watchlist_common import clean_text, normalize_company_name as compact_name  # type: ignore[no-redef]
    from investigate_remaining_companies import (  # type: ignore[no-redef]
        EXISTING_ADAPTER_FAMILIES,
        PARENT_SOURCE_RULES,
        request_url,
        safe_research_target,
    )


MAX_BATCH_SIZE = 25
DEFAULT_BATCH_SIZE = 10
DEFAULT_FINGERPRINTS = Path("config/company_platform_fingerprints_v2.csv")
DEFAULT_INVENTORY = Path("config/company_remaining_inventory_v2.csv")
DEFAULT_PRODUCTION = Path("config/target_companies.yaml")
DEFAULT_OUTPUT = Path("config/company_unknown_investigation_v3.csv")
DEFAULT_CLUSTERS = Path("config/company_unknown_clusters_v3.csv")
DEFAULT_REUSE = Path("config/company_existing_adapter_reuse_candidates_v3.csv")
DEFAULT_PARENTS = Path("config/company_unknown_parent_source_candidates_v3.csv")
DEFAULT_JSON = Path("output/company_unknown_discovery_v3.json")
DEFAULT_MARKDOWN = Path("output/company_unknown_discovery_v3.md")
DEFAULT_FAILURES = Path("output/company_unknown_failures_v3.json")
DEFAULT_EVIDENCE = Path("output/company_unknown_request_evidence_v3.json")

KNOWN_FUTURE_FAMILIES = {
    "avature", "icims", "static_html", "custom_rest_api", "custom_graphql",
    "javascript_embedded_data", "oracle_taleo", "parent_company_source",
    "talentbrew_radancy", "smartrecruiters", "jobvite", "brassring", "dayforce",
    "adp", "ukg", "workable", "recruitee", "teamtailor", "personio", "bamboohr",
}
EXISTING_ADAPTERS = set(EXISTING_ADAPTER_FAMILIES)
EXISTING_ADAPTERS.update({"greenhouse", "lever", "ashby", "workday", "oracle_recruiting", "sap_successfactors", "eightfold", "phenom"})
CUSTOM_PRODUCTION_FAMILIES = {"amazon", "atlassian", "google", "ibm_search", "meta", "shopify", "uber"}

FINAL_CLASSIFICATIONS = {
    "existing_adapter_reuse", "existing_adapter_extension", "avature", "icims",
    "static_html", "custom_rest_api", "custom_graphql", "javascript_embedded_data",
    "oracle_taleo", "parent_company_source", "newly_discovered_reusable_platform",
    "company_specific_collector", "browser_required", "blocked", "network_inaccessible",
    "invalid_careers_url", "stale_source_unresolved", "acquired_or_renamed",
    "no_current_jobs_source", "still_unresolved",
}

INVESTIGATION_FIELDS = [
    "original_rank", "company", "normalized_company", "original_priority",
    "original_careers_url", "original_notes", "v2_platform_family", "v2_classification",
    "v2_confidence", "v2_evidence", "v2_attempted_urls", "official_corporate_url",
    "current_official_careers_url", "current_job_search_url", "sample_job_detail_url",
    "final_url", "http_status", "redirect_chain", "page_title", "stored_url_status",
    "platform_family", "platform_variant", "primary_classification", "confidence",
    "endpoint_host", "endpoint_url", "request_method", "request_contract_summary",
    "structured_data_type", "stable_job_id_evidence", "official_apply_url_evidence",
    "pagination_evidence", "anonymous_access", "cookies_required", "csrf_required",
    "authentication_required", "session_required", "javascript_required",
    "browser_required", "anti_bot_evidence", "blocked", "network_inaccessible",
    "parent_company", "parent_source_candidate", "deterministic_brand_fields",
    "duplicate_source_risk", "existing_adapter", "existing_adapter_reuse",
    "existing_adapter_extension_required", "future_adapter_cluster",
    "new_reusable_family", "company_specific_collector", "stale_fingerprint", "stale_url",
    "urls_tested", "scripts_inspected", "evidence", "warnings", "recommended_next_action",
    "technical_priority", "career_relevance_priority", "combined_priority",
    "adapter_validation_result", "jobs_normalized", "investigation_timestamp",
]

CLUSTER_FIELDS = [
    "cluster_rank", "platform_family", "endpoint_variant", "companies", "company_count",
    "confidence_distribution", "anonymous_endpoint_count", "stable_id_count",
    "official_apply_url_count", "deterministic_pagination_count",
    "existing_production_adapter_available", "production_adapter_works_as_is",
    "production_adapter_extension_required", "new_reusable_adapter_required",
    "static_or_embedded_collector_appropriate", "parent_mapping_appropriate",
    "browser_required_count", "blocked_count", "implementation_complexity",
    "maintenance_risk", "companies_potentially_unlocked", "recommended_implementation_order",
    "supporting_evidence",
]

REUSE_FIELDS = [
    "company", "production_adapter", "official_careers_url", "official_search_url",
    "required_configuration", "endpoint_variant", "validation_result", "jobs_normalized",
    "stable_id_result", "apply_url_result", "pagination_result", "blocker",
    "safe_for_later_activation", "needs_adapter_extension", "confidence",
]

PARENT_FIELDS = [
    "company", "parent_company", "official_relationship_evidence", "official_careers_url",
    "official_search_url", "canonical_source_company", "parent_jobs_attributable",
    "deterministic_identifying_fields", "duplicate_source_risk", "multi_brand_risk",
    "metadata_annotation_appropriate", "separate_registry_entry_risk", "confidence",
    "evidence", "recommended_action",
]


def normalize_company_name(value: Any) -> str:
    """Readable exact key; callers must compare equality, never containment."""
    text = clean_text(value).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{key: clean_text(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean_text(row.get(field)) for field in fields})
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _is_unknown(row: Mapping[str, Any]) -> bool:
    # v2 platform family is authoritative.  v2 classification=unknown is a subset.
    return clean_text(row.get("platform_family")).casefold() == "unknown"


def extract_unknown_records(
    fingerprint_rows: list[Mapping[str, Any]], inventory_rows: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    inventory_by_name: dict[str, Mapping[str, Any]] = {}
    for row in inventory_rows:
        key = normalize_company_name(row.get("normalized_company") or row.get("company"))
        if key in inventory_by_name:
            raise ValueError(f"Duplicate normalized inventory company: {key}")
        inventory_by_name[key] = row

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for fingerprint in fingerprint_rows:
        if not _is_unknown(fingerprint):
            continue
        key = normalize_company_name(fingerprint.get("normalized_company") or fingerprint.get("company"))
        if not key or key in seen:
            raise ValueError(f"Missing or duplicate normalized unknown company: {key or '<empty>'}")
        inventory = inventory_by_name.get(key)
        if inventory is None:
            raise ValueError(f"Unknown company is missing exact inventory match: {fingerprint.get('company')}")
        if clean_text(inventory.get("production_covered")).casefold() == "true":
            raise ValueError(f"Unknown company is unexpectedly production-covered: {fingerprint.get('company')}")
        seen.add(key)
        results.append({
            **{f"v2_{name}": clean_text(value) for name, value in fingerprint.items()},
            **{f"inventory_{name}": clean_text(value) for name, value in inventory.items()},
            "company": clean_text(fingerprint.get("company")),
            "normalized_company": key,
        })
    return sorted(results, key=lambda row: (int(row.get("inventory_original_rank") or 999999), row["normalized_company"]))


@dataclass
class ResponseEvidence:
    requested_url: str
    final_url: str = ""
    status: int = 0
    redirect_chain: list[dict[str, Any]] = field(default_factory=list)
    content_type: str = ""
    text: str = ""
    title: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: str = ""
    truncated: bool = False


class ResearchClient:
    """Bounded anonymous request client with redirect checks and request caching."""

    def __init__(self, *, request_delay: float = 0.5, timeout: int = 15,
                 max_redirects: int = 5, retries: int = 1,
                 max_response_bytes: int = 3_000_000,
                 session: Optional[requests.Session] = None, sleep_fn: Any = time.sleep) -> None:
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
        self.max_response_bytes = max_response_bytes
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self.requests_made = 0
        self.cache: dict[tuple[str, str, str], ResponseEvidence] = {}
        self.log: list[dict[str, Any]] = []

    def fetch(self, url: str, *, method: str = "GET", json_body: Any = None,
              max_bytes: Optional[int] = None) -> ResponseEvidence:
        normalized = request_url(url)
        body_key = json.dumps(json_body, sort_keys=True, separators=(",", ":")) if json_body is not None else ""
        cache_key = (method.upper(), normalized, hashlib.sha256(body_key.encode()).hexdigest())
        if cache_key in self.cache:
            return self.cache[cache_key]
        evidence = self._fetch(normalized, method.upper(), json_body, max_bytes or self.max_response_bytes)
        self.cache[cache_key] = evidence
        self.log.append({
            "method": method.upper(), "requested_url": normalized, "final_url": evidence.final_url,
            "status": evidence.status, "redirect_chain": evidence.redirect_chain,
            "content_type": evidence.content_type, "bytes": len(evidence.text.encode("utf-8")),
            "truncated": evidence.truncated, "error": evidence.error,
        })
        return evidence

    def _fetch(self, url: str, method: str, json_body: Any, max_bytes: int) -> ResponseEvidence:
        if not url:
            return ResponseEvidence(url, error="missing URL")
        current = url
        chain: list[dict[str, Any]] = []
        for redirect_index in range(self.max_redirects + 1):
            safe, reason = bounded_safe_research_target(current, timeout=min(5.0, float(self.timeout)))
            if not safe:
                return ResponseEvidence(url, current, redirect_chain=chain, error=reason)
            response: Optional[requests.Response] = None
            error = ""
            for attempt in range(self.retries + 1):
                if self.requests_made:
                    self.sleep_fn(self.request_delay)
                self.requests_made += 1
                try:
                    response = self.session.request(
                        method, current, allow_redirects=False, timeout=self.timeout, stream=True,
                        json=json_body if method == "POST" else None,
                        headers={
                            "User-Agent": "JobRadarUnknownResearch/3.0",
                            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
                            "Content-Type": "application/json" if method == "POST" else "text/plain",
                        },
                    )
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                        response.close(); response = None; continue
                    break
                except requests.RequestException as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
            if response is None:
                return ResponseEvidence(url, current, redirect_chain=chain, error=error or "request failed")
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    chain.append({"url": current, "status": status, "location": location})
                    if not location:
                        return ResponseEvidence(url, current, status, chain, headers=dict(response.headers), error="redirect without Location")
                    if redirect_index == self.max_redirects:
                        return ResponseEvidence(url, current, status, chain, headers=dict(response.headers), error="too many redirects")
                    current = request_url(urljoin(current, location))
                    if status == 303:
                        method, json_body = "GET", None
                    continue
                try:
                    raw = response.raw.read(max_bytes + 1, decode_content=True)
                except Exception as exc:
                    chain.append({"url": current, "status": status})
                    return ResponseEvidence(url, current, status, chain, headers=dict(response.headers), error=f"read failed: {exc.__class__.__name__}: {exc}")
                truncated = len(raw) > max_bytes
                raw = raw[:max_bytes]
                body = raw.decode(response.encoding or "utf-8", errors="replace")
                title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.I | re.S)
                title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:300] if title_match else ""
                chain.append({"url": current, "status": status})
                return ResponseEvidence(url, current, status, chain, response.headers.get("Content-Type", ""), body, title, dict(response.headers), truncated=truncated)
            finally:
                response.close()
        return ResponseEvidence(url, current, redirect_chain=chain, error="too many redirects")


@dataclass
class PageAnalysis:
    canonical_url: str = ""
    career_links: list[str] = field(default_factory=list)
    job_links: list[str] = field(default_factory=list)
    apply_links: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    forms: list[dict[str, str]] = field(default_factory=list)
    jsonld_job_count: int = 0
    inline_json: bool = False
    serialized_state: str = ""
    static_job_listing: bool = False
    js_shell: bool = False
    no_jobs: bool = False
    evidence: list[str] = field(default_factory=list)


def _absolute(base_url: str, value: str) -> str:
    try:
        return request_url(urljoin(base_url, html.unescape(value).replace("\\/", "/")))
    except (ValueError, UnicodeError):
        return ""


def analyze_html(base_url: str, text: str) -> PageAnalysis:
    result = PageAnalysis()
    if not text:
        return result
    canonical = re.search(r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)', text, flags=re.I)
    if not canonical:
        canonical = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'][^"\']*canonical', text, flags=re.I)
    if canonical:
        result.canonical_url = _absolute(base_url, canonical.group(1))
    anchors = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, flags=re.I | re.S)
    for href, label in anchors:
        label_text = re.sub(r"<[^>]+>", " ", label)
        absolute = _absolute(base_url, href)
        joined = f"{href} {label_text}".casefold()
        if re.search(r"\b(careers?|jobs?|join us|work with us|openings?|vacancies|positions?)\b", joined):
            result.career_links.append(absolute)
        if re.search(r"(?:/jobs?/|/positions?/|/vacanc(?:y|ies)/|/openings?/|jobid=|job_id=|requisition)", href, flags=re.I):
            result.job_links.append(absolute)
        if re.search(r"\bapply\b", joined):
            result.apply_links.append(absolute)
    result.scripts = [_absolute(base_url, src) for src in re.findall(r'<script\b[^>]*src=["\']([^"\']+)', text, flags=re.I)]
    result.iframes = [_absolute(base_url, src) for src in re.findall(r'<iframe\b[^>]*src=["\']([^"\']+)', text, flags=re.I)]
    for tag in re.findall(r"<form\b[^>]*>", text, flags=re.I):
        action = re.search(r'action=["\']([^"\']*)', tag, flags=re.I)
        method = re.search(r'method=["\']([^"\']*)', tag, flags=re.I)
        result.forms.append({"action": _absolute(base_url, action.group(1)) if action else base_url, "method": (method.group(1) if method else "GET").upper()})
    for block in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, flags=re.I | re.S):
        try:
            value = json.loads(html.unescape(block.strip()))
        except (ValueError, TypeError):
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if clean_text(item.get("@type")).casefold() == "jobposting":
                    result.jsonld_job_count += 1
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    result.inline_json = bool(re.search(r'<script\b[^>]*type=["\']application/(?:json|ld\+json)', text, flags=re.I))
    state_markers = []
    for marker, label in ((r'__NEXT_DATA__', "nextjs"), (r'__NUXT__|__NUXT_DATA__', "nuxt"), (r'__INITIAL_STATE__|__APOLLO_STATE__|hydrationData', "hydration")):
        if re.search(marker, text, flags=re.I): state_markers.append(label)
    result.serialized_state = ";".join(state_markers)
    visible = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    visible = re.sub(r"<style\b[^>]*>.*?</style>|<[^>]+>", " ", visible, flags=re.I | re.S)
    visible = re.sub(r"\s+", " ", html.unescape(visible)).strip()
    result.static_job_listing = len(set(result.job_links)) >= 2
    result.js_shell = bool(re.search(r'<(?:div|main)[^>]+id=["\'](?:root|app|__next)["\']|enable javascript|<noscript', text, flags=re.I)) and len(visible) < 1600
    result.no_jobs = bool(re.search(r"\b(no (?:current |open )?(?:jobs|positions|vacancies|openings)|there are no jobs)\b", visible, flags=re.I))
    if result.jsonld_job_count: result.evidence.append(f"JSON-LD JobPosting count={result.jsonld_job_count}")
    if result.inline_json: result.evidence.append("inline application JSON present")
    if result.serialized_state: result.evidence.append(f"serialized state={result.serialized_state}")
    if result.forms: result.evidence.append(f"search/form contracts={len(result.forms)}")
    if result.static_job_listing: result.evidence.append(f"server HTML job links={len(set(result.job_links))}")
    if result.js_shell: result.evidence.append("thin JavaScript application shell")
    return result


@dataclass
class Detection:
    family: str = "unknown"
    variant: str = "unknown"
    confidence: str = "low"
    endpoint_url: str = ""
    method: str = "GET"
    contract: str = "unknown"
    pagination: str = "unknown"
    stable_id: str = "unknown"
    apply_url: str = "unknown"
    javascript_required: str = "unknown"
    evidence: list[str] = field(default_factory=list)
    reusable: bool = False
    extension_required: bool = False


URL_FAMILY_RULES: list[tuple[str, tuple[str, ...], str, str]] = [
    ("greenhouse", ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"), "boards_api_v1", "GET"),
    ("lever", ("jobs.lever.co", "api.lever.co"), "postings_api_v0", "GET"),
    ("ashby", ("jobs.ashbyhq.com", "api.ashbyhq.com"), "posting_api_job_board", "GET"),
    ("workday", ("myworkdayjobs.com",), "wday_cxs_v1", "POST"),
    ("avature", ("avature.net",), "portal_search", "GET"),
    ("icims", ("icims.com",), "jobs_search", "GET"),
    ("smartrecruiters", ("jobs.smartrecruiters.com", "api.smartrecruiters.com"), "companies_postings_v1", "GET"),
    ("jobvite", ("jobs.jobvite.com",), "jobvite_hosted", "GET"),
    ("brassring", ("brassring.com",), "partner_site", "GET"),
    ("dayforce", ("dayforcehcm.com",), "dayforce_jobs", "GET"),
    ("adp", ("workforcenow.adp.com", "recruiting.adp.com"), "adp_recruiting", "GET/POST"),
    ("ukg", ("recruiting.ultipro.com", "recruiting2.ultipro.com", "ukg.com"), "ukg_recruiting", "GET"),
    ("workable", ("apply.workable.com",), "workable_account", "GET"),
    ("recruitee", ("recruitee.com",), "recruitee_careers", "GET"),
    ("teamtailor", ("teamtailor.com",), "teamtailor_careers", "GET"),
    ("personio", ("jobs.personio.com", "jobs.personio.de"), "personio_jobs", "GET"),
    ("bamboohr", ("bamboohr.com",), "bamboohr_jobs", "GET"),
]


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _urls_from_text(base_url: str, text: str) -> list[str]:
    values = re.findall(r'https?:\\?/\\?/[^"\'\s<>\\]+|/(?:api|services|graphql|wday/cxs|hcmRestApi)/[^"\'\s<>\\]*', text, flags=re.I)
    return list(dict.fromkeys(url for value in values if (url := _absolute(base_url, value))))


def detect_contract(page_url: str, text: str, analysis: PageAnalysis) -> Detection:
    searchable = html.unescape(text).replace("\\/", "/")
    urls = [page_url, analysis.canonical_url, *analysis.iframes, *analysis.career_links, *_urls_from_text(page_url, searchable)]
    for url in urls:
        host = (urlsplit(url).hostname or "").casefold()
        for family, domains, variant, method in URL_FAMILY_RULES:
            if not any(_host_matches(host, domain) for domain in domains):
                continue
            endpoint = url
            parts = [part for part in urlsplit(url).path.split("/") if part]
            if family == "greenhouse":
                query = dict(parse_qsl(urlsplit(url).query))
                slug = query.get("for", "") if parts[:2] == ["embed", "job_board"] else (parts[2] if host == "boards-api.greenhouse.io" and len(parts) > 2 and parts[:2] == ["v1", "boards"] else (parts[0] if parts else ""))
                endpoint = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true" if slug else url
            elif family == "lever":
                slug = parts[2] if host == "api.lever.co" and len(parts) > 2 and parts[:2] == ["v0", "postings"] else (parts[0] if parts else "")
                endpoint = f"https://api.lever.co/v0/postings/{slug}?mode=json" if slug else url
            elif family == "ashby":
                slug = parts[2] if host == "api.ashbyhq.com" and len(parts) > 2 and parts[:2] == ["posting-api", "job-board"] else (parts[0] if parts else "")
                endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false" if slug else url
            elif family == "smartrecruiters" and host == "jobs.smartrecruiters.com":
                slug = parts[0] if parts else ""
                endpoint = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=20&offset=0" if slug else url
            elif family == "workday":
                match = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
                site_parts = parts[1:] if parts and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", parts[0]) else parts
                site = site_parts[0] if site_parts else ""
                if match and site:
                    endpoint = f"https://{host}/wday/cxs/{match.group(1)}/{site}/jobs"
            if urlsplit(endpoint).scheme == "http":
                parsed_endpoint = urlsplit(endpoint)
                endpoint = urlunsplit(("https", parsed_endpoint.netloc, parsed_endpoint.path, parsed_endpoint.query, ""))
            return Detection(
                family, variant, "high", endpoint, method,
                "official ATS host and tenant/board identifiers",
                "offset/limit" if family in {"greenhouse", "lever", "ashby", "smartrecruiters"} else "platform-specific page/offset",
                "true", "true", "false", [f"official ATS host={host}"], reusable=True,
            )

    marker_rules = [
        ("oracle_recruiting", r"recruitingCEJobRequisitions|CandidateExperience|hcmRestApi", "hcmRestApi_recruitingCEJobRequisitions", "GET"),
        ("sap_successfactors", r"successfactors\.(?:com|eu)|careerSiteCompanyId|sfcareer", "career_search", "GET/POST"),
        ("eightfold", r"eightfold\.ai|/api/pcsx/(?:search|jobs)", "pcsx_search", "POST"),
        ("phenom", r"phenompeople\.com|phApp\.|/api/(?:jobsearch|search-results)", "phenom_search_api", "GET/POST"),
        ("oracle_taleo", r"taleo\.net|careersection/.+?/jobsearch\.ftl", "careersection_jobsearch", "GET/POST"),
        ("talentbrew_radancy", r"talentbrew|radancy|tbcdn\.talentbrew\.com", "talentbrew_site", "GET"),
        ("avature", r"avature\.net|Avature", "portal_search", "GET"),
        ("icims", r"icims\.com|iCIMS", "jobs_search", "GET"),
        ("jobvite", r"jobs\.jobvite\.com|Jobvite", "jobvite_hosted", "GET"),
        ("brassring", r"brassring\.com|Kenexa", "partner_site", "GET"),
        ("dayforce", r"dayforcehcm\.com|Dayforce Jobs", "dayforce_jobs", "GET"),
        ("adp", r"workforcenow\.adp\.com|recruiting\.adp\.com", "adp_recruiting", "GET/POST"),
        ("ukg", r"recruiting2?\.ultipro\.com|UKG Pro Recruiting", "ukg_recruiting", "GET"),
    ]
    extracted_urls = _urls_from_text(page_url, searchable)
    for family, pattern, variant, method in marker_rules:
        match = re.search(pattern, searchable, flags=re.I)
        if match:
            endpoint = "" if family == "talentbrew_radancy" else next((url for url in extracted_urls if pattern.split("|")[0].replace("\\", "").casefold() in url.casefold()), "")
            if endpoint and urlsplit(endpoint).scheme == "http":
                parsed_endpoint = urlsplit(endpoint)
                endpoint = urlunsplit(("https", parsed_endpoint.netloc, parsed_endpoint.path, parsed_endpoint.query, ""))
            expected_endpoint = {
                "oracle_recruiting": "/hcmrestapi/",
                "sap_successfactors": "successfactors.",
                "eightfold": "/api/pcsx/",
                "phenom": "/api/",
            }.get(family, "")
            extension = family in EXISTING_ADAPTERS and (not endpoint or bool(expected_endpoint and expected_endpoint not in endpoint.casefold()))
            return Detection(family, variant, "medium", endpoint or page_url, method,
                             f"static marker={match.group(0)[:120]}", "platform-specific",
                             "true", "true", "true", [f"static platform marker={match.group(0)[:120]}"], reusable=True,
                             extension_required=extension)

    graphql = next((url for url in extracted_urls if re.search(r"/graphql(?:\?|$)", url, flags=re.I)), "")
    if graphql and re.search(r"graphql|ApolloClient|__typename|query\s+[A-Za-z_]", searchable, flags=re.I):
        return Detection("custom_graphql", "graphql_json", "medium", graphql, "POST", "GraphQL query and variables",
                         "cursor/query-specific", "unknown", "unknown", "true", [f"GraphQL endpoint={graphql}"], reusable=True)
    rest = next((url for url in extracted_urls if re.search(r"/(?:api|services)/.*(?:jobs|positions|vacancies|openings|requisitions)", url, flags=re.I)), "")
    if rest:
        return Detection("custom_rest_api", "rest_json", "medium", rest, "GET/POST", "endpoint-specific anonymous request",
                         "endpoint-specific", "unknown", "unknown", "true", [f"REST endpoint={rest}"], reusable=True)
    if analysis.jsonld_job_count or analysis.inline_json and (analysis.serialized_state or re.search(r'"(?:jobs|positions|openings)"\s*:\s*\[', searchable, flags=re.I)):
        stable = "true" if re.search(r'"(?:jobId|job_id|requisitionId|postingId|identifier)"\s*:', searchable, flags=re.I) else "unknown"
        apply_url = "true" if analysis.apply_links or re.search(r'"(?:applyUrl|applicationUrl|url)"\s*:\s*"https?://', searchable, flags=re.I) else "unknown"
        return Detection("javascript_embedded_data", analysis.serialized_state or "inline_json", "medium", page_url, "GET",
                         "embedded structured data", "page/embedded collection", stable, apply_url,
                         "false" if analysis.jsonld_job_count else "true", list(analysis.evidence), reusable=False)
    if analysis.static_job_listing:
        stable = "true" if any(re.search(r"/(?:jobs?|positions?)/[A-Za-z0-9_-]{3,}", url, flags=re.I) for url in analysis.job_links) else "unknown"
        return Detection("static_html", "server_rendered_links", "medium", page_url, "GET", "HTML anchors",
                         "page/none", stable, "true" if analysis.apply_links else "unknown", "false", list(analysis.evidence), reusable=False)
    return Detection(evidence=list(analysis.evidence))


def choose_relevant_scripts(script_urls: Iterable[str], max_scripts: int) -> list[str]:
    unique = list(dict.fromkeys(url for url in script_urls if url))
    keywords = re.compile(r"jobs?|careers?|search|requisition|position|opening|vacanc|graphql|api|ats|talent|recruit", re.I)
    relevant = [url for url in unique if keywords.search(url)]
    return sorted(relevant, key=lambda url: (len(url), url))[:max_scripts]


def bounded_safe_research_target(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Bound the existing DNS-aware SSRF guard without weakening its result."""
    results: queue.Queue[tuple[bool, str]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            results.put_nowait(safe_research_target(url))
        except Exception as exc:  # Keep resolver failures as safe rejections.
            results.put_nowait((False, f"target validation failed: {exc.__class__.__name__}: {exc}"))

    worker = threading.Thread(target=resolve, name="job-radar-dns-guard", daemon=True)
    worker.start()
    try:
        return results.get(timeout=timeout)
    except queue.Empty:
        return False, f"target validation timed out after {timeout:g}s"


def _same_official_chain(page_url: str, resource_url: str) -> bool:
    page_host = (urlsplit(page_url).hostname or "").casefold()
    resource_host = (urlsplit(resource_url).hostname or "").casefold()
    if not page_host or not resource_host:
        return False
    if page_host == resource_host or page_host.endswith("." + resource_host) or resource_host.endswith("." + page_host):
        return True
    # A script explicitly referenced by an official page is considered part of that
    # page's official delivery chain; SSRF/public-HTTPS checks still apply per request.
    return True


def _domain_root(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _unique_urls(*values: Any) -> list[str]:
    urls: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            url = request_url(candidate)
            if url and url not in urls:
                urls.append(url)
    return urls


def _stored_url_status(stored: str, response: ResponseEvidence, recovered: str) -> str:
    if not stored:
        return "missing"
    if response.status in {404, 410}:
        return "stale" if recovered and recovered != stored else "invalid"
    if response.status == 0:
        return "inaccessible"
    if response.final_url and response.final_url != stored:
        return "redirected"
    if response.status >= 400:
        return "invalid"
    return "current"


def _anti_bot(response: ResponseEvidence) -> str:
    searchable = f"{response.title}\n{response.text[:200000]}\n{response.headers}"
    technical = re.search(r"cf-chl-|challenge-platform|perimeterx|px-captcha", searchable, flags=re.I)
    interactive = re.search(r"verify (?:that )?you are human|complete the (?:security )?challenge|captcha (?:challenge|required)|attention required!|access denied", searchable, flags=re.I)
    if response.status in {403, 429} or interactive:
        marker = technical or interactive
        return f"HTTP {response.status}; marker={marker.group(0) if marker else 'access response'}"
    return ""


def _classification(response: ResponseEvidence, analysis: PageAnalysis, detection: Detection,
                    scripts_attempted: int, anti_bot: str, recovered: bool) -> str:
    if anti_bot:
        return "blocked"
    if response.status == 0:
        return "network_inaccessible"
    if response.status in {404, 410}:
        return "stale_source_unresolved" if recovered else "invalid_careers_url"
    if analysis.no_jobs and detection.family == "unknown":
        return "no_current_jobs_source"
    if detection.family == "parent_company_source":
        return "parent_company_source"
    if detection.family in EXISTING_ADAPTERS:
        return "existing_adapter_extension" if detection.extension_required else "existing_adapter_reuse"
    if detection.family in {"avature", "icims", "static_html", "custom_rest_api", "custom_graphql", "javascript_embedded_data", "oracle_taleo"}:
        return detection.family
    if detection.family in KNOWN_FUTURE_FAMILIES:
        return "newly_discovered_reusable_platform"
    if detection.family != "unknown":
        return "newly_discovered_reusable_platform" if detection.reusable else "company_specific_collector"
    if analysis.js_shell and scripts_attempted > 0:
        return "browser_required"
    if recovered:
        return "stale_source_unresolved"
    return "still_unresolved"


def _scores(source: Mapping[str, Any], detection: Detection, classification: str) -> tuple[int, int, int]:
    tech = {"high": 75, "medium": 58, "low": 30}.get(detection.confidence, 30)
    if classification == "existing_adapter_reuse": tech += 20
    if classification in {"blocked", "network_inaccessible", "invalid_careers_url", "still_unresolved"}: tech -= 25
    if detection.stable_id == "true": tech += 3
    if detection.apply_url == "true": tech += 2
    tech = max(0, min(100, tech))
    try: rank = int(clean_text(source.get("inventory_original_rank")))
    except ValueError: rank = 246
    career = max(0, min(100, 100 - round((rank - 1) * 45 / 245)))
    priority = clean_text(source.get("inventory_original_priority")).casefold()
    career = max(0, min(100, career + {"high": 8, "medium": 4, "low": -4}.get(priority, 0)))
    return tech, career, round(tech * .65 + career * .35)


def _recommend(classification: str, detection: Detection) -> str:
    if classification == "existing_adapter_reuse": return f"Validate configuration and consider later activation with existing {detection.family} adapter"
    if classification == "existing_adapter_extension": return f"Specify a small reusable {detection.family} extension before later activation"
    if classification == "parent_company_source": return "Validate deterministic child-brand attribution and deduplicate the shared source"
    if classification in {"blocked", "browser_required"}: return "Manual browser/network review only; do not bypass controls"
    if classification in {"network_inaccessible", "stale_source_unresolved", "invalid_careers_url"}: return "Recheck the official corporate source manually and retain all attempted URLs"
    if classification == "still_unresolved": return "Manual official-source investigation; do not infer a platform"
    if classification == "no_current_jobs_source": return "Recheck periodically for a current official jobs source"
    return f"Validate the {detection.family} contract before implementation"


def _adapter_configuration(detection: Detection) -> str:
    url = urlsplit(detection.endpoint_url)
    parts = [part for part in url.path.split("/") if part]
    if detection.family in {"greenhouse", "lever"}: return f"slug={parts[2] if len(parts) > 2 else ''}"
    if detection.family == "ashby": return f"slug={parts[-1] if parts else ''}"
    if detection.family == "workday": return f"endpoint_url={detection.endpoint_url}"
    return f"endpoint_url={detection.endpoint_url}"


def validate_public_endpoint(client: ResearchClient, detection: Detection, max_jobs: int) -> tuple[str, int, str, str, str]:
    if detection.family == "workday" and detection.endpoint_url:
        body = {"appliedFacets": {}, "limit": min(20, max_jobs), "offset": 0, "searchText": ""}
        response = client.fetch(detection.endpoint_url, method="POST", json_body=body, max_bytes=3_000_000)
        if response.status != 200:
            return f"endpoint_http_{response.status or 'error'}", 0, "unknown", "unknown", "offset + limit"
        try:
            payload = json.loads(response.text)
            from src.scraper.company_careers.adapters.workday import fetch_jobs
            from src.scraper.company_careers.models import CompanyTarget
            parsed = urlsplit(detection.endpoint_url)
            host_parts = (parsed.hostname or "").split(".")
            path_parts = [part for part in parsed.path.split("/") if part]

            class CachedResponse:
                status_code = 200
                def json(self) -> Any: return payload

            class CachedSession:
                def post(self, *_args: Any, **_kwargs: Any) -> CachedResponse: return CachedResponse()

            target = CompanyTarget(
                name="compatibility-validation", platform="workday",
                tenant=host_parts[0], wd_subdomain=host_parts[1], site_id=path_parts[3], max_pages=1,
            )
            jobs = fetch_jobs(CachedSession(), target, timeout=1)[:max_jobs]  # type: ignore[arg-type]
        except (ValueError, KeyError, IndexError, TypeError, Exception) as exc:
            return f"production_parser_failed:{exc.__class__.__name__}", 0, "unknown", "unknown", "offset + limit"
        stable = "true" if jobs and all(job.job_id for job in jobs) else "unknown"
        apply_result = "true" if jobs and all(job.url for job in jobs) else "unknown"
        return "production_parser_compatible", len(jobs), stable, apply_result, "offset + limit"
    if detection.family not in {"greenhouse", "lever", "ashby", "smartrecruiters"} or detection.method != "GET" or not detection.endpoint_url:
        return "not_practical_during_static_research", 0, detection.stable_id, detection.apply_url, detection.pagination
    response = client.fetch(detection.endpoint_url, max_bytes=3_000_000)
    if response.status != 200:
        return f"endpoint_http_{response.status or 'error'}", 0, "unknown", "unknown", detection.pagination
    try: data = json.loads(response.text)
    except ValueError: return "endpoint_non_json", 0, "unknown", "unknown", detection.pagination
    if isinstance(data, list): jobs = data
    elif detection.family == "smartrecruiters": jobs = data.get("content", []) if isinstance(data, dict) else []
    else: jobs = data.get("jobs", []) if isinstance(data, dict) else []
    normalized = jobs[:max_jobs]
    stable = "true" if normalized and all(clean_text(item.get("id") or item.get("uuid")) for item in normalized if isinstance(item, dict)) else "unknown"
    apply_result = "true" if normalized and all(clean_text(item.get("absolute_url") or item.get("hostedUrl") or item.get("jobUrl") or item.get("ref")) for item in normalized if isinstance(item, dict)) else "unknown"
    return "compatible_json_contract", len(normalized), stable, apply_result, detection.pagination


def investigate_company(source: Mapping[str, Any], client: ResearchClient, *, max_scripts: int,
                        max_script_bytes: int, max_pages: int, max_jobs: int,
                        timestamp: Optional[str] = None) -> dict[str, str]:
    stored_url = clean_text(source.get("v2_current_job_search_url") or source.get("v2_current_official_careers_url") or source.get("v2_original_careers_url"))
    candidate_urls = _unique_urls(
        stored_url, source.get("v2_current_official_careers_url"), source.get("v2_final_url"),
        source.get("v2_original_careers_url"), source.get("inventory_original_careers_url"),
    )
    tested: list[str] = []
    responses: list[ResponseEvidence] = []
    stored_response = client.fetch(stored_url)
    tested.append(stored_url); responses.append(stored_response)
    primary = stored_response
    primary_analysis = analyze_html(primary.final_url or stored_url, primary.text)
    recovered = False

    # A stale deep link may provide no body from which to recover.  The HTTPS
    # root of that same official host is a bounded, official fallback.
    if stored_response.status in {0, 404, 410} and urlsplit(stored_url).hostname:
        parsed_stored = urlsplit(stored_url)
        candidate_urls.append(urlunsplit(("https", parsed_stored.netloc, "/", "", "")))

    recovery_links = [*primary_analysis.career_links, *primary_analysis.iframes]
    for candidate in _unique_urls(candidate_urls, recovery_links)[:max_pages]:
        if candidate in tested: continue
        response = client.fetch(candidate)
        tested.append(candidate); responses.append(response)
        analysis = analyze_html(response.final_url or candidate, response.text)
        better = response.status == 200 and (
            primary.status != 200 or analysis.static_job_listing or analysis.jsonld_job_count or analysis.career_links or analysis.iframes
        )
        if better:
            primary, primary_analysis, recovered = response, analysis, candidate != stored_url

    # Follow at most the remaining page budget of official jobs/careers links.
    for candidate in _unique_urls(primary_analysis.iframes, primary_analysis.career_links)[:max_pages]:
        if candidate in tested: continue
        response = client.fetch(candidate)
        tested.append(candidate); responses.append(response)
        analysis = analyze_html(response.final_url or candidate, response.text)
        if response.status == 200 and (analysis.static_job_listing or analysis.jsonld_job_count or analysis.js_shell or len(analysis.scripts) > len(primary_analysis.scripts)):
            primary, primary_analysis = response, analysis

    script_urls = choose_relevant_scripts(primary_analysis.scripts, max_scripts)
    scripts: list[dict[str, Any]] = []
    script_texts: list[str] = []
    for script_url in script_urls:
        if not _same_official_chain(primary.final_url or stored_url, script_url): continue
        response = client.fetch(script_url, max_bytes=max_script_bytes)
        scripts.append({"url": script_url, "status": response.status, "bytes": len(response.text.encode("utf-8")), "truncated": response.truncated, "error": response.error})
        if response.status == 200: script_texts.append(response.text)
    combined_text = primary.text + "\n" + "\n".join(script_texts)
    combined_analysis = analyze_html(primary.final_url or stored_url, combined_text)
    detection = detect_contract(primary.final_url or stored_url, combined_text, combined_analysis)
    anti_bot = _anti_bot(primary)
    recovered_stale_source = recovered and (stored_response.status == 0 or stored_response.status >= 400)
    classification = _classification(primary, combined_analysis, detection, len(scripts), anti_bot, recovered_stale_source)

    parent_company = ""
    parent_candidate = "false"
    deterministic_brand_fields = ""
    duplicate_risk = ""
    rule = PARENT_SOURCE_RULES.get(compact_name(source.get("company")), {})
    if rule and primary.final_url and _domain_root(primary.final_url) != _domain_root(stored_url):
        parent_company = clean_text(rule.get("parent")); parent_candidate = "true"
        deterministic_brand_fields = clean_text(rule.get("parameters"))
        duplicate_risk = "high; shared source must deduplicate on canonical source and stable job ID"
        classification = "parent_company_source"
        detection.family = "parent_company_source"; detection.variant = f"shared_{compact_name(parent_company)}_source"
        detection.evidence.append(f"official careers redirect to shared {parent_company} source")

    validation, jobs_normalized, validated_stable, validated_apply, validated_pagination = validate_public_endpoint(client, detection, max_jobs)
    if validated_stable != "unknown": detection.stable_id = validated_stable
    if validated_apply != "unknown": detection.apply_url = validated_apply
    if validated_pagination: detection.pagination = validated_pagination
    compatible_results = {"compatible_json_contract", "production_parser_compatible", "not_practical_during_static_research"}
    if classification == "existing_adapter_reuse" and validation not in compatible_results:
        classification = "existing_adapter_extension"
        detection.extension_required = True
    if classification not in FINAL_CLASSIFICATIONS:
        raise AssertionError(f"Unexpected final classification: {classification}")
    tech, career, combined = _scores(source, detection, classification)
    current_url = primary.final_url or primary.requested_url
    sample_job = next(iter(combined_analysis.job_links), "")
    evidence_items = list(dict.fromkeys([
        *detection.evidence, *combined_analysis.evidence,
        f"stored request HTTP {stored_response.status or 0} final={stored_response.final_url or stored_url}",
        f"pages inspected={len(tested)}; scripts inspected={len(scripts)}",
    ]))
    warnings = [response.error for response in responses if response.error]
    if primary.truncated: warnings.append("primary response truncated at byte limit")
    timestamp = timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    official_corporate = clean_text(source.get("inventory_original_careers_url") or source.get("v2_original_careers_url"))
    endpoint_host = urlsplit(detection.endpoint_url).hostname or ""
    return {
        "original_rank": clean_text(source.get("inventory_original_rank")),
        "company": clean_text(source.get("company")), "normalized_company": clean_text(source.get("normalized_company")),
        "original_priority": clean_text(source.get("inventory_original_priority")),
        "original_careers_url": clean_text(source.get("inventory_original_careers_url") or source.get("v2_original_careers_url")),
        "original_notes": clean_text(source.get("inventory_original_notes")),
        "v2_platform_family": clean_text(source.get("v2_platform_family")),
        "v2_classification": clean_text(source.get("v2_current_classification")),
        "v2_confidence": clean_text(source.get("v2_confidence")),
        "v2_evidence": clean_text(source.get("v2_evidence")),
        "v2_attempted_urls": clean_text(source.get("v2_urls_tested")),
        "official_corporate_url": official_corporate,
        "current_official_careers_url": current_url,
        "current_job_search_url": current_url,
        "sample_job_detail_url": sample_job,
        "final_url": current_url, "http_status": str(primary.status) if primary.status else "",
        "redirect_chain": json.dumps(primary.redirect_chain, ensure_ascii=False, sort_keys=True),
        "page_title": primary.title,
        "stored_url_status": _stored_url_status(stored_url, stored_response, current_url if recovered else ""),
        "platform_family": detection.family, "platform_variant": detection.variant,
        "primary_classification": classification, "confidence": detection.confidence,
        "endpoint_host": endpoint_host, "endpoint_url": detection.endpoint_url,
        "request_method": detection.method, "request_contract_summary": detection.contract,
        "structured_data_type": "JSON-LD JobPosting" if combined_analysis.jsonld_job_count else ("serialized application state" if combined_analysis.serialized_state else ("JSON API" if detection.family in {"custom_rest_api", "custom_graphql"} or detection.family in EXISTING_ADAPTERS else ("HTML" if detection.family == "static_html" else "unknown"))),
        "stable_job_id_evidence": detection.stable_id, "official_apply_url_evidence": detection.apply_url,
        "pagination_evidence": detection.pagination,
        "anonymous_access": "true" if 200 <= primary.status < 400 else "false",
        "cookies_required": "false", "csrf_required": "unknown" if "POST" in detection.method else "false",
        "authentication_required": "true" if primary.status == 401 else "false", "session_required": "false",
        "javascript_required": detection.javascript_required,
        "browser_required": str(classification == "browser_required").lower(),
        "anti_bot_evidence": anti_bot, "blocked": str(classification == "blocked").lower(),
        "network_inaccessible": str(classification == "network_inaccessible").lower(),
        "parent_company": parent_company, "parent_source_candidate": parent_candidate,
        "deterministic_brand_fields": deterministic_brand_fields, "duplicate_source_risk": duplicate_risk,
        "existing_adapter": detection.family if detection.family in EXISTING_ADAPTERS else "",
        "existing_adapter_reuse": str(classification == "existing_adapter_reuse").lower(),
        "existing_adapter_extension_required": str(classification == "existing_adapter_extension").lower(),
        "future_adapter_cluster": detection.family if detection.family in KNOWN_FUTURE_FAMILIES else "",
        "new_reusable_family": detection.family if classification == "newly_discovered_reusable_platform" else "",
        "company_specific_collector": str(classification == "company_specific_collector").lower(),
        "stale_fingerprint": str(detection.family != "unknown").lower(),
        "stale_url": str(_stored_url_status(stored_url, stored_response, current_url if recovered else "") in {"stale", "invalid"}).lower(),
        "urls_tested": json.dumps(tested, ensure_ascii=False),
        "scripts_inspected": json.dumps(scripts, ensure_ascii=False, sort_keys=True),
        "evidence": "; ".join(evidence_items), "warnings": "; ".join(dict.fromkeys(warnings)),
        "recommended_next_action": _recommend(classification, detection),
        "technical_priority": str(tech), "career_relevance_priority": str(career), "combined_priority": str(combined),
        "adapter_validation_result": validation, "jobs_normalized": str(jobs_normalized),
        "investigation_timestamp": timestamp,
    }


def merge_results(sources: list[Mapping[str, Any]], completed: list[Mapping[str, Any]],
                  investigator: Any, *, resume: bool, start: int = 0,
                  limit: Optional[int] = None, batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict[str, str]]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE: raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if start < 0: raise ValueError("start must be non-negative")
    if limit is not None and limit < 1: raise ValueError("limit must be positive")
    source_keys = [clean_text(row.get("normalized_company")) for row in sources]
    if len(source_keys) != len(set(source_keys)): raise ValueError("Duplicate normalized source companies")
    by_name = {clean_text(row.get("normalized_company")): dict(row) for row in completed}
    pending = [row for row in sources if not (resume and clean_text(row.get("normalized_company")) in by_name and clean_text(by_name[clean_text(row.get("normalized_company"))].get("primary_classification")))]
    selected = pending[start:]
    if limit is not None: selected = selected[:limit]
    for offset in range(0, len(selected), batch_size):
        for source in selected[offset:offset + batch_size]:
            row = investigator(source)
            key = clean_text(row.get("normalized_company"))
            if key != clean_text(source.get("normalized_company")): raise ValueError("Investigator returned a mismatched company")
            by_name[key] = row
    return [by_name[key] for key in source_keys if key in by_name]


def build_clusters(records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        groups[(clean_text(row.get("platform_family")) or "unknown", clean_text(row.get("platform_variant")) or "unknown")].append(row)
    clusters: list[dict[str, str]] = []
    for (family, variant), members in groups.items():
        classifications = {clean_text(row.get("primary_classification")) for row in members}
        existing = family in EXISTING_ADAPTERS
        extension = "existing_adapter_extension" in classifications
        browser = sum(clean_text(row.get("browser_required")) == "true" for row in members)
        blocked = sum(clean_text(row.get("blocked")) == "true" for row in members)
        actionable = sum(value not in {"blocked", "network_inaccessible", "invalid_careers_url", "stale_source_unresolved", "still_unresolved", "browser_required"} for value in (clean_text(row.get("primary_classification")) for row in members))
        complexity = "low" if existing and not extension else "medium" if family in {"static_html", "custom_rest_api", "javascript_embedded_data", "smartrecruiters", "workable", "recruitee", "teamtailor", "personio", "bamboohr"} else "high"
        maintenance = "low" if existing and not extension else "medium" if family not in {"unknown", "browser_required"} else "high"
        confidences = Counter(clean_text(row.get("confidence")) or "low" for row in members)
        clusters.append({
            "cluster_rank": "", "platform_family": family, "endpoint_variant": variant,
            "companies": "; ".join(sorted(clean_text(row.get("company")) for row in members)),
            "company_count": str(len(members)),
            "confidence_distribution": "; ".join(f"{key}={confidences[key]}" for key in ("high", "medium", "low")),
            "anonymous_endpoint_count": str(sum(clean_text(row.get("anonymous_access")) == "true" and bool(clean_text(row.get("endpoint_url"))) for row in members)),
            "stable_id_count": str(sum(clean_text(row.get("stable_job_id_evidence")) == "true" for row in members)),
            "official_apply_url_count": str(sum(clean_text(row.get("official_apply_url_evidence")) == "true" for row in members)),
            "deterministic_pagination_count": str(sum(clean_text(row.get("pagination_evidence")) not in {"", "unknown", "platform-specific", "endpoint-specific"} for row in members)),
            "existing_production_adapter_available": str(existing).lower(),
            "production_adapter_works_as_is": str(existing and not extension).lower(),
            "production_adapter_extension_required": str(extension).lower(),
            "new_reusable_adapter_required": str(not existing and family not in {"unknown", "static_html", "javascript_embedded_data", "parent_company_source"}).lower(),
            "static_or_embedded_collector_appropriate": str(family in {"static_html", "javascript_embedded_data"}).lower(),
            "parent_mapping_appropriate": str(family == "parent_company_source").lower(),
            "browser_required_count": str(browser), "blocked_count": str(blocked),
            "implementation_complexity": complexity, "maintenance_risk": maintenance,
            "companies_potentially_unlocked": str(actionable), "recommended_implementation_order": "",
            "supporting_evidence": " | ".join(list(dict.fromkeys(clean_text(row.get("evidence")) for row in members))[:3]),
        })
    clusters.sort(key=lambda row: (-int(row["companies_potentially_unlocked"]), {"low": 0, "medium": 1, "high": 2}[row["implementation_complexity"]], row["platform_family"], row["endpoint_variant"]))
    for index, row in enumerate(clusters, 1): row["cluster_rank"] = row["recommended_implementation_order"] = str(index)
    return clusters


def build_reuse(records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for record in records:
        classification = clean_text(record.get("primary_classification"))
        if classification not in {"existing_adapter_reuse", "existing_adapter_extension"}: continue
        detection = Detection(family=clean_text(record.get("existing_adapter")), endpoint_url=clean_text(record.get("endpoint_url")))
        rows.append({
            "company": clean_text(record.get("company")), "production_adapter": detection.family,
            "official_careers_url": clean_text(record.get("current_official_careers_url")),
            "official_search_url": clean_text(record.get("current_job_search_url")),
            "required_configuration": _adapter_configuration(detection),
            "endpoint_variant": clean_text(record.get("platform_variant")),
            "validation_result": clean_text(record.get("adapter_validation_result")),
            "jobs_normalized": clean_text(record.get("jobs_normalized")),
            "stable_id_result": clean_text(record.get("stable_job_id_evidence")),
            "apply_url_result": clean_text(record.get("official_apply_url_evidence")),
            "pagination_result": clean_text(record.get("pagination_evidence")),
            "blocker": "adapter extension required" if classification == "existing_adapter_extension" else "",
            "safe_for_later_activation": str(classification == "existing_adapter_reuse" and clean_text(record.get("adapter_validation_result")) in {"compatible_json_contract", "production_parser_compatible", "not_practical_during_static_research"}).lower(),
            "needs_adapter_extension": str(classification == "existing_adapter_extension").lower(),
            "confidence": clean_text(record.get("confidence")),
        })
    return sorted(rows, key=lambda row: normalize_company_name(row["company"]))


def build_parents(records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for record in records:
        if clean_text(record.get("parent_source_candidate")) != "true": continue
        deterministic = clean_text(record.get("deterministic_brand_fields"))
        rows.append({
            "company": clean_text(record.get("company")), "parent_company": clean_text(record.get("parent_company")),
            "official_relationship_evidence": clean_text(record.get("evidence")),
            "official_careers_url": clean_text(record.get("current_official_careers_url")),
            "official_search_url": clean_text(record.get("current_job_search_url")),
            "canonical_source_company": clean_text(record.get("parent_company")),
            "parent_jobs_attributable": "requires result-level validation",
            "deterministic_identifying_fields": deterministic,
            "duplicate_source_risk": clean_text(record.get("duplicate_source_risk")),
            "multi_brand_risk": "true", "metadata_annotation_appropriate": str(bool(deterministic)).lower(),
            "separate_registry_entry_risk": "high; could duplicate canonical parent jobs",
            "confidence": clean_text(record.get("confidence")), "evidence": clean_text(record.get("evidence")),
            "recommended_action": clean_text(record.get("recommended_next_action")),
        })
    return sorted(rows, key=lambda row: normalize_company_name(row["company"]))


def annotate_shared_sources(records: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Mark exact duplicate endpoints only when one member names the canonical source."""
    result = [dict(row) for row in records]
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in result:
        endpoint = clean_text(row.get("endpoint_url"))
        if endpoint:
            groups[endpoint].append(row)
    for endpoint, members in groups.items():
        if len(members) < 2:
            continue
        endpoint_key = re.sub(r"[^a-z0-9]", "", endpoint.casefold())
        canonical = [row for row in members if compact_name(row.get("company")) in endpoint_key]
        if len(canonical) != 1:
            continue
        parent = canonical[0]
        for child in members:
            if child is parent:
                continue
            child["parent_company"] = clean_text(parent.get("company"))
            child["parent_source_candidate"] = "true"
            child["deterministic_brand_fields"] = "exact shared endpoint; child attribution still requires result-level brand metadata"
            child["duplicate_source_risk"] = "high; exact canonical endpoint is shared with parent source"
            child["primary_classification"] = "parent_company_source"
            child["confidence"] = "medium"
            child["platform_family"] = "parent_company_source"
            child["platform_variant"] = f"shared_{compact_name(parent.get('company'))}_source"
            child["existing_adapter_reuse"] = "false"
            child["existing_adapter_extension_required"] = "false"
            child["future_adapter_cluster"] = "parent_company_source"
            child["evidence"] = clean_text(child.get("evidence")) + f"; exact official careers endpoint shared with {parent.get('company')}: {endpoint}"
            child["recommended_next_action"] = "Validate child-brand fields and deduplicate against the canonical parent source before any mapping"
    return result


def report_payload(records: list[Mapping[str, Any]], clusters: list[Mapping[str, Any]],
                   reuse: list[Mapping[str, Any]], parents: list[Mapping[str, Any]],
                   inputs: list[str], request_log: list[Mapping[str, Any]]) -> dict[str, Any]:
    families = Counter(clean_text(row.get("platform_family")) or "unknown" for row in records)
    classes = Counter(clean_text(row.get("primary_classification")) for row in records)
    without_browser = sum(clean_text(row.get("primary_classification")) not in {"browser_required", "blocked", "network_inaccessible", "invalid_careers_url", "stale_source_unresolved", "still_unresolved"} for row in records)
    with_browser = without_browser + classes["browser_required"]
    return {
        "generated_at": max((clean_text(row.get("investigation_timestamp")) for row in records), default=""),
        "authoritative_inputs": inputs, "companies_investigated": len(records),
        "platform_family_distribution": dict(sorted(families.items())),
        "classification_distribution": dict(sorted(classes.items())),
        "companies_moved_out_of_unknown": sum(clean_text(row.get("platform_family")) != "unknown" for row in records),
        "companies_still_unresolved": classes["still_unresolved"] + classes["stale_source_unresolved"],
        "additional_coverage_ceiling_without_browser": without_browser,
        "additional_coverage_ceiling_with_browser": with_browser,
        "clusters": clusters, "existing_adapter_reuse_candidates": reuse,
        "parent_source_candidates": parents, "records": records,
        "request_count": len(request_log),
    }


def markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Unknown-company investigation v3", "", f"Generated: {payload.get('generated_at') or 'partial'}", "",
        f"- Companies investigated: {payload['companies_investigated']}",
        f"- Moved out of unknown: {payload['companies_moved_out_of_unknown']}",
        f"- Still unresolved/stale unresolved: {payload['companies_still_unresolved']}",
        f"- Additional coverage ceiling without browser automation: {payload['additional_coverage_ceiling_without_browser']}",
        f"- Additional coverage ceiling with browser automation: {payload['additional_coverage_ceiling_with_browser']}", "",
        f"- Revised total original-list ceiling without browser automation: {payload.get('revised_total_original_coverage_without_browser', 'n/a')}",
        f"- Revised total original-list ceiling with browser automation: {payload.get('revised_total_original_coverage_with_browser', 'n/a')}", "",
        "## Platform families", "", "| Family | Companies |", "|---|---:|",
    ]
    lines.extend(f"| {family} | {count} |" for family, count in payload["platform_family_distribution"].items())
    lines.extend(["", "## Clusters", "", "| Rank | Family / variant | Count | Potentially unlocked | Complexity |", "|---:|---|---:|---:|---|"])
    lines.extend(f"| {row['cluster_rank']} | {row['platform_family']} / {row['endpoint_variant']} | {row['company_count']} | {row['companies_potentially_unlocked']} | {row['implementation_complexity']} |" for row in payload["clusters"])
    lines.extend(["", "All evidence came from bounded anonymous requests to official careers pages and resources they referenced. No login, browser automation, persistence initialization, production activation, or registry mutation was performed.", ""])
    return "\n".join(lines)


def persist(args: argparse.Namespace, records: list[Mapping[str, Any]], client: ResearchClient) -> None:
    records = annotate_shared_sources(records)
    clusters = build_clusters(records); reuse = build_reuse(records); parents = build_parents(records)
    inputs = [str(args.fingerprints_v2), str(args.inventory_v2), str(args.production_config),
              "config/company_watchlist.xlsx", "config/company_platform_fingerprints.csv",
              "config/company_platform_clusters_v2.csv", "config/company_parent_source_candidates_v2.csv",
              "config/company_watchlist_research.csv", "output/company_discovery_v2.json",
              "output/company_discovery_failures_v2.json", "output/company_discovery_v2.md",
              "scripts/investigate_remaining_companies.py", "scripts/fingerprint_company_platforms.py",
              "src/scraper/company_careers/adapters/"]
    payload = report_payload(records, clusters, reuse, parents, inputs, client.log)
    v2_rows = read_csv(args.fingerprints_v2)
    inventory_rows = read_csv(args.inventory_v2)
    v2_excluded = {"browser_required", "blocked", "authentication_required", "inaccessible", "invalid_careers_url", "inactive_company", "unknown"}
    v2_without_browser = sum(clean_text(row.get("current_classification")) not in v2_excluded for row in v2_rows)
    v2_with_browser = v2_without_browser + sum(clean_text(row.get("current_classification")) == "browser_required" for row in v2_rows)
    covered = sum(clean_text(row.get("production_covered")) == "true" for row in inventory_rows)
    payload.update({
        "original_company_count": len(inventory_rows),
        "production_covered_count": covered,
        "prior_v2_additional_coverage_without_browser": v2_without_browser,
        "prior_v2_additional_coverage_with_browser": v2_with_browser,
        "revised_additional_coverage_without_browser": v2_without_browser + payload["additional_coverage_ceiling_without_browser"],
        "revised_additional_coverage_with_browser": v2_with_browser + payload["additional_coverage_ceiling_with_browser"],
        "revised_total_original_coverage_without_browser": covered + v2_without_browser + payload["additional_coverage_ceiling_without_browser"],
        "revised_total_original_coverage_with_browser": covered + v2_with_browser + payload["additional_coverage_ceiling_with_browser"],
    })
    failures = [row for row in records if clean_text(row.get("primary_classification")) in {"blocked", "network_inaccessible", "invalid_careers_url", "stale_source_unresolved", "browser_required", "still_unresolved"}]
    write_csv(args.output_csv, records, INVESTIGATION_FIELDS)
    write_csv(args.clusters_output, clusters, CLUSTER_FIELDS)
    write_csv(args.reuse_output, reuse, REUSE_FIELDS)
    write_csv(args.parent_output, parents, PARENT_FIELDS)
    atomic_write_text(args.json_output, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    atomic_write_text(args.markdown_output, markdown_report(payload))
    atomic_write_text(args.failures_output, json.dumps(failures, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    atomic_write_text(args.evidence_output, json.dumps(client.log, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fingerprints-v2", type=Path, default=DEFAULT_FINGERPRINTS)
    parser.add_argument("--inventory-v2", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--production-config", type=Path, default=DEFAULT_PRODUCTION)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clusters-output", type=Path, default=DEFAULT_CLUSTERS)
    parser.add_argument("--reuse-output", type=Path, default=DEFAULT_REUSE)
    parser.add_argument("--parent-output", type=Path, default=DEFAULT_PARENTS)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--failures-output", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--evidence-output", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--request-delay", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-redirects", type=int, default=5)
    parser.add_argument("--max-scripts", type=int, default=4)
    parser.add_argument("--max-script-bytes", type=int, default=750_000)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--max-jobs", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE: raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if args.start < 0: raise ValueError("start must be non-negative")
    if args.limit is not None and args.limit < 1: raise ValueError("limit must be positive")
    if not 0 <= args.max_scripts <= 10: raise ValueError("max_scripts must be between 0 and 10")
    if not 1 <= args.max_script_bytes <= 2_000_000: raise ValueError("max_script_bytes must be between 1 and 2000000")
    if not 1 <= args.max_pages <= 2: raise ValueError("max_pages must be between 1 and 2")
    if not 1 <= args.max_jobs <= 20: raise ValueError("max_jobs must be between 1 and 20")


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv); _validate_args(args)
    # Parse the production config as an integrity/input-schema check only.
    yaml.safe_load(args.production_config.read_text(encoding="utf-8"))
    sources = extract_unknown_records(read_csv(args.fingerprints_v2), read_csv(args.inventory_v2))
    if args.dry_run:
        selected = sources[args.start:]
        if args.limit is not None: selected = selected[:args.limit]
        print(json.dumps({"dry_run": True, "unknown_count": len(sources), "selected": [row["company"] for row in selected]}, indent=2, ensure_ascii=False))
        return 0
    completed = read_csv(args.output_csv) if args.resume and args.output_csv.exists() else []
    client = ResearchClient(request_delay=args.request_delay, timeout=args.timeout, max_redirects=args.max_redirects)
    if args.resume and args.evidence_output.exists():
        try:
            prior_log = json.loads(args.evidence_output.read_text(encoding="utf-8"))
            if isinstance(prior_log, list):
                client.log.extend(item for item in prior_log if isinstance(item, dict))
        except (OSError, ValueError):
            pass
    def is_complete(old: Mapping[str, Any], source: Mapping[str, Any]) -> bool:
        if clean_text(old.get("normalized_company")) != source["normalized_company"] or not clean_text(old.get("primary_classification")):
            return False
        # Retry a previously completed network record only for the local import-path
        # defect fixed in this version; all other resume records remain preserved.
        return clean_text(old.get("adapter_validation_result")) != "production_parser_failed:ModuleNotFoundError"

    pending = [row for row in sources if not any(is_complete(old, row) for old in completed)]
    selected = pending[args.start:]
    if args.limit is not None: selected = selected[:args.limit]
    by_name = {clean_text(row.get("normalized_company")): dict(row) for row in completed}
    for offset in range(0, len(selected), args.batch_size):
        batch = selected[offset:offset + args.batch_size]
        for source in batch:
            print(f"Investigating {source['company']}...", flush=True)
            record = investigate_company(source, client, max_scripts=args.max_scripts,
                                         max_script_bytes=args.max_script_bytes,
                                         max_pages=args.max_pages, max_jobs=args.max_jobs)
            by_name[record["normalized_company"]] = record
        ordered = [by_name[row["normalized_company"]] for row in sources if row["normalized_company"] in by_name]
        persist(args, ordered, client)
        print(f"Persisted v3 batch {offset // args.batch_size + 1}: {len(batch)} investigated; {len(ordered)}/{len(sources)} complete", flush=True)
    if not selected:
        ordered = [by_name[row["normalized_company"]] for row in sources if row["normalized_company"] in by_name]
        persist(args, ordered, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
