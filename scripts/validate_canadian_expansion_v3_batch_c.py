"""Build and validate Canadian Expansion v3 Batch C.

The investigation starts from the exact 216-source production registry, skips
active companies, and writes only candidate, validated, inventory, and ignored
report artifacts. It never activates sources or changes parent mappings,
persistence, student filters, dashboard code, or publication state.
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

PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_canadian_expansion_v3_batch_c_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.canadian_expansion_v3_batch_c_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.canadian_expansion_v3_batch_c_validated.yaml"
REPORT = ROOT / "output" / "canadian_expansion_v3_batch_c_validation.json"

HISTORICAL_PRODUCTION_COUNT = 216
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 186
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Clio", "Benevity", "SMART Technologies", "CoolIT Systems", "Attabotics",
    "Hexagon Autonomy & Positioning / NovAtel", "Geotab", "Miovision",
    "Ballard Power Systems", "ATS Corporation", "Linamar", "1Password", "D2L",
    "Hootsuite", "PointClickCare",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "Clio": "https://www.clio.com/about/careers/",
    "Benevity": "https://benevity.com/join-the-team",
    "SMART Technologies": "https://www.smarttech.com/careers",
    "CoolIT Systems": "https://www.coolitsystems.com/contact/careers/",
    "Attabotics": "https://attabotics.com/careers/",
    "Hexagon Autonomy & Positioning / NovAtel": "https://www2.novatel.com/join-us",
    "Geotab": "https://careers.geotab.com/",
    "Miovision": "https://miovision.com/company/careers/",
    "Ballard Power Systems": "https://www.ballard.com/careers/",
    "ATS Corporation": "https://atsautomation.com/working-at-ats/",
    "Linamar": "https://www.linamar.com/career-development/",
    "1Password": "https://1password.com/careers",
    "D2L": "https://www.d2l.com/careers/jobs/",
    "Hootsuite": "https://careers.hootsuite.com/jobs/search",
    "PointClickCare": "https://pointclickcare.com/life-at-pointclickcare/",
}

CANADA_RELEVANCE = {
    "Clio": "Clio is the brand of Themis Solutions Inc.; its official site identifies Vancouver as global headquarters and lists Calgary, Toronto, Vancouver, and Remote - Canada.",
    "Benevity": "Benevity's exact company board includes Calgary, Toronto, Vancouver, and Canada-remote opportunities.",
    "SMART Technologies": "The current official careers site and Dayforce tenant are SMART-branded; current corporate materials identify SMART Technologies ULC in Calgary. Foxconn ownership is not treated as a shared job source.",
    "CoolIT Systems": "The official Calgary-headquartered CoolIT Systems Inc. careers page links an exact CoolIT UKG board. Ecolab closed its acquisition in July 2026, but no Ecolab jobs are inferred as CoolIT jobs.",
    "Attabotics": "Attabotics' official careers page remains online and links its exact Attabotics Lever board; the board currently returns an explicit empty collection.",
    "Hexagon Autonomy & Positioning / NovAtel": "Hexagon's official division page says Autonomy & Positioning includes NovAtel, AutonomouStuff, Veripos, and Antcom; the division's official NovAtel-hosted careers page links the exact hexagonpositioning iCIMS board, whose jobs retain division and NovAtel attribution.",
    "Geotab": "Geotab's exact careers source includes Oakville and Canada-remote roles and an official global co-op/internship program.",
    "Miovision": "Miovision's official Waterloo-origin careers page links its exact Ashby board with Kitchener and Canada-remote roles.",
    "Ballard Power Systems": "Ballard Power Systems Inc. is headquartered in Burnaby; its official careers page links an exact Paycor tenant and describes co-op, internship, and summer placements.",
    "ATS Corporation": "The current legal and public brand is ATS Corporation. The legacy atsautomation.com domain hosts the exact ATS Corporation SuccessFactors source; division-labelled jobs remain inside that official corporate source.",
    "Linamar": "Linamar Corporation's official site links its exact Oracle Recruiting site, which includes Guelph and other Canadian locations.",
    "1Password": "1Password is a Canadian-founded remote-first company; its exact board explicitly labels Canada and combined United States/Canada remote roles, providing job-level Canadian identity evidence.",
    "D2L": "D2L Corporation's exact board includes Kitchener, Toronto, Winnipeg, Vancouver, and Remote - Canada, plus a Canadian early-talent program.",
    "Hootsuite": "Hootsuite Media Inc.'s exact board includes Vancouver, Toronto, Montreal, Alberta, and other Canadian locations, including Canadian co-op roles.",
    "PointClickCare": "PointClickCare's exact Lever board includes Mississauga, Toronto, Remote- Canada, and Canada-labelled roles; the bounded fallback preserves exact Lever UUIDs and URLs.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Clio": OFFICIAL_CAREERS_URLS["Clio"],
    "Benevity": "https://jobs.ashbyhq.com/benevity",
    "SMART Technologies": OFFICIAL_CAREERS_URLS["SMART Technologies"],
    "CoolIT Systems": OFFICIAL_CAREERS_URLS["CoolIT Systems"],
    "Attabotics": OFFICIAL_CAREERS_URLS["Attabotics"],
    "Hexagon Autonomy & Positioning / NovAtel": OFFICIAL_CAREERS_URLS["Hexagon Autonomy & Positioning / NovAtel"],
    "Geotab": "https://careers.geotab.com/campus/",
    "Miovision": OFFICIAL_CAREERS_URLS["Miovision"],
    "Ballard Power Systems": OFFICIAL_CAREERS_URLS["Ballard Power Systems"],
    "ATS Corporation": "https://jobs.atsautomation.com/",
    "Linamar": OFFICIAL_CAREERS_URLS["Linamar"],
    "1Password": OFFICIAL_CAREERS_URLS["1Password"],
    "D2L": "https://www.d2l.com/careers/early-talent/",
    "Hootsuite": "https://careers.hootsuite.com/jobs/search",
    "PointClickCare": "https://jobs.lever.co/pointclickcare",
}

COMPATIBILITY = {
    "Clio": "Canada/US/remote compatible; Canadian hub and Remote - Canada evidence is explicit on official pages and job records.",
    "Benevity": "Canada/US/remote compatible; exact current board has Canadian locations but no current student title in the bounded collection.",
    "SMART Technologies": "Canada/US/global student roles may be compatible, but Dayforce has no reviewed production adapter.",
    "CoolIT Systems": "Calgary/US/global and internship compatible in principle, but UKG Pro has no reviewed production adapter.",
    "Attabotics": "Calgary/remote compatible in principle; exact board is currently empty, preventing current stable-ID/detail/apply verification.",
    "Hexagon Autonomy & Positioning / NovAtel": "Canada/US/global compatible on the exact division board; Calgary roles are explicit and attribution is limited to this official division source.",
    "Geotab": "Canada/US/remote compatible; official co-op/internship program accepts current post-secondary students.",
    "Miovision": "Canada/US/remote compatible; current exact board includes Canada Remote and Kitchener roles.",
    "Ballard Power Systems": "Canada/US/global and co-op/intern/summer compatible in principle, but Paycor has no reviewed production adapter.",
    "ATS Corporation": "Canada/US/global compatible; exact corporate source contains intern/co-op roles, including Canadian postings when current.",
    "Linamar": "Canada/US/global compatible; exact source includes Canadian jobs and student/apprenticeship pathways.",
    "1Password": "Canada/US/remote compatible; remote-first policy and job-level Canada labels provide explicit Canadian evidence, and paid internships are official.",
    "D2L": "Canada/US/remote compatible; Canadian co-op and new-graduate eligibility is explicit.",
    "Hootsuite": "Canada/US/remote compatible; current exact board includes Canadian co-op/intern roles.",
    "PointClickCare": "Canada/US/remote compatible; exact board includes job-level Canada and Remote- Canada evidence.",
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
    _entry("Clio", "workday", OFFICIAL_CAREERS_URLS["Clio"], identity_regex=r"ClioCareerSite|Clio",
           allowed_apply_hosts=["clio.wd3.myworkdayjobs.com"], identity_url="https://clio.wd3.myworkdayjobs.com/ClioCareerSite",
           tenant="clio", wd_subdomain="wd3", site_id="ClioCareerSite", search_url="https://clio.wd3.myworkdayjobs.com/ClioCareerSite"),
    _entry("Benevity", "ashby", OFFICIAL_CAREERS_URLS["Benevity"], identity_regex=r"Benevity",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://jobs.ashbyhq.com/benevity", slug="benevity"),
    _entry("Hexagon Autonomy & Positioning / NovAtel", "icims", OFFICIAL_CAREERS_URLS["Hexagon Autonomy & Positioning / NovAtel"],
           identity_regex=r"Autonomy\s*&amp;\s*Positioning|Autonomy\s*&\s*Positioning", allowed_apply_hosts=["careers-hexagonpositioning.icims.com"],
           identity_url=OFFICIAL_CAREERS_URLS["Hexagon Autonomy & Positioning / NovAtel"], search_url="https://careers-hexagonpositioning.icims.com/jobs/search",
           endpoint_variant="icims_legacy_portal_html", tenant="hexagonpositioning", board_id="hexagonpositioning-public", page_size=50),
    _entry("Geotab", "greenhouse", OFFICIAL_CAREERS_URLS["Geotab"], identity_regex=r"Geotab",
           allowed_apply_hosts=["job-boards.greenhouse.io", "boards.greenhouse.io"], identity_url="https://job-boards.greenhouse.io/geotab", slug="geotab"),
    _entry("Miovision", "ashby", OFFICIAL_CAREERS_URLS["Miovision"], identity_regex=r"Miovision",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://jobs.ashbyhq.com/miovision", slug="miovision"),
    _entry("ATS Corporation", "sap_successfactors", OFFICIAL_CAREERS_URLS["ATS Corporation"], identity_regex=r"ATS Corporation|Jobs at ATS",
           allowed_apply_hosts=["jobs.atsautomation.com"], identity_url="https://jobs.atsautomation.com/", search_url="https://jobs.atsautomation.com/search/", endpoint_variant="rmk_tile"),
    _entry("Linamar", "oracle_recruiting", "https://fa-epmd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_3001/jobs", identity_regex=r"Linamar Career Site|Linamar",
           allowed_apply_hosts=["fa-epmd-saasfaprod1.fa.ocs.oraclecloud.com"], identity_url="https://fa-epmd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_3001/jobs",
           endpoint_url="https://fa-epmd-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
           site_number="CX_3001", locale="en"),
    _entry("1Password", "ashby", OFFICIAL_CAREERS_URLS["1Password"], identity_regex=r"1Password",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://jobs.ashbyhq.com/1password", slug="1password"),
    _entry("D2L", "greenhouse", OFFICIAL_CAREERS_URLS["D2L"], identity_regex=r"D2L",
           allowed_apply_hosts=["www.d2l.com"], identity_url=OFFICIAL_CAREERS_URLS["D2L"], slug="d2l"),
    _entry("Hootsuite", "greenhouse", OFFICIAL_CAREERS_URLS["Hootsuite"], identity_regex=r"Hootsuite",
           allowed_apply_hosts=["job-boards.greenhouse.io"], identity_url="https://job-boards.greenhouse.io/hootsuite", slug="hootsuite"),
    _entry("PointClickCare", "static_html", OFFICIAL_CAREERS_URLS["PointClickCare"], identity_regex=r"PointClickCare",
           allowed_apply_hosts=["jobs.lever.co"], identity_url="https://jobs.lever.co/pointclickcare", listing_url="https://jobs.lever.co/pointclickcare",
           endpoint_variant="server_rendered_cards", listing_selector="div.postings-wrapper", job_card_selector="div.posting",
           title_selector="h5", detail_link_selector='a.posting-title[href*="/pointclickcare/"]',
           external_id_regex=r"/pointclickcare/([A-Za-z0-9-]+)", location_selector="span.location"),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {"company": company, "platform": platform, "adapter": "none", "listing_url": listing_url,
            "classification": classification, "exclusion_reason": reason, "identity_regex": identity_regex}


EXCLUDED_REVIEWS = [
    _excluded("SMART Technologies", "Dayforce", "https://jobs.dayforcehcm.com/smarttech/CANDIDATEPORTAL", "unsupported_platform",
              "The official SMART Technologies careers page links the exact smarttech Dayforce tenant, and current corporate evidence identifies SMART Technologies ULC. No reviewed Dayforce production adapter exists; Foxconn ownership is not used to infer a parent source.", r"SMART Technologies|meaningful connections"),
    _excluded("CoolIT Systems", "UKG Pro Recruiting", "https://recruiting.ultipro.ca/COO5001COLT/JobBoard/05857940-9028-4d4a-8544-26bd990d20d6/", "unsupported_platform",
              "The official CoolIT Systems Inc. careers page links the exact CoolIT UKG Pro board, but no reviewed UKG production adapter exists. Ecolab's July 2026 acquisition does not justify attributing Ecolab jobs to CoolIT.", r"CoolIT Systems|Innovative Team"),
    _excluded("Attabotics", "Lever", "https://jobs.lever.co/attabotics", "no_current_jobs_for_id_apply_validation",
              "Attabotics' official careers page and exact Lever board are live, but the anonymous API and server-rendered board currently contain zero postings. Current stable IDs, job details, and job-specific apply URLs therefore cannot be observed, so the source is not promoted.", r"Attabotics|Warehouse Automation Careers"),
    _excluded("Ballard Power Systems", "Paycor Recruiting", "https://recruitingbypaycor.com/career/iframe.action?clientId=8a7883d06937f5f7016978fec55861f7", "unsupported_platform",
              "Ballard's official careers page links its exact Paycor Recruiting tenant and verifies co-op, internship, and summer programs, but no reviewed Paycor production adapter exists.", r"Ballard|Fuel your future"),
]

PLATFORM_NOTES = {
    "Clio": ("Workday", "workday"), "Benevity": ("Ashby", "ashby"),
    "SMART Technologies": ("Dayforce", "none"), "CoolIT Systems": ("UKG Pro Recruiting", "none"),
    "Attabotics": ("Lever (current exact zero collection)", "none"),
    "Hexagon Autonomy & Positioning / NovAtel": ("iCIMS legacy division portal", "icims"),
    "Geotab": ("Greenhouse", "greenhouse"), "Miovision": ("Ashby", "ashby"),
    "Ballard Power Systems": ("Paycor Recruiting", "none"),
    "ATS Corporation": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "Linamar": ("Oracle Recruiting Cloud", "oracle_recruiting"), "1Password": ("Ashby", "ashby"),
    "D2L": ("Greenhouse with D2L job URLs", "greenhouse"), "Hootsuite": ("Greenhouse", "greenhouse"),
    "PointClickCare": ("Lever server-rendered board", "static_html"),
}

CSV_FIELDS = [
    "company", "official_careers_url", "listing_url", "canadian_identity_location_evidence",
    "canada_us_remote_student_compatibility", "platform", "adapter", "result", "jobs_normalized",
    "current_student_intern_hits", "current_student_role_evidence", "canadian_locations_represented",
    "student_program_evidence_url", "exclusion_reason", "original_246_master", "master_rank",
    "validation_status", "stable_id_result", "apply_url_result", "allowed_apply_hosts",
    "job_count_run_1", "job_count_run_2",
]

CANADIAN_LOCATION_RE = re.compile(
    r"\b(?:Canada|Canadian|CA-Remote|Canada Remote|Remote- Canada|Remote - Canada|Alberta|British Columbia|Ontario|Quebec|Québec|"
    r"Manitoba|Saskatchewan|Nova Scotia|New Brunswick|Newfoundland|Prince Edward Island|Northwest Territories|Yukon|Nunavut|"
    r"Calgary|Edmonton|Toronto|Vancouver|Ottawa|Montreal|Montréal|Waterloo|Kitchener|Burnaby|Mississauga|Oakville|Guelph|Winnipeg|"
    r"CA-AB-Calgary|CA-Home Based)\b|,\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT),\s*CA(?:,|\b)", re.IGNORECASE,
)


def _configure_engine() -> None:
    values = {
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS, "MASTER_WORKBOOK": MASTER_WORKBOOK,
        "CANDIDATE_CSV": CANDIDATE_CSV, "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY, "VALIDATED_REGISTRY": VALIDATED_REGISTRY,
        "REPORT": REPORT, "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS, "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT, "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS, "MAX_PAGES": MAX_PAGES,
        "MAX_JOBS": MAX_JOBS, "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES, "REVIEWED_COMPANIES": REVIEWED_COMPANIES,
        "MASTER_RANKS": MASTER_RANKS, "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS, "CALGARY_RELEVANCE": CANADA_RELEVANCE,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE, "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES,
        "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS, "PLATFORM_NOTES": PLATFORM_NOTES,
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
    return _ENGINE_BUILD_CANDIDATE_REGISTRY(PRODUCTION, CANDIDATE_REGISTRY, CANDIDATE_ENTRIES)


def _canadian_locations(jobs: Iterable[Any]) -> list[str]:
    values: dict[str, str] = {}
    for job in list(jobs)[:MAX_JOBS]:
        location = " ".join(str(job.location or "").split())
        if location and CANADIAN_LOCATION_RE.search(location):
            values.setdefault(location.casefold(), location)
    return [values[key] for key in sorted(values)]


def _student_hits(jobs: Iterable[Any]) -> list[dict[str, str]]:
    hits = []
    for job in jobs:
        if engine.STUDENT_ROLE_RE.search(str(job.title or "")):
            hits.append({"title": job.title, "location": job.location, "url": job.url})
        if len(hits) >= MAX_JOBS:
            break
    return hits


def _run_direct(entry: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    _configure_engine()
    started = time.monotonic()
    session = requests.Session()
    try:
        identity = engine._identity_check(entry, session, timeout)
        jobs = scanner.ADAPTERS[str(entry["platform"])](session, CompanyTarget.from_dict(dict(entry)), timeout)
        signature = engine._verify_jobs(jobs, entry)
        pagination = dict(engine._pagination(session, str(entry["platform"])))
        zero = not signature and bool(pagination.get("zero_job_contract") or pagination.get("stop_reason") == "validated_zero_jobs")
        if not signature and not zero:
            raise AdapterError("Candidate did not conclusively prove a zero-job collection")
        return {"runtime_seconds": round(time.monotonic() - started, 3), "success": True,
                "jobs_observed": len(jobs), "jobs_normalized": len(signature),
                "stable_ids": [item[0] for item in signature], "apply_urls": [item[1] for item in signature],
                "student_hits": _student_hits(jobs), "canadian_locations": _canadian_locations(jobs),
                "zero_job_contract": zero, "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
                "identity": identity, "pagination_stop_reason": pagination.get("stop_reason", "adapter_bounded_collection")}
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        return {"runtime_seconds": round(time.monotonic() - started, 3), "success": False,
                "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [], "apply_urls": [], "student_hits": [],
                "canadian_locations": [], "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
                "status": _status_for_error(exc), "error": str(exc)}
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
        rows.append({"company": name, "official_careers_url": OFFICIAL_CAREERS_URLS[name],
                     "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
                     "canadian_identity_location_evidence": CANADA_RELEVANCE[name],
                     "canada_us_remote_student_compatibility": COMPATIBILITY[name], "platform": PLATFORM_NOTES[name][0],
                     "adapter": PLATFORM_NOTES[name][1], "result": result["result"], "jobs_normalized": result["jobs_normalized"],
                     "current_student_intern_hits": result["current_student_intern_hits"],
                     "current_student_role_evidence": "; ".join(hit["title"] for hit in result["current_student_role_evidence"]),
                     "canadian_locations_represented": " | ".join(result.get("canadian_locations_represented") or []),
                     "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name], "exclusion_reason": result["exclusion_reason"],
                     "original_246_master": str(name in MASTER_RANKS).lower(), "master_rank": MASTER_RANKS.get(name, ""),
                     "validation_status": result["validation_status"], "stable_id_result": result["stable_id_result"],
                     "apply_url_result": result["apply_url_result"], "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
                     "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
                     "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else ""})
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
    previous_run_direct = engine._run_direct
    engine._run_direct = _run_direct
    try:
        payload = engine.validate(timeout)
    finally:
        engine._run_direct = previous_run_direct
    for result in payload["results"]:
        result["canadian_identity_location_evidence"] = CANADA_RELEVANCE[str(result["company"])]
        result["canada_us_remote_student_compatibility"] = COMPATIBILITY[str(result["company"])]
        runs = list(result.get("runs") or [])
        result["canadian_locations_represented"] = list(runs[0].get("canadian_locations") or []) if runs else []
    payload["candidate_registry_has_exact_216_entry_prefix"] = payload.pop("candidate_registry_has_exact_186_entry_prefix")
    payload["validated_registry_has_exact_216_entry_prefix"] = payload.pop("validated_registry_has_exact_186_entry_prefix")
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
    parser.add_argument("--build-only", action="store_true"); parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.build_only:
            reconcile(); build_candidate_registry(); _write_bytes(VALIDATED_REGISTRY, engine._registry_bytes(PRODUCTION, []))
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Canadian expansion v3 Batch C validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
