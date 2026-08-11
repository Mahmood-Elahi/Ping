"""Build and validate Calgary/Alberta Expansion v2 Batch B without activation.

The validator starts from the exact 298-source production registry, performs
two bounded anonymous reads for every promotable source, and writes only
candidate, validated, inventory, and ignored report artifacts. It never
activates sources, changes mappings, publishes jobs, or writes persistence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_ontario_canada_expansion_v1_batch_a as base
from src.scraper.company_careers.models import AdapterError


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_calgary_alberta_expansion_v2_batch_b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.calgary_alberta_expansion_v2_batch_b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.calgary_alberta_expansion_v2_batch_b_validated.yaml"
REPORT = ROOT / "output" / "calgary_alberta_expansion_v2_batch_b_validation.json"

HISTORICAL_PRODUCTION_COUNT = 298
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 196
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "WestJet", "Enerflex", "Eavor Technologies", "Attabotics",
    "SMART Technologies", "Blackline Safety", "Parkland Corporation",
    "AltaLink", "Critical Mass", "Symend", "Circle Cardiovascular Imaging",
    "Summit Nanotech", "Carbon Upcycling", "E3 Lithium", "ATB Financial",
]
RECOVERY_TARGETS = {
    "WestJet", "Enerflex", "Eavor Technologies", "Attabotics",
    "SMART Technologies", "Blackline Safety", "Parkland Corporation", "AltaLink",
}
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "WestJet": "https://www.westjet.com/en-ca/careers",
    "Enerflex": "https://www.enerflex.com/careers/",
    "Eavor Technologies": "https://eavor.com/careers/",
    "Attabotics": "https://attabotics.com/careers/",
    "SMART Technologies": "https://www.smarttech.com/careers",
    "Blackline Safety": "https://careers.blacklinesafety.com/home",
    "Parkland Corporation": "https://www.parkland.ca/en/careers",
    "AltaLink": "https://www.altalink.ca/join-our-team/",
    "Critical Mass": "https://www.criticalmass.com/jobs",
    "Symend": "https://www.symend.com/company/careers",
    "Circle Cardiovascular Imaging": "https://www.circlecvi.com/careers",
    "Summit Nanotech": "https://www.summitnanotech.com/careers",
    "Carbon Upcycling": "https://carbonupcycling.com/careers/",
    "E3 Lithium": "https://e3lithium.ca/recruitment/",
    "ATB Financial": "https://www.atb.com/company/careers/",
}

CALGARY_ALBERTA_RELEVANCE = {
    "WestJet": "Calgary is WestJet's national headquarters and a base for corporate, technical operations, operations-control, and engineering work.",
    "Enerflex": "Calgary-headquartered energy-infrastructure employer with local engineering, electrical, controls, IT, manufacturing, and early-career pathways.",
    "Eavor Technologies": "Calgary geothermal-technology company with local drilling, facility, project, and energy engineering work.",
    "Attabotics": "Calgary-headquartered robotics and warehouse-automation company spanning software, controls, electronics, hardware, and systems engineering.",
    "SMART Technologies": "Calgary education-technology company with software, embedded, hardware, product, and university internship relevance.",
    "Blackline Safety": "Calgary-headquartered connected-safety company with local firmware, hardware, embedded, cloud, software, data, and manufacturing teams.",
    "Parkland Corporation": "Calgary-headquartered energy and convenience company with engineering, technology, operations, and new-graduate pathways.",
    "AltaLink": "Calgary-headquartered Alberta transmission utility with electrical, controls, power-systems, grid, engineering, and student relevance.",
    "Critical Mass": "Calgary-headquartered digital agency with software, data, analytics, technology, and an emerging-talent development program.",
    "Symend": "Calgary behavioral-engagement software company with product engineering, data, AI, cloud, and software roles.",
    "Circle Cardiovascular Imaging": "Calgary medical-imaging software company with local AI/ML, C++, platform, research-software, QA, and product engineering roles.",
    "Summit Nanotech": "Calgary clean-technology company, now presented as Summit Lithium Technologies, focused on direct lithium extraction and process engineering.",
    "Carbon Upcycling": "Calgary-headquartered carbon-technology company with process, chemical, project, mechanical, and scale-up engineering relevance.",
    "E3 Lithium": "Calgary-based Alberta lithium developer with process, project, reservoir, facilities, controls, and energy engineering roles.",
    "ATB Financial": "Alberta financial institution with Calgary and province-wide technology, data, design, AI, cybersecurity, and ATB 101 student roles.",
}

COMPATIBILITY = {
    "WestJet": "Canada student-compatible in principle; the current Dayforce search depends on a protected session/WAF contract.",
    "Enerflex": "Canada/US/global and early-career compatible; current exact jobs are accepted only after repeatable field-level validation.",
    "Eavor Technologies": "Calgary and new-graduate compatible in principle; applications still use shared email addresses instead of job-specific apply URLs.",
    "Attabotics": "Calgary/remote and robotics-student compatible in principle; the exact Lever collection is currently empty.",
    "SMART Technologies": "Canada/US/global and university-internship compatible; the current Dayforce search depends on a protected session/WAF contract.",
    "Blackline Safety": "Calgary/Canada/US and technical-student compatible in principle; the official Workable apply shell currently enables Turnstile.",
    "Parkland Corporation": "Canada/US and new-graduate compatible in principle; the exact TTC portal currently returns a Cloudflare challenge.",
    "AltaLink": "Calgary/Alberta student compatible in principle; the official Workable apply shell currently enables Turnstile.",
    "Critical Mass": "Calgary/Canada/US and emerging-talent compatible; exact child-company attribution is not exposed through the registered Workday adapter.",
    "Symend": "Calgary/remote and software compatible; the official page embeds an exact, anonymous JazzHR board.",
    "Circle Cardiovascular Imaging": "Calgary software and AI/ML compatible; the official page links an exact BambooHR tenant with complete job-level fields.",
    "Summit Nanotech": "Calgary clean-energy compatible in principle; the former careers URL now redirects to a company homepage without a current job collection.",
    "Carbon Upcycling": "Calgary clean-energy compatible in principle; the exact Deel board currently exposes no jobs and has no registered adapter.",
    "E3 Lithium": "Calgary engineering compatible; current official postings are PDFs whose applications route to a shared email address.",
    "ATB Financial": "Alberta and student compatible; ATB 101 explicitly includes Tech, Data & Design and the exact Eightfold source retains job-level geography.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "WestJet": OFFICIAL_CAREERS_URLS["WestJet"],
    "Enerflex": "https://www.enerflex.com/careers/students-and-early-career/",
    "Eavor Technologies": OFFICIAL_CAREERS_URLS["Eavor Technologies"],
    "Attabotics": OFFICIAL_CAREERS_URLS["Attabotics"],
    "SMART Technologies": OFFICIAL_CAREERS_URLS["SMART Technologies"],
    "Blackline Safety": OFFICIAL_CAREERS_URLS["Blackline Safety"],
    "Parkland Corporation": OFFICIAL_CAREERS_URLS["Parkland Corporation"],
    "AltaLink": OFFICIAL_CAREERS_URLS["AltaLink"],
    "Critical Mass": "https://www.criticalmass.com/calgary",
    "Symend": OFFICIAL_CAREERS_URLS["Symend"],
    "Circle Cardiovascular Imaging": OFFICIAL_CAREERS_URLS["Circle Cardiovascular Imaging"],
    "Summit Nanotech": OFFICIAL_CAREERS_URLS["Summit Nanotech"],
    "Carbon Upcycling": OFFICIAL_CAREERS_URLS["Carbon Upcycling"],
    "E3 Lithium": OFFICIAL_CAREERS_URLS["E3 Lithium"],
    "ATB Financial": "https://www.atb.com/company/careers/atb-101/",
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex, "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS.get(name),
        "max_pages": 1, "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _entry(
        "Enerflex", "sap_successfactors", OFFICIAL_CAREERS_URLS["Enerflex"],
        identity_regex=r"Enerflex", allowed_apply_hosts=["jobs.enerflex.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Enerflex"],
        search_url="https://jobs.enerflex.com/search/", endpoint_variant="rmk_tile",
    ),
    _entry(
        "Symend", "static_html", OFFICIAL_CAREERS_URLS["Symend"],
        identity_regex=r"Symend", allowed_apply_hosts=["symend.applytojob.com"],
        identity_url="https://symend.applytojob.com/apply",
        listing_url="https://symend.applytojob.com/apply",
        endpoint_variant="server_rendered_cards", listing_selector="ul.list-group",
        job_card_selector="li.list-group-item",
        title_selector="h3.list-group-item-heading a",
        location_selector="ul.list-inline li:first-child",
        department_selector="ul.list-inline li:nth-child(2)",
        detail_link_selector="h3.list-group-item-heading a",
        external_id_regex=r"/apply/([^/]+)/",
    ),
    _entry(
        "Circle Cardiovascular Imaging", "bamboohr",
        OFFICIAL_CAREERS_URLS["Circle Cardiovascular Imaging"],
        identity_regex=r"Circle Cardiovascular Imaging|Circle CVI",
        allowed_apply_hosts=["circlecardiovascularimaging.bamboohr.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Circle Cardiovascular Imaging"],
        listing_url="https://circlecardiovascularimaging.bamboohr.com/careers/list",
        slug="circlecardiovascularimaging",
    ),
    _entry(
        "ATB Financial", "eightfold", "https://careers.atb.com/careers",
        identity_regex=r"ATB Financial|ATB", allowed_apply_hosts=["careers.atb.com"],
        identity_url="https://careers.atb.com/careers",
        endpoint_url="https://careers.atb.com/api/pcsx/search",
        endpoint_variant="pcsx", organization_id="atb.com", max_pages=2,
    ),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {
        "company": company, "platform": platform, "adapter": "none",
        "listing_url": listing_url, "classification": classification,
        "exclusion_reason": reason, "identity_regex": identity_regex,
    }


EXCLUDED_REVIEWS = [
    _excluded(
        "WestJet", "Dayforce", "https://jobs.dayforcehcm.com/en-CA/WestJet/CANDIDATEPORTAL",
        "session_or_waf_dependency",
        "The exact WestJet Dayforce shell is anonymously visible, but its official jobposting search POST returns HTTP 403 and Dayforce is not registered. No browser session, WAF, reCAPTCHA, or search-state bypass is attempted.",
        r"WestJet|Great careers",
    ),
    _excluded(
        "Eavor Technologies", "Company static page / email application", OFFICIAL_CAREERS_URLS["Eavor Technologies"],
        "no_job_specific_apply_urls",
        "The current exact Eavor page publishes position headings, including a Calgary role, but every application still routes to shared careers email addresses. Stable job-specific apply URLs are absent.",
        r"Eavor|Careers",
    ),
    _excluded(
        "Attabotics", "Lever", "https://jobs.lever.co/attabotics",
        "no_current_jobs_for_id_apply_validation",
        "The current official Attabotics page links its exact Lever board, but two anonymous reads return an explicit zero-job collection. Current IDs, titles, locations, and job-specific URLs cannot be observed, so the source is not validated.",
        r"Attabotics|Warehouse Automation Careers",
    ),
    _excluded(
        "SMART Technologies", "Dayforce", "https://jobs.dayforcehcm.com/smarttech/CANDIDATEPORTAL",
        "session_or_waf_dependency",
        "The exact SMART Technologies Dayforce shell is anonymously visible, but its official jobposting search POST returns HTTP 403 and Dayforce is not registered. No browser session, WAF, or CAPTCHA path is used.",
        r"SMART Technologies|meaningful connections",
    ),
    _excluded(
        "Blackline Safety", "Workable / Turnstile", "https://apply.workable.com/blacklinesafety/",
        "captcha_dependency",
        "The current official Blackline Safety page still links its exact Workable account, and the current apply shell explicitly enables Turnstile. The unregistered Workable adapter fails closed before its widget request.",
        r"Blackline Safety|Careers at Blackline",
    ),
    _excluded(
        "Parkland Corporation", "TTC portal / Cloudflare", "https://parkland-careers.ttcportals.com/",
        "waf_dependency",
        "The current official Parkland page still links its exact TTC portal, which returns an HTTP 403 Cloudflare challenge to bounded anonymous requests. No challenge, cookie, or session bypass is attempted.",
        r"Parkland|Power Your Career",
    ),
    _excluded(
        "AltaLink", "Workable / Turnstile", "https://apply.workable.com/altalink/",
        "captcha_dependency",
        "The current official AltaLink page still links its exact Workable account, and the current apply shell explicitly enables Turnstile. The unregistered Workable adapter fails closed before its widget request.",
        r"AltaLink|Join our Team",
    ),
    _excluded(
        "Critical Mass", "Critical Mass site + shared Interpublic Workday", OFFICIAL_CAREERS_URLS["Critical Mass"],
        "structured_attribution_required",
        "The official Critical Mass page serializes selected roles from Interpublic's shared Workday tenant, but the registered Workday adapter has no reviewed exact Critical Mass agency field. Directly collecting the mixed parent tenant would misattribute jobs.",
        r"Critical Mass|Digital Marketing Agency Job Openings",
    ),
    _excluded(
        "Summit Nanotech", "Summit Lithium Technologies company site", "https://summitlithium.tech/",
        "no_current_job_contract",
        "The former Summit Nanotech careers URL currently redirects to the Summit Lithium Technologies homepage. Its careers anchor exposes no bounded current job collection, stable job IDs, or job-specific apply URLs.",
        r"Summit Lithium|Summit Nanotech",
    ),
    _excluded(
        "Carbon Upcycling", "Deel", "https://jobs.deel.com/carbon-upcycling-technologies",
        "no_current_jobs_and_unsupported_platform",
        "The current official Carbon Upcycling page links its exact Deel board, whose public page currently exposes an empty ItemList. Deel has no registered adapter and current job IDs and apply URLs cannot be validated.",
        r"Carbon Upcycling Technologies|Carbon Upcycling",
    ),
    _excluded(
        "E3 Lithium", "Company PDFs / email application", OFFICIAL_CAREERS_URLS["E3 Lithium"],
        "no_job_specific_apply_urls",
        "The current exact E3 Lithium page publishes stable job-description PDFs, but applications route to a shared HR email address. A detail document is not a job-specific official apply URL.",
        r"E3 Lithium|Careers",
    ),
]

PLATFORM_NOTES = {
    "WestJet": ("Dayforce", "none"),
    "Enerflex": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "Eavor Technologies": ("Company static page / email application", "none"),
    "Attabotics": ("Lever (current exact zero collection)", "none"),
    "SMART Technologies": ("Dayforce", "none"),
    "Blackline Safety": ("Workable / Turnstile", "none"),
    "Parkland Corporation": ("TTC portal / Cloudflare", "none"),
    "AltaLink": ("Workable / Turnstile", "none"),
    "Critical Mass": ("Critical Mass site + shared Interpublic Workday", "none"),
    "Symend": ("JazzHR server-rendered board", "static_html"),
    "Circle Cardiovascular Imaging": ("BambooHR", "bamboohr"),
    "Summit Nanotech": ("Summit Lithium Technologies company site", "none"),
    "Carbon Upcycling": ("Deel", "none"),
    "E3 Lithium": ("Company PDFs / email application", "none"),
    "ATB Financial": ("Eightfold PCSx", "eightfold"),
}

CANADA_LOCATION_RE = re.compile(
    r"\bCanada\b|\b(?:Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland|Nova Scotia|"
    r"Northwest Territories|Nunavut|Ontario|Prince Edward Island|Quebec|Québec|Saskatchewan|Yukon)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)(?:,|\b)", re.IGNORECASE,
)
ALBERTA_LOCATION_RE = re.compile(
    r"\b(?:Alberta|Calgary|Edmonton|Airdrie|Banff|Canmore|Cochrane|Lethbridge|Medicine Hat|"
    r"Red Deer|Fort McMurray|Grande Prairie|Lloydminster|Okotoks|Strathmore)\b|,\s*AB(?:,|\b)",
    re.IGNORECASE,
)
FOCUS_ROLE_RE = re.compile(
    r"\b(?:software|developer|programmer|artificial intelligence|AI|machine learning|ML|data|embedded|"
    r"firmware|hardware|electrical|controls?|robotics?|automation|energy|process|chemical|mechanical|"
    r"instrumentation|technology|analytics|FPGA|ASIC|systems? engineer|test engineer|verification|"
    r"DevOps|cloud|cybersecurity|platform|product engineer|project engineer)\b", re.IGNORECASE,
)
STUDENT_ROLE_RE = re.compile(
    r"\b(?:intern(?:ship)?s?|co\s*[-/]?\s*op|summer\s+student|student\s+engineer|apprentice|"
    r"new\s+grad(?:uate)?|estagi[aá]ri[oa]s?)\b|^student\s*[,\-:]", re.IGNORECASE,
)

CSV_FIELDS = [
    "company", "fresh_vs_recovery_target", "official_careers_url", "listing_url",
    "company_attribution_evidence", "calgary_alberta_relevance", "student_compatibility",
    "platform", "adapter", "result", "jobs_normalized", "current_student_intern_hits",
    "current_focus_role_hits", "current_student_focus_hits", "current_student_role_evidence",
    "calgary_alberta_locations_represented", "canadian_locations_represented",
    "student_program_evidence_url", "exclusion_reason", "original_246_master", "master_rank",
    "validation_status", "stable_id_result", "title_result", "location_result",
    "apply_url_result", "allowed_apply_hosts", "job_count_run_1", "job_count_run_2",
]


def _configure_base() -> None:
    values = {
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS,
        "MASTER_WORKBOOK": MASTER_WORKBOOK, "CANDIDATE_CSV": CANDIDATE_CSV,
        "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY, "VALIDATED_REGISTRY": VALIDATED_REGISTRY,
        "REPORT": REPORT, "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT, "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS,
        "MAX_PAGES": MAX_PAGES, "MAX_JOBS": MAX_JOBS,
        "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES,
        "REVIEWED_COMPANIES": REVIEWED_COMPANIES, "MASTER_RANKS": MASTER_RANKS,
        "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS,
        "CANADA_RELEVANCE": CALGARY_ALBERTA_RELEVANCE, "COMPATIBILITY": COMPATIBILITY,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES, "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES, "CANADA_LOCATION_RE": CANADA_LOCATION_RE,
        "ONTARIO_LOCATION_RE": ALBERTA_LOCATION_RE, "FOCUS_ROLE_RE": FOCUS_ROLE_RE,
        "STUDENT_ROLE_RE": STUDENT_ROLE_RE,
    }
    for name, value in values.items():
        setattr(base, name, value)
    base._locations = _locations


@contextmanager
def _isolated_base() -> Iterable[None]:
    """Contain the inherited validator's intentional module-global wiring."""
    modules: list[ModuleType] = []
    for module in (base, base.engine):
        if all(module is not existing for existing in modules):
            modules.append(module)
    snapshots = [(module, dict(vars(module))) for module in modules]
    try:
        yield
    finally:
        for module, snapshot in reversed(snapshots):
            namespace = vars(module)
            for name in set(namespace) - set(snapshot):
                del namespace[name]
            namespace.update(snapshot)


