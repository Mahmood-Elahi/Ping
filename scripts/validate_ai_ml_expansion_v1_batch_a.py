"""Build and validate AI/ML Expansion v1 Batch A without activation."""

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
from src.scraper.company_careers.adapters import ashby, greenhouse, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.student_roles import (
    student_job_exclusion_reason,
    student_job_signals,
    student_location_exclusion_reason,
)


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_ai_ml_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.ai_ml_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.ai_ml_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "ai_ml_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 418
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "Together AI", "Groq", "SambaNova Systems", "Etched", "d-Matrix",
    "Cognition", "Anysphere", "Glean", "Harvey", "Writer", "Runway",
    "ElevenLabs", "Synthesia", "Sierra", "Abridge", "Hebbia", "Pinecone",
    "Modal", "Baseten", "LangChain",
]

SUMMER_2027_RE = re.compile(r"(?i)\bsummer\s+2027\b")
FOCUS_RE = re.compile(
    r"(?i)\b(artificial intelligence|AI|machine learning|ML|deep learning|"
    r"research|research engineer|research scientist|software|compiler|kernel|"
    r"inference|performance|accelerator|ASIC|SoC|silicon|firmware|embedded|"
    r"hardware systems?|design verification|RTL|physical design|architecture|"
    r"distributed systems?|infrastructure|platform|compute|GPU|cloud|runtime|"
    r"backend|back[ -]?end|data systems?|model|training|post-training)\b"
)
IN_SCOPE_RE = re.compile(
    r"(?i)\b(canada|canadian|united states|usa|u\.s\.|remote|north america|"
    r"alberta|british columbia|ontario|quebec|calgary|edmonton|vancouver|"
    r"toronto|ottawa|montreal|san francisco|san jose|new york|austin|seattle|"
    r"boston|palo alto|mountain view|redwood city|sunnyvale|bellevue|chicago|"
    r"atlanta|washington dc|california|massachusetts|texas|washington)\b|"
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


def _ashby(
    name: str, slug: str, careers_url: str, identity: str, **fields: Any
) -> dict[str, Any]:
    return _common(
        name,
        "ashby",
        careers_url,
        slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=["jobs.ashbyhq.com"],
        **fields,
    )


def _greenhouse(
    name: str, slug: str, careers_url: str, identity: str, **fields: Any
) -> dict[str, Any]:
    apply_hosts = fields.pop("allowed_apply_hosts", ["job-boards.greenhouse.io"])
    return _common(
        name,
        "greenhouse",
        careers_url,
        slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=apply_hosts,
        **fields,
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _greenhouse(
        "Together AI", "togetherai", "https://www.together.ai/careers", r"Together AI",
    ),
    _greenhouse(
        "SambaNova Systems", "sambanovasystems",
        "https://sambanova.ai/company/careers/job-openings", r"SambaNova Systems",
        allowed_apply_hosts=["sambanova.ai"],
    ),
    _ashby("Etched", "etched", "https://www.etched.com/join", r"Etched"),
    _ashby("d-Matrix", "d-Matrix", "https://www.d-matrix.ai/careers/", r"d-Matrix"),
    _ashby("Cognition", "cognition", "https://cognition.com/careers", r"Cognition"),
    _common(
        "Anysphere",
        "static_html",
        "https://cursor.com/careers",
        listing_url="https://cursor.com/careers",
        endpoint_variant="server_rendered_links",
        source_identity_regex=r"Anysphere, Inc\.",
        listing_selector="main#main",
        job_card_selector="a.card[href^=/careers/]",
        title_selector="p.type-base",
        location_selector=".text-theme-text-sec span:last-child",
        department_selector=".text-theme-text-sec span:first-child",
        detail_link_selector=":self",
        external_id_regex=r"/careers/([^/?#]+)",
        allowed_apply_hosts=["cursor.com"],
    ),
    _greenhouse("Glean", "gleanwork", "https://www.glean.com/careers", r"Glean"),
    _ashby("Harvey", "harvey", "https://www.harvey.ai/careers", r"Harvey"),
    _ashby("Writer", "writer", "https://writer.com/company/careers/", r"WRITER|Writer"),
    _ashby("Runway", "runway-ml", "https://runway.com/careers", r"Runway"),
    _ashby("ElevenLabs", "elevenlabs", "https://elevenlabs.io/careers", r"ElevenLabs"),
    _ashby("Synthesia", "synthesia", "https://www.synthesia.io/careers", r"Synthesia"),
    _ashby("Sierra", "Sierra", "https://sierra.ai/careers", r"Sierra"),
    _ashby("Abridge", "Abridge", "https://www.abridge.com/life-at-abridge", r"Abridge"),
    _ashby("Hebbia", "hebbia-ai", "https://www.hebbia.com/careers", r"Hebbia"),
    _ashby("Pinecone", "pinecone", "https://www.pinecone.io/careers/", r"Pinecone"),
    _ashby("Modal", "modal", "https://modal.com/company", r"Modal"),
    _ashby(
        "Baseten", "baseten", "https://www.baseten.co/resources/careers/", r"Baseten",
    ),
    _ashby("LangChain", "langchain", "https://www.langchain.com/careers", r"LangChain"),
]

EXCLUDED: list[dict[str, Any]] = [
    {
        "company": "Groq",
        "platform": "stale Greenhouse / no current listing contract",
        "official_careers_url": "https://groq.com/careers-at-groq",
        "jobs_observed": None,
        "validation_status": "stale_board_and_route",
        "exclusion_reason": (
            "The former groq Greenhouse board and anonymous API both return HTTP 404, while "
            "the official careers route redirects to the general company page without a current "
            "job collection. Groq remains independent after its non-exclusive NVIDIA licensing "
            "transaction, so no parent mapping is inferred."
        ),
    },
]

OFFICIAL_CONTRACT_MARKERS: dict[str, str | None] = {
    "Together AI": r"boards-api\.greenhouse\.io/v1/boards/togetherai/jobs",
    "SambaNova Systems": r"greenhouse\.io/embed/job_board/js\?for=sambanovasystems",
    "Etched": r"jobs\.ashbyhq\.com/etched/",
    "d-Matrix": r"jobs\.ashbyhq\.com/d-Matrix",
    "Cognition": r"jobs\.ashbyhq\.com/cognition/",
    "Anysphere": r"href=[\"']?/careers/",
    "Glean": r"gleanwork",
    "Harvey": None,
    "Writer": r"jobs\.ashbyhq\.com/writer",
    "Runway": r"jobs\.ashbyhq\.com/runway-ml/",
    "ElevenLabs": r"jobs\.ashbyhq\.com/elevenlabs",
    "Synthesia": r"JOB_BOARD_NAME\s*=\s*[\"']synthesia[\"']",
    "Sierra": r"jobs\.ashbyhq\.com/Sierra/",
    "Abridge": r"jobs\.ashbyhq\.com/Abridge",
    "Hebbia": r"jobs\.ashbyhq\.com/hebbia-ai/",
    "Pinecone": r"jobs\.ashbyhq\.com/pinecone/",
    "Modal": r"jobs\.ashbyhq\.com/modal",
    "Baseten": r"jobs\.ashbyhq\.com/baseten",
    "LangChain": r"jobs\.ashbyhq\.com/langchain",
}

ROUTING_REVIEW = {
    "Groq": (
        "The December 2025 NVIDIA transaction was a non-exclusive technology license, not an "
        "acquisition; Groq states that it remains independent. Its old Greenhouse board is 404 "
        "and its careers route now redirects to a non-listing company page, so it fails closed."
    ),
    "Anysphere": (
        "Anysphere hires under the Cursor product brand. The official Cursor careers and detail "
        "pages identify Anysphere, Inc. as the legal employer; the disabled Ashby mirror is not used."
    ),
    "Runway": (
        "The first-party careers route has moved from runwayml.com to runway.com. The official "
        "page still links the exact runway-ml Ashby board, so the current domain and legacy ATS "
        "slug are intentionally paired."
    ),
    "Writer": (
        "The current first-party route is /company/careers and the official WRITER page links the "
        "case-insensitive writer Ashby board; no stale /careers route is stored."
    ),
    "Hebbia": (
        "Hebbia's former Greenhouse pages remain discoverable but the current first-party page "
        "links hebbia-ai Ashby UUIDs, so the stale Greenhouse source is not selected."
    ),
}

IDENTITY_EVIDENCE = {
    "Together AI": "Official Together AI careers HTML embeds the exact togetherai Greenhouse API contract.",
    "Groq": "Official Groq careers route redirects away from listings; the former exact Greenhouse board is 404.",
    "SambaNova Systems": "Official SambaNova jobs page embeds the exact sambanovasystems Greenhouse board.",
    "Etched": "Official Etched Join page links exact etched Ashby UUID job URLs.",
    "d-Matrix": "Official d-Matrix careers page links the exact d-Matrix Ashby board.",
    "Cognition": "Official Cognition careers page links exact cognition Ashby UUID job URLs.",
    "Anysphere": "Official Cursor careers and job pages identify Anysphere, Inc. and expose stable first-party paths.",
    "Glean": "Official Glean careers page loads the exact gleanwork Greenhouse board contract.",
    "Harvey": "Official Harvey careers UUIDs match the exact Harvey-attributed Ashby board UUIDs.",
    "Writer": "Official WRITER careers page links the exact writer Ashby board.",
    "Runway": "Official runway.com careers page links exact runway-ml Ashby UUID job URLs.",
    "ElevenLabs": "Official ElevenLabs careers UUIDs match exact ElevenLabs-attributed Ashby board UUIDs.",
    "Synthesia": "Official Synthesia careers UUIDs match exact Synthesia-attributed Ashby board UUIDs.",
    "Sierra": "Official Sierra careers page links exact Sierra Ashby UUID job URLs.",
    "Abridge": "Official Abridge Life page links the exact Abridge Ashby board and UUID roles.",
    "Hebbia": "Official Hebbia careers page links current hebbia-ai Ashby UUID job URLs.",
    "Pinecone": "Official Pinecone careers page links exact pinecone Ashby UUID job URLs.",
    "Modal": "Official Modal company careers section links exact modal Ashby UUID job URLs.",
    "Baseten": "Official Baseten careers page links the exact baseten Ashby board.",
    "LangChain": "Official LangChain careers page embeds the exact langchain Ashby board.",
}

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "ashby": ashby.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "static_html": static_html.fetch_jobs,
}

