"""Build and validate Requested Company Expansion v3 Batch A.

The investigation starts from the exact 198-source production registry, skips
active companies, and writes only candidate, validated, inventory, and ignored
report artifacts.  It never activates sources or changes parent mappings,
persistence, student filters, dashboard code, or publication state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_calgary_student_employers_v1_batch_a as engine
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers import scanner
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


_ENGINE_BUILD_CANDIDATE_REGISTRY = engine.build_candidate_registry


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_requested_expansion_v3_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_a_validated.yaml"
REPORT = ROOT / "output" / "requested_expansion_v3_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 198
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 184
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Precision Drilling", "Parkland Corporation", "AltaGas", "MEG Energy",
    "Whitecap Resources", "Vermilion Energy", "Baytex Energy",
    "SECURE Energy Services", "Trican Well Service", "ExxonMobil", "SLB",
    "Baker Hughes", "Netflix", "BlackBerry/QNX", "CAE Inc.",
]

MASTER_RANKS = {
    "Netflix": 48,
    "SLB": 157,
    "Baker Hughes": 159,
    "Precision Drilling": 184,
}

OFFICIAL_CAREERS_URLS = {
    "Precision Drilling": "https://www.precisiondrilling.com/careers/",
    "Parkland Corporation": "https://www.parkland.ca/en/careers",
    "AltaGas": "https://www.altagas.ca/careers",
    "MEG Energy": "https://www.megenergy.com/",
    "Whitecap Resources": "https://www.wcap.ca/careers",
    "Vermilion Energy": "https://www.vermilionenergy.com/careers/",
    "Baytex Energy": "https://www.baytexenergy.com/careers/opportunities/",
    "SECURE Energy Services": "https://secure.ca/careers",
    "Trican Well Service": "https://tricanwellservice.com/careers/",
    "ExxonMobil": "https://corporate.exxonmobil.com/careers",
    "SLB": "https://careers.slb.com/",
    "Baker Hughes": "https://careers.bakerhughes.com/global/en",
    "Netflix": "https://jobs.netflix.com/",
    "BlackBerry/QNX": "https://www.blackberry.com/en/careers",
    "CAE Inc.": "https://www.cae.com/careers/",
}

CANADA_CALGARY_RELEVANCE = {
    "Precision Drilling": "Calgary-headquartered; the official source has Canada and US jobs plus a Student and New Grad category.",
    "Parkland Corporation": "Calgary-headquartered with Canada and US operations and new-graduate messaging; the official job board is blocked to anonymous validation.",
    "AltaGas": "Calgary-headquartered; its official parent board covers AltaGas in Canada and affiliated US businesses.",
    "MEG Energy": "Former Calgary-headquartered producer and now a Cenovus subsidiary; no independent current MEG job source remains validated.",
    "Whitecap Resources": "Calgary-headquartered Canadian producer with multiple current Calgary jobs on its exact Workday board.",
    "Vermilion Energy": "Calgary-headquartered global producer; the current sample is international and includes one student role outside Canada/US.",
    "Baytex Energy": "Calgary-headquartered with an official summer-student program, but the current collection is empty and exposes no verifiable job contract.",
    "SECURE Energy Services": "Calgary-headquartered; the exact Canada iCIMS board contains Alberta jobs and the careers site also links a separate US board.",
    "Trican Well Service": "Calgary-headquartered and Canada-focused with a summer-student program; the StartDate SPA does not expose verifiable job-specific links to existing adapters.",
    "ExxonMobil": "Independent global ExxonMobil source with US jobs and official internship pathways; Imperial-only coverage is not used as ExxonMobil coverage.",
    "SLB": "Independent global SLB source with US and Calgary filters plus early-career programs; current sampled internship hits are outside Canada/US.",
    "Baker Hughes": "Independent Baker Hughes global source with Canada/Calgary and internship evidence, but anonymous collection is blocked by Incapsula.",
    "Netflix": "Official internships are offered in the US and selected other countries and the source includes remote roles; its embedded source is rejected by the existing CAPTCHA safety guard.",
    "BlackBerry/QNX": "Exact QNX Workday board under BlackBerry, with Canadian and US roles; no shared Student-board jobs are attributed to QNX without exact evidence.",
    "CAE Inc.": "Canadian aviation-technology employer with Canada and US roles; the current sample contains US and international student/intern jobs.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Precision Drilling": "https://jobs.precisiondrilling.com/go/Student-and-New-Grad/8968500/",
    "Parkland Corporation": "https://www.parkland.ca/en/careers",
    "AltaGas": "https://www.altagas.ca/careers",
    "MEG Energy": "https://www.megenergy.com/",
    "Whitecap Resources": "https://www.wcap.ca/careers",
    "Vermilion Energy": "https://www.vermilionenergy.com/careers/",
    "Baytex Energy": "https://www.baytexenergy.com/careers/opportunities/",
    "SECURE Energy Services": "https://secure.ca/careers",
    "Trican Well Service": "https://tricanwellservice.com/careers/",
    "ExxonMobil": "https://corporate.exxonmobil.com/careers/internship-opportunities",
    "SLB": "https://careers.slb.com/early-careers/internships",
    "Baker Hughes": "https://careers.bakerhughes.com/global/en/early-careers-internships",
    "Netflix": "https://jobs.netflix.com/careers/internships",
    "BlackBerry/QNX": "https://www.blackberry.com/en/careers",
    "CAE Inc.": "https://www.cae.com/careers/students",
}

COMPATIBILITY = {
    "Precision Drilling": "Canada+US compatible; student/new-grad category verified; 0 current sampled student/intern titles.",
    "Parkland Corporation": "Canada+US compatible in principle; current student-job verification blocked.",
    "AltaGas": "Canada+US compatible; 0 current sampled student/intern titles.",
    "MEG Energy": "Not compatible as an independent source after acquisition by Cenovus.",
    "Whitecap Resources": "Canada compatible; 0 current sampled student/intern titles.",
    "Vermilion Energy": "Canada/global compatible; 1 current sampled student title in the Netherlands, none in Canada/US/Remote.",
    "Baytex Energy": "Canada/Calgary student program verified; current source has no jobs and no observable stable job contract.",
    "SECURE Energy Services": "Canada compatible with a separate US board; 0 current sampled student/intern titles.",
    "Trican Well Service": "Canada student program verified; current stable apply-link contract unsupported.",
    "ExxonMobil": "US/global and internship compatible; 0 current sampled student/intern titles; Imperial is not substituted.",
    "SLB": "US/Canada/global and internship compatible; 2 current sampled internship titles, both outside Canada/US/Remote.",
    "Baker Hughes": "Canada/US/global and internship compatible in principle; current anonymous collection blocked.",
    "Netflix": "US and remote-role compatible; internship program verified; current safe normalization blocked.",
    "BlackBerry/QNX": "Canada+US compatible; exact QNX board has 0 current sampled student/intern titles.",
    "CAE Inc.": "Canada+US compatible; 2 current sampled student/intern titles, including 1 US internship.",
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
    _entry(
        "Precision Drilling", "sap_successfactors", OFFICIAL_CAREERS_URLS["Precision Drilling"],
        identity_regex=r"Precision Drilling Career Site|Precision Drilling",
        allowed_apply_hosts=["jobs.precisiondrilling.com"],
        identity_url="https://jobs.precisiondrilling.com/search/",
        search_url="https://jobs.precisiondrilling.com/search/", endpoint_variant="rmk_tile",
    ),
    _entry(
        "AltaGas", "workday", OFFICIAL_CAREERS_URLS["AltaGas"], identity_regex=r"AltaGas",
        allowed_apply_hosts=["wgl.wd5.myworkdayjobs.com"],
        identity_url="https://wgl.wd5.myworkdayjobs.com/AltaGas",
        tenant="wgl", wd_subdomain="wd5", site_id="AltaGas",
        search_url="https://wgl.wd5.myworkdayjobs.com/AltaGas",
    ),
    _entry(
        "Whitecap Resources", "workday", OFFICIAL_CAREERS_URLS["Whitecap Resources"],
        identity_regex=r"Whitecap", allowed_apply_hosts=["wcap.wd10.myworkdayjobs.com"],
        identity_url="https://wcap.wd10.myworkdayjobs.com/Whitecap_Careers",
        tenant="wcap", wd_subdomain="wd10", site_id="Whitecap_Careers",
        search_url="https://wcap.wd10.myworkdayjobs.com/Whitecap_Careers",
    ),
    _entry(
        "Vermilion Energy", "workday", OFFICIAL_CAREERS_URLS["Vermilion Energy"],
        identity_regex=r"Vermilion", allowed_apply_hosts=["vermilionenergy.wd10.myworkdayjobs.com"],
        identity_url="https://vermilionenergy.wd10.myworkdayjobs.com/VEI_EXTERNAL",
        tenant="vermilionenergy", wd_subdomain="wd10", site_id="VEI_EXTERNAL",
        search_url="https://vermilionenergy.wd10.myworkdayjobs.com/VEI_EXTERNAL",
    ),
    _entry(
        "SECURE Energy Services", "icims", OFFICIAL_CAREERS_URLS["SECURE Energy Services"],
        identity_regex=r"SECURE Careers|SECURE", allowed_apply_hosts=["careers-canada-secure.icims.com"],
        identity_url="https://careers-canada-secure.icims.com/jobs/search?hashed=-625882119",
        search_url="https://careers-canada-secure.icims.com/jobs/search?hashed=-625882119",
        endpoint_variant="icims_legacy_portal_html", tenant="secure",
        board_id="secure-canada", page_size=20,
    ),
    _entry(
        "ExxonMobil", "sap_successfactors", OFFICIAL_CAREERS_URLS["ExxonMobil"],
        identity_regex=r"Careers at ExxonMobil|ExxonMobil", allowed_apply_hosts=["jobs.exxonmobil.com"],
        identity_url="https://jobs.exxonmobil.com/search/",
        search_url="https://jobs.exxonmobil.com/search/", endpoint_variant="rmk_table",
    ),
    _entry(
        "SLB", "eightfold", "https://apply.slb.com/careers", identity_regex=r"\bSLB\b",
        allowed_apply_hosts=["apply.slb.com"], identity_url="https://apply.slb.com/careers",
        endpoint_url="https://apply.slb.com/api/pcsx/search", endpoint_variant="pcsx",
        organization_id="slb.com",
    ),
    _entry(
        "BlackBerry/QNX", "workday", OFFICIAL_CAREERS_URLS["BlackBerry/QNX"],
        identity_regex=r"QNX software, a subsidiary of BlackBerry|BlackBerry.{0,80}QNX",
        allowed_apply_hosts=["bb.wd3.myworkdayjobs.com"],
        identity_url="https://bb.wd3.myworkdayjobs.com/QNX",
        tenant="bb", wd_subdomain="wd3", site_id="QNX",
        search_url="https://bb.wd3.myworkdayjobs.com/QNX",
    ),
    _entry(
        "CAE Inc.", "workday", OFFICIAL_CAREERS_URLS["CAE Inc."],
        identity_regex=r"Welcome to CAE|One CAE", allowed_apply_hosts=["cae.wd3.myworkdayjobs.com"],
        identity_url="https://cae.wd3.myworkdayjobs.com/career",
        tenant="cae", wd_subdomain="wd3", site_id="career",
        search_url="https://cae.wd3.myworkdayjobs.com/career",
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
        "Parkland Corporation", "Radancy/TalentBrew TTC portal",
        "https://parkland-careers.ttcportals.com/", "blocked",
        "The official Parkland page links this board, but the board returns a Cloudflare challenge (HTTP 403) to bounded anonymous requests. No challenge bypass is attempted.",
        r"Parkland|Careers",
    ),
    _excluded(
        "MEG Energy", "no independent current ATS", "https://www.megenergy.com/", "parent_source_required",
        "MEG became a Cenovus subsidiary on November 13, 2025. No independent current MEG job collection with stable IDs and exact MEG apply URLs is available, and no unvalidated Cenovus parent mapping is inferred.",
        r"MEG Energy|Cenovus Energy",
    ),
    _excluded(
        "Baytex Energy", "WordPress static HTML", OFFICIAL_CAREERS_URLS["Baytex Energy"],
        "no_observable_job_contract",
        "The official page currently states that there are no job or summer-student postings. With no current detail page, stable ID, or job-specific apply URL, a reusable extraction contract cannot be verified.",
        r"Baytex|Career Opportunities",
    ),
    _excluded(
        "Trican Well Service", "StartDate/HireGround SPA", "https://tricanwellservice.startdate.ca/#/job-list/en",
        "unsupported_public_api",
        "The official StartDate SPA loads jobs from a public Origin-dependent API, but rendered cards expose only click handlers rather than stable job-specific hrefs. Existing adapters cannot verify official apply URLs without a new reviewed contract.",
        r"Trican|Careers",
    ),
    _excluded(
        "Baker Hughes", "Phenom", "https://careers.bakerhughes.com/global/en/search-results",
        "blocked",
        "The independent Baker Hughes Phenom search returns an Incapsula challenge to bounded anonymous validation. It is not inferred from another oilfield-services employer and no anti-bot bypass is attempted.",
        r"Baker Hughes|Career Opportunities",
    ),
    _excluded(
        "Netflix", "Eightfold embedded careers", "https://explore.jobs.netflix.net/careers",
        "blocked",
        "The official page embeds current Eightfold positions, but its PCSX API returns HTTP 403 and the embedded state advertises reCAPTCHA. The existing embedded-data safety guard rejects the page, and that guard is not weakened.",
        r"Netflix|Careers",
    ),
]

PLATFORM_NOTES = {
    "Precision Drilling": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "Parkland Corporation": ("Radancy/TalentBrew TTC portal", "none"),
    "AltaGas": ("Workday", "workday"), "MEG Energy": ("No independent current ATS", "none"),
    "Whitecap Resources": ("Workday", "workday"),
    "Vermilion Energy": ("Workday", "workday"),
    "Baytex Energy": ("WordPress static HTML", "none"),
    "SECURE Energy Services": ("iCIMS legacy portal", "icims"),
    "Trican Well Service": ("StartDate/HireGround SPA", "none"),
    "ExxonMobil": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "SLB": ("Eightfold PCSX", "eightfold"),
    "Baker Hughes": ("Phenom", "none"), "Netflix": ("Eightfold embedded careers", "none"),
    "BlackBerry/QNX": ("Workday QNX site", "workday"), "CAE Inc.": ("Workday", "workday"),
}

CSV_FIELDS = [
    "company", "official_careers_url", "listing_url", "calgary_alberta_relevance",
    "canada_us_remote_student_compatibility", "platform", "adapter", "result",
    "jobs_normalized", "current_student_intern_hits", "current_student_role_evidence",
    "student_program_evidence_url", "exclusion_reason", "original_246_master", "master_rank",
    "validation_status", "stable_id_result", "apply_url_result", "allowed_apply_hosts",
    "job_count_run_1", "job_count_run_2",
]


def _configure_engine() -> None:
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
        "CALGARY_RELEVANCE": CANADA_CALGARY_RELEVANCE,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES, "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES, "CSV_FIELDS": CSV_FIELDS,
    }
    for name, value in values.items():
        setattr(engine, name, value)
    engine.build_candidate_registry = _build_candidate_registry_for_engine


source_key = engine.source_key
_duplicates = engine._duplicates
_verify_jobs = engine._verify_jobs
_status_for_error = engine._status_for_error


def reconcile() -> list[dict[str, Any]]:
    _configure_engine()
    return engine.reconcile()


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY,
                             additions: Optional[Iterable[Mapping[str, Any]]] = None) -> bytes:
    _configure_engine()
    return _ENGINE_BUILD_CANDIDATE_REGISTRY(production, destination, additions)


def _build_candidate_registry_for_engine() -> bytes:
    """Bind the reused engine to this batch's paths, avoiding captured defaults."""
    return _ENGINE_BUILD_CANDIDATE_REGISTRY(PRODUCTION, CANDIDATE_REGISTRY, CANDIDATE_ENTRIES)


