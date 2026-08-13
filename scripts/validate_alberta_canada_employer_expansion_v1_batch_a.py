"""Build and validate Alberta/Canada Employer Expansion v1 Batch A.

This validator performs bounded anonymous reads and writes only candidate,
validated, inventory, and report artifacts.  It never activates production,
changes parent mappings, publishes jobs, or writes job persistence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import (
    ashby,
    bamboohr,
    greenhouse,
    phenom,
    sap_successfactors,
    static_html,
    workday,
)
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.student_roles import (
    classify_student_location,
    student_job_signals,
)


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_alberta_canada_employer_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.alberta_canada_employer_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.alberta_canada_employer_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "alberta_canada_employer_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 344
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
MAX_JOBS = 20
MAX_RAW_JOBS = 250
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "Air Canada",
    "Rogers Communications",
    "Constellation Software",
    "CGI",
    "Lightspeed Commerce",
    "Nuvei",
    "Cineplex",
    "Loblaw",
    "George Weston",
    "Shaw Communications",
    "Canadian Western Bank",
    "Solium Capital",
    "Trulioo",
    "Helm Operations",
    "Solera Holdings",
    "Sensor Cortek",
    "Showbie",
    "Coconut Software",
    "Certn",
    "Latitude Technologies",
]


def _entry(name: str, platform: str, careers_url: str, *, identity_url: str,
           identity_regex: str, allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
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
        "Air Canada", "phenom", "https://careers.aircanada.com/ca/en",
        identity_url="https://careers.aircanada.com/ca/en/search-results",
        identity_regex=r"Air Canada|AIPACICA",
        allowed_apply_hosts=["careers.aircanada.com"],
        search_url="https://careers.aircanada.com/ca/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="AIPACICA", locale="en_ca",
        apply_url_template="https://careers.aircanada.com/ca/en/job/{job_seq_no}",
    ),
    _entry(
        "Rogers Communications", "sap_successfactors", "https://jobs.rogers.com/",
        identity_url="https://jobs.rogers.com/search/", identity_regex=r"Rogers",
        allowed_apply_hosts=["jobs.rogers.com"], search_url="https://jobs.rogers.com/search/",
        endpoint_variant="rmk_table",
    ),
    _entry(
        "Lightspeed Commerce", "static_html", "https://www.lightspeedhq.com/careers/",
        identity_url="https://www.lightspeedhq.com/careers/openings/",
        identity_regex=r"Lightspeed", allowed_apply_hosts=["www.lightspeedhq.com"],
        listing_url="https://www.lightspeedhq.com/careers/openings/",
        endpoint_variant="server_rendered_cards", listing_selector="ul.filter__results__list",
        job_card_selector=(
            "a[href*=office=Montreal],a[href*=office=Toronto],a[href*=office=Ottawa],"
            "a[href*=office=Quebec],a[href$=Canada],a[href$=US]"
        ),
        title_selector="span.job-title", location_selector="span.job-office",
        department_selector="span.job-dept", detail_link_selector=":self",
        external_id_regex=r"/([0-9a-f-]{36})/",
    ),
    _entry(
        "Cineplex", "workday", "https://corp.cineplex.com/careers",
        identity_url="https://corp.cineplex.com/careers", identity_regex=r"Cineplex",
        allowed_apply_hosts=["cineplex.wd3.myworkdayjobs.com"],
        search_url="https://cineplex.wd3.myworkdayjobs.com/Cineplex",
        tenant="cineplex", wd_subdomain="wd3", site_id="Cineplex",
    ),
    _entry(
        "Trulioo", "ashby", "https://www.trulioo.com/company/careers",
        identity_url="https://www.trulioo.com/apply", identity_regex=r"Trulioo|jobs\.ashbyhq\.com/trulioo",
        allowed_apply_hosts=["jobs.ashbyhq.com"], slug="trulioo",
    ),
    _entry(
        "Helm Operations", "workday", "https://www.helmoperations.com/careers",
        identity_url="https://www.helmoperations.com/careers",
        identity_regex=r"Helm|HelmOperations|volarisgroup",
        allowed_apply_hosts=["volarisgroup.wd3.myworkdayjobs.com"],
        search_url="https://volarisgroup.wd3.myworkdayjobs.com/HelmOperations",
        tenant="volarisgroup", wd_subdomain="wd3", site_id="HelmOperations",
        applied_facets={"locations": ["fc8da1b0f51d018732ec146710020601"]},
    ),
    _entry(
        "Solera Holdings", "workday", "https://www.solera.com/careers/",
        identity_url="https://www.solera.com/careers/", identity_regex=r"Solera",
        allowed_apply_hosts=["solera.wd5.myworkdayjobs.com"],
        search_url="https://solera.wd5.myworkdayjobs.com/Global_Career_Site",
        tenant="solera", wd_subdomain="wd5", site_id="Global_Career_Site",
        applied_facets={"locationCountry": ["a30a87ed25634629aa6c3958aa2b91ea"]},
    ),
    _entry(
        "Showbie", "bamboohr", "https://showbie.bamboohr.com/careers",
        identity_url="https://www.showbie.com/about", identity_regex=r"Showbie|showbie\.bamboohr\.com",
        allowed_apply_hosts=["showbie.bamboohr.com"], slug="showbie",
        listing_url="https://showbie.bamboohr.com/careers/list",
    ),
    _entry(
        "Coconut Software", "greenhouse", "https://www.coconutsoftware.com/careers/",
        identity_url="https://www.coconutsoftware.com/careers/",
        identity_regex=r"Coconut Software|boards-api\.greenhouse\.io/v1/boards/coconutsoftware",
        allowed_apply_hosts=["job-boards.greenhouse.io", "boards.greenhouse.io"],
        slug="coconutsoftware",
    ),
]


EXCLUDED: list[dict[str, Any]] = [
    {
        "company": "Constellation Software", "platform": "autonomous subsidiary recruiting",
        "official_careers_url": "https://www.csisoftware.com/operating-groups/",
        "validation_status": "no_exact_central_source",
        "exclusion_reason": "Constellation's operating businesses recruit autonomously; child boards are not relabelled as parent jobs.",
    },
    {
        "company": "CGI", "platform": "Njoyn XWeb / Radware",
        "official_careers_url": "https://cgi.njoyn.com/CORP/xweb/xweb.asp?page=JobListing&CLID=21001&lang=1",
        "validation_status": "captcha_blocked",
        "exclusion_reason": "The exact CGI Njoyn board still returns a Radware hCaptcha block page to anonymous requests; no challenge bypass is attempted.",
    },
    {
        "company": "Nuvei", "platform": "Workable",
        "official_careers_url": "https://www.nuvei.com/careers",
        "validation_status": "captcha_blocked",
        "exclusion_reason": "Nuvei retained its brand after the 2024 Advent transaction, but its official Workable apply shell enables Turnstile; the guarded adapter remains unregistered.",
    },
    {
        "company": "Loblaw", "platform": "Paradox mixed-brand parent board",
        "official_careers_url": "https://careers.loblaw.ca/jobs",
        "validation_status": "ambiguous_parent_child_attribution",
        "exclusion_reason": "The parent board mixes Loblaw, retail banners, franchises, and business units; card-level brand fields do not establish one exact canonical employer contract.",
    },
    {
        "company": "George Weston", "platform": "holding-company careers routing",
        "official_careers_url": "https://www.weston.ca/en/",
        "validation_status": "no_exact_central_source",
        "exclusion_reason": "George Weston is a holding company for Loblaw and Choice Properties and exposes no anonymous central job collection with exact parent attribution.",
    },
    {
        "company": "Shaw Communications", "platform": "acquired and amalgamated into Rogers",
        "official_careers_url": "https://about.rogers.com/investor-relations/shaw/",
        "validation_status": "stale_acquired_company",
        "exclusion_reason": "Rogers completed the Shaw merger in 2023 and states Shaw was amalgamated with Rogers; no stale Shaw source is added.",
    },
    {
        "company": "Canadian Western Bank", "platform": "acquired and rebranded by National Bank",
        "official_careers_url": "https://www.cwbank.com/en/careers",
        "validation_status": "stale_acquired_company",
        "exclusion_reason": "The former CWB careers route now redirects to National Bank after the 2025 acquisition; National Bank roles are not relabelled as CWB.",
    },
    {
        "company": "Solium Capital", "platform": "acquired and rebranded by Morgan Stanley",
        "official_careers_url": "https://www.morganstanley.com/press-releases/morgan-stanley-completes-acquisition-of-solium",
        "validation_status": "stale_acquired_company",
        "exclusion_reason": "Morgan Stanley acquired Solium in 2019 and rebranded it Shareworks by Morgan Stanley; parent jobs are not attributed to the stale Solium name.",
    },
    {
        "company": "Sensor Cortek", "platform": "official corporate site only",
        "official_careers_url": "https://sensorcortek.ai/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "The official Ottawa AI company site exposes company and contact information but no anonymous careers collection or stable job URLs.",
    },
    {
        "company": "Certn", "platform": "official future-opportunities page",
        "official_careers_url": "https://certn.co/careers/",
        "validation_status": "no_observable_job_contract",
        "exclusion_reason": "Certn's official careers page currently contains only a future-opportunities message and no anonymous job collection to prove as zero.",
    },
    {
        "company": "Latitude Technologies", "platform": "SKYTRAC parent careers routing",
        "official_careers_url": "https://www.latitudetech.com/about-us/careers/",
        "validation_status": "acquired_parent_source_required",
        "exclusion_reason": "Latitude says SKYTRAC acquired it in 2018 and routes openings to SKYTRAC; no exact child-brand field or approved parent mapping is available.",
    },
]


OWNERSHIP_REVIEWS = [
    {"company": "Shaw Communications", "relationship": "amalgamated into Rogers Communications", "disposition": "excluded_stale"},
    {"company": "Canadian Western Bank", "relationship": "acquired and rebranded by National Bank", "disposition": "excluded_stale"},
    {"company": "Solium Capital", "relationship": "acquired and rebranded Shareworks by Morgan Stanley", "disposition": "excluded_stale"},
    {"company": "Latitude Technologies", "relationship": "acquired brand of SKYTRAC", "disposition": "excluded_parent_mapping_required"},
    {"company": "Nuvei", "relationship": "acquired by Advent-led purchaser; Nuvei brand retained", "disposition": "excluded_captcha"},
    {"company": "Helm Operations", "relationship": "Volaris-owned, Constellation operating business; brand and independence retained", "disposition": "validated_exact_child_board"},
    {"company": "Constellation Software", "relationship": "parent of autonomous operating businesses including Volaris", "disposition": "excluded_no_central_source"},
    {"company": "George Weston", "relationship": "holding company of Loblaw and Choice Properties", "disposition": "excluded_no_central_source"},
]

SAFE_TO_STAGE_FILES = [
    "config/company_alberta_canada_employer_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.alberta_canada_employer_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.alberta_canada_employer_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_alberta_canada_employer_expansion_v1_batch_a.py",
    "src/scraper/company_careers/adapters/workday.py",
    "tests/test_alberta_canada_employer_expansion_v1_batch_a.py",
]

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "ashby": ashby.fetch_jobs,
    "bamboohr": bamboohr.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "phenom": phenom.fetch_jobs,
    "sap_successfactors": sap_successfactors.fetch_jobs,
    "static_html": static_html.fetch_jobs,
    "workday": workday.fetch_jobs,
}


def _identity_check(session: requests.Session, entry: Mapping[str, Any]) -> str:
    url = str(entry["identity_url"])
    response = session.get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
        timeout=MAX_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise AdapterError(f"identity endpoint HTTP {response.status_code}")
    content = response.content
    if not content or len(content) > int(entry["max_response_bytes"]):
        raise AdapterError("identity endpoint was empty or exceeded the byte limit")
    if not re.search(str(entry["source_identity_regex"]), response.text, re.IGNORECASE):
        raise AdapterError("official source identity was not proven")
    return response.url


def _facet_proves_scope(entry: Mapping[str, Any]) -> bool:
    facets = entry.get("applied_facets") or {}
    return facets in (
        {"locationCountry": ["a30a87ed25634629aa6c3958aa2b91ea"]},
        {"locations": ["fc8da1b0f51d018732ec146710020601"]},
    )


def _in_scope(job: JobRecord, entry: Mapping[str, Any]) -> bool:
    if _facet_proves_scope(entry):
        return True
    return not classify_student_location(job).startswith("excluded_")


def _student_hits(jobs: Iterable[JobRecord]) -> list[dict[str, str]]:
    return [
        {"title": job.title, "location": job.location, "url": job.url}
        for job in jobs
        if student_job_signals(job)["matching_rule"]
    ]


def _verify_jobs(jobs: list[JobRecord], entry: Mapping[str, Any]) -> None:
    ids = [str(job.job_id).strip() for job in jobs]
    urls = [str(job.url).strip() for job in jobs]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise AdapterError("candidate has missing or duplicate stable IDs")
    if any(not job.title.strip() or not job.location.strip() for job in jobs):
        raise AdapterError("candidate has a missing title or location")
    if any(job.company != entry["name"] for job in jobs):
        raise AdapterError("candidate has ambiguous company attribution")
    if any(not _in_scope(job, entry) for job in jobs):
        raise AdapterError("candidate has a job outside Canada/US/Remote scope")
    allowed = [str(value).casefold() for value in entry["allowed_apply_hosts"]]
    for url in urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not any(host == value or host.endswith("." + value) for value in allowed):
            raise AdapterError("candidate has an unofficial apply URL")


def _signature(jobs: list[JobRecord]) -> str:
    rows = [(job.job_id, job.title, job.company, job.location, job.url) for job in jobs]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _pagination(session: requests.Session, platform: str) -> dict[str, Any]:
    return dict(getattr(session, f"{platform}_pagination", {}) or {})


def _validate_twice(entry: dict[str, Any]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    scoped_runs: list[list[JobRecord]] = []
    for run_number in range(1, REPEAT_RUNS + 1):
        session = requests.Session()
        started = time.monotonic()
        try:
            identity_url = _identity_check(session, entry)
            raw_jobs = ADAPTERS[str(entry["platform"])](
                session, CompanyTarget.from_dict(entry), MAX_TIMEOUT_SECONDS
            )
            if len(raw_jobs) > MAX_RAW_JOBS:
                raise AdapterError("adapter exceeded the raw collection safety bound")
            scoped = [job for job in raw_jobs if _in_scope(job, entry)][: int(entry["max_jobs"])]
            _verify_jobs(scoped, entry)
            hits = _student_hits(scoped)
            run = {
                "run": run_number, "success": True,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "identity_url": identity_url, "jobs_observed": len(raw_jobs),
                "jobs_normalized": len(scoped), "jobs_outside_scope": len(raw_jobs) - len([job for job in raw_jobs if _in_scope(job, entry)]),
                "student_hits": len(hits), "student_role_evidence": hits,
                "signature": _signature(scoped), "stable_ids": [job.job_id for job in scoped],
                "titles": [job.title for job in scoped], "locations": [job.location for job in scoped],
                "detail_apply_urls": [job.url for job in scoped],
                "pagination": _pagination(session, str(entry["platform"])),
            }
        except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
            scoped = []
            run = {
                "run": run_number, "success": False,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": 0, "jobs_normalized": 0, "student_hits": 0,
                "signature": "", "error": str(exc),
            }
        finally:
            session.close()
        runs.append(run)
        scoped_runs.append(scoped)
    stable = (
        all(run["success"] for run in runs)
        and runs[0]["signature"] == runs[1]["signature"]
        and runs[0]["jobs_normalized"] == runs[1]["jobs_normalized"]
    )
    jobs = scoped_runs[0] if stable else []
    hits = _student_hits(jobs)
    return {
        "company": entry["name"], "platform": entry["platform"], "adapter": entry["platform"],
        "result": "validated" if stable else "excluded",
        "official_careers_url": entry["careers_url"],
        "validation_status": "repeatable_anonymous" if stable else "unstable_or_failed",
        "jobs_observed": runs[0].get("jobs_observed", 0) if stable else 0,
        "jobs_normalized": len(jobs), "current_student_intern_hits": len(hits),
        "current_student_role_evidence": hits,
        "stable_id_result": "valid" if stable else "invalid",
        "title_result": "valid" if stable else "invalid",
        "location_result": "valid_in_scope" if stable else "invalid",
        "apply_url_result": "valid" if stable else "invalid",
        "company_attribution_result": "exact" if stable else "invalid",
        "deterministic_result": "stable_ordered_signature" if stable else "invalid",
        "production_ready": stable, "original_246_master": False, "master_rank": None,
        "runs": runs,
    }


def _excluded_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row, "adapter": "none", "result": "excluded", "jobs_observed": 0,
        "jobs_normalized": 0, "current_student_intern_hits": 0,
        "stable_id_result": "not_validated", "title_result": "not_validated",
        "location_result": "not_validated", "apply_url_result": "not_validated",
        "company_attribution_result": "not_validated", "deterministic_result": "fail_closed",
        "production_ready": False, "original_246_master": False, "master_rank": None,
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
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)


def _mapping_count() -> int:
    return len(load_parent_source_mappings(PARENT_MAPPINGS))


def build() -> dict[str, Any]:
    production_bytes = PRODUCTION.read_bytes()
    mapping_bytes = PARENT_MAPPINGS.read_bytes()
    production = (yaml.safe_load(production_bytes) or {})["target_companies"]
    if len(production) != BASELINE_SOURCES:
        raise RuntimeError(f"Expected {BASELINE_SOURCES} production sources, found {len(production)}")
    if _mapping_count() != BASELINE_MAPPINGS:
        raise RuntimeError(f"Expected {BASELINE_MAPPINGS} parent mappings, found {_mapping_count()}")
    existing = {str(entry.get("name") or "").casefold() for entry in production}
    duplicates = [entry["name"] for entry in CANDIDATE_ENTRIES if entry["name"].casefold() in existing]
    if duplicates:
        raise RuntimeError(f"Candidate companies already exist in production: {duplicates}")
    errors = {entry["name"]: registry_validator.configuration_error(entry) for entry in CANDIDATE_ENTRIES}
    if any(errors.values()):
        raise RuntimeError(f"Candidate configuration errors: {errors}")

    validated = [_validate_twice(entry) for entry in CANDIDATE_ENTRIES]
    failures = {row["company"]: row["validation_status"] for row in validated if not row["production_ready"]}
    if failures:
        raise RuntimeError(f"At least one candidate failed closed: {failures}")
    by_name = {row["company"]: row for row in [*validated, *map(_excluded_row, EXCLUDED)]}
    if set(by_name) != set(REVIEWED_COMPANIES):
        raise RuntimeError("Reviewed company inventory drifted")
    results = [by_name[name] for name in REVIEWED_COMPANIES]

    candidate_bytes = registry_validator.build_candidate_registry_bytes(PRODUCTION, CANDIDATE_ENTRIES)
    CANDIDATE_REGISTRY.write_bytes(candidate_bytes)
    VALIDATED_REGISTRY.write_bytes(candidate_bytes)
    _write_csv(results)
    projected = BASELINE_SOURCES + len(CANDIDATE_ENTRIES)
    report = {
        "batch": "Alberta/Canada Employer Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES, "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "maximum_pages_per_source": 1, "maximum_normalized_jobs_per_source": MAX_JOBS,
            "maximum_raw_jobs_per_source": MAX_RAW_JOBS,
            "repeat_validation_runs": REPEAT_RUNS, "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": sorted({entry["platform"] for entry in CANDIDATE_ENTRIES}),
        "generic_adapter_improvements": [
            "Workday exact-facet verification now supports structured nested facet groups while preserving count equality."
        ],
        "validated_companies": [row["company"] for row in validated],
        "excluded_companies": [row["company"] for row in results if not row["production_ready"]],
        "ownership_reviews": OWNERSHIP_REVIEWS,
        "stale_acquired_companies": [
            row["company"] for row in results
            if row["validation_status"] in {"stale_acquired_company", "acquired_parent_source_required"}
        ],
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
            "production_sources": projected, "source_delta": len(CANDIDATE_ENTRIES),
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
    if PRODUCTION.read_bytes() != production_bytes or PARENT_MAPPINGS.read_bytes() != mapping_bytes:
        raise RuntimeError("Production registry or parent mappings changed during validation")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        report = build()
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Alberta/Canada Employer Expansion v1 Batch A failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "validated": report["validated_companies"], "excluded": report["excluded_companies"],
        "jobs": report["jobs"], "projection": report["projection"],
        "production_registry_activated": report["production_registry_activated"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
