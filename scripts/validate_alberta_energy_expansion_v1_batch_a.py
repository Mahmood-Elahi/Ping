"""Build and validate Alberta Energy Expansion v1 Batch A without activation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import adp, paycor, static_html, ukg_pro, workday
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_alberta_energy_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.alberta_energy_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.alberta_energy_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "alberta_energy_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 339
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "Baytex Energy", "MEG Energy", "Peyto Exploration", "Birchcliff Energy",
    "Athabasca Oil", "Obsidian Energy", "Kelt Exploration", "Cardinal Energy",
    "NuVista Energy", "Advantage Energy", "Paramount Resources", "Cameco",
    "PrairieSky Royalty", "Topaz Energy", "STEP Energy Services",
    "Calfrac Well Services", "Total Energy Services", "Weir Group", "Enverus",
    "Freehold Royalties",
]

STUDENT_RE = re.compile(
    r"(?i)\b(intern(?:ship)?|student|co[ -]?op|new grad(?:uate)?|graduate program)\b"
)
IN_SCOPE_LOCATION_RE = re.compile(
    r"(?i)\b(canada|canadian|alberta|calgary|edmonton|british columbia|ontario|quebec|"
    r"nova scotia|new brunswick|manitoba|saskatchewan|newfoundland|prince edward island|"
    r"northwest territories|nunavut|yukon|united states|u\.s\.|usa|remote)\b|\b[A-Z]{2}, CA\b"
)

CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "Athabasca Oil",
        "platform": "static_html",
        "careers_url": "https://www.atha.com/careers-details/calgary-ab.html",
        "listing_url": "https://www.atha.com/careers-details/calgary-ab.html",
        "endpoint_variant": "server_rendered_cards",
        "listing_selector": ".blog",
        "job_card_selector": "[itemprop=blogPost]",
        "title_selector": "h2[itemprop=name] a",
        "detail_link_selector": "h2[itemprop=name] a",
        "external_id_regex": r"/([0-9]+)-",
        "fixed_location": "Calgary, Alberta, Canada",
        "identity_regex": r"Athabasca Oil|ATHA Energy",
        "allowed_apply_hosts": ["www.atha.com"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 500_000,
    },
    {
        "name": "Paramount Resources",
        "platform": "adp",
        "careers_url": (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=b8dcb5ca-ab7a-4dad-b6cd-2c7a858453dd&ccId=19000101_000001&lang=en_CA"
        ),
        "endpoint_url": (
            "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
            "staffing/v1/job-requisitions"
        ),
        "endpoint_variant": adp.WORKFORCE_NOW,
        "identity_regex": r"Paramount Resources",
        "allowed_apply_hosts": ["workforcenow.adp.com"],
        "original_workbook_rank": None,
        "page_size": 10,
        "max_pages": 1,
        "max_jobs": 20,
    },
    {
        "name": "STEP Energy Services",
        "platform": "ukg_pro",
        "careers_url": (
            "https://recruiting.ultipro.ca/STE5005SEEP/JobBoard/"
            "36169e92-ed1b-40fd-afd8-17e31780054c"
        ),
        "identity_regex": r"STEP Energy Services",
        "allowed_apply_hosts": ["recruiting.ultipro.ca"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
    },
    {
        "name": "Calfrac Well Services",
        "platform": "paycor",
        "careers_url": "https://calfrac.com/careers/",
        "endpoint_url": (
            "https://recruitingbypaycor.com/career/CareerHome.action"
            "?clientId=8a7883d0676d10a101678af01334303a"
        ),
        "identity_regex": r"Calfrac Well Services",
        "allowed_apply_hosts": ["recruitingbypaycor.com"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
    },
    {
        "name": "Weir Group",
        "platform": "workday",
        "careers_url": "https://www.global.weir/careers/",
        "search_url": "https://weir.wd3.myworkdayjobs.com/Weir_External_Careers",
        "tenant": "weir",
        "wd_subdomain": "wd3",
        "site_id": "Weir_External_Careers",
        "applied_facets": {"Country": ["a30a87ed25634629aa6c3958aa2b91ea"]},
        "identity_regex": r"Weir",
        "allowed_apply_hosts": ["weir.wd3.myworkdayjobs.com"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
    },
]

EXCLUDED: list[dict[str, Any]] = [
    {
        "company": "Baytex Energy", "platform": "official static careers page", "jobs_observed": 0,
        "official_careers_url": "https://www.baytexenergy.com/current-opportunities/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The official page currently says there are no job postings; its summer-student section also has no current posting.",
    },
    {
        "company": "MEG Energy", "platform": "acquired company", "jobs_observed": 0,
        "official_careers_url": "https://www.megenergy.com/",
        "validation_status": "acquired_parent_source_required",
        "exclusion_reason": "MEG became a Cenovus subsidiary on November 13, 2025; exact attribution now requires a reviewed parent-source mapping.",
    },
    {
        "company": "Peyto Exploration", "platform": "no public careers collection", "jobs_observed": 0,
        "official_careers_url": "https://www.peyto.com/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The current official site exposes corporate and contact pages but no anonymous careers listing contract.",
    },
    {
        "company": "Birchcliff Energy", "platform": "official PDF postings", "jobs_observed": 2,
        "official_careers_url": "https://www.birchcliffenergy.com/careers",
        "validation_status": "unsupported_pdf_email_contract",
        "exclusion_reason": "Two current roles are direct PDFs with email application instructions; no reusable adapter establishes stable normalized apply contracts.",
    },
    {
        "company": "Obsidian Energy", "platform": "official PDF postings", "jobs_observed": 3,
        "official_careers_url": "https://obsidianenergy.com/careers/",
        "validation_status": "unsupported_pdf_email_contract",
        "exclusion_reason": "Three current field roles are direct PDFs with email application instructions; the existing HTML adapter does not parse PDF application contracts.",
    },
    {
        "company": "Kelt Exploration", "platform": "no public careers collection", "jobs_observed": 0,
        "official_careers_url": "https://keltexploration.com/contact/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The official contact page contains only recruitment-fraud guidance and no public jobs collection.",
    },
    {
        "company": "Cardinal Energy", "platform": "official modal/static page", "jobs_observed": 1,
        "official_careers_url": "https://cardinalenergy.ca/contact/careers/",
        "validation_status": "no_stable_detail_apply_contract",
        "exclusion_reason": "The current role uses a fragment-only Job Description control and exposes no stable job-specific public URL.",
    },
    {
        "company": "NuVista Energy", "platform": "acquired company", "jobs_observed": 0,
        "official_careers_url": "https://investor.ovintiv.com/2026-02-03-Ovintiv-Announces-Closing-of-NuVista-Energy-Acquisition",
        "validation_status": "acquired_parent_source_required",
        "exclusion_reason": "Ovintiv closed its acquisition of NuVista on February 3, 2026; a parent-source mapping is required for exact attribution.",
    },
    {
        "company": "Advantage Energy", "platform": "no public careers collection", "jobs_observed": 0,
        "official_careers_url": "https://www.advantageog.com/contact/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The current official site (renamed from Advantage Oil & Gas in 2021) has recruitment-fraud guidance but no anonymous listing source.",
    },
    {
        "company": "Cameco", "platform": "legacy SAP SuccessFactors", "jobs_observed": None,
        "official_careers_url": "https://www.cameco.com/careers/students",
        "validation_status": "unsupported_session_dependent_contract",
        "exclusion_reason": "The official student program is current, but the legacy career17 SuccessFactors board is not the reusable RMK contract and depends on client/session state; no bypass is implemented.",
    },
    {
        "company": "PrairieSky Royalty", "platform": "no public careers collection", "jobs_observed": 0,
        "official_careers_url": "https://www.prairiesky.com/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The official site has no current public careers collection; historical summer-student references are not a live source.",
    },
    {
        "company": "Topaz Energy", "platform": "no public careers collection", "jobs_observed": 0,
        "official_careers_url": "https://www.topazenergy.ca/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The official site exposes corporate, team, investor, and contact pages but no anonymous careers listing.",
    },
    {
        "company": "Total Energy Services", "platform": "divisional career sources", "jobs_observed": None,
        "official_careers_url": "https://www.totalenergy.ca/careers/",
        "validation_status": "child_source_attribution_required",
        "exclusion_reason": "The parent directs applicants to divisional websites; collecting those roles under the parent would violate exact employer attribution without mappings.",
    },
    {
        "company": "Enverus", "platform": "Jobvite", "jobs_observed": None,
        "official_careers_url": "https://www.enverus.com/careers/",
        "validation_status": "unsupported_javascript_contract",
        "exclusion_reason": "The official page embeds a Jobvite client; no existing adapter provides a reviewed bounded anonymous contract, so no new one-off adapter was added.",
    },
    {
        "company": "Freehold Royalties", "platform": "EasyApply/GetHired", "jobs_observed": 1,
        "official_careers_url": "https://freeholdroyalties.com/careers/",
        "validation_status": "unsupported_javascript_session_contract",
        "exclusion_reason": "The official link opens a JavaScript/CSRF-backed EasyApply tenant; no session, token, or browser workaround is implemented.",
    },
]

SAFE_TO_STAGE_FILES = [
    "config/company_alberta_energy_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.alberta_energy_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.alberta_energy_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_alberta_energy_expansion_v1_batch_a.py",
    "src/scraper/company_careers/adapters/static_html.py",
    "src/scraper/company_careers/adapters/workday.py",
    "src/scraper/company_careers/models.py",
    "tests/test_alberta_energy_expansion_v1_batch_a.py",
]

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "static_html": static_html.fetch_jobs,
    "adp": adp.fetch_jobs,
    "ukg_pro": ukg_pro.fetch_jobs,
    "paycor": paycor.fetch_jobs,
    "workday": workday.fetch_jobs,
}


def _signature(jobs: list[JobRecord]) -> str:
    rows = [(job.job_id, job.title, job.company, job.location, job.url) for job in jobs]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _pagination(session: requests.Session, platform: str) -> dict[str, Any]:
    return getattr(session, f"{platform}_pagination", {})


def _job_in_scope(job: JobRecord, entry: dict[str, Any]) -> bool:
    if IN_SCOPE_LOCATION_RE.search(job.location):
        return True
    return bool(
        job.location
        and entry.get("platform") == "workday"
        and entry.get("applied_facets")
        == {"Country": ["a30a87ed25634629aa6c3958aa2b91ea"]}
    )


def _validate_twice(entry: dict[str, Any]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    job_sets: list[list[JobRecord]] = []
    for run_number in range(1, REPEAT_RUNS + 1):
        session = requests.Session()
        started = time.monotonic()
        try:
            jobs = ADAPTERS[str(entry["platform"])](
                session, CompanyTarget.from_dict(entry), MAX_TIMEOUT_SECONDS
            )
            if len(jobs) > int(entry["max_jobs"]):
                raise AdapterError("adapter exceeded the configured deterministic job bound")
            if len({job.job_id for job in jobs}) != len(jobs):
                raise AdapterError("adapter returned duplicate stable IDs")
            if any(job.company != entry["name"] for job in jobs):
                raise AdapterError("adapter returned ambiguous company attribution")
            if any(not _job_in_scope(job, entry) for job in jobs):
                raise AdapterError("adapter returned a job outside Canada/US/Remote scope")
            run = {
                "run": run_number,
                "success": True,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_normalized": len(jobs),
                "student_hits": sum(bool(STUDENT_RE.search(job.title)) for job in jobs),
                "signature": _signature(jobs),
                "stable_ids": [job.job_id for job in jobs],
                "titles": [job.title for job in jobs],
                "locations": [job.location for job in jobs],
                "detail_apply_urls": [job.url for job in jobs],
                "pagination": _pagination(session, str(entry["platform"])),
            }
        except (AdapterError, requests.RequestException, ValueError) as exc:
            jobs = []
            run = {
                "run": run_number, "success": False,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_normalized": 0, "student_hits": 0, "signature": "", "error": str(exc),
            }
        runs.append(run)
        job_sets.append(jobs)
    stable = (
        all(run["success"] for run in runs)
        and runs[0]["signature"] == runs[1]["signature"]
        and runs[0]["jobs_normalized"] == runs[1]["jobs_normalized"]
    )
    jobs = job_sets[0] if stable else []
    return {
        "company": entry["name"], "platform": entry["platform"], "adapter": entry["platform"],
        "result": "validated" if stable else "excluded", "official_careers_url": entry["careers_url"],
        "validation_status": "repeatable_anonymous" if stable else "unstable_or_failed",
        "jobs_normalized": len(jobs),
        "current_student_intern_hits": sum(bool(STUDENT_RE.search(job.title)) for job in jobs),
        "current_student_role_evidence": [job.title for job in jobs if STUDENT_RE.search(job.title)],
        "stable_id_result": "valid" if stable else "invalid",
        "title_result": "valid" if stable and all(job.title for job in jobs) else "invalid",
        "location_result": "valid_in_scope" if stable and all(_job_in_scope(job, entry) for job in jobs) else "invalid",
        "apply_url_result": "valid" if stable and all(job.url.startswith("https://") for job in jobs) else "invalid",
        "company_attribution_result": "exact" if stable and all(job.company == entry["name"] for job in jobs) else "invalid",
        "deterministic_result": "stable_ordered_signature" if stable else "invalid",
        "production_ready": stable, "original_246_master": False, "master_rank": None,
        "runs": runs,
    }


def _excluded_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row, "adapter": "none", "result": "excluded", "jobs_normalized": 0,
        "current_student_intern_hits": 0, "stable_id_result": "not_validated",
        "title_result": "not_validated", "location_result": "not_validated",
        "apply_url_result": "not_validated", "company_attribution_result": "not_validated",
        "deterministic_result": "fail_closed", "production_ready": False,
        "original_246_master": False, "master_rank": None,
    }


def _write_csv(results: list[dict[str, Any]]) -> None:
    fields = [
        "company", "platform", "adapter", "result", "official_careers_url",
        "validation_status", "jobs_observed", "jobs_normalized", "current_student_intern_hits",
        "stable_id_result", "title_result", "location_result", "apply_url_result",
        "company_attribution_result", "deterministic_result", "production_ready",
        "original_246_master", "master_rank", "exclusion_reason",
    ]
    with CANDIDATE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def _mapping_count() -> int:
    payload = yaml.safe_load(PARENT_MAPPINGS.read_text(encoding="utf-8")) or {}
    rows = payload.get("company_parent_source_mappings") or payload.get("mappings") or []
    return len(rows)


def build() -> dict[str, Any]:
    production_bytes = PRODUCTION.read_bytes()
    production_payload = yaml.safe_load(production_bytes)
    production_entries = list(production_payload["target_companies"])
    if len(production_entries) != BASELINE_SOURCES:
        raise RuntimeError(f"Expected {BASELINE_SOURCES} production sources, found {len(production_entries)}")
    if _mapping_count() != BASELINE_MAPPINGS:
        raise RuntimeError(f"Expected {BASELINE_MAPPINGS} parent mappings, found {_mapping_count()}")
    existing = {str(entry.get("name") or "").casefold() for entry in production_entries}
    duplicates = [entry["name"] for entry in CANDIDATE_ENTRIES if entry["name"].casefold() in existing]
    if duplicates:
        raise RuntimeError(f"Candidate companies already exist in production: {duplicates}")
    config_errors = {
        entry["name"]: registry_validator.configuration_error(entry) for entry in CANDIDATE_ENTRIES
    }
    if any(config_errors.values()):
        raise RuntimeError(f"Candidate configuration errors: {config_errors}")

    validated = [_validate_twice(entry) for entry in CANDIDATE_ENTRIES]
    if not all(row["production_ready"] for row in validated):
        failures = {row["company"]: row["validation_status"] for row in validated if not row["production_ready"]}
        raise RuntimeError(f"At least one candidate failed closed: {failures}")
    by_name = {row["company"]: row for row in [*validated, *map(_excluded_row, EXCLUDED)]}
    if set(by_name) != set(REVIEWED_COMPANIES):
        raise RuntimeError("Reviewed company inventory drifted")
    results = [by_name[name] for name in REVIEWED_COMPANIES]

    candidate_bytes = registry_validator.build_candidate_registry_bytes(PRODUCTION, CANDIDATE_ENTRIES)
    CANDIDATE_REGISTRY.write_bytes(candidate_bytes)
    VALIDATED_REGISTRY.write_bytes(candidate_bytes)
    _write_csv(results)
    projected_sources = BASELINE_SOURCES + len(CANDIDATE_ENTRIES)
    report = {
        "batch": "Alberta Energy Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "maximum_pages_per_source": 1, "maximum_normalized_jobs_per_source": 20,
            "repeat_validation_runs": REPEAT_RUNS, "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": ["static_html", "adp", "ukg_pro", "paycor", "workday"],
        "generic_adapter_improvements": [
            "Static HTML supports an explicit fixed_location for officially scoped listing pages.",
            "Workday falls back to a requisition suffix or unique posting-path token when bullet fields are presentation metadata.",
        ],
        "validated_companies": [row["company"] for row in validated],
        "excluded_companies": [row["company"] for row in results if not row["production_ready"]],
        "results": results,
        "jobs": {
            "normalized_total": sum(row["jobs_normalized"] for row in validated),
            "student_intern_hits_total": sum(row["current_student_intern_hits"] for row in validated),
            "by_company": {
                row["company"]: {
                    "jobs": row["jobs_normalized"],
                    "student_intern_hits": row["current_student_intern_hits"],
                }
                for row in validated
            },
        },
        "projection": {
            "production_sources": projected_sources, "source_delta": len(CANDIDATE_ENTRIES),
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}", "master_delta": 0,
        },
        "candidate_registry": str(CANDIDATE_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "validated_registry": str(VALIDATED_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "safe_to_stage_files": SAFE_TO_STAGE_FILES,
        "production_registry_activated": False,
        "publication": {
            "production_run": False, "staged": False, "committed": False, "pushed": False,
            "dashboard_scan": False, "dashboard_publication": False,
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if PRODUCTION.read_bytes() != production_bytes:
        raise RuntimeError("Production registry changed during non-activation validation")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    report = build()
    print(json.dumps({
        "validated": report["validated_companies"], "excluded": report["excluded_companies"],
        "jobs": report["jobs"], "projection": report["projection"],
        "production_registry_activated": report["production_registry_activated"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
