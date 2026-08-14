"""Build and validate Robotics Expansion v1 Batch A without activation."""

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
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import adp, bamboohr, workable, workday
from src.scraper.company_careers.adapters import ashby, greenhouse, lever
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_robotics_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.robotics_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.robotics_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "robotics_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 370
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "Franka Robotics", "Berkshire Grey", "RightHand Robotics", "6 River Systems",
    "Fetch Robotics", "Diligent Robotics", "Cobalt Robotics", "Zipline", "Wing",
    "Formant", "Vicarious Surgical", "Realtime Robotics", "Bright Machines",
    "Symbotic", "Plus One Robotics", "Covariant", "Dexterity", "Built Robotics",
    "Chef Robotics", "Attabotics", "Avidbots", "Novarc Technologies", "RoboSense",
]

REVIEW_NAME_BY_CANONICAL = {"Cobalt AI": "Cobalt Robotics"}
STUDENT_RE = re.compile(
    r"(?i)\b(intern(?:ship)?|student|co[ -]?op|new grad(?:uate)?|graduate program)\b"
)
SUMMER_2027_RE = re.compile(r"(?i)\bsummer\s+2027\b")
FOCUS_RE = re.compile(
    r"(?i)\b(robot(?:ics?)?|autonom(?:y|ous)|perception|computer vision|controls?|"
    r"embedded|firmware|electrical|machine learning|ML|AI|software|simulation|"
    r"mechatronics?|hardware|systems? engineer|manufacturing engineer)\b"
)
IN_SCOPE_RE = re.compile(
    r"(?i)\b(canada|united states|usa|us|u\.s\.|remote|anywhere in the us|"
    r"alberta|british columbia|ontario|quebec|massachusetts|texas|california|"
    r"colorado|illinois|utah|tennessee|north carolina|new york|ohio|wisconsin|"
    r"maryland|san francisco|redwood city|florence|austin|bedford|burnaby|"
    r"kitchener|waterloo|tracy|gardena|hagerstown)\b|"
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
    r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
    r"TX|UT|VT|VA|WA|WV|WI|WY)\b"
)


def _common(name: str, platform: str, careers_url: str, **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "platform": platform,
        "careers_url": careers_url,
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _common(
        "Berkshire Grey", "bamboohr", "https://www.berkshiregrey.com/careers/",
        slug="berkshiregrey", listing_url="https://berkshiregrey.bamboohr.com/careers/list",
        source_identity_regex=r"Berkshire Grey", allowed_apply_hosts=["berkshiregrey.bamboohr.com"],
    ),
    _common(
        "Diligent Robotics", "greenhouse", "https://www.diligentrobots.com/about/",
        slug="diligentrobotics", source_identity_regex=r"Diligent Robotics",
        allowed_apply_hosts=["job-boards.greenhouse.io"],
    ),
    _common(
        "Cobalt AI", "bamboohr", "https://www.cobaltai.com/careers/",
        slug="cobaltai", listing_url="https://cobaltai.bamboohr.com/careers/list",
        source_identity_regex=r"Cobalt AI|Cobalt Robotics",
        allowed_apply_hosts=["cobaltai.bamboohr.com"],
    ),
    _common(
        "Zipline", "greenhouse", "https://www.zipline.com/careers",
        slug="flyzipline", department_allowlist=["2027 Internships"],
        source_identity_regex=r"Zipline", allowed_apply_hosts=["www.zipline.com"],
        max_response_bytes=5_000_000,
    ),
    _common(
        "Bright Machines", "lever", "https://www.brightmachines.com/careers/",
        slug="brightmachines", endpoint_variant=lever.ENDPOINT_VARIANT,
        source_identity_regex=r"Bright Machines", allowed_apply_hosts=["jobs.lever.co"],
    ),
    _common(
        "Symbotic", "workday", "https://www.symbotic.com/careers/",
        tenant="symbotic", wd_subdomain="wd504", site_id="Symbotic",
        search_url="https://symbotic.wd504.myworkdayjobs.com/Symbotic",
        source_identity_regex=r"Symbotic", allowed_apply_hosts=["symbotic.wd504.myworkdayjobs.com"],
    ),
    _common(
        "Plus One Robotics", "bamboohr", "https://www.plusonerobotics.com/careers",
        slug="plusonerobotics", listing_url="https://plusonerobotics.bamboohr.com/careers/list",
        source_identity_regex=r"Plus One Robotics", allowed_apply_hosts=["plusonerobotics.bamboohr.com"],
    ),
    _common(
        "Dexterity", "lever", "https://jobs.lever.co/dexterity",
        slug="dexterity", endpoint_variant=lever.ENDPOINT_VARIANT,
        source_identity_regex=r"Dexterity", allowed_apply_hosts=["jobs.lever.co"],
    ),
    _common(
        "Built Robotics", "ashby", "https://www.builtrobotics.com/careers/openings",
        slug="built-robotics", source_identity_regex=r"Built Robotics",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
    ),
    _common(
        "Chef Robotics", "lever", "https://www.chefrobotics.ai/careers",
        slug="ChefRobotics", endpoint_variant=lever.ENDPOINT_VARIANT,
        source_identity_regex=r"Chef Robotics", allowed_apply_hosts=["jobs.lever.co"],
    ),
    _common(
        "Avidbots", "bamboohr", "https://avidbots.com/company/careers/",
        slug="avidbots", listing_url="https://avidbots.bamboohr.com/careers/list",
        source_identity_regex=r"Avidbots", allowed_apply_hosts=["avidbots.bamboohr.com"],
    ),
    _common(
        "Novarc Technologies", "adp",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        "?cid=d355e8f6-9a6c-48a9-b7ba-45a41dc5daad&ccId=9200648065638_2&lang=en_CA",
        endpoint_url=(
            "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
            "staffing/v1/job-requisitions"
        ),
        endpoint_variant=adp.WORKFORCE_NOW,
        identity_regex=r"WNVPD0000191809",
        source_identity_regex=r"Novarc Technologies|WNVPD0000191809",
        allowed_apply_hosts=["workforcenow.adp.com"], page_size=10, max_pages=2,
    ),
]


