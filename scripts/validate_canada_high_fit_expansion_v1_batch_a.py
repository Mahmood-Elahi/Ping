"""Build and validate Canada/High-Fit Expansion v1 Batch A.

The validator starts from the exact 302-source production registry, performs
two bounded anonymous reads for every promotable source, and writes only
candidate, validated, inventory, and ignored report artifacts. It never
activates sources, changes mappings, publishes jobs, or writes persistence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_ontario_canada_expansion_v1_batch_a as base
from src.scraper.company_careers.models import AdapterError


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_canada_high_fit_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.canada_high_fit_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.canada_high_fit_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "canada_high_fit_expansion_v1_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 302
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 196
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "ecobee", "Jobber", "Cerebras Systems", "CN Rail", "Cisco",
    "GE Aerospace", "Bombardier", "Fortinet", "Groq",
    "Zebra Technologies", "SAP", "ServiceNow", "Affirm",
    "Palo Alto Networks", "Nordic Semiconductor",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "ecobee": "https://careers.generac.com/en/brands/ecobee/",
    "Jobber": "https://www.getjobber.com/about/careers/",
    "Cerebras Systems": "https://www.cerebras.ai/join-us",
    "CN Rail": "https://www.cn.ca/en/careers",
    "Cisco": "https://careers.cisco.com/global/en",
    "GE Aerospace": "https://careers.geaerospace.com/global/en",
    "Bombardier": "https://bombardier.com/en/careers",
    "Fortinet": "https://www.fortinet.com/corporate/careers",
    "Groq": "https://groq.com/company",
    "Zebra Technologies": "https://www.zebra.com/us/en/about-zebra/careers.html",
    "SAP": "https://jobs.sap.com/",
    "ServiceNow": "https://careers.servicenow.com/",
    "Affirm": "https://www.affirm.com/careers",
    "Palo Alto Networks": "https://jobs.paloaltonetworks.com/en",
    "Nordic Semiconductor": "https://www.nordicsemi.com/About-us/Careers",
}

CANADA_RELEVANCE = {
    "ecobee": "Toronto smart-home and IoT employer with software, data, embedded, firmware, hardware, electrical, controls, and student roles, attributed on Generac's official ecobee brand page.",
    "Jobber": "Canadian SaaS employer with Edmonton, Toronto, Vancouver, remote, software, data, AI/ML, mobile, infrastructure, and product engineering roles.",
    "Cerebras Systems": "AI-compute company whose exact current board includes Toronto software, ML, compiler, systems, silicon, hardware, and new-graduate roles.",
    "CN Rail": "Canadian railway with Canada/US internships, co-ops, information technology, engineering, electrical, controls, networking, and operations roles.",
    "Cisco": "Global networking and security employer with Ottawa, Kanata, Toronto, Montreal, Vancouver, Canada, US, remote, hardware, ASIC, software, and AI roles.",
    "GE Aerospace": "Aerospace engineering employer with Canadian operations in Bromont and other sites plus software, embedded, electrical, controls, manufacturing, and student programs.",
    "Bombardier": "Canadian aerospace employer with Quebec and Ontario software, avionics, systems, controls, electrical, manufacturing, engineering, and student roles.",
    "Fortinet": "Cybersecurity and networking employer with major Burnaby R&D plus Ottawa and Calgary operations and software, embedded, firmware, hardware, QA, and intern roles.",
    "Groq": "High-fit AI inference, systems, compiler, silicon, hardware, and software employer, but its former careers routes no longer expose a current official collection.",
    "Zebra Technologies": "Enterprise hardware and software employer with Mississauga/Canada, US, embedded, firmware, electrical, robotics, computer vision, and early-career relevance.",
    "SAP": "Enterprise software and AI employer with Toronto, Vancouver, Montreal, Waterloo, Canada/US, iXp internships, co-ops, and new-graduate programs.",
    "ServiceNow": "Enterprise cloud and AI employer with Toronto and Montreal engineering/research offices plus software, systems, database, security, and early-career roles.",
    "Affirm": "Remote-first financial technology employer with extensive Remote Canada/US software, ML, data, infrastructure, security, apprentice, and internship relevance.",
    "Palo Alto Networks": "Cybersecurity employer with Canadian roles in Toronto, Ottawa, Montreal, Calgary, and Vancouver plus software, networking, security research, and internships.",
    "Nordic Semiconductor": "Wireless semiconductor employer with US and global embedded, firmware, radio, silicon, hardware, electrical, test, cloud, and recurring student roles.",
}

COMPATIBILITY = {
    "ecobee": "Official Generac ecobee page exposes an exact structured ecobee value on every listed card and ecobee student roles when current.",
    "Jobber": "Exact Jobber Ashby source; Canadian/remote roles are current, while student hits remain strictly title-based.",
    "Cerebras Systems": "Exact Cerebras Ashby source; current Toronto new-graduate roles exist, but no current title contains intern, co-op, or student.",
    "CN Rail": "Official early-career page explicitly offers internships and co-ops in Canada and the US, but the current Cornerstone source is unsupported.",
    "Cisco": "Official internship/co-op program and exact global Cisco source; geography and student status remain job-level evidence.",
    "GE Aerospace": "Official Canada students/new-grads and internships/co-ops pages; exact global source retains job-level location evidence.",
    "Bombardier": "Official internship board states Canadian internships are student-only and exposes Canada engineering roles through the exact Bombardier source.",
    "Fortinet": "Official early-talent program covers undergraduate and graduate internships; exact Oracle source retains job-level geography.",
    "Groq": "Official material describes internships, but no current first-party job collection remains available for validation.",
    "Zebra Technologies": "Official early-careers pathway and exact Workday board; current student hits require explicit titles.",
    "SAP": "Official SAP Student Training and Rotation (STAR), iXp internship, vocational, and graduate programs with current Canadian roles.",
    "ServiceNow": "Official early-career programs and an exact Ontario location collection; student hits require explicit current titles.",
    "Affirm": "Exact Greenhouse board includes Remote Canada and US early-career/apprentice/intern roles when current.",
    "Palo Alto Networks": "Official 12-week internship and LEAP programs; the candidate source is the exact official Canada collection.",
    "Nordic Semiconductor": "Official students-and-graduates program and recurring embedded/hardware internships; current collection may be between internship cycles.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "ecobee": "https://careers.generac.com/en/brands/ecobee/",
    "Jobber": "https://www.getjobber.com/about/careers/",
    "Cerebras Systems": "https://www.cerebras.ai/open-positions",
    "CN Rail": "https://www.cn.ca/en/careers/students-internships",
    "Cisco": "https://careers.cisco.com/global/en/internships-and-co-ops",
    "GE Aerospace": "https://careers.geaerospace.com/global/en/canada-students-new-grads",
    "Bombardier": "https://jobs.bombardier.com/go/Internship/9387201/",
    "Fortinet": "https://www.fortinet.com/corporate/careers/early-talent-program",
    "Groq": "https://groq.com/careers-at-groq",
    "Zebra Technologies": "https://www.zebra.com/us/en/about-zebra/careers.html",
    "SAP": "https://jobs.sap.com/content/Students-and-Graduates/?locale=en_US",
    "ServiceNow": "https://careers.servicenow.com/early-careers/",
    "Affirm": "https://www.affirm.com/careers",
    "Palo Alto Networks": "https://jobs.paloaltonetworks.com/en/early-in-career",
    "Nordic Semiconductor": "https://www.nordicsemi.com/About-us/Careers/Students-and-Graduates?lang=en",
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


def _static(name: str, careers_url: str, listing_url: str, *, identity_regex: str,
            listing_selector: str, job_card_selector: str, title_selector: str,
            location_selector: str, detail_link_selector: str,
            external_id_regex: Optional[str] = None,
            external_id_attribute: Optional[str] = None,
            department_selector: Optional[str] = None) -> dict[str, Any]:
    return _entry(
        name, "static_html", careers_url, identity_regex=identity_regex,
        allowed_apply_hosts=[listing_url.split("/")[2]], identity_url=listing_url,
        listing_url=listing_url, endpoint_variant="server_rendered_cards",
        listing_selector=listing_selector, job_card_selector=job_card_selector,
        title_selector=title_selector, location_selector=location_selector,
        department_selector=department_selector,
        detail_link_selector=detail_link_selector,
        external_id_regex=external_id_regex,
        external_id_attribute=external_id_attribute,
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _static(
        "ecobee", OFFICIAL_CAREERS_URLS["ecobee"], OFFICIAL_CAREERS_URLS["ecobee"],
        identity_regex=r"\becobee\b", listing_selector="div.job-listing",
        job_card_selector="div.card-job", title_selector="h2.card-title a",
        location_selector="ul.job-meta li:first-child",
        department_selector="ul.job-meta li:nth-child(2), ul.job-meta li:nth-child(3)",
        detail_link_selector="h2.card-title a", external_id_attribute="data-id",
    ),
    _entry(
        "Jobber", "ashby", OFFICIAL_CAREERS_URLS["Jobber"], identity_regex=r"\bJobber\b",
        allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://www.getjobber.com/careers/",
        slug="jobber",
    ),
    _entry(
        "Cerebras Systems", "ashby", OFFICIAL_CAREERS_URLS["Cerebras Systems"],
        identity_regex=r"Cerebras", allowed_apply_hosts=["jobs.ashbyhq.com"],
        identity_url="https://www.cerebras.ai/open-positions", slug="cerebras",
    ),
    _entry(
        "Cisco", "phenom", OFFICIAL_CAREERS_URLS["Cisco"], identity_regex=r"\bCisco\b",
        allowed_apply_hosts=["cisco.wd5.myworkdayjobs.com"],
        identity_url="https://careers.cisco.com/global/en/search-results",
        search_url="https://careers.cisco.com/global/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="CISCISGLOBAL",
        locale="en_global", max_pages=2,
    ),
    _entry(
        "GE Aerospace", "phenom", OFFICIAL_CAREERS_URLS["GE Aerospace"],
        identity_regex=r"GE Aerospace|GE Aviation",
        allowed_apply_hosts=["geaerospace.wd5.myworkdayjobs.com"],
        identity_url="https://careers.geaerospace.com/global/en/search-results",
        search_url="https://careers.geaerospace.com/global/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="GAOGAYGLOBAL",
        locale="en_global", max_pages=1,
    ),
    _entry(
        "Bombardier", "sap_successfactors", OFFICIAL_CAREERS_URLS["Bombardier"],
        identity_regex=r"\bBombardier\b", allowed_apply_hosts=["jobs.bombardier.com"],
        identity_url="https://jobs.bombardier.com/search/",
        search_url="https://jobs.bombardier.com/search/",
        endpoint_url="https://jobs.bombardier.com/services/recruiting/v1/jobs",
        endpoint_variant="rmk_columnized", locale="en_US", page_size=10,
        max_pages=2,
    ),
    _entry(
        "Fortinet", "oracle_recruiting",
        "https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/jobs",
        identity_regex=r"\bFortinet\b", allowed_apply_hosts=["edel.fa.us2.oraclecloud.com"],
        identity_url="https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/jobs",
        endpoint_url="https://edel.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
        site_number="CX_2001", locale="en", endpoint_variant="recruiting_ce_v1",
    ),
    _entry(
        "Zebra Technologies", "workday", OFFICIAL_CAREERS_URLS["Zebra Technologies"],
        identity_regex=r"Zebra_careers|Zebra", allowed_apply_hosts=["zebra.wd501.myworkdayjobs.com"],
        identity_url="https://zebra.wd501.myworkdayjobs.com/Zebra_careers",
        search_url="https://zebra.wd501.myworkdayjobs.com/Zebra_careers",
        tenant="zebra", wd_subdomain="wd501", site_id="Zebra_careers",
    ),
    _entry(
        "SAP", "sap_successfactors", OFFICIAL_CAREERS_URLS["SAP"], identity_regex=r"\bSAP\b",
        allowed_apply_hosts=["jobs.sap.com"], identity_url="https://jobs.sap.com/search/",
        search_url="https://jobs.sap.com/search/", endpoint_variant="rmk_table",
    ),
    _static(
        "ServiceNow", OFFICIAL_CAREERS_URLS["ServiceNow"],
        "https://careers.servicenow.com/locations/ams/ontario-canada/",
        identity_regex=r"ServiceNow", listing_selector="div.job-listing",
        job_card_selector="div.card-job", title_selector="h2.card-title a",
        location_selector="ul.job-meta li:first-child",
        detail_link_selector="h2.card-title a", external_id_regex=r"/jobs/([0-9]+)/",
    ),
    _entry(
        "Affirm", "greenhouse", OFFICIAL_CAREERS_URLS["Affirm"], identity_regex=r"\bAffirm\b",
        allowed_apply_hosts=["job-boards.greenhouse.io", "boards.greenhouse.io"],
        identity_url="https://job-boards.greenhouse.io/affirm", slug="affirm",
    ),
    _static(
        "Palo Alto Networks", OFFICIAL_CAREERS_URLS["Palo Alto Networks"],
        "https://jobs.paloaltonetworks.com/en/location/canada-jobs/47263/6251999/2",
        identity_regex=r"Palo Alto Networks", listing_selector="ul.section29__search-results-ul",
        job_card_selector="li.section29__search-results-li",
        title_selector="h2.section29__search-results-job-title",
        location_selector="span.section29__result-location",
        department_selector="span.section29__result-category",
        detail_link_selector="a.section29__search-results-link", external_id_regex=r"/([0-9]+)$",
    ),
    _static(
        "Nordic Semiconductor", OFFICIAL_CAREERS_URLS["Nordic Semiconductor"],
        "https://careers.nordicsemi.com/jobs", identity_regex=r"Nordic Semiconductor|Nordic",
        listing_selector="ul#jobs_list_container", job_card_selector="li.group",
        title_selector="a[href*=/jobs/]", location_selector="span.text-base span:nth-child(3)",
        department_selector="span.text-base span:first-child",
        detail_link_selector="a[href*=/jobs/]", external_id_regex=r"/jobs/([0-9]+)-",
    ),
]

EXCLUDED_REVIEWS = [
    {
        "company": "CN Rail", "platform": "Cornerstone CSOD", "adapter": "none",
        "listing_url": "https://cn360.csod.com/ux/ats/careersite/1/home?c=cn360&lang=en-US",
        "classification": "unsupported_platform",
        "exclusion_reason": (
            "CN's official careers and early-career pages route current jobs to exact cn360 Cornerstone "
            "career sites, including a separate early-career site. No Cornerstone adapter is registered, "
            "and the JavaScript application is not treated as a static or session-derived contract; no "
            "CAPTCHA, WAF, or session bypass is attempted."
        ),
        "identity_regex": r"Careers|\bCN\b",
    },
    {
        "company": "Groq", "platform": "Retired careers route / no current board", "adapter": "none",
        "listing_url": "https://groq.com/careers-at-groq",
        "classification": "no_current_official_collection",
        "exclusion_reason": (
            "Groq's former careers URLs now redirect to the company page and no longer expose the prior "
            "job board or current official openings. Historical internship copy and cached Greenhouse links "
            "are not normalized as current jobs."
        ),
        "identity_regex": r"\bGroq\b",
    },
]

PLATFORM_NOTES = {
    "ecobee": ("Generac server-rendered ecobee brand collection", "static_html"),
    "Jobber": ("Ashby", "ashby"), "Cerebras Systems": ("Ashby", "ashby"),
    "CN Rail": ("Cornerstone CSOD", "none"), "Cisco": ("Phenom + Workday", "phenom"),
    "GE Aerospace": ("Phenom + Workday", "phenom"),
    "Bombardier": ("SAP SuccessFactors RMK columnized", "sap_successfactors"),
    "Fortinet": ("Oracle Recruiting Cloud", "oracle_recruiting"),
    "Groq": ("Retired careers route / no current board", "none"),
    "Zebra Technologies": ("Workday", "workday"),
    "SAP": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "ServiceNow": ("Server-rendered official Ontario collection", "static_html"),
    "Affirm": ("Greenhouse", "greenhouse"),
    "Palo Alto Networks": ("TalentBrew server-rendered Canada collection", "static_html"),
    "Nordic Semiconductor": ("Teamtailor server-rendered collection", "static_html"),
}

NON_CANADIAN_LOCATION_RE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b|"
    r"\b(?:United States|United Kingdom|USA|GBR)\b|^London,\s*London$",
    re.IGNORECASE,
)


def _locations(jobs: Iterable[Any], pattern: re.Pattern[str]) -> list[str]:
    values: dict[str, str] = {}
    for job in list(jobs)[:MAX_JOBS]:
        location = " ".join(str(job.location or "").split())
        for part in location.split(" | "):
            cleaned = part.strip()
            if not cleaned or NON_CANADIAN_LOCATION_RE.search(cleaned):
                continue
            if pattern.search(cleaned):
                values.setdefault(cleaned.casefold(), cleaned)
    return [values[key] for key in sorted(values)]


_BASE_VERIFY_JOBS = base._verify_jobs


def _verify_jobs(jobs: Iterable[Any], entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    inspected = list(jobs)[:MAX_JOBS]
    if entry["name"] == "ecobee":
        for job in inspected:
            brands = {" ".join(str(value).split()).casefold() for value in job.departments}
            if "ecobee" not in brands:
                raise AdapterError("ecobee candidate lacks exact structured Generac brand attribution")
    return _BASE_VERIFY_JOBS(inspected, entry)


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
        "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS, "CANADA_RELEVANCE": CANADA_RELEVANCE,
        "COMPATIBILITY": COMPATIBILITY, "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES, "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES,
    }
    for name, value in values.items():
        setattr(base, name, value)
    base._locations = _locations
    base._verify_jobs = _verify_jobs


_BASE_CONFIG_NAMES = (
    "PRODUCTION", "PARENT_MAPPINGS", "MASTER_WORKBOOK", "CANDIDATE_CSV",
    "CANDIDATE_REGISTRY", "VALIDATED_REGISTRY", "REPORT",
    "HISTORICAL_PRODUCTION_COUNT", "HISTORICAL_PARENT_MAPPINGS",
    "HISTORICAL_ORIGINAL_COVERAGE", "MASTER_COMPANY_COUNT", "MAX_TIMEOUT_SECONDS",
    "MAX_PAGES", "MAX_JOBS", "MAX_RENDERED_HTML_BYTES", "REVIEWED_COMPANIES",
    "MASTER_RANKS", "OFFICIAL_CAREERS_URLS", "CANADA_RELEVANCE", "COMPATIBILITY",
    "STUDENT_PROGRAM_EVIDENCE", "CANDIDATE_ENTRIES", "EXCLUDED_REVIEWS",
    "PLATFORM_NOTES", "_locations", "_verify_jobs",
)
_ENGINE_CONFIG_NAMES = (
    "PRODUCTION", "PARENT_MAPPINGS", "MASTER_WORKBOOK", "CANDIDATE_CSV",
    "CANDIDATE_REGISTRY", "VALIDATED_REGISTRY", "REPORT",
    "HISTORICAL_PRODUCTION_COUNT", "HISTORICAL_PARENT_MAPPINGS",
    "HISTORICAL_ORIGINAL_COVERAGE", "MASTER_COMPANY_COUNT", "MAX_TIMEOUT_SECONDS",
    "MAX_PAGES", "MAX_JOBS", "MAX_RENDERED_HTML_BYTES", "REVIEWED_COMPANIES",
    "MASTER_RANKS", "OFFICIAL_CAREERS_URLS", "CALGARY_RELEVANCE",
    "STUDENT_PROGRAM_EVIDENCE", "CANDIDATE_ENTRIES", "EXCLUDED_REVIEWS",
    "PLATFORM_NOTES", "STUDENT_ROLE_RE", "build_candidate_registry",
    "_verify_jobs", "_finalize", "_run_direct",
)


@contextmanager
def _configured_base() -> Iterable[None]:
    """Scope the legacy validation engine's module-level configuration."""
    base_before = {name: getattr(base, name) for name in _BASE_CONFIG_NAMES}
    engine_before = {name: getattr(base.engine, name) for name in _ENGINE_CONFIG_NAMES}
    _configure_base()
    try:
        yield
    finally:
        for name, value in engine_before.items():
            setattr(base.engine, name, value)
        for name, value in base_before.items():
            setattr(base, name, value)