SAFE_TO_STAGE_FILES = [
    "config/company_ai_ml_expansion_v1_batch_a_candidates.csv",
    "config/target_companies.ai_ml_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.ai_ml_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_ai_ml_expansion_v1_batch_a.py",
    "src/scraper/company_careers/student_roles.py",
    "tests/test_ai_ml_expansion_v1_batch_a.py",
    "tests/test_company_careers.py",
]


def _in_scope(job: JobRecord) -> bool:
    return bool(IN_SCOPE_RE.search(" | ".join([job.location, *job.offices])))


def _production_student_exclusion_reason(job: JobRecord) -> str | None:
    role_reason = student_job_exclusion_reason(job)
    if role_reason:
        return role_reason
    return student_location_exclusion_reason(job)


def _is_production_student_hit(job: JobRecord) -> bool:
    return _production_student_exclusion_reason(job) is None


def _student_candidate_evidence(jobs: list[JobRecord]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for job in jobs:
        signals = student_job_signals(job)
        if not signals["matching_rule"]:
            continue
        role_reason = student_job_exclusion_reason(job)
        location_reason = student_location_exclusion_reason(job)
        evidence.append({
            "job_id": job.job_id,
            "title": job.title,
            "location": job.location,
            "matching_rule": signals["matching_rule"],
            "role_exclusion": role_reason,
            "location_exclusion": location_reason,
            "production_match": role_reason is None and location_reason is None,
        })
    return evidence


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


def _official_contract(entry: dict[str, Any], stable_ids: list[str]) -> dict[str, Any]:
    response = requests.get(
        str(entry["careers_url"]),
        headers={"Accept": "text/html", "User-Agent": "job-radar-contract-validator/1.0"},
        timeout=MAX_TIMEOUT_SECONDS,
    )
    body = bytes(response.content or b"")
    if response.status_code != 200:
        raise AdapterError(f"official careers HTTP {response.status_code}")
    if not body or len(body) > int(entry["max_response_bytes"]):
        raise AdapterError("official careers response is empty or exceeds the byte limit")
    document = body.decode("utf-8")
    identity = re.compile(str(entry["source_identity_regex"]), re.IGNORECASE)
    if not identity.search(document):
        raise AdapterError("official careers response failed exact identity attribution")
    marker = OFFICIAL_CONTRACT_MARKERS[entry["name"]]
    marker_result = bool(marker and re.search(marker, document, re.IGNORECASE))
    overlap = [job_id for job_id in stable_ids if job_id.casefold() in document.casefold()]
    endpoint_result = "not_required"
    if entry["name"] == "Harvey":
        endpoint = requests.get(
            "https://www.harvey.ai/api/ashby/jobs",
            headers={"Accept": "application/json", "User-Agent": "job-radar-contract-validator/1.0"},
            timeout=MAX_TIMEOUT_SECONDS,
        )
        endpoint_body = bytes(endpoint.content or b"")
        if endpoint.status_code != 200 or not endpoint_body or len(endpoint_body) > 1_000_000:
            raise AdapterError("official Harvey Ashby proxy failed its bounded anonymous contract")
        endpoint_payload = endpoint.json()
        endpoint_jobs = endpoint_payload.get("jobs") if isinstance(endpoint_payload, dict) else None
        if not isinstance(endpoint_jobs, list):
            raise AdapterError("official Harvey Ashby proxy is missing its stable job collection")
        endpoint_ids = {
            str(item.get("id") or "").strip().casefold()
            for item in endpoint_jobs if isinstance(item, dict)
        }
        overlap = [job_id for job_id in stable_ids if job_id.casefold() in endpoint_ids]
        endpoint_result = "stable_id_overlap" if overlap else "invalid"
    if entry["name"] == "ElevenLabs" and marker and not marker_result:
        script_urls = re.findall(r"<script[^>]+src=[\"']([^\"']+)", document, re.IGNORECASE)
        reviewed_scripts = [url for url in script_urls if "global-error-" in url]
        if len(reviewed_scripts) != 1:
            raise AdapterError("official ElevenLabs page has no unique reviewed client configuration")
        script_url = reviewed_scripts[0]
        script_host = (urlparse(script_url).hostname or "").casefold()
        if script_host != "eleven-public-cdn.elevenlabs.io":
            raise AdapterError("official ElevenLabs client configuration uses an unreviewed host")
        script = requests.get(
            script_url,
            headers={"Accept": "application/javascript", "User-Agent": "job-radar-contract-validator/1.0"},
            timeout=MAX_TIMEOUT_SECONDS,
        )
        script_body = bytes(script.content or b"")
        if script.status_code != 200 or not script_body or len(script_body) > 1_000_000:
            raise AdapterError("official ElevenLabs client configuration failed its byte contract")
        marker_result = bool(re.search(marker, script_body.decode("utf-8"), re.IGNORECASE))
    if not marker_result and not overlap:
        raise AdapterError("official careers response does not expose the reviewed source or stable IDs")
    return {
        "status_code": response.status_code,
        "final_url": response.url,
        "response_bytes": len(body),
        "identity_result": "exact_match",
        "source_marker_result": "exact_match" if marker_result else "stable_id_overlap",
        "endpoint_result": endpoint_result,
        "stable_id_overlap": overlap[:5],
    }


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
                "student_hits": sum(_is_production_student_hit(job) for job in scoped),
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
    contract: dict[str, Any] = {}
    contract_error = ""
    if stable:
        try:
            contract = _official_contract(entry, [job.job_id for job in jobs])
        except (AdapterError, requests.RequestException, UnicodeError, re.error) as exc:
            contract_error = str(exc)
            stable = False
            jobs = []
    return {
        "company": entry["name"],
        "canonical_company": entry["name"],
        "platform": entry["platform"],
        "adapter": entry["platform"],
        "result": "validated" if stable else "excluded",
        "official_careers_url": entry["careers_url"],
        "identity_attribution_evidence": IDENTITY_EVIDENCE[entry["name"]],
        "official_contract": contract,
        "validation_status": "repeatable_anonymous" if stable else "unstable_or_failed",
        "jobs_observed": runs[0].get("jobs_observed", 0),
        "jobs_normalized": len(jobs),
        "current_student_intern_hits": sum(_is_production_student_hit(job) for job in jobs),
        "summer_2027_hits": sum(bool(SUMMER_2027_RE.search(job.title)) for job in jobs),
        "current_focus_role_hits": sum(bool(FOCUS_RE.search(job.title)) for job in jobs),
        "current_student_role_evidence": [
            job.title for job in jobs if _is_production_student_hit(job)
        ],
        "current_student_candidate_evidence": _student_candidate_evidence(jobs),
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
        "failure_reason": contract_error,
        "runs": runs,
    }


def _excluded_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "canonical_company": row["company"],
        "adapter": "none",
        "result": "excluded",
        "identity_attribution_evidence": IDENTITY_EVIDENCE[row["company"]],
        "official_contract": {},
        "jobs_normalized": 0,
        "current_student_intern_hits": 0,
        "summer_2027_hits": 0,
        "current_focus_role_hits": 0,
        "current_student_role_evidence": [],
        "current_student_candidate_evidence": [],
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
        "failure_reason": row["exclusion_reason"],
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
        raise RuntimeError(f"Expected {BASELINE_SOURCES} production sources, found {len(production_entries)}")
    if _mapping_count() != BASELINE_MAPPINGS:
        raise RuntimeError(f"Expected {BASELINE_MAPPINGS} parent mappings, found {_mapping_count()}")
    existing = {str(item.get("name") or "").strip().casefold() for item in production_entries}
    duplicates = [item["name"] for item in CANDIDATE_ENTRIES if item["name"].casefold() in existing]
    if duplicates:
        raise RuntimeError(f"Candidate companies already exist in production: {duplicates}")
    config_errors = {item["name"]: registry_validator.configuration_error(item) for item in CANDIDATE_ENTRIES}
    if any(config_errors.values()):
        raise RuntimeError(f"Candidate configuration errors: {config_errors}")

    validated = [_validate_twice(item) for item in CANDIDATE_ENTRIES]
    if not all(row["production_ready"] for row in validated):
        failures = {
            row["company"]: row["failure_reason"] or [
                run.get("error") for run in row["runs"] if not run["success"]
            ]
            for row in validated if not row["production_ready"]
        }
        raise RuntimeError(f"At least one candidate failed closed: {failures}")
    by_name = {row["company"]: row for row in [*validated, *map(_excluded_row, EXCLUDED)]}
    if set(by_name) != set(REVIEWED_COMPANIES):
        raise RuntimeError("Reviewed company inventory drifted")
    results = [by_name[name] for name in REVIEWED_COMPANIES]

    candidate_bytes = registry_validator.build_candidate_registry_bytes(PRODUCTION, CANDIDATE_ENTRIES)
    CANDIDATE_REGISTRY.write_bytes(candidate_bytes)
    VALIDATED_REGISTRY.write_bytes(candidate_bytes)
    _write_csv(results)
    report = {
        "batch": "AI/ML Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "focus": [
                "AI/ML", "software", "inference", "ML systems", "embedded/accelerator", "research",
            ],
            "maximum_pages_per_source": 1,
            "maximum_normalized_jobs_per_source": 20,
            "repeat_validation_runs": REPEAT_RUNS,
            "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": sorted({item["platform"] for item in CANDIDATE_ENTRIES}),
        "generic_adapter_improvements": [],
        "validated_companies": [row["company"] for row in validated],
        "excluded_companies": [row["company"] for row in results if not row["production_ready"]],
        "routing_review": ROUTING_REVIEW,
        "results": results,
        "blockers": {row["company"]: row["exclusion_reason"] for row in results if not row["production_ready"]},
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
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