EXCLUDED: list[dict[str, Any]] = [
    {
        "company": "Franka Robotics", "platform": "Personio", "jobs_observed": None,
        "official_careers_url": "https://franka.de/careers",
        "validation_status": "acquired_rebranded_out_of_scope",
        "exclusion_reason": (
            "Agile Robots acquired Franka Emika in 2023 and renamed it Franka Robotics. "
            "The current official Personio source is Germany-focused, outside this Canada/US/Remote batch."
        ),
    },
    {
        "company": "RightHand Robotics", "platform": "first-party static", "jobs_observed": 0,
        "official_careers_url": "https://righthandrobotics.com/careers",
        "validation_status": "no_current_job_contract",
        "exclusion_reason": (
            "The live official careers HTML currently exposes no job card or stable job-specific apply URL; "
            "search-indexed older role text is not used as a current contract."
        ),
    },
    {
        "company": "6 River Systems", "platform": "Ocado Group parent", "jobs_observed": None,
        "official_careers_url": "https://careers.ocadogroup.com/",
        "validation_status": "acquired_parent_source_required",
        "exclusion_reason": (
            "Ocado Group acquired 6 River Systems from Shopify in 2023. The parent board does not expose a "
            "reviewed exact child-brand field, so routing it without a mapping would weaken attribution."
        ),
    },
    {
        "company": "Fetch Robotics", "platform": "Zebra Technologies parent", "jobs_observed": None,
        "official_careers_url": "https://www.zebra.com/us/en/about-zebra/careers.html",
        "validation_status": "acquired_parent_source_required",
        "exclusion_reason": (
            "Zebra completed its Fetch Robotics acquisition in 2021. Zebra is already a production source, "
            "but no current exact Fetch child-brand metadata was observed for a safe parent mapping."
        ),
    },
    {
        "company": "Wing", "platform": "first-party/Alphabet", "jobs_observed": 0,
        "official_careers_url": "https://wing.com/careers",
        "validation_status": "no_current_jobs_for_validation",
        "exclusion_reason": (
            "Wing's official careers page and internship route are live, but the current collection explicitly "
            "reports no positions, so current stable IDs and job-specific apply URLs cannot be validated."
        ),
    },
    {
        "company": "Formant", "platform": "Rippling ATS", "jobs_observed": 3,
        "official_careers_url": "https://ats.rippling.com/formant-careers/jobs",
        "validation_status": "unsupported_platform_variant",
        "exclusion_reason": (
            "The exact official Rippling board has current US-remote roles, but the repository has no bounded "
            "Rippling adapter; a one-company parser was not added."
        ),
    },
    {
        "company": "Vicarious Surgical", "platform": "Greenhouse", "jobs_observed": 0,
        "official_careers_url": "https://www.vicarioussurgical.com/careers",
        "validation_status": "no_current_jobs_for_validation",
        "exclusion_reason": (
            "The official site still identifies its exact vicarioussurgical Greenhouse board, but that anonymous "
            "collection is empty, preventing current job ID/detail/apply validation."
        ),
    },
    {
        "company": "Realtime Robotics", "platform": "Workable", "jobs_observed": 5,
        "official_careers_url": "https://rtr.ai/careers/",
        "validation_status": "captcha_protected_apply_flow",
        "exclusion_reason": (
            "The public Workable widget has current jobs, including a Berlin robotics intern, but the official "
            "apply portal advertises challenge/CAPTCHA state. The existing adapter fails closed and no bypass is used."
        ),
    },
    {
        "company": "Covariant", "platform": "stale first-party", "jobs_observed": 0,
        "official_careers_url": "https://covariant.ai/careers/",
        "validation_status": "no_current_official_job_collection",
        "exclusion_reason": (
            "Amazon's 2024 transaction was a non-exclusive technology licence and staff transfer, not an "
            "acquisition. Covariant remains separate, but its careers route is absent and has no current contract."
        ),
    },
    {
        "company": "Attabotics", "platform": "Lever", "jobs_observed": 0,
        "official_careers_url": "https://attabotics.com/careers/",
        "validation_status": "no_current_jobs_for_validation",
        "exclusion_reason": (
            "The operating Calgary company's official page still links jobs.lever.co/attabotics, but the exact "
            "anonymous board remains an explicit zero-job collection."
        ),
    },
    {
        "company": "RoboSense", "platform": "first-party join-us page", "jobs_observed": 0,
        "official_careers_url": "https://www.robosense.ai/en/about/joinus",
        "validation_status": "no_in_scope_anonymous_job_contract",
        "exclusion_reason": (
            "The active official global join-us page exposes no public job collection or Canada/US/Remote posting "
            "contract; China contact details alone are not an in-scope source."
        ),
    },
]


