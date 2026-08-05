"""Build and anonymously validate the v4 Workday/Lever recovery batch.

Only the two candidate registries, the reviewed CSV inventory, and the ignored
JSON report are written. Production scanning, persistence, and publishing are
deliberately outside this module's import graph and execution path.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MANIFEST = ROOT / "config" / "company_next_bulk_batch_v4.csv"
UNCOVERED_INVENTORY = ROOT / "config" / "company_uncovered_inventory_v4.csv"
FINGERPRINTS = ROOT / "config" / "company_uncovered_fingerprints_v4.csv"
INVENTORY = ROOT / "config" / "company_existing_adapter_v4_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.existing_adapter_v4_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.existing_adapter_v4_validated.yaml"
REPORT = ROOT / "output" / "existing_adapter_v4_validation.json"

HISTORICAL_PRODUCTION_COUNT = 127
HISTORICAL_ORIGINAL_COVERAGE = 122
EXPECTED_PARENT_MAPPINGS = 4
MAX_BATCH_SIZE = 5
MAX_PAGES = 2
MAX_JOBS = 20
SAFE_STATUSES = {validator.VALIDATED_WITH_JOBS, validator.VALIDATED_ZERO_JOBS}
TEMPORARY_STATUSES = {"temporary_failure", "network_inaccessible", "failed_technical"}

INVENTORY_FIELDS = [
    "master_rank", "company", "expected_platform", "current_platform",
    "careers_url", "listing_url", "tenant", "site_id", "board",
    "organization", "source_identity", "endpoint_variant",
    "allowed_source_hosts", "allowed_apply_hosts", "stable_id_contract",
    "pagination_contract", "prior_evidence_source", "current_evidence_source",
    "evidence_confidence", "evidence_timestamp", "validation_status",
    "safe_to_activate", "warnings", "exclusion_reason",
]


def _workday(
    rank: int,
    name: str,
    careers_url: str,
    tenant: str,
    subdomain: str,
    site_id: str,
) -> dict[str, Any]:
    host = f"{tenant}.{subdomain}.myworkdayjobs.com"
    return {
        "name": name,
        "platform": "workday",
        "tenant": tenant,
        "wd_subdomain": subdomain,
        "site_id": site_id,
        "careers_url": careers_url,
        "search_url": f"https://{host}/{site_id}",
        "endpoint_variant": "wday_cxs_v1",
        "original_workbook_rank": rank,
        "allowed_apply_hosts": [host],
        "max_pages": MAX_PAGES,
        "max_jobs": MAX_JOBS,
    }


def _lever(
    rank: int,
    name: str,
    careers_url: str,
    board: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": "lever",
        "slug": board,
        "careers_url": careers_url,
        "search_url": f"https://jobs.lever.co/{board}",
        "endpoint_variant": "postings_api_v0",
        "original_workbook_rank": rank,
        "allowed_apply_hosts": ["jobs.lever.co"],
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
    }


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _workday(19, "Intel", "https://www.intel.com/content/www/us/en/jobs/jobs-at-intel.html", "intel", "wd1", "External"),
    _workday(32, "NXP Semiconductors", "https://www.nxp.com/company/about-nxp/careers:CAREERS", "nxp", "wd3", "careers"),
    _workday(41, "KLA", "https://www.kla.com/careers", "kla", "wd1", "Search"),
    _workday(89, "Capital One", "https://www.capitalonecareers.com/search-jobs", "capitalone", "wd12", "Capital_One"),
    _workday(135, "Unity", "https://unity.com/careers/positions", "unitytech", "wd1", "Unity"),
    _lever(195, "Loft Orbital", "https://loftorbital.com/careers/", "loftorbital"),
]
EXPECTED_COMPANIES = [entry["name"] for entry in CANDIDATE_ENTRIES]


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _indexed_rows(path: Path) -> dict[str, dict[str, str]]:
    return {row["company"]: row for row in _csv_rows(path)}


def reconcile_candidates() -> None:
    manifest = [
        row for row in _csv_rows(MANIFEST)
        if row.get("final_classification") == "ready_existing_adapter"
    ]
    manifest_names = [row["company"] for row in manifest]
    if manifest_names != EXPECTED_COMPANIES:
        raise ValueError(
            "The v4 manifest does not contain the exact ordered six-company batch: "
            f"expected={EXPECTED_COMPANIES!r}, found={manifest_names!r}"
        )

    fingerprints = _indexed_rows(FINGERPRINTS)
    uncovered = _indexed_rows(UNCOVERED_INVENTORY)
    for entry, manifest_row in zip(CANDIDATE_ENTRIES, manifest):
        name = entry["name"]
        fingerprint = fingerprints.get(name)
        inventory = uncovered.get(name)
        if not fingerprint or not inventory:
            raise ValueError(f"{name}: missing v4 evidence")
        expected_platform = manifest_row["recommended_adapter"].rsplit("/", 1)[-1]
        if expected_platform != entry["platform"]:
            raise ValueError(f"{name}: configured platform differs from the v4 manifest")
        if fingerprint.get("detected_platform") != entry["platform"]:
            raise ValueError(f"{name}: configured platform differs from the v4 fingerprint")
        if fingerprint.get("public_endpoint") != validator.endpoint_for(entry):
            raise ValueError(f"{name}: configured endpoint differs from the v4 fingerprint")
        if inventory.get("uncovered", "").casefold() != "true":
            raise ValueError(f"{name}: candidate is no longer uncovered in the v4 inventory")
        error = validator.configuration_error(entry)
        if error:
            raise ValueError(f"{name}: {error}")
        if not 1 <= int(entry["max_pages"]) <= MAX_PAGES:
            raise ValueError(f"{name}: max_pages exceeds {MAX_PAGES}")
        if int(entry["max_jobs"]) != MAX_JOBS:
            raise ValueError(f"{name}: max_jobs must equal {MAX_JOBS}")

    ranks = [int(entry["original_workbook_rank"]) for entry in CANDIDATE_ENTRIES]
    if ranks != sorted(ranks):
        raise ValueError("Candidate entries do not follow master-workbook order")


def _mapped_company_names() -> set[str]:
    return {
        " ".join(mapping.child_company.casefold().split())
        for mapping in load_parent_source_mappings(PARENT_MAPPINGS)
    }


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
) -> bytes:
    reconcile_candidates()
    _, production_entries = validator.load_registry(production)
    if len(production_entries) != HISTORICAL_PRODUCTION_COUNT:
        raise ValueError("Production is not the 127-entry v4 historical baseline")
    mapped = _mapped_company_names()
    mapped_candidates = [
        entry["name"] for entry in CANDIDATE_ENTRIES
        if validator._name_key(entry) in mapped
    ]
    if mapped_candidates:
        raise ValueError(f"Mapped children cannot be canonical candidates: {mapped_candidates}")
    duplicate_names = validator._duplicate_company_owners(production_entries, CANDIDATE_ENTRIES)
    duplicate_sources = validator._duplicate_owners(production_entries, CANDIDATE_ENTRIES)
    if duplicate_names or duplicate_sources:
        raise ValueError(
            f"Candidate registry duplicates: companies={duplicate_names}, sources={duplicate_sources}"
        )
    content = validator.build_candidate_registry_bytes(production, CANDIDATE_ENTRIES)
    validator._write_bytes_if_changed(destination, content)
    if not content.startswith(production.read_bytes()):
        raise ValueError("Candidate registry does not preserve the production byte prefix")
    if len(load_target_companies(str(destination))) != HISTORICAL_PRODUCTION_COUNT + len(CANDIDATE_ENTRIES):
        raise ValueError("Candidate registry is not loader-compatible at the expected count")
    return content


class JsonResponseView:
    """A response-shaped immutable JSON view used for safe pagination stops."""

    def __init__(self, response: Any, payload: Any) -> None:
        self.status_code = getattr(response, "status_code", 200)
        self.url = getattr(response, "url", "")
        self.history = list(getattr(response, "history", []))
        self.headers = dict(getattr(response, "headers", {}))
        self._payload = payload

    def json(self) -> Any:
        return copy.deepcopy(self._payload)


class CachedBoundedSession:
    """Anonymous request cache plus Workday repeated/no-new-page termination."""

    def __init__(self, session: Optional[Any] = None, *, max_pages: int = MAX_PAGES) -> None:
        self.session = session or requests.Session()
        self.max_pages = min(MAX_PAGES, max(1, max_pages))
        self.cache: dict[str, Any] = {}
        self.cache_hits = 0
        self.network_requests = 0
        self.logical_page_offsets: list[int] = []
        self.repeated_page_detected = False
        self.no_new_stable_id_detected = False
        self.page_limit_reached = False
        self._page_signatures: set[str] = set()
        self._stable_ids: set[str] = set()

    @staticmethod
    def _cache_key(method: str, url: str, kwargs: Mapping[str, Any]) -> str:
        material = {
            "method": method,
            "url": url,
            "json": kwargs.get("json"),
            "params": kwargs.get("params"),
        }
        return json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))

    @staticmethod
    def _empty_workday(response: Any) -> JsonResponseView:
        try:
            payload = response.json()
        except (AttributeError, ValueError, requests.RequestException):
            payload = {}
        empty = dict(payload) if isinstance(payload, Mapping) else {}
        empty["jobPostings"] = []
        return JsonResponseView(response, empty)

    @staticmethod
    def _workday_ids(postings: Iterable[Mapping[str, Any]]) -> set[str]:
        identities: set[str] = set()
        for item in postings:
            bullets = item.get("bulletFields") or []
            identity = str(bullets[0]).strip() if bullets else ""
            if not identity:
                identity = str(item.get("externalPath") or "").rstrip("/").rsplit("/", 1)[-1]
            if identity:
                identities.add(identity.casefold())
        return identities

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        key = self._cache_key(method, url, kwargs)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]

        body = kwargs.get("json")
        is_workday = method == "post" and "/wday/cxs/" in url and isinstance(body, Mapping)
        offset = int(body.get("offset") or 0) if is_workday else 0
        if is_workday and offset not in self.logical_page_offsets:
            if len(self.logical_page_offsets) >= self.max_pages:
                self.page_limit_reached = True
                return JsonResponseView(type("Limit", (), {"status_code": 200, "url": url})(), {"jobPostings": []})
            self.logical_page_offsets.append(offset)

        self.network_requests += 1
        response = getattr(self.session, method)(url, **kwargs)
        if getattr(response, "status_code", None) == 200:
            self.cache[key] = response

        if not is_workday or getattr(response, "status_code", None) != 200:
            return response
        try:
            payload = response.json()
        except (AttributeError, ValueError, requests.RequestException):
            return response
        postings = payload.get("jobPostings") if isinstance(payload, Mapping) else None
        if not isinstance(postings, list) or not postings:
            return response
        signature = json.dumps(postings, sort_keys=True, default=str, separators=(",", ":"))
        identities = self._workday_ids(item for item in postings if isinstance(item, Mapping))
        if offset > 0 and signature in self._page_signatures:
            self.repeated_page_detected = True
            return self._empty_workday(response)
        if offset > 0 and identities and not (identities - self._stable_ids):
            self.no_new_stable_id_detected = True
            return self._empty_workday(response)
        self._page_signatures.add(signature)
        self._stable_ids.update(identities)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("post", url, **kwargs)


def _review_status(raw: Mapping[str, Any]) -> str:
    status = str(raw.get("validation_status") or "failed_technical")
    error = str(raw.get("error") or "").casefold()
    if status in SAFE_STATUSES:
        return status
    if status == validator.STALE_FINGERPRINT:
        return "stale_source"
    if status == validator.TEMPORARY_FAILURE:
        return "network_inaccessible"
    if status == validator.UNSUPPORTED_RESPONSE:
        if "stable job_id" in error or "duplicates stable" in error:
            return "missing_stable_id"
        if "apply host" in error:
            return "invalid_apply_host"
        if "valid apply url" in error:
            return "invalid_apply_url"
        return "malformed_response"
    return status


def _stop_reason(entry: Mapping[str, Any], raw: Mapping[str, Any], session: CachedBoundedSession) -> str:
    if raw.get("validation_status") == validator.VALIDATED_ZERO_JOBS:
        return "validated_zero_jobs"
    if session.repeated_page_detected:
        return "repeated_page"
    if session.no_new_stable_id_detected:
        return "no_new_stable_ids"
    if entry["platform"] == "lever":
        return "single_public_collection"
    if len(session.logical_page_offsets) >= min(MAX_PAGES, int(entry.get("max_pages") or 1)):
        return "maximum_pages_reached"
    return "short_or_empty_page"


def _report_result(
    entry: Mapping[str, Any],
    raw: Mapping[str, Any],
    session: CachedBoundedSession,
) -> dict[str, Any]:
    status = _review_status(raw)
    safe = status in SAFE_STATUSES
    endpoint = validator.endpoint_for(entry) or ""
    pages = len(session.logical_page_offsets) if entry["platform"] == "workday" else int(bool(session.network_requests))
    warning = str(raw.get("warning") or "").strip()
    error = str(raw.get("error") or "").strip()
    return {
        "company": entry["name"],
        "master_rank": entry.get("original_workbook_rank"),
        "expected_platform": entry["platform"],
        "detected_current_platform": entry["platform"] if raw.get("parse_success") else None,
        "endpoint_identifiers": validator.configured_identifiers(entry),
        "endpoint": endpoint,
        "source_identity": validator.source_key(entry),
        "locale_behavior_result": (
            "valid_en-US_detail_path"
            if entry["platform"] == "workday" and "/en-US/" in str(raw.get("sample_apply_url") or "")
            else ("valid_empty_search_schema" if entry["platform"] == "workday" and status == validator.VALIDATED_ZERO_JOBS else "not_applicable")
        ),
        "pages_requested": pages,
        "jobs_observed": int(raw.get("jobs_returned") or 0),
        "normalized_jobs_inspected": int(raw.get("jobs_normalized") or 0),
        "stable_id_result": raw.get("stable_job_id_result"),
        "apply_url_result": raw.get("apply_url_result"),
        "allowed_host_result": (
            "valid" if raw.get("apply_url_result") in {"valid", "not_applicable_zero_jobs"} else "invalid"
        ),
        "pagination_result": {
            "deterministic": pages <= MAX_PAGES,
            "offsets": list(session.logical_page_offsets),
            "maximum_pages": min(MAX_PAGES, int(entry.get("max_pages") or 1)),
            "stop_reason": _stop_reason(entry, raw, session),
        },
        "repeated_page_result": "terminated" if session.repeated_page_detected else "not_detected",
        "no_new_id_result": "terminated" if session.no_new_stable_id_detected else "not_detected",
        "valid_empty_result": status == validator.VALIDATED_ZERO_JOBS,
        "authentication_check": "not_required" if safe else ("rejected" if status == validator.AUTHENTICATION_REQUIRED else "not_detected"),
        "session_check": "not_required" if safe else ("rejected" if status == validator.SESSION_REQUIRED else "not_detected"),
        "browser_check": "not_required" if safe else ("rejected" if status == validator.BROWSER_REQUIRED else "not_detected"),
        "malformed_response_check": "accepted_valid_schema" if raw.get("parse_success") else "rejected_or_failed",
        "duplicate_company_result": raw.get("duplicate_company_status", "unique"),
        "duplicate_source_result": raw.get("duplicate_source_status", "unique"),
        "status": status,
        "safe_to_activate": safe,
        "warnings": [warning] if warning else [],
        "diagnostics": {
            "http_status": raw.get("http_status"),
            "network_attempts": raw.get("attempts"),
            "network_requests": session.network_requests,
            "request_cache_hits": session.cache_hits,
            "endpoint_tested": raw.get("endpoint_tested") or endpoint,
            "error": error or None,
        },
    }


def validate_candidate(
    entry: Mapping[str, Any],
    *,
    duplicate_of: Optional[str] = None,
    duplicate_company_of: Optional[str] = None,
    timeout: int = validator.DEFAULT_TIMEOUT,
    attempts: int = validator.DEFAULT_ATTEMPTS,
    retry_delay: float = validator.DEFAULT_RETRY_DELAY,
    session_factory: Any = requests.Session,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    sessions: list[CachedBoundedSession] = []

    def bounded_factory() -> CachedBoundedSession:
        bounded = CachedBoundedSession(session_factory(), max_pages=int(entry.get("max_pages") or 1))
        sessions.append(bounded)
        return bounded

    raw = validator.validate_company(
        entry,
        duplicate_of=duplicate_of,
        duplicate_company_of=duplicate_company_of,
        timeout=timeout,
        attempts=attempts,
        retry_delay=retry_delay,
        session_factory=bounded_factory,
        sleep=sleep,
        adapters={str(entry["platform"]): scanner.ADAPTERS[str(entry["platform"])]},
        max_normalized_jobs=MAX_JOBS,
        max_pages_limit=MAX_PAGES,
    )
    bounded = sessions[0] if sessions else CachedBoundedSession(type("Unused", (), {})())
    return _report_result(entry, raw, bounded)


def _inventory_rows(results: Optional[Iterable[Mapping[str, Any]]] = None) -> list[dict[str, Any]]:
    manifest = _indexed_rows(MANIFEST)
    uncovered = _indexed_rows(UNCOVERED_INVENTORY)
    fingerprints = _indexed_rows(FINGERPRINTS)
    result_by_name = {str(row["company"]): row for row in (results or [])}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    for entry in CANDIDATE_ENTRIES:
        name = str(entry["name"])
        prior = uncovered[name]
        fingerprint = fingerprints[name]
        result = result_by_name.get(name)
        platform = str(entry["platform"])
        endpoint = validator.endpoint_for(entry) or ""
        host = urlparse(endpoint).hostname or ""
        status = str(result["status"]) if result else "pending_validation"
        safe = bool(result and result["safe_to_activate"])
        warnings = ";".join(result.get("warnings", [])) if result else ""
        exclusion = "" if safe or not result else str(result.get("diagnostics", {}).get("error") or status)
        rows.append({
            "master_rank": entry["original_workbook_rank"],
            "company": name,
            "expected_platform": manifest[name]["recommended_adapter"].rsplit("/", 1)[-1],
            "current_platform": platform if result is None or result.get("detected_current_platform") else "unconfirmed",
            "careers_url": entry["careers_url"],
            "listing_url": entry["search_url"],
            "tenant": entry.get("tenant", ""),
            "site_id": entry.get("site_id", ""),
            "board": entry.get("slug", "") if platform == "lever" else "",
            "organization": "",
            "source_identity": endpoint,
            "endpoint_variant": entry["endpoint_variant"],
            "allowed_source_hosts": host,
            "allowed_apply_hosts": ";".join(entry["allowed_apply_hosts"]),
            "stable_id_contract": (
                "bulletFields[0] external requisition ID; externalPath terminal ID fallback"
                if platform == "workday" else "Lever posting id"
            ),
            "pagination_contract": (
                "POST limit=20 with deterministic offsets 0,20; stop on empty, repeated page, no new stable IDs, or two pages"
                if platform == "workday" else "single anonymous public JSON collection; inspect at most 20 normalized jobs"
            ),
            "prior_evidence_source": prior.get("prior_evidence_source", ""),
            "current_evidence_source": f"{fingerprint.get('public_endpoint', endpoint)};{entry['careers_url']}",
            "evidence_confidence": fingerprint.get("evidence_confidence", "high"),
            "evidence_timestamp": now if result else fingerprint.get("evidence_timestamp", ""),
            "validation_status": status,
            "safe_to_activate": str(safe).lower(),
            "warnings": warnings,
            "exclusion_reason": exclusion,
        })
    return rows


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    rows = _inventory_rows(results)
    from io import StringIO
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=INVENTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    validator._write_bytes_if_changed(INVENTORY, buffer.getvalue().encode("utf-8"))


def validate(
    *,
    timeout: int = validator.DEFAULT_TIMEOUT,
    attempts: int = validator.DEFAULT_ATTEMPTS,
    retry_delay: float = validator.DEFAULT_RETRY_DELAY,
    batch_delay: float = validator.DEFAULT_BATCH_DELAY,
    session_factory: Any = requests.Session,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    production_before = PRODUCTION.read_bytes()
    mappings_before = PARENT_MAPPINGS.read_bytes()
    candidate_bytes = build_candidate_registry()
    _, production_entries = validator.load_registry(PRODUCTION)
    _, candidate_entries = validator.load_registry(CANDIDATE_REGISTRY)
    additions = validator.detect_additions(production_entries, candidate_entries)
    if [entry["name"] for entry in additions] != EXPECTED_COMPANIES:
        raise ValueError("Candidate registry does not contain the exact ordered v4 batch")

    duplicate_sources = validator._duplicate_owners(production_entries, additions)
    duplicate_companies = validator._duplicate_company_owners(production_entries, additions)
    results: list[dict[str, Any]] = []
    for batch_start in range(0, len(additions), MAX_BATCH_SIZE):
        batch = additions[batch_start : batch_start + MAX_BATCH_SIZE]
        for offset, entry in enumerate(batch):
            index = batch_start + offset
            results.append(validate_candidate(
                entry,
                duplicate_of=duplicate_sources.get(index),
                duplicate_company_of=duplicate_companies.get(index),
                timeout=timeout,
                attempts=attempts,
                retry_delay=retry_delay,
                session_factory=session_factory,
                sleep=sleep,
            ))
        if batch_start + MAX_BATCH_SIZE < len(additions) and batch_delay > 0:
            sleep(batch_delay)

    safe_names = {row["company"] for row in results if row["safe_to_activate"]}
    safe_additions = [entry for entry in additions if entry["name"] in safe_names]
    validated_bytes = validator.build_validated_registry_bytes(PRODUCTION, safe_additions)
    validator._write_bytes_if_changed(VALIDATED_REGISTRY, validated_bytes)
    validated_targets = load_target_companies(str(VALIDATED_REGISTRY))
    _write_inventory(results)

    counts = Counter(str(row["status"]) for row in results)
    safe_count = len(safe_additions)
    totals = {
        "reviewed_candidates": len(results),
        "workday_candidates": sum(row["expected_platform"] == "workday" for row in results),
        "lever_candidates": sum(row["expected_platform"] == "lever" for row in results),
        "reclassified_candidates": sum(row["expected_platform"] != row["detected_current_platform"] for row in results),
        "validated_with_jobs": counts[validator.VALIDATED_WITH_JOBS],
        "validated_zero_jobs": counts[validator.VALIDATED_ZERO_JOBS],
        "excluded": sum(not row["safe_to_activate"] for row in results),
        "temporary_failures": sum(row["status"] in TEMPORARY_STATUSES for row in results),
        "candidate_registry_count": len(candidate_entries),
        "validated_registry_count": len(validated_targets),
        "projected_original_company_coverage": HISTORICAL_ORIGINAL_COVERAGE + safe_count,
    }
    report = {
        "evidence_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "production_registry_sha256": hashlib.sha256(production_before).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mappings_before).hexdigest().upper(),
        "candidate_registry_has_exact_production_prefix": candidate_bytes.startswith(production_before),
        "validated_registry_has_exact_production_prefix": validated_bytes.startswith(production_before),
        "batch_size_limit": MAX_BATCH_SIZE,
        "maximum_pages_per_company": MAX_PAGES,
        "maximum_normalized_jobs_inspected_per_company": MAX_JOBS,
        "totals": totals,
        "results": results,
    }
    validator._write_json_if_changed(REPORT, report)
    if PRODUCTION.read_bytes() != production_before:
        raise RuntimeError("Production registry changed during validation")
    if PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Parent mappings changed during validation")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=validator.DEFAULT_TIMEOUT)
    parser.add_argument("--attempts", type=int, default=validator.DEFAULT_ATTEMPTS)
    parser.add_argument("--retry-delay", type=float, default=validator.DEFAULT_RETRY_DELAY)
    parser.add_argument("--batch-delay", type=float, default=validator.DEFAULT_BATCH_DELAY)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.build_only:
            content = build_candidate_registry()
            _write_inventory()
            payload: Mapping[str, Any] = {
                "candidate_registry": str(CANDIDATE_REGISTRY),
                "candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))),
                "bytes": len(content),
            }
        else:
            payload = validate(
                timeout=args.timeout,
                attempts=args.attempts,
                retry_delay=args.retry_delay,
                batch_delay=args.batch_delay,
            )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Validation setup failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
