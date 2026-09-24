"""Build and anonymously validate the first v4 adapter-extension review batch.

The command uses only production company-career adapters.  It writes milestone
candidate artifacts and an ignored JSON report; it never scans the production
registry or initializes persistence, dashboards, Supabase, SQLite, or Sheets.
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
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
UNCOVERED_INVENTORY = ROOT / "config" / "company_uncovered_inventory_v4.csv"
FINGERPRINTS = ROOT / "config" / "company_uncovered_fingerprints_v4.csv"
CLUSTERS = ROOT / "config" / "company_uncovered_clusters_v4.csv"
RECOVERY_BATCHES = ROOT / "config" / "company_recovery_batches_v4.csv"
INVENTORY = ROOT / "config" / "company_adapter_extensions_v4a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.adapter_extensions_v4a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.adapter_extensions_v4a_validated.yaml"
REPORT = ROOT / "output" / "adapter_extensions_v4a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 133
HISTORICAL_ORIGINAL_COVERAGE = 128
EXPECTED_PARENT_MAPPINGS = 4
MAX_BATCH_SIZE = 5
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RESPONSE_BYTES = 3_000_000
MAX_UNIQUE_REQUESTS = 2
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
    location_selector: str,
    detail_link_selector: str,
    external_id_regex: str,
    allowed_apply_hosts: list[str],
    department_selector: Optional[str] = None,
    next_page_selector: Optional[str] = None,
    pagination_url_template: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": "static_html",
        "careers_url": careers_url,
        "listing_url": listing_url,
        "endpoint_variant": "server_rendered_cards",
        "listing_selector": listing_selector,
        "job_card_selector": job_card_selector,
        "title_selector": title_selector,
        "location_selector": location_selector,
        "detail_link_selector": detail_link_selector,
        "external_id_regex": external_id_regex,
        "department_selector": department_selector,
        "next_page_selector": next_page_selector,
        "pagination_url_template": pagination_url_template,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": rank,
        "max_pages": MAX_PAGES,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RESPONSE_BYTES,
    }


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "onsemi",
        "platform": "oracle_recruiting",
        "careers_url": "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
        "search_url": "https://www.onsemi.com/careers",
        "endpoint_url": "https://hctz.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
        "endpoint_variant": "hcmRestApi_recruitingCEJobRequisitions",
        "site_number": "CX_1001",
        "locale": "en",
        "allowed_apply_hosts": ["hctz.fa.us2.oraclecloud.com"],
        "original_workbook_rank": 38,
        "max_pages": MAX_PAGES,
        "max_jobs": MAX_JOBS,
    },
    _static(
        91, "Siemens", "https://www.siemens.com/global/en/company/jobs.html",
        "https://jobs.siemens.com/en_US/externaljobs/SearchJobs",
        listing_selector="div.results--listed",
        job_card_selector="article.article--result",
        title_selector="h3.article__header__text__title a[href]",
        location_selector="span.list-item-location",
        detail_link_selector="h3.article__header__text__title a[href]",
        external_id_regex=r"/JobDetail/([0-9]+)",
        department_selector="span.list-item-family",
        next_page_selector="li.paginationNextLink a[href]",
        allowed_apply_hosts=["jobs.siemens.com"],
    ),
    {
        "name": "Schneider Electric",
        "platform": "icims",
        "tenant": "schneider-electric",
        "board_id": "schneider-electric-public",
        "careers_url": "https://careers.se.com/careers-at-schneider-electric",
        "search_url": "https://careers.se.com/jobs",
        "endpoint_url": "https://careers.se.com/api/jobs",
        "endpoint_variant": "icims_jibe_api",
        "allowed_apply_hosts": [
            "careers-se.icims.com", "zhcareers-se.icims.com",
            "frcareers-se.icims.com", "schneiderele.taleo.net",
        ],
        "apply_url_id_query_param": "job",
        "original_workbook_rank": 93,
        "page_size": 10,
        "max_pages": MAX_PAGES,
        "max_jobs": MAX_JOBS,
    },
    _static(
        171, "Chevron", "https://careers.chevron.com/",
        "https://careers.chevron.com/search-jobs",
        listing_selector="section#search-results",
        job_card_selector="section#search-results-list li",
        title_selector="a[data-job-id] h2",
        location_selector="a[data-job-id] span.job-location",
        detail_link_selector="a[data-job-id]",
        external_id_regex=r"/([0-9]+)/?$",
        pagination_url_template="https://careers.chevron.com/search-jobs?p={page}",
        allowed_apply_hosts=["careers.chevron.com"],
    ),
    {
        "name": "Mercado Libre",
        "platform": "eightfold",
        "careers_url": "https://mercadolibre.eightfold.ai/careers",
        "search_url": "https://careers-meli.mercadolibre.com/es/positions",
        "endpoint_url": "https://mercadolibre.eightfold.ai/api/pcsx/search",
        "endpoint_variant": "pcsx",
        "organization_id": "mercadolibre.com",
        "allowed_apply_hosts": ["mercadolibre.eightfold.ai"],
        "original_workbook_rank": 199,
        "max_pages": MAX_PAGES,
        "max_jobs": MAX_JOBS,
    },
    _static(
        218, "Bolt", "https://bolt.eu/en/careers/",
        "https://bolt.eu/en/careers/positions/",
        listing_selector="nav[aria-label=pagination]",
        job_card_selector="div[data-testid=AccordionItem]",
        title_selector="h3 span.rt-high-contrast",
        location_selector="h3 span.rt-truncate",
        detail_link_selector="a[href*=/careers/positions/]",
        external_id_regex=r"/positions/([0-9a-f-]{36})/",
        department_selector="h3 span.rt-r-size-1",
        pagination_url_template="https://bolt.eu/en/careers/positions/?page={page}",
        allowed_apply_hosts=["bolt.eu"],
    ),
]


REVIEWS: list[dict[str, Any]] = [
    {
        "master_rank": 38, "company": "onsemi", "prior_platform": "oracle_recruiting",
        "current_platform": "oracle_recruiting", "implementation_path": "existing_adapter_configuration_only",
        "adapter_family": "oracle_recruiting", "endpoint_variant": "hcmRestApi_recruitingCEJobRequisitions",
        "official_careers_url": "https://www.onsemi.com/careers",
        "listing_url": "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
        "api_url": "https://hctz.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
        "request_method": "GET", "site_id": "CX_1001", "portal": "CX_1001",
        "allowed_source_hosts": "www.onsemi.com;hctz.fa.us2.oraclecloud.com",
        "allowed_apply_hosts": "hctz.fa.us2.oraclecloud.com",
        "stable_id_contract": "Oracle requisition Id; source namespaced by Oracle host and site number",
        "apply_url_contract": "HTTPS CandidateExperience site job URL containing the same requisition Id",
        "pagination_contract": "Oracle finder offsets 0 and 24; maximum two pages",
        "current_evidence_source": "https://www.onsemi.com/careers;https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 91, "company": "Siemens", "prior_platform": "static_html",
        "current_platform": "avature", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "avature_folder_portal_server_rendered",
        "official_careers_url": "https://www.siemens.com/global/en/company/jobs.html",
        "listing_url": "https://jobs.siemens.com/en_US/externaljobs/SearchJobs",
        "request_method": "GET", "tenant": "jobs.siemens.com", "board": "144", "portal": "en_US/externaljobs",
        "allowed_source_hosts": "www.siemens.com;jobs.siemens.com", "allowed_apply_hosts": "jobs.siemens.com",
        "stable_id_contract": "numeric JobDetail path identifier",
        "apply_url_contract": "same-host HTTPS JobDetail URL",
        "pagination_contract": "reviewed next link with folderOffset and folderRecordsPerPage; maximum two pages",
        "current_evidence_source": "https://www.siemens.com/global/en/company/jobs.html;https://jobs.siemens.com/en_US/externaljobs/SearchJobs",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 93, "company": "Schneider Electric", "prior_platform": "icims",
        "current_platform": "icims", "implementation_path": "existing_adapter_variant_extension",
        "adapter_family": "icims", "endpoint_variant": "icims_jibe_api_with_allowlisted_query_id",
        "official_careers_url": "https://careers.se.com/careers-at-schneider-electric",
        "listing_url": "https://careers.se.com/jobs", "api_url": "https://careers.se.com/api/jobs",
        "request_method": "GET", "tenant": "schneider-electric", "board": "schneider-electric-public",
        "allowed_source_hosts": "careers.se.com;careers-se.icims.com",
        "allowed_apply_hosts": "careers-se.icims.com;zhcareers-se.icims.com;frcareers-se.icims.com;schneiderele.taleo.net",
        "stable_id_contract": "Jibe slug/req_id equals /jobs/{id} or explicit job query parameter",
        "apply_url_contract": "allowlisted iCIMS URL or allowlisted Taleo URL with one validated job query ID",
        "pagination_contract": "GET page=1 then page=2; maximum ten items per page",
        "current_evidence_source": "https://careers.se.com/jobs;https://careers.se.com/api/jobs",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 100, "company": "Yaskawa Electric", "prior_platform": "static_html",
        "current_platform": "regional_careers_directory", "implementation_path": "stale_source",
        "adapter_family": "none", "endpoint_variant": "regional_landing_links",
        "official_careers_url": "https://www.yaskawa-global.com/jobs",
        "listing_url": "https://www.yaskawa-global.com/jobs", "request_method": "GET",
        "allowed_source_hosts": "www.yaskawa-global.com", "allowed_apply_hosts": "",
        "stable_id_contract": "none: page contains regional destinations, not job records",
        "apply_url_contract": "none", "pagination_contract": "none",
        "current_evidence_source": "https://www.yaskawa-global.com/jobs", "evidence_confidence": "high",
        "validation_status": "stale_source", "exclusion_reason": "Global source is a regional careers directory and exposes no canonical job collection or stable job IDs.",
    },
    {
        "master_rank": 126, "company": "Aptiv", "prior_platform": "static_html",
        "current_platform": "hawksearch_rest", "implementation_path": "unsupported_variant",
        "adapter_family": "none", "endpoint_variant": "hawksearch_v2_search",
        "official_careers_url": "https://www.aptiv.com/en/jobs",
        "listing_url": "https://www.aptiv.com/en/jobs/search",
        "api_url": "https://aptivcareers.searchapi-na.hawksearch.com/api/v2/search/", "request_method": "POST",
        "tenant": "28fad22cfe584b879917858203dd97ce",
        "allowed_source_hosts": "www.aptiv.com;aptivcareers.searchapi-na.hawksearch.com",
        "allowed_apply_hosts": "www.aptiv.com",
        "stable_id_contract": "HawkSearch jobrequisitionid when a reusable adapter exists",
        "apply_url_contract": "HawkSearch externalapplyurl when allowlisted",
        "pagination_contract": "HawkSearch request page contract; not implemented for a singleton candidate",
        "current_evidence_source": "https://www.aptiv.com/en/jobs/search;https://www.aptiv.com/scripts/careers.js",
        "evidence_confidence": "high", "validation_status": "unsupported_variant",
        "exclusion_reason": "Public HawkSearch REST is a singleton contract in this batch; milestone rules prohibit a one-company new adapter.",
    },
    {
        "master_rank": 171, "company": "Chevron", "prior_platform": "eightfold",
        "current_platform": "talentbrew_static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "talentbrew_server_rendered",
        "official_careers_url": "https://careers.chevron.com/", "listing_url": "https://careers.chevron.com/search-jobs",
        "request_method": "GET", "allowed_source_hosts": "careers.chevron.com",
        "allowed_apply_hosts": "careers.chevron.com", "stable_id_contract": "numeric data-job-id and terminal detail-path ID",
        "apply_url_contract": "same-host HTTPS /job/.../{id} URL",
        "pagination_contract": "GET ?p=1 then ?p=2; stop at 20 normalized jobs",
        "current_evidence_source": "https://careers.chevron.com/search-jobs", "evidence_confidence": "high",
    },
    {
        "master_rank": 197, "company": "OHB SE", "prior_platform": "static_html",
        "current_platform": "cornerstone_csod", "implementation_path": "browser_required",
        "adapter_family": "none", "endpoint_variant": "csod_careersite_shell",
        "official_careers_url": "https://www.ohb.de/en/career",
        "listing_url": "https://career-ohb.csod.com/ux/ats/careersite/4/home?c=career-ohb&lang=en-GB",
        "request_method": "GET", "tenant": "career-ohb", "site_id": "4",
        "allowed_source_hosts": "www.ohb.de;career-ohb.csod.com", "allowed_apply_hosts": "career-ohb.csod.com",
        "stable_id_contract": "not available without browser-rendered listing",
        "apply_url_contract": "not conclusively available", "pagination_contract": "browser-rendered",
        "current_evidence_source": "https://www.ohb.de/en/career;https://career-ohb.csod.com/ux/ats/careersite/4/home?c=career-ohb&lang=en-GB",
        "evidence_confidence": "high", "validation_status": "browser_required",
        "exclusion_reason": "Official OHB link opens a Cornerstone browser shell; anonymous static requests do not expose a job collection.",
    },
    {
        "master_rank": 199, "company": "Mercado Libre", "prior_platform": "eightfold",
        "current_platform": "eightfold", "implementation_path": "existing_adapter_configuration_only",
        "adapter_family": "eightfold", "endpoint_variant": "pcsx",
        "official_careers_url": "https://careers-meli.mercadolibre.com/es/positions",
        "listing_url": "https://mercadolibre.eightfold.ai/careers",
        "api_url": "https://mercadolibre.eightfold.ai/api/pcsx/search", "request_method": "GET",
        "organization": "mercadolibre.com", "allowed_source_hosts": "careers-meli.mercadolibre.com;mercadolibre.eightfold.ai",
        "allowed_apply_hosts": "mercadolibre.eightfold.ai",
        "stable_id_contract": "displayJobId/atsJobId namespaced by Eightfold host",
        "apply_url_contract": "same-host HTTPS /careers/job/{numeric position ID}",
        "pagination_contract": "start offsets 0 and 10; repeated-page and no-new-ID termination",
        "current_evidence_source": "https://careers-meli.mercadolibre.com/es/positions;https://mercadolibre.eightfold.ai/api/pcsx/search",
        "evidence_confidence": "high",
    },
    {
        "master_rank": 218, "company": "Bolt", "prior_platform": "static_html",
        "current_platform": "nextjs_static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "server_rendered_uuid_cards",
        "official_careers_url": "https://bolt.eu/en/careers/", "listing_url": "https://bolt.eu/en/careers/positions/",
        "request_method": "GET", "allowed_source_hosts": "bolt.eu", "allowed_apply_hosts": "bolt.eu",
        "stable_id_contract": "UUID in official position path",
        "apply_url_contract": "same-host HTTPS position URL preserving ?tab=apply",
        "pagination_contract": "GET ?page=1 then ?page=2; stop at 20 normalized jobs",
        "current_evidence_source": "https://bolt.eu/en/careers/;https://bolt.eu/en/careers/positions/",
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
        name = review["company"]
        if name not in uncovered or name not in fingerprints:
            raise ValueError(f"{name}: missing v4 evidence")
        if uncovered[name].get("recoverability_class") != "adapter_extension_required":
            raise ValueError(f"{name}: not derived from the v4 adapter-extension class")
        if int(uncovered[name]["master_rank"]) != int(review["master_rank"]):
            raise ValueError(f"{name}: master rank differs from v4 evidence")
    if [int(item["master_rank"]) for item in REVIEWS] != sorted(int(item["master_rank"]) for item in REVIEWS):
        raise ValueError("Reviewed candidates do not follow master-workbook order")
    batch = next(row for row in _csv_rows(RECOVERY_BATCHES) if row["batch_id"] == "v4-02")
    batch_names = [value.strip() for value in batch["companies"].split(";")]
    if any(name not in batch_names for name in EXPECTED_COMPANIES):
        raise ValueError("Reviewed candidates are not all present in v4-02")
    for entry in CANDIDATE_ENTRIES:
        error = validator.configuration_error(entry)
        if error:
            raise ValueError(f"{entry['name']}: {error}")
        if not 1 <= int(entry.get("max_pages") or 0) <= MAX_PAGES:
            raise ValueError(f"{entry['name']}: invalid page bound")
        if int(entry.get("max_jobs") or 0) != MAX_JOBS:
            raise ValueError(f"{entry['name']}: invalid job bound")


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
        raise ValueError("Production is not the 133-entry v4a historical baseline")
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
    """Cache identical anonymous requests and enforce hard byte/page bounds."""

    def __init__(self, session: Optional[Any] = None) -> None:
        self.session = session or requests.Session()
        self.cache: dict[str, Any] = {}
        self.cache_hits = 0
        self.network_requests = 0
        self.unique_requests: list[str] = []
        self.responses: list[Any] = []

    @staticmethod
    def _key(method: str, url: str, kwargs: Mapping[str, Any]) -> str:
        material = {
            "method": method, "url": url, "params": kwargs.get("params"),
            "json": kwargs.get("json"), "data": kwargs.get("data"),
        }
        return json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))

    @staticmethod
    def _size(response: Any) -> int:
        content = getattr(response, "content", None)
        if content is not None:
            return len(content)
        return len(str(getattr(response, "text", "") or "").encode("utf-8"))

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.pop("cookies", None)
        key = self._key(method, url, kwargs)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        if key not in self.unique_requests:
            if len(self.unique_requests) >= MAX_UNIQUE_REQUESTS:
                raise AdapterError("v4a validator two-page request limit exceeded")
            self.unique_requests.append(key)
        self.network_requests += 1
        response = getattr(self.session, method)(url, **kwargs)
        if self._size(response) > MAX_RESPONSE_BYTES:
            raise AdapterError("v4a validator response byte limit exceeded")
        self.responses.append(response)
        if getattr(response, "status_code", None) == 200:
            self.cache[key] = response
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("post", url, **kwargs)


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _metadata(session: CachedBoundedSession, platform: str) -> Mapping[str, Any]:
    attribute = {
        "static_html": "static_html_pagination", "icims": "icims_pagination",
        "eightfold": "eightfold_pagination", "oracle_recruiting": "oracle_recruiting_pagination",
    }.get(platform)
    value = getattr(session, attribute, {}) if attribute else {}
    return value if isinstance(value, Mapping) else {}


def _valid_empty(session: CachedBoundedSession, platform: str) -> bool:
    metadata = _metadata(session, platform)
    if metadata.get("zero_job_contract") or metadata.get("stop_reason") == "validated_zero_jobs":
        return True
    if platform == "oracle_recruiting" and session.responses:
        try:
            payload = session.responses[-1].json()
            envelope = payload["items"][0]
            return envelope.get("TotalJobsCount") == 0 and envelope.get("requisitionList") == []
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            return False
    return False


def _status_for_error(exc: BaseException) -> str:
    message = str(exc).casefold()
    if any(
        code in message
        for code in (
            "unsupported_listing_contract",
            "html_too_large",
            "structured_payload_malformed",
            "unproven_empty_collection",
        )
    ):
        return "malformed_response"
    if "authentication" in message or "login" in message:
        return "authentication_required"
    if "session" in message:
        return "session_required"
    if "browser" in message or "javascript" in message:
        return "browser_required"
    if "blocked" in message or "access denied" in message or "http 403" in message or "http 451" in message:
        return "blocked"
    if "byte limit" in message:
        return "malformed_response"
    if "apply host" in message or "allowed-host" in message:
        return "invalid_apply_host"
    if "apply url" in message:
        return "invalid_apply_url"
    if "stable" in message or "identifier" in message:
        return "missing_stable_id"
    if "unsupported" in message:
        return "unsupported_variant"
    if "malformed" in message or "invalid json" in message or "must contain" in message:
        return "malformed_response"
    if isinstance(exc, requests.RequestException):
        return "network_inaccessible"
    return "failed_technical"


def _base_result(review: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company": review["company"], "master_rank": review["master_rank"],
        "prior_classification": "adapter_extension_required",
        "current_platform": review["current_platform"],
        "implementation_path": review["implementation_path"],
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
    review: Mapping[str, Any],
    *,
    duplicate_of: Optional[str] = None,
    duplicate_company_of: Optional[str] = None,
    timeout: int = 20,
    session_factory: Any = requests.Session,
) -> dict[str, Any]:
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
        warnings = list(metadata.get("warnings") or [])
        if diagnostics:
            warnings.append(f"production parser skipped {len(diagnostics)} unsupported records")
        result.update(
            pages_requested=len(session.unique_requests), jobs_observed=len(jobs),
            normalized_jobs_inspected=len(inspected), stable_id_result="valid" if jobs else "not_applicable_zero_jobs",
            apply_url_result="valid" if jobs else "not_applicable_zero_jobs",
            apply_host_result="valid" if jobs else "not_applicable_zero_jobs",
            pagination_result=stop_reason,
            repeated_page_or_cursor_result="terminated" if "repeated" in stop_reason else "not_detected",
            no_new_id_result="terminated" if "no_new" in stop_reason else "not_detected",
            valid_empty_result=valid_empty, authentication_check="not_required",
            session_check="not_required", browser_check="not_required",
            blocked_response_check="not_detected", malformed_response_check="accepted_valid_schema",
            status="validated_with_jobs" if jobs else "validated_zero_jobs", safe_to_activate=True,
            warnings=warnings,
            diagnostics={
                "endpoint": validator.endpoint_for(entry), "network_requests": session.network_requests,
                "request_cache_hits": session.cache_hits, "adapter_diagnostics": diagnostics,
            },
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
            diagnostics={
                "endpoint": validator.endpoint_for(entry), "network_requests": session.network_requests,
                "request_cache_hits": session.cache_hits, "error": str(exc),
            },
        )
    return result


def _excluded_results() -> list[dict[str, Any]]:
    results = []
    for review in REVIEWS:
        if _entry_for(str(review["company"])) is not None:
            continue
        result = _base_result(review)
        status = str(review["validation_status"])
        result.update(
            status=status,
            browser_check="rejected" if status == "browser_required" else "not_detected",
            warnings=[str(review["exclusion_reason"])],
            diagnostics={"error": review["exclusion_reason"]},
        )
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
        source_identity = validator.source_key(entry) if entry else ""
        rows.append({
            field: {
                "source_identity": source_identity or "",
                "prior_evidence_source": v4[name].get("prior_evidence_source", ""),
                "evidence_timestamp": now,
                "validation_status": status,
                "safe_to_activate": str(safe).lower(),
                "warnings": ";".join(str(value) for value in (result.get("warnings", []) if result else [])),
                "exclusion_reason": "" if safe else str(review.get("exclusion_reason") or (result or {}).get("diagnostics", {}).get("error") or ""),
            }.get(field, review.get(field, ""))
            for field in INVENTORY_FIELDS
        })
    return rows


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    from io import StringIO
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=INVENTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_inventory_rows(results))
    validator._write_bytes_if_changed(INVENTORY, buffer.getvalue().encode("utf-8"))


def validate(
    *,
    timeout: int = 20,
    batch_delay: float = 0.25,
    session_factory: Any = requests.Session,
    sleep: Any = time.sleep,
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
    validated_entries = load_target_companies(str(VALIDATED_REGISTRY))
    _write_inventory(results)
    counts = Counter(str(item["status"]) for item in results)
    totals = {
        "reviewed_candidates": len(results),
        "configuration_only_candidates": sum(item["implementation_path"] == "existing_adapter_configuration_only" for item in results),
        "adapter_extension_candidates": sum(item["implementation_path"] in {"existing_adapter_variant_extension", "reusable_static_html_variant"} for item in results),
        "reclassified_candidates": sum(item["prior_platform"] != item["current_platform"] for item in REVIEWS),
        "validated_with_jobs": counts["validated_with_jobs"],
        "validated_zero_jobs": counts["validated_zero_jobs"],
        "excluded": sum(not item["safe_to_activate"] for item in results),
        "temporary_failures": sum(counts[name] for name in TEMPORARY_STATUSES),
        "candidate_registry_count": len(candidate_entries),
        "validated_registry_count": len(validated_entries),
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
    if PRODUCTION.read_bytes() != production_before:
        raise RuntimeError("Production registry changed during validation")
    if PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Parent mappings changed during validation")
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
            payload: Mapping[str, Any] = {
                "candidate_registry": str(CANDIDATE_REGISTRY),
                "candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))),
                "bytes": len(content),
            }
        else:
            payload = validate(timeout=args.timeout, batch_delay=args.batch_delay)
    except (OSError, ValueError) as exc:
        print(f"Validation setup failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
