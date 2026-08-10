"""Build and validate Requested Company Expansion v3 Batch D.

The investigation starts from the exact 230-source production registry, skips
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
CANDIDATE_CSV = ROOT / "config" / "company_requested_expansion_v3_batch_d_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_d_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_d_validated.yaml"
REPORT = ROOT / "output" / "requested_expansion_v3_batch_d_validation.json"

HISTORICAL_PRODUCTION_COUNT = 230
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 186
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "IBM", "Applied Intuition", "Apptronik", "1X Technologies", "Nuro",
    "Kodiak Robotics", "Gatik", "Physical Intelligence", "Serve Robotics",
    "Hugging Face", "Scale AI", "Renesas Electronics", "Silicon Labs",
    "TEKTELIC Communications", "Blackline Safety",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "IBM": "https://www.ibm.com/careers/search",
    "Applied Intuition": "https://www.appliedintuition.com/careers",
    "Apptronik": "https://apptronik.com/careers/job-listings",
    "1X Technologies": "https://www.1x.tech/careers",
    "Nuro": "https://www.nuro.ai/careers",
    "Kodiak Robotics": "https://kodiak.ai/careers",
    "Gatik": "https://archive.gatik.ai/careers/",
    "Physical Intelligence": "https://www.pi.website/join-us",
    "Serve Robotics": "https://www.serverobotics.com/careers/index.html",
    "Hugging Face": "https://apply.workable.com/huggingface/",
    "Scale AI": "https://scale.com/careers",
    "Renesas Electronics": "https://jobs.renesas.com",
    "Silicon Labs": "https://www.silabs.com/about-us/careers",
    "TEKTELIC Communications": "https://tektelic.com/career/",
    "Blackline Safety": "https://careers.blacklinesafety.com/home",
}

CANADA_RELEVANCE = {
    "IBM": "IBM's exact corporate search covers Canada and the US. The current intern query includes a Toronto/Calgary/Montreal term and Canada-labelled conversion roles; HashiCorp-only coverage is not substituted for IBM.",
    "Applied Intuition": "Applied Intuition's exact company board is currently US-focused, including current Sunnyvale research internships; no Canadian or Calgary role is represented.",
    "Apptronik": "Apptronik's official Greenhouse board is currently US-focused and includes a current Austin robotics software internship; no Canadian or Calgary role is represented.",
    "1X Technologies": "The exact 1X board covers US and Norwegian operations and includes a current California internship; no Canadian or Calgary role is represented.",
    "Nuro": "Nuro's official company careers page embeds the exact Nuro Greenhouse board, currently US-focused with a current Mountain View internship; no Canadian or Calgary role is represented.",
    "Kodiak Robotics": "Kodiak's exact company board covers US on-site and US-remote roles; no Canadian, Calgary, or current student title is represented.",
    "Gatik": "Gatik's official careers URL currently resolves to its official archive subdomain and publishes the exact gatikaiinc Greenhouse board with California, Texas, Arkansas, Arizona, and Ontario jobs. Current Canada jobs include Mississauga and Ajax; no Calgary role is represented.",
    "Physical Intelligence": "Physical Intelligence's former physicalintelligence.company domain redirects to pi.website, whose Join Us page embeds only the exact physicalintelligence Ashby board. Current roles are California-based and include Research Internships.",
    "Serve Robotics": "Serve Robotics' exact /careers/index.html page embeds only jobs.ashbyhq.com/serverobotics. Current jobs include US locations and USA-remote roles; no Canadian or Calgary role is represented.",
    "Hugging Face": "Hugging Face's exact Workable account is globally remote-oriented, but its official apply flow advertises challenge state and is not normalized.",
    "Scale AI": "Scale AI's exact corporate Greenhouse board includes US, Canada, and combined Canada/United States jobs; current internships in the source are US-based.",
    "Renesas Electronics": "Renesas' exact corporate SmartRecruiters source currently advertises Canada and US jobs. The bounded server-rendered group sample includes Austin but no Canadian location.",
    "Silicon Labs": "Silicon Laboratories Canada ULC has a Montreal office, while the exact corporate Workday source covers US and global roles. The official internship site currently has zero postings.",
    "TEKTELIC Communications": "TEKTELIC Communications Inc. is exactly attributed by its official same-domain career and vacancy pages. The source exposes Canada and Calgary filters, but its two current roles are in Kyiv and Germany.",
    "Blackline Safety": "Blackline Safety's exact official careers site identifies Calgary, Canada as headquarters and Houston as a US office, and links only the blacklinesafety Workable account. Current public listings include Calgary and US jobs, but the apply flow advertises challenge state.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "IBM": "https://www.ibm.com/careers/internships",
    "Applied Intuition": OFFICIAL_CAREERS_URLS["Applied Intuition"],
    "Apptronik": OFFICIAL_CAREERS_URLS["Apptronik"],
    "1X Technologies": OFFICIAL_CAREERS_URLS["1X Technologies"],
    "Nuro": "https://www.nuro.ai/early-career",
    "Kodiak Robotics": OFFICIAL_CAREERS_URLS["Kodiak Robotics"],
    "Gatik": OFFICIAL_CAREERS_URLS["Gatik"],
    "Physical Intelligence": OFFICIAL_CAREERS_URLS["Physical Intelligence"],
    "Serve Robotics": OFFICIAL_CAREERS_URLS["Serve Robotics"],
    "Hugging Face": OFFICIAL_CAREERS_URLS["Hugging Face"],
    "Scale AI": "https://scale.com/careers/university",
    "Renesas Electronics": "https://jobs.renesas.com/jobs",
    "Silicon Labs": OFFICIAL_CAREERS_URLS["Silicon Labs"],
    "TEKTELIC Communications": OFFICIAL_CAREERS_URLS["TEKTELIC Communications"],
    "Blackline Safety": OFFICIAL_CAREERS_URLS["Blackline Safety"],
}

COMPATIBILITY = {
    "IBM": "Canada/US student compatible; the bounded exact-source query normalizes current intern roles including Canada, Calgary, and US evidence.",
    "Applied Intuition": "US student compatible; five current Sunnyvale research-intern titles are present in the exact source.",
    "Apptronik": "US student compatible; one current Austin robotics-software intern title is present in the exact source.",
    "1X Technologies": "US student compatible; one current California internship title is present in the exact source.",
    "Nuro": "US student compatible; the official early-career program and one current Mountain View intern title are verified.",
    "Kodiak Robotics": "US/US-remote compatible; zero current student/intern titles are present.",
    "Gatik": "Canada/US compatible; current Ontario and US jobs are verified, with zero current student/intern titles.",
    "Physical Intelligence": "US student compatible; one current San Francisco Research Internships posting is verified.",
    "Serve Robotics": "US/US-remote compatible; zero current student/intern titles are present.",
    "Hugging Face": "Remote roles may be compatible, but safe current student-role normalization is blocked by the Workable challenge safeguard.",
    "Scale AI": "Canada/US compatible; two current US internship titles are present in the exact source.",
    "Renesas Electronics": "Canada/US/global and intern compatible; the current board advertises Canada, US, and intern facets, while the bounded static sample has zero student titles.",
    "Silicon Labs": "Canada/US/global and internship compatible; the official internship board currently has zero postings and the bounded all-jobs sample has zero student titles.",
    "TEKTELIC Communications": "Calgary/Canada/Europe compatible in principle; the exact source has zero current Calgary, Canada, or student titles.",
    "Blackline Safety": "Calgary/Canada/US compatible in principle; current Calgary and US listings are visible, but safe normalization is blocked by the Workable challenge safeguard.",
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
    _entry("IBM", "ibm_search", OFFICIAL_CAREERS_URLS["IBM"], identity_regex=r"IBM Careers|IBM",
           allowed_apply_hosts=["careers.ibm.com"], identity_url=OFFICIAL_CAREERS_URLS["IBM"], search_terms=["intern"]),
    _entry("Applied Intuition", "ashby", OFFICIAL_CAREERS_URLS["Applied Intuition"], identity_regex=r"Applied Intuition",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://jobs.ashbyhq.com/applied", slug="applied"),
    _entry("Apptronik", "greenhouse", OFFICIAL_CAREERS_URLS["Apptronik"], identity_regex=r"Apptronik",
           allowed_apply_hosts=["boards.greenhouse.io", "job-boards.greenhouse.io"], identity_url=OFFICIAL_CAREERS_URLS["Apptronik"], slug="apptronik"),
    _entry("1X Technologies", "ashby", OFFICIAL_CAREERS_URLS["1X Technologies"], identity_regex=r"1X Jobs|\b1X\b",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url="https://jobs.ashbyhq.com/1x", slug="1x"),
    _entry("Nuro", "greenhouse", OFFICIAL_CAREERS_URLS["Nuro"], identity_regex=r"\bNuro\b",
           allowed_apply_hosts=["nuro.ai", "www.nuro.ai"], identity_url=OFFICIAL_CAREERS_URLS["Nuro"], slug="nuro"),
    _entry("Kodiak Robotics", "greenhouse", OFFICIAL_CAREERS_URLS["Kodiak Robotics"], identity_regex=r"Kodiak",
           allowed_apply_hosts=["job-boards.greenhouse.io"], identity_url="https://job-boards.greenhouse.io/kodiak", slug="kodiak"),
    _entry("Gatik", "greenhouse", OFFICIAL_CAREERS_URLS["Gatik"], identity_regex=r"\bGatik\b",
           allowed_apply_hosts=["boards.greenhouse.io", "job-boards.greenhouse.io"], identity_url=OFFICIAL_CAREERS_URLS["Gatik"], slug="gatikaiinc"),
    _entry("Physical Intelligence", "ashby", OFFICIAL_CAREERS_URLS["Physical Intelligence"], identity_regex=r"Physical Intelligence",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url=OFFICIAL_CAREERS_URLS["Physical Intelligence"], slug="physicalintelligence"),
    _entry("Serve Robotics", "ashby", OFFICIAL_CAREERS_URLS["Serve Robotics"], identity_regex=r"Serve Robotics|Serve is delivering",
           allowed_apply_hosts=["jobs.ashbyhq.com"], identity_url=OFFICIAL_CAREERS_URLS["Serve Robotics"], slug="serverobotics"),
    _entry("Scale AI", "greenhouse", OFFICIAL_CAREERS_URLS["Scale AI"], identity_regex=r"Scale AI|Scale is growing",
           allowed_apply_hosts=["job-boards.greenhouse.io"], identity_url="https://job-boards.greenhouse.io/scaleai", slug="scaleai"),
    _entry("Renesas Electronics", "static_html", OFFICIAL_CAREERS_URLS["Renesas Electronics"], identity_regex=r"Careers at Renesas Electronics|Renesas Electronics",
           allowed_apply_hosts=["jobs.smartrecruiters.com"], identity_url="https://careers.smartrecruiters.com/renesaselectronics",
           listing_url="https://careers.smartrecruiters.com/renesaselectronics", endpoint_variant="server_rendered_cards",
           listing_selector="div.js-openings-load", job_card_selector="section.openings-section",
           title_selector="h4.job-title", detail_link_selector="a.js-job-ad-link",
           external_id_regex=r"/renesaselectronics/([0-9]+)-", location_selector="h3.opening-title"),
    _entry("Silicon Labs", "workday", OFFICIAL_CAREERS_URLS["Silicon Labs"], identity_regex=r"Silicon Labs|SiliconlabsCareers",
           allowed_apply_hosts=["silabs.wd1.myworkdayjobs.com"], identity_url="https://silabs.wd1.myworkdayjobs.com/SiliconlabsCareers",
           tenant="silabs", wd_subdomain="wd1", site_id="SiliconlabsCareers", search_url="https://silabs.wd1.myworkdayjobs.com/SiliconlabsCareers"),
    _entry("TEKTELIC Communications", "static_html", OFFICIAL_CAREERS_URLS["TEKTELIC Communications"], identity_regex=r"TEKTELIC",
           allowed_apply_hosts=["tektelic.com"], identity_url="https://tektelic.com/career/vacancies/",
           listing_url="https://tektelic.com/career/vacancies/", endpoint_variant="server_rendered_cards",
           listing_selector="div.vacancies-list", job_card_selector="div.vacancies-list div.col-lg-12",
           title_selector="p.vacancy-title", detail_link_selector='a[href*="/career/vacancies/"]',
           external_id_regex=r"/career/vacancies/([^/]+)/", location_selector="div.loc span"),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {"company": company, "platform": platform, "adapter": "none", "listing_url": listing_url,
            "classification": classification, "exclusion_reason": reason, "identity_regex": identity_regex}


EXCLUDED_REVIEWS = [
    _excluded(
        "Hugging Face", "Workable", "https://apply.workable.com/huggingface/", "blocked",
        "The exact Hugging Face Workable account is identifiable and has job-specific Workable URLs, but the official apply flow advertises Turnstile/challenge state. The repository intentionally keeps its Workable collector unregistered until CAPTCHA-free validation, and that safeguard is not weakened.",
        r"Hugging Face",
    ),
    _excluded(
        "Blackline Safety", "Workable", "https://apply.workable.com/blacklinesafety/", "blocked",
        "Blackline Safety's official Calgary-headquartered careers site links the exact blacklinesafety Workable account, whose public board currently shows Calgary and US jobs. Its official apply flow advertises Turnstile/challenge state; the intentionally unregistered Workable collector is not enabled and no challenge bypass is attempted.",
        r"Blackline Safety|Careers at Blackline",
    ),
]

PLATFORM_NOTES = {
    "IBM": ("IBM Careers search API", "ibm_search"),
    "Applied Intuition": ("Ashby", "ashby"), "Apptronik": ("Greenhouse", "greenhouse"),
    "1X Technologies": ("Ashby", "ashby"), "Nuro": ("Greenhouse with Nuro job URLs", "greenhouse"),
    "Kodiak Robotics": ("Greenhouse", "greenhouse"), "Gatik": ("Greenhouse", "greenhouse"),
    "Physical Intelligence": ("Ashby", "ashby"), "Serve Robotics": ("Ashby", "ashby"),
    "Hugging Face": ("Workable", "none"), "Scale AI": ("Greenhouse", "greenhouse"),
    "Renesas Electronics": ("SmartRecruiters server-rendered careers", "static_html"),
    "Silicon Labs": ("Workday", "workday"),
    "TEKTELIC Communications": ("WordPress server-rendered vacancies", "static_html"),
    "Blackline Safety": ("Workable", "none"),
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
    r"\b(?:Canada|Canadian|Alberta|British Columbia|Ontario|Quebec|Québec|Manitoba|Saskatchewan|Nova Scotia|"
    r"New Brunswick|Newfoundland|Prince Edward Island|Northwest Territories|Yukon|Nunavut|Calgary|Edmonton|Toronto|"
    r"Vancouver|Ottawa|Montreal|Montréal|Waterloo|Kitchener|Burnaby|Mississauga|Ajax|Oakville|Guelph|Winnipeg)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT),\s*CA(?:,|\b)", re.IGNORECASE,
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
    return sorted(hits, key=lambda item: (item["url"], item["title"]))[:MAX_JOBS]


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
        result["canadian_identity_location_evidence"] = CANADA_RELEVANCE[str(result["company"])]
        result["canada_us_remote_student_compatibility"] = COMPATIBILITY[str(result["company"])]
        runs = list(result.get("runs") or [])
        result["canadian_locations_represented"] = list(runs[0].get("canadian_locations") or []) if runs else []
    payload["candidate_registry_has_exact_230_entry_prefix"] = payload.pop("candidate_registry_has_exact_186_entry_prefix")
    payload["validated_registry_has_exact_230_entry_prefix"] = payload.pop("validated_registry_has_exact_186_entry_prefix")
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
        print(f"Requested expansion v3 Batch D validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
