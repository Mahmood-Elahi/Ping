"""Build and validate Requested Company Expansion v3 Batch E (Canada/Calgary)."""

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
CANDIDATE_CSV = ROOT / "config" / "company_requested_expansion_v3_batch_e_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_e_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_e_validated.yaml"
REPORT = ROOT / "output" / "requested_expansion_v3_batch_e_validation.json"

HISTORICAL_PRODUCTION_COUNT = 243
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 186
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Gibson Energy", "Strathcona Resources", "Tartigrade Ltd", "HV Logic Ltd",
    "Chemelex", "Copperleaf", "SensorUp", "mCloud Technologies", "BeWhere",
    "Rigstar Industrial Telecom", "Semtech", "Pason Systems", "Parkland Corporation",
    "Trican Well Service", "Blackline Safety",
]
MASTER_RANKS: dict[str, int] = {}
RECOVERY_TARGETS = {"Pason Systems", "Parkland Corporation", "Trican Well Service", "Blackline Safety"}

OFFICIAL_CAREERS_URLS = {
    "Gibson Energy": "https://www.gibsonenergy.com/careers/",
    "Strathcona Resources": "https://www.strathconaresources.com/careers/",
    "Tartigrade Ltd": "https://www.tartigrade.ca/career/",
    "HV Logic Ltd": "https://www.hvlogic.com/",
    "Chemelex": "https://jobs.dayforcehcm.com/chemelex/CANDIDATEPORTAL",
    "Copperleaf": "https://www.copperleaf.com/careers",
    "SensorUp": "https://www.sensorup.com/careers",
    "mCloud Technologies": "https://mcloudcorp.com/careers",
    "BeWhere": "https://bewhere.com/",
    "Rigstar Industrial Telecom": "https://www.rigstar.ca/",
    "Semtech": "https://www.semtech.com/careers",
    "Pason Systems": "https://pason.com/careers/",
    "Parkland Corporation": "https://www.parkland.ca/en/careers",
    "Trican Well Service": "https://tricanwellservice.com/careers/",
    "Blackline Safety": "https://careers.blacklinesafety.com/home",
}

CANADA_RELEVANCE = {
    "Gibson Energy": "Exact Gibson Energy board; current jobs include Calgary and Edmonton co-op roles plus other Alberta work.",
    "Strathcona Resources": "Exact Strathcona Resources Workday board; Calgary-headquartered Canadian energy employer with Alberta operations and a documented student program.",
    "Tartigrade Ltd": "Exact Calgary family-office identity, but its official Wix career route exposes no current job collection, stable IDs, or job-specific apply URLs.",
    "HV Logic Ltd": "Exact Calgary engineering company site has no official careers collection; third-party internship advertisements are not accepted as attribution.",
    "Chemelex": "Chemelex has Canadian operations and an exact Dayforce tenant. It is the former nVent Thermal Management business acquired by Brookfield, but no parent mapping is inferred.",
    "Copperleaf": "Copperleaf is Vancouver-founded and was acquired by IFS in August 2024. Its former careers URL now leads to IFS's mixed SmartRecruiters board; no parent mapping is inferred.",
    "SensorUp": "Current official pages identify SensorUp Inc. and Calgary headquarters, with a remote-first Canada/US model. No acquisition or parent relationship is inferred, and Bentley's sensemetrics acquisition is not attributed to SensorUp.",
    "mCloud Technologies": "The current site is branded mCloudTech.ai and says there are no open positions; the requested legacy identity has no current job-specific contract.",
    "BeWhere": "BeWhere Inc. is a Canadian operating company and wholly owned subsidiary of BeWhere Holdings Inc.; its official site exposes no careers collection or shared parent board.",
    "Rigstar Industrial Telecom": "The Calgary company rebranded as RSG Telecom in March 2024. The former domain now resolves to unrelated content, so it is not treated as an official successor careers source.",
    "Semtech": "Exact Semtech corporate Workday board, validated independently. Semtech's acquisition of Sierra Wireless does not make Sierra Wireless a Batch E target or evidence source.",
    "Pason Systems": "Exact Pason per-job attribution is required on the Calgary/Alberta/US UKG board; IWS-attributed postings are discarded.",
    "Parkland Corporation": "Calgary-headquartered Parkland links its exact TTC careers board, but that board is protected by Cloudflare and is not bypassed.",
    "Trican Well Service": "Exact Calgary-based Trican source; the official StartDate client now exposes anonymous numeric IDs and stable job-specific routes for Canadian roles.",
    "Blackline Safety": "Calgary-headquartered Blackline's main board remains Workable with Turnstile. Its legacy single-role form is not a stable job-specific alternative.",
}

