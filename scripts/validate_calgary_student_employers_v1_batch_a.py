"""Build and validate Calgary/UCalgary Student Employers v1 Batch A.

This is a read-only source investigation.  It appends candidates only to
milestone artifacts, runs every promotable source twice, and never changes the
production registry, parent mappings, student filters, persistence, or the
dashboard publisher.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import browser_rendered_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "calgary_student_employers_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = (
    ROOT / "config" / "target_companies.calgary_student_employers_v1_batch_a_candidates.yaml"
)
VALIDATED_REGISTRY = (
    ROOT / "config" / "target_companies.calgary_student_employers_v1_batch_a_validated.yaml"
)
REPORT = ROOT / "output" / "calgary_student_employers_v1_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 186
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 181
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Pason Systems", "Spartan Controls", "Garmin Canada", "AltaLink", "ENMAX",
    "ATCO", "AESO (Alberta Electric System Operator)", "TransAlta",
    "Cenovus Energy", "Imperial Oil", "ARC Resources", "Nutrien", "Stantec",
    "City of Calgary", "YYC Calgary Airport Authority",
]

MASTER_RANKS = {"Imperial Oil": 169, "Cenovus Energy": 175, "ARC Resources": 182}

OFFICIAL_CAREERS_URLS = {
    "Pason Systems": "https://pason.com/careers/",
    "Spartan Controls": "https://www.spartancontrols.com/careers/",
    "Garmin Canada": "https://www.garmin.com/en-CA/careers/job-listings/",
    "AltaLink": "https://www.altalink.ca/join-our-team/",
    "ENMAX": "https://www.enmax.com/careers",
    "ATCO": "https://www.atco.com/en-ca/about-us/careers.html",
    "AESO (Alberta Electric System Operator)": "https://www.aeso.ca/aeso/work-with-us/",
    "TransAlta": "https://transalta.com/careers/",
    "Cenovus Energy": "https://www.cenovus.com/Careers",
    "Imperial Oil": "https://www.imperialoil.ca/careers/careers",
    "ARC Resources": "https://www.arcresources.com/join-our-team/",
    "Nutrien": "https://www.nutrien.com/careers",
    "Stantec": "https://www.stantec.com/en/careers",
    "City of Calgary": "https://www.calgary.ca/careers.html",
    "YYC Calgary Airport Authority": "https://www.yyc.com/careers",
}

CALGARY_RELEVANCE = {
    "Pason Systems": "Calgary-headquartered drilling technology employer with local software, electronics, QA, data, and engineering internships.",
    "Spartan Controls": "Calgary-headquartered industrial automation and process-control employer serving Alberta energy and industry.",
    "Garmin Canada": "Cochrane engineering office, about 15 minutes from Calgary, develops embedded and wireless products.",
    "AltaLink": "Calgary-headquartered Alberta electricity-transmission utility with engineering and grid roles.",
    "ENMAX": "Calgary-headquartered electric and gas utility with engineering, controls, grid, IT, co-op, and summer programs.",
    "ATCO": "Calgary-headquartered utilities and infrastructure group with Alberta engineering, electrical, and automation work.",
    "AESO (Alberta Electric System Operator)": "Calgary-based Alberta grid operator with power-system, markets, software, and engineering roles.",
    "TransAlta": "Calgary-headquartered power producer with Alberta generation, electrical, controls, and student roles.",
    "Cenovus Energy": "Calgary-headquartered energy producer with Alberta engineering, software, data, and student programs.",
    "Imperial Oil": "Calgary corporate presence and Alberta operations with engineering, controls, EIT, intern, and co-op pathways.",
    "ARC Resources": "Calgary-headquartered energy producer that recruits engineering students and new graduates.",
    "Nutrien": "Saskatchewan-headquartered employer with major Alberta operations and established internship/co-op programs.",
    "Stantec": "Large Calgary office with electrical, instrumentation, controls, automation, software, and engineering roles.",
    "City of Calgary": "Local municipal employer with engineering, IT, infrastructure, and summer-student programs.",
    "YYC Calgary Airport Authority": "Calgary airport operator with local engineering, technology, operations, and co-op roles.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Pason Systems": "https://pason.com/careers/interns-and-students/",
    "Spartan Controls": "https://www.spartancontrols.com/careers/",
    "Garmin Canada": "https://careers.garmin.com/students",
    "AltaLink": "https://www.altalink.ca/join-our-team/",
    "ENMAX": "https://www.enmax.com/careers",
    "ATCO": "https://www.atco.com/en-ca/about-us/careers.html",
    "AESO (Alberta Electric System Operator)": "https://www.aeso.ca/aeso/work-with-us/",
    "TransAlta": "https://jobs.transalta.com/go/New-Graduates-and-Students/1556800/",
    "Cenovus Energy": "https://www.cenovus.com/Careers/Students-and-new-grads",
    "Imperial Oil": "https://www.imperialoil.ca/en-CA/careers/careers/intern-co-op-and-apprenticeship-opportunities",
    "ARC Resources": "https://www.arcresources.com/join-our-team/",
    "Nutrien": "https://www.nutrien.com/careers/students-and-new-grads",
    "Stantec": "https://www.stantec.com/en/careers/students-graduates",
    "City of Calgary": "https://www.calgary.ca/careers/summer-students.html",
    "YYC Calgary Airport Authority": "https://www.yyc.com/careers",
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS.get(name),
        "max_pages": 1, "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    value.update(fields)
    return value


def _browser(name: str, careers_url: str, listing_url: str, *, wait_selector: str,
             listing_selector: str, job_card_selector: str, title_selector: str,
             location_selector: Optional[str], identity_regex: str,
             external_id_regex: str, allowed_apply_hosts: list[str],
             empty_selector: Optional[str] = None) -> dict[str, Any]:
    return _entry(
        name, "browser_rendered_html", careers_url,
        identity_regex=identity_regex, allowed_apply_hosts=allowed_apply_hosts,
        listing_url=listing_url, endpoint_variant="browser_rendered_html_v1",
        browser_wait_selector=wait_selector, browser_scroll_steps=0,
        browser_static_variant="server_rendered_links",
        browser_identity_selector="title", browser_identity_regex=identity_regex,
        listing_selector=listing_selector, job_card_selector=job_card_selector,
        title_selector=title_selector, location_selector=location_selector,
        detail_link_selector=":self", external_id_regex=external_id_regex,
        empty_selector=empty_selector,
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _entry(
        "Spartan Controls", "sap_successfactors", OFFICIAL_CAREERS_URLS["Spartan Controls"],
        identity_regex=r"Spartan Controls", allowed_apply_hosts=["careers.spartancontrols.com"],
        identity_url="https://careers.spartancontrols.com/search/",
        search_url="https://careers.spartancontrols.com/search/", endpoint_variant="rmk_table",
    ),
    _entry(
        "Garmin Canada", "icims", OFFICIAL_CAREERS_URLS["Garmin Canada"],
        identity_regex=r"Garmin Canada", allowed_apply_hosts=["canada-garmin.icims.com"],
        identity_url=("https://careers.garmin.com/api/jobs?location=Cochrane%2C%20Alberta"
                      "&stretch=25&stretchUnit=MILES&page=1"),
        endpoint_url=("https://careers.garmin.com/api/jobs?location=Cochrane%2C%20Alberta"
                      "&stretch=25&stretchUnit=MILES&sortBy=relevance&descending=false&internal=false"),
        endpoint_variant="icims_jibe_api", tenant="garmin",
        board_id="garmin-canada-cochrane", page_size=20,
    ),
    _entry(
        "ENMAX", "workday", OFFICIAL_CAREERS_URLS["ENMAX"], identity_regex=r"ENMAX",
        allowed_apply_hosts=["enmax.wd3.myworkdayjobs.com"],
        identity_url="https://enmax.wd3.myworkdayjobs.com/ENMAXCareers/jobs",
        tenant="enmax", wd_subdomain="wd3", site_id="ENMAXCareers",
        search_url="https://enmax.wd3.myworkdayjobs.com/ENMAXCareers/jobs",
    ),
    _entry(
        "ATCO", "phenom", "https://careers.atco.com/global/en", identity_regex=r"ATCO",
        allowed_apply_hosts=["eezy.fa.ca2.oraclecloud.com"],
        identity_url="https://careers.atco.com/global/en/search-results",
        search_url="https://careers.atco.com/global/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="AGZAGAGLOBAL",
        locale="en_global", max_pages=2,
    ),
    _entry(
        "AESO (Alberta Electric System Operator)", "workday",
        OFFICIAL_CAREERS_URLS["AESO (Alberta Electric System Operator)"],
        identity_regex=r"AESO|Alberta Electric System Operator",
        allowed_apply_hosts=["aeso.wd3.myworkdayjobs.com"],
        identity_url="https://aeso.wd3.myworkdayjobs.com/External",
        tenant="aeso", wd_subdomain="wd3", site_id="External",
        search_url="https://aeso.wd3.myworkdayjobs.com/External",
    ),
    _entry(
        "TransAlta", "sap_successfactors", OFFICIAL_CAREERS_URLS["TransAlta"],
        identity_regex=r"TransAlta", allowed_apply_hosts=["jobs.transalta.com"],
        identity_url="https://jobs.transalta.com/search/",
        search_url="https://jobs.transalta.com/search/", endpoint_variant="rmk_table",
    ),
    _entry(
        "Cenovus Energy", "workday", OFFICIAL_CAREERS_URLS["Cenovus Energy"],
        identity_regex=r"Cenovus", allowed_apply_hosts=["cenovus.wd3.myworkdayjobs.com"],
        identity_url="https://cenovus.wd3.myworkdayjobs.com/careers",
        tenant="cenovus", wd_subdomain="wd3", site_id="careers",
        search_url="https://cenovus.wd3.myworkdayjobs.com/careers",
    ),
    _entry(
        "Imperial Oil", "static_html", OFFICIAL_CAREERS_URLS["Imperial Oil"],
        identity_regex=r"Jobs in Canada \(Imperial\)|Imperial", allowed_apply_hosts=["jobs.exxonmobil.com"],
        identity_url="https://jobs.exxonmobil.com/Imperial/go/Jobs-in-Canada-%28Imperial%29/3271200/",
        listing_url="https://jobs.exxonmobil.com/Imperial/go/Jobs-in-Canada-%28Imperial%29/3271200/",
        endpoint_variant="server_rendered_cards", listing_selector="#searchresults",
        job_card_selector="tr.data-row", title_selector="a.jobTitle-link",
        location_selector="td.colLocation span.jobLocation",
        department_selector="span.jobDepartment", date_selector="td.colDate span.jobDate",
        detail_link_selector="a.jobTitle-link", external_id_regex=r"/([0-9]+)/?$",
    ),
    _entry(
        "ARC Resources", "sap_successfactors", OFFICIAL_CAREERS_URLS["ARC Resources"],
        identity_regex=r"ARC Resources", allowed_apply_hosts=["careers.arcresources.com"],
        identity_url="https://careers.arcresources.com/search/",
        search_url="https://careers.arcresources.com/search/", endpoint_variant="rmk_table",
    ),
    _browser(
        "Nutrien", OFFICIAL_CAREERS_URLS["Nutrien"],
        "https://jobs.nutrien.com/North-America/search/",
        wait_selector='[data-testid="headerTitle"]',
        listing_selector='[data-testid="headerTitle"]', job_card_selector='a[href*="/job/"]',
        title_selector=":self", location_selector=None, identity_regex=r"Nutrien Jobs",
        external_id_regex=r"/([0-9]+)/?$", allowed_apply_hosts=["jobs.nutrien.com"],
        empty_selector='[data-testid="headerTitle"]',
    ),
    _browser(
        "Stantec", OFFICIAL_CAREERS_URLS["Stantec"],
        "https://stantec.jobs/jobs/?q=Calgary", wait_selector='a[href*="/job/"]',
        listing_selector="main", job_card_selector='a[href*="/job/"]', title_selector="h2",
        location_selector="p:nth-child(3)", identity_regex=r"Stantec",
        external_id_regex=r"/([0-9A-F]{32})/job/", allowed_apply_hosts=["stantec.jobs"],
    ),
    _entry(
        "YYC Calgary Airport Authority", "static_html",
        OFFICIAL_CAREERS_URLS["YYC Calgary Airport Authority"],
        identity_regex=r"Calgary Airport Authority|YYC", allowed_apply_hosts=["yyc.careers"],
        identity_url="https://yyc.careers/Opportunities",
        listing_url="https://yyc.careers/Opportunities", endpoint_variant="server_rendered_links",
        listing_selector="body", job_card_selector="a[data-job-id]", title_selector=":self",
        detail_link_selector=":self", external_id_regex=r"[?&]JobId=([0-9]+)",
    ),
]


EXCLUDED_REVIEWS = [
    {
        "company": "Pason Systems", "platform": "UKG/UltiPro",
        "adapter": "none", "listing_url": ("https://recruiting.ultipro.ca/PAS5000PASON/"
            "JobBoard/736c1025-c469-4ece-a487-4884545272a7/"),
        "identity_regex": r"Pason", "classification": "mixed_or_ambiguous_identity",
        "exclusion_reason": ("The live official board is branded 'Pason & IWS Career Opportunities' and includes "
            "IWS jobs. The existing declarative adapter cannot prove exact employer attribution per card."),
    },
    {
        "company": "AltaLink", "platform": "Workable", "adapter": "none",
        "listing_url": "https://apply.workable.com/altalink/", "identity_regex": r"AltaLink",
        "classification": "blocked",
        "exclusion_reason": ("The official Workable source exposes an unsupported public API and its application "
            "flow advertises a Turnstile anti-bot dependency. No CAPTCHA-dependent source is promoted."),
    },
    {
        "company": "City of Calgary", "platform": "Oracle PeopleSoft", "adapter": "none",
        "listing_url": "https://www.calgary.ca/careers.html", "identity_regex": r"City of Calgary|Careers",
        "classification": "session_required",
        "exclusion_reason": ("Public listing links redirect into a PeopleSoft HCM cookie/login session instead of "
            "remaining anonymous, stable, job-specific official application URLs."),
    },
]

PLATFORM_NOTES = {
    "Pason Systems": ("UKG/UltiPro", "none"),
    "Spartan Controls": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "Garmin Canada": ("iCIMS Jibe", "icims"), "AltaLink": ("Workable", "none"),
    "ENMAX": ("Workday", "workday"), "ATCO": ("Phenom + Oracle Recruiting", "phenom"),
    "AESO (Alberta Electric System Operator)": ("Workday", "workday"),
    "TransAlta": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "Cenovus Energy": ("Workday", "workday"),
    "Imperial Oil": ("SAP SuccessFactors Recruiting Marketing", "static_html"),
    "ARC Resources": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "Nutrien": ("SAP SuccessFactors unified search", "browser_rendered_html"),
    "Stantec": ("Jobsyn/NLX client search", "browser_rendered_html"),
    "City of Calgary": ("Oracle PeopleSoft", "none"),
    "YYC Calgary Airport Authority": ("Custom YYC careers + Dayforce", "static_html"),
}

STUDENT_ROLE_RE = re.compile(
    r"\b(?:intern(?:ship)?s?|students?|undergraduates?)\b|\bco\s*[-/]?\s*op\b|"
    r"\bsummer\s+student\b|\bstudent\s+engineer\b",
    re.IGNORECASE,
)

CSV_FIELDS = [
    "company", "official_careers_url", "listing_url", "calgary_alberta_relevance",
    "platform", "adapter", "result", "jobs_normalized", "current_student_intern_hits",
    "current_student_role_evidence", "student_program_evidence_url", "exclusion_reason",
    "original_246_master", "master_rank", "validation_status", "stable_id_result",
    "apply_url_result", "allowed_apply_hosts", "job_count_run_1", "job_count_run_2",
]


def source_key(entry: Mapping[str, Any]) -> Optional[str]:
    if entry.get("platform") != "browser_rendered_html":
        return registry_validator.source_key(entry)
    parsed = urlparse(str(entry.get("listing_url") or ""))
    return f"browser_rendered_html:{(parsed.hostname or '').casefold()}:{parsed.path.rstrip('/').casefold()}"


def _name_key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("name") or "").casefold().split())


def _duplicates(baseline: Iterable[Mapping[str, Any]], additions: Iterable[Mapping[str, Any]]) -> tuple[dict[int, str], dict[int, str]]:
    names = {_name_key(item): str(item["name"]) for item in baseline}
    sources = {source_key(item): str(item["name"]) for item in baseline if source_key(item)}
    duplicate_names: dict[int, str] = {}
    duplicate_sources: dict[int, str] = {}
    for index, item in enumerate(additions):
        name, source = _name_key(item), source_key(item)
        if name in names:
            duplicate_names[index] = names[name]
        else:
            names[name] = str(item["name"])
        if source and source in sources:
            duplicate_sources[index] = sources[source]
        elif source:
            sources[source] = str(item["name"])
    return duplicate_names, duplicate_sources


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def _registry_bytes(production: Path, additions: Iterable[Mapping[str, Any]]) -> bytes:
    return registry_validator.build_candidate_registry_bytes(production, list(additions))


def reconcile() -> list[dict[str, Any]]:
    _, production = registry_validator.load_registry(PRODUCTION)
    mappings = load_parent_source_mappings(PARENT_MAPPINGS)
    if len(production) != HISTORICAL_PRODUCTION_COUNT:
        raise ValueError("Production registry is not the exact 186-source baseline")
    if len(mappings) != HISTORICAL_PARENT_MAPPINGS:
        raise ValueError("Parent mappings are not the exact four-mapping baseline")
    reviewed = [item["name"] for item in CANDIDATE_ENTRIES] + [item["company"] for item in EXCLUDED_REVIEWS]
    if sorted(reviewed, key=REVIEWED_COMPANIES.index) != REVIEWED_COMPANIES:
        raise ValueError("Batch A must review exactly the requested 15 companies")
    names, sources = _duplicates(production, CANDIDATE_ENTRIES)
    if names:
        raise ValueError(f"duplicate_company: {names}")
    if sources:
        raise ValueError(f"duplicate_source: {sources}")
    master = reconcile_master(load_master_workbook(MASTER_WORKBOOK), load_target_companies(str(PRODUCTION)), mappings)
    covered_count = len(master.canonical_names) + len(master.mapped_names)
    if (master.total, covered_count) != (MASTER_COMPANY_COUNT, HISTORICAL_ORIGINAL_COVERAGE):
        raise ValueError("Master reconciliation is not the exact 181/246 baseline")
    master_names = {str(row["company"]) for row in master.rows}
    actual = {name for name in REVIEWED_COMPANIES if name in master_names}
    if actual != set(MASTER_RANKS):
        raise ValueError(f"Requested-company master membership changed: {sorted(actual)}")
    for item in CANDIDATE_ENTRIES:
        if item["max_pages"] > MAX_PAGES or item["max_jobs"] > MAX_JOBS:
            raise ValueError(f"{item['name']}: candidate exceeds page or job limits")
        target = CompanyTarget.from_dict(item)
        if item["platform"] == "browser_rendered_html":
            browser_rendered_html.validate_target(target)
        elif item["platform"] not in scanner.ADAPTERS:
            raise ValueError(f"{item['name']}: unsupported production adapter")
    return production


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY,
                             additions: Optional[Iterable[Mapping[str, Any]]] = None) -> bytes:
    chosen = list(CANDIDATE_ENTRIES if additions is None else additions)
    _, baseline = registry_validator.load_registry(production)
    names, sources = _duplicates(baseline, chosen)
    if names:
        raise ValueError(f"duplicate_company: {names}")
    if sources:
        raise ValueError(f"duplicate_source: {sources}")
    content = _registry_bytes(production, chosen)
    if not content.startswith(production.read_bytes()):
        raise ValueError("Candidate registry lost the exact production byte prefix")
    _write_bytes(destination, content)
    return content


def _identity_check(entry: Mapping[str, Any], session: requests.Session, timeout: int) -> dict[str, Any]:
    requested = str(entry.get("identity_url") or entry["careers_url"])
    response = session.get(requested, headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"}, timeout=timeout)
    if response.status_code in {401, 407}:
        raise AdapterError("Official identity source requires authentication")
    if response.status_code in {403, 429, 451}:
        raise AdapterError("Official identity source is blocked")
    if response.status_code != 200 or not response.content or len(response.content) > MAX_RENDERED_HTML_BYTES:
        raise AdapterError(f"Official identity source HTTP {response.status_code} or malformed content")
    expected_host = (urlparse(requested).hostname or "").casefold()
    actual_host = (urlparse(response.url).hostname or "").casefold()
    if actual_host != expected_host:
        raise AdapterError("Official identity source redirected to an unreviewed host")
    text = response.text
    if not re.search(str(entry["source_identity_regex"]), text, re.IGNORECASE):
        raise AdapterError("Official careers page has missing, mixed, or ambiguous company identity")
    if entry["platform"] == "workday":
        tenant = re.escape(str(entry["tenant"])); site = re.escape(str(entry["site_id"]))
        if not re.search(rf'tenant\s*:\s*["\']{tenant}["\']', text, re.IGNORECASE):
            raise AdapterError("Official Workday page has missing exact tenant identity")
        if not re.search(rf'siteId\s*:\s*["\']{site}["\']', text, re.IGNORECASE):
            raise AdapterError("Official Workday page has missing exact site identity")
    return {"status": 200, "url": response.url, "bytes": len(response.content)}


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _verify_jobs(jobs: Iterable[Any], entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    inspected = list(jobs)[:MAX_JOBS]
    ids = [str(job.job_id) for job in inspected]
    urls = [str(job.url) for job in inspected]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise AdapterError("Candidate has missing or duplicate stable IDs")
    if any(not value for value in urls) or len(urls) != len(set(urls)):
        raise AdapterError("Candidate apply URLs are missing or not job-specific")
    allowed = [str(value).casefold() for value in entry.get("allowed_apply_hosts") or []]
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not _allowed_host((parsed.hostname or "").casefold(), allowed):
            raise AdapterError("Candidate has an unofficial apply URL")
    return sorted(zip(ids, urls), key=lambda item: item[0])


def _pagination(session: requests.Session, platform: str) -> Mapping[str, Any]:
    attributes = {
        "sap_successfactors": "sap_successfactors_pagination", "icims": "icims_pagination",
        "phenom": "phenom_pagination", "static_html": "static_html_pagination",
    }
    return getattr(session, attributes.get(platform, ""), {}) or {}


def _student_hits(jobs: Iterable[Any]) -> list[dict[str, str]]:
    hits = []
    for job in list(jobs)[:MAX_JOBS]:
        # Role detection is deliberately title-based. Generic benefit copy such as
        # "student loan repayment" must not turn an experienced role into a hit.
        text = str(job.title or "")
        if STUDENT_ROLE_RE.search(text):
            hits.append({"title": job.title, "location": job.location, "url": job.url})
    return hits


def _run_direct(entry: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    started = time.monotonic(); session = requests.Session()
    try:
        identity = _identity_check(entry, session, timeout)
        jobs = scanner.ADAPTERS[str(entry["platform"])](session, CompanyTarget.from_dict(dict(entry)), timeout)
        signature = _verify_jobs(jobs, entry)
        pagination = dict(_pagination(session, str(entry["platform"])))
        zero = not signature and bool(pagination.get("zero_job_contract") or pagination.get("stop_reason") == "validated_zero_jobs")
        if not signature and not zero:
            raise AdapterError("Candidate did not conclusively prove a zero-job collection")
        return {
            "runtime_seconds": round(time.monotonic() - started, 3), "success": True,
            "jobs_observed": len(jobs), "jobs_normalized": len(signature),
            "stable_ids": [item[0] for item in signature], "apply_urls": [item[1] for item in signature],
            "student_hits": _student_hits(jobs), "zero_job_contract": zero,
            "pages_requested": min(int(entry["max_pages"]), MAX_PAGES), "identity": identity,
            "pagination_stop_reason": pagination.get("stop_reason", "adapter_bounded_collection"),
        }
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        return {"runtime_seconds": round(time.monotonic() - started, 3), "success": False,
                "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [], "apply_urls": [],
                "student_hits": [], "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
                "status": _status_for_error(exc), "error": str(exc)}
    finally:
        session.close()


def _run_browser(entry: Mapping[str, Any], batch: browser_rendered_html.BrowserBatch, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        jobs = batch.fetch_jobs(CompanyTarget.from_dict(dict(entry)), timeout)
        signature = _verify_jobs(jobs, entry)
        diagnostics = dict(batch.last_run); parser = diagnostics.get("static_parser") or {}
        zero = not signature and bool(parser.get("zero_job_contract") or parser.get("stop_reason") == "validated_zero_jobs")
        if not signature and not zero:
            raise AdapterError("Browser candidate did not conclusively prove a zero-job collection")
        return {
            "runtime_seconds": round(time.monotonic() - started, 3), "success": True,
            "jobs_observed": len(jobs), "jobs_normalized": len(signature),
            "stable_ids": [item[0] for item in signature], "apply_urls": [item[1] for item in signature],
            "student_hits": _student_hits(jobs), "zero_job_contract": zero,
            "pages_requested": diagnostics.get("pages_rendered", 0),
            "rendered_bytes": diagnostics.get("rendered_bytes", 0),
            "rendered_urls": diagnostics.get("rendered_urls", []),
            "pagination_stop_reason": parser.get("stop_reason"),
        }
    except (AdapterError, ValueError, TypeError) as exc:
        return {"runtime_seconds": round(time.monotonic() - started, 3), "success": False,
                "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [], "apply_urls": [],
                "student_hits": [], "pages_requested": 0, "status": _status_for_error(exc), "error": str(exc)}


def _status_for_error(exc: BaseException) -> str:
    message = str(exc).casefold()
    if "duplicate" in message and "id" in message: return "unstable_ids"
    if "apply" in message or "allowed-host" in message: return "unofficial_apply_url"
    if "identity" in message: return "mixed_or_ambiguous_identity"
    if "authentication" in message or "login" in message: return "authentication_required"
    if "session" in message: return "session_required"
    if "captcha" in message or "blocked" in message or "consent" in message: return "blocked"
    if "timeout" in message or "wait selector" in message: return "timeout_or_wait_selector_missing"
    if "malformed" in message or "byte" in message or "limit" in message: return "malformed_or_limit_failure"
    return "failed_technical"


def _result_base(company: str) -> dict[str, Any]:
    platform, adapter = PLATFORM_NOTES[company]
    return {
        "company": company, "official_careers_url": OFFICIAL_CAREERS_URLS[company],
        "calgary_alberta_relevance": CALGARY_RELEVANCE[company], "platform": platform,
        "adapter": adapter, "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[company],
        "original_246_master": company in MASTER_RANKS, "master_rank": MASTER_RANKS.get(company),
    }


def _finalize(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    result = _result_base(str(entry["name"])); result["runs"] = runs
    failures = [run for run in runs if not run.get("success")]
    if failures:
        failure = failures[0]
        result.update(result="excluded", safe_to_activate=False, jobs_normalized=0,
                      current_student_intern_hits=0, current_student_role_evidence=[],
                      validation_status=failure.get("status", "failed_technical"),
                      stable_id_result="not_validated", apply_url_result="not_validated",
                      exclusion_reason=failure.get("error", "validation failed"))
        return result
    signatures = [list(zip(run["stable_ids"], run["apply_urls"])) for run in runs]
    if len(signatures) != 2 or signatures[0] != signatures[1]:
        result.update(result="excluded", safe_to_activate=False, jobs_normalized=0,
                      current_student_intern_hits=0, current_student_role_evidence=[],
                      validation_status="non_deterministic", stable_id_result="not_repeatable",
                      apply_url_result="not_repeatable", exclusion_reason="Repeated runs produced different stable IDs or apply URLs")
        return result
    zero = not signatures[0]
    if zero and not all(run.get("zero_job_contract") for run in runs):
        result.update(result="excluded", safe_to_activate=False, jobs_normalized=0,
                      current_student_intern_hits=0, current_student_role_evidence=[],
                      validation_status="unproven_zero_collection", stable_id_result="not_observable",
                      apply_url_result="not_observable", exclusion_reason="Empty source did not prove a repeatable zero-job contract")
        return result
    hits = runs[0].get("student_hits") or []
    result.update(
        result="validated", safe_to_activate=True, jobs_normalized=len(signatures[0]),
        current_student_intern_hits=len(hits), current_student_role_evidence=hits,
        validation_status="validated_zero_jobs" if zero else "validated_with_jobs",
        stable_id_result="not_observable_current_zero_collection" if zero else "stable_across_two_runs",
        apply_url_result="not_observable_current_zero_collection" if zero else "stable_official_and_job_specific_across_two_runs",
        exclusion_reason="",
    )
    return result


def _probe_exclusion(review: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    runs = []
    for number in (1, 2):
        started = time.monotonic()
        try:
            response = requests.get(OFFICIAL_CAREERS_URLS[str(review["company"])], timeout=timeout,
                                    headers={"Accept": "text/html", "User-Agent": "job-radar/1.0"})
            content = response.text
            success = response.status_code == 200 and bool(re.search(str(review["identity_regex"]), content, re.IGNORECASE))
            runs.append({"run": number, "runtime_seconds": round(time.monotonic() - started, 3),
                         "success": success, "http_status": response.status_code,
                         "jobs_observed": 0, "jobs_normalized": 0, "pages_requested": 1,
                         "stable_ids": [], "apply_urls": [], "student_hits": []})
        except requests.RequestException as exc:
            runs.append({"run": number, "runtime_seconds": round(time.monotonic() - started, 3),
                         "success": False, "error": str(exc), "jobs_observed": 0,
                         "jobs_normalized": 0, "pages_requested": 1, "stable_ids": [],
                         "apply_urls": [], "student_hits": []})
    result = _result_base(str(review["company"]))
    result.update(result="excluded", safe_to_activate=False, jobs_normalized=0,
                  current_student_intern_hits=0, current_student_role_evidence=[],
                  validation_status=review["classification"], stable_id_result="not_validated",
                  apply_url_result="not_validated", exclusion_reason=review["exclusion_reason"], runs=runs)
    return result


def _entry_for(name: str) -> Optional[dict[str, Any]]:
    return next((item for item in CANDIDATE_ENTRIES if item["name"] == name), None)


def _review_for(name: str) -> Optional[dict[str, Any]]:
    return next((item for item in EXCLUDED_REVIEWS if item["company"] == name), None)


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    by_name = {str(item["company"]): item for item in (results or [])}
    rows = []
    for name in REVIEWED_COMPANIES:
        item, review, result = _entry_for(name), _review_for(name), by_name.get(name, {})
        source = item or review or {}; runs = list(result.get("runs") or [])
        rows.append({
            "company": name, "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "calgary_alberta_relevance": CALGARY_RELEVANCE[name], "platform": PLATFORM_NOTES[name][0],
            "adapter": PLATFORM_NOTES[name][1], "result": result.get("result", "pending"),
            "jobs_normalized": result.get("jobs_normalized", ""),
            "current_student_intern_hits": result.get("current_student_intern_hits", ""),
            "current_student_role_evidence": "; ".join(hit["title"] for hit in result.get("current_student_role_evidence") or []),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name],
            "exclusion_reason": result.get("exclusion_reason") or (review or {}).get("exclusion_reason", ""),
            "original_246_master": str(name in MASTER_RANKS).lower(), "master_rank": MASTER_RANKS.get(name, ""),
            "validation_status": result.get("validation_status", "pending_validation"),
            "stable_id_result": result.get("stable_id_result", "pending_validation"),
            "apply_url_result": result.get("apply_url_result", "pending_validation"),
            "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
            "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
            "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
        })
    buffer = StringIO(newline=""); writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    _write_bytes(CANDIDATE_CSV, buffer.getvalue().encode("utf-8"))


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    production_before = PRODUCTION.read_bytes(); mappings_before = PARENT_MAPPINGS.read_bytes()
    baseline = reconcile(); candidate_bytes = build_candidate_registry(); results_by_name = {}
    for entry in CANDIDATE_ENTRIES:
        if entry["platform"] == "browser_rendered_html": continue
        runs = []
        for number in (1, 2):
            run = _run_direct(entry, timeout); run["run"] = number; runs.append(run)
        results_by_name[str(entry["name"])] = _finalize(entry, runs)
    browser_entries = [item for item in CANDIDATE_ENTRIES if item["platform"] == "browser_rendered_html"]
    if browser_entries:
        with browser_rendered_html.BrowserBatch(timeout=timeout) as batch:
            for entry in browser_entries:
                runs = []
                for number in (1, 2):
                    run = _run_browser(entry, batch, timeout); run["run"] = number; runs.append(run)
                results_by_name[str(entry["name"])] = _finalize(entry, runs)
    for review in EXCLUDED_REVIEWS:
        results_by_name[str(review["company"])] = _probe_exclusion(review, timeout)
    results = [results_by_name[name] for name in REVIEWED_COMPANIES]
    validated_names = {item["company"] for item in results if item["safe_to_activate"]}
    validated_additions = [item for item in CANDIDATE_ENTRIES if item["name"] in validated_names]
    validated_bytes = _registry_bytes(PRODUCTION, validated_additions)
    if not validated_bytes.startswith(production_before):
        raise RuntimeError("Validated registry lost the exact production byte prefix")
    _write_bytes(VALIDATED_REGISTRY, validated_bytes); _write_inventory(results)
    master_recovered = [name for name in REVIEWED_COMPANIES if name in MASTER_RANKS and name in validated_names]
    outside_master = [name for name in REVIEWED_COMPANIES if name not in MASTER_RANKS and name in validated_names]
    counts = Counter(str(item["validation_status"]) for item in results)
    payload = {
        "evidence_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "production_registry_sha256": hashlib.sha256(production_before).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mappings_before).hexdigest().upper(),
        "reviewed_companies": REVIEWED_COMPANIES,
        "candidate_registry_has_exact_186_entry_prefix": candidate_bytes.startswith(production_before),
        "validated_registry_has_exact_186_entry_prefix": validated_bytes.startswith(production_before),
        "limits": {"maximum_pages_per_source": MAX_PAGES, "maximum_normalized_jobs_per_source": MAX_JOBS,
                   "repeat_validation_runs": 2, "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
                   "maximum_rendered_html_bytes": MAX_RENDERED_HTML_BYTES},
        "totals": {
            "production_sources": len(baseline), "parent_mappings": HISTORICAL_PARENT_MAPPINGS,
            "reviewed": len(results), "candidate_sources": len(CANDIDATE_ENTRIES),
            "validated": len(validated_additions), "excluded": len(results) - len(validated_additions),
            "additional_canonical_production_sources_projected": len(validated_additions),
            "projected_production_sources": len(baseline) + len(validated_additions),
            "original_master_companies_recovered": len(master_recovered),
            "original_master_companies_recovered_names": master_recovered,
            "projected_original_master_coverage": HISTORICAL_ORIGINAL_COVERAGE + len(master_recovered),
            "projected_original_master_total": MASTER_COMPANY_COUNT,
            "calgary_focused_validated_outside_master": len(outside_master),
            "calgary_focused_validated_outside_master_names": outside_master,
            "statuses": dict(sorted(counts.items())),
        },
        "results": results,
        "publication": {"production_activated": False, "parent_mappings_changed": False,
                        "student_filters_changed": False, "live_publication": False,
                        "dashboard_write": False, "supabase": False, "google_sheets": False,
                        "sqlite": False},
    }
    _write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    if PRODUCTION.read_bytes() != production_before or PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Production registry or parent mappings changed during validation")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.build_only:
            reconcile(); candidate = build_candidate_registry()
            validated = _registry_bytes(PRODUCTION, []); _write_bytes(VALIDATED_REGISTRY, validated)
            _write_inventory()
            payload: dict[str, Any] = {"candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))),
                                      "validated_registry_count": len(load_target_companies(str(VALIDATED_REGISTRY))),
                                      "candidate_bytes": len(candidate)}
        else:
            payload = validate(args.timeout)
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Calgary student employers v1 Batch A validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
