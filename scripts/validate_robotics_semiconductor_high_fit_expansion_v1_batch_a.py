"""Build and validate Robotics/Semiconductor High-Fit Expansion v1 Batch A.

The validator starts from the exact 315-source production registry, performs
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

from scripts import validate_canada_high_fit_expansion_v1_batch_a as base
from src.scraper.company_careers.models import AdapterError


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_robotics_semiconductor_high_fit_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.robotics_semiconductor_high_fit_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.robotics_semiconductor_high_fit_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "robotics_semiconductor_high_fit_expansion_v1_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 315
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 196
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Motional", "Ouster", "Aeva Technologies", "Innoviz Technologies",
    "Plus AI", "Einride", "Gecko Robotics", "Locus Robotics", "Qorvo",
    "Skyworks Solutions", "Diodes Incorporated", "Allegro MicroSystems",
    "Monolithic Power Systems", "Ambarella", "indie Semiconductor",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "Motional": "https://motional.com/careers",
    "Ouster": "https://ouster.com/company/careers",
    "Aeva Technologies": "https://www.aeva.com/careers/",
    "Innoviz Technologies": "https://innoviz.tech/careers",
    "Plus AI": "https://www.plus.ai/careers",
    "Einride": "https://careers.einride.tech/",
    "Gecko Robotics": "https://www.geckorobotics.com/careers",
    "Locus Robotics": "https://locusrobotics.com/company/careers",
    "Qorvo": "https://www.qorvo.com/careers",
    "Skyworks Solutions": "https://www.skyworksinc.com/Careers/",
    "Diodes Incorporated": "https://careers.diodes.com/",
    "Allegro MicroSystems": "https://www.allegromicro.com/en/About-Allegro/Careers",
    "Monolithic Power Systems": "https://www.monolithicpower.com/about-mps/careers.html",
    "Ambarella": "https://www.ambarella.com/about/careers/",
    "indie Semiconductor": "https://www.indie.inc/careers/",
}

CANADA_RELEVANCE = {
    "Motional": "Autonomous-vehicle company with US autonomy, robotics, perception, machine-learning, embedded, systems, safety, and internship roles.",
    "Ouster": "Digital-lidar and autonomy company with Ottawa/Toronto and US software, perception, embedded, electrical, hardware, and systems roles.",
    "Aeva Technologies": "4D-lidar company with US and remote sensing, perception, embedded, silicon-photonics, electrical, hardware, validation, and autonomy roles.",
    "Innoviz Technologies": "LiDAR and perception company with US/global embedded, firmware, optics, electrical, test, computer-vision, and autonomy relevance.",
    "Plus AI": "Autonomous-trucking company with current Santa Clara internships in controls, perception, computer vision, robotics, mapping, planning, and ML.",
    "Einride": "Autonomous and electric freight company with US/global autonomy, robotics, embedded, controls, hardware, software, and student relevance.",
    "Gecko Robotics": "Infrastructure robotics company with US robotics, embedded, hardware, software, autonomy, sensing, and field-engineering roles.",
    "Locus Robotics": "Warehouse-robotics company with US/remote robotics, autonomy, computer vision, embedded, hardware, controls, software, internship, and co-op relevance.",
    "Qorvo": "RF and power semiconductor company with broad US electrical, hardware, firmware, embedded, test, design, internship, and new-graduate hiring.",
    "Skyworks Solutions": "Semiconductor company with US/Canada RF, analog, electrical, hardware, firmware, test, internship, co-op, and university hiring.",
    "Diodes Incorporated": "Semiconductor company with US design, process, test, manufacturing, electrical, hardware, internship, and apprenticeship roles.",
    "Allegro MicroSystems": "Power and sensing semiconductor company with US/global electrical, hardware, embedded, firmware, IC design, test, and controls roles.",
    "Monolithic Power Systems": "Power-semiconductor company with US electrical, hardware, embedded, analog/digital IC, test, new-grad, and internship roles.",
    "Ambarella": "Edge-AI semiconductor company with US computer-vision, embedded, firmware, ASIC, verification, algorithm, software, and internship relevance.",
    "indie Semiconductor": "Automotive semiconductor and perception company with Toronto/US embedded, firmware, ASIC, electrical, radar, vision, and student relevance.",
}

COMPATIBILITY = {
    name: text for name, text in {
        "Motional": "Exact Motional Greenhouse source; student status is accepted only from explicit current titles.",
        "Ouster": "The official Ouster careers page links the exact public JazzHR board; the bounded static contract exposes stable board IDs.",
        "Aeva Technologies": "Exact Aeva public Lever source; US/remote geography and student status remain job-level evidence.",
        "Innoviz Technologies": "The official current collection is relevant but returns a WAF challenge to anonymous adapter reads.",
        "Plus AI": "Exact PlusAI public Lever source with a dedicated current US internship department and explicit intern titles.",
        "Einride": "The official Teamtailor careers site blocks bounded anonymous reads, so no session or WAF path is attempted.",
        "Gecko Robotics": "The official Gecko careers page links its exact Ashby board; explicit title evidence governs student hits.",
        "Locus Robotics": "The official careers program advertises internships/co-ops and its exact Greenhouse board supplies stable current jobs.",
        "Qorvo": "Official internships/recent-graduate program and exact public SuccessFactors RMK collection.",
        "Skyworks Solutions": "Official university-relations program, but the current SuccessFactors Unify collection is unsupported by the reviewed RMK parser.",
        "Diodes Incorporated": "Official early-career programs and exact Diodes ADP Workforce Now tenant for Americas roles.",
        "Allegro MicroSystems": "Exact official Allegro Workday tenant; explicit current titles govern student hits.",
        "Monolithic Power Systems": "Official internship material exists, but the current job-listing route returns a client challenge to anonymous reads.",
        "Ambarella": "High-fit student roles are advertised elsewhere, but the blocked official careers page does not safely corroborate a current exact-company ATS.",
        "indie Semiconductor": "Exact official Greenhouse board plus official internship and university-relations pathways, including Toronto roles.",
    }.items()
}

STUDENT_PROGRAM_EVIDENCE = {
    "Motional": "https://motional.com/careers",
    "Ouster": OFFICIAL_CAREERS_URLS["Ouster"],
    "Aeva Technologies": "https://jobs.lever.co/aeva",
    "Innoviz Technologies": OFFICIAL_CAREERS_URLS["Innoviz Technologies"],
    "Plus AI": OFFICIAL_CAREERS_URLS["Plus AI"],
    "Einride": OFFICIAL_CAREERS_URLS["Einride"],
    "Gecko Robotics": OFFICIAL_CAREERS_URLS["Gecko Robotics"],
    "Locus Robotics": OFFICIAL_CAREERS_URLS["Locus Robotics"],
    "Qorvo": "https://www.qorvo.com/careers/internships",
    "Skyworks Solutions": "https://www.skyworksinc.com/Careers/University-Relations",
    "Diodes Incorporated": OFFICIAL_CAREERS_URLS["Diodes Incorporated"],
    "Allegro MicroSystems": OFFICIAL_CAREERS_URLS["Allegro MicroSystems"],
    "Monolithic Power Systems": "https://www.monolithicpower.com/about-mps/careers/internship.html",
    "Ambarella": OFFICIAL_CAREERS_URLS["Ambarella"],
    "indie Semiconductor": "https://www.indie.inc/careers/internships/",
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
        "Motional", "greenhouse", OFFICIAL_CAREERS_URLS["Motional"],
        identity_regex=r"\bMotional\b", allowed_apply_hosts=["motional.com"],
        identity_url="https://motional.com/open-positions", slug="motional",
    ),
    _entry(
        "Ouster", "static_html", OFFICIAL_CAREERS_URLS["Ouster"],
        identity_regex=r"\bOuster\b", allowed_apply_hosts=["ouster.applytojob.com"],
        identity_url="https://ouster.applytojob.com/apply",
        listing_url="https://ouster.applytojob.com/apply",
        endpoint_variant="server_rendered_cards", listing_selector="ul.list-group",
        job_card_selector="li.list-group-item",
        title_selector="h3.list-group-item-heading a",
        location_selector="ul.list-group-item-text li:first-child",
        department_selector="ul.list-group-item-text li:nth-child(2)",
        detail_link_selector="h3.list-group-item-heading a",
        external_id_regex=r"/apply/([^/]+)/",
    ),
    _entry(
        "Aeva Technologies", "lever", OFFICIAL_CAREERS_URLS["Aeva Technologies"],
        identity_regex=r"Aeva(?:, Inc\.)?", allowed_apply_hosts=["jobs.lever.co"],
        identity_url="https://jobs.lever.co/aeva", slug="aeva",
        endpoint_variant="postings_api_v0",
    ),
    _entry(
        "Plus AI", "lever", OFFICIAL_CAREERS_URLS["Plus AI"],
        identity_regex=r"PlusAI|Plus AI", allowed_apply_hosts=["jobs.lever.co"],
        identity_url=OFFICIAL_CAREERS_URLS["Plus AI"], slug="plus-2",
        endpoint_variant="postings_api_v0",
    ),
    _entry(
        "Gecko Robotics", "ashby", OFFICIAL_CAREERS_URLS["Gecko Robotics"],
        identity_regex=r"Gecko Robotics", allowed_apply_hosts=["jobs.ashbyhq.com"],
        identity_url="https://jobs.ashbyhq.com/gecko-robotics", slug="gecko-robotics",
    ),
    _entry(
        "Locus Robotics", "greenhouse", OFFICIAL_CAREERS_URLS["Locus Robotics"],
        identity_regex=r"Locus Robotics", allowed_apply_hosts=["job-boards.greenhouse.io"],
        identity_url="https://job-boards.greenhouse.io/locusrobotics", slug="locusrobotics",
    ),
    _entry(
        "Qorvo", "sap_successfactors", OFFICIAL_CAREERS_URLS["Qorvo"],
        identity_regex=r"\bQorvo\b", allowed_apply_hosts=["careers.qorvo.com"],
        identity_url="https://careers.qorvo.com/search/",
        search_url="https://careers.qorvo.com/search/", endpoint_variant="rmk_table",
    ),
    _entry(
        "Diodes Incorporated", "adp",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?ccId=19000101_000001&cid=d72519df-1ce1-4f1d-b01b-50a082de06d5&lang=en_US",
        identity_regex=r"Diodes(?: Incorporated|, Inc\.)?",
        allowed_apply_hosts=["workforcenow.adp.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Diodes Incorporated"],
        endpoint_url="https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions",
        endpoint_variant="workforce_now_requisitions_v1", page_size=10, max_pages=2,
    ),
    _entry(
        "Allegro MicroSystems", "workday", OFFICIAL_CAREERS_URLS["Allegro MicroSystems"],
        identity_regex=r"AllegroCareers|Allegro MicroSystems",
        allowed_apply_hosts=["allegromicro.wd5.myworkdayjobs.com"],
        identity_url="https://allegromicro.wd5.myworkdayjobs.com/AllegroCareers",
        search_url="https://allegromicro.wd5.myworkdayjobs.com/AllegroCareers",
        tenant="allegromicro", wd_subdomain="wd5", site_id="AllegroCareers",
    ),
    _entry(
        "indie Semiconductor", "greenhouse", OFFICIAL_CAREERS_URLS["indie Semiconductor"],
        identity_regex=r"indie Semiconductor|\bindie\b",
        allowed_apply_hosts=["www.indie.inc"],
        identity_url="https://www.indie.inc/careers/job-opportunities/",
        slug="indiesemiconductor",
    ),
]

EXCLUDED_REVIEWS = [
    {
        "company": "Innoviz Technologies", "platform": "Comeet embedded careers collection",
        "adapter": "none", "listing_url": "https://innoviz.tech/join-us",
        "classification": "blocked_official_source",
        "exclusion_reason": (
            "Innoviz's official careers page exposes current Comeet-style job routes to browsers, but "
            "bounded anonymous HTTP reads return a WAF challenge instead of the listing. No CAPTCHA, "
            "WAF, cookie, or session bypass is attempted."
        ), "identity_regex": r"Innoviz",
    },
    {
        "company": "Einride", "platform": "Teamtailor", "adapter": "none",
        "listing_url": "https://careers.einride.tech/", "classification": "blocked_official_source",
        "exclusion_reason": (
            "Einride's official Teamtailor careers source returns HTTP 403 to bounded anonymous reads. "
            "The source is not converted into a browser/session contract and no WAF bypass is attempted."
        ), "identity_regex": r"Einride",
    },
    {
        "company": "Skyworks Solutions", "platform": "SAP SuccessFactors RMK Unify",
        "adapter": "none", "listing_url": "https://careers.skyworksinc.com/search/",
        "classification": "unsupported_platform_variant",
        "exclusion_reason": (
            "Skyworks' exact official SuccessFactors site now returns a client-rendered Unify search shell "
            "without the server-rendered RMK table/tile contract supported by the existing adapter. The "
            "empty shell is not treated as a zero-job collection."
        ), "identity_regex": r"Skyworks",
    },
    {
        "company": "Monolithic Power Systems", "platform": "First-party protected job listing",
        "adapter": "none",
        "listing_url": "https://www.monolithicpower.com/about-mps/careers/job-listings.html",
        "classification": "blocked_official_source",
        "exclusion_reason": (
            "MPS' official job-listing route returns a client challenge to bounded anonymous reads. "
            "Search-indexed job text is not a stable collection contract and no challenge or session "
            "bypass is attempted."
        ), "identity_regex": r"Monolithic Power Systems|MPS",
    },
    {
        "company": "Ambarella", "platform": "Unresolved current ATS", "adapter": "none",
        "listing_url": "https://www.ambarella.com/about/careers/",
        "classification": "ambiguous_company_attribution",
        "exclusion_reason": (
            "Ambarella's official careers route blocks bounded anonymous reads, and accessible similarly "
            "named third-party Jobvite results identify a different company. Without an official exact-company "
            "ATS link, current jobs fail closed as ambiguous; no WAF or session bypass is attempted."
        ), "identity_regex": r"Ambarella",
    },
]

PLATFORM_NOTES = {
    "Motional": ("Greenhouse", "greenhouse"),
    "Ouster": ("JazzHR server-rendered board", "static_html"),
    "Aeva Technologies": ("Lever", "lever"),
    "Innoviz Technologies": ("Comeet embedded collection / WAF", "none"),
    "Plus AI": ("Lever", "lever"),
    "Einride": ("Teamtailor / blocked", "none"),
    "Gecko Robotics": ("Ashby", "ashby"),
    "Locus Robotics": ("Greenhouse", "greenhouse"),
    "Qorvo": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "Skyworks Solutions": ("SAP SuccessFactors RMK Unify", "none"),
    "Diodes Incorporated": ("ADP Workforce Now", "adp"),
    "Allegro MicroSystems": ("Workday", "workday"),
    "Monolithic Power Systems": ("First-party protected listing", "none"),
    "Ambarella": ("Unresolved current ATS", "none"),
    "indie Semiconductor": ("Greenhouse", "greenhouse"),
}

FOCUS_ROLE_RE = re.compile(
    r"\b(?:robotics?|autonom(?:y|ous)|perception|computer vision|embedded|firmware|hardware|"
    r"electrical|semiconductor|controls?|ASIC|FPGA|silicon|photonics?|analog|mixed[- ]signal|"
    r"LiDAR|radar|sensor|systems? engineer|test engineer|verification)\b", re.IGNORECASE,
)
CANADA_US_REMOTE_RE = re.compile(
    r"\b(?:Canada|United States|USA|Remote)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|"
    r"IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|"
    r"SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)(?:,|\b)|\bToronto\b", re.IGNORECASE,
)

_CONFIG_NAMES = (
    "PRODUCTION", "PARENT_MAPPINGS", "MASTER_WORKBOOK", "CANDIDATE_CSV",
    "CANDIDATE_REGISTRY", "VALIDATED_REGISTRY", "REPORT",
    "HISTORICAL_PRODUCTION_COUNT", "HISTORICAL_PARENT_MAPPINGS",
    "HISTORICAL_ORIGINAL_COVERAGE", "MASTER_COMPANY_COUNT", "MAX_TIMEOUT_SECONDS",
    "MAX_PAGES", "MAX_JOBS", "MAX_RENDERED_HTML_BYTES", "REVIEWED_COMPANIES",
    "MASTER_RANKS", "OFFICIAL_CAREERS_URLS", "CANADA_RELEVANCE", "COMPATIBILITY",
    "STUDENT_PROGRAM_EVIDENCE", "CANDIDATE_ENTRIES", "EXCLUDED_REVIEWS", "PLATFORM_NOTES",
)


@contextmanager
def _configured_base() -> Iterable[None]:
    before = {name: getattr(base, name) for name in _CONFIG_NAMES}
    focus_before = base.base.FOCUS_ROLE_RE
    values = {name: globals()[name] for name in _CONFIG_NAMES}
    for name, value in values.items():
        setattr(base, name, value)
    base.base.FOCUS_ROLE_RE = FOCUS_ROLE_RE
    try:
        yield
    finally:
        base.base.FOCUS_ROLE_RE = focus_before
        for name, value in before.items():
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


def _represented_locations(result: Mapping[str, Any]) -> list[str]:
    runs = list(result.get("runs") or [])
    if not runs:
        return []
    values: dict[str, str] = {}
    for job in runs[0].get("normalized_job_evidence") or []:
        location = " ".join(str(job.get("location") or "").split())
        for part in location.split(" | "):
            cleaned = part.strip()
            if cleaned and CANADA_US_REMOTE_RE.search(cleaned):
                values.setdefault(cleaned.casefold(), cleaned)
    return [values[key] for key in sorted(values)]


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    with _configured_base():
        payload = base.validate(timeout)
    payload["candidate_registry_has_exact_315_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_302_entry_prefix"
    )
    payload["validated_registry_has_exact_315_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_302_entry_prefix"
    )
    for result in payload["results"]:
        result["canada_us_remote_locations_represented"] = _represented_locations(result)
    payload["totals"]["projected_parent_mappings"] = HISTORICAL_PARENT_MAPPINGS
    payload["adapter_opportunities"] = [
        {
            "platform": "JazzHR",
            "companies": ["Ouster"],
            "opportunity": "A dedicated bounded JazzHR adapter could replace the validated static-card contract while retaining stable public board IDs.",
        },
        {
            "platform": "SAP SuccessFactors RMK Unify",
            "companies": ["Skyworks Solutions"],
            "opportunity": "Add an anonymous bounded Unify search adapter only after documenting its public request schema and stable requisition contract.",
        },
        {
            "platform": "Teamtailor",
            "companies": ["Einride"],
            "opportunity": "Re-evaluate only if the official site exposes a public anonymous API or server-rendered contract without a blocked/session flow.",
        },
        {
            "platform": "First-party protected listings",
            "companies": ["Innoviz Technologies", "Monolithic Power Systems", "Ambarella"],
            "opportunity": "Refingerprint only when each company publishes a stable anonymous official collection; do not use challenge, session, or search-index substitutes.",
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
            content = base.base.engine._registry_bytes(PRODUCTION, [])
            if not VALIDATED_REGISTRY.exists() or VALIDATED_REGISTRY.read_bytes() != content:
                VALIDATED_REGISTRY.write_bytes(content)
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Robotics/Semiconductor High-Fit Expansion v1 Batch A validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
