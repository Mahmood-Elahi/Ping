"""Build and validate Ontario/Canada Expansion v1 Batch A.

The validator starts from the exact 274-source production registry, performs
two bounded anonymous reads for every promotable source, and writes only
candidate, validated, inventory, and ignored report artifacts.  It never
activates sources, changes mappings, publishes jobs, or writes persistence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_calgary_student_employers_v1_batch_a as engine
from src.scraper.company_careers import scanner
from src.scraper.company_careers.models import AdapterError, CompanyTarget


_ENGINE_BUILD_CANDIDATE_REGISTRY = engine.build_candidate_registry
_ENGINE_FINALIZE = engine._finalize

PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_ontario_canada_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.ontario_canada_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.ontario_canada_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "ontario_canada_expansion_v1_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 274
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 196
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "RBC", "TD Bank", "Scotiabank", "BMO", "CIBC", "Wealthsimple",
    "Thomson Reuters", "Manulife", "Sun Life", "Ciena", "Nokia Canada",
    "Ericsson Canada", "Ross Video", "Xanadu", "ApplyBoard",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "RBC": "https://jobs.rbc.com/ca/en",
    "TD Bank": "https://careers.td.com/",
    "Scotiabank": "https://www.scotiabank.com/careers/en/careers.html",
    "BMO": "https://jobs.bmo.com/ca/en",
    "CIBC": "https://www.cibc.com/en/about-cibc/careers.html",
    "Wealthsimple": "https://www.wealthsimple.com/en-ca/careers",
    "Thomson Reuters": "https://www.thomsonreuters.com/en/careers",
    "Manulife": "https://careers.manulife.com/global/en",
    "Sun Life": "https://www.sunlife.ca/en/careers/",
    "Ciena": "https://www.ciena.com/careers",
    "Nokia Canada": "https://www.nokia.com/careers/our-locations/canada/",
    "Ericsson Canada": "https://www.ericsson.com/en/careers/global-locations/canada",
    "Ross Video": "https://www.rossvideo.com/careers",
    "Xanadu": "https://www.xanadu.ai/careers/",
    "ApplyBoard": "https://www.applyboard.com/careers/",
}

CANADA_RELEVANCE = {
    "RBC": "Canadian bank source with Toronto/GTA, Waterloo, Ottawa, Ontario, Canada-remote, technology, analytics, research, and internship roles.",
    "TD Bank": "Canadian bank source with Toronto/GTA and Ontario technology, data, analytics, co-op, and internship hiring.",
    "Scotiabank": "Canadian bank source with Toronto-based technology, digital banking, data, Velocity, internship, and co-op pathways.",
    "BMO": "Canadian bank source whose largest student hiring market is Toronto and whose campus program includes Technology & Operations and Data & AI.",
    "CIBC": "The official CIBC campus Workday site is a distinct student/new-graduate source with Toronto technology programs.",
    "Wealthsimple": "Toronto-founded Canadian financial technology employer with Remote (Canada), Toronto, engineering, data, AI, and early-talent roles.",
    "Thomson Reuters": "Canadian-headquartered technology employer with a Toronto office, Labs internships, AI/ML, applied research, data, and software roles.",
    "Manulife": "Canadian insurer with Waterloo headquarters, Toronto offices, Canada-remote roles, and technology, advanced analytics, AI, internship, and co-op programs.",
    "Sun Life": "Canadian insurer with Waterloo and Toronto student programs spanning IT, development, big data, mobile, analytics, and engineering-adjacent work.",
    "Ciena": "Ottawa R&D hub with high-volume software, optical, photonics, hardware, electrical, test, and engineering internships and co-ops.",
    "Nokia Canada": "Nokia's official Canada program identifies Ottawa as a major R&D centre for software, embedded, hardware, optical, networking, AI, and student roles.",
    "Ericsson Canada": "Ericsson's exact global company source includes Ottawa, Toronto, and Montreal roles; Canada internships cover AI/ML, mobile software, IoT, and big data.",
    "Ross Video": "Ottawa-area Canadian broadcast technology manufacturer with software, hardware, FPGA, verification, electrical, and student opportunities.",
    "Xanadu": "Toronto quantum-computing company with exact AI/ML, software, photonics, hardware, data, controls, and electrical job attribution.",
    "ApplyBoard": "Kitchener-headquartered Canadian education technology company with software, data, AI, product, and engineering relevance.",
}

COMPATIBILITY = {
    name: text for name, text in {
        "RBC": "Official Canadian co-op/intern and Tech@RBC programs; public source also contains US/global roles, so location remains job-level evidence.",
        "TD Bank": "Official Canada co-op/intern programs include Global Technology & Solutions, digital strategy, and data/analytics.",
        "Scotiabank": "Official Canada internships begin in January, May, and September and include Technology and Data & Analytics.",
        "BMO": "Official paid co-op/intern roles are available across Canada, with the largest volume generally in Toronto.",
        "CIBC": "Dedicated official campus board plus official Canadian internships and co-op program.",
        "Wealthsimple": "Official early-talent program and Canada-remote/Toronto job labels; board may contain experienced roles between student cycles.",
        "Thomson Reuters": "Official campus program includes technology internships and Toronto Labs pathways; global board requires job-level geography.",
        "Manulife": "Official internship/co-op program includes Technology, Advanced Analytics, and AI in Canada and globally.",
        "Sun Life": "Dedicated official Campus board; Canadian co-op and summer programs explicitly include Toronto and Waterloo IT roles.",
        "Ciena": "Official student/new-graduate program and current Ottawa engineering internship volume.",
        "Nokia Canada": "Official Canada early-careers program hires software, embedded, hardware, test, and networking students three times yearly.",
        "Ericsson Canada": "Official US/Canada internship program; global Ericsson contract retains exact company attribution and job-level location evidence.",
        "Ross Video": "Ontario engineering and student compatible, but the exact Dayforce source is not safely collectable by a registered adapter.",
        "Xanadu": "Official exact-company board contains Toronto/Canada engineering roles and a separate residency pathway.",
        "ApplyBoard": "Official exact-company board and Kitchener identity; student roles are accepted only when explicitly present in current titles.",
    }.items()
}

STUDENT_PROGRAM_EVIDENCE = {
    "RBC": "https://jobs.rbc.com/ca/en/students-grad-co-ops-internships",
    "TD Bank": "https://careers.td.com/student-new-grads/early-talent-programs/",
    "Scotiabank": "https://www.scotiabank.com/careers/en/careers/careers-students-and-new-grads.html",
    "BMO": "https://jobs.bmo.com/global/en/students",
    "CIBC": "https://www.cibc.com/en/about-cibc/careers/students-and-graduates/internships-and-co-op-students.html",
    "Wealthsimple": OFFICIAL_CAREERS_URLS["Wealthsimple"],
    "Thomson Reuters": "https://www.thomsonreuters.com/en/careers/campus-careers",
    "Manulife": "https://careers.manulife.com/global/en/internship-co-op",
    "Sun Life": "https://www.sunlife.ca/en/careers/student-and-new-graduate-programs/co-op-and-summer-internship-opportunities/",
    "Ciena": "https://www.ciena.com/careers/students-new-graduates",
    "Nokia Canada": "https://www.nokia.com/careers/our-locations/canada/students-and-graduates/",
    "Ericsson Canada": "https://www.ericsson.com/en/careers/students-early-careers/internships-us-canada",
    "Ross Video": OFFICIAL_CAREERS_URLS["Ross Video"],
    "Xanadu": OFFICIAL_CAREERS_URLS["Xanadu"],
    "ApplyBoard": OFFICIAL_CAREERS_URLS["ApplyBoard"],
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS.get(name),
        "max_pages": 1, "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _entry("RBC", "workday", "https://jobs.rbc.com/ca/en", identity_regex=r"RBCGLOBAL1|Careers at RBC",
           allowed_apply_hosts=["rbc.wd3.myworkdayjobs.com"], identity_url="https://rbc.wd3.myworkdayjobs.com/RBCGLOBAL1",
           search_url="https://rbc.wd3.myworkdayjobs.com/RBCGLOBAL1", tenant="rbc", wd_subdomain="wd3", site_id="RBCGLOBAL1"),
    _entry("TD Bank", "workday", OFFICIAL_CAREERS_URLS["TD Bank"], identity_regex=r"TD_Bank_Careers|Careers at TD",
           allowed_apply_hosts=["td.wd3.myworkdayjobs.com"], identity_url="https://td.wd3.myworkdayjobs.com/TD_Bank_Careers",
           search_url="https://td.wd3.myworkdayjobs.com/TD_Bank_Careers", tenant="td", wd_subdomain="wd3", site_id="TD_Bank_Careers"),
    _entry("Scotiabank", "sap_successfactors", "https://jobs.scotiabank.com/", identity_regex=r"Scotiabank|Scotia",
           allowed_apply_hosts=["jobs.scotiabank.com"], identity_url="https://jobs.scotiabank.com/search/",
           search_url="https://jobs.scotiabank.com/search/", endpoint_variant="rmk_table"),
    _entry("BMO", "phenom", "https://jobs.bmo.com/global/en", identity_regex=r"BMO|Bank of Montreal",
           allowed_apply_hosts=["bmo.wd3.myworkdayjobs.com"], identity_url="https://jobs.bmo.com/global/en/search-results",
           search_url="https://jobs.bmo.com/global/en/search-results", endpoint_variant="embedded_refine_search",
           ref_num="BOMOGLOBAL", locale="en_global"),
    _entry("CIBC", "workday", OFFICIAL_CAREERS_URLS["CIBC"], identity_regex=r"CIBC|Canadian Imperial Bank",
           allowed_apply_hosts=["cibc.wd3.myworkdayjobs.com"], identity_url="https://cibc.wd3.myworkdayjobs.com/campus",
           search_url="https://cibc.wd3.myworkdayjobs.com/campus", tenant="cibc", wd_subdomain="wd3", site_id="campus"),
    _entry("Wealthsimple", "ashby", OFFICIAL_CAREERS_URLS["Wealthsimple"], identity_regex=r"Wealthsimple",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://jobs.ashbyhq.com/wealthsimple", slug="wealthsimple"),
    _entry("Thomson Reuters", "workday", OFFICIAL_CAREERS_URLS["Thomson Reuters"], identity_regex=r"Thomson Reuters",
           allowed_apply_hosts=["thomsonreuters.wd5.myworkdayjobs.com"], identity_url="https://thomsonreuters.wd5.myworkdayjobs.com/External_Career_Site",
           search_url="https://thomsonreuters.wd5.myworkdayjobs.com/External_Career_Site", tenant="thomsonreuters", wd_subdomain="wd5", site_id="External_Career_Site"),
    _entry("Manulife", "phenom", "https://careers.manulife.com/global/en", identity_regex=r"Manulife|John Hancock",
           allowed_apply_hosts=["manulife.wd3.myworkdayjobs.com"], identity_url="https://careers.manulife.com/global/en/search-results",
           search_url="https://careers.manulife.com/global/en/search-results", endpoint_variant="embedded_refine_search",
           ref_num="MFZMFIUS", locale="en_global"),
    _entry("Sun Life", "workday", OFFICIAL_CAREERS_URLS["Sun Life"], identity_regex=r"Sun Life",
           allowed_apply_hosts=["sunlife.wd3.myworkdayjobs.com"], identity_url="https://sunlife.wd3.myworkdayjobs.com/Campus",
           search_url="https://sunlife.wd3.myworkdayjobs.com/Campus", tenant="sunlife", wd_subdomain="wd3", site_id="Campus"),
    _entry("Ciena", "workday", OFFICIAL_CAREERS_URLS["Ciena"], identity_regex=r"Ciena",
           allowed_apply_hosts=["ciena.wd5.myworkdayjobs.com"], identity_url="https://ciena.wd5.myworkdayjobs.com/Careers",
           search_url="https://ciena.wd5.myworkdayjobs.com/Careers", tenant="ciena", wd_subdomain="wd5", site_id="Careers"),
    _entry("Nokia Canada", "oracle_recruiting", "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
           identity_regex=r"Nokia", allowed_apply_hosts=["fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com"],
           identity_url="https://jobs.nokia.com/en/sites/CX_1", endpoint_url="https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
           site_number="CX_1", locale="en"),
    _entry("Ericsson Canada", "eightfold", "https://jobs.ericsson.com/careers", identity_regex=r"Ericsson",
           allowed_apply_hosts=["jobs.ericsson.com"], identity_url="https://jobs.ericsson.com/careers",
           endpoint_url="https://jobs.ericsson.com/api/pcsx/search", endpoint_variant="pcsx",
           organization_id="ericsson.com"),
    _entry("Xanadu", "static_html", OFFICIAL_CAREERS_URLS["Xanadu"], identity_regex=r"Xanadu",
           allowed_apply_hosts=["xanadu.applytojob.com"], identity_url="https://xanadu.applytojob.com/",
           listing_url="https://xanadu.applytojob.com/", endpoint_variant="server_rendered_cards",
           listing_selector="ul.list-group", job_card_selector="li.list-group-item",
           title_selector="h3.list-group-item-heading a", location_selector="ul.list-inline li:first-child",
           department_selector="ul.list-inline li:nth-child(2)", detail_link_selector="h3.list-group-item-heading a",
           external_id_regex=r"/apply/([^/]+)/"),
    _entry("ApplyBoard", "workday", OFFICIAL_CAREERS_URLS["ApplyBoard"], identity_regex=r"ApplyBoard",
           allowed_apply_hosts=["applyboard.wd3.myworkdayjobs.com"], identity_url="https://applyboard.wd3.myworkdayjobs.com/ApplyBoardNH",
           search_url="https://applyboard.wd3.myworkdayjobs.com/ApplyBoardNH", tenant="applyboard", wd_subdomain="wd3", site_id="ApplyBoardNH"),
]

EXCLUDED_REVIEWS = [{
    "company": "Ross Video", "platform": "Dayforce", "adapter": "none",
    "listing_url": "https://jobs.dayforcehcm.com/rossvideo/CANDIDATEPORTAL",
    "classification": "unsupported_platform",
    "exclusion_reason": ("Ross Video's official careers page routes current openings to the exact rossvideo "
                         "Dayforce tenant. Dayforce is not a registered production adapter and its application "
                         "shell advertises protected interactive flows; no CAPTCHA, WAF, or session path is used."),
    "identity_regex": r"Ross Video|Careers",
}]

PLATFORM_NOTES = {
    "RBC": ("Phenom front end + Workday", "workday"), "TD Bank": ("Workday", "workday"),
    "Scotiabank": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "BMO": ("Phenom + Workday", "phenom"), "CIBC": ("Workday Campus", "workday"),
    "Wealthsimple": ("Ashby", "ashby"), "Thomson Reuters": ("Workday", "workday"),
    "Manulife": ("Phenom + Workday", "phenom"), "Sun Life": ("Workday Campus", "workday"),
    "Ciena": ("Workday", "workday"), "Nokia Canada": ("Oracle Recruiting Cloud", "oracle_recruiting"),
    "Ericsson Canada": ("Eightfold PCSx", "eightfold"), "Ross Video": ("Dayforce", "none"),
    "Xanadu": ("JazzHR server-rendered board", "static_html"), "ApplyBoard": ("Workday", "workday"),
}

CSV_FIELDS = [
    "company", "official_careers_url", "listing_url", "ontario_canada_relevance",
    "canada_student_compatibility", "platform", "adapter", "result", "jobs_normalized",
    "current_student_intern_hits", "current_focus_role_hits", "current_student_focus_hits",
    "current_student_role_evidence", "ontario_locations_represented", "canadian_locations_represented",
    "student_program_evidence_url", "exclusion_reason", "original_246_master", "master_rank",
    "validation_status", "stable_id_result", "title_result", "location_result", "apply_url_result",
    "allowed_apply_hosts", "job_count_run_1", "job_count_run_2",
]

CANADA_LOCATION_RE = re.compile(
    r"\bCanada\b|\b(?:Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland(?: and Labrador)?|"
    r"Nova Scotia|Northwest Territories|Nunavut|Ontario|Prince Edward Island|Quebec|Qu[eé]bec|Saskatchewan|Yukon)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)(?:,|\b)|"
    r"\b(?:Toronto|Waterloo|Kitchener|Ottawa|Kanata|Mississauga|Brampton|Markham|Oakville|Burlington|"
    r"Hamilton|Guelph|Cambridge|London|Nepean|Richmond Hill|Vaughan|Scarborough|North York)\b", re.IGNORECASE,
)
ONTARIO_LOCATION_RE = re.compile(
    r"\b(?:Toronto|Waterloo|Kitchener|Ottawa|Kanata|Mississauga|Brampton|Markham|Oakville|Burlington|"
    r"Hamilton|Guelph|Cambridge|London|Nepean|Richmond Hill|Vaughan|Scarborough|North York)\b|"
    r"\bOntario\b(?!\s*,\s*California)|,\s*ON(?:,|\b)", re.IGNORECASE,
)
FOCUS_ROLE_RE = re.compile(
    r"\b(?:software|developer|programmer|artificial intelligence|AI|machine learning|ML|data|embedded|"
    r"firmware|hardware|electrical|controls?|robotics?|automation|technology|analytics|FPGA|ASIC|photonics?|optical|"
    r"systems? engineer|test engineer|verification|DevOps|cloud|cybersecurity|quantum)\b", re.IGNORECASE,
)
STUDENT_ROLE_RE = re.compile(
    r"\b(?:intern(?:ship)?s?|co\s*[-/]?\s*op|summer\s+student|student\s+engineer)\b|"
    r"^student\s*[,\-:]", re.IGNORECASE,
)


def _configure_engine() -> None:
    values = {
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS, "MASTER_WORKBOOK": MASTER_WORKBOOK,
        "CANDIDATE_CSV": CANDIDATE_CSV, "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY,
        "VALIDATED_REGISTRY": VALIDATED_REGISTRY, "REPORT": REPORT,
        "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT, "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS,
        "MAX_PAGES": MAX_PAGES, "MAX_JOBS": MAX_JOBS, "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES,
        "REVIEWED_COMPANIES": REVIEWED_COMPANIES, "MASTER_RANKS": MASTER_RANKS,
        "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS, "CALGARY_RELEVANCE": CANADA_RELEVANCE,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE, "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES,
        "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS, "PLATFORM_NOTES": PLATFORM_NOTES,
        "STUDENT_ROLE_RE": STUDENT_ROLE_RE,
    }
    for name, value in values.items():
        setattr(engine, name, value)
    engine.build_candidate_registry = _build_candidate_registry_for_engine
    engine._verify_jobs = _verify_jobs
    engine._finalize = _finalize


source_key = engine.source_key
_duplicates = engine._duplicates
_status_for_error = engine._status_for_error


def reconcile() -> list[dict[str, Any]]:
    _configure_engine()
    return engine.reconcile()


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY,
                             additions: Optional[Iterable[Mapping[str, Any]]] = None) -> bytes:
    _configure_engine()
    return _ENGINE_BUILD_CANDIDATE_REGISTRY(production, destination, additions)


def _build_candidate_registry_for_engine() -> bytes:
    return _ENGINE_BUILD_CANDIDATE_REGISTRY(PRODUCTION, CANDIDATE_REGISTRY, CANDIDATE_ENTRIES)


def _verify_jobs(jobs: Iterable[Any], entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    inspected = list(jobs)[:MAX_JOBS]
    for job in inspected:
        if not str(job.job_id or "").strip():
            raise AdapterError("Candidate has a missing stable ID")
        if not str(job.title or "").strip():
            raise AdapterError("Candidate has a missing title")
        if not str(job.location or "").strip():
            raise AdapterError("Candidate has a missing location")
        if str(job.company) != str(entry["name"]):
            raise AdapterError("Candidate has ambiguous company attribution")
    return _verify_urls(inspected, entry)


def _verify_urls(jobs: list[Any], entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    ids = [str(job.job_id).strip() for job in jobs]
    urls = [str(job.url).strip() for job in jobs]
    if len(ids) != len(set(ids)):
        raise AdapterError("Candidate has duplicate stable IDs")
    if any(not value for value in urls) or len(urls) != len(set(urls)):
        raise AdapterError("Candidate apply URLs are missing or not job-specific")
    allowed = [str(value).casefold() for value in entry.get("allowed_apply_hosts") or []]
    for url in urls:
        parsed = engine.urlparse(url)
        if parsed.scheme != "https" or not engine._allowed_host((parsed.hostname or "").casefold(), allowed):
            raise AdapterError("Candidate has an unofficial apply URL")
    return sorted(zip(ids, urls), key=lambda item: item[0])


def _locations(jobs: Iterable[Any], pattern: re.Pattern[str]) -> list[str]:
    values: dict[str, str] = {}
    for job in list(jobs)[:MAX_JOBS]:
        location = " ".join(str(job.location or "").split())
        for part in location.split(" | "):
            cleaned = part.strip()
            lowered = cleaned.casefold()
            if any(country in lowered for country in ("united states", "united kingdom")) and "canada" not in lowered:
                continue
            if cleaned and pattern.search(cleaned):
                values.setdefault(cleaned.casefold(), cleaned)
    return [values[key] for key in sorted(values)]


def _role_hits(jobs: Iterable[Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    focus, student_focus = [], []
    for job in list(jobs)[:MAX_JOBS]:
        evidence = {"title": job.title, "location": job.location, "url": job.url}
        if FOCUS_ROLE_RE.search(str(job.title or "")):
            focus.append(evidence)
            if engine.STUDENT_ROLE_RE.search(str(job.title or "")):
                student_focus.append(evidence)
    return focus, student_focus


def _finalize(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    result = _ENGINE_FINALIZE(entry, runs)
    if not result.get("safe_to_activate"):
        return result
    evidence = [
        sorted(
            (item["job_id"], item["title"], item["location"], item["url"])
            for item in run.get("normalized_job_evidence") or []
        )
        for run in runs
    ]
    if len(evidence) != 2 or evidence[0] != evidence[1]:
        result.update(
            result="excluded", safe_to_activate=False, jobs_normalized=0,
            current_student_intern_hits=0, current_student_role_evidence=[],
            validation_status="non_deterministic_job_fields", stable_id_result="not_repeatable",
            apply_url_result="not_repeatable",
            exclusion_reason="Repeated runs changed stable IDs, titles, locations, or job-specific URLs",
        )
    return result


def _run_direct(entry: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    _configure_engine()
    started = time.monotonic()
    session = requests.Session()
    try:
        identity = engine._identity_check(entry, session, timeout)
        jobs = scanner.ADAPTERS[str(entry["platform"])](session, CompanyTarget.from_dict(dict(entry)), timeout)
        observed = list(jobs)[:MAX_JOBS]
        inspected = [
            job for job in observed
            if all(str(value or "").strip() for value in (job.job_id, job.title, job.location, job.url))
        ]
        signature = _verify_jobs(inspected, entry)
        pagination = dict(engine._pagination(session, str(entry["platform"])))
        zero = not signature and bool(pagination.get("zero_job_contract") or pagination.get("stop_reason") == "validated_zero_jobs")
        if not signature and not zero:
            raise AdapterError("Candidate did not conclusively prove a zero-job collection")
        focus, student_focus = _role_hits(inspected)
        evidence = [{"job_id": job.job_id, "title": job.title, "location": job.location, "url": job.url} for job in inspected]
        return {
            "runtime_seconds": round(time.monotonic() - started, 3), "success": True,
            "jobs_observed": len(jobs), "jobs_normalized": len(signature),
            "incomplete_jobs_rejected": len(observed) - len(inspected),
            "stable_ids": [item[0] for item in signature], "apply_urls": [item[1] for item in signature],
            "normalized_job_evidence": evidence, "student_hits": engine._student_hits(inspected),
            "focus_role_hits": focus, "student_focus_hits": student_focus,
            "ontario_locations": _locations(inspected, ONTARIO_LOCATION_RE),
            "canadian_locations": _locations(inspected, CANADA_LOCATION_RE),
            "zero_job_contract": zero, "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
            "identity": identity, "pagination_stop_reason": pagination.get("stop_reason", "adapter_bounded_collection"),
        }
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        return {
            "runtime_seconds": round(time.monotonic() - started, 3), "success": False,
            "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [], "apply_urls": [],
            "incomplete_jobs_rejected": 0,
            "normalized_job_evidence": [], "student_hits": [], "focus_role_hits": [], "student_focus_hits": [],
            "ontario_locations": [], "canadian_locations": [],
            "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
            "status": _status_for_error(exc), "error": str(exc),
        }
    finally:
        session.close()


def _inventory_bytes(results: Iterable[Mapping[str, Any]]) -> bytes:
    by_name = {str(item["company"]): item for item in results}
    rows = []
    for name in REVIEWED_COMPANIES:
        entry = next((item for item in CANDIDATE_ENTRIES if item["name"] == name), None)
        review = next((item for item in EXCLUDED_REVIEWS if item["company"] == name), None)
        source = entry or review or {}
        result = by_name[name]
        runs = list(result.get("runs") or [])
        rows.append({
            "company": name, "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "ontario_canada_relevance": CANADA_RELEVANCE[name], "canada_student_compatibility": COMPATIBILITY[name],
            "platform": PLATFORM_NOTES[name][0], "adapter": PLATFORM_NOTES[name][1], "result": result["result"],
            "jobs_normalized": result["jobs_normalized"], "current_student_intern_hits": result["current_student_intern_hits"],
            "current_focus_role_hits": result.get("current_focus_role_hits", 0),
            "current_student_focus_hits": result.get("current_student_focus_hits", 0),
            "current_student_role_evidence": "; ".join(hit["title"] for hit in result["current_student_role_evidence"]),
            "ontario_locations_represented": " | ".join(result.get("ontario_locations_represented") or []),
            "canadian_locations_represented": " | ".join(result.get("canadian_locations_represented") or []),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name], "exclusion_reason": result["exclusion_reason"],
            "original_246_master": str(name in MASTER_RANKS).lower(), "master_rank": MASTER_RANKS.get(name, ""),
            "validation_status": result["validation_status"], "stable_id_result": result["stable_id_result"],
            "title_result": result.get("title_result", "not_validated"), "location_result": result.get("location_result", "not_validated"),
            "apply_url_result": result["apply_url_result"], "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
            "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
            "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
        })
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    _configure_engine()
    production_before = PRODUCTION.read_bytes()
    mappings_before = PARENT_MAPPINGS.read_bytes()
    previous_run_direct = engine._run_direct
    engine._run_direct = _run_direct
    try:
        payload = engine.validate(timeout)
    finally:
        engine._run_direct = previous_run_direct
    for result in payload["results"]:
        name = str(result["company"])
        result["ontario_canada_relevance"] = CANADA_RELEVANCE[name]
        result["canada_student_compatibility"] = COMPATIBILITY[name]
        runs = list(result.get("runs") or [])
        first = runs[0] if runs else {}
        result["ontario_locations_represented"] = list(first.get("ontario_locations") or [])
        result["canadian_locations_represented"] = list(first.get("canadian_locations") or [])
        result["current_focus_role_hits"] = len(first.get("focus_role_hits") or [])
        result["current_student_focus_hits"] = len(first.get("student_focus_hits") or [])
        if result["safe_to_activate"]:
            result["title_result"] = "present_across_two_runs"
            result["location_result"] = "present_across_two_runs"
        else:
            result["title_result"] = "not_validated"
            result["location_result"] = "not_validated"
    payload["candidate_registry_has_exact_274_entry_prefix"] = payload.pop("candidate_registry_has_exact_186_entry_prefix")
    payload["validated_registry_has_exact_274_entry_prefix"] = payload.pop("validated_registry_has_exact_186_entry_prefix")
    totals = payload["totals"]
    totals["validated_outside_original_master"] = totals.pop("calgary_focused_validated_outside_master")
    totals["validated_outside_original_master_names"] = totals.pop("calgary_focused_validated_outside_master_names")
    totals["projected_canonical_production_count"] = totals["projected_production_sources"]
    totals["projected_original_master_coverage_ratio"] = f"{totals['projected_original_master_coverage']}/{totals['projected_original_master_total']}"
    totals["statuses"] = dict(sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items()))
    _write_bytes(CANDIDATE_CSV, _inventory_bytes(payload["results"]))
    _write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    if PRODUCTION.read_bytes() != production_before or PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Production registry or parent mappings changed during validation")
    return payload


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.build_only:
            reconcile()
            build_candidate_registry()
            _write_bytes(VALIDATED_REGISTRY, engine._registry_bytes(PRODUCTION, []))
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Ontario/Canada expansion v1 Batch A validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