def _run_direct(entry: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    _configure_engine()
    return engine._run_direct(entry, timeout)


def _finalize(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    _configure_engine()
    result = engine._finalize(entry, runs)
    result["canada_us_remote_student_compatibility"] = COMPATIBILITY[str(entry["name"])]
    return result


def _inventory_bytes(results: Iterable[Mapping[str, Any]]) -> bytes:
    by_name = {str(item["company"]): item for item in results}
    entries = {str(item["name"]): item for item in CANDIDATE_ENTRIES}
    reviews = {str(item["company"]): item for item in EXCLUDED_REVIEWS}
    rows = []
    for name in REVIEWED_COMPANIES:
        result = by_name[name]; source = entries.get(name) or reviews[name]; runs = list(result.get("runs") or [])
        rows.append({
            "company": name, "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "calgary_alberta_relevance": CANADA_CALGARY_RELEVANCE[name],
            "canada_us_remote_student_compatibility": COMPATIBILITY[name],
            "platform": PLATFORM_NOTES[name][0], "adapter": PLATFORM_NOTES[name][1],
            "result": result["result"], "jobs_normalized": result["jobs_normalized"],
            "current_student_intern_hits": result["current_student_intern_hits"],
            "current_student_role_evidence": "; ".join(hit["title"] for hit in result["current_student_role_evidence"]),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name],
            "exclusion_reason": result["exclusion_reason"],
            "original_246_master": str(name in MASTER_RANKS).lower(), "master_rank": MASTER_RANKS.get(name, ""),
            "validation_status": result["validation_status"], "stable_id_result": result["stable_id_result"],
            "apply_url_result": result["apply_url_result"],
            "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
            "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
            "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
        })
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    _configure_engine()
    production_before = PRODUCTION.read_bytes(); mappings_before = PARENT_MAPPINGS.read_bytes()
    payload = engine.validate(timeout)
    for result in payload["results"]:
        result["canada_us_remote_student_compatibility"] = COMPATIBILITY[str(result["company"])]
    payload["candidate_registry_has_exact_198_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_186_entry_prefix"
    )
    payload["validated_registry_has_exact_198_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_186_entry_prefix"
    )
    totals = payload["totals"]
    totals["validated_outside_original_master"] = totals.pop("calgary_focused_validated_outside_master")
    totals["validated_outside_original_master_names"] = totals.pop("calgary_focused_validated_outside_master_names")
    totals["projected_canonical_production_count"] = totals["projected_production_sources"]
    totals["projected_original_master_coverage_ratio"] = (
        f"{totals['projected_original_master_coverage']}/{totals['projected_original_master_total']}"
    )
    totals["statuses"] = dict(sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items()))
    _write_bytes(CANDIDATE_CSV, _inventory_bytes(payload["results"]))
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
            reconcile(); build_candidate_registry()
            content = engine._registry_bytes(PRODUCTION, [])
            _write_bytes(VALIDATED_REGISTRY, content)
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
