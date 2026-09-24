"""Build and anonymously validate v4 adapter-extension Batch B.

The command uses production scanner dispatch and production adapters. It writes
only milestone candidate/validated artifacts and an ignored JSON report; it
never initializes persistence, dashboards, browsers, SQLite, Sheets, or Supabase.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import lever, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
UNCOVERED_INVENTORY = ROOT / "config" / "company_uncovered_inventory_v4.csv"
FINGERPRINTS = ROOT / "config" / "company_uncovered_fingerprints_v4.csv"
RECOVERY_BATCHES = ROOT / "config" / "company_recovery_batches_v4.csv"
INVENTORY = ROOT / "config" / "company_adapter_extensions_v4b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.adapter_extensions_v4b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.adapter_extensions_v4b_validated.yaml"
REPORT = ROOT / "output" / "adapter_extensions_v4b_validation.json"

HISTORICAL_PRODUCTION_COUNT = 139
HISTORICAL_ORIGINAL_COVERAGE = 134
EXPECTED_PARENT_MAPPINGS = 4
MAX_BATCH_SIZE = 5
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RESPONSE_BYTES = 3_000_000
MAX_UNIQUE_REQUESTS = 12
SAFE_STATUSES = {"validated_with_jobs", "validated_zero_jobs"}
TEMPORARY_STATUSES = {"temporary_failure", "network_inaccessible", "failed_technical"}

INVENTORY_FIELDS = [
    "master_rank", "company", "prior_platform", "current_platform",
    "implementation_path", "adapter_family", "endpoint_variant",
    "official_careers_url", "listing_url", "api_url", "request_method",
    "tenant", "site_id", "board", "organization", "portal",
    "source_identity", "allowed_source_hosts", "allowed_apply_hosts",
    "stable_id_contract", "apply_url_contract", "pagination_contract",
    "prior_evidence_source", "current_evidence_source", "evidence_confidence",
    "evidence_timestamp", "validation_status", "safe_to_activate", "warnings",
    "exclusion_reason",
]


def _static(
    rank: int,
    name: str,
    careers_url: str,
    listing_url: str,
    *,
    listing_selector: str,
    job_card_selector: str,
    title_selector: str,
    location_selector: Optional[str],
    detail_link_selector: str,
    external_id_regex: str,
    department_selector: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": "static_html",
        "careers_url": careers_url,
        "listing_url": listing_url,
        "endpoint_variant": "server_rendered_links",
        "listing_selector": listing_selector,
        "job_card_selector": job_card_selector,
        "title_selector": title_selector,
        "location_selector": location_selector,
        "detail_link_selector": detail_link_selector,
        "external_id_regex": external_id_regex,
        "department_selector": department_selector,
        "allowed_apply_hosts": [(urlparse(listing_url).hostname or "").casefold()],
        "original_workbook_rank": rank,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RESPONSE_BYTES,
    }


def _lever(
    rank: int,
    name: str,
    careers_url: str,
    slug: str,
    *,
    department_allowlist: Optional[list[str]] = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "platform": "lever",
        "slug": slug,
        "careers_url": careers_url,
        "search_url": f"https://jobs.lever.co/{slug}",
        "endpoint_variant": "postings_api_v0",
        "allowed_apply_hosts": ["jobs.lever.co"],
        "original_workbook_rank": rank,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RESPONSE_BYTES,
    }
    if department_allowlist:
        entry["department_allowlist"] = department_allowlist
    return entry


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _static(
        80, "IMC Trading", "https://www.imc.com/us/careers",
        "https://www.imc.com/us/search-careers",
        listing_selector="h2", job_card_selector="a[href*=/us/careers/jobs/]",
        title_selector="h2", location_selector=None, detail_link_selector=":self",
        external_id_regex=r"/jobs/([0-9]+)$",
    ),
    _static(
        139, "Valve", "https://www.valvesoftware.com/en/about",
        "https://www.valvesoftware.com/en/",
        listing_selector="a[href*=job_id=]", job_card_selector="a[href*=job_id=]",
        title_selector="h5.job_title", location_selector=None,
        detail_link_selector=":self", external_id_regex=r"[?&]job_id=([0-9]+)",
    ),
    _static(
        143, "Roblox", "https://careers.roblox.com/", "https://careers.roblox.com/jobs",
        listing_selector="a[href^=/jobs/]", job_card_selector="a[href^=/jobs/]",
        title_selector="p.body--large", location_selector="span.caption:first-child",
        detail_link_selector=":self", external_id_regex=r"/jobs/([0-9]+)$",
        department_selector="span.caption:nth-child(2)",
    ),
    _lever(152, "Zoox", "https://zoox.com/careers", "zoox"),
    _static(
        156, "Unitree Robotics", "https://www.unitree.com/careers",
        "https://www.unitree.com/position/",
        listing_selector="a[href^=/position/]", job_card_selector="a[href^=/position/]",
        title_selector="p.title", location_selector="p.base-info",
        detail_link_selector=":self", external_id_regex=r"/position/([0-9]+)$",
    ),
    _lever(
        240, "Match Group", "https://mtch.com/careers", "matchgroup",
        department_allowlist=["Match Group"],
    ),
]


REVIEWS: list[dict[str, Any]] = [
    {
        "master_rank": 80, "company": "IMC Trading", "prior_platform": "static_html",
        "current_platform": "static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "server_rendered_numeric_links",
        "official_careers_url": "https://www.imc.com/us/careers",
        "listing_url": "https://www.imc.com/us/search-careers", "request_method": "GET",
        "allowed_source_hosts": "www.imc.com", "allowed_apply_hosts": "www.imc.com",
        "stable_id_contract": "numeric identifier in the official /us/careers/jobs/{id} path",
        "apply_url_contract": "same-host HTTPS IMC Trading job-detail URL",
        "pagination_contract": "one server-rendered listing page; maximum 20 normalized jobs",
        "current_evidence_source": "https://www.imc.com/us/careers;https://www.imc.com/us/search-careers",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 139, "company": "Valve", "prior_platform": "static_html",
        "current_platform": "static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "server_rendered_query_id_links",
        "official_careers_url": "https://www.valvesoftware.com/en/about",
        "listing_url": "https://www.valvesoftware.com/en/", "request_method": "GET",
        "allowed_source_hosts": "www.valvesoftware.com", "allowed_apply_hosts": "www.valvesoftware.com",
        "stable_id_contract": "numeric job_id query parameter on Valve Corporation job links",
        "apply_url_contract": "same-host HTTPS Valve Corporation job URL preserving job_id",
        "pagination_contract": "one server-rendered Valve Corporation listing page",
        "current_evidence_source": "https://www.valvesoftware.com/en/about;https://www.valvesoftware.com/en/",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 141, "company": "Niantic", "prior_platform": "invalid_careers_url",
        "current_platform": "scopely_phenom", "implementation_path": "stale_source",
        "adapter_family": "none", "endpoint_variant": "parent_portal_without_child_contract",
        "official_careers_url": "https://nianticlabs.com/careers",
        "listing_url": "https://careers.scopely.com/us/en/search-results", "request_method": "GET",
        "allowed_source_hosts": "nianticlabs.com;careers.scopely.com", "allowed_apply_hosts": "",
        "stable_id_contract": "Scopely stable IDs exist but are not a standalone Niantic collection",
        "apply_url_contract": "Scopely parent-portal URLs cannot establish canonical Niantic ownership",
        "pagination_contract": "not applicable to an independently representable Niantic source",
        "current_evidence_source": "https://nianticlabs.com/careers;https://careers.scopely.com/us/en/search-results",
        "evidence_confidence": "high", "validation_status": "stale_source",
        "exclusion_reason": "The official Niantic careers URL redirects to Scopely's mixed 134-job portal; references to Scopely Explore (formerly Niantic) do not form an independently attributable canonical Niantic collection.",
    },
    {
        "master_rank": 143, "company": "Roblox", "prior_platform": "static_html",
        "current_platform": "static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "server_rendered_numeric_links",
        "official_careers_url": "https://careers.roblox.com/", "listing_url": "https://careers.roblox.com/jobs",
        "request_method": "GET", "allowed_source_hosts": "careers.roblox.com",
        "allowed_apply_hosts": "careers.roblox.com", "stable_id_contract": "numeric /jobs/{id} path identifier",
        "apply_url_contract": "same-host HTTPS Roblox job-detail URL",
        "pagination_contract": "one server-rendered collection; maximum 20 normalized jobs",
        "current_evidence_source": "https://careers.roblox.com/;https://careers.roblox.com/jobs",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 152, "company": "Zoox", "prior_platform": "browser_required",
        "current_platform": "lever", "implementation_path": "existing_adapter_configuration_only",
        "adapter_family": "lever", "endpoint_variant": "postings_api_v0",
        "official_careers_url": "https://zoox.com/careers", "listing_url": "https://jobs.lever.co/zoox",
        "api_url": "https://api.lever.co/v0/postings/zoox?mode=json", "request_method": "GET",
        "board": "zoox", "allowed_source_hosts": "zoox.com;api.lever.co",
        "allowed_apply_hosts": "jobs.lever.co", "stable_id_contract": "Lever posting UUID scoped by the zoox board",
        "apply_url_contract": "HTTPS jobs.lever.co/zoox/{same UUID}",
        "pagination_contract": "one bounded public board response; maximum 20 normalized jobs",
        "current_evidence_source": "https://zoox.com/careers;https://api.lever.co/v0/postings/zoox?mode=json",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 156, "company": "Unitree Robotics", "prior_platform": "static_html",
        "current_platform": "static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "server_rendered_numeric_links",
        "official_careers_url": "https://www.unitree.com/careers",
        "listing_url": "https://www.unitree.com/position/", "request_method": "GET",
        "allowed_source_hosts": "www.unitree.com", "allowed_apply_hosts": "www.unitree.com",
        "stable_id_contract": "numeric identifier in the official /position/{id} path",
        "apply_url_contract": "same-host HTTPS official Unitree Robotics position URL",
        "pagination_contract": "one server-rendered collection; maximum 20 normalized jobs",
        "current_evidence_source": "https://www.unitree.com/careers;https://www.unitree.com/position/",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 237, "company": "LINE", "prior_platform": "static_html",
        "current_platform": "gatsby_shell", "implementation_path": "browser_required",
        "adapter_family": "none", "endpoint_variant": "client_rendered_job_collection",
        "official_careers_url": "https://careers.linecorp.com/",
        "listing_url": "https://careers.linecorp.com/jobs/", "request_method": "GET",
        "allowed_source_hosts": "careers.linecorp.com;linecorp.com;lycorp.co.jp", "allowed_apply_hosts": "",
        "stable_id_contract": "not present in anonymous server-rendered /jobs/ response",
        "apply_url_contract": "not conclusively present without client rendering",
        "pagination_contract": "browser-rendered",
        "current_evidence_source": "https://careers.linecorp.com/;https://careers.linecorp.com/jobs/",
        "evidence_confidence": "high", "validation_status": "browser_required",
        "exclusion_reason": "The still-active LINE careers identity is independently recognizable, but its anonymous /jobs/ HTML is a Gatsby shell with no job collection, stable IDs, or official apply URLs; LY Corporation links do not supply a standalone static contract.",
    },
    {
        "master_rank": 240, "company": "Match Group", "prior_platform": "blocked",
        "current_platform": "lever", "implementation_path": "existing_adapter_variant_extension",
        "adapter_family": "lever", "endpoint_variant": "postings_api_v0_department_allowlist",
        "official_careers_url": "https://mtch.com/careers", "listing_url": "https://jobs.lever.co/matchgroup",
        "api_url": "https://api.lever.co/v0/postings/matchgroup?mode=json", "request_method": "GET",
        "board": "matchgroup", "allowed_source_hosts": "mtch.com;api.lever.co",
        "allowed_apply_hosts": "jobs.lever.co", "stable_id_contract": "Lever posting UUID scoped by the matchgroup board",
        "apply_url_contract": "HTTPS jobs.lever.co/matchgroup/{same UUID}",
        "pagination_contract": "one bounded board response filtered by exact department=Match Group; maximum 20 jobs",
        "current_evidence_source": "https://mtch.com/careers;https://api.lever.co/v0/postings/matchgroup?mode=json",
        "evidence_confidence": "high",
    },
]

EXPECTED_COMPANIES = [review["company"] for review in REVIEWS]
CANDIDATE_COMPANIES = [entry["name"] for entry in CANDIDATE_ENTRIES]


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _indexed_rows(path: Path) -> dict[str, dict[str, str]]:
    return {row["company"]: row for row in _csv_rows(path)}


def _entry_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in CANDIDATE_ENTRIES if entry["name"] == name), None)


def reconcile_candidates() -> None:
    uncovered = _indexed_rows(UNCOVERED_INVENTORY)
    fingerprints = _indexed_rows(FINGERPRINTS)
    for review in REVIEWS:
        name = str(review["company"])
        if name not in uncovered or name not in fingerprints:
            raise ValueError(f"{name}: missing v4 evidence")
        if uncovered[name].get("recoverability_class") != "adapter_extension_required":
            raise ValueError(f"{name}: not derived from the v4 adapter-extension class")
        if int(uncovered[name]["master_rank"]) != int(review["master_rank"]):
            raise ValueError(f"{name}: master rank differs from v4 evidence")
    ranks = [int(item["master_rank"]) for item in REVIEWS]
    if ranks != sorted(ranks):
        raise ValueError("Reviewed companies do not follow master-workbook order")
    batch = next(row for row in _csv_rows(RECOVERY_BATCHES) if row["batch_id"] == "v4-02")
    batch_names = [value.strip() for value in batch["companies"].split(";")]
    if any(name not in batch_names for name in EXPECTED_COMPANIES):
        raise ValueError("Reviewed companies are not all present in v4-02")
    for entry in CANDIDATE_ENTRIES:
        error = validator.configuration_error(entry)
        if error:
            raise ValueError(f"{entry['name']}: {error}")
        target = CompanyTarget.from_dict(entry)
        (static_html.validate_target if target.platform == "static_html" else lever.validate_target)(target)
        if not 1 <= int(entry["max_pages"]) <= MAX_PAGES or int(entry["max_jobs"]) != MAX_JOBS:
            raise ValueError(f"{entry['name']}: invalid validation bounds")


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    entries: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    additions = [dict(item) for item in (entries if entries is not None else CANDIDATE_ENTRIES)]
    if production.resolve() == PRODUCTION.resolve():
        reconcile_candidates()
    _, baseline = validator.load_registry(production)
    if production.resolve() == PRODUCTION.resolve() and len(baseline) != HISTORICAL_PRODUCTION_COUNT:
        raise ValueError("Production is not the 139-entry v4b historical baseline")
    duplicate_names = validator._duplicate_company_owners(baseline, additions)
    duplicate_sources = validator._duplicate_owners(baseline, additions)
    if duplicate_names:
        raise ValueError(f"duplicate_company: {duplicate_names}")
    if duplicate_sources:
        raise ValueError(f"duplicate_source: {duplicate_sources}")
    content = validator.build_candidate_registry_bytes(production, additions)
    validator._write_bytes_if_changed(destination, content)
    if not content.startswith(production.read_bytes()):
        raise ValueError("Candidate registry does not preserve the historical byte prefix")
    if len(load_target_companies(str(destination))) != len(baseline) + len(additions):
        raise ValueError("Candidate registry is not loader-compatible")
    return content


class CachedBoundedSession:
    """Cache identical anonymous requests and enforce hard request/byte bounds."""

    def __init__(self, session: Optional[Any] = None) -> None:
        self.session = session or requests.Session()
        self.cache: dict[str, Any] = {}
        self.cache_hits = 0
        self.network_requests = 0
        self.unique_requests: list[str] = []
        self.responses: list[Any] = []

    @staticmethod
    def _key(method: str, url: str, kwargs: Mapping[str, Any]) -> str:
        material = {"method": method, "url": url, "params": kwargs.get("params"),
                    "json": kwargs.get("json"), "data": kwargs.get("data")}
        return json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))

    @staticmethod
    def _size(response: Any) -> int:
        content = getattr(response, "content", None)
        return len(content) if content is not None else len(str(getattr(response, "text", "") or "").encode())

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.pop("cookies", None)
        key = self._key(method, url, kwargs)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        if key not in self.unique_requests:
            if len(self.unique_requests) >= MAX_UNIQUE_REQUESTS:
                raise AdapterError("v4b validator adaptive request limit exceeded")
            self.unique_requests.append(key)
        self.network_requests += 1
        response = getattr(self.session, method)(url, **kwargs)
        if self._size(response) > MAX_RESPONSE_BYTES:
            raise AdapterError("v4b validator response byte limit exceeded")
        self.responses.append(response)
        if getattr(response, "status_code", None) == 200:
            self.cache[key] = response
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("post", url, **kwargs)


def _metadata(session: CachedBoundedSession, platform: str) -> Mapping[str, Any]:
    attribute = {"static_html": "static_html_pagination", "lever": "lever_pagination"}.get(platform)
    value = getattr(session, attribute, {}) if attribute else {}
    return value if isinstance(value, Mapping) else {}


def _valid_empty(session: CachedBoundedSession, platform: str) -> bool:
    metadata = _metadata(session, platform)
    return bool(metadata.get("zero_job_contract") or metadata.get("stop_reason") == "validated_zero_jobs")


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _status_for_error(exc: BaseException) -> str:
    message = str(exc).casefold()
    if "authentication" in message or "login" in message:
        return "authentication_required"
    if "session" in message:
        return "session_required"
    if "browser" in message or "javascript" in message:
        return "browser_required"
    if "blocked" in message or "access denied" in message or "http 403" in message or "http 451" in message:
        return "blocked"
    if "apply" in message and ("host" in message or "allowed-host" in message):
        return "invalid_apply_host"
    if "apply" in message:
        return "invalid_apply_url"
    if "stable" in message or "identifier" in message:
        return "missing_stable_id"
    if "unsupported" in message:
        return "unsupported_variant"
    if "byte limit" in message or "malformed" in message or "invalid json" in message:
        return "malformed_response"
    if isinstance(exc, requests.RequestException):
        return "network_inaccessible"
    return "failed_technical"


def _base_result(review: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company": review["company"], "master_rank": review["master_rank"],
        "prior_classification": "adapter_extension_required",
        "current_platform": review["current_platform"], "implementation_path": review["implementation_path"],
        "adapter_family": review["adapter_family"], "adapter_variant": review["endpoint_variant"],
        "endpoint_identifiers": {}, "pages_requested": 0, "jobs_observed": 0,
        "normalized_jobs_inspected": 0, "stable_id_result": "not_tested",
        "apply_url_result": "not_tested", "apply_host_result": "not_tested",
        "pagination_result": "not_tested", "repeated_page_or_cursor_result": "not_detected",
        "no_new_id_result": "not_detected", "valid_empty_result": False,
        "authentication_check": "not_detected", "session_check": "not_detected",
        "browser_check": "not_detected", "blocked_response_check": "not_detected",
        "malformed_response_check": "not_detected", "duplicate_company_result": "unique",
        "duplicate_source_result": "unique", "status": review.get("validation_status", "pending_validation"),
        "safe_to_activate": False, "warnings": [], "diagnostics": {},
    }


def validate_candidate(
    entry: Mapping[str, Any],
    review: Optional[Mapping[str, Any]] = None,
    *,
    duplicate_of: Optional[str] = None,
    duplicate_company_of: Optional[str] = None,
    timeout: int = 20,
    session_factory: Any = requests.Session,
) -> dict[str, Any]:
    review = review or next(item for item in REVIEWS if item["company"] == entry["name"])
    result = _base_result(review)
    result["endpoint_identifiers"] = validator.configured_identifiers(entry)
    result["diagnostics"] = {"endpoint": validator.endpoint_for(entry), "network_requests": 0, "request_cache_hits": 0}
    if duplicate_company_of:
        result.update(status="duplicate_company", duplicate_company_result=f"duplicate_of:{duplicate_company_of}")
        return result
    if duplicate_of:
        result.update(status="duplicate_source", duplicate_source_result=f"duplicate_of:{duplicate_of}")
        return result
    error = validator.configuration_error(entry)
    if error:
        result.update(status="invalid_configuration", diagnostics={**result["diagnostics"], "error": error})
        return result
    adapter = scanner.ADAPTERS.get(str(entry["platform"]))
    if adapter is None:
        result.update(status="unsupported_variant")
        return result
    session = CachedBoundedSession(session_factory())
    try:
        jobs = adapter(session, CompanyTarget.from_dict(dict(entry)), timeout)
        inspected = list(jobs[:MAX_JOBS])
        if len({job.job_id for job in inspected}) != len(inspected) or any(not job.job_id for job in inspected):
            raise AdapterError("Candidate has missing or duplicate stable IDs")
        allowed = [str(value).casefold() for value in entry.get("allowed_apply_hosts") or []]
        for job in inspected:
            parsed = urlparse(job.url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise AdapterError("Candidate has an invalid official apply URL")
            if not _allowed_host(parsed.hostname.casefold(), allowed):
                raise AdapterError("Candidate has an invalid apply host")
        valid_empty = not jobs and _valid_empty(session, str(entry["platform"]))
        if not jobs and not valid_empty:
            raise AdapterError("Candidate empty response did not prove a valid empty collection")
        metadata = _metadata(session, str(entry["platform"]))
        stop_reason = str(metadata.get("stop_reason") or ("validated_zero_jobs" if valid_empty else "bounded_collection"))
        diagnostics = list(metadata.get("diagnostics") or [])
        warnings = [f"production parser skipped {len(diagnostics)} unsupported records"] if diagnostics else []
        result.update(
            pages_requested=len(session.unique_requests), jobs_observed=len(jobs),
            normalized_jobs_inspected=len(inspected), stable_id_result="valid" if jobs else "not_applicable_zero_jobs",
            apply_url_result="valid" if jobs else "not_applicable_zero_jobs",
            apply_host_result="valid" if jobs else "not_applicable_zero_jobs", pagination_result=stop_reason,
            repeated_page_or_cursor_result="terminated" if "repeated" in stop_reason else "not_detected",
            no_new_id_result="terminated" if "no_new" in stop_reason else "not_detected",
            valid_empty_result=valid_empty, authentication_check="not_required", session_check="not_required",
            browser_check="not_required", blocked_response_check="not_detected",
            malformed_response_check="accepted_valid_schema",
            status="validated_with_jobs" if jobs else "validated_zero_jobs", safe_to_activate=True,
            warnings=warnings,
            diagnostics={"endpoint": validator.endpoint_for(entry), "network_requests": session.network_requests,
                         "request_cache_hits": session.cache_hits, "adapter_diagnostics": diagnostics},
        )
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        status = _status_for_error(exc)
        result.update(
            pages_requested=len(session.unique_requests), status=status,
            authentication_check="rejected" if status == "authentication_required" else "not_detected",
            session_check="rejected" if status == "session_required" else "not_detected",
            browser_check="rejected" if status == "browser_required" else "not_detected",
            blocked_response_check="rejected" if status == "blocked" else "not_detected",
            malformed_response_check="rejected" if status == "malformed_response" else "not_detected",
            diagnostics={"endpoint": validator.endpoint_for(entry), "network_requests": session.network_requests,
                         "request_cache_hits": session.cache_hits, "error": str(exc)},
        )
    return result


def _excluded_results() -> list[dict[str, Any]]:
    results = []
    for review in REVIEWS:
        if _entry_for(str(review["company"])) is not None:
            continue
        result = _base_result(review)
        status = str(review["validation_status"])
        result.update(status=status, browser_check="rejected" if status == "browser_required" else "not_detected",
                      warnings=[str(review["exclusion_reason"])], diagnostics={"error": review["exclusion_reason"]})
        results.append(result)
    return results


def _inventory_rows(results: Optional[Iterable[Mapping[str, Any]]] = None) -> list[dict[str, Any]]:
    v4 = _indexed_rows(UNCOVERED_INVENTORY)
    result_by_name = {str(item["company"]): item for item in (results or [])}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for review in REVIEWS:
        name = str(review["company"])
        entry = _entry_for(name)
        result = result_by_name.get(name)
        status = str(result["status"]) if result else str(review.get("validation_status") or "pending_validation")
        safe = bool(result and result.get("safe_to_activate"))
        rows.append({field: {
            "source_identity": validator.source_key(entry) if entry else "",
            "prior_evidence_source": v4[name].get("prior_evidence_source", ""),
            "evidence_timestamp": now, "validation_status": status,
            "safe_to_activate": str(safe).lower(),
            "warnings": ";".join(str(value) for value in (result.get("warnings", []) if result else [])),
            "exclusion_reason": "" if safe else str(review.get("exclusion_reason") or (result or {}).get("diagnostics", {}).get("error") or ""),
        }.get(field, review.get(field, "")) for field in INVENTORY_FIELDS})
    return rows


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    from io import StringIO
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=INVENTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_inventory_rows(results))
    validator._write_bytes_if_changed(INVENTORY, buffer.getvalue().encode("utf-8"))


def validate(
    *, timeout: int = 20, batch_delay: float = 0.25,
    session_factory: Any = requests.Session, sleep: Any = time.sleep,
) -> dict[str, Any]:
    production_before = PRODUCTION.read_bytes()
    mappings_before = PARENT_MAPPINGS.read_bytes()
    if len(load_parent_source_mappings(PARENT_MAPPINGS)) != EXPECTED_PARENT_MAPPINGS:
        raise ValueError("Parent mappings are not the four-entry historical baseline")
    candidate_bytes = build_candidate_registry()
    _, production_entries = validator.load_registry(PRODUCTION)
    _, candidate_entries = validator.load_registry(CANDIDATE_REGISTRY)
    additions = validator.detect_additions(production_entries, candidate_entries)
    if [entry["name"] for entry in additions] != CANDIDATE_COMPANIES:
        raise ValueError("Candidate registry does not contain the exact ordered safe representations")
    duplicate_sources = validator._duplicate_owners(production_entries, additions)
    duplicate_companies = validator._duplicate_company_owners(production_entries, additions)
    reviews = {str(item["company"]): item for item in REVIEWS}
    results = []
    for start in range(0, len(additions), MAX_BATCH_SIZE):
        for offset, entry in enumerate(additions[start:start + MAX_BATCH_SIZE]):
            index = start + offset
            results.append(validate_candidate(
                entry, reviews[str(entry["name"])], duplicate_of=duplicate_sources.get(index),
                duplicate_company_of=duplicate_companies.get(index), timeout=timeout,
                session_factory=session_factory,
            ))
        if start + MAX_BATCH_SIZE < len(additions) and batch_delay > 0:
            sleep(batch_delay)
    results.extend(_excluded_results())
    results.sort(key=lambda item: int(item["master_rank"]))
    safe_names = {str(item["company"]) for item in results if item["safe_to_activate"]}
    safe_additions = [entry for entry in additions if entry["name"] in safe_names]
    validated_bytes = validator.build_validated_registry_bytes(PRODUCTION, safe_additions)
    validator._write_bytes_if_changed(VALIDATED_REGISTRY, validated_bytes)
    _write_inventory(results)
    counts = Counter(str(item["status"]) for item in results)
    totals = {
        "reviewed_companies": len(results), "representable_candidates": len(additions),
        "configuration_only_candidates": sum(item["implementation_path"] == "existing_adapter_configuration_only" for item in results),
        "adapter_extension_candidates": sum(item["implementation_path"] in {"existing_adapter_variant_extension", "reusable_static_html_variant"} for item in results),
        "reclassified_companies": sum(item["prior_platform"] != item["current_platform"] for item in REVIEWS),
        "validated_with_jobs": counts["validated_with_jobs"], "validated_zero_jobs": counts["validated_zero_jobs"],
        "excluded": sum(not item["safe_to_activate"] for item in results),
        "temporary_failures": sum(counts[name] for name in TEMPORARY_STATUSES),
        "candidate_registry_count": len(candidate_entries),
        "validated_registry_count": len(load_target_companies(str(VALIDATED_REGISTRY))),
        "projected_original_company_coverage": HISTORICAL_ORIGINAL_COVERAGE + len(safe_additions),
    }
    report = {
        "evidence_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "production_registry_sha256": hashlib.sha256(production_before).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mappings_before).hexdigest().upper(),
        "candidate_registry_has_exact_production_prefix": candidate_bytes.startswith(production_before),
        "validated_registry_has_exact_production_prefix": validated_bytes.startswith(production_before),
        "batch_size_limit": MAX_BATCH_SIZE, "maximum_pages_per_company": MAX_PAGES,
        "maximum_normalized_jobs_inspected_per_company": MAX_JOBS,
        "maximum_response_bytes": MAX_RESPONSE_BYTES, "totals": totals, "results": results,
    }
    validator._write_json_if_changed(REPORT, report)
    if PRODUCTION.read_bytes() != production_before or PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Production registry or parent mappings changed during validation")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--batch-delay", type=float, default=0.25)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.build_only:
            content = build_candidate_registry()
            _write_inventory()
            payload: Mapping[str, Any] = {"candidate_registry": str(CANDIDATE_REGISTRY),
                                          "candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))),
                                          "bytes": len(content)}
        else:
            payload = validate(timeout=args.timeout, batch_delay=args.batch_delay)
    except (OSError, ValueError) as exc:
        print(f"Validation setup failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
