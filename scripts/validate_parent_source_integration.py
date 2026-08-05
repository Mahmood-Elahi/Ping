"""Bounded, read-only validation for approved parent-source mappings."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.adapters import meta, sap_successfactors
from src.scraper.company_careers.models import CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import (
    annotate_child_brand_metadata,
    canonical_job_key,
    load_parent_source_mappings,
)
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION_REGISTRY = ROOT / "config" / "target_companies.yaml"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.parent_mappings_candidates.yaml"
OUTPUT_PATH = ROOT / "output" / "parent_source_integration_validation.json"
MAX_JOBS_PER_PARENT = 20
BATCH_SIZE = 5
PRIOR_VALIDATED_COUNTS = {
    "Instagram": 10,
    "Universal Robots": 3,
    "WhatsApp": 8,
}
PRIOR_VALIDATED_META_OVERLAPS = 5


def _batches(values: list[JobRecord]) -> Iterable[list[JobRecord]]:
    for offset in range(0, len(values), BATCH_SIZE):
        yield values[offset : offset + BATCH_SIZE]


def _inspect(jobs: list[JobRecord], mappings: list[Any]) -> dict[str, Any]:
    inspected = jobs[:MAX_JOBS_PER_PARENT]
    annotated: list[JobRecord] = []
    stable = True
    for batch in _batches(inspected):
        for job in batch:
            mapped = annotate_child_brand_metadata(job, mappings)
            stable = stable and canonical_job_key(job) == canonical_job_key(mapped)
            annotated.append(mapped)
    keys = [canonical_job_key(job) for job in annotated]
    return {
        "jobs": annotated,
        "parent_jobs_inspected": len(annotated),
        "stable_source_key_checks": stable,
        "duplicate_checks": len(keys) == len(set(keys)),
        "batch_sizes": [len(batch) for batch in _batches(inspected)],
    }


def _synthetic_false_positive_checks(mappings: list[Any]) -> dict[str, bool]:
    meta_job = JobRecord(
        job_id="false-meta",
        title="Instagram Infrastructure Intern",
        company="Meta",
        location="Remote",
        url="https://www.metacareers.com/jobs/false-meta/",
        updated_at="",
        source="custom:meta",
        description="Work with WhatsApp systems.",
        departments=["Infrastructure"],
    )
    robotics_job = JobRecord(
        job_id="false-robotics",
        title="Robotics Engineer",
        company="Teradyne",
        location="Odense",
        url="https://jobs.teradyne.com/job/example/false-robotics/",
        updated_at="",
        source="sap_successfactors:jobs.teradyne.com",
        description="General robotics work.",
        raw_metadata={"fields": {"Subsidiary": ["Teradyne"]}},
    )
    return {
        "meta_title_and_description_rejected": not annotate_child_brand_metadata(
            meta_job, mappings
        ).child_brands,
        "robotics_title_and_description_rejected": not annotate_child_brand_metadata(
            robotics_job, mappings
        ).child_brands,
    }


def run_validation(*, timeout: int = 20) -> dict[str, Any]:
    mappings = load_parent_source_mappings()
    production_targets = load_target_companies(str(PRODUCTION_REGISTRY))
    candidate_targets = load_target_companies(str(CANDIDATE_REGISTRY))
    production_names = {target.name for target in production_targets}
    candidate_by_name = {target.name: target for target in candidate_targets}
    session = requests.Session()

    parent_jobs: dict[str, list[JobRecord]] = {}
    diagnostics: dict[str, list[str]] = {"Meta": [], "Teradyne": []}

    meta_target = next(target for target in production_targets if target.name == "Meta")
    meta_target.max_pages = 1
    meta_target.search_terms = ["Software Engineer"]
    try:
        parent_jobs["Meta"] = meta.fetch_jobs(session, meta_target, timeout)
    except Exception as exc:
        parent_jobs["Meta"] = []
        diagnostics["Meta"].append(f"{exc.__class__.__name__}: {exc}")

    teradyne_target = candidate_by_name["Teradyne"]
    # One observed RMK table page contains 20 jobs, which keeps validation at
    # the stricter per-parent inspection ceiling while still exercising the
    # production parser and deterministic pagination contract.
    teradyne_target.max_pages = 1
    try:
        parent_jobs["Teradyne"] = sap_successfactors.fetch_jobs(
            session, teradyne_target, timeout
        )
    except Exception as exc:
        parent_jobs["Teradyne"] = []
        diagnostics["Teradyne"].append(f"{exc.__class__.__name__}: {exc}")

    inspected_by_parent = {
        parent: _inspect(jobs, mappings) for parent, jobs in parent_jobs.items()
    }
    false_positive_checks = _synthetic_false_positive_checks(mappings)
    results = []
    for mapping in mappings:
        inspected = inspected_by_parent[mapping.parent_company]
        mapped = [
            job
            for job in inspected["jobs"]
            if mapping.child_company in job.child_brands
        ]
        overlaps = [
            job
            for job in inspected["jobs"]
            if {"Instagram", "WhatsApp"}.issubset(job.child_brands)
        ]
        safe = (
            not diagnostics[mapping.parent_company]
            and inspected["stable_source_key_checks"]
            and inspected["duplicate_checks"]
            and all(false_positive_checks.values())
        )
        mapping_warnings: list[str] = []
        if not mapped:
            mapping_warnings.append(
                f"No current {mapping.child_company} jobs occurred in the bounded sample."
            )
        results.append(
            {
                "child_company": mapping.child_company,
                "parent_company": mapping.parent_company,
                "canonical_source_present_in_production": (
                    mapping.parent_company in production_names
                ),
                "canonical_source_present_in_candidate_registry": (
                    mapping.parent_company in candidate_by_name
                ),
                "canonical_adapter": (
                    "meta" if mapping.parent_company == "Meta" else "sap_successfactors"
                ),
                "parent_jobs_inspected": inspected["parent_jobs_inspected"],
                "mapped_jobs_found": len(mapped),
                "overlapping_brand_matches": len(overlaps),
                "prior_validated_mapped_jobs": PRIOR_VALIDATED_COUNTS[
                    mapping.child_company
                ],
                "prior_validated_overlapping_meta_jobs": (
                    PRIOR_VALIDATED_META_OVERLAPS
                    if mapping.parent_company == "Meta"
                    else 0
                ),
                "prior_validation_artifact": "output/quick_wins_candidate_validation.json",
                "match_fields": [mapping.match_field],
                "accepted_values": list(mapping.exact_values),
                "false_positive_checks": false_positive_checks,
                "duplicate_checks": inspected["duplicate_checks"],
                "stable_source_key_checks": inspected["stable_source_key_checks"],
                "canonical_company_checks": all(
                    job.company == mapping.parent_company for job in mapped
                ),
                "batch_sizes": inspected["batch_sizes"],
                "status": "validated" if safe else "validation_failed",
                "safe_to_activate": safe,
                "warnings": mapping_warnings,
                "diagnostics": diagnostics[mapping.parent_company],
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "maximum_jobs_inspected_per_parent": MAX_JOBS_PER_PARENT,
        "maximum_batch_size": BATCH_SIZE,
        "persistence_initialized": {
            "supabase": False,
            "google_sheets": False,
            "sqlite": False,
        },
        "results": results,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)
    payload = run_validation(timeout=max(1, min(args.timeout, 30)))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if all(item["safe_to_activate"] for item in payload["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