SAFE_TO_STAGE_FILES = [
    "config/company_robotics_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.robotics_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.robotics_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_robotics_expansion_v1_batch_a.py",
    "src/scraper/company_careers/adapters/adp.py",
    "src/scraper/company_careers/adapters/greenhouse.py",
    "tests/test_robotics_expansion_v1_batch_a.py",
]

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "adp": adp.fetch_jobs,
    "ashby": ashby.fetch_jobs,
    "bamboohr": bamboohr.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "workable": workable.fetch_jobs,
    "workday": workday.fetch_jobs,
}


def _signature(jobs: list[JobRecord]) -> str:
    rows = [(job.job_id, job.title, job.company, job.location, job.url) for job in jobs]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _in_scope(job: JobRecord) -> bool:
    return bool(job.location and IN_SCOPE_RE.search(job.location))


def _allowed_url(url: str, hosts: list[str]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and any(
        host == allowed.casefold() or host.endswith("." + allowed.casefold())
        for allowed in hosts
    )


def _pagination(session: requests.Session, platform: str) -> dict[str, Any]:
    return getattr(session, f"{platform}_pagination", {})


def _validate_twice(entry: dict[str, Any]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    job_sets: list[list[JobRecord]] = []
    for run_number in range(1, REPEAT_RUNS + 1):
        session = requests.Session()
        started = time.monotonic()
        try:
            observed = ADAPTERS[str(entry["platform"])](
                session, CompanyTarget.from_dict(entry), MAX_TIMEOUT_SECONDS
            )
            if len(observed) > int(entry["max_jobs"]):
                raise AdapterError("adapter exceeded the configured job bound")
            jobs = [job for job in observed if _in_scope(job)]
            if not jobs:
                raise AdapterError("source returned no Canada/US/Remote jobs")
            if len({job.job_id for job in jobs}) != len(jobs):
                raise AdapterError("adapter returned duplicate stable IDs")
            if any(job.company != entry["name"] for job in jobs):
                raise AdapterError("adapter returned ambiguous company attribution")
            if any(not _allowed_url(job.url, entry["allowed_apply_hosts"]) for job in jobs):
                raise AdapterError("adapter returned a non-allow-listed apply URL")
            run = {
                "run": run_number,
                "success": True,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": len(observed),
                "jobs_normalized": len(jobs),
                "out_of_scope_filtered": len(observed) - len(jobs),
                "student_hits": sum(bool(STUDENT_RE.search(job.title)) for job in jobs),
                "summer_2027_hits": sum(bool(SUMMER_2027_RE.search(job.title)) for job in jobs),
                "focus_hits": sum(bool(FOCUS_RE.search(job.title)) for job in jobs),
                "signature": _signature(jobs),
                "stable_ids": [job.job_id for job in jobs],
                "titles": [job.title for job in jobs],
                "locations": [job.location for job in jobs],
                "detail_apply_urls": [job.url for job in jobs],
                "normalized_job_evidence": [
                    {"job_id": job.job_id, "title": job.title, "location": job.location, "url": job.url}
                    for job in jobs
                ],
                "pagination": _pagination(session, str(entry["platform"])),
            }
        except (AdapterError, requests.RequestException, TypeError, ValueError) as exc:
            jobs = []
            run = {
                "run": run_number, "success": False,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": 0, "jobs_normalized": 0, "signature": "", "error": str(exc),
            }
        runs.append(run)
        job_sets.append(jobs)
    stable = (
        all(run["success"] for run in runs)
        and runs[0]["signature"] == runs[1]["signature"]
        and runs[0]["jobs_normalized"] == runs[1]["jobs_normalized"]
    )
    jobs = job_sets[0] if stable else []
    review_name = REVIEW_NAME_BY_CANONICAL.get(entry["name"], entry["name"])
    return {
        "company": review_name,
        "canonical_company": entry["name"],
        "platform": entry["platform"],
        "adapter": entry["platform"],
        "result": "validated" if stable else "excluded",
        "official_careers_url": entry["careers_url"],
        "validation_status": "repeatable_anonymous" if stable else "unstable_or_failed",
        "jobs_observed": runs[0].get("jobs_observed", 0),
        "jobs_normalized": len(jobs),
        "current_student_intern_hits": sum(bool(STUDENT_RE.search(job.title)) for job in jobs),
        "summer_2027_hits": sum(bool(SUMMER_2027_RE.search(job.title)) for job in jobs),
        "current_focus_role_hits": sum(bool(FOCUS_RE.search(job.title)) for job in jobs),
        "current_student_role_evidence": [job.title for job in jobs if STUDENT_RE.search(job.title)],
        "current_focus_role_evidence": [job.title for job in jobs if FOCUS_RE.search(job.title)],
        "stable_id_result": "valid" if stable else "invalid",
        "title_result": "valid" if stable and all(job.title for job in jobs) else "invalid",
        "location_result": "valid_in_scope" if stable and all(_in_scope(job) for job in jobs) else "invalid",
        "apply_url_result": "valid" if stable else "invalid",
        "company_attribution_result": "exact_current_name" if stable else "invalid",
        "deterministic_result": "stable_ordered_signature" if stable else "invalid",
        "production_ready": stable,
        "original_246_master": False,
        "master_rank": None,
        "runs": runs,
    }


def _excluded_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "canonical_company": row["company"],
        "adapter": "none",
        "result": "excluded",
        "jobs_normalized": 0,
        "current_student_intern_hits": 0,
        "summer_2027_hits": 0,
        "current_focus_role_hits": 0,
        "current_student_role_evidence": [],
        "current_focus_role_evidence": [],
        "stable_id_result": "not_validated",
        "title_result": "not_validated",
        "location_result": "not_validated",
        "apply_url_result": "not_validated",
        "company_attribution_result": "not_validated",
        "deterministic_result": "fail_closed",
        "production_ready": False,
        "original_246_master": False,
        "master_rank": None,
        "runs": [],
    }


def _write_csv(results: list[dict[str, Any]]) -> None:
    fields = [
        "company", "canonical_company", "platform", "adapter", "result",
        "official_careers_url", "validation_status", "jobs_observed", "jobs_normalized",
        "current_student_intern_hits", "summer_2027_hits", "current_focus_role_hits",
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
    return len(payload.get("company_parent_source_mappings") or payload.get("mappings") or [])


def build() -> dict[str, Any]:
    production_bytes = PRODUCTION.read_bytes()
    production_payload = yaml.safe_load(production_bytes) or {}
    production_entries = list(production_payload.get("target_companies") or [])
    if len(production_entries) != BASELINE_SOURCES:
        raise RuntimeError(f"Expected {BASELINE_SOURCES} production sources, found {len(production_entries)}")
    if _mapping_count() != BASELINE_MAPPINGS:
        raise RuntimeError(f"Expected {BASELINE_MAPPINGS} parent mappings, found {_mapping_count()}")

    existing = {str(entry.get("name") or "").strip().casefold() for entry in production_entries}
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
        failures = {row["company"]: row["runs"][0].get("error") for row in validated if not row["production_ready"]}
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
        "batch": "Robotics Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "maximum_pages_per_source": 2,
            "maximum_normalized_jobs_per_source": 20,
            "repeat_validation_runs": REPEAT_RUNS,
            "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": ["adp", "ashby", "bamboohr", "greenhouse", "lever", "workday"],
        "generic_adapter_improvements": [
            "Greenhouse now enforces configured result bounds and an optional exact department allowlist.",
            "ADP now ignores empty location placeholders only when another stable location remains.",
        ],
        "validated_companies": [row["company"] for row in validated],
        "validated_canonical_companies": [row["canonical_company"] for row in validated],
        "excluded_companies": [row["company"] for row in results if not row["production_ready"]],
        "stale_acquired_rebranded": {
            "Franka Robotics": "Acquired by Agile Robots; Franka Emika renamed Franka Robotics; current roles are outside scope.",
            "Berkshire Grey": "Acquired by SoftBank in 2023; exact Berkshire Grey BambooHR remains active.",
            "6 River Systems": "Acquired by Ocado Group in 2023; exact child attribution unavailable.",
            "Fetch Robotics": "Acquired by Zebra in 2021; exact child attribution unavailable on the parent source.",
            "Cobalt Robotics": "Renamed Cobalt AI and acquired by Dean Drako in 2024; exact Cobalt AI board remains active.",
            "Covariant": "Amazon licensed technology and hired staff in 2024 but did not acquire Covariant; careers source is stale.",
        },
        "results": results,
        "jobs": {
            "normalized_total": sum(row["jobs_normalized"] for row in validated),
            "student_intern_hits_total": sum(row["current_student_intern_hits"] for row in validated),
            "summer_2027_hits_total": sum(row["summer_2027_hits"] for row in validated),
            "focus_role_hits_total": sum(row["current_focus_role_hits"] for row in validated),
            "by_company": {
                row["company"]: {
                    "jobs": row["jobs_normalized"],
                    "student_intern_hits": row["current_student_intern_hits"],
                    "summer_2027_hits": row["summer_2027_hits"],
                    "focus_role_hits": row["current_focus_role_hits"],
                }
                for row in validated
            },
        },
        "projection": {
            "production_sources": projected_sources,
            "source_delta": len(CANDIDATE_ENTRIES),
            "parent_mappings": BASELINE_MAPPINGS,
            "mapping_delta": 0,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
            "master_delta": 0,
        },
        "candidate_registry": str(CANDIDATE_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "validated_registry": str(VALIDATED_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "production_registry_sha256": hashlib.sha256(production_bytes).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
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
        "validated": report["validated_companies"],
        "excluded": report["excluded_companies"],
        "jobs": report["jobs"],
        "projection": report["projection"],
        "production_registry_activated": report["production_registry_activated"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
