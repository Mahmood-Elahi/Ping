"""Build and validate Software Expansion v1 Batch A without activation."""

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
from src.scraper.company_careers.adapters import (
    ashby,
    embedded_data,
    greenhouse,
    phenom,
    smartrecruiters,
    static_html,
)
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_software_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.software_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.software_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "software_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 382
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "HubSpot", "Dropbox", "Zoom", "Okta", "Box", "Asana", "Notion", "Canva",
    "Grammarly", "Vercel", "Replit", "Airtable", "AppLovin", "Appian",
    "CarGurus", "Samsara", "PagerDuty", "Miro", "Fivetran", "Braze",
]

STUDENT_RE = re.compile(
    r"(?i)\b(intern(?:ship)?|student|co[ -]?op|undergrad(?:uate)?|"
    r"new grad(?:uate)?|early career|graduate program)\b"
)
SUMMER_2027_RE = re.compile(r"(?i)\bsummer\s+2027\b")
FOCUS_RE = re.compile(
    r"(?i)\b(software|backend|back[ -]?end|full[ -]?stack|platform|cloud|"
    r"machine learning|ML|embedded|firmware|infrastructure|site reliability|SRE|"
    r"data engineer|AI engineer|artificial intelligence|developer experience|DevOps)\b"
)
IN_SCOPE_RE = re.compile(
    r"(?i)\b(canada|canadian|united states|usa|u\.s\.|remote|"
    r"alberta|british columbia|ontario|quebec|calgary|edmonton|vancouver|"
    r"toronto|ottawa|montreal|san francisco|new york|austin|seattle|boston|"
    r"palo alto|redwood city|mclean|virginia|california|massachusetts|texas|"
    r"washington|chicago|foster city|denver|atlanta|raleigh|salt lake city)\b|"
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
) -> dict[str, Any]:
    return _common(
        name,
        "greenhouse",
        careers_url,
        slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=[apply_host],
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _greenhouse("HubSpot", "hubspotjobs", "https://www.hubspot.com/careers/jobs/all", "www.hubspot.com", r"HubSpot"),
    _greenhouse("Dropbox", "dropbox", "https://www.dropbox.jobs/en/jobs/", "jobs.dropbox.com", r"Dropbox"),
    _common(
        "Zoom", "static_html", "https://careers.zoom.us/",
        listing_url="https://careers.zoom.us/jobs/search",
        endpoint_variant="server_rendered_cards",
        listing_selector="div[id^=jobs_search_results_]",
        job_card_selector="article.job-search-results-card-col",
        title_selector="h3.job-search-results-card-title a",
        location_selector="li.job-component-location span",
        department_selector="li.job-component-category span",
        detail_link_selector="h3.job-search-results-card-title a",
        external_id_selector="li.job-component-requisition-identifier span",
        job_attribution_selector="li.job-component-dropdown-field-1 span",
        job_attribution_regex=r"Zoom",
        source_identity_regex=r"Zoom",
        allowed_apply_hosts=["careers.zoom.us"],
    ),
    _greenhouse("Okta", "okta", "https://www.okta.com/company/careers/job-listing/", "www.okta.com", r"Okta|Auth0"),
    _common(
        "Box", "static_html", "https://careers.box.com/en/",
        listing_url="https://careers.box.com/en/jobs/",
        endpoint_variant="server_rendered_cards",
        listing_selector="div#js-job-search-results",
        job_card_selector="div.card-job",
        title_selector="h2.card-title a",
        location_selector="ul.job-meta li:first-child",
        department_selector="ul.job-meta li:last-child",
        detail_link_selector="h2.card-title a",
        external_id_attribute="data-id",
        source_identity_regex=r"Box",
        allowed_apply_hosts=["careers.box.com"],
    ),
    _greenhouse("Asana", "asana", "https://asana.com/jobs/all", "www.asana.com", r"Asana"),
    _common(
        "Notion", "ashby", "https://www.notion.com/careers",
        slug="notion", source_identity_regex=r"Notion",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
    ),
    _common(
        "Canva", "smartrecruiters", "https://www.lifeatcanva.com/en/jobs/",
        slug="Canva",
        endpoint_url="https://api.smartrecruiters.com/v1/companies/Canva/postings",
        endpoint_variant=smartrecruiters.ENDPOINT_VARIANT,
        identity_regex=r"Canva", source_identity_regex=r"Canva",
        allowed_apply_hosts=["jobs.smartrecruiters.com"], page_size=20,
    ),
    _greenhouse("Vercel", "vercel", "https://vercel.com/careers", "job-boards.greenhouse.io", r"Vercel"),
    _common(
        "Replit", "ashby", "https://replit.com/careers",
        slug="replit", source_identity_regex=r"Replit",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
    ),
    _greenhouse("Airtable", "airtable", "https://airtable.com/careers", "job-boards.greenhouse.io", r"Airtable"),
    _greenhouse("AppLovin", "applovin", "https://www.applovin.com/en/careers", "boards.greenhouse.io", r"AppLovin"),
    _greenhouse("Appian", "appian", "https://careers.appian.com/jobs", "job-boards.greenhouse.io", r"Appian"),
    _common(
        "CarGurus", "phenom", "https://careers.cargurus.com/us/en",
        search_url="https://careers.cargurus.com/us/en/search-results",
        endpoint_variant="embedded_refine_search", ref_num="CABCARUS", locale="en_us",
        identity_regex=r"CarGurus", source_identity_regex=r"CarGurus|CABCARUS",
        allowed_apply_hosts=["careers.cargurus.com"], page_size=10, max_pages=2,
    ),
    _greenhouse("Samsara", "samsara", "https://www.samsara.com/company/careers", "www.samsara.com", r"Samsara"),
    _greenhouse("PagerDuty", "pagerduty", "https://careers.pagerduty.com/search-page", "job-boards.greenhouse.io", r"PagerDuty"),
    _common(
        "Miro", "javascript_embedded_data", "https://miro.com/careers/",
        listing_url="https://miro.com/careers/open-positions/",
        endpoint_variant="next_data", embedded_script_selector="script#__NEXT_DATA__",
        embedded_json_path="props.pageProps.jobs", id_field="id", title_field="title",
        location_field="location", url_field="id", department_field="departmentName",
        apply_url_template="https://miro.com/careers/vacancy/{id}/?gh_jid={id}",
        source_identity_regex=r"Miro", allowed_apply_hosts=["miro.com"],
        max_scripts=1, max_script_bytes=1_000_000,
    ),
    _greenhouse("Fivetran", "fivetran", "https://www.fivetran.com/careers", "www.fivetran.com", r"Fivetran"),
    _greenhouse("Braze", "braze", "https://www.braze.com/company/careers", "boards.greenhouse.io", r"Braze"),
]

