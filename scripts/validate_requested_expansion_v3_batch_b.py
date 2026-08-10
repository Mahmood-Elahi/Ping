"""Build and validate Requested Company Expansion v3 Batch B (Canada only).

The investigation starts from the exact 207-source production registry, skips
active companies, and writes only candidate, validated, inventory, and ignored
report artifacts. It never activates sources or changes parent mappings,
persistence, student filters, dashboard code, or publication state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_calgary_student_employers_v1_batch_a as engine
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers import scanner
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


_ENGINE_BUILD_CANDIDATE_REGISTRY = engine.build_candidate_registry

PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_requested_expansion_v3_batch_b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_b_validated.yaml"
REPORT = ROOT / "output" / "requested_expansion_v3_batch_b_validation.json"

HISTORICAL_PRODUCTION_COUNT = 207
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 186
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Cohere", "Waabi", "OpenText", "CGI", "Celestica",
    "Constellation Software", "Kinaxis", "Descartes Systems Group", "TELUS",
    "BCE / Bell", "Sierra Wireless", "Absolute Software",
]
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "Cohere": "https://cohere.com/careers",
    "Waabi": "https://waabi.ai/careers",
    "OpenText": "https://careers.opentext.com/us/en/home",
    "CGI": "https://www.cgi.com/en/careers",
    "Celestica": "https://www.celestica.com/careers/working-at-celestica",
    "Constellation Software": "https://www.csisoftware.com/",
    "Kinaxis": "https://www.kinaxis.com/en/careers",
    "Descartes Systems Group": "https://careers.descartes.com/",
    "TELUS": "https://www.telus.com/en/about/careers",
    "BCE / Bell": "https://jobs.bell.ca/ca/en/",
    "Sierra Wireless": "https://www.sierrawireless.com/company/careers/",
    "Absolute Software": "https://www.absolute.com/company/careers",
}

CANADA_CALGARY_RELEVANCE = {
    "Cohere": "Canadian-founded AI company with Toronto, Montreal, Calgary, Canada-wide, US, and international roles on its exact board.",
    "Waabi": "Waabi Innovation Inc. is Toronto-founded; its exact Waabi Lever board includes Toronto, Canada/US remote, and Canadian internship roles.",
    "OpenText": "Waterloo-founded Canadian software company whose exact global OpenText source contains Canadian roles and a formal intern/new-graduate program.",
    "CGI": "Montreal-headquartered Canadian consulting company with a verified Canadian co-op and internship program; anonymous job collection is blocked.",
    "Celestica": "Toronto-headquartered Canadian technology manufacturer with Canadian roles and a verified student internship/co-op program.",
    "Constellation Software": "Toronto-headquartered holding company whose autonomous operating businesses recruit independently; no central exact-attribution job collection was found.",
    "Kinaxis": "Ottawa-headquartered Canadian supply-chain software company with Canada-remote roles and a verified North American co-op/intern program.",
    "Descartes Systems Group": "Waterloo-headquartered Canadian logistics-software company with Canadian roles and a verified Canadian co-op/intern program.",
    "TELUS": "Canadian telecommunications company whose exact TELUS board represents locations across Canada, including Calgary and Edmonton.",
    "BCE / Bell": "Bell Canada is a 100%-owned BCE Inc. subsidiary; the source is explicitly Bell-branded and only Bell-attributed jobs are normalized.",
    "Sierra Wireless": "The former Vancouver-headquartered company was acquired by Semtech in 2023; the remaining Sierra careers page routes to Semtech careers.",
    "Absolute Software": "Vancouver-founded Absolute Software Corporation now operates under the Absolute Security brand and remains a wholly-owned Crosspoint company with its own exact careers board.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Cohere": "https://jobs.ashbyhq.com/cohere",
    "Waabi": "https://jobs.lever.co/waabi",
    "OpenText": "https://careers.opentext.com/us/en/studentsandgraduates",
    "CGI": "https://www.cgi.com/canada/en-ca/startmycareer",
    "Celestica": "https://careers.celestica.com/go/Students-and-Interns/8944801/",
    "Constellation Software": "https://www.csisoftware.com/operating-groups/",
    "Kinaxis": "https://www.kinaxis.com/en/blog/build-experience-and-relationships-co-op/intern-kinaxis",
    "Descartes Systems Group": "https://careers.descartes.com/career-resources/the-descartes-co-operative-education-and-internship-program/",
    "TELUS": "https://careers.telus.com/go/Students-and-New-Grads/2637017/",
    "BCE / Bell": "https://jobs.bell.ca/ca/en/students-graduates",
    "Sierra Wireless": "https://www.sierrawireless.com/company/careers/",
    "Absolute Software": "https://www.absolute.com/company/careers",
}

COMPATIBILITY = {
    "Cohere": "Canada/US/remote compatible; three current Canadian intern/co-op titles observed on the exact board.",
    "Waabi": "Canada/US/remote compatible; current Toronto PhD research internship observed in the bounded collection.",
    "OpenText": "Canada/US/global compatible; official internship/new-graduate program verified; no current intern title in the bounded collection.",
    "CGI": "Canadian student/co-op compatible in principle; current anonymous job verification is blocked by Radware CAPTCHA.",
    "Celestica": "Canada/US/global compatible; student internship/co-op program and current Canadian program page verified; no student title in the bounded collection.",
    "Constellation Software": "Subsidiary opportunities may exist, but they cannot be attributed to the parent without an exact shared-source contract.",
    "Kinaxis": "Canada/US/remote compatible; Canadian co-op/intern program verified; no current intern title in the bounded collection.",
    "Descartes Systems Group": "Canada/US/global compatible; Canadian co-op program verified and one current international intern title observed.",
    "TELUS": "Canada compatible; student/new-graduate program verified; no current student title in the bounded collection.",
    "BCE / Bell": "Canada compatible; Bell internship/graduate programs verified; no current student title in the bounded collection.",
    "Sierra Wireless": "Not independently compatible after acquisition because the current careers route is Semtech and exact Sierra attribution is unavailable.",
    "Absolute Software": "Canada/US/global compatible under the current Absolute Security brand; new-graduate support is stated but no current intern title was observed.",
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
        "Cohere", "ashby", OFFICIAL_CAREERS_URLS["Cohere"],
        identity_regex=r"Join the mission to scale intelligence|Cohere",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Cohere"], slug="cohere",
    ),
    _entry(
        "Waabi", "lever", OFFICIAL_CAREERS_URLS["Waabi"],
        identity_regex=r"\bWaabi\b", allowed_apply_hosts=["jobs.lever.co"],
        identity_url="https://jobs.lever.co/waabi", slug="waabi",
    ),
    _entry(
        "OpenText", "phenom", OFFICIAL_CAREERS_URLS["OpenText"],
        identity_regex=r"OpenText", allowed_apply_hosts=["careers.opentext.com"],
        identity_url="https://careers.opentext.com/us/en/search-results",
        search_url="https://careers.opentext.com/us/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="OPJOPEUS", locale="en_us",
        apply_url_template="https://careers.opentext.com/us/en/job/{job_seq_no}",
    ),
    _entry(
        "Celestica", "sap_successfactors", OFFICIAL_CAREERS_URLS["Celestica"],
        identity_regex=r"Careers at Celestica|Celestica International",
        allowed_apply_hosts=["careers.celestica.com"],
        identity_url="https://careers.celestica.com/search/",
        search_url="https://careers.celestica.com/search/", endpoint_variant="rmk_table",
    ),
    _entry(
        "Kinaxis", "icims", OFFICIAL_CAREERS_URLS["Kinaxis"],
        identity_regex=r"Kinaxis Inc|Kinaxis",
        allowed_apply_hosts=["careers-kinaxis.icims.com"],
        identity_url="https://careers-kinaxis.icims.com/jobs/search",
        search_url="https://careers-kinaxis.icims.com/jobs/search",
        endpoint_variant="icims_legacy_portal_html", tenant="kinaxis",
        board_id="kinaxis-public", page_size=50,
    ),
    _entry(
        "Descartes Systems Group", "sap_successfactors",
        OFFICIAL_CAREERS_URLS["Descartes Systems Group"],
        identity_regex=r"Descartes Systems Group|Careers at Descartes",
        allowed_apply_hosts=["jobs.descartes.com"],
        identity_url="https://jobs.descartes.com/search/",
        search_url="https://jobs.descartes.com/search/", endpoint_variant="rmk_tile",
    ),
    _entry(
        "TELUS", "sap_successfactors", OFFICIAL_CAREERS_URLS["TELUS"],
        identity_regex=r"\bTELUS\b", allowed_apply_hosts=["careers.telus.com"],
        identity_url="https://careers.telus.com/search/",
        search_url="https://careers.telus.com/search/", endpoint_variant="rmk_table",
    ),
    _entry(
        "BCE / Bell", "phenom", OFFICIAL_CAREERS_URLS["BCE / Bell"],
        identity_regex=r"Careers at Bell|Bell Canada",
        allowed_apply_hosts=["jobs.bell.ca"],
        identity_url="https://jobs.bell.ca/ca/en/search-results",
        search_url="https://jobs.bell.ca/ca/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="BECACA", locale="en_ca",
        apply_url_template="https://jobs.bell.ca/ca/en/job/{job_seq_no}",
    ),
    _entry(
        "Absolute Software", "static_html", OFFICIAL_CAREERS_URLS["Absolute Software"],
        identity_regex=r"Absolute(?: Security| Logo| provides)",
        allowed_apply_hosts=["jobs.jobvite.com"],
        identity_url="https://jobs.jobvite.com/absolute/jobs",
        listing_url="https://jobs.jobvite.com/absolute/jobs",
        endpoint_variant="server_rendered_cards",
        listing_selector="table.jv-job-list", job_card_selector="table.jv-job-list tr",
        title_selector="td.jv-job-list-name a",
        detail_link_selector="td.jv-job-list-name a[href*='/absolute/job/']",
        external_id_regex=r"/job/([^/?#]+)",
        location_selector="td.jv-job-list-location",
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
        "CGI", "Njoyn XWeb", "https://cgi.njoyn.com/CORP/xweb/xweb.asp?page=JobListing&CLID=21001&lang=1",
        "blocked",
        "CGI's official careers page links the exact CGI Njoyn board, but bounded anonymous requests return a Radware hCaptcha page. No challenge bypass is attempted.",
        r"CGI|Careers",
    ),
    _excluded(
        "Constellation Software", "Autonomous subsidiary recruiting", OFFICIAL_CAREERS_URLS["Constellation Software"],
        "no_exact_central_source",
        "Constellation states that its more than 1,100 businesses operate autonomously, and its corporate site exposes no central job collection with exact parent attribution. Subsidiary boards are not inferred as Constellation Software jobs and no parent mapping is added.",
        r"Constellation Software|Vertical Market",
    ),
    _excluded(
        "Sierra Wireless", "Semtech parent careers route", "https://www.semtech.com/careers",
        "parent_source_required",
        "Semtech completed its acquisition of Sierra Wireless on January 12, 2023, and the remaining Sierra careers page now links Semtech careers. No independently attributable Sierra collection with stable IDs and Sierra-specific apply URLs remains, so no Semtech parent mapping is inferred.",
        r"Semtech|Sierra Wireless|Careers",
    ),
]

PLATFORM_NOTES = {
    "Cohere": ("Ashby", "ashby"),
    "Waabi": ("Lever", "lever"),
    "OpenText": ("Phenom", "phenom"),
    "CGI": ("Njoyn XWeb (Radware protected)", "none"),
    "Celestica": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "Constellation Software": ("Autonomous subsidiary recruiting", "none"),
    "Kinaxis": ("iCIMS legacy portal", "icims"),
    "Descartes Systems Group": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "TELUS": ("SAP SuccessFactors Recruiting Marketing", "sap_successfactors"),
    "BCE / Bell": ("Bell-branded Phenom tenant (BCE-owned)", "phenom"),
    "Sierra Wireless": ("Semtech parent careers route", "none"),
    "Absolute Software": ("Jobvite static HTML (current Absolute Security brand)", "static_html"),
}

CSV_FIELDS = [
    "company", "official_careers_url", "listing_url", "calgary_alberta_relevance",
    "canada_us_remote_student_compatibility", "platform", "adapter", "result",
    "jobs_normalized", "current_student_intern_hits", "current_student_role_evidence",
    "canadian_locations_represented", "student_program_evidence_url", "exclusion_reason",
    "original_246_master", "master_rank", "validation_status", "stable_id_result",
    "apply_url_result", "allowed_apply_hosts", "job_count_run_1", "job_count_run_2",
]

CANADIAN_LOCATION_RE = re.compile(
    r"\b(?:Canada|CAN|CA-Remote|Alberta|British Columbia|Ontario|Quebec|Québec|"
    r"Manitoba|Saskatchewan|Nova Scotia|New Brunswick|Newfoundland|Prince Edward Island|"
    r"Northwest Territories|Yukon|Nunavut|Calgary|Edmonton|Toronto|Vancouver|Ottawa|"
    r"Montreal|Montréal|Waterloo|Burnaby|Dartmouth|Winnipeg|Lloydminster|Barrie|"
    r"Welland|Peterborough|Listowel|Corner Brook)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT),\s*CA(?:,|\b)",
    re.IGNORECASE,
)


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
    """Run the standard bounded proof while retaining Canada/student evidence."""
    _configure_engine()
    started = time.monotonic()
    session = requests.Session()
    try:
        identity = engine._identity_check(entry, session, timeout)
        jobs = scanner.ADAPTERS[str(entry["platform"])](
            session, CompanyTarget.from_dict(dict(entry)), timeout,
        )
        signature = engine._verify_jobs(jobs, entry)
        pagination = dict(engine._pagination(session, str(entry["platform"])))
        zero = not signature and bool(
            pagination.get("zero_job_contract")
            or pagination.get("stop_reason") == "validated_zero_jobs"
        )
        if not signature and not zero:
            raise AdapterError("Candidate did not conclusively prove a zero-job collection")
        return {
            "runtime_seconds": round(time.monotonic() - started, 3), "success": True,
            "jobs_observed": len(jobs), "jobs_normalized": len(signature),
            "stable_ids": [item[0] for item in signature],
            "apply_urls": [item[1] for item in signature],
            "student_hits": _student_hits(jobs),
            "canadian_locations": _canadian_locations(jobs),
            "zero_job_contract": zero,
            "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
            "identity": identity,
            "pagination_stop_reason": pagination.get("stop_reason", "adapter_bounded_collection"),
        }
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        return {
            "runtime_seconds": round(time.monotonic() - started, 3), "success": False,
            "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [],
            "apply_urls": [], "student_hits": [], "canadian_locations": [],
            "pages_requested": min(int(entry["max_pages"]), MAX_PAGES),
            "status": engine._status_for_error(exc), "error": str(exc),
        }
    finally:
        session.close()


def _inventory_bytes(results: Iterable[Mapping[str, Any]]) -> bytes:
    by_name = {str(item["company"]): item for item in results}
    entries = {str(item["name"]): item for item in CANDIDATE_ENTRIES}
    reviews = {str(item["company"]): item for item in EXCLUDED_REVIEWS}
    rows = []
    for name in REVIEWED_COMPANIES:
        result = by_name[name]
        source = entries.get(name) or reviews[name]
        runs = list(result.get("runs") or [])
        rows.append({
            "company": name, "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "calgary_alberta_relevance": CANADA_CALGARY_RELEVANCE[name],
            "canada_us_remote_student_compatibility": COMPATIBILITY[name],
            "platform": PLATFORM_NOTES[name][0], "adapter": PLATFORM_NOTES[name][1],
            "result": result["result"], "jobs_normalized": result["jobs_normalized"],
            "current_student_intern_hits": result["current_student_intern_hits"],
            "current_student_role_evidence": "; ".join(hit["title"] for hit in result["current_student_role_evidence"]),
            "canadian_locations_represented": "; ".join(result["canadian_locations_represented"]),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name],
            "exclusion_reason": result["exclusion_reason"],
            "original_246_master": str(name in MASTER_RANKS).lower(),
            "master_rank": MASTER_RANKS.get(name, ""),
            "validation_status": result["validation_status"],
            "stable_id_result": result["stable_id_result"],
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
        result["canada_us_remote_student_compatibility"] = COMPATIBILITY[str(result["company"])]
        runs = list(result.get("runs") or [])
        result["canadian_locations_represented"] = (
            list(runs[0].get("canadian_locations") or []) if runs else []
        )
    payload["candidate_registry_has_exact_207_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_186_entry_prefix"
    )
    payload["validated_registry_has_exact_207_entry_prefix"] = payload.pop(
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
            reconcile()
            build_candidate_registry()
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
