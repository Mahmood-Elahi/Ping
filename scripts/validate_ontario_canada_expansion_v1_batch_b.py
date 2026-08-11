"""Build and validate Ontario/Canada Expansion v1 Batch B.

The validator starts from the exact 288-source production registry, performs
two bounded anonymous reads for every promotable source, and writes only
candidate, validated, inventory, and ignored report artifacts. It never
activates sources, changes mappings, publishes jobs, or writes persistence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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
CANDIDATE_CSV = ROOT / "config" / "company_ontario_canada_expansion_v1_batch_b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.ontario_canada_expansion_v1_batch_b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.ontario_canada_expansion_v1_batch_b_validated.yaml"
REPORT = ROOT / "output" / "ontario_canada_expansion_v1_batch_b_validation.json"

HISTORICAL_PRODUCTION_COUNT = 288
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 196
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Tenstorrent", "Untether AI", "Magnet Forensics", "Arctic Wolf",
    "Canadian Tire", "Loblaw Digital", "CPP Investments", "OMERS",
    "Hydro One", "Ontario Power Generation", "Bruce Power",
    "Vector Institute", "Deep Genomics", "Multimatic",
    "ATS Industrial Automation",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "Tenstorrent": "https://open.tenstorrent.com/careers",
    "Untether AI": "https://www.untether.ai/",
    "Magnet Forensics": "https://www.magnetforensics.com/careers-at-magnet/",
    "Arctic Wolf": "https://arcticwolf.com/company/careers/",
    "Canadian Tire": "https://corp.canadiantire.ca/English/careers/corporate/default.aspxwelcome.asp",
    "Loblaw Digital": "https://www.loblawdigital.co/",
    "CPP Investments": "https://www.cppinvestments.com/careers/",
    "OMERS": "https://www.omers.com/Careers",
    "Hydro One": "https://www.hydroone.com/careers",
    "Ontario Power Generation": "https://www.opg.com/careers/",
    "Bruce Power": "https://www.brucepower.com/careers/",
    "Vector Institute": "https://vectorinstitute.ai/about/careers/",
    "Deep Genomics": "https://www.deepgenomics.com/careers/",
    "Multimatic": "https://www.multimatic.com/careers/",
    "ATS Industrial Automation": "https://atsautomation.com/working-at-ats/",
}

CANADA_RELEVANCE = {
    "Tenstorrent": "Toronto AI-compute company with AI compiler, ML, silicon, RTL, verification, systems, hardware, and software roles.",
    "Untether AI": "Former Toronto AI-chip company whose official site now states that its journey has ended.",
    "Magnet Forensics": "Waterloo-headquartered digital-forensics employer with Waterloo, Ottawa, Toronto, software, AI/ML, embedded, and co-op roles.",
    "Arctic Wolf": "Cybersecurity employer with a substantial Waterloo R&D presence and Canadian software, data, security, and early-talent relevance.",
    "Canadian Tire": "Toronto-headquartered Canadian employer with corporate technology, AI/ML, data, digital, cybersecurity, and campus roles.",
    "Loblaw Digital": "Toronto digital arm of Loblaw Companies, focused on software, data, mobile, digital products, and co-op experience.",
    "CPP Investments": "Toronto-headquartered pension investor with technology, data, quantitative, engineering, internship, and co-op programs.",
    "OMERS": "Toronto-headquartered Ontario pension plan with technology, data, analytics, and student/new-graduate programs.",
    "Hydro One": "Ontario electricity utility with engineering, electrical, controls, technology, and more than 300 annual student/new-grad roles.",
    "Ontario Power Generation": "Ontario power producer with electrical, mechanical, computer, controls, IT, engineering, and paid student programs.",
    "Bruce Power": "Ontario nuclear operator with engineering, electrical, controls, IT, robotics, AI, and recurring development-student roles.",
    "Vector Institute": "Toronto AI institute with machine-learning research, engineering, software, and internship roles.",
    "Deep Genomics": "Toronto AI-driven genetic-medicine company with ML research, MLOps, software, data, and computational biology roles.",
    "Multimatic": "Markham-area automotive engineering company with controls, robotics, electrical, software, vehicle systems, and student relevance.",
    "ATS Industrial Automation": "Cambridge automation division represented inside the existing ATS Corporation source, with controls, robotics, electrical, systems, and mechanical roles.",
}

COMPATIBILITY = {
    "Tenstorrent": "Official University Recruiting pathway; current titles are accepted only when explicitly student or intern roles.",
    "Untether AI": "Not currently compatible because the official company site states that operations have ended.",
    "Magnet Forensics": "Official exact-company board currently includes Ontario software co-op opportunities.",
    "Arctic Wolf": "Official hiring material addresses early talent; global Workday results retain job-level geography.",
    "Canadian Tire": "Official campus and tech-careers programs cover Toronto/Oakville digital, data, AI, cybersecurity, and technology work.",
    "Loblaw Digital": "Official co-op evidence exists, but current jobs route to the Loblaw parent board without a reviewed exact child-brand mapping.",
    "CPP Investments": "Official Toronto internships include Technology & Data, quantitative investing, engineering, programming, and analytics.",
    "OMERS": "Official 4-12 month student roles include Technology and are recruited throughout the year in Canada.",
    "Hydro One": "Official student/new-grad program welcomes more than 300 students yearly across engineering and related disciplines.",
    "Ontario Power Generation": "Official paid 4-16 month co-op, developmental, and professional engineering student programs.",
    "Bruce Power": "Official January, May, and September student intake includes Engineering, IT, electrical, and controls fields.",
    "Vector Institute": "Official Toronto opening board exposes intern employment type and the institute publishes internship contacts/programs.",
    "Deep Genomics": "Official careers page explicitly recruits top recent graduates; current student hits remain title-based.",
    "Multimatic": "Ontario automotive engineering source is student compatible; student hits require explicit current titles.",
    "ATS Industrial Automation": "Potential child-brand mapping only; v1 requires an approved structured exact-match field from the existing parent source.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Tenstorrent": "https://open.tenstorrent.com/careers/university",
    "Untether AI": "https://www.untether.ai/",
    "Magnet Forensics": "https://www.magnetforensics.com/careers-at-magnet/",
    "Arctic Wolf": "https://arcticwolf.com/company/hiring/",
    "Canadian Tire": "https://corp.canadiantire.ca/English/careers/campus/default.aspx",
    "Loblaw Digital": "https://www.loblawdigital.co/",
    "CPP Investments": "https://www.cppinvestments.com/careers/early-careers/internships/",
    "OMERS": "https://www.omers.com/students-and-new-grads",
    "Hydro One": "https://jobs.hydroone.com/go/Student-Jobs/826900/",
    "Ontario Power Generation": "https://jobs.opg.com/go/Our-Students-%26-New-Grads/2626017",
    "Bruce Power": "https://www.brucepower.com/careers/student-opportunities/",
    "Vector Institute": "https://vectorinstitute.ai/about/contact/",
    "Deep Genomics": "https://www.deepgenomics.com/careers/",
    "Multimatic": "https://www.multimatic.com/careers/",
    "ATS Industrial Automation": "https://jobs.atsautomation.com/search/",
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex,
        "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS.get(name),
        "max_pages": 1, "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _entry("Tenstorrent", "greenhouse", OFFICIAL_CAREERS_URLS["Tenstorrent"],
           identity_regex=r"Tenstorrent", allowed_apply_hosts=["job-boards.greenhouse.io", "boards.greenhouse.io"],
           identity_url="https://job-boards.greenhouse.io/tenstorrent", slug="tenstorrent"),
    _entry("Magnet Forensics", "lever", OFFICIAL_CAREERS_URLS["Magnet Forensics"],
           identity_regex=r"Magnet Forensics|Magnet", allowed_apply_hosts=["jobs.lever.co"],
           identity_url="https://jobs.lever.co/magnetforensics", slug="magnetforensics"),
    _entry("Arctic Wolf", "workday", OFFICIAL_CAREERS_URLS["Arctic Wolf"],
           identity_regex=r"Arctic Wolf", allowed_apply_hosts=["arcticwolf.wd1.myworkdayjobs.com"],
           identity_url="https://arcticwolf.wd1.myworkdayjobs.com/External",
           search_url="https://arcticwolf.wd1.myworkdayjobs.com/External",
           tenant="arcticwolf", wd_subdomain="wd1", site_id="External"),
    _entry("Canadian Tire", "workday", OFFICIAL_CAREERS_URLS["Canadian Tire"],
           identity_regex=r"Enterprise_External_Careers_Site|Canadian Tire", allowed_apply_hosts=["canadiantirecorporation.wd3.myworkdayjobs.com"],
           identity_url="https://canadiantirecorporation.wd3.myworkdayjobs.com/Enterprise_External_Careers_Site",
           search_url="https://canadiantirecorporation.wd3.myworkdayjobs.com/Enterprise_External_Careers_Site",
           tenant="canadiantirecorporation", wd_subdomain="wd3", site_id="Enterprise_External_Careers_Site"),
    _entry("OMERS", "workday", "https://careers.omers.com/ca/en",
           identity_regex=r"OMERS_External|OMERS", allowed_apply_hosts=["omers.wd3.myworkdayjobs.com"],
           identity_url="https://omers.wd3.myworkdayjobs.com/OMERS_External",
           search_url="https://omers.wd3.myworkdayjobs.com/OMERS_External",
           tenant="omers", wd_subdomain="wd3", site_id="OMERS_External"),
    _entry("Hydro One", "sap_successfactors", "https://jobs.hydroone.com/",
           identity_regex=r"Hydro One", allowed_apply_hosts=["jobs.hydroone.com"],
           identity_url="https://jobs.hydroone.com/search/", search_url="https://jobs.hydroone.com/search/",
           endpoint_variant="rmk_table"),
    _entry("Bruce Power", "workday", OFFICIAL_CAREERS_URLS["Bruce Power"],
           identity_regex=r"Bruce Power", allowed_apply_hosts=["brucepower.wd3.myworkdayjobs.com"],
           identity_url="https://brucepower.wd3.myworkdayjobs.com/BrucePower",
           search_url="https://brucepower.wd3.myworkdayjobs.com/BrucePower",
           tenant="brucepower", wd_subdomain="wd3", site_id="BrucePower"),
    _entry("Vector Institute", "adp",
           "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?ccId=9200420850870_2&cid=4ed2be93-8bc5-40e2-915f-931884eac404&lang=en_CA",
           identity_regex=r"Vector", allowed_apply_hosts=["workforcenow.adp.com"],
           identity_url="https://vectorinstitute.ai/about/careers/open-positions/",
           endpoint_url="https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions",
           endpoint_variant="workforce_now_requisitions_v1", page_size=10),
    _entry("Deep Genomics", "lever", OFFICIAL_CAREERS_URLS["Deep Genomics"],
           identity_regex=r"Deep Genomics", allowed_apply_hosts=["jobs.lever.co"],
           identity_url="https://jobs.lever.co/deepgenomics", slug="deepgenomics"),
    _entry("Multimatic", "workday", OFFICIAL_CAREERS_URLS["Multimatic"],
           identity_regex=r"Multimatic|Fast Track Your Career", allowed_apply_hosts=["multimatic.wd10.myworkdayjobs.com"],
           identity_url="https://multimatic.wd10.myworkdayjobs.com/MMEC",
           search_url="https://multimatic.wd10.myworkdayjobs.com/MMEC",
           tenant="multimatic", wd_subdomain="wd10", site_id="MMEC"),
]

EXCLUDED_REVIEWS = [
    {
        "company": "Untether AI", "platform": "Company ceased operations", "adapter": "none",
        "listing_url": "https://www.untether.ai/", "classification": "company_ceased_operations",
        "exclusion_reason": ("Untether AI's official site states that its journey has ended. There is no current "
                             "official job collection to normalize, and no stale or third-party postings are used."),
        "identity_regex": r"Untether AI|end of Untether AI.s journey",
    },
    {
        "company": "Loblaw Digital", "platform": "Loblaw parent board / Paradox", "adapter": "none",
        "listing_url": "https://careers.loblaw.ca/jobs", "classification": "parent_source_required",
        "exclusion_reason": ("Loblaw Digital's official site routes jobs to the Loblaw Companies parent board. "
                             "No reviewed structured field or parent mapping independently attributes Loblaw Digital, "
                             "so parent jobs are not relabelled and the protected Paradox flow is not bypassed."),
        "identity_regex": r"Loblaw Digital|digital properties of Loblaw",
    },
    {
        "company": "CPP Investments", "platform": "SmartRecruiters", "adapter": "none",
        "listing_url": "https://api.smartrecruiters.com/v1/companies/CPPInvestmentsInvestissementsRPC/postings",
        "classification": "unproven_zero_collection",
        "exclusion_reason": ("The previously observed CPP Investments SmartRecruiters identifier currently returns "
                             "zero jobs without company identity in the payload, while the corporate careers page "
                             "returns a bot-verification response to anonymous validation. A zero-job exact-company "
                             "contract therefore cannot be proven without bypassing controls."),
        "identity_regex": r"CPP Investments|Investissements RPC",
    },
    {
        "company": "Ontario Power Generation", "platform": "SAP SuccessFactors RMK", "adapter": "none",
        "listing_url": "https://jobs.opg.com/search/", "classification": "scheduled_migration_pause",
        "exclusion_reason": ("OPG's official jobs site announces a July 26 to August 17 recruitment-system transition. "
                             "During the pause, the public search route returns a branded shell without job rows or "
                             "a conclusive zero-job contract, so cached postings are not normalized."),
        "identity_regex": r"Ontario Power Generation|OPG",
    },
    {
        "company": "ATS Industrial Automation", "platform": "Existing ATS Corporation SuccessFactors", "adapter": "none",
        "listing_url": "https://jobs.atsautomation.com/search/", "classification": "structured_attribution_required",
        "exclusion_reason": ("ATS Industrial Automation roles occur inside the active ATS Corporation source, but the "
                             "registered adapter does not expose ATS Company as an approved structured exact-match field. "
                             "A duplicate source or description-text inference would violate canonical attribution."),
        "identity_regex": r"ATS Corporation|ATS Automation",
    },
]

PLATFORM_NOTES = {
    "Tenstorrent": ("Greenhouse", "greenhouse"),
    "Untether AI": ("Company ceased operations", "none"),
    "Magnet Forensics": ("Lever", "lever"),
    "Arctic Wolf": ("Workday", "workday"),
    "Canadian Tire": ("Workday", "workday"),
    "Loblaw Digital": ("Loblaw parent board / Paradox", "none"),
    "CPP Investments": ("SmartRecruiters", "none"),
    "OMERS": ("Phenom front end + Workday", "workday"),
    "Hydro One": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "Ontario Power Generation": ("SAP SuccessFactors RMK migration pause", "none"),
    "Bruce Power": ("Workday", "workday"),
    "Vector Institute": ("ADP Workforce Now", "adp"),
    "Deep Genomics": ("Lever", "lever"),
    "Multimatic": ("Workday", "workday"),
    "ATS Industrial Automation": ("Existing ATS Corporation SuccessFactors", "none"),
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


source_key = base.source_key
_duplicates = base._duplicates
_verify_jobs = base._verify_jobs


def reconcile() -> list[dict[str, Any]]:
    _configure_base()
    return base.reconcile()


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    additions: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    _configure_base()
    return base.build_candidate_registry(production, destination, additions)


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    _configure_base()
    payload = base.validate(timeout)
    payload["candidate_registry_has_exact_288_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_274_entry_prefix"
    )
    payload["validated_registry_has_exact_288_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_274_entry_prefix"
    )
    payload["totals"]["projected_parent_mappings"] = HISTORICAL_PARENT_MAPPINGS
    payload["adapter_opportunities"] = [
        {
            "platform": "Loblaw parent board / Paradox",
            "companies": ["Loblaw Digital"],
            "opportunity": "Expose a stable public listing contract and a reviewed structured Loblaw Digital business-unit field.",
        },
        {
            "platform": "SAP SuccessFactors parent metadata",
            "companies": ["ATS Industrial Automation"],
            "opportunity": "Normalize ATS Company into an approved exact-match field before proposing a child-brand mapping.",
        },
        {
            "platform": "SmartRecruiters zero-job identity",
            "companies": ["CPP Investments"],
            "opportunity": "Revalidate when the official board has jobs or exposes a separate anonymous company-identity contract.",
        },
        {
            "platform": "OPG post-migration source",
            "companies": ["Ontario Power Generation"],
            "opportunity": "Refingerprint and revalidate the official board after the announced August 17 migration window.",
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
        print(f"Ontario/Canada expansion v1 Batch B validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