source_key = base.source_key
_duplicates = base._duplicates


def reconcile() -> list[dict[str, Any]]:
    with _configured_base():
        return base.reconcile()


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    additions: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    with _configured_base():
        return base.build_candidate_registry(production, destination, additions)


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    with _configured_base():
        payload = base.validate(timeout)
    payload["candidate_registry_has_exact_302_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_274_entry_prefix"
    )
    payload["validated_registry_has_exact_302_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_274_entry_prefix"
    )
    payload["totals"]["projected_parent_mappings"] = HISTORICAL_PARENT_MAPPINGS
    payload["adapter_opportunities"] = [
        {
            "platform": "Cornerstone CSOD",
            "companies": ["CN Rail"],
            "opportunity": "Add a bounded anonymous Cornerstone careersite adapter that proves tenant, site, stable requisition IDs, locations, and official apply URLs.",
        },
        {
            "platform": "SAP SuccessFactors RMK",
            "companies": ["Bombardier", "SAP"],
            "opportunity": "The reusable adapter now accepts Bombardier's structured Requisition ID detail token and SAP's City location column; retain these regression fixtures.",
        },
        {
            "platform": "Teamtailor",
            "companies": ["Nordic Semiconductor"],
            "opportunity": "A dedicated bounded Teamtailor API adapter could replace the validated server-rendered-card fallback while preserving exact attribution.",
        },
        {
            "platform": "TalentBrew",
            "companies": ["Palo Alto Networks"],
            "opportunity": "A dedicated bounded TalentBrew adapter could paginate the exact Canada collection beyond its validated first server-rendered page.",
        },
        {
            "platform": "Groq current careers source",
            "companies": ["Groq"],
            "opportunity": "Refingerprint only if Groq publishes a new first-party job collection; do not reuse historical Greenhouse identifiers.",
        },
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if not REPORT.exists() or REPORT.read_bytes() != content:
        REPORT.write_bytes(content)
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
            content = base.engine._registry_bytes(PRODUCTION, [])
            if not VALIDATED_REGISTRY.exists() or VALIDATED_REGISTRY.read_bytes() != content:
                VALIDATED_REGISTRY.write_bytes(content)
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Canada/High-Fit Expansion v1 Batch A validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
