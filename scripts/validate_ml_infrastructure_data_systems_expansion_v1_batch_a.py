"""Build and validate ML Infrastructure / Data Systems Expansion v1 Batch A.

The validator is intentionally non-activating: it writes isolated candidate and
validated artifacts, never production registry or publication state.
"""

from __future__ import annotations

import argparse
import copy
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
    lever,
    static_html,
)
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import (
    annotate_child_brand_metadata,
    load_parent_source_mappings,
)
from src.scraper.company_careers.student_roles import (
    student_job_exclusion_reason,
    student_job_signals,
    student_location_exclusion_reason,
)


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_ml_infrastructure_data_systems_expansion_v1_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.ml_infrastructure_data_systems_expansion_v1_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.ml_infrastructure_data_systems_expansion_v1_batch_a_validated.yaml"
CANDIDATE_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.ml_infrastructure_data_systems_expansion_v1_batch_a_candidates.yaml"
VALIDATED_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.ml_infrastructure_data_systems_expansion_v1_batch_a_validated.yaml"
REPORT = ROOT / "output" / "ml_infrastructure_data_systems_expansion_v1_batch_a_validation.json"

BASELINE_SOURCES = 437
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 208
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30
REPEAT_RUNS = 2

REVIEWED_COMPANIES = [
    "Weights & Biases", "Anyscale", "Fireworks AI", "Replicate", "Arize AI",
    "Snorkel AI", "Labelbox", "Weaviate", "Qdrant", "Zilliz", "Redis",
    "SingleStore", "ClickHouse", "MotherDuck", "dbt Labs", "Prefect",
    "Elementl / Dagster", "Astronomer", "Tecton", "H2O.ai",
]