COMPATIBILITY = {
    "Gibson Energy": "Canada student/co-op compatible; current Calgary and Edmonton co-op titles are verified.",
    "Strathcona Resources": "Canada student/co-op compatible; current exact-source jobs are checked and the official student program is documented.",
    "Tartigrade Ltd": "Calgary relevant in principle; no current official jobs or student contract can be normalized.",
    "HV Logic Ltd": "Calgary internship relevant in principle; only third-party role evidence is visible, so compatibility is not validated.",
    "Chemelex": "Canada/US compatible in principle; the exact Dayforce platform is unsupported by registered adapters.",
    "Copperleaf": "Canada/US/global compatible in principle; exact Copperleaf roles occur on a mixed IFS board that is not safely normalized here.",
    "SensorUp": "Canada/US/remote compatible; the current exact SensorUp BambooHR collection is verified.",
    "mCloud Technologies": "Calgary/Canada relevant, but the current renamed site declares no open positions and exposes no job-specific contract.",
    "BeWhere": "Canada relevant; no official jobs or student collection is exposed.",
    "Rigstar Industrial Telecom": "Calgary relevant historically; current compatibility cannot be validated after the rebrand and domain failure.",
    "Semtech": "Canada/US/global and internship compatible; the exact Semtech source is independently validated.",
    "Pason Systems": "Canada/US compatible; current Pason-attributed Alberta, Calgary, and Houston roles are verified with zero current student titles.",
    "Parkland Corporation": "Canada/US and student compatible in principle; safe live normalization is blocked by Cloudflare.",
    "Trican Well Service": "Canada and student-program compatible; current exact-source Alberta/BC/Saskatchewan jobs are verified.",
    "Blackline Safety": "Calgary/Canada/US compatible in principle; Workable Turnstile prevents safe normalization.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Gibson Energy": "https://www.gibsonenergy.com/careers/student-new-grad-programs/",
    "Strathcona Resources": "https://www.strathconaresources.com/sustainability/",
    "Tartigrade Ltd": OFFICIAL_CAREERS_URLS["Tartigrade Ltd"],
    "HV Logic Ltd": OFFICIAL_CAREERS_URLS["HV Logic Ltd"],
    "Chemelex": OFFICIAL_CAREERS_URLS["Chemelex"],
    "Copperleaf": OFFICIAL_CAREERS_URLS["Copperleaf"],
    "SensorUp": OFFICIAL_CAREERS_URLS["SensorUp"],
    "mCloud Technologies": OFFICIAL_CAREERS_URLS["mCloud Technologies"],
    "BeWhere": OFFICIAL_CAREERS_URLS["BeWhere"],
    "Rigstar Industrial Telecom": OFFICIAL_CAREERS_URLS["Rigstar Industrial Telecom"],
    "Semtech": "https://www.semtech.com/careers",
    "Pason Systems": "https://pason.com/careers/interns-and-students/",
    "Parkland Corporation": OFFICIAL_CAREERS_URLS["Parkland Corporation"],
    "Trican Well Service": OFFICIAL_CAREERS_URLS["Trican Well Service"],
    "Blackline Safety": OFFICIAL_CAREERS_URLS["Blackline Safety"],
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex, "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS.get(name), "max_pages": 1,
        "max_jobs": MAX_JOBS, "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES = [
    _entry("Gibson Energy", "sap_successfactors", OFFICIAL_CAREERS_URLS["Gibson Energy"],
           identity_regex=r"Gibson Energy Jobs|Gibson Energy", allowed_apply_hosts=["careers.gibsonenergy.com"],
           identity_url="https://careers.gibsonenergy.com/search/", search_url="https://careers.gibsonenergy.com/search/",
           endpoint_variant="rmk_table"),
    _entry("Strathcona Resources", "workday", OFFICIAL_CAREERS_URLS["Strathcona Resources"],
           identity_regex=r"Strathcona Resources", allowed_apply_hosts=["strathconaresources.wd10.myworkdayjobs.com"],
           identity_url="https://strathconaresources.wd10.myworkdayjobs.com/Careers",
           search_url="https://strathconaresources.wd10.myworkdayjobs.com/Careers", tenant="strathconaresources",
           wd_subdomain="wd10", site_id="Careers"),
    _entry("SensorUp", "bamboohr", OFFICIAL_CAREERS_URLS["SensorUp"], identity_regex=r"SensorUp",
           allowed_apply_hosts=["sensorup.bamboohr.com"], identity_url="https://www.sensorup.com/terms-of-use",
           listing_url="https://sensorup.bamboohr.com/careers/list", slug="sensorup"),
    _entry("Semtech", "workday", OFFICIAL_CAREERS_URLS["Semtech"], identity_regex=r"Semtech",
           allowed_apply_hosts=["semtech.wd1.myworkdayjobs.com"],
           identity_url="https://semtech.wd1.myworkdayjobs.com/SemtechCareers",
           search_url="https://semtech.wd1.myworkdayjobs.com/SemtechCareers", tenant="semtech",
           wd_subdomain="wd1", site_id="SemtechCareers"),
    _entry("Pason Systems", "ukg_pro",
           "https://recruiting.ultipro.ca/PAS5000PASON/JobBoard/736c1025-c469-4ece-a487-4884545272a7",
           identity_regex=r"Pason", allowed_apply_hosts=["recruiting.ultipro.ca"],
           identity_url="https://recruiting.ultipro.ca/PAS5000PASON/JobBoard/736c1025-c469-4ece-a487-4884545272a7",
           job_attribution_regex=r"\bPason(?: Systems)?\b",
           job_attribution_exclude_regex=r"\b(?:Intelligent Wellhead Systems|IWS)\b"),
    _entry("Trican Well Service", "startdate", "https://tricanwellservice.startdate.ca/",
           identity_regex=r"\bTrican\b", allowed_apply_hosts=["tricanwellservice.startdate.ca"],
           identity_url="https://tricanwellservice.com/",
           endpoint_url="https://api.startdate.ca/jobs/active?page=candidate_dashboard", locale="en",
           job_attribution_regex=r"\bTrican(?: Well Service)?\b"),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {"company": company, "platform": platform, "adapter": "none", "listing_url": listing_url,
            "classification": classification, "exclusion_reason": reason, "identity_regex": identity_regex}


EXCLUDED_REVIEWS = [
    _excluded("Tartigrade Ltd", "Wix/static", OFFICIAL_CAREERS_URLS["Tartigrade Ltd"], "no_current_official_job_collection",
              "The exact Tartigrade Limited Calgary site exposes a Wix career route but no current job collection, stable IDs, or anonymous job-specific apply URLs.", r"Tartigrade"),
    _excluded("HV Logic Ltd", "No ATS observed", OFFICIAL_CAREERS_URLS["HV Logic Ltd"], "no_current_official_careers_source",
              "The exact HV Logic Ltd site has no careers or jobs collection. LinkedIn and Indeed advertisements are third-party evidence and are not accepted as official attribution.", r"HV Logic"),
    _excluded("Chemelex", "Dayforce", OFFICIAL_CAREERS_URLS["Chemelex"], "unsupported_platform",
              "The exact Chemelex Dayforce tenant is official, but Dayforce is not a registered production adapter. Chemelex is the former nVent Thermal Management business acquired by Brookfield; no acquisition-based parent mapping is inferred.", r"Chemelex"),
    _excluded("Copperleaf", "IFS SmartRecruiters shared board", "https://careers.smartrecruiters.com/IFS1/", "parent_source_required",
              "IFS completed its acquisition of Copperleaf in August 2024, and Copperleaf's careers URL now redirects to IFS's mixed SmartRecruiters board. Exact Copperleaf titles are visible, but no reviewed per-job shared-board attribution adapter or parent mapping exists.", r"Copperleaf|IFS"),
    _excluded("mCloud Technologies", "Squarespace/static", OFFICIAL_CAREERS_URLS["mCloud Technologies"], "renamed_or_identity_changed",
              "The current official site is branded mCloudTech.ai and explicitly reports no open positions. The requested mCloud Technologies identity has no current stable job IDs or job-specific apply URLs.", r"mCloudTech|mCloud"),
    _excluded("BeWhere", "No ATS observed", OFFICIAL_CAREERS_URLS["BeWhere"], "no_current_official_careers_source",
              "BeWhere Inc., a wholly owned subsidiary of BeWhere Holdings Inc., exposes no official careers collection, stable job IDs, or shared parent job board. No parent mapping is inferred.", r"BeWhere"),
    _excluded("Rigstar Industrial Telecom", "No valid current source", OFFICIAL_CAREERS_URLS["Rigstar Industrial Telecom"], "renamed_and_source_compromised",
              "Rigstar Industrial Telecom rebranded as RSG Telecom in March 2024. Its former domain currently resolves to unrelated gambling content, so it is rejected rather than treated as an official successor source.", r"Rigstar|RSG Telecom"),
    _excluded("Parkland Corporation", "TTC portals behind Cloudflare", "https://parkland-careers.ttcportals.com/", "blocked",
              "Parkland's official page links the exact TTC careers board, but the board returns a Cloudflare challenge. No Cloudflare, cookie, session, or browser bypass is attempted.", r"Parkland"),
    _excluded("Blackline Safety", "Workable", "https://apply.workable.com/blacklinesafety/", "blocked",
              "Blackline Safety's official hub still links its exact Workable account, whose apply flow has Turnstile enabled. Workable remains unregistered; the legacy generic /careers form is not a stable job-specific alternative.", r"Blackline Safety|Careers at Blackline"),
]

PLATFORM_NOTES = {
    "Gibson Energy": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "Strathcona Resources": ("Workday", "workday"), "Tartigrade Ltd": ("Wix/static", "none"),
    "HV Logic Ltd": ("No ATS observed", "none"), "Chemelex": ("Dayforce", "none"),
    "Copperleaf": ("IFS SmartRecruiters shared board", "none"), "SensorUp": ("BambooHR", "bamboohr"),
    "mCloud Technologies": ("Squarespace/static", "none"), "BeWhere": ("No ATS observed", "none"),
    "Rigstar Industrial Telecom": ("No valid current source", "none"), "Semtech": ("Workday", "workday"),
    "Pason Systems": ("UKG Pro", "ukg_pro"), "Parkland Corporation": ("TTC portals / Cloudflare", "none"),
    "Trican Well Service": ("StartDate/HireGround", "startdate"), "Blackline Safety": ("Workable / Turnstile", "none"),
}

CSV_FIELDS = [
    "company", "fresh_vs_recovery_target", "official_careers_url", "listing_url",
    "canadian_identity_location_evidence", "canada_us_remote_student_compatibility",
    "platform", "adapter", "result", "jobs_normalized", "current_student_intern_hits",
    "current_student_role_evidence", "canadian_locations_represented", "student_program_evidence_url",
    "exclusion_reason", "original_246_master", "master_rank", "validation_status", "stable_id_result",
    "apply_url_result", "allowed_apply_hosts", "job_count_run_1", "job_count_run_2",
]

CANADIAN_LOCATION_RE = re.compile(
    r"\b(?:Canada|Canadian|Alberta|British Columbia|Ontario|Quebec|Manitoba|Saskatchewan|Calgary|Edmonton|"
    r"Vancouver|Toronto|Ottawa|Montreal|Mississauga|Grande Prairie|Red Deer|Fort St\.? John|Peace River|Athabasca|Cold Lake)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)(?:,\s*CA)?(?:,|\b)", re.IGNORECASE,
)


def _configure_engine() -> None:
    values = {
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS, "MASTER_WORKBOOK": MASTER_WORKBOOK,
        "CANDIDATE_CSV": CANDIDATE_CSV, "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY,
        "VALIDATED_REGISTRY": VALIDATED_REGISTRY, "REPORT": REPORT,
        "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT, "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE, "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT,
        "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS, "MAX_PAGES": MAX_PAGES, "MAX_JOBS": MAX_JOBS,
        "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES, "REVIEWED_COMPANIES": REVIEWED_COMPANIES,
        "MASTER_RANKS": MASTER_RANKS, "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS,
        "CALGARY_RELEVANCE": CANADA_RELEVANCE, "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES, "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES,
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
        location = " ".join(str(job.location or "").replace("â€¦", "…").split())
        if location and CANADIAN_LOCATION_RE.search(location):
            values.setdefault(location.casefold(), location)
    return [values[key] for key in sorted(values)]


def _student_hits(jobs: Iterable[Any]) -> list[dict[str, str]]:
    return sorted(
        ({"title": job.title, "location": job.location, "url": job.url} for job in jobs
         if engine.STUDENT_ROLE_RE.search(str(job.title or ""))),
        key=lambda item: (item["url"], item["title"]),
    )[:MAX_JOBS]


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
        rows.append({"company": name, "fresh_vs_recovery_target": "recovery" if name in RECOVERY_TARGETS else "fresh",
                     "official_careers_url": OFFICIAL_CAREERS_URLS[name],
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
        company = str(result["company"])
        result["fresh_vs_recovery_target"] = "recovery" if company in RECOVERY_TARGETS else "fresh"
        result["canadian_identity_location_evidence"] = CANADA_RELEVANCE[company]
        result["canada_us_remote_student_compatibility"] = COMPATIBILITY[company]
        runs = list(result.get("runs") or [])
        result["canadian_locations_represented"] = list(runs[0].get("canadian_locations") or []) if runs else []
    payload["candidate_registry_has_exact_243_entry_prefix"] = payload.pop("candidate_registry_has_exact_186_entry_prefix")
    payload["validated_registry_has_exact_243_entry_prefix"] = payload.pop("validated_registry_has_exact_186_entry_prefix")
    totals = payload["totals"]
    totals["validated_outside_original_master"] = totals.pop("calgary_focused_validated_outside_master")
    totals["validated_outside_original_master_names"] = totals.pop("calgary_focused_validated_outside_master_names")
    totals["projected_canonical_production_count"] = totals["projected_production_sources"]
    totals["projected_original_master_coverage_ratio"] = f"{totals['projected_original_master_coverage']}/{totals['projected_original_master_total']}"
    totals["fresh_targets"] = len(REVIEWED_COMPANIES) - len(RECOVERY_TARGETS)
    totals["recovery_targets"] = len(RECOVERY_TARGETS)
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
        print(f"Requested expansion v3 Batch E validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