EXCLUDED: list[dict[str, Any]] = [
    {
        "company": "Grammarly",
        "platform": "Superhuman unified careers",
        "official_careers_url": "https://www.grammarly.com/careers/jobs",
        "jobs_observed": None,
        "validation_status": "rebranded_parent_source_attribution_ambiguous",
        "exclusion_reason": (
            "The Grammarly careers route now redirects to Superhuman's unified careers site. "
            "Superhuman states that Grammarly acquired Coda and Superhuman Mail and launched the "
            "unified Superhuman brand; the current collection is not a Grammarly-only job source, "
            "so no child attribution is inferred."
        ),
    }
]

ROUTING_REVIEW = {
    "Grammarly": "Rebranded into the unified Superhuman company; old Grammarly Greenhouse board returns 404 and mixed parent jobs fail closed.",
    "Zoom": "The current first-party board mixes Zoom and acquired Workvivo brand cards; the contract accepts only cards whose Brand field is exactly Zoom.",
    "Okta": "The current Okta corporate board explicitly routes both Okta and acquired Auth0 hiring; it is retained as the exact Okta parent-company source.",
    "Miro": "The standalone Greenhouse board slug is stale, while the current Miro page embeds stable Greenhouse IDs and first-party vacancy URLs in __NEXT_DATA__.",
}