SUMMER_2027_RE = re.compile(r"(?i)\bsummer\s+2027\b")
FOCUS_RE = re.compile(
    r"(?i)\b(machine learning|ML|AI|data|database|distributed|inference|"
    r"infrastructure|platform|backend|back[ -]?end|cloud|compute|runtime|"
    r"systems?|storage|query|vector|reliability|site reliability|SRE|DevOps|"
    r"software|engineer|orchestrat|pipeline|observability|performance|kernel|"
    r"compiler|feature store|serving)\b"
)
IN_SCOPE_RE = re.compile(
    r"(?i)\b(canada|canadian|united states|usa|u\.s\.|remote|north america|"
    r"alberta|british columbia|ontario|quebec|calgary|edmonton|vancouver|"
    r"toronto|ottawa|montreal|san francisco|san mateo|san jose|new york|"
    r"austin|seattle|boston|palo alto|mountain view|redwood city|sunnyvale|"
    r"bellevue|chicago|atlanta|washington dc|california|massachusetts|texas|"
    r"washington|amer|est timezone)\b|"
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


def _ashby(name: str, slug: str, careers_url: str, identity: str) -> dict[str, Any]:
    return _common(
        name, "ashby", careers_url, slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=["jobs.ashbyhq.com"],
    )


def _greenhouse(name: str, slug: str, careers_url: str, identity: str) -> dict[str, Any]:
    return _common(
        name, "greenhouse", careers_url, slug=slug,
        source_identity_regex=identity,
        allowed_apply_hosts=["job-boards.greenhouse.io"],
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _ashby("Anyscale", "anyscale", "https://www.anyscale.com/careers", r"\bAnyscale\b"),
    _ashby("Fireworks AI", "fireworks", "https://fireworks.ai/careers", r"Fireworks(?: AI)?"),
    _greenhouse("Arize AI", "arizeai", "https://arize.com/careers/", r"Arize(?: AI)?"),
    _greenhouse("Snorkel AI", "snorkelai", "https://snorkel.ai/join-us/", r"Snorkel(?: AI)?"),
    _greenhouse("Labelbox", "labelbox", "https://labelbox.com/company/careers/", r"\bLabelbox\b"),
    _ashby("Weaviate", "weaviate", "https://weaviate.io/company/careers", r"\bWeaviate\b"),
    _ashby("Qdrant", "qdrant.tech", "https://qdrant.tech/about-us/", r"\bQdrant\b"),
    _common(
        "Zilliz", "lever", "https://zilliz.com/careers", slug="zilliz",
        source_identity_regex=r"\bZilliz\b", allowed_apply_hosts=["jobs.lever.co"],
    ),
    _common(
        "Redis", "javascript_embedded_data",
        "https://redis.io/company/careers/current-job-openings/",
        listing_url="https://redis.io/company/careers/current-job-openings/",
        endpoint_variant="next_flight_collection",
        source_identity_regex=r"\bRedis\b",
        embedded_script_selector="script",
        embedded_json_path="positions",
        title_field="title", location_field="location", url_field="pathname",
        department_field="department",
        external_id_regex=r"-([0-9a-f]{8}-[0-9a-f-]{27})/$",
        allowed_apply_hosts=["redis.io"], max_script_bytes=1_000_000,
    ),
    _greenhouse(
        "SingleStore", "singlestore", "https://www.singlestore.com/careers/jobs/",
        r"\bSingleStore\b",
    ),
    _greenhouse(
        "ClickHouse", "clickhouse", "https://clickhouse.com/company/careers",
        r"\bClickHouse\b",
    ),
    _ashby("MotherDuck", "MotherDuck", "https://motherduck.com/careers/", r"\bMotherDuck\b"),
    _ashby("Prefect", "prefect", "https://www.prefect.io/careers", r"\bPrefect\b"),
    _ashby("Astronomer", "astronomer", "https://www.astronomer.io/careers/", r"\bAstronomer\b"),
    _common(
        "H2O.ai", "static_html", "https://h2o.ai/company/careers/",
        listing_url="https://h2oai.applytojob.com/apply",
        endpoint_variant="server_rendered_cards",
        source_identity_regex=r"H2O\.ai|h2o\.ai",
        identity_regex=r"H2O\.ai|h2o\.ai",
        listing_selector="ul.list-group", job_card_selector="li.list-group-item",
        title_selector="h3.list-group-item-heading a",
        location_selector="ul.list-inline li:first-child",
        department_selector="ul.list-inline li:nth-child(2)",
        detail_link_selector="h3.list-group-item-heading a",
        external_id_regex=r"/apply/([^/]+)/",
        allowed_apply_hosts=["h2oai.applytojob.com"],
    ),
]

WEIGHTS_BIASES_ENTRY = _common(
    "Weights & Biases", "greenhouse", "https://site.wandb.ai/careers/",
    slug="coreweave", source_identity_regex=r"Weights\s*(?:&|and)\s*Biases|W&B",
    allowed_apply_hosts=["coreweave.com"], max_response_bytes=5_000_000,
    job_attribution_field="greenhouse_metadata.Acquisition Company",
    job_attribution_regex=r"Weights & Biases",
)

WEIGHTS_BIASES_MAPPING = {
    "child_company": "Weights & Biases",
    "parent_company": "CoreWeave",
    "enabled": True,
    "match_field": "raw_metadata.greenhouse_metadata.Acquisition Company",
    "match_type": "exact_normalized",
    "accepted_values": ["Weights & Biases"],
    "canonical_source": "greenhouse:coreweave",
    "display_behavior": "child_brand_metadata",
    "confidence": "high",
    "evidence": {
        "official_child_careers_url": "https://site.wandb.ai/careers/",
        "official_parent_search_url": "https://www.coreweave.com/careers",
        "validation_artifact": "output/ml_infrastructure_data_systems_expansion_v1_batch_a_validation.json",
    },
}

EXCLUDED = [
    {
        "company": "Replicate", "platform": "Cloudflare parent routing",
        "official_careers_url": "https://replicate.com/about",
        "validation_status": "acquired_no_exact_child_attribution",
        "exclusion_reason": (
            "Replicate became part of Cloudflare in December 2025 and its official About page now "
            "routes candidates to Cloudflare Careers. The reviewed Cloudflare source exposes no "
            "structured per-job Replicate field, so title/description inference is rejected."
        ),
    },
    {
        "company": "dbt Labs", "platform": "Fivetran merged board",
        "official_careers_url": "https://www.getdbt.com/about-us/careers",
        "validation_status": "merged_stale_board_ambiguous_attribution",
        "exclusion_reason": (
            "dbt Labs and Fivetran now publish a combined Fivetran board; the former dbtlabsinc "
            "Greenhouse contract returns HTTP 404. Some office labels contain '(dbt)', but location "
            "text is not an approved structured company-attribution field."
        ),
    },
    {
        "company": "Elementl / Dagster", "platform": "Prefect parent routing",
        "official_careers_url": "https://dagster.io/careers",
        "validation_status": "acquired_redirect_no_exact_child_attribution",
        "exclusion_reason": (
            "Elementl rebranded to Dagster Labs in 2023; Prefect announced its acquisition in July "
            "2026 and the Dagster careers route now redirects to Prefect. The Prefect Ashby source "
            "has no exact structured Dagster employer field."
        ),
    },
    {
        "company": "Tecton", "platform": "Databricks parent routing",
        "official_careers_url": "https://www.tecton.ai/careers/openings/",
        "validation_status": "acquired_redirect_stale_board",
        "exclusion_reason": (
            "Databricks acquired Tecton in August 2025; Tecton's careers route redirects to "
            "Databricks and the former tecton Lever collection is empty. No exact structured "
            "Tecton attribution is exposed by the reviewed parent source."
        ),
    },
]

IDENTITY_EVIDENCE = {
    "Weights & Biases": "Official W&B careers routes to CoreWeave; the CoreWeave board exposes exact Acquisition Company metadata.",
    "Anyscale": "Official Anyscale careers links the exact anyscale Ashby board.",
    "Fireworks AI": "Official Fireworks careers embeds exact fireworks Ashby UUID job URLs.",
    "Replicate": "Official Replicate About page routes hiring to Cloudflare after the completed acquisition.",
    "Arize AI": "Official Arize careers embeds the exact arizeai Greenhouse contract.",
    "Snorkel AI": "Official Snorkel careers links exact snorkelai Greenhouse job IDs.",
    "Labelbox": "Official Labelbox careers and every bounded labelbox board description identify Labelbox.",
    "Weaviate": "Official Weaviate careers advertises its openings and every bounded weaviate board description identifies Weaviate.",
    "Qdrant": "Official Qdrant company page links open roles and every qdrant.tech board description identifies Qdrant.",
    "Zilliz": "Official Zilliz careers embeds exact zilliz Lever UUID job URLs.",
    "Redis": "Official Redis careers contains the bounded server-rendered positions collection and stable first-party UUID paths.",
    "SingleStore": "Official SingleStore jobs page links exact singlestore Greenhouse job IDs.",
    "ClickHouse": "Official ClickHouse careers declares its job collection and every bounded clickhouse board description identifies ClickHouse.",
    "MotherDuck": "Official MotherDuck careers embeds exact MotherDuck Ashby UUID job URLs.",
    "dbt Labs": "Official dbt careers now consumes the merged Fivetran board; the former exact board is 404.",
    "Prefect": "Official Prefect careers lists current roles and every bounded prefect Ashby description identifies Prefect.",
    "Elementl / Dagster": "Official Dagster acquisition page identifies Prefect as the new company and careers redirect to Prefect.",
    "Astronomer": "Official Astronomer careers exposes exact astronomer Ashby UUID job URLs.",
    "Tecton": "Official Tecton careers redirects to Databricks after acquisition; the former Lever source is empty.",
    "H2O.ai": "Official H2O.ai careers embeds the exact anonymous h2oai JazzHR widget and board.",
}

OFFICIAL_MARKERS: dict[str, str | None] = {
    "Weights & Biases": r"coreweave\.com/careers",
    "Anyscale": r"jobs\.ashbyhq\.com/anyscale",
    "Fireworks AI": r"jobs\.ashbyhq\.com/fireworks/",
    "Arize AI": r"arizeai",
    "Snorkel AI": r"job-boards\.greenhouse\.io/snorkelai/jobs/",
    "Labelbox": r"Explore open roles",
    "Weaviate": r"View Openings",
    "Qdrant": r"Join our team",
    "Zilliz": r"jobs\.lever\.co/zilliz/",
    "Redis": None,
    "SingleStore": r"(?:job-boards|boards)\.greenhouse\.io/singlestore/jobs/",
    "ClickHouse": r"Loading job openings|Job openings",
    "MotherDuck": r"jobs\.ashbyhq\.com/MotherDuck/",
    "Prefect": r"Open Roles",
    "Astronomer": r"ashbyhq\.com",
    "H2O.ai": r"app\.jazz\.co/widgets/basic/create/h2oai",
}

ROUTING_REVIEW = {
    "Weights & Biases": "Acquired by CoreWeave; exact Greenhouse Acquisition Company metadata supports one child-brand mapping.",
    "Replicate": "Acquired by Cloudflare; official hiring routes to the parent without exact child metadata.",
    "dbt Labs": "Merged with Fivetran; the legacy board is 404 and current location labels are insufficient attribution.",
    "Elementl / Dagster": "Elementl became Dagster Labs, then was acquired by Prefect; no exact child field is exposed.",
    "Tecton": "Acquired by Databricks; official hiring redirects and the legacy Lever board is empty.",
}

ADAPTERS: dict[str, Callable[[requests.Session, CompanyTarget, int], list[JobRecord]]] = {
    "ashby": ashby.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "javascript_embedded_data": embedded_data.fetch_jobs,
    "lever": lever.fetch_jobs,
    "static_html": static_html.fetch_jobs,
}

SAFE_TO_STAGE_FILES = [
    "config/company_ml_infrastructure_data_systems_expansion_v1_batch_a_candidates.csv",
    "config/company_parent_source_mappings.ml_infrastructure_data_systems_expansion_v1_batch_a_candidates.yaml",
    "config/company_parent_source_mappings.ml_infrastructure_data_systems_expansion_v1_batch_a_validated.yaml",
    "config/target_companies.ml_infrastructure_data_systems_expansion_v1_batch_a_candidates.yaml",
    "config/target_companies.ml_infrastructure_data_systems_expansion_v1_batch_a_validated.yaml",
    "scripts/validate_ml_infrastructure_data_systems_expansion_v1_batch_a.py",
    "src/scraper/company_careers/adapters/embedded_data.py",
    "src/scraper/company_careers/adapters/greenhouse.py",
    "src/scraper/company_careers/parent_sources.py",
    "tests/test_embedded_data_adapter.py",
    "tests/test_greenhouse_byte_limit_regression.py",
    "tests/test_ml_infrastructure_data_systems_expansion_v1_batch_a.py",
    "tests/test_parent_source_integration.py",
]


def _in_scope(job: JobRecord) -> bool:
    workplace = str(job.raw_metadata.get("workplaceType") or "")
    work_modes = job.raw_metadata.get("work_modes") or []
    text = " | ".join([job.location, *job.offices, workplace, *map(str, work_modes)])
    return bool(IN_SCOPE_RE.search(text))


def _student_exclusion(job: JobRecord) -> str | None:
    return student_job_exclusion_reason(job) or student_location_exclusion_reason(job)


def _student_evidence(jobs: list[JobRecord]) -> list[dict[str, Any]]:
    evidence = []
    for job in jobs:
        signals = student_job_signals(job)
        if not signals["matching_rule"]:
            continue
        reason = _student_exclusion(job)
        evidence.append({
            "job_id": job.job_id, "title": job.title, "location": job.location,
            "matching_rule": signals["matching_rule"], "exclusion_reason": reason,
            "production_match": reason is None,
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
    attribute = {
        "javascript_embedded_data": "embedded_data_pagination",
    }.get(platform, f"{platform}_pagination")
    value = getattr(session, attribute, {})
    return dict(value) if isinstance(value, dict) else {}


def _official_contract(entry: dict[str, Any], jobs: list[JobRecord]) -> dict[str, Any]:
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
    marker = OFFICIAL_MARKERS[entry["name"]]
    marker_match = bool(marker and re.search(marker, document, re.IGNORECASE))
    overlap = [
        job.job_id
        for job in jobs
        if any(
            value and str(value).casefold() in document.casefold()
            for value in (
                job.job_id,
                job.raw_metadata.get("external_job_id"),
            )
        )
    ]
    described = [job for job in jobs if (job.description or "").strip()]
    descriptions_match = bool(described) and all(
        identity.search(job.description or "") for job in described
    )
    if not marker_match and not overlap and not descriptions_match:
        raise AdapterError("official careers and reviewed board lack exact linking attribution")
    return {
        "status_code": response.status_code,
        "final_url": response.url,
        "response_bytes": len(body),
        "identity_result": "exact_match",
        "source_marker_result": "exact_match" if marker_match else "not_exposed",
        "stable_id_overlap": overlap[:5],
        "board_identity_result": (
            "exact_normalized_descriptions" if descriptions_match else "official_integration_marker"
        ),
    }


def _validate_twice(entry: dict[str, Any], *, mapped: bool = False) -> dict[str, Any]:
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
                "run": run_number, "success": True,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": len(observed), "jobs_normalized": len(scoped),
                "out_of_scope_filtered": len(observed) - len(scoped),
                "signature": _signature(scoped),
                "stable_ids": [job.job_id for job in scoped],
                "titles": [job.title for job in scoped],
                "locations": [job.location for job in scoped],
                "detail_apply_urls": [job.url for job in scoped],
                "pagination": _pagination(session, str(entry["platform"])),
            }
        except (AdapterError, requests.RequestException, TypeError, ValueError) as exc:
            scoped = []
            run = {
                "run": run_number, "success": False,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": 0, "jobs_normalized": 0, "signature": "",
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
    failure_reason = ""
    if stable:
        try:
            contract = _official_contract(entry, jobs)
        except (AdapterError, requests.RequestException, UnicodeError, re.error) as exc:
            stable = False
            jobs = []
            failure_reason = str(exc)
    return {
        "company": entry["name"], "canonical_company": entry["name"],
        "platform": ("greenhouse parent mapping" if mapped else entry["platform"]),
        "adapter": entry["platform"], "result": ("validated_parent_mapping" if mapped else "validated") if stable else "excluded",
        "official_careers_url": entry["careers_url"],
        "identity_attribution_evidence": IDENTITY_EVIDENCE[entry["name"]],
        "official_contract": contract,
        "validation_status": "repeatable_anonymous" if stable else "unstable_or_failed",
        "jobs_observed": runs[0].get("jobs_observed", 0),
        "jobs_normalized": len(jobs),
        "current_student_intern_hits": sum(_student_exclusion(job) is None for job in jobs),
        "summer_2027_hits": sum(bool(SUMMER_2027_RE.search(job.title)) for job in jobs),
        "current_focus_role_hits": sum(bool(FOCUS_RE.search(job.title)) for job in jobs),
        "current_student_candidate_evidence": _student_evidence(jobs),
        "current_focus_role_evidence": [job.title for job in jobs if FOCUS_RE.search(job.title)],
        "stable_id_result": "valid" if stable else "invalid",
        "title_result": "valid" if stable and all(job.title for job in jobs) else "invalid",
        "location_result": "valid_in_scope" if stable else "invalid",
        "apply_url_result": "valid" if stable else "invalid",
        "company_attribution_result": "exact_reviewed_source" if stable else "invalid",
        "deterministic_result": "stable_ordered_signature" if stable else "invalid",
        "production_ready": stable, "original_246_master": False, "master_rank": None,
        "failure_reason": failure_reason, "runs": runs,
    }


def _excluded_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **item, "canonical_company": item["company"], "adapter": "none",
        "result": "excluded", "identity_attribution_evidence": IDENTITY_EVIDENCE[item["company"]],
        "official_contract": {}, "jobs_observed": None, "jobs_normalized": 0,
        "current_student_intern_hits": 0, "summer_2027_hits": 0,
        "current_focus_role_hits": 0, "current_student_candidate_evidence": [],
        "current_focus_role_evidence": [], "stable_id_result": "not_validated",
        "title_result": "not_validated", "location_result": "not_validated",
        "apply_url_result": "not_validated", "company_attribution_result": "not_validated",
        "deterministic_result": "fail_closed", "production_ready": False,
        "original_246_master": False, "master_rank": None,
        "failure_reason": item["exclusion_reason"], "runs": [],
    }


def _candidate_registry_bytes() -> bytes:
    appended = registry_validator.build_candidate_registry_bytes(PRODUCTION, CANDIDATE_ENTRIES)
    old = b"    job_attribution_regex: CoreWeave\n"
    new = b"    job_attribution_regex: CoreWeave|Weights & Biases\n"
    if appended.count(old) != 1:
        raise RuntimeError("CoreWeave attribution contract was not found exactly once")
    return appended.replace(old, new, 1)


def _candidate_mapping_bytes(mapping_bytes: bytes) -> bytes:
    payload = yaml.safe_load(mapping_bytes) or {}
    mappings = list(payload.get("company_parent_source_mappings") or [])
    mappings.append(copy.deepcopy(WEIGHTS_BIASES_MAPPING))
    return yaml.safe_dump(
        {"company_parent_source_mappings": mappings},
        sort_keys=False, allow_unicode=True,
    ).encode("utf-8")


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


def _validate_parent_integration(candidate_entries: list[dict[str, Any]]) -> dict[str, Any]:
    coreweave = next(item for item in candidate_entries if item["name"] == "CoreWeave")
    session = requests.Session()
    jobs = greenhouse.fetch_jobs(
        session, CompanyTarget.from_dict(coreweave), MAX_TIMEOUT_SECONDS
    )
    mappings = load_parent_source_mappings(CANDIDATE_MAPPINGS)
    mapped = [annotate_child_brand_metadata(job, mappings) for job in jobs]
    child_jobs = [job for job in mapped if "Weights & Biases" in job.child_brands]
    if not child_jobs:
        raise RuntimeError("Candidate CoreWeave source did not retain an exact W&B mapped row")
    return {
        "canonical_source": "greenhouse:coreweave",
        "parent_jobs_observed": len(jobs),
        "mapped_jobs_observed": len(child_jobs),
        "mapped_stable_ids": [job.job_id for job in child_jobs],
        "match_field": WEIGHTS_BIASES_MAPPING["match_field"],
        "accepted_values": WEIGHTS_BIASES_MAPPING["accepted_values"],
    }


def build() -> dict[str, Any]:
    production_bytes = PRODUCTION.read_bytes()
    mapping_bytes = PARENT_MAPPINGS.read_bytes()
    production_payload = yaml.safe_load(production_bytes) or {}
    production_entries = list(production_payload.get("target_companies") or [])
    if len(production_entries) != BASELINE_SOURCES:
        raise RuntimeError(f"Expected {BASELINE_SOURCES} production sources, found {len(production_entries)}")
    if len(load_parent_source_mappings(PARENT_MAPPINGS)) != BASELINE_MAPPINGS:
        raise RuntimeError("Production parent-mapping baseline drifted")
    existing = {str(item.get("name") or "").strip().casefold() for item in production_entries}
    duplicates = [item["name"] for item in CANDIDATE_ENTRIES if item["name"].casefold() in existing]
    if duplicates:
        raise RuntimeError(f"Candidate companies already exist in production: {duplicates}")
    config_errors = {
        item["name"]: registry_validator.configuration_error(item)
        for item in [*CANDIDATE_ENTRIES, WEIGHTS_BIASES_ENTRY]
    }
    if any(config_errors.values()):
        raise RuntimeError(f"Candidate configuration errors: {config_errors}")

    direct = [_validate_twice(item) for item in CANDIDATE_ENTRIES]
    weights = _validate_twice(WEIGHTS_BIASES_ENTRY, mapped=True)
    validated = [weights, *direct]
    if not all(row["production_ready"] for row in validated):
        failures = {
            row["company"]: row["failure_reason"] or [
                run.get("error") for run in row["runs"] if not run["success"]
            ]
            for row in validated if not row["production_ready"]
        }
        raise RuntimeError(f"At least one candidate failed closed: {failures}")

    candidate_bytes = _candidate_registry_bytes()
    CANDIDATE_REGISTRY.write_bytes(candidate_bytes)
    VALIDATED_REGISTRY.write_bytes(candidate_bytes)
    mapping_candidate_bytes = _candidate_mapping_bytes(mapping_bytes)
    CANDIDATE_MAPPINGS.write_bytes(mapping_candidate_bytes)
    VALIDATED_MAPPINGS.write_bytes(mapping_candidate_bytes)

    candidate_payload = yaml.safe_load(candidate_bytes) or {}
    candidate_entries = list(candidate_payload.get("target_companies") or [])
    if len(candidate_entries) != BASELINE_SOURCES + len(CANDIDATE_ENTRIES):
        raise RuntimeError("Candidate registry source projection drifted")
    if len(load_parent_source_mappings(CANDIDATE_MAPPINGS)) != BASELINE_MAPPINGS + 1:
        raise RuntimeError("Candidate parent-mapping projection drifted")
    parent_validation = _validate_parent_integration(candidate_entries)

    excluded = [_excluded_row(item) for item in EXCLUDED]
    by_name = {row["company"]: row for row in [*validated, *excluded]}
    if set(by_name) != set(REVIEWED_COMPANIES):
        raise RuntimeError("Reviewed company inventory drifted")
    results = [by_name[name] for name in REVIEWED_COMPANIES]
    _write_csv(results)

    report = {
        "batch": "ML Infrastructure / Data Systems Expansion v1 Batch A",
        "reviewed_companies": REVIEWED_COMPANIES,
        "baseline": {
            "production_sources": BASELINE_SOURCES,
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
        },
        "limits": {
            "geography": ["Canada", "United States", "Remote"],
            "focus": [
                "ML systems", "data infrastructure", "distributed systems", "backend",
                "platform", "inference", "software",
            ],
            "maximum_pages_per_source": 1, "maximum_jobs_per_source": 20,
            "repeat_validation_runs": REPEAT_RUNS,
            "maximum_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "adapter_reuse": sorted({item["platform"] for item in CANDIDATE_ENTRIES} | {"greenhouse"}),
        "generic_adapter_improvements": [
            "bounded Next.js Flight inline-collection decoding without JavaScript execution",
            "structured Greenhouse attribution-group representation after student priority",
            "exact Greenhouse acquisition-company parent mapping field",
        ],
        "validated_companies": [row["company"] for row in validated],
        "direct_source_companies": [row["company"] for row in direct],
        "mapped_companies": ["Weights & Biases"],
        "excluded_companies": [row["company"] for row in excluded],
        "routing_review": ROUTING_REVIEW,
        "parent_mapping_validation": parent_validation,
        "results": results,
        "blockers": {row["company"]: row["exclusion_reason"] for row in excluded},
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
            "parent_mappings": BASELINE_MAPPINGS + 1,
            "mapping_delta": 1,
            "original_master_coverage": f"{BASELINE_COVERAGE}/{MASTER_TOTAL}",
            "master_delta": 0,
        },
        "candidate_registry": str(CANDIDATE_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "validated_registry": str(VALIDATED_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "candidate_parent_mappings": str(CANDIDATE_MAPPINGS.relative_to(ROOT)).replace("\\", "/"),
        "validated_parent_mappings": str(VALIDATED_MAPPINGS.relative_to(ROOT)).replace("\\", "/"),
        "production_registry_sha256": hashlib.sha256(production_bytes).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mapping_bytes).hexdigest().upper(),
        "safe_to_stage_files": SAFE_TO_STAGE_FILES,
        "production_registry_activated": False,
        "publication": {
            "production_run": False, "staged": False, "committed": False,
            "pushed": False, "dashboard_scan": False, "dashboard_publication": False,
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
        "jobs": report["jobs"], "projection": report["projection"],
        "production_registry_activated": report["production_registry_activated"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
