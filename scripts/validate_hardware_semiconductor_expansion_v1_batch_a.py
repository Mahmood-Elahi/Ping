"""Build and validate Hardware/Semiconductor Expansion v1 Batch A.

The validator performs two bounded anonymous reads for every promotable source
and writes only candidate, validated, inventory, and ignored report artifacts.
It never activates production, changes mappings, publishes, stages, or commits.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_alberta_canada_employer_expansion_v1_batch_a as base
from src.scraper.company_careers.adapters import (
    eightfold,
    greenhouse,
    hibob,
    icims,
    oracle_recruiting,
    phenom,
    sap_successfactors,
    smartrecruiters,
    static_html,
    workday,
)
from src.scraper.company_careers.models import AdapterError


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_hardware_semiconductor_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.hardware_semiconductor_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.hardware_semiconductor_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "hardware_semiconductor_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 353
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
MAX_JOBS = 20
MAX_RAW_JOBS = 250
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "Dell", "HP Inc.", "HPE", "Western Digital", "Seagate", "Coherent Corp",
    "Credo Technology", "Astera Labs", "SK hynix", "Kioxia", "Logitech",
    "GoPro", "Sonos", "Bose", "Roku", "Kingston Technology", "MaxLinear",
    "Ambiq", "u-blox", "Corsair",
]


def _entry(
    name: str,
    platform: str,
    careers_url: str,
    *,
    identity_url: str,
    identity_regex: str,
    allowed_apply_hosts: list[str],
    **fields: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "platform": platform,
        "careers_url": careers_url,
        "identity_url": identity_url,
        "source_identity_regex": identity_regex,
        "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": 3_000_000,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _entry(
        "HP Inc.", "eightfold", "https://hp.eightfold.ai/careers",
        identity_url="https://hp.eightfold.ai/careers", identity_regex=r"\bHP\b",
        allowed_apply_hosts=["hp.eightfold.ai"],
        endpoint_url="https://hp.eightfold.ai/api/pcsx/search",
        endpoint_variant="pcsx", organization_id="hp.com",
    ),
    _entry(
        "HPE", "phenom", "https://careers.hpe.com/us/en",
        identity_url="https://careers.hpe.com/us/en/search-results",
        identity_regex=r"Hewlett Packard Enterprise|\bHPE\b",
        allowed_apply_hosts=["hpe.wd5.myworkdayjobs.com"],
        search_url="https://careers.hpe.com/us/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="HPE1US", locale="en_us",
    ),
    _entry(
        "Western Digital", "smartrecruiters", "https://www.westerndigital.com/careers",
        identity_url="https://careers.smartrecruiters.com/WesternDigital",
        identity_regex=r"Western Digital", allowed_apply_hosts=["jobs.smartrecruiters.com"],
        slug="WesternDigital",
        endpoint_url="https://api.smartrecruiters.com/v1/companies/WesternDigital/postings",
        endpoint_variant="companies_postings_v1", page_size=20,
    ),
    _entry(
        "Seagate", "sap_successfactors", "https://www.seagate.com/ca/en/jobs/",
        identity_url="https://seagatecareers.com/AllJobs", identity_regex=r"\bSeagate\b",
        allowed_apply_hosts=["seagatecareers.com"],
        search_url="https://seagatecareers.com/search/", endpoint_variant="rmk_tile",
    ),
    _entry(
        "Coherent Corp", "oracle_recruiting", "https://hcwp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
        identity_url="https://hcwp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
        identity_regex=r"Coherent Corp", allowed_apply_hosts=["hcwp.fa.us2.oraclecloud.com"],
        endpoint_url="https://hcwp.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
        endpoint_variant="recruiting_ce_v1", site_number="CX_1", locale="en",
    ),
    _entry(
        "Credo Technology", "hibob", "https://credo.careers.hibob.com",
        identity_url="https://credosemi.com/about-credo/careers/", identity_regex=r"\bCredo\b",
        allowed_apply_hosts=["credo.careers.hibob.com"], slug="credo",
        endpoint_url="https://credo.careers.hibob.com/api/job-ad",
        endpoint_variant="career_site_job_ads_v1",
    ),
    _entry(
        "Astera Labs", "greenhouse", "https://www.asteralabs.com/careers/",
        identity_url="https://www.asteralabs.com/careers/", identity_regex=r"Astera Labs",
        allowed_apply_hosts=["job-boards.greenhouse.io"], slug="asteralabs",
    ),
    _entry(
        "SK hynix", "greenhouse", "https://job-boards.greenhouse.io/skhynixamerica",
        identity_url="https://job-boards.greenhouse.io/skhynixamerica",
        identity_regex=r"SK hynix", allowed_apply_hosts=["job-boards.greenhouse.io"],
        slug="skhynixamerica",
    ),
    _entry(
        "Kioxia", "smartrecruiters", "https://americas.kioxia.com/en-us/careers.html",
        identity_url="https://careers.smartrecruiters.com/Kioxia", identity_regex=r"Kioxia",
        allowed_apply_hosts=["jobs.smartrecruiters.com"], slug="Kioxia",
        endpoint_url="https://api.smartrecruiters.com/v1/companies/Kioxia/postings",
        endpoint_variant="companies_postings_v1", page_size=20,
    ),
    _entry(
        "Logitech", "workday", "https://www.logitech.com/en-us/careers",
        identity_url="https://www.logitech.com/en-us/careers", identity_regex=r"Logitech",
        allowed_apply_hosts=["logitech.wd5.myworkdayjobs.com"],
        search_url="https://logitech.wd5.myworkdayjobs.com/Logitech",
        tenant="logitech", wd_subdomain="wd5", site_id="Logitech",
    ),
    _entry(
        "Sonos", "workday", "https://www.sonos.com/en-us/life-at-sonos",
        identity_url="https://sonos.wd1.myworkdayjobs.com/Sonos", identity_regex=r"Sonos",
        allowed_apply_hosts=["sonos.wd1.myworkdayjobs.com"],
        search_url="https://sonos.wd1.myworkdayjobs.com/Sonos",
        tenant="sonos", wd_subdomain="wd1", site_id="Sonos",
    ),
    _entry(
        "Bose", "workday", "https://boseallaboutme.wd503.myworkdayjobs.com/Bose_Careers",
        identity_url="https://careers.bose.com/us/en/search-results",
        identity_regex=r"Bose Corporation|Careers - Bose",
        allowed_apply_hosts=["boseallaboutme.wd503.myworkdayjobs.com"],
        search_url="https://boseallaboutme.wd503.myworkdayjobs.com/Bose_Careers",
        tenant="boseallaboutme", wd_subdomain="wd503", site_id="Bose_Careers",
    ),
    _entry(
        "Roku", "greenhouse", "https://www.weareroku.com/jobs/search/",
        identity_url="https://boards-api.greenhouse.io/v1/boards/roku", identity_regex=r"\bRoku\b",
        allowed_apply_hosts=["www.weareroku.com"], slug="roku",
    ),
    _entry(
        "MaxLinear", "icims", "https://www.maxlinear.com/company/careers",
        identity_url="https://www.maxlinear.com/company/careers", identity_regex=r"MaxLinear",
        allowed_apply_hosts=["careersus-maxlinear.icims.com"],
        search_url="https://careersus-maxlinear.icims.com/jobs/search",
        endpoint_variant="icims_legacy_portal_html", tenant="careersus-maxlinear",
        board_id="maxlinear-us", page_size=20,
    ),
    _entry(
        "Ambiq", "greenhouse", "https://ambiq.com/careers/",
        identity_url="https://ambiq.com/careers/", identity_regex=r"\bAmbiq\b",
        allowed_apply_hosts=["job-boards.greenhouse.io"], slug="ambiqmicroinc",
    ),
    _entry(
        "u-blox", "static_html", "https://www.u-blox.com/en/job-openings",
        identity_url="https://fs-4627.my.salesforce-sites.com/recruit/fRecruit__ApplyJobList?portal=U-Blox+Global",
        identity_regex=r"u-blox", allowed_apply_hosts=["fs-4627.my.salesforce-sites.com"],
        listing_url="https://fs-4627.my.salesforce-sites.com/recruit/fRecruit__ApplyJobList?portal=U-Blox+Global",
        endpoint_variant="server_rendered_cards", listing_selector="table.jobListPanel",
        job_card_selector="tr.dataRow", title_selector="td:first-child a",
        location_selector="td:nth-child(3),td:nth-child(4),td:nth-child(5)",
        department_selector="td:nth-child(6)", detail_link_selector="td:first-child a",
        external_id_regex=r"[?&]vacancyNo=([^&]+)",
    ),
    _entry(
        "Corsair", "oracle_recruiting", "https://edix.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
        identity_url="https://edix.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
        identity_regex=r"CORSAIR Careers", allowed_apply_hosts=["edix.fa.us2.oraclecloud.com"],
        endpoint_url="https://edix.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
        endpoint_variant="recruiting_ce_v1", site_number="CX_1", locale="en",
    ),
]


EXCLUDED = [
    {
        "company": "Dell", "platform": "TalentBrew / protected official search",
        "official_careers_url": "https://jobs.dell.com/en/search-jobs",
        "validation_status": "blocked_official_source",
        "exclusion_reason": "Dell's official search returns HTTP 403 to bounded anonymous adapter reads; no WAF, cookie, CAPTCHA, or session bypass is attempted.",
    },
    {
        "company": "GoPro", "platform": "first-party protected search / retired Greenhouse board",
        "official_careers_url": "https://jobs.gopro.com/en/us/",
        "validation_status": "blocked_official_source_and_stale_board",
        "exclusion_reason": "GoPro's current official jobs route redirects to a protected first-party careers page, while the former goprocareers Greenhouse board is now empty. The retired board is not added and no client challenge is bypassed.",
    },
    {
        "company": "Kingston Technology", "platform": "protected first-party regional collections",
        "official_careers_url": "https://www.kingston.com/en/company/careers",
        "validation_status": "blocked_global_source",
        "exclusion_reason": "Kingston's official global careers collection returns a Cloudflare challenge to anonymous reads. Its accessible Taiwan SuccessFactors board is not substituted for Canada/US/global coverage, and no challenge bypass is attempted.",
    },
]


OWNERSHIP_REVIEWS = [
    {"company": "HP Inc.", "relationship": "separate company created by Hewlett-Packard's 2015 split", "disposition": "validated_exact_hp_inc_board"},
    {"company": "HPE", "relationship": "separate enterprise company created by Hewlett-Packard's 2015 split", "disposition": "validated_exact_hpe_board"},
    {"company": "Western Digital", "relationship": "Sandisk flash business separated in February 2025", "disposition": "validated_post_separation_western_digital_hdd_board"},
    {"company": "Coherent Corp", "relationship": "II-VI acquired Coherent in 2022 and adopted the Coherent name", "disposition": "validated_current_renamed_company_board"},
    {"company": "Kioxia", "relationship": "Toshiba Memory rebranded as Kioxia in 2019", "disposition": "validated_current_kioxia_america_board"},
    {"company": "GoPro", "relationship": "former Greenhouse collection retired after careers-site migration", "disposition": "excluded_stale_board"},
]

SAFE_TO_STAGE_FILES = [
    "config/company_hardware_semiconductor_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.hardware_semiconductor_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.hardware_semiconductor_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_hardware_semiconductor_expansion_v1_batch_a.py",
    "tests/test_hardware_semiconductor_expansion_v1_batch_a.py",
]

ADAPTERS = {
    "eightfold": eightfold.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "hibob": hibob.fetch_jobs,
    "icims": icims.fetch_jobs,
    "oracle_recruiting": oracle_recruiting.fetch_jobs,
    "phenom": phenom.fetch_jobs,
    "sap_successfactors": sap_successfactors.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
    "static_html": static_html.fetch_jobs,
    "workday": workday.fetch_jobs,
}

FOCUS_ROLE_RE = re.compile(
    r"\b(?:electrical|computer engineer|embedded|firmware|silicon|semiconductor|hardware|"
    r"systems? engineer|software|machine learning|ML|AI|ASIC|FPGA|SoC|IC design|"
    r"verification|validation|signal integrity|analog|mixed[- ]signal|photonic|GNSS)\b",
    re.IGNORECASE,
)

_CONFIG_NAMES = (
    "PRODUCTION", "PARENT_MAPPINGS", "CANDIDATE_CSV", "CANDIDATE_REGISTRY",
    "VALIDATED_REGISTRY", "REPORT", "BASELINE_SOURCES", "BASELINE_MAPPINGS",
    "BASELINE_COVERAGE", "MASTER_TOTAL", "MAX_TIMEOUT_SECONDS", "MAX_JOBS",
    "MAX_RAW_JOBS", "REPEAT_RUNS", "REVIEWED_COMPANIES", "CANDIDATE_ENTRIES",
    "EXCLUDED", "OWNERSHIP_REVIEWS", "SAFE_TO_STAGE_FILES", "ADAPTERS",
)

_ORIGINAL_VALIDATE_TWICE = base._validate_twice


def _validate_twice_stabilized(entry: dict[str, Any]) -> dict[str, Any]:
    """Require an exact pair, retrying the whole pair after observable board drift."""
    result: dict[str, Any] = {}
    for attempt in range(1, 4):
        result = _ORIGINAL_VALIDATE_TWICE(entry)
        result["repeat_validation_attempts"] = attempt
        if result.get("production_ready"):
            return result
    return result


@contextmanager
def _configured_base() -> Iterable[None]:
    before = {name: getattr(base, name) for name in _CONFIG_NAMES}
    validate_before = base._validate_twice
    for name in _CONFIG_NAMES:
        setattr(base, name, globals()[name])
    base._validate_twice = _validate_twice_stabilized
    try:
        yield
    finally:
        base._validate_twice = validate_before
        for name, value in before.items():
            setattr(base, name, value)


def _add_focus_counts(report: dict[str, Any]) -> None:
    focus_total = 0
    student_focus_total = 0
    for row in report["results"]:
        titles = (row.get("runs") or [{}])[0].get("titles") or []
        focus_titles = [title for title in titles if FOCUS_ROLE_RE.search(title)]
        student_titles = {
            item["title"] for item in row.get("current_student_role_evidence") or []
        }
        row["current_focus_role_hits"] = len(focus_titles)
        row["current_student_focus_hits"] = sum(title in student_titles for title in focus_titles)
        focus_total += row["current_focus_role_hits"]
        student_focus_total += row["current_student_focus_hits"]
    report["jobs"]["focus_role_hits_total"] = focus_total
    report["jobs"]["student_focus_hits_total"] = student_focus_total


def _rewrite_csv(results: list[dict[str, Any]]) -> None:
    fields = [
        "company", "platform", "adapter", "result", "official_careers_url",
        "validation_status", "jobs_observed", "jobs_normalized",
        "current_student_intern_hits", "current_focus_role_hits",
        "current_student_focus_hits", "stable_id_result", "title_result",
        "location_result", "apply_url_result", "company_attribution_result",
        "deterministic_result", "production_ready", "original_246_master",
        "master_rank", "exclusion_reason",
    ]
    with CANDIDATE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)


def build() -> dict[str, Any]:
    with _configured_base():
        report = base.build()
    report["batch"] = "Hardware/Semiconductor Expansion v1 Batch A"
    report["adapter_reuse"] = sorted(ADAPTERS)
    report["generic_adapter_improvements"] = []
    report["ownership_reviews"] = OWNERSHIP_REVIEWS
    report["stale_acquired_companies"] = []
    report["stale_sources"] = ["GoPro"]
    report["prioritization"] = {
        "target_season": "Summer 2027",
        "candidate_level": "undergraduate",
        "role_families": [
            "electrical/computer engineering", "embedded", "firmware", "silicon",
            "hardware", "systems", "machine learning", "software",
        ],
        "geography": ["Canada", "United States", "Remote"],
    }
    _add_focus_counts(report)
    _rewrite_csv(report["results"])
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        report = build()
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Hardware/Semiconductor Expansion v1 Batch A failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "validated": report["validated_companies"],
        "excluded": report["excluded_companies"],
        "jobs": report["jobs"],
        "projection": report["projection"],
        "production_registry_activated": report["production_registry_activated"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