IDENTITY_EVIDENCE = {
    "HubSpot": "Official HubSpot page and hubspotjobs Greenhouse API; HubSpot-owned apply URLs.",
    "Dropbox": "Official Dropbox careers page and dropbox Greenhouse API; Dropbox-owned apply URLs.",
    "Zoom": "Official Zoom board with exact requisition IDs and per-card Brand=Zoom attribution.",
    "Okta": "Official Okta parent board for Okta and Auth0 hiring; Okta-owned apply URLs.",
    "Box": "Official Box server-rendered listing with stable numeric card IDs and Box-owned detail URLs.",
    "Asana": "Official Asana page and asana Greenhouse API; Asana-owned apply URLs.",
    "Notion": "Official Notion page links the notion Ashby board and stable UUID URLs.",
    "Canva": "Official Canva careers page routes to Canva's exact SmartRecruiters company API.",
    "Grammarly": "Current Grammarly careers route redirects to unified parent Superhuman careers.",
    "Vercel": "Official Vercel careers page links the exact vercel Greenhouse board.",
    "Replit": "Official Replit careers page links the exact replit Ashby board.",
    "Airtable": "Official Airtable careers route and exact airtable Greenhouse board.",
    "AppLovin": "Official AppLovin careers page and exact applovin Greenhouse board.",
    "Appian": "Official Appian careers page and exact appian Greenhouse board.",
    "CarGurus": "Official CarGurus Phenom page exposes refNum CABCARUS and first-party detail URLs.",
    "Samsara": "Official Samsara page and exact samsara Greenhouse board with Samsara-owned URLs.",
    "PagerDuty": "Official PagerDuty route and exact pagerduty Greenhouse board.",
    "Miro": "Official Miro page embeds stable job IDs and Miro-owned vacancy URLs.",
    "Fivetran": "Official Fivetran page and exact fivetran Greenhouse board.",
    "Braze": "Official Braze page and exact braze Greenhouse board.",
}

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "ashby": ashby.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "javascript_embedded_data": embedded_data.fetch_jobs,
    "phenom": phenom.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
    "static_html": static_html.fetch_jobs,
}

SAFE_TO_STAGE_FILES = [
    "config/company_software_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.software_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.software_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_software_expansion_v1_batch_a.py",
    "src/scraper/company_careers/adapters/ashby.py",
    "src/scraper/company_careers/adapters/greenhouse.py",
    "src/scraper/company_careers/adapters/static_html.py",
    "src/scraper/company_careers/models.py",
    "tests/test_greenhouse_byte_limit_regression.py",
    "tests/test_software_expansion_v1_batch_a.py",
    "tests/test_static_html_adapter.py",
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
    payload = [
        [job.job_id, job.title, job.location, job.url]
        for job in jobs
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest().upper()


def _pagination(session: requests.Session, platform: str) -> dict[str, Any]:
    attribute = {
        "javascript_embedded_data": "embedded_data_pagination",
    }.get(platform, f"{platform}_pagination")
    value = getattr(session, attribute, {})
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
        "company_attribution_result": "ambiguous_parent_collection",
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
            row["company"]: row["runs"][0].get("error")
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
        "batch": "Software Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "focus": ["software", "backend", "full-stack", "platform", "cloud", "ML", "embedded-adjacent"],
            "maximum_pages_per_source": 2,
            "maximum_normalized_jobs_per_source": 20,
            "repeat_validation_runs": REPEAT_RUNS,
            "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": sorted({item["platform"] for item in CANDIDATE_ENTRIES}),
        "generic_adapter_improvements": [
            "Greenhouse preserves student-first selection and backfills unused bounded capacity with ordinary roles.",
            "Ashby now enforces response/result bounds, stable board-owned URLs, duplicate rejection, and student-first ordering.",
            "Static HTML can extract a stable identifier from one reviewed text selector and fail closed on exact per-card brand attribution.",
        ],
        "validated_companies": [row["company"] for row in validated],
        "excluded_companies": [row["company"] for row in results if not row["production_ready"]],
        "routing_review": ROUTING_REVIEW,
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
        "parent_mappings_sha256": hashlib.sha256(PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
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
