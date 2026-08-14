"""Build and validate Infrastructure Expansion v1 Batch A without activation."""

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
from src.scraper.company_careers.adapters import ashby, greenhouse, oracle_recruiting
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_infrastructure_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.infrastructure_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.infrastructure_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "infrastructure_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 401
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "DigitalOcean", "Fastly", "Akamai", "Equinix", "CoreWeave", "Crusoe",
    "Lambda", "Cockroach Labs", "Grafana Labs", "Sentry", "Vanta", "Pulumi",
    "Temporal", "Tailscale", "Netlify", "Render", "Fly.io", "PlanetScale",
    "Neon", "Supabase",
]

STUDENT_RE = re.compile(
    r"(?i)\b(intern(?:ship)?|student|co[ -]?op|undergrad(?:uate)?|"
    r"new grad(?:uate)?|early career|graduate program)\b"
)
SUMMER_2027_RE = re.compile(r"(?i)\bsummer\s+2027\b")
FOCUS_RE = re.compile(
    r"(?i)\b(software|backend|back[ -]?end|distributed systems?|platform|cloud|"
    r"infrastructure|site reliability|SRE|network(?:ing)?|machine learning|ML|"
    r"AI|database|storage|compute|Kubernetes|DevOps|observability|reliability)\b"
)
IN_SCOPE_RE = re.compile(
    r"(?i)\b(canada|canadian|united states|usa|u\.s\.|remote|"
    r"alberta|british columbia|ontario|quebec|calgary|edmonton|vancouver|"
    r"toronto|ottawa|montreal|san francisco|new york|austin|seattle|boston|"
    r"palo alto|redwood city|sunnyvale|bellevue|dallas|denver|atlanta|chicago|"
    r"virginia|california|massachusetts|texas|washington)\b|"
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
    r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
    r"TX|UT|VT|VA|WA|WV|WI|WY|AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b"
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


def _greenhouse(
    name: str,
    slug: str,
    careers_url: str,
    apply_host: str,
    identity: str,
    **fields: Any,
) -> dict[str, Any]:
    return _common(
        name,
        "greenhouse",
        careers_url,
        slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=[apply_host],
        **fields,
    )


def _ashby(name: str, slug: str, careers_url: str, identity: str) -> dict[str, Any]:
    return _common(
        name,
        "ashby",
        careers_url,
        slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=["jobs.ashbyhq.com"],
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _greenhouse(
        "DigitalOcean", "digitalocean98", "https://www.digitalocean.com/careers",
        "www.digitalocean.com", r"DigitalOcean",
    ),
    _greenhouse(
        "Fastly", "fastly", "https://www.fastly.com/about/careers",
        "www.fastly.com", r"Fastly",
    ),
    _common(
        "Akamai", "oracle_recruiting", "https://jobs.akamai.com/en/sites/CX_1/jobs",
        endpoint_url=(
            "https://fa-extu-saasfaprod1.fa.ocs.oraclecloud.com/"
            "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        ),
        endpoint_variant="recruiting_ce_v1",
        site_number="CX_1",
        locale="en",
        source_identity_regex=r"Akamai Career Site|Akamai Technologies",
        allowed_apply_hosts=["jobs.akamai.com"],
        max_jobs=24,
    ),
    _greenhouse(
        "CoreWeave", "coreweave", "https://www.coreweave.com/careers",
        "coreweave.com", r"CoreWeave", max_response_bytes=5_000_000,
        department_allowlist=["Technology"],
        job_attribution_field="greenhouse_metadata.Acquisition Company",
        job_attribution_regex=r"CoreWeave",
    ),
    _ashby("Crusoe", "Crusoe", "https://www.crusoe.ai/about/careers", r"Crusoe"),
    _ashby("Lambda", "lambda", "https://lambda.ai/careers", r"Lambda"),
    _greenhouse(
        "Cockroach Labs", "cockroachlabs", "https://www.cockroachlabs.com/careers/jobs/",
        "www.cockroachlabs.com", r"Cockroach Labs",
    ),
    _greenhouse(
        "Grafana Labs", "grafanalabs", "https://grafana.com/careers/",
        "job-boards.greenhouse.io", r"Grafana Labs", max_response_bytes=5_000_000,
    ),
    _ashby("Sentry", "sentry", "https://sentry.io/careers/", r"Sentry"),
    _ashby("Vanta", "vanta", "https://www.vanta.com/company/careers", r"Vanta"),
    _greenhouse(
        "Pulumi", "pulumicorporation", "https://www.pulumi.com/careers/",
        "job-boards.greenhouse.io", r"Pulumi",
    ),
    _greenhouse(
        "Temporal", "temporaltechnologies", "https://temporal.io/careers",
        "job-boards.greenhouse.io", r"Temporal Technologies|Temporal",
    ),
    _greenhouse(
        "Tailscale", "tailscale", "https://tailscale.com/careers",
        "job-boards.greenhouse.io", r"Tailscale",
    ),
    _greenhouse(
        "Netlify", "netlify", "https://www.netlify.com/careers/",
        "job-boards.greenhouse.io", r"Netlify",
    ),
    _ashby("Render", "render", "https://render.com/careers", r"Render"),
    _greenhouse(
        "PlanetScale", "planetscale", "https://planetscale.com/careers",
        "job-boards.greenhouse.io", r"PlanetScale",
    ),
    _ashby("Supabase", "supabase", "https://supabase.com/careers", r"Supabase"),
]

EXCLUDED: list[dict[str, Any]] = [
    {
        "company": "Equinix",
        "platform": "custom/AWS WAF",
        "official_careers_url": "https://careers.equinix.com/",
        "jobs_observed": None,
        "validation_status": "blocked_anonymous_waf",
        "exclusion_reason": (
            "The official Equinix search route returns an AWS WAF challenge and HTTP 202 to "
            "bounded anonymous requests. No CAPTCHA, cookie, browser, or session bypass is attempted."
        ),
    },
    {
        "company": "Fly.io",
        "platform": "first-party static",
        "official_careers_url": "https://fly.io/jobs/",
        "jobs_observed": 0,
        "validation_status": "no_current_official_jobs",
        "exclusion_reason": (
            "The current official Fly.io jobs page explicitly says there are no open positions, "
            "so there is no non-empty stable job collection to validate."
        ),
    },
    {
        "company": "Neon",
        "platform": "Databricks parent routing",
        "official_careers_url": "https://neon.com/about-us",
        "jobs_observed": None,
        "validation_status": "acquired_parent_source_attribution_ambiguous",
        "exclusion_reason": (
            "Databricks acquired Neon in May 2025 and Neon's official site now routes open roles "
            "to Databricks. No reviewed Databricks per-job field exactly attributes Neon roles, "
            "so no child source or parent mapping is inferred."
        ),
    },
]

ROUTING_REVIEW = {
    "Akamai": (
        "Akamai's official corporate page routes Search jobs to jobs.akamai.com; that branded "
        "Oracle Candidate Experience site uses the exact anonymous CX_1 requisition contract."
    ),
    "CoreWeave": (
        "CoreWeave's board also contains acquired Weights & Biases, Marimo, and Monolith rows. "
        "Only Technology rows whose Acquisition Company metadata is exactly CoreWeave are accepted."
    ),
    "Lambda": (
        "The former Lambda Labs company now presents itself as Lambda; the official Lambda page "
        "links the exact case-insensitive lambda Ashby board."
    ),
    "Temporal": (
        "The current first-party Temporal page links temporaltechnologies Greenhouse jobs. A live "
        "Temporal Ashby mirror exists, but it is not selected over the first-party-linked contract."
    ),
    "Equinix": "Official search remains blocked by an anonymous AWS WAF challenge and fails closed.",
    "Fly.io": "Official jobs page is current but explicitly has no open positions.",
    "Neon": "Acquired by Databricks; official hiring routes to the parent without exact Neon metadata.",
}

IDENTITY_EVIDENCE = {
    "DigitalOcean": "Official DigitalOcean page embeds board slug digitalocean98 and DigitalOcean-owned apply URLs.",
    "Fastly": "Official Fastly page and exact fastly Greenhouse board return Fastly-owned apply URLs.",
    "Akamai": "Official Akamai Search jobs link resolves to branded jobs.akamai.com CX_1 Oracle recruiting.",
    "Equinix": "Official Equinix career site and search route; anonymous search response is WAF-blocked.",
    "CoreWeave": "Official CoreWeave page and board with exact per-job Acquisition Company metadata.",
    "Crusoe": "Official Crusoe page embeds the exact Crusoe Ashby board and API route.",
    "Lambda": "Official Lambda page links exact lambda Ashby UUID job URLs.",
    "Cockroach Labs": "Official Cockroach Labs page embeds the cockroachlabs Greenhouse board.",
    "Grafana Labs": "Official Grafana Labs page links the exact grafanalabs Greenhouse board.",
    "Sentry": "Official Sentry page embeds exact sentry Ashby UUID job URLs.",
    "Vanta": "Official Vanta page fetches the exact vanta Ashby job-board endpoint.",
    "Pulumi": "Official Pulumi page uses the current pulumicorporation Greenhouse board.",
    "Temporal": "Official Temporal page links current temporaltechnologies Greenhouse IDs.",
    "Tailscale": "Official Tailscale page fetches the exact tailscale Greenhouse board.",
    "Netlify": "Official Netlify page links current netlify Greenhouse IDs.",
    "Render": "Official Render page links exact render Ashby UUID job URLs.",
    "Fly.io": "Official Fly.io jobs page explicitly reports no current positions.",
    "PlanetScale": "Official PlanetScale page links current planetscale Greenhouse IDs.",
    "Neon": "Official Neon site states it is a Databricks company and routes hiring to Databricks.",
    "Supabase": "Official Supabase page links exact supabase Ashby UUID job URLs.",
}

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "ashby": ashby.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "oracle_recruiting": oracle_recruiting.fetch_jobs,
}

SAFE_TO_STAGE_FILES = [
    "config/company_infrastructure_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.infrastructure_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.infrastructure_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_infrastructure_expansion_v1_batch_a.py",
    "src/scraper/company_careers/adapters/greenhouse.py",
    "tests/test_greenhouse_byte_limit_regression.py",
    "tests/test_infrastructure_expansion_v1_batch_a.py",
]


def _in_scope(job: JobRecord) -> bool:
    return bool(IN_SCOPE_RE.search(" | ".join([job.location, *job.offices])))


def _allowed_url(url: str, allowed_hosts: list[str]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and any(
        host == value.casefold() or host.endswith("." + value.casefold())
        for value in allowed_hosts
    )


def _signature(jobs: list[JobRecord]) -> str:
    payload = [[job.job_id, job.title, job.location, job.url] for job in jobs]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest().upper()


def _pagination(session: requests.Session, platform: str) -> dict[str, Any]:
    value = getattr(session, f"{platform}_pagination", {})
    return dict(value) if isinstance(value, dict) else {}


def _validate_twice(entry: dict[str, Any]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    scoped_sets: list[list[JobRecord]] = []
    for run_number in range(1, REPEAT_RUNS + 1):
        session = requests.Session()
        started = time.monotonic()
        try:
            observed = ADAPTERS[str(entry["platform"])](
                session, CompanyTarget.from_dict(entry), MAX_TIMEOUT_SECONDS
            )
            if not observed:
                raise AdapterError("source returned no current jobs")
            if len(observed) > int(entry["max_jobs"]):
                raise AdapterError("adapter exceeded the configured job bound")
            if len({job.job_id.casefold() for job in observed}) != len(observed):
                raise AdapterError("adapter returned duplicate stable IDs")
            if any(job.company != entry["name"] for job in observed):
                raise AdapterError("adapter returned ambiguous company attribution")
            if any(not _allowed_url(job.url, entry["allowed_apply_hosts"]) for job in observed):
                raise AdapterError("adapter returned a non-allow-listed apply URL")
            scoped = [job for job in observed if _in_scope(job)]
            if not scoped:
                raise AdapterError("source returned no Canada/US/Remote jobs in the bounded result")
            run = {
                "run": run_number,
                "success": True,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": len(observed),
                "jobs_normalized": len(scoped),
                "out_of_scope_filtered": len(observed) - len(scoped),
                "student_hits": sum(bool(STUDENT_RE.search(job.title)) for job in scoped),
                "summer_2027_hits": sum(bool(SUMMER_2027_RE.search(job.title)) for job in scoped),
                "focus_hits": sum(bool(FOCUS_RE.search(job.title)) for job in scoped),
                "signature": _signature(scoped),
                "stable_ids": [job.job_id for job in scoped],
                "titles": [job.title for job in scoped],
                "locations": [job.location for job in scoped],
                "detail_apply_urls": [job.url for job in scoped],
                "normalized_job_evidence": [
                    {"job_id": job.job_id, "title": job.title, "location": job.location, "url": job.url}
                    for job in scoped
                ],
                "pagination": _pagination(session, str(entry["platform"])),
            }
        except (AdapterError, requests.RequestException, TypeError, ValueError) as exc:
            scoped = []
            run = {
                "run": run_number,
                "success": False,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": 0,
                "jobs_normalized": 0,
                "signature": "",
                "error": str(exc),
            }
        runs.append(run)
        scoped_sets.append(scoped)
    stable = (
        all(run["success"] for run in runs)
        and runs[0]["signature"] == runs[1]["signature"]
        and runs[0]["jobs_normalized"] == runs[1]["jobs_normalized"]
    )
    jobs = scoped_sets[0] if stable else []
    return {
        "company": entry["name"],
        "canonical_company": entry["name"],
        "platform": entry["platform"],
        "adapter": entry["platform"],
        "result": "validated" if stable else "excluded",
        "official_careers_url": entry["careers_url"],
        "identity_attribution_evidence": IDENTITY_EVIDENCE[entry["name"]],
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
        "location_result": "valid_in_scope" if stable else "invalid",
        "apply_url_result": "valid" if stable else "invalid",
        "company_attribution_result": "exact_reviewed_source" if stable else "invalid",
        "deterministic_result": "stable_ordered_signature" if stable else "invalid",
        "production_ready": stable,
        "original_246_master": False,
        "master_rank": None,
        "runs": runs,
    }


def _excluded_row(row: dict[str, Any]) -> dict[str, Any]:
    attribution = (
        "ambiguous_parent_collection"
        if row["company"] == "Neon"
        else "not_validated"
    )
    return {
        **row,
        "canonical_company": row["company"],
        "adapter": "none",
        "result": "excluded",
        "identity_attribution_evidence": IDENTITY_EVIDENCE[row["company"]],
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
        "company_attribution_result": attribution,
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
        "original_246_master", "master_rank", "identity_attribution_evidence",
        "exclusion_reason",
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
    mapping_bytes = PARENT_MAPPINGS.read_bytes()
    production_payload = yaml.safe_load(production_bytes) or {}
    production_entries = list(production_payload.get("target_companies") or [])
    if len(production_entries) != BASELINE_SOURCES:
        raise RuntimeError(
            f"Expected {BASELINE_SOURCES} production sources, found {len(production_entries)}"
        )
    if _mapping_count() != BASELINE_MAPPINGS:
        raise RuntimeError(
            f"Expected {BASELINE_MAPPINGS} parent mappings, found {_mapping_count()}"
        )
    existing = {str(item.get("name") or "").strip().casefold() for item in production_entries}
    duplicates = [item["name"] for item in CANDIDATE_ENTRIES if item["name"].casefold() in existing]
    if duplicates:
        raise RuntimeError(f"Candidate companies already exist in production: {duplicates}")
    config_errors = {
        item["name"]: registry_validator.configuration_error(item)
        for item in CANDIDATE_ENTRIES
    }
    if any(config_errors.values()):
        raise RuntimeError(f"Candidate configuration errors: {config_errors}")

    validated = [_validate_twice(item) for item in CANDIDATE_ENTRIES]
    if not all(row["production_ready"] for row in validated):
        failures = {
            row["company"]: [run.get("error") for run in row["runs"] if not run["success"]]
            for row in validated if not row["production_ready"]
        }
        raise RuntimeError(f"At least one candidate failed closed: {failures}")
    by_name = {row["company"]: row for row in [*validated, *map(_excluded_row, EXCLUDED)]}
    if set(by_name) != set(REVIEWED_COMPANIES):
        raise RuntimeError("Reviewed company inventory drifted")
    results = [by_name[name] for name in REVIEWED_COMPANIES]

    candidate_bytes = registry_validator.build_candidate_registry_bytes(
        PRODUCTION, CANDIDATE_ENTRIES
    )
    CANDIDATE_REGISTRY.write_bytes(candidate_bytes)
    VALIDATED_REGISTRY.write_bytes(candidate_bytes)
    _write_csv(results)
    report = {
        "batch": "Infrastructure Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "focus": [
                "software", "backend", "distributed systems", "cloud", "platform",
                "SRE", "networking", "ML infrastructure",
            ],
            "maximum_pages_per_source": 1,
            "maximum_normalized_jobs_per_greenhouse_or_ashby_source": 20,
            "maximum_oracle_page_size": 24,
            "repeat_validation_runs": REPEAT_RUNS,
            "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": sorted({item["platform"] for item in CANDIDATE_ENTRIES}),
        "generic_adapter_improvements": [
            "Greenhouse can now filter a shared board by an exact reviewed per-job metadata field and regex, with malformed or incomplete attribution contracts rejected.",
        ],
        "validated_companies": [row["company"] for row in validated],
        "excluded_companies": [row["company"] for row in results if not row["production_ready"]],
        "routing_review": ROUTING_REVIEW,
        "results": results,
        "blockers": {
            row["company"]: row["exclusion_reason"]
            for row in results if not row["production_ready"]
        },
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
            "production_sources": BASELINE_SOURCES + len(CANDIDATE_ENTRIES),
            "source_delta": len(CANDIDATE_ENTRIES),
            "parent_mappings": BASELINE_MAPPINGS,
            "mapping_delta": 0,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
            "master_delta": 0,
        },
        "candidate_registry": str(CANDIDATE_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "validated_registry": str(VALIDATED_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "production_registry_sha256": hashlib.sha256(production_bytes).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mapping_bytes).hexdigest().upper(),
        "safe_to_stage_files": SAFE_TO_STAGE_FILES,
        "production_registry_activated": False,
        "publication": {
            "production_run": False,
            "staged": False,
            "committed": False,
            "pushed": False,
            "dashboard_scan": False,
            "dashboard_publication": False,
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if PRODUCTION.read_bytes() != production_bytes or PARENT_MAPPINGS.read_bytes() != mapping_bytes:
        raise RuntimeError("Production registry or parent mappings changed during validation")
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