source_key = base.source_key
_duplicates = base._duplicates
_verify_jobs = base._verify_jobs


def _locations(jobs: Iterable[Any], pattern: re.Pattern[str]) -> list[str]:
    values: dict[str, str] = {}
    for job in list(jobs)[:MAX_JOBS]:
        for part in str(job.location or "").split(" | "):
            cleaned = " ".join(part.split())
            if cleaned and pattern.search(cleaned):
                values.setdefault(cleaned.casefold(), cleaned)
    return [values[key] for key in sorted(values)]


def reconcile() -> list[dict[str, Any]]:
    with _isolated_base():
        _configure_base()
        return base.reconcile()


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    additions: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    with _isolated_base():
        _configure_base()
        return base.build_candidate_registry(production, destination, additions)


def _inventory_bytes(results: Iterable[Mapping[str, Any]]) -> bytes:
    by_name = {str(item["company"]): item for item in results}
    rows: list[dict[str, Any]] = []
    for name in REVIEWED_COMPANIES:
        entry = next((item for item in CANDIDATE_ENTRIES if item["name"] == name), None)
        review = next((item for item in EXCLUDED_REVIEWS if item["company"] == name), None)
        source = entry or review or {}
        result = by_name[name]
        runs = list(result.get("runs") or [])
        rows.append({
            "company": name,
            "fresh_vs_recovery_target": "recovery" if name in RECOVERY_TARGETS else "fresh",
            "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "company_attribution_evidence": CALGARY_ALBERTA_RELEVANCE[name],
            "calgary_alberta_relevance": CALGARY_ALBERTA_RELEVANCE[name],
            "student_compatibility": COMPATIBILITY[name],
            "platform": PLATFORM_NOTES[name][0], "adapter": PLATFORM_NOTES[name][1],
            "result": result["result"], "jobs_normalized": result["jobs_normalized"],
            "current_student_intern_hits": result["current_student_intern_hits"],
            "current_focus_role_hits": result.get("current_focus_role_hits", 0),
            "current_student_focus_hits": result.get("current_student_focus_hits", 0),
            "current_student_role_evidence": "; ".join(
                hit["title"] for hit in result["current_student_role_evidence"]
            ),
            "calgary_alberta_locations_represented": " | ".join(
                result.get("calgary_alberta_locations_represented") or []
            ),
            "canadian_locations_represented": " | ".join(
                result.get("canadian_locations_represented") or []
            ),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name],
            "exclusion_reason": result["exclusion_reason"],
            "original_246_master": "false", "master_rank": "",
            "validation_status": result["validation_status"],
            "stable_id_result": result["stable_id_result"],
            "title_result": result.get("title_result", "not_validated"),
            "location_result": result.get("location_result", "not_validated"),
            "apply_url_result": result["apply_url_result"],
            "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
            "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
            "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
        })
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _validate_once(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    _configure_base()
    payload = base.validate(timeout)
    payload["candidate_registry_has_exact_298_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_274_entry_prefix"
    )
    payload["validated_registry_has_exact_298_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_274_entry_prefix"
    )
    for result in payload["results"]:
        name = str(result["company"])
        result.pop("ontario_canada_relevance", None)
        result.pop("canada_student_compatibility", None)
        result["fresh_vs_recovery_target"] = "recovery" if name in RECOVERY_TARGETS else "fresh"
        result["company_attribution_evidence"] = CALGARY_ALBERTA_RELEVANCE[name]
        result["calgary_alberta_relevance"] = CALGARY_ALBERTA_RELEVANCE[name]
        result["student_compatibility"] = COMPATIBILITY[name]
        result["calgary_alberta_locations_represented"] = result.pop(
            "ontario_locations_represented", []
        )
        for run in result.get("runs") or []:
            run["calgary_alberta_locations"] = run.pop("ontario_locations", [])
    totals = payload["totals"]
    totals["fresh_targets"] = len(REVIEWED_COMPANIES) - len(RECOVERY_TARGETS)
    totals["recovery_targets"] = len(RECOVERY_TARGETS)
    totals["projected_parent_mappings"] = HISTORICAL_PARENT_MAPPINGS
    payload["adapter_opportunities"] = [
        {
            "platform": "BambooHR location fallback",
            "companies": ["Circle Cardiovascular Imaging"],
            "opportunity": "Reuse the exact public location object when atsLocation is present but empty; implemented generically and validated here.",
        },
        {
            "platform": "Dayforce",
            "companies": ["WestJet", "SMART Technologies"],
            "opportunity": "Revalidate only if a bounded anonymous search contract works without browser, WAF, session, or CAPTCHA state.",
        },
        {
            "platform": "Workable",
            "companies": ["Blackline Safety", "AltaLink"],
            "opportunity": "The existing fail-closed adapter can be registered only after the official apply shells no longer enable Turnstile.",
        },
        {
            "platform": "Critical Mass shared Workday attribution",
            "companies": ["Critical Mass"],
            "opportunity": "Expose and validate an exact structured agency field before collecting from the Interpublic parent tenant.",
        },
        {
            "platform": "Deel",
            "companies": ["Carbon Upcycling"],
            "opportunity": "Review a bounded exact-company Deel contract when current jobs make IDs, locations, and apply URLs observable.",
        },
    ]
    base._write_bytes(CANDIDATE_CSV, _inventory_bytes(payload["results"]))
    base._write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return payload


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    with _isolated_base():
        return _validate_once(timeout)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.build_only:
            reconcile()
            build_candidate_registry()
            base._write_bytes(VALIDATED_REGISTRY, base.engine._registry_bytes(PRODUCTION, []))
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Calgary/Alberta expansion v2 Batch B validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
