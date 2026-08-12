"""Build and validate ATS Adapter Expansion v2 Batch B without activation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import deel, talentbrew, teamtailor
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_ats_adapter_expansion_v2_batch_b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.ats_adapter_expansion_v2_batch_b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.ats_adapter_expansion_v2_batch_b_validated.yaml"
REPORT = ROOT / "output" / "ats_adapter_expansion_v2_batch_b_validation.json"

BASELINE_SOURCES = 338
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
FIXTURES = [
    "Best Buy",
    "Nordic Semiconductor",
    "Palo Alto Networks",
    "Rakuten",
    "Carbon Upcycling",
]

CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "Nordic Semiconductor",
        "platform": "teamtailor",
        "careers_url": "https://www.nordicsemi.com/About-us/Careers",
        "listing_url": "https://careers.nordicsemi.com/jobs",
        "endpoint_variant": teamtailor.ENDPOINT_VARIANT,
        "source_identity_regex": r"Nordic Semiconductor|Nordic",
        "identity_regex": r"Nordic Semiconductor",
        "allowed_apply_hosts": ["careers.nordicsemi.com"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    },
    {
        "name": "Palo Alto Networks",
        "platform": "talentbrew",
        "careers_url": "https://jobs.paloaltonetworks.com/en",
        "listing_url": "https://jobs.paloaltonetworks.com/en/location/canada-jobs/47263/6251999/2",
        "endpoint_variant": talentbrew.ENDPOINT_VARIANT,
        "source_identity_regex": r"Palo Alto Networks",
        "identity_regex": r"Palo Alto Networks",
        "allowed_apply_hosts": ["jobs.paloaltonetworks.com"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    },
    {
        "name": "Rakuten",
        "platform": "talentbrew",
        "careers_url": "https://global.rakuten.com/corp/careers/",
        "listing_url": "https://japan-job-en.rakuten.careers/search-jobs/",
        "endpoint_variant": talentbrew.ENDPOINT_VARIANT,
        "source_identity_regex": r"Rakuten",
        "identity_regex": r"Rakuten",
        "allowed_apply_hosts": ["japan-job-en.rakuten.careers"],
        "original_workbook_rank": 212,
        "max_pages": 2,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    },
    {
        "name": "Carbon Upcycling",
        "platform": "deel",
        "careers_url": "https://carbonupcycling.com/careers/",
        "listing_url": "https://jobs.deel.com/carbon-upcycling-technologies",
        "endpoint_variant": deel.ENDPOINT_VARIANT,
        "tenant": "carbon-upcycling-technologies",
        "source_identity_regex": r"Carbon Upcycling Technologies",
        "identity_regex": r"Carbon Upcycling Technologies",
        "allowed_apply_hosts": ["jobs.deel.com"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    },
]

SAFE_TO_STAGE_FILES = [
    "config/company_ats_adapter_expansion_v2_batch_b_candidates.csv",
    "config/target_companies.ats_adapter_expansion_v2_batch_b_candidates.yaml",
    "config/target_companies.ats_adapter_expansion_v2_batch_b_validated.yaml",
    "scripts/company_watchlist_common.py",
    "scripts/fingerprint_company_platforms.py",
    "scripts/investigate_remaining_companies.py",
    "scripts/investigate_unknown_companies.py",
    "scripts/validate_ats_adapter_expansion_v2_batch_b.py",
    "scripts/validate_company_registry.py",
    "src/scraper/company_careers/adapters/__init__.py",
    "src/scraper/company_careers/adapters/deel.py",
    "src/scraper/company_careers/adapters/talentbrew.py",
    "src/scraper/company_careers/adapters/teamtailor.py",
    "src/scraper/company_careers/scanner.py",
    "tests/test_ats_adapter_expansion_v2_batch_b.py",
]

STUDENT_RE = re.compile(
    r"(?i)\b(intern(?:ship)?|student|co[ -]?op|new grad(?:uate)?|graduate program)\b"
)


def _signature(jobs: list[JobRecord]) -> str:
    rows = [(job.job_id, job.title, job.location, job.url) for job in jobs]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _adapter(entry: dict[str, Any]):
    return {
        "teamtailor": teamtailor.fetch_jobs,
        "talentbrew": talentbrew.fetch_jobs,
        "deel": deel.fetch_jobs,
    }[str(entry["platform"])]


def _validate_twice(entry: dict[str, Any]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    job_sets: list[list[JobRecord]] = []
    for run_number in (1, 2):
        target = CompanyTarget.from_dict(entry)
        session = requests.Session()
        started = time.monotonic()
        try:
            jobs = _adapter(entry)(session, target, MAX_TIMEOUT_SECONDS)
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
                "pagination": getattr(session, f"{entry['platform']}_pagination", {}),
            }
            job_sets.append(jobs)
        except (AdapterError, requests.RequestException) as exc:
            run = {
                "run": run_number,
                "success": False,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "error": str(exc),
                "jobs_normalized": 0,
                "student_hits": 0,
                "signature": "",
            }
            job_sets.append([])
        runs.append(run)
    stable = (
        all(run["success"] for run in runs)
        and runs[0]["signature"] == runs[1]["signature"]
        and runs[0]["jobs_normalized"] == runs[1]["jobs_normalized"]
    )
    jobs = job_sets[0] if stable else []
    return {
        "company": entry["name"],
        "platform": entry["platform"],
        "adapter": entry["platform"],
        "result": "recovered" if entry["name"] == "Carbon Upcycling" else "migrated",
        "official_careers_url": entry["careers_url"],
        "listing_url": entry["listing_url"],
        "endpoint_variant": entry["endpoint_variant"],
        "validation_status": (
            "repeatable_anonymous_zero_jobs"
            if stable and not jobs
            else "repeatable_anonymous"
            if stable
            else "unstable_or_failed"
        ),
        "validation_runs": 2,
        "jobs_normalized": len(jobs),
        "current_student_intern_hits": sum(bool(STUDENT_RE.search(job.title)) for job in jobs),
        "current_student_role_evidence": [job.title for job in jobs if STUDENT_RE.search(job.title)],
        "stable_id_result": "valid" if stable and jobs else "not_applicable_zero_jobs" if stable else "invalid",
        "title_result": "valid" if stable and all(job.title for job in jobs) else "not_applicable_zero_jobs" if stable else "invalid",
        "location_result": "valid" if stable and all(job.location for job in jobs) else "not_applicable_zero_jobs" if stable else "invalid",
        "apply_url_result": "valid" if stable and all(job.url for job in jobs) else "not_applicable_zero_jobs" if stable else "invalid",
        "deterministic_result": "stable_ordered_signature" if stable else "invalid",
        "production_ready": stable,
        "original_246_master": entry.get("original_workbook_rank") is not None,
        "master_rank": entry.get("original_workbook_rank"),
        "runs": runs,
    }


def _find_map_widget(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if value.get("name") == "Best Buy Career Map" and value.get("public") is True:
            return value
        for child in value.values():
            found = _find_map_widget(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_map_widget(child)
            if found:
                return found
    return None


def _probe_best_buy() -> dict[str, Any]:
    page_url = "https://jobs.bestbuy.com/api/now/sp/page?id=all_jobs&spa=1"
    session = requests.Session()
    try:
        response = session.get(
            page_url,
            headers={"Accept": "application/json", "User-Agent": "job-radar/1.0"},
            timeout=MAX_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        widget = _find_map_widget(payload.get("result"))
        if not widget or not widget.get("sys_id"):
            raise ValueError("public Best Buy careers map widget was not present")
        update = session.post(
            f"https://jobs.bestbuy.com/api/now/sp/widget/{widget['sys_id']}",
            json={"action": "update_data", "options": widget.get("options") or {}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=MAX_TIMEOUT_SECONDS,
        )
        update_payload = update.json()
        invalid_token = bool((update_payload.get("result") or {}).get("invalid_token"))
        evidence = {
            "page_status": response.status_code,
            "page_content_type": response.headers.get("content-type", ""),
            "public_widget": True,
            "widget_sys_id": widget["sys_id"],
            "widget_update_status": update.status_code,
            "widget_update_invalid_token": invalid_token,
        }
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        invalid_token = False
        evidence = {"probe_error": str(exc)}
    return {
        "company": "Best Buy",
        "platform": "ServiceNow careers",
        "adapter": "none",
        "result": "excluded",
        "official_careers_url": "https://jobs.bestbuy.com/",
        "listing_url": "https://jobs.bestbuy.com/bby?id=all_jobs&spa=1",
        "endpoint_url": page_url,
        "validation_status": "session_token_dependency" if invalid_token else "contract_not_validated",
        "jobs_normalized": 0,
        "current_student_intern_hits": 0,
        "stable_id_result": "not_validated",
        "title_result": "not_validated",
        "location_result": "not_validated",
        "apply_url_result": "not_validated",
        "deterministic_result": "fail_closed",
        "production_ready": False,
        "original_246_master": True,
        "master_rank": 215,
        "exclusion_reason": (
            "The anonymous ServiceNow page contract exposes only widget metadata. "
            "The public job-widget update rejects a request without its session-bound token; "
            "no cookie, token, browser, CAPTCHA, WAF, or session workaround is implemented."
        ),
        "evidence": evidence,
    }


def _candidate_registry() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    production = yaml.safe_load(PRODUCTION.read_text(encoding="utf-8"))
    entries = [dict(entry) for entry in production["target_companies"]]
    if len(entries) != BASELINE_SOURCES:
        raise RuntimeError(f"Expected {BASELINE_SOURCES} production sources, found {len(entries)}")
    replacements = {entry["name"]: entry for entry in CANDIDATE_ENTRIES[:-1]}
    replaced: set[str] = set()
    candidate: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if name in replacements:
            candidate.append(dict(replacements[name]))
            replaced.add(name)
        else:
            candidate.append(entry)
    if replaced != set(replacements):
        raise RuntimeError(f"Missing migration fixtures: {sorted(set(replacements) - replaced)}")
    if any(entry.get("name") == "Carbon Upcycling" for entry in candidate):
        raise RuntimeError("Carbon Upcycling is already present in production")
    candidate.append(dict(CANDIDATE_ENTRIES[-1]))
    return production, candidate


def _write_yaml(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(
        yaml.safe_dump({"target_companies": entries}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_csv(results: list[dict[str, Any]]) -> None:
    fields = [
        "company", "platform", "adapter", "result", "official_careers_url", "listing_url",
        "validation_status", "jobs_normalized", "current_student_intern_hits",
        "stable_id_result", "title_result", "location_result", "apply_url_result",
        "deterministic_result", "production_ready", "original_246_master", "master_rank",
        "exclusion_reason",
    ]
    with CANDIDATE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def build() -> dict[str, Any]:
    production_bytes = PRODUCTION.read_bytes()
    production, candidate = _candidate_registry()
    config_errors = {
        entry["name"]: registry_validator.configuration_error(entry)
        for entry in CANDIDATE_ENTRIES
    }
    if any(config_errors.values()):
        raise RuntimeError(f"Candidate configuration errors: {config_errors}")
    migrated = [_validate_twice(entry) for entry in CANDIDATE_ENTRIES]
    if not all(row["production_ready"] for row in migrated):
        raise RuntimeError("At least one candidate did not produce a stable anonymous contract")
    best_buy = _probe_best_buy()
    results_by_name = {row["company"]: row for row in [best_buy, *migrated]}
    results = [results_by_name[name] for name in FIXTURES]
    _write_yaml(CANDIDATE_REGISTRY, candidate)
    _write_yaml(VALIDATED_REGISTRY, candidate)
    _write_csv(results)
    mappings = yaml.safe_load(PARENT_MAPPINGS.read_text(encoding="utf-8")) or {}
    mapping_rows = (
        mappings.get("mappings")
        or mappings.get("parent_source_mappings")
        or mappings.get("company_parent_source_mappings")
        or []
    )
    report = {
        "batch": "ATS Adapter Expansion v2 Batch B",
        "fixtures": FIXTURES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "maximum_pages_per_source": 2,
            "maximum_normalized_jobs_per_source": 20,
            "repeat_validation_runs": 2,
            "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapters_implemented": [
            {
                "platform": "Teamtailor",
                "adapter": "teamtailor",
                "contract": "server-rendered jobs_list_container with numeric detail IDs",
            },
            {
                "platform": "TalentBrew / Radancy",
                "adapter": "talentbrew",
                "contract": "server-rendered search results with stable data-job-id attributes",
            },
            {
                "platform": "Deel",
                "adapter": "deel",
                "contract": "schema.org Organization + ItemList + bounded JobPosting details",
            },
        ],
        "migrated_fixtures": ["Nordic Semiconductor", "Palo Alto Networks", "Rakuten"],
        "recovered_fixtures": ["Carbon Upcycling"],
        "remaining_blockers": [best_buy],
        "results": results,
        "jobs": {
            "normalized_total": sum(row["jobs_normalized"] for row in results),
            "student_intern_hits_total": sum(row["current_student_intern_hits"] for row in results),
            "by_company": {
                row["company"]: {
                    "jobs": row["jobs_normalized"],
                    "student_intern_hits": row["current_student_intern_hits"],
                }
                for row in results
            },
        },
        "projection": {
            "production_sources": BASELINE_SOURCES + 1,
            "parent_mappings": len(mapping_rows),
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
            "master_delta": 0,
        },
        "production_registry_activated": False,
        "publication": {
            "production_run": False,
            "staged": False,
            "committed": False,
            "pushed": False,
            "dashboard_scan": False,
            "dashboard_publication": False,
        },
        "candidate_registry": str(CANDIDATE_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "validated_registry": str(VALIDATED_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "safe_to_stage_files": SAFE_TO_STAGE_FILES,
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if PRODUCTION.read_bytes() != production_bytes or production != yaml.safe_load(production_bytes):
        raise RuntimeError("Production registry changed during non-activation validation")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    report = build()
    print(json.dumps({
        "results": {row["company"]: row["validation_status"] for row in report["results"]},
        "jobs": report["jobs"],
        "projection": report["projection"],
        "production_registry_activated": report["production_registry_activated"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
