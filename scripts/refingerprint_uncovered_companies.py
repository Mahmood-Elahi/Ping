#!/usr/bin/env python3
"""Re-fingerprint every original company not covered by production.

This is a research-only utility.  It makes bounded anonymous HTTP requests,
uses production adapter dispatch for read-only compatibility probes, and writes
only the v4 research artifacts.  It never imports persistence, dashboard,
publishing, or browser-automation modules.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import openpyxl
import requests
import yaml

from scripts.company_watchlist_common import (
    clean_text,
    normalize_company_name as compact_company_name,
    prohibited_job_board,
)
from scripts.investigate_unknown_companies import (
    Detection,
    PageAnalysis,
    ResearchClient,
    ResponseEvidence,
    _anti_bot,
    _domain_root,
    analyze_html,
    choose_relevant_scripts,
    detect_contract,
    request_url,
)
from src.scraper.company_careers import scanner as production_scanner
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import (
    ParentSourceMapping,
    load_parent_source_mappings,
)
from src.scraper.company_careers.registry import load_target_companies


MASTER_WORKBOOK = PROJECT_ROOT / "config" / "company_watchlist.xlsx"
PRODUCTION_REGISTRY = PROJECT_ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = PROJECT_ROOT / "config" / "company_parent_source_mappings.yaml"
INVENTORY_OUTPUT = PROJECT_ROOT / "config" / "company_uncovered_inventory_v4.csv"
FINGERPRINT_OUTPUT = PROJECT_ROOT / "config" / "company_uncovered_fingerprints_v4.csv"
CLUSTER_OUTPUT = PROJECT_ROOT / "config" / "company_uncovered_clusters_v4.csv"
BATCH_OUTPUT = PROJECT_ROOT / "config" / "company_recovery_batches_v4.csv"
NEXT_BATCH_OUTPUT = PROJECT_ROOT / "config" / "company_next_bulk_batch_v4.csv"
SUMMARY_OUTPUT = PROJECT_ROOT / "config" / "company_uncovered_summary_v4.md"
JSON_OUTPUT = PROJECT_ROOT / "output" / "uncovered_refingerprint_v4.json"

MAX_BATCH_SIZE = 25
MAX_PRIMARY_REQUESTS = 3
MAX_PRIMARY_BYTES = 3_000_000
MAX_COMPATIBILITY_PAGES = 2
MAX_COMPATIBILITY_JOBS = 20
MAX_RETRIES = 2
MAX_SCRIPTS = 10
MAX_SCRIPT_BYTES = 1_000_000

RECOVERABILITY_CLASSES = {
    "ready_existing_adapter",
    "ready_declarative_config",
    "adapter_extension_required",
    "reusable_new_adapter_candidate",
    "parent_mapping_ready",
    "parent_source_required",
    "browser_required",
    "session_required",
    "authentication_required",
    "blocked",
    "network_inaccessible",
    "stale_or_invalid",
    "unresolved",
}

EXISTING_ADAPTERS = {
    "workday", "greenhouse", "lever", "ashby", "eightfold", "phenom",
    "sap_successfactors", "oracle_recruiting", "avature", "icims",
    "static_html", "javascript_embedded_data",
}

UNSAFE_NEXT_BATCH_CLASSES = {
    "browser_required", "session_required", "authentication_required", "blocked",
    "network_inaccessible", "stale_or_invalid", "unresolved",
    "reusable_new_adapter_candidate", "parent_source_required",
}

INVENTORY_FIELDS = [
    "master_rank", "company", "normalized_company", "current_coverage_status",
    "current_canonical_source", "current_parent_mapping", "uncovered",
    "prior_classification", "prior_evidence_source", "current_official_careers_url",
    "redirect_chain", "current_platform", "recommended_family",
    "recoverability_class", "next_action", "evidence_confidence",
    "evidence_timestamp", "notes",
]

FINGERPRINT_FIELDS = [
    "master_rank", "company", "official_domain", "official_careers_url", "final_url",
    "redirect_chain", "http_status", "content_type", "detected_platform",
    "platform_variant", "ats_host", "tenant", "board", "site_id", "organization",
    "career_section", "public_endpoint", "endpoint_method", "stable_id_evidence",
    "apply_url_evidence", "allowed_apply_hosts", "pagination_evidence",
    "jobs_observed", "jobs_normalized", "adapter_compatibility_result",
    "parent_company", "parent_source_identity", "child_attribution_field",
    "child_attribution_values", "browser_evidence", "session_evidence",
    "authentication_evidence", "blocked_evidence", "stale_evidence",
    "recoverability_class", "recommended_family", "next_action",
    "evidence_confidence", "evidence_timestamp", "warnings", "diagnostics",
]

CLUSTER_FIELDS = [
    "cluster", "detected_platform", "recommended_family", "company_count", "companies",
    "current_adapter_available", "adapter_extension_required", "browser_required",
    "estimated_implementation_effort", "estimated_activation_yield",
    "recommended_priority", "rationale",
]

BATCH_FIELDS = [
    "batch_id", "batch_name", "recommended_order", "implementation_type",
    "candidate_count", "companies", "expected_safe_yield", "dependency", "risk",
    "estimated_effort", "exact_next_milestone", "rationale",
]

NEXT_BATCH_FIELDS = [
    "master_rank", "company", "final_classification", "recommended_adapter",
    "endpoint_identifiers", "source_identity", "parent_company", "mapping_rule",
    "validation_evidence", "confidence", "expected_implementation_work",
    "reason_selected",
]


def normalize_company_name(value: Any) -> str:
    """Readable exact-comparison key; never use it for containment matching."""
    text = clean_text(value).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def exact_company_match(left: Any, right: Any) -> bool:
    return bool(normalize_company_name(left)) and normalize_company_name(left) == normalize_company_name(right)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{key: clean_text(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: Sequence[str]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: clean_text(row.get(field)) for field in fields})
    atomic_write_text(path, "\ufeff" + buffer.getvalue())


def load_master_workbook(path: Path = MASTER_WORKBOOK) -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook["Companies"]
    headers = [clean_text(value) for value in next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True))]
    rows: list[dict[str, Any]] = []
    for values in worksheet.iter_rows(min_row=2, values_only=True):
        row = dict(zip(headers, values))
        company = clean_text(row.get("Company"))
        if not company:
            continue
        rows.append({
            "master_rank": int(row.get("Rank")),
            "company": company,
            "careers_url": clean_text(row.get("Careers URL")),
            "priority": clean_text(row.get("Priority")),
            "notes": clean_text(row.get("Notes")),
        })
    keys = [normalize_company_name(row["company"]) for row in rows]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ValueError("Master workbook contains missing or duplicate exact normalized company names")
    if [row["master_rank"] for row in rows] != sorted(row["master_rank"] for row in rows):
        raise ValueError("Master workbook is not in deterministic rank order")
    return rows


@dataclass
class Reconciliation:
    rows: list[dict[str, Any]]
    canonical_names: list[str]
    mapped_names: list[str]
    uncovered_names: list[str]
    production_only_names: list[str]

    @property
    def total(self) -> int:
        return len(self.rows)


def reconcile_master(
    master_rows: Sequence[Mapping[str, Any]],
    targets: Sequence[Any],
    mappings: Sequence[ParentSourceMapping],
) -> Reconciliation:
    master_by = {normalize_company_name(row["company"]): row for row in master_rows}
    target_by: dict[str, Any] = {}
    for target in targets:
        key = normalize_company_name(getattr(target, "name", ""))
        if not key or key in target_by:
            raise ValueError(f"Duplicate or missing production company: {getattr(target, 'name', '')}")
        target_by[key] = target
    enabled_mappings = {normalize_company_name(item.child_company): item for item in mappings if item.enabled}
    overlap = set(target_by).intersection(enabled_mappings)
    if overlap:
        raise ValueError(f"Mapped children are also independent targets: {sorted(overlap)}")
    result_rows: list[dict[str, Any]] = []
    canonical: list[str] = []
    mapped: list[str] = []
    uncovered: list[str] = []
    for source in master_rows:
        key = normalize_company_name(source["company"])
        target = target_by.get(key)
        mapping = enabled_mappings.get(key)
        if target is not None:
            status = "canonical_covered"
            canonical.append(source["company"])
        elif mapping is not None:
            if normalize_company_name(mapping.parent_company) not in target_by:
                raise ValueError(f"Mapped parent is not configured: {mapping.parent_company}")
            status = "mapped_covered"
            mapped.append(source["company"])
        else:
            status = "uncovered"
            uncovered.append(source["company"])
        result_rows.append({
            **dict(source),
            "normalized_company": key,
            "current_coverage_status": status,
            "current_canonical_source": (
                f"{target.platform}:{target.name}" if target is not None else ""
            ),
            "current_parent_mapping": (
                f"{mapping.parent_company}|{mapping.canonical_source}|{mapping.match_field}"
                if mapping is not None else ""
            ),
            "uncovered": str(status == "uncovered").lower(),
        })
    if len(result_rows) != len(master_by):
        raise AssertionError("Every original company must appear exactly once")
    production_only = [
        target.name for target in targets
        if normalize_company_name(target.name) not in master_by
    ]
    return Reconciliation(result_rows, canonical, mapped, uncovered, production_only)


def _prior_artifacts() -> list[Path]:
    named = [
        "company_platform_fingerprints_v2.csv", "company_remaining_inventory_v2.csv",
        "company_unknown_investigation_v3.csv", "company_unknown_clusters_v3.csv",
        "company_avature_icims_candidates.csv", "company_static_embedded_candidates.csv",
        "company_shared_api_candidates.csv", "company_existing_adapter_reuse_candidates_v3.csv",
        "company_parent_source_candidates_v2.csv", "company_unknown_parent_source_candidates_v3.csv",
    ]
    paths = [PROJECT_ROOT / "config" / name for name in named]
    paths.extend(sorted((PROJECT_ROOT / "config").glob("target_companies.*_candidates.yaml")))
    paths.extend(sorted((PROJECT_ROOT / "config").glob("target_companies.*_validated.yaml")))
    paths.extend(sorted((PROJECT_ROOT / "output").glob("*validation.json")))
    return [path for path in paths if path.exists()]


def load_prior_evidence() -> tuple[dict[str, dict[str, str]], list[str]]:
    """Merge prior rows by exact company key; later/current artifacts win fields."""
    merged: dict[str, dict[str, str]] = {}
    sources = [str(path.relative_to(PROJECT_ROOT)).replace("\\", "/") for path in _prior_artifacts()]
    priority_csvs = [
        PROJECT_ROOT / "config" / "company_remaining_inventory_v2.csv",
        PROJECT_ROOT / "config" / "company_platform_fingerprints_v2.csv",
        PROJECT_ROOT / "config" / "company_unknown_investigation_v3.csv",
        PROJECT_ROOT / "config" / "company_avature_icims_candidates.csv",
        PROJECT_ROOT / "config" / "company_static_embedded_candidates.csv",
        PROJECT_ROOT / "config" / "company_shared_api_candidates.csv",
        PROJECT_ROOT / "config" / "company_existing_adapter_reuse_candidates_v3.csv",
        PROJECT_ROOT / "config" / "company_parent_source_candidates_v2.csv",
        PROJECT_ROOT / "config" / "company_unknown_parent_source_candidates_v3.csv",
    ]
    for path in priority_csvs:
        if not path.exists():
            continue
        for row in read_csv(path):
            company = clean_text(row.get("company") or row.get("Company"))
            key = normalize_company_name(company)
            if not key:
                continue
            destination = merged.setdefault(key, {"company": company, "evidence_sources": ""})
            for field, value in row.items():
                value = clean_text(value)
                if value:
                    destination[field] = value
            existing_sources = [value for value in destination["evidence_sources"].split(";") if value]
            label = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
            if label not in existing_sources:
                existing_sources.append(label)
            destination["evidence_sources"] = ";".join(existing_sources)
    return merged, sources


def load_candidate_configs() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted((PROJECT_ROOT / "config").glob("target_companies.*.yaml")):
        if path == PRODUCTION_REGISTRY:
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in data.get("target_companies") or data.get("companies") or []:
            if isinstance(item, dict) and clean_text(item.get("name")):
                copy = dict(item)
                copy["_artifact"] = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
                result[normalize_company_name(item["name"])].append(copy)
    return result


def load_validation_results() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted((PROJECT_ROOT / "output").glob("*validation.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for collection in ("results", "canonical_candidates", "parent_mapping_candidates"):
            for row in payload.get(collection, []) if isinstance(payload, dict) else []:
                if not isinstance(row, dict):
                    continue
                company = clean_text(row.get("company") or row.get("child_company") or row.get("name"))
                if company:
                    copy = dict(row)
                    copy["_artifact"] = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
                    result[normalize_company_name(company)].append(copy)
    return result


@dataclass
class RequestBudget:
    company: str
    primary_requests: int = 0
    primary_bytes: int = 0
    scripts: int = 0
    compatibility_pages: int = 0
    compatibility_jobs: int = 0


class BoundedResearchClient(ResearchClient):
    """The existing anonymous client plus per-company v4 hard limits."""

    def __init__(self, **kwargs: Any) -> None:
        if int(kwargs.get("retries", 1)) > MAX_RETRIES:
            raise ValueError(f"retries cannot exceed {MAX_RETRIES}")
        super().__init__(**kwargs)
        self.budget = RequestBudget("")

    def begin_company(self, company: str) -> None:
        self.budget = RequestBudget(company)

    @staticmethod
    def _cache_key(url: str, method: str, json_body: Any) -> tuple[str, str, str]:
        normalized = request_url(url)
        body = json.dumps(json_body, sort_keys=True, separators=(",", ":")) if json_body is not None else ""
        return method.upper(), normalized, hashlib.sha256(body.encode()).hexdigest()

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: Any = None,
        max_bytes: Optional[int] = None,
        request_kind: str = "primary",
    ) -> ResponseEvidence:
        cache_key = self._cache_key(url, method, json_body)
        cached = cache_key in self.cache
        if request_kind == "primary" and not cached:
            if self.budget.primary_requests >= MAX_PRIMARY_REQUESTS:
                raise RuntimeError("primary request limit exceeded")
            remaining = MAX_PRIMARY_BYTES - self.budget.primary_bytes
            if remaining <= 0:
                raise RuntimeError("primary response byte limit exceeded")
            max_bytes = min(max_bytes or remaining, remaining)
            self.budget.primary_requests += 1
        elif request_kind == "script" and not cached:
            if self.budget.scripts >= MAX_SCRIPTS:
                raise RuntimeError("script-count limit exceeded")
            self.budget.scripts += 1
            max_bytes = min(max_bytes or MAX_SCRIPT_BYTES, MAX_SCRIPT_BYTES)
        evidence = super().fetch(url, method=method, json_body=json_body, max_bytes=max_bytes)
        if request_kind == "primary" and not cached:
            self.budget.primary_bytes += len(evidence.text.encode("utf-8"))
            if self.budget.primary_bytes > MAX_PRIMARY_BYTES:
                raise RuntimeError("primary response byte limit exceeded")
        if self.log:
            self.log[-1].setdefault("company", self.budget.company)
            self.log[-1].setdefault("request_kind", request_kind)
        return evidence


class CompatibilitySession:
    """Anonymous requests.Session facade capped at two production-parser pages."""

    def __init__(self, client: BoundedResearchClient, *, timeout: int = 15) -> None:
        self.client = client
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if self.client.budget.compatibility_pages >= MAX_COMPATIBILITY_PAGES:
            raise AdapterError("compatibility page limit exceeded")
        if prohibited_job_board(url):
            raise AdapterError("third-party aggregator is not an official source")
        self.client.budget.compatibility_pages += 1
        kwargs["timeout"] = min(int(kwargs.get("timeout") or self.timeout), 60)
        kwargs["allow_redirects"] = False
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("User-Agent", "JobRadarUncoveredResearch/4.0")
        kwargs["headers"] = headers
        response: Optional[requests.Response] = None
        error = ""
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == MAX_RETRIES:
                    break
                response.close()
            except requests.RequestException as exc:
                error = f"{exc.__class__.__name__}: {exc}"
        if response is None:
            raise AdapterError(error or "compatibility request failed")
        body = response.content
        if len(body) > MAX_SCRIPT_BYTES:
            response.close()
            raise AdapterError("compatibility response byte limit exceeded")
        self.client.log.append({
            "company": self.client.budget.company,
            "request_kind": "compatibility",
            "method": method.upper(),
            "requested_url": request_url(url),
            "final_url": request_url(response.url),
            "status": response.status_code,
            "bytes": len(body),
            "redirect_chain": [],
            "content_type": response.headers.get("Content-Type", ""),
            "error": "",
        })
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)


def _first(row: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        value = clean_text(row.get(field))
        if value:
            return value
    return ""


def _root_url(url: str) -> str:
    parsed = urlsplit(request_url(url))
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else ""


def official_seed_urls(master: Mapping[str, Any], prior: Mapping[str, Any]) -> list[str]:
    careers = _first(
        prior, "current_official_careers_url", "official_careers_url", "careers_url",
        "original_careers_url", "Official Careers URL",
    ) or clean_text(master.get("careers_url"))
    search = _first(
        prior, "current_job_search_url", "official_search_url", "search_url", "listing_url",
        "final_url", "Official Job Search URL",
    )
    endpoint = _first(prior, "endpoint_url", "public_search_endpoint", "api_url", "public_endpoint")
    candidates: list[str] = []
    primary_domain = _domain_root(careers or search or endpoint)
    alternative_domain = ""
    for value in (careers, search, endpoint, _root_url(careers or search or endpoint)):
        url = request_url(value)
        if not url or prohibited_job_board(url):
            continue
        domain = _domain_root(url)
        if primary_domain and domain != primary_domain:
            if alternative_domain and domain != alternative_domain:
                continue
            alternative_domain = domain
        if url not in candidates:
            candidates.append(url)
    return candidates[:MAX_PRIMARY_REQUESTS]


def _response_score(response: ResponseEvidence, analysis: PageAnalysis, detection: Detection) -> tuple[int, int, int, int]:
    return (
        int(response.status == 200),
        int(detection.family != "unknown"),
        analysis.jsonld_job_count + len(set(analysis.job_links)),
        len(response.text),
    )


def _parse_identifiers(detection: Detection) -> dict[str, str]:
    parsed = urlsplit(detection.endpoint_url)
    host = parsed.hostname or ""
    parts = [part for part in parsed.path.split("/") if part]
    result = {"tenant": "", "board": "", "site_id": "", "organization": "", "career_section": ""}
    if detection.family == "workday":
        host_match = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
        if host_match:
            result["tenant"] = host_match.group(1)
        if len(parts) >= 4 and parts[:2] == ["wday", "cxs"]:
            result["site_id"] = parts[3]
    elif detection.family == "greenhouse" and "boards" in parts:
        result["board"] = parts[parts.index("boards") + 1] if parts.index("boards") + 1 < len(parts) else ""
    elif detection.family in {"lever", "ashby"}:
        result["board"] = parts[-1] if parts else ""
    elif detection.family == "eightfold":
        result["organization"] = host.split(".")[0]
    query = dict(parse_qsl(parsed.query))
    result["career_section"] = query.get("careersection", "")
    return result


def _choose_candidate_config(company: str, family: str, configs: Mapping[str, list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    candidates = [item for item in configs.get(normalize_company_name(company), []) if clean_text(item.get("platform")) == family]
    if not candidates:
        return None
    return max(candidates, key=lambda item: sum(value not in (None, "", [], {}) for key, value in item.items() if not key.startswith("_")))


def _derived_target(company: str, detection: Detection) -> Optional[CompanyTarget]:
    identifiers = _parse_identifiers(detection)
    parsed = urlsplit(detection.endpoint_url)
    parts = [part for part in parsed.path.split("/") if part]
    data: dict[str, Any] = {
        "name": company, "platform": detection.family, "max_pages": MAX_COMPATIBILITY_PAGES,
        "max_jobs": MAX_COMPATIBILITY_JOBS, "page_size": 10,
    }
    if detection.family == "greenhouse" and identifiers["board"]:
        data["slug"] = identifiers["board"]
    elif detection.family == "lever" and parts:
        data["slug"] = parts[-1]
    elif detection.family == "ashby" and parts:
        data["slug"] = parts[-1]
    elif detection.family == "workday" and identifiers["tenant"] and identifiers["site_id"]:
        host_parts = (parsed.hostname or "").split(".")
        data.update({"tenant": identifiers["tenant"], "wd_subdomain": host_parts[1], "site_id": identifiers["site_id"]})
    else:
        return None
    return CompanyTarget.from_dict(data)


def compatibility_probe(
    company: str,
    detection: Detection,
    configs: Mapping[str, list[dict[str, Any]]],
    client: BoundedResearchClient,
) -> tuple[str, int, str]:
    if detection.family not in EXISTING_ADAPTERS:
        return "not_applicable", 0, ""
    adapter = production_scanner.ADAPTERS.get(detection.family)
    if adapter is None:
        return "production_dispatch_missing", 0, ""
    config = _choose_candidate_config(company, detection.family, configs)
    target = CompanyTarget.from_dict(config) if config is not None else _derived_target(company, detection)
    if target is None:
        return "unsupported_variant_missing_configuration", 0, ""
    target.max_pages = min(target.max_pages, MAX_COMPATIBILITY_PAGES)
    target.max_jobs = min(target.max_jobs, MAX_COMPATIBILITY_JOBS)
    target.page_size = min(target.page_size, MAX_COMPATIBILITY_JOBS)
    session = CompatibilitySession(client)
    try:
        jobs = adapter(session, target, 15)
    except Exception as exc:
        return f"unsupported_variant:{exc.__class__.__name__}:{clean_text(exc)[:240]}", 0, clean_text(config.get("_artifact")) if config else ""
    normalized = list(jobs)[:MAX_COMPATIBILITY_JOBS]
    client.budget.compatibility_jobs = len(normalized)
    if any(not job.job_id or not job.url for job in normalized):
        return "unsupported_variant_incomplete_normalized_job", len(normalized), clean_text(config.get("_artifact")) if config else ""
    status = "production_parser_compatible" if normalized else "production_parser_compatible_zero_jobs"
    return status, len(normalized), clean_text(config.get("_artifact")) if config else "derived current identifiers"


def _parent_evidence(
    company: str,
    validations: Mapping[str, list[dict[str, Any]]],
    configured_targets: set[str],
) -> dict[str, str]:
    values = validations.get(normalize_company_name(company), [])
    result = {"parent_company": "", "source": "", "field": "", "values": "", "status": ""}
    for row in values:
        parent = _first(row, "parent_company", "canonical_parent", "canonical_source_company")
        field_name = _first(row, "exact_metadata_field", "child_attribution_field", "match_field")
        field_values = _first(row, "exact_metadata_values", "child_attribution_values", "accepted_values")
        status = _first(row, "status", "validation_status")
        if parent:
            result["parent_company"] = parent
            result["source"] = _first(row, "parent_source_identity", "canonical_source", "parent_source_candidate")
        if field_name and field_values and status in {"validated_parent_mapping_with_current_jobs", "safe_with_metadata_mapping", "validated"}:
            result.update({"field": field_name, "values": field_values, "status": "ready"})
        elif status in {"weak_parent_attribution", "insufficient_filter_evidence", "stale_mapping", "invalid_configuration", "blocked"}:
            result["status"] = status
    if result["status"] == "ready":
        result["status"] = "parent_mapping_ready" if normalize_company_name(result["parent_company"]) in configured_targets else "parent_source_required"
    return result


def _classify(
    response: ResponseEvidence,
    all_responses: Sequence[ResponseEvidence],
    analysis: PageAnalysis,
    detection: Detection,
    compatibility: str,
    parent: Mapping[str, str],
    prior: Mapping[str, Any],
) -> tuple[str, str, str]:
    if parent.get("status") in {"parent_mapping_ready", "parent_source_required"}:
        value = clean_text(parent["status"])
        return value, "parent_company_source", (
            "Add a reviewed exact-field parent mapping" if value == "parent_mapping_ready"
            else "Configure and validate the canonical parent source before mapping"
        )
    if compatibility.startswith("production_parser_compatible"):
        if detection.family in {"static_html", "javascript_embedded_data"}:
            return "ready_declarative_config", f"existing_adapter_reuse/{detection.family}", "Prepare a declarative candidate entry and validate it without activation"
        return "ready_existing_adapter", f"existing_adapter_reuse/{detection.family}", "Prepare a bounded candidate-registry validation using the existing production adapter"
    compatibility_text = compatibility.casefold()
    if any(marker in compatibility_text for marker in ("authentication", "login required", "http 401")):
        return "authentication_required", "authentication_required", "Do not use private credentials; document the official access requirement"
    if any(marker in compatibility_text for marker in ("session required", "session_required", "csrf", "xsrf", "enable cookies")):
        return "session_required", "session_required", "Document the anonymous session-init contract before any implementation"
    if any(marker in compatibility_text for marker in ("blocked", "captcha", "http 403", "http 429")):
        return "blocked", "blocked", "Retain the bounded evidence and recheck manually; do not bypass controls"
    if (
        detection.family == "javascript_embedded_data"
        and compatibility.startswith("unsupported_variant")
        and not analysis.jsonld_job_count
        and not analysis.static_job_listing
        and detection.stable_id != "true"
    ):
        return "browser_required", "browser_required", "Defer to a separately authorized browser-support milestone"
    if detection.family in EXISTING_ADAPTERS and compatibility.startswith("unsupported_variant"):
        return "adapter_extension_required", f"existing_adapter_extension/{detection.family}", "Specify one reusable reviewed parser variant, then validate candidates"
    statuses = {item.status for item in all_responses}
    searchable = "\n".join(item.text[:200000] for item in all_responses)
    if 401 in statuses or re.search(r"login required|sign in to (?:view|continue)|authentication required", searchable, re.I):
        return "authentication_required", "authentication_required", "Do not use private credentials; document the official access requirement"
    session_marker = re.search(r"csrf|xsrf|session(?:id)? required|enable cookies", searchable, re.I)
    if session_marker and detection.method in {"POST", "GET/POST"} and not response.status:
        return "session_required", "session_required", "Document the anonymous session-init contract before any implementation"
    anti_bot = _anti_bot(response)
    if anti_bot or statuses.intersection({403, 429}):
        return "blocked", "blocked", "Retain the bounded evidence and recheck manually; do not bypass controls"
    if not any(item.status for item in all_responses):
        return "network_inaccessible", "network_inaccessible", "Retry the official source in a later bounded research milestone"
    if all(item.status in {404, 410} for item in all_responses if item.status):
        return "stale_or_invalid", "stale_source", "Recover a replacement only from the official corporate domain"
    prior_class = _first(prior, "primary_classification", "current_classification", "prior_classification")
    prior_text = " ".join(str(value) for value in prior.values())
    if re.search(r"acquired|renamed|ceased|inactive|shut down|discontinued", prior_text, re.I) and detection.family == "unknown":
        return "stale_or_invalid", "acquired_or_renamed", "Preserve the original brand identity and periodically recheck its official parent/owner"
    if analysis.js_shell or (detection.javascript_required == "true" and not detection.endpoint_url):
        return "browser_required", "browser_required", "Defer to a separately authorized browser-support milestone"
    if prior_class in {"invalid_careers_url", "stale_source_unresolved"} and response.status >= 400:
        return "stale_or_invalid", "stale_source", "Recover a replacement only from the official corporate domain"
    return "unresolved", detection.family if detection.family != "unknown" else "unresolved", "Perform a focused official-source follow-up without inferring an unsupported contract"


def investigate_company(
    master: Mapping[str, Any],
    prior: Mapping[str, Any],
    client: BoundedResearchClient,
    configs: Mapping[str, list[dict[str, Any]]],
    validations: Mapping[str, list[dict[str, Any]]],
    configured_targets: set[str],
    *,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    company = clean_text(master["company"])
    client.begin_company(company)
    seeds = official_seed_urls(master, prior)
    responses: list[ResponseEvidence] = []
    analyses: list[PageAnalysis] = []
    detections: list[Detection] = []
    warnings: list[str] = []
    for url in seeds:
        try:
            response = client.fetch(url, request_kind="primary")
        except (ValueError, RuntimeError) as exc:
            warnings.append(clean_text(exc))
            continue
        analysis = analyze_html(response.final_url or url, response.text)
        detection = detect_contract(response.final_url or url, response.text, analysis)
        responses.append(response)
        analyses.append(analysis)
        detections.append(detection)
    if responses:
        best_index = max(range(len(responses)), key=lambda index: _response_score(responses[index], analyses[index], detections[index]))
        response, analysis, detection = responses[best_index], analyses[best_index], detections[best_index]
    else:
        requested = seeds[0] if seeds else ""
        response, analysis, detection = ResponseEvidence(requested, error="no current official URL available"), PageAnalysis(), Detection()
        responses.append(response)
    scripts: list[dict[str, Any]] = []
    script_text: list[str] = []
    for script_url in choose_relevant_scripts(analysis.scripts, MAX_SCRIPTS):
        try:
            script_response = client.fetch(script_url, max_bytes=MAX_SCRIPT_BYTES, request_kind="script")
        except (ValueError, RuntimeError) as exc:
            warnings.append(clean_text(exc))
            continue
        scripts.append({
            "url": script_url, "status": script_response.status,
            "bytes": len(script_response.text.encode("utf-8")), "truncated": script_response.truncated,
            "error": script_response.error,
        })
        if script_response.status == 200:
            script_text.append(script_response.text)
    if script_text:
        combined = response.text + "\n" + "\n".join(script_text)
        analysis = analyze_html(response.final_url or response.requested_url, combined)
        detection = detect_contract(response.final_url or response.requested_url, combined, analysis)
    compatibility, jobs_normalized, config_source = compatibility_probe(company, detection, configs, client)
    parent = _parent_evidence(company, validations, configured_targets)
    recoverability, recommended_family, next_action = _classify(
        response, responses, analysis, detection, compatibility, parent, prior,
    )
    if recoverability not in RECOVERABILITY_CLASSES:
        raise AssertionError(f"Unexpected recoverability class: {recoverability}")
    identifiers = _parse_identifiers(detection)
    endpoint_host = urlsplit(detection.endpoint_url).hostname or ""
    final_url = response.final_url or response.requested_url
    timestamp = timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    apply_hosts = sorted({urlsplit(url).hostname or "" for url in analysis.apply_links if urlsplit(url).hostname})
    jobs_observed = min(MAX_COMPATIBILITY_JOBS, analysis.jsonld_job_count + len(set(analysis.job_links)))
    confidence = detection.confidence
    if compatibility.startswith("production_parser_compatible") or parent.get("status") in {"parent_mapping_ready", "parent_source_required"}:
        confidence = "high"
    elif recoverability in {"blocked", "network_inaccessible", "stale_or_invalid"} and response.status:
        confidence = "medium"
    diagnostics = {
        "primary_urls": seeds,
        "primary_requests": client.budget.primary_requests,
        "primary_bytes": client.budget.primary_bytes,
        "scripts_inspected": scripts,
        "compatibility_pages": client.budget.compatibility_pages,
        "compatibility_jobs": client.budget.compatibility_jobs,
        "config_source": config_source,
        "prior_evidence_sources": clean_text(prior.get("evidence_sources")),
    }
    return {
        "master_rank": str(master["master_rank"]),
        "company": company,
        "normalized_company": normalize_company_name(company),
        "official_domain": _root_url(seeds[0] if seeds else final_url),
        "official_careers_url": seeds[0] if seeds else "",
        "final_url": final_url,
        "redirect_chain": json.dumps(response.redirect_chain, ensure_ascii=False, sort_keys=True),
        "http_status": str(response.status) if response.status else "",
        "content_type": response.content_type,
        "detected_platform": detection.family if detection.family != "unknown" else recommended_family,
        "platform_variant": detection.variant,
        "ats_host": endpoint_host,
        **identifiers,
        "public_endpoint": detection.endpoint_url,
        "endpoint_method": detection.method,
        "stable_id_evidence": detection.stable_id,
        "apply_url_evidence": detection.apply_url,
        "allowed_apply_hosts": ";".join(apply_hosts),
        "pagination_evidence": detection.pagination,
        "jobs_observed": str(jobs_observed),
        "jobs_normalized": str(jobs_normalized),
        "adapter_compatibility_result": compatibility,
        "parent_company": clean_text(parent.get("parent_company")),
        "parent_source_identity": clean_text(parent.get("source")),
        "child_attribution_field": clean_text(parent.get("field")),
        "child_attribution_values": clean_text(parent.get("values")),
        "browser_evidence": "; ".join(analysis.evidence) if recoverability == "browser_required" else "",
        "session_evidence": "session/CSRF marker in public response" if recoverability == "session_required" else "",
        "authentication_evidence": "HTTP 401 or explicit login requirement" if recoverability == "authentication_required" else "",
        "blocked_evidence": _anti_bot(response) if recoverability == "blocked" else "",
        "stale_evidence": f"HTTP {response.status}; prior={_first(prior, 'primary_classification', 'current_classification')}" if recoverability == "stale_or_invalid" else "",
        "recoverability_class": recoverability,
        "recommended_family": recommended_family,
        "next_action": next_action,
        "evidence_confidence": confidence,
        "evidence_timestamp": timestamp,
        "warnings": "; ".join(dict.fromkeys([*warnings, *(item.error for item in responses if item.error)])),
        "diagnostics": json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
        "prior_classification": _first(prior, "primary_classification", "current_classification", "prior_classification"),
        "prior_evidence_source": clean_text(prior.get("evidence_sources")),
    }


def promote_reusable_clusters(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in records]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in result:
        if row["recoverability_class"] != "unresolved":
            continue
        family = clean_text(row.get("detected_platform"))
        endpoint = clean_text(row.get("public_endpoint"))
        if family in {"public_rest_api", "custom_rest_api", "public_graphql", "custom_graphql", "smartrecruiters", "adp", "talentbrew_radancy"} and endpoint:
            groups[(family, clean_text(row.get("platform_variant")))].append(row)
    for members in groups.values():
        if len(members) < 2:
            continue
        for row in members:
            row["recoverability_class"] = "reusable_new_adapter_candidate"
            row["recommended_family"] = f"reusable_new_adapter/{row['detected_platform']}"
            row["next_action"] = "Specify one shared public contract and validate this cluster before implementation"
    return result


def apply_review_rules(
    records: Sequence[Mapping[str, Any]],
    prior: Mapping[str, Mapping[str, Any]],
    validations: Mapping[str, list[dict[str, Any]]],
    configured_targets: set[str],
) -> list[dict[str, Any]]:
    """Apply conservative cross-artifact rules after current requests finish.

    Current HTTP evidence remains authoritative.  Prior artifacts may only make
    a result less actionable (for example, by exposing duplicate parent-source
    risk); they cannot promote a company into a ready class.
    """
    result = [dict(row) for row in records]
    actionable = {
        "ready_existing_adapter", "ready_declarative_config",
        "adapter_extension_required", "reusable_new_adapter_candidate",
    }
    for row in result:
        key = normalize_company_name(row.get("company"))
        previous = prior.get(key, {})
        row["prior_classification"] = _first(
            previous, "primary_classification", "current_classification", "prior_classification",
        )
        row["prior_evidence_source"] = clean_text(previous.get("evidence_sources"))
        if row.get("detected_platform") == "custom_rest_api":
            row["detected_platform"] = "public_rest_api"
        elif row.get("detected_platform") == "custom_graphql":
            row["detected_platform"] = "public_graphql"

        statuses = {
            _first(item, "status", "validation_status")
            for item in validations.get(key, [])
        }
        if "session_required" in statuses and row["recoverability_class"] not in {
            "ready_existing_adapter", "ready_declarative_config",
        }:
            row.update({
                "recoverability_class": "session_required",
                "recommended_family": "session_required",
                "next_action": "Document the anonymous session-init contract before any implementation",
                "session_evidence": "Current official source matches a bounded validation that required session initialization",
                "evidence_confidence": "high",
            })
        elif "browser_required" in statuses and row["recoverability_class"] == "unresolved":
            row.update({
                "recoverability_class": "browser_required",
                "recommended_family": "browser_required",
                "next_action": "Defer to a separately authorized browser-support milestone",
                "browser_evidence": "Current official source remains consistent with the reviewed browser-required contract",
                "evidence_confidence": "high",
            })

        parent = _parent_evidence(clean_text(row.get("company")), validations, configured_targets)
        parent_company = clean_text(parent.get("parent_company")) or _first(
            previous, "parent_company", "canonical_parent", "canonical_source_company",
        )
        parent_source = clean_text(parent.get("source")) or _first(
            previous, "parent_source_identity", "parent_source_candidate", "canonical_source",
        )
        if parent_company:
            row["parent_company"] = parent_company
            row["parent_source_identity"] = parent_source
        if parent.get("status") in {"parent_mapping_ready", "parent_source_required"}:
            row.update({
                "recoverability_class": parent["status"],
                "recommended_family": "parent_company_source",
                "child_attribution_field": clean_text(parent.get("field")),
                "child_attribution_values": clean_text(parent.get("values")),
                "next_action": (
                    "Add a reviewed exact-field parent mapping"
                    if parent["status"] == "parent_mapping_ready"
                    else "Configure and validate the canonical parent source before mapping"
                ),
                "evidence_confidence": "high",
            })
        elif parent_company and row["recoverability_class"] in actionable:
            row.update({
                "recoverability_class": "unresolved",
                "recommended_family": "parent_company_source/attribution_unproven",
                "next_action": "Validate exact structured child attribution on the canonical parent source; do not add an independent duplicate target",
                "child_attribution_field": "",
                "child_attribution_values": "",
                "evidence_confidence": "medium",
            })
    return result


def build_inventory(reconciliation: Reconciliation, fingerprints: Sequence[Mapping[str, Any]], prior: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_name = {normalize_company_name(row["company"]): row for row in fingerprints}
    result: list[dict[str, Any]] = []
    for source in reconciliation.rows:
        key = normalize_company_name(source["company"])
        finding = by_name.get(key, {})
        previous = prior.get(key, {})
        result.append({
            **source,
            "prior_classification": clean_text(finding.get("prior_classification") or _first(previous, "primary_classification", "current_classification")),
            "prior_evidence_source": clean_text(finding.get("prior_evidence_source") or previous.get("evidence_sources")),
            "current_official_careers_url": clean_text(finding.get("official_careers_url")),
            "redirect_chain": clean_text(finding.get("redirect_chain")),
            "current_platform": clean_text(finding.get("detected_platform")) if finding else clean_text(source.get("current_canonical_source")),
            "recommended_family": clean_text(finding.get("recommended_family")),
            "recoverability_class": clean_text(finding.get("recoverability_class")),
            "next_action": clean_text(finding.get("next_action")),
            "evidence_confidence": clean_text(finding.get("evidence_confidence")),
            "evidence_timestamp": clean_text(finding.get("evidence_timestamp")),
            "notes": clean_text(source.get("notes")),
        })
    return result


def build_clusters(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        groups[(
            clean_text(row["recoverability_class"]),
            clean_text(row["detected_platform"]),
            clean_text(row["recommended_family"]),
            clean_text(row.get("platform_variant")) or "default",
        )].append(row)
    class_order = {name: index for index, name in enumerate([
        "ready_existing_adapter", "ready_declarative_config", "parent_mapping_ready",
        "adapter_extension_required", "reusable_new_adapter_candidate", "parent_source_required",
        "browser_required", "session_required", "authentication_required", "blocked",
        "network_inaccessible", "stale_or_invalid", "unresolved",
    ], 1)}
    ranked_rows: list[tuple[int, dict[str, str]]] = []
    for (recoverability, platform, family, variant), members in groups.items():
        members = sorted(members, key=lambda row: int(row["master_rank"]))
        available = platform in EXISTING_ADAPTERS
        effort = "low" if recoverability in {"ready_existing_adapter", "ready_declarative_config", "parent_mapping_ready"} else "medium" if recoverability in {"adapter_extension_required", "reusable_new_adapter_candidate", "parent_source_required"} else "high"
        safe_yield = len(members) if recoverability not in UNSAFE_NEXT_BATCH_CLASSES else 0
        cluster_row = {
            "cluster": f"{recoverability}:{platform}:{variant}",
            "detected_platform": platform,
            "recommended_family": family,
            "company_count": str(len(members)),
            "companies": "; ".join(clean_text(row["company"]) for row in members),
            "current_adapter_available": str(available).lower(),
            "adapter_extension_required": str(recoverability == "adapter_extension_required").lower(),
            "browser_required": str(recoverability == "browser_required").lower(),
            "estimated_implementation_effort": effort,
            "estimated_activation_yield": str(safe_yield),
            "recommended_priority": str(class_order[recoverability]),
            "rationale": f"{len(members)} companies share the same current recoverability, platform variant, and implementation family.",
        }
        ranked_rows.append((min(int(row["master_rank"]) for row in members), cluster_row))
    ranked_rows.sort(key=lambda item: (int(item[1]["recommended_priority"]), item[0], item[1]["cluster"]))
    return [row for _, row in ranked_rows]


def pending_master_rows(
    uncovered_rows: Sequence[Mapping[str, Any]],
    completed: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    completed_keys = {
        normalize_company_name(row.get("company"))
        for row in completed
        if clean_text(row.get("recoverability_class")) in RECOVERABILITY_CLASSES
    }
    return [row for row in uncovered_rows if normalize_company_name(row.get("company")) not in completed_keys]


def build_recovery_batches(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    definitions = [
        ("ready_existing_adapter", "Existing adapter reuse", "existing_adapter_reuse", "Candidate registry validation", "low"),
        ("ready_declarative_config", "Declarative static and embedded", "declarative_static_embedded", "Declarative configuration review", "low"),
        ("parent_mapping_ready", "Exact parent mappings", "parent_source_expansion", "Parent mapping candidate validation", "low"),
        ("adapter_extension_required", "Reusable adapter extensions", "reusable_adapter_extensions", "Adapter variant specification and unit tests", "medium"),
        ("reusable_new_adapter_candidate", "Reusable new adapters", "reusable_new_adapters", "Shared contract design review", "medium"),
        ("parent_source_required", "Parent source expansion", "parent_source_expansion", "Canonical parent activation research", "medium"),
        ("browser_required", "Browser-required backlog", "browser_required", "Separately authorized browser milestone", "high"),
        ("session_required", "Session-required backlog", "session_required", "Anonymous session-contract research", "high"),
        ("authentication_required", "Authentication-required backlog", "authentication_required", "Access-policy review only", "high"),
        ("blocked", "Blocked-source rechecks", "access_constraint_followup", "Later bounded official-source recheck", "high"),
        ("network_inaccessible", "Network-inaccessible rechecks", "access_constraint_followup", "Later bounded network recheck", "high"),
        ("stale_or_invalid", "Stale and invalid source recovery", "unresolved_research_followup", "Official-domain URL recovery", "high"),
        ("unresolved", "Unresolved research follow-up", "unresolved_research_followup", "Focused official-source research", "high"),
    ]
    rows: list[dict[str, str]] = []
    for classification, name, implementation, milestone, risk in definitions:
        members = [row for row in records if row["recoverability_class"] == classification]
        if not members:
            continue
        members.sort(key=lambda row: int(row["master_rank"]))
        safe_yield = len(members) if classification not in UNSAFE_NEXT_BATCH_CLASSES else 0
        order = len(rows) + 1
        rows.append({
            "batch_id": f"v4-{order:02d}", "batch_name": name,
            "recommended_order": str(order), "implementation_type": implementation,
            "candidate_count": str(len(members)),
            "companies": "; ".join(clean_text(row["company"]) for row in members),
            "expected_safe_yield": str(safe_yield),
            "dependency": "Completed v4 evidence review",
            "risk": risk, "estimated_effort": "small" if risk == "low" else "medium" if risk == "medium" else "research",
            "exact_next_milestone": milestone,
            "rationale": f"Selected from exact final class {classification}; no activation occurs in this milestone.",
        })
    return rows


def build_next_batch(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    priority = {"ready_existing_adapter": 1, "ready_declarative_config": 2, "parent_mapping_ready": 3, "adapter_extension_required": 4}
    eligible = [row for row in records if row["recoverability_class"] in priority and row["evidence_confidence"] == "high"]
    if not eligible:
        return []
    best_class = min(priority[row["recoverability_class"]] for row in eligible)
    selected = [row for row in eligible if priority[row["recoverability_class"]] == best_class]
    selected.sort(key=lambda row: int(row["master_rank"]))
    rows: list[dict[str, str]] = []
    for row in selected:
        mapping_rule = ""
        if row["recoverability_class"] == "parent_mapping_ready":
            mapping_rule = f"{row['child_attribution_field']} exact {row['child_attribution_values']}"
        rows.append({
            "master_rank": clean_text(row["master_rank"]), "company": clean_text(row["company"]),
            "final_classification": clean_text(row["recoverability_class"]),
            "recommended_adapter": clean_text(row["recommended_family"]),
            "endpoint_identifiers": ";".join(f"{name}={clean_text(row.get(name))}" for name in ("tenant", "board", "site_id", "organization", "career_section") if clean_text(row.get(name))),
            "source_identity": clean_text(row.get("public_endpoint") or row.get("parent_source_identity")),
            "parent_company": clean_text(row.get("parent_company")), "mapping_rule": mapping_rule,
            "validation_evidence": f"{row['adapter_compatibility_result']}; jobs_normalized={row['jobs_normalized']}",
            "confidence": clean_text(row["evidence_confidence"]),
            "expected_implementation_work": clean_text(row["next_action"]),
            "reason_selected": "Highest-priority high-confidence safe final class in deterministic master order",
        })
    return rows


def _company_list(records: Sequence[Mapping[str, Any]], classification: str) -> str:
    names = [clean_text(row["company"]) for row in records if row["recoverability_class"] == classification]
    return ", ".join(names) if names else "None"


def classification_changed(row: Mapping[str, Any]) -> bool:
    prior = clean_text(row.get("prior_classification")).casefold()
    current = clean_text(row.get("recoverability_class")).casefold()
    platform = clean_text(row.get("detected_platform")).casefold()
    if not prior:
        return False
    direct = {
        "existing_adapter_reuse": "ready_existing_adapter",
        "existing_adapter_extension": "adapter_extension_required",
        "browser_required": "browser_required",
        "blocked": "blocked",
        "network_inaccessible": "network_inaccessible",
        "authentication_required": "authentication_required",
        "invalid_careers_url": "stale_or_invalid",
        "stale_source_unresolved": "stale_or_invalid",
        "still_unresolved": "unresolved",
        "unknown": "unresolved",
    }
    if direct.get(prior) == current:
        return False
    platform_aliases = {
        "custom_rest_api": "public_rest_api",
        "custom_graphql": "public_graphql",
    }
    prior_platform = platform_aliases.get(prior, prior)
    if prior_platform == platform:
        return False
    if prior == "parent_company_source" and clean_text(row.get("parent_company")):
        return False
    if prior == "newly_discovered_reusable_platform" and platform not in {
        "", "unresolved", "blocked", "browser_required", "network_inaccessible", "stale_source",
    }:
        return False
    return True


def build_summary(reconciliation: Reconciliation, records: Sequence[Mapping[str, Any]], next_batch: Sequence[Mapping[str, Any]]) -> str:
    platforms = Counter(clean_text(row["detected_platform"]) for row in records)
    classes = Counter(clean_text(row["recoverability_class"]) for row in records)
    changed = [row["company"] for row in records if classification_changed(row)]
    non_browser_recoverable = sum(classes[name] for name in ("ready_existing_adapter", "ready_declarative_config", "adapter_extension_required", "reusable_new_adapter_candidate", "parent_mapping_ready", "parent_source_required"))
    browser_count = classes["browser_required"]
    lines = [
        "# Uncovered company re-fingerprinting v4", "",
        "This is a research and planning artifact. No source was activated and no permanent coverage ceiling is claimed.", "",
        "## 1. Current reconciliation", "",
        f"- Original master companies: {reconciliation.total}",
        f"- Proof: {reconciliation.total} = {len(reconciliation.canonical_names)} + {len(reconciliation.mapped_names)} + {len(reconciliation.uncovered_names)}",
        "- Every original company occurs exactly once; canonical and mapped sets do not overlap.", "",
        "- Requested prior artifact not present in the repository: `config/company_unknown_discovery_v3.csv`. Existing v2/v3 inventories, candidates, validated registries, and validation reports were used.", "",
        "## 2. Exact uncovered count", "",
        str(len(reconciliation.uncovered_names)), "",
        "## 3. Covered canonical count", "",
        str(len(reconciliation.canonical_names)), "",
        "## 4. Covered mapped count", "",
        str(len(reconciliation.mapped_names)), "",
        "## 5. Production-only companies", "",
        f"{', '.join(reconciliation.production_only_names) or 'None'}", "",
        "## 6. Current platform distribution", "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in sorted(platforms.items()))
    lines.extend(["", "## 7. Recoverability distribution", ""])
    lines.extend(f"- {name}: {count}" for name, count in sorted(classes.items()))
    sections = [
        ("Ready existing-adapter candidates", "ready_existing_adapter"),
        ("Ready declarative-config candidates", "ready_declarative_config"),
        ("Adapter-extension candidates", "adapter_extension_required"),
        ("Reusable new-adapter clusters", "reusable_new_adapter_candidate"),
        ("Parent-mapping-ready candidates", "parent_mapping_ready"),
        ("Parent-source-required candidates", "parent_source_required"),
        ("Browser-required companies", "browser_required"),
    ]
    for index, (title, classification) in enumerate(sections, 8):
        lines.extend(["", f"## {index}. {title}", "", _company_list(records, classification)])
    lines.extend([
        "", "## 15. Session/authentication-required companies", "",
        f"- Session required: {_company_list(records, 'session_required')}",
        f"- Authentication required: {_company_list(records, 'authentication_required')}",
        "", "## 16. Blocked companies", "", _company_list(records, "blocked"),
        "", "## 17. Network-inaccessible companies", "", _company_list(records, "network_inaccessible"),
        "", "## 18. Stale or invalid companies", "", _company_list(records, "stale_or_invalid"),
        "", "## 19. Unresolved companies", "", _company_list(records, "unresolved"),
        "", "## 20. Recommended next bulk rollout", "",
        ", ".join(clean_text(row["company"]) for row in next_batch) if next_batch else "No high-confidence safe batch was proven.", "",
        "## 21. Estimated maximum non-browser coverage after current findings", "",
        f"Estimated additional non-browser recoverable findings: {non_browser_recoverable}. Estimated original-list coverage: {len(reconciliation.canonical_names) + len(reconciliation.mapped_names) + non_browser_recoverable} of {reconciliation.total}. Assumption: every listed ready/extension/new-adapter/parent finding passes a later implementation validation. This is not a permanent maximum or ceiling.", "",
        "## 22. Estimated coverage requiring browser support", "",
        f"{browser_count} additional companies currently appear browser-dependent. This estimate excludes blocked, session-required, authenticated, stale, inaccessible, and unresolved sources.", "",
        "## 23. Remaining strategically important custom-source targets", "",
        _company_list(records, "unresolved"), "",
        "## 24. Classifications changed from prior research", "",
        ", ".join(changed) if changed else "None", "",
        "All requests were anonymous and bounded. No JavaScript was executed; no browser automation, login, persistence, dashboard scan, Supabase, Google Sheets, or SQLite initialization occurred.", "",
    ])
    return "\n".join(lines)


def persist_all(
    reconciliation: Reconciliation,
    records: Sequence[Mapping[str, Any]],
    prior: Mapping[str, Mapping[str, Any]],
    client: BoundedResearchClient,
    prior_sources: Sequence[str],
    validations: Optional[Mapping[str, list[dict[str, Any]]]] = None,
    configured_targets: Optional[set[str]] = None,
) -> None:
    reviewed = apply_review_rules(
        records, prior, validations or {}, configured_targets or set(),
    )
    promoted = promote_reusable_clusters(reviewed)
    inventory = build_inventory(reconciliation, promoted, prior)
    clusters = build_clusters(promoted)
    batches = build_recovery_batches(promoted)
    next_batch = build_next_batch(promoted)
    atomic_write_csv(INVENTORY_OUTPUT, inventory, INVENTORY_FIELDS)
    atomic_write_csv(FINGERPRINT_OUTPUT, promoted, FINGERPRINT_FIELDS)
    atomic_write_csv(CLUSTER_OUTPUT, clusters, CLUSTER_FIELDS)
    atomic_write_csv(BATCH_OUTPUT, batches, BATCH_FIELDS)
    atomic_write_csv(NEXT_BATCH_OUTPUT, next_batch, NEXT_BATCH_FIELDS)
    atomic_write_text(SUMMARY_OUTPUT, build_summary(reconciliation, promoted, next_batch))
    payload = {
        "generated_at": max((clean_text(row.get("evidence_timestamp")) for row in promoted), default=""),
        "limits": {
            "batch_size": MAX_BATCH_SIZE, "primary_requests_per_company": MAX_PRIMARY_REQUESTS,
            "primary_bytes_per_company": MAX_PRIMARY_BYTES, "compatibility_pages_per_company": MAX_COMPATIBILITY_PAGES,
            "normalized_jobs_per_company": MAX_COMPATIBILITY_JOBS, "retries_per_failed_request": MAX_RETRIES,
            "script_references_per_company": MAX_SCRIPTS, "script_or_json_bytes": MAX_SCRIPT_BYTES,
        },
        "reconciliation": {
            "original": reconciliation.total, "canonical_covered": len(reconciliation.canonical_names),
            "mapped_covered": len(reconciliation.mapped_names), "uncovered": len(reconciliation.uncovered_names),
            "production_only": reconciliation.production_only_names,
        },
        "prior_artifacts": list(prior_sources),
        "requested_artifacts_not_present": [
            "config/company_unknown_discovery_v3.csv",
        ],
        "platform_distribution": dict(sorted(Counter(clean_text(row["detected_platform"]) for row in promoted).items())),
        "recoverability_distribution": dict(sorted(Counter(clean_text(row["recoverability_class"]) for row in promoted).items())),
        "companies": list(promoted), "requests": client.log,
    }
    atomic_write_text(JSON_OUTPUT, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def validate_completed(records: Sequence[Mapping[str, Any]], uncovered: Sequence[str]) -> None:
    expected = [normalize_company_name(name) for name in uncovered]
    observed = [normalize_company_name(row.get("company")) for row in records]
    if observed != expected[:len(observed)]:
        raise ValueError("Completed rows are not a deterministic prefix of the uncovered master order")
    if len(observed) != len(set(observed)):
        raise ValueError("Completed rows contain duplicate companies")
    for row in records:
        if clean_text(row.get("recoverability_class")) not in RECOVERABILITY_CLASSES:
            raise ValueError(f"Invalid completed recoverability class for {row.get('company')}")
        if not clean_text(row.get("next_action")):
            raise ValueError(f"Missing next action for {row.get('company')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--request-delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")
    if not 0 <= args.retries <= MAX_RETRIES:
        raise ValueError(f"retries must be between 0 and {MAX_RETRIES}")
    master = load_master_workbook()
    targets = load_target_companies(str(PRODUCTION_REGISTRY))
    mappings = load_parent_source_mappings(PARENT_MAPPINGS)
    reconciliation = reconcile_master(master, targets, mappings)
    prior, prior_sources = load_prior_evidence()
    if args.dry_run:
        print(json.dumps({
            "original": reconciliation.total,
            "canonical_covered": len(reconciliation.canonical_names),
            "mapped_covered": len(reconciliation.mapped_names),
            "uncovered": len(reconciliation.uncovered_names),
            "production_only": reconciliation.production_only_names,
        }, indent=2))
        return 0
    configs = load_candidate_configs()
    validations = load_validation_results()
    target_names = {normalize_company_name(target.name) for target in targets}
    uncovered_rows = [row for row in master if row["company"] in set(reconciliation.uncovered_names)]
    completed = read_csv(FINGERPRINT_OUTPUT) if args.resume else []
    validate_completed(completed, reconciliation.uncovered_names)
    by_name = {normalize_company_name(row["company"]): dict(row) for row in completed}
    pending = list(pending_master_rows(uncovered_rows, completed))
    if args.limit is not None:
        pending = pending[:args.limit]
    client = BoundedResearchClient(
        request_delay=args.request_delay, timeout=args.timeout, retries=args.retries,
        max_redirects=5, max_response_bytes=MAX_PRIMARY_BYTES,
    )
    if args.resume and JSON_OUTPUT.exists():
        try:
            old = json.loads(JSON_OUTPUT.read_text(encoding="utf-8"))
            if isinstance(old.get("requests"), list):
                client.log.extend(item for item in old["requests"] if isinstance(item, dict))
        except (OSError, ValueError, AttributeError):
            pass
    for batch_offset in range(0, len(pending), args.batch_size):
        batch = pending[batch_offset:batch_offset + args.batch_size]
        for source in batch:
            company = source["company"]
            print(f"Re-fingerprinting {company}...", flush=True)
            record = investigate_company(
                source, prior.get(normalize_company_name(company), {}), client,
                configs, validations, target_names,
            )
            by_name[normalize_company_name(company)] = record
            ordered = [by_name[normalize_company_name(name)] for name in reconciliation.uncovered_names if normalize_company_name(name) in by_name]
            persist_all(
                reconciliation, ordered, prior, client, prior_sources,
                validations, target_names,
            )
            print(f"Saved {len(ordered)}/{len(reconciliation.uncovered_names)}", flush=True)
    if not pending:
        ordered = [by_name[normalize_company_name(name)] for name in reconciliation.uncovered_names if normalize_company_name(name) in by_name]
        persist_all(
            reconciliation, ordered, prior, client, prior_sources,
            validations, target_names,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
