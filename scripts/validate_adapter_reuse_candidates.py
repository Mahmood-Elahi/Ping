"""Build and live-validate the v3 existing-adapter reuse candidates.

The command is deliberately read-only outside its candidate registry, validated
registry, and Git-ignored JSON report. It uses the production scanner dispatch
and never initializes persistence or publishing integrations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
RESEARCH = ROOT / "config" / "company_existing_adapter_reuse_candidates_v3.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.adapter_reuse_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.adapter_reuse_validated.yaml"
REPORT = ROOT / "output" / "adapter_reuse_candidate_validation.json"

MAX_BATCH_SIZE = 5
MAX_PAGES = 2
MAX_JOBS = 20
SAFE_STATUSES = {validator.VALIDATED_WITH_JOBS, validator.VALIDATED_ZERO_JOBS}


def _workday(
    rank: int,
    name: str,
    careers_url: str,
    tenant: str,
    wd_subdomain: str,
    site_id: str,
) -> dict[str, Any]:
    host = f"{tenant}.{wd_subdomain}.myworkdayjobs.com"
    return {
        "name": name,
        "platform": "workday",
        "tenant": tenant,
        "wd_subdomain": wd_subdomain,
        "site_id": site_id,
        "careers_url": careers_url,
        "search_url": f"https://{host}/{site_id}",
        "endpoint_variant": "wday_cxs_v1",
        "original_workbook_rank": rank,
        "allowed_apply_hosts": [host],
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
    }


def _slug_target(
    rank: int,
    name: str,
    platform: str,
    slug: str,
    careers_url: str,
    search_url: str,
    endpoint_variant: str,
    allowed_apply_hosts: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": platform,
        "slug": slug,
        "careers_url": careers_url,
        "search_url": search_url,
        "endpoint_variant": endpoint_variant,
        "original_workbook_rank": rank,
        "allowed_apply_hosts": allowed_apply_hosts,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
    }


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _workday(21, "Samsung Electronics", "https://www.samsung.com/us/careers/", "sec", "wd3", "Samsung_Careers"),
    _workday(56, "Visa", "https://corporate.visa.com/en/careers.html", "visa", "wd5", "Visa"),
    _slug_target(63, "Plaid", "ashby", "plaid", "https://plaid.com/careers/", "https://jobs.ashbyhq.com/plaid", "posting_api_job_board", ["jobs.ashbyhq.com"]),
    _slug_target(71, "Kraken", "ashby", "kraken.com", "https://www.kraken.com/careers", "https://jobs.ashbyhq.com/kraken.com", "posting_api_job_board", ["jobs.ashbyhq.com"]),
    _slug_target(76, "Hudson River Trading", "greenhouse", "hrttalentcommunity", "https://www.hudsonrivertrading.com/careers/", "https://job-boards.greenhouse.io/hrttalentcommunity", "boards_api_v1", ["boards.greenhouse.io", "job-boards.greenhouse.io", "www.hudsonrivertrading.com"]),
    _workday(92, "Rockwell Automation", "https://www.rockwellautomation.com/en-us/careers.html", "rockwellautomation", "wd1", "External_Rockwell_Automation"),
    _slug_target(105, "Phoenix Contact", "greenhouse", "phoenixcontact", "https://www.phoenixcontact.com/en-us/company/careers", "https://job-boards.greenhouse.io/phoenixcontact", "boards_api_v1", ["boards.greenhouse.io", "job-boards.greenhouse.io"]),
    _workday(108, "GE Vernova", "https://careers.gevernova.com/", "gevernova", "wd5", "Vernova_ExternalSite"),
    _workday(129, "Valeo", "https://www.valeo.com/en/careers/", "valeo", "wd3", "valeo_jobs"),
    _workday(146, "PTC", "https://www.ptc.com/en/careers", "ptc", "wd1", "PTC"),
    _slug_target(153, "Agility Robotics", "greenhouse", "agilityrobotics", "https://www.agilityrobotics.com/careers", "https://job-boards.greenhouse.io/agilityrobotics", "boards_api_v1", ["boards.greenhouse.io", "job-boards.greenhouse.io", "www.agilityrobotics.com"]),
    _workday(176, "TC Energy", "https://www.tcenergy.com/careers/", "tcenergy", "wd3", "CAREER_SITE_TC"),
    _workday(177, "ConocoPhillips", "https://careers.conocophillips.com/", "conocophillips", "wd1", "External"),
    _workday(180, "Ovintiv", "https://www.ovintiv.com/careers/", "ovintiv", "wd3", "ovintivcareers"),
    _workday(183, "Keyera", "https://www.keyera.com/careers/", "keyera", "wd10", "Keyera_Careers"),
    _workday(188, "Sierra Space", "https://www.sierraspace.com/careers/", "snc", "wd1", "Sierra_Space_External_Career_Site"),
    _slug_target(196, "ispace", "lever", "ispace-inc", "https://www.ispace-inc.com/careers/", "https://jobs.lever.co/ispace-inc", "postings_api_v0", ["jobs.lever.co"]),
]

EXPECTED_COMPANIES = [entry["name"] for entry in CANDIDATE_ENTRIES]


def _research_candidates(path: Path = RESEARCH) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    candidates = [
        row["company"]
        for row in rows
        if row.get("safe_for_later_activation", "").casefold() == "true"
        and row.get("needs_adapter_extension", "").casefold() == "false"
    ]
    return candidates


def reconcile_candidates() -> None:
    research_names = _research_candidates()
    if set(research_names) != set(EXPECTED_COMPANIES) or len(research_names) != len(EXPECTED_COMPANIES):
        raise ValueError(
            "Research input does not contain the exact 17-company adapter-reuse set: "
            f"expected={EXPECTED_COMPANIES!r}, found={research_names!r}"
        )
    ranks = [int(entry["original_workbook_rank"]) for entry in CANDIDATE_ENTRIES]
    if ranks != sorted(ranks):
        raise ValueError("Candidate entries are not in original workbook order")
    for entry in CANDIDATE_ENTRIES:
        error = validator.configuration_error(entry)
        if error:
            raise ValueError(f"{entry['name']}: {error}")
        if not 1 <= int(entry["max_pages"]) <= MAX_PAGES:
            raise ValueError(f"{entry['name']}: max_pages exceeds validation bound")
        if int(entry["max_jobs"]) != MAX_JOBS:
            raise ValueError(f"{entry['name']}: max_jobs must be {MAX_JOBS}")


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
) -> bytes:
    reconcile_candidates()
    _, production_entries = validator.load_registry(production)
    duplicate_names = validator._duplicate_company_owners(production_entries, CANDIDATE_ENTRIES)
    duplicate_sources = validator._duplicate_owners(production_entries, CANDIDATE_ENTRIES)
    if duplicate_names or duplicate_sources:
        raise ValueError(
            f"Candidate registry contains duplicates: companies={duplicate_names}, sources={duplicate_sources}"
        )
    content = validator.build_candidate_registry_bytes(production, CANDIDATE_ENTRIES)
    validator._write_bytes_if_changed(destination, content)
    loaded = load_target_companies(str(destination))
    if len(loaded) != len(production_entries) + len(CANDIDATE_ENTRIES):
        raise ValueError("Production registry loader did not accept every candidate entry")
    return content


def _status(result: Mapping[str, Any]) -> str:
    status = str(result["validation_status"])
    if status in SAFE_STATUSES:
        return status
    if status == validator.STALE_FINGERPRINT:
        return "stale_source"
    if status == validator.UNSUPPORTED_RESPONSE:
        return "malformed_response" if result.get("http_status") == 200 else "failed_technical"
    if status == validator.TEMPORARY_FAILURE:
        return "failed_technical"
    return status


def _pagination(entry: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    existing = result.get("pagination_result")
    if isinstance(existing, Mapping):
        pagination = dict(existing)
    else:
        pagination = {
            "pages_fetched": int(result.get("pages_requested") or 0),
            "stop_reason": "single_page_validation_cap",
        }
    pagination.update(
        maximum_pages=min(MAX_PAGES, int(entry.get("max_pages") or 1)),
        maximum_jobs=MAX_JOBS,
        deterministic=int(result.get("pages_requested") or 0) <= MAX_PAGES,
    )
    return pagination


def _report_result(entry: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    status = _status(raw)
    safe = status in SAFE_STATUSES
    endpoint = validator.endpoint_for(entry) or ""
    warning = raw.get("warning")
    error = raw.get("error")
    diagnostics = [
        f"HTTP status: {raw.get('http_status')}",
        f"Network attempts: {raw.get('attempts')}",
        f"Endpoint tested: {raw.get('endpoint_tested') or endpoint}",
    ]
    if error:
        diagnostics.append(str(error))
    return {
        "company": entry["name"],
        "original_workbook_rank": entry["original_workbook_rank"],
        "production_adapter": entry["platform"],
        "official_careers_url": entry["careers_url"],
        "official_search_url": entry["search_url"],
        "platform_family": entry["platform"],
        "platform_variant": entry["endpoint_variant"],
        "endpoint_host": urlparse(endpoint).hostname,
        "source_identifier": validator.source_key(entry),
        "configured_identifiers": validator.configured_identifiers(entry),
        "maximum_pages": entry["max_pages"],
        "maximum_jobs": entry["max_jobs"],
        "allowed_apply_hosts": entry["allowed_apply_hosts"],
        "pages_requested": raw.get("pages_requested", 0),
        "jobs_returned": raw.get("jobs_returned", 0),
        "jobs_normalized": raw.get("jobs_normalized", 0),
        "total_available": raw.get("total_available_jobs"),
        "stable_id_result": raw.get("stable_job_id_result"),
        "apply_url_result": raw.get("apply_url_result"),
        "pagination_result": _pagination(entry, raw),
        "anonymous_access_result": "valid" if safe else "not_validated",
        "authentication_result": "not_required" if safe else "not_validated",
        "session_initialization_result": "not_required" if safe else "not_validated",
        "browser_dependency_result": "not_required" if safe else "not_validated",
        "anti_bot_bypass_result": "not_used",
        "response_identity_result": "valid" if raw.get("parse_success") else "invalid",
        "duplicate_company_result": raw.get("duplicate_company_status"),
        "duplicate_source_result": raw.get("duplicate_source_status"),
        "status": status,
        "safe_to_activate": safe,
        "warnings": [str(warning)] if warning else [],
        "diagnostics": diagnostics,
        "recommended_action": (
            "Add to production registry in a separate activation change"
            if safe
            else f"Exclude from activation: {error or status}"
        ),
    }


def validate(
    *,
    timeout: int = validator.DEFAULT_TIMEOUT,
    batch_delay: float = validator.DEFAULT_BATCH_DELAY,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    candidate_bytes = build_candidate_registry()
    production_bytes = PRODUCTION.read_bytes()
    _, production_entries = validator.load_registry(PRODUCTION)
    _, candidate_entries = validator.load_registry(CANDIDATE_REGISTRY)
    additions = validator.detect_additions(production_entries, candidate_entries)
    if [entry["name"] for entry in additions] != EXPECTED_COMPANIES:
        raise ValueError("Candidate registry does not contain the exact ordered investigation set")
    duplicate_sources = validator._duplicate_owners(production_entries, additions)
    duplicate_companies = validator._duplicate_company_owners(production_entries, additions)
    production_adapters = {
        platform: scanner.ADAPTERS[platform]
        for platform in {str(entry["platform"]) for entry in additions}
    }

    raw_results: list[dict[str, Any]] = []
    for batch_start in range(0, len(additions), MAX_BATCH_SIZE):
        batch = additions[batch_start : batch_start + MAX_BATCH_SIZE]
        for offset, entry in enumerate(batch):
            index = batch_start + offset
            raw_results.append(
                validator.validate_company(
                    entry,
                    duplicate_of=duplicate_sources.get(index),
                    duplicate_company_of=duplicate_companies.get(index),
                    timeout=timeout,
                    sleep=sleep,
                    adapters=production_adapters,
                    max_normalized_jobs=MAX_JOBS,
                )
            )
        if batch_start + MAX_BATCH_SIZE < len(additions) and batch_delay > 0:
            sleep(batch_delay)

    results = [_report_result(entry, raw) for entry, raw in zip(additions, raw_results)]
    safe_names = {result["company"] for result in results if result["safe_to_activate"]}
    safe_additions = [entry for entry in additions if entry["name"] in safe_names]
    validated_bytes = validator.build_validated_registry_bytes(PRODUCTION, safe_additions)
    validator._write_bytes_if_changed(VALIDATED_REGISTRY, validated_bytes)
    load_target_companies(str(VALIDATED_REGISTRY))

    status_counts = Counter(result["status"] for result in results)
    report = {
        "production_registry_sha256": hashlib.sha256(production_bytes).hexdigest().upper(),
        "production_registry_entries": len(production_entries),
        "candidate_registry_entries": len(candidate_entries),
        "validated_registry_entries": len(yaml.safe_load(validated_bytes)["target_companies"]),
        "candidate_registry_has_exact_production_prefix": candidate_bytes.startswith(production_bytes),
        "batch_size": MAX_BATCH_SIZE,
        "maximum_pages_per_company": MAX_PAGES,
        "maximum_normalized_jobs_per_company": MAX_JOBS,
        "summary": {
            "companies_investigated": len(results),
            "status_counts": dict(sorted(status_counts.items())),
            "validated_with_jobs": [r["company"] for r in results if r["status"] == validator.VALIDATED_WITH_JOBS],
            "validated_zero_jobs": [r["company"] for r in results if r["status"] == validator.VALIDATED_ZERO_JOBS],
            "safe_to_activate": [r["company"] for r in results if r["safe_to_activate"]],
            "excluded": [
                {"company": r["company"], "status": r["status"], "reason": r["recommended_action"]}
                for r in results
                if not r["safe_to_activate"]
            ],
        },
        "results": results,
    }
    validator._write_json_if_changed(REPORT, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=validator.DEFAULT_TIMEOUT)
    parser.add_argument("--batch-delay", type=float, default=validator.DEFAULT_BATCH_DELAY)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.build_only:
        content = build_candidate_registry()
        print(json.dumps({"candidate_registry": str(CANDIDATE_REGISTRY), "bytes": len(content)}, indent=2))
        return 0
    print(json.dumps(validate(timeout=args.timeout, batch_delay=args.batch_delay), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
