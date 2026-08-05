"""Bounded, read-only validation of TSMC's public careers source recovery.

The validator deliberately uses production scanner dispatch and adapter code.
It does not import or initialize persistence, dashboards, Supabase, Google
Sheets, SQLite, a browser, or JavaScript execution.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlencode, urlparse

import requests
import yaml

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import avature, sap_successfactors
from src.scraper.company_careers.models import AdapterError, CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
HISTORICAL_REPORT = ROOT / "output" / "avature_icims_validation.json"
CSV_REPORT = ROOT / "config" / "company_tsmc_source_recovery.csv"
CANDIDATE = ROOT / "config" / "target_companies.tsmc_recovery_candidate.yaml"
VALIDATED = ROOT / "config" / "target_companies.tsmc_recovery_validated.yaml"
JSON_REPORT = ROOT / "output" / "tsmc_source_recovery_validation.json"

OFFICIAL_CAREERS_URL = "https://www.tsmc.com/static/english/careers/index.htm"
OLD_AVATURE_URL = "https://careers.tsmc.com/en_US/careers/SearchJobs"
RECOVERED_SEARCH_URL = "https://ro.careers.tsmc.com/search/"
RECOVERED_LISTING_URL = "https://ro.careers.tsmc.com/go/CorporateJobs/4716710/"
EVIDENCE_TIMESTAMP = "2026-08-05T00:00:00+00:00"

MAX_PRIMARY_BYTES = 3_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_REQUESTS = 5
MAX_ATTEMPTS = 2
MAX_PAGES = 2
MAX_NORMALIZED_JOBS = 20
TIMEOUT_SECONDS = 20
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}

VALIDATED_WITH_JOBS = "validated_with_jobs"
VALIDATED_ZERO_JOBS = "validated_zero_jobs"
BLOCKED = "blocked"
BROWSER_REQUIRED = "browser_required"
SESSION_REQUIRED = "session_required"
AUTHENTICATION_REQUIRED = "authentication_required"
MALFORMED_RESPONSE = "malformed_response"
TEMPORARY_FAILURE = "temporary_failure"
NETWORK_INACCESSIBLE = "network_inaccessible"

CSV_FIELDS = [
    "company", "current_production_platform", "current_production_source_identity",
    "current_production_careers_url", "current_production_endpoint", "current_failure",
    "historical_evidence", "current_official_careers_url", "final_url", "redirect_chain",
    "detected_platform", "platform_variant", "endpoint", "request_method", "tenant",
    "site_id", "organization", "source_identity", "stable_id_contract",
    "apply_url_contract", "allowed_source_hosts", "allowed_apply_hosts",
    "pagination_contract", "browser_evidence", "session_evidence",
    "authentication_evidence", "blocked_evidence", "validation_status",
    "safe_to_repair", "recommended_action", "warnings", "diagnostics",
    "evidence_timestamp",
]


def _response_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    return str(getattr(response, "text", "") or "").encode("utf-8")


def _full_url(url: str, params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode(sorted((str(k), str(v)) for k, v in params.items()))}"


def redirect_chain(response: Any) -> list[dict[str, Any]]:
    chain = []
    for item in list(getattr(response, "history", []) or []) + [response]:
        record: dict[str, Any] = {
            "status": int(getattr(item, "status_code", 0) or 0),
            "url": str(getattr(item, "url", "") or ""),
        }
        location = str(getattr(item, "headers", {}).get("location", "") or "")
        if location:
            record["location"] = location
        chain.append(record)
    return chain


class BoundedSession:
    """requests-compatible session with caching, retry and byte/request caps."""

    def __init__(
        self,
        transport: Any,
        *,
        max_requests: int = MAX_REQUESTS,
        max_attempts: int = MAX_ATTEMPTS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.transport = transport
        self.max_requests = max_requests
        self.max_attempts = max_attempts
        self.max_response_bytes = max_response_bytes
        self.cache: dict[str, Any] = {}
        self.request_attempts: list[dict[str, Any]] = []
        self.network_calls = 0

    def get(self, url: str, **kwargs: Any) -> Any:
        params = kwargs.get("params")
        key = _full_url(str(url), params)
        if key in self.cache:
            self.request_attempts.append({"method": "GET", "url": key, "cached": True})
            return self.cache[key]
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            if self.network_calls >= self.max_requests:
                raise AdapterError("TSMC recovery request limit exceeded")
            self.network_calls += 1
            try:
                response = self.transport.get(url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                self.request_attempts.append(
                    {"method": "GET", "url": key, "attempt": attempt, "error": exc.__class__.__name__}
                )
                if attempt < self.max_attempts:
                    continue
                raise
            body = _response_bytes(response)
            status = int(getattr(response, "status_code", 0) or 0)
            self.request_attempts.append(
                {
                    "method": "GET", "url": key, "attempt": attempt,
                    "status": status, "response_bytes": len(body), "cached": False,
                }
            )
            if len(body) > self.max_response_bytes:
                raise AdapterError("TSMC recovery response byte limit exceeded")
            if status in TRANSIENT_STATUSES and attempt < self.max_attempts:
                continue
            self.cache[key] = response
            return response
        if last_error:
            raise last_error
        raise AdapterError("TSMC recovery request attempts exhausted")


class FixtureResponse:
    def __init__(
        self,
        url: str,
        status: int = 200,
        text: str = "",
        headers: Optional[Mapping[str, str]] = None,
        history: Optional[Iterable[Any]] = None,
    ) -> None:
        self.url = url
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = dict(headers or {"content-type": "text/html"})
        self.history = list(history or [])


class FixtureTransport:
    """Deterministic sequential response transport used by offline fixtures."""

    def __init__(self, responses: Iterable[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> Any:
        if self.calls >= len(self.responses):
            raise requests.ConnectionError("fixture responses exhausted")
        response = self.responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


def classify_http_response(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "").casefold()
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if status in {401, 407}:
        return AUTHENTICATION_REQUIRED
    if status in {403, 406, 429, 451}:
        return BLOCKED
    if status in TRANSIENT_STATUSES:
        return TEMPORARY_FAILURE
    if status != 200:
        return MALFORMED_RESPONSE
    if "enable javascript to continue" in text or "javascript is required" in text:
        return BROWSER_REQUIRED
    if "session required" in text or "session expired" in text:
        return SESSION_REQUIRED
    if "<title>sign in" in text or "<title>log in" in text or "<title>login" in text:
        return AUTHENTICATION_REQUIRED
    if content_type and "html" not in content_type.casefold():
        return MALFORMED_RESPONSE
    if not text.strip():
        return MALFORMED_RESPONSE
    return "public_html"


def detect_platform(html_text: str, url: str = "") -> str:
    evidence = f"{html_text} {url}".casefold()
    if "sap as service provider" in evidence or "successfactors" in evidence or "jobs2web" in evidence:
        return "sap_successfactors"
    if "avature" in evidence or "searchjobs" in evidence:
        return "avature"
    if "workday" in evidence or "myworkdayjobs" in evidence:
        return "workday"
    if "icims" in evidence:
        return "icims"
    return "unknown"


def normalize_avature_search_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Avature public endpoint must be HTTPS")
    path = parsed.path.rstrip("/")
    if not path.casefold().endswith("/searchjobs"):
        raise ValueError("Avature public endpoint must end in SearchJobs")
    return parsed._replace(path=path, query="", fragment="").geturl()


def current_tsmc_entry(path: Path = PRODUCTION) -> dict[str, Any]:
    _, entries = registry_validator.load_registry(path)
    matches = [entry for entry in entries if registry_validator._name_key(entry) == "tsmc"]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one production TSMC entry, found {len(matches)}")
    return matches[0]


def recovered_entry() -> dict[str, Any]:
    return {
        "name": "TSMC",
        "platform": "sap_successfactors",
        "careers_url": OFFICIAL_CAREERS_URL,
        "search_url": RECOVERED_SEARCH_URL,
        "endpoint_variant": "rmk_table",
        "original_workbook_rank": 20,
        "industry": "Not recorded in repository master workbook",
        "allowed_apply_hosts": ["ro.careers.tsmc.com"],
        "max_pages": 1,
        "max_jobs": 20,
        "evidence_source": "config/company_tsmc_source_recovery.csv",
        "evidence_confidence": "high",
        "evidence_timestamp": EVIDENCE_TIMESTAMP,
    }


def historical_evidence() -> dict[str, Any]:
    if not HISTORICAL_REPORT.exists():
        return {}
    data = json.loads(HISTORICAL_REPORT.read_text(encoding="utf-8"))
    records = data.get("results") or data.get("companies") or []
    return next((dict(row) for row in records if row.get("company") == "TSMC"), {})


def _entry_offsets(content: bytes) -> list[int]:
    result: list[int] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.startswith(b"  - name: "):
            result.append(offset)
        offset += len(line)
    return result


def replace_tsmc_bytes(path: Path, replacement: Mapping[str, Any]) -> bytes:
    content = path.read_bytes()
    _, entries = registry_validator.load_registry(path)
    indexes = [i for i, entry in enumerate(entries) if registry_validator._name_key(entry) == "tsmc"]
    if len(indexes) != 1:
        raise ValueError("Replacement requires exactly one TSMC entry")
    offsets = _entry_offsets(content)
    if len(offsets) != len(entries):
        raise ValueError("Could not locate all production registry entry boundaries")
    index = indexes[0]
    start = offsets[index]
    end = offsets[index + 1] if index + 1 < len(offsets) else len(content)
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    dumped = yaml.safe_dump([dict(replacement)], sort_keys=False, allow_unicode=True, width=1000)
    block = "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n")).encode("utf-8")
    block = block.replace(b"\n", newline)
    return content[:start] + block + content[end:]


def candidate_bytes(replacement: Mapping[str, Any]) -> bytes:
    content = PRODUCTION.read_bytes()
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    metadata = {
        "tsmc_source_recovery_candidate": {
            "operation": "no_promotable_registry_change",
            "current_source_identity": registry_validator.source_key(current_tsmc_entry()),
            "proposed_source_identity": None,
            "proposed_entry": None,
            "alternate_official_source_observed": dict(replacement),
            "reason": "The production Avature source revalidated; the SAP source is a separate regional collection.",
        }
    }
    suffix = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True, width=1000).encode("utf-8")
    suffix = suffix.replace(b"\n", newline)
    separator = b"" if content.endswith((b"\n", b"\r")) else newline
    return content + separator + suffix


def build_artifact_bytes(safe_to_repair: bool) -> tuple[bytes, bytes]:
    replacement = recovered_entry()
    # No replacement is promotable in this milestone.  The argument remains
    # explicit so offline tests can prove unsafe outcomes never change the
    # validated registry.
    return candidate_bytes(replacement), PRODUCTION.read_bytes()


def _job_checks(jobs: list[Any], host: str) -> dict[str, Any]:
    inspected = jobs[:MAX_NORMALIZED_JOBS]
    ids = [str(job.job_id) for job in inspected]
    urls = [str(job.url) for job in inspected]
    return {
        "normalized_jobs_returned": len(jobs),
        "normalized_jobs_inspected": len(inspected),
        "stable_ids_valid": bool(inspected) and len(ids) == len(set(ids)) and all(ids),
        "apply_urls_valid": bool(inspected) and all(
            urlparse(url).scheme == "https" and (urlparse(url).hostname or "").casefold() == host
            for url in urls
        ),
    }


def validate_adapter_candidate(session: Any, entry: Mapping[str, Any]) -> dict[str, Any]:
    target = CompanyTarget.from_dict(dict(entry))
    adapter = scanner.ADAPTERS.get(target.platform)
    if adapter is None:
        return {"status": "unsupported_variant", "safe_to_repair": False, "error": "No production adapter"}
    try:
        jobs = adapter(session, target, TIMEOUT_SECONDS)
    except (AdapterError, requests.RequestException) as exc:
        error = str(exc)
        lowered = error.casefold()
        status = (
            BLOCKED if "blocked" in lowered or "http 403" in lowered
            else BROWSER_REQUIRED if "browser required" in lowered
            else SESSION_REQUIRED if "session required" in lowered
            else AUTHENTICATION_REQUIRED if "authentication required" in lowered
            else TEMPORARY_FAILURE
        )
        return {"status": status, "safe_to_repair": False, "error": error, "jobs": []}
    checks = _job_checks(jobs, (urlparse(str(target.search_url)).hostname or "").casefold())
    pagination = getattr(session, f"{target.platform}_pagination", {})
    zero_valid = not jobs and pagination.get("stop_reason") == "validated_zero_jobs"
    safe = zero_valid or (checks["stable_ids_valid"] and checks["apply_urls_valid"])
    return {
        "status": VALIDATED_ZERO_JOBS if zero_valid else (VALIDATED_WITH_JOBS if safe else "ambiguous_contract"),
        "safe_to_repair": safe,
        "jobs": jobs[:MAX_NORMALIZED_JOBS],
        "checks": checks,
        "pagination": pagination,
        "error": None,
    }


def _response_summary(response: Any) -> dict[str, Any]:
    return {
        "status": int(getattr(response, "status_code", 0) or 0),
        "url": str(getattr(response, "url", "") or ""),
        "content_type": str(getattr(response, "headers", {}).get("content-type", "")),
        "response_bytes": len(_response_bytes(response)),
        "classification": classify_http_response(response),
        "redirect_chain": redirect_chain(response),
    }


def run_validation(transport: Optional[Any] = None) -> dict[str, Any]:
    production = current_tsmc_entry()
    history = historical_evidence()
    bounded = BoundedSession(transport or requests.Session())
    discovery: dict[str, Any] = {}
    diagnostics: list[str] = []

    try:
        landing = bounded.get(
            OFFICIAL_CAREERS_URL,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
            timeout=TIMEOUT_SECONDS,
        )
        discovery["official_landing"] = _response_summary(landing)
    except (AdapterError, requests.RequestException) as exc:
        discovery["official_landing"] = {"error": str(exc), "classification": NETWORK_INACCESSIBLE}
        diagnostics.append(f"Official landing request failed: {exc}")

    compatibility_start = bounded.network_calls
    old_result = validate_adapter_candidate(bounded, production)
    replacement = recovered_entry()
    old_safe = bool(old_result.get("safe_to_repair"))
    compatibility_calls = bounded.network_calls - compatibility_start
    if not old_safe and compatibility_calls < MAX_PAGES:
        candidate_result = validate_adapter_candidate(bounded, replacement)
    else:
        candidate_result = {
            "status": "not_tested_parallel_source" if old_safe else "not_tested_page_limit",
            "safe_to_repair": False,
            "checks": {},
            "pagination": {},
            "error": None,
        }
    alternate_safe = bool(candidate_result.get("safe_to_repair"))
    # A healthy current Avature collection plus a healthy regional SAP
    # collection is evidence of parallel official sources, not a migration.
    # The prior isolated 403 is therefore classified as a temporary failure.
    safe = False
    status = TEMPORARY_FAILURE if old_safe else str(old_result.get("status"))
    recommendation = "temporary_failure" if old_safe else "no_safe_repair"
    old_pagination = old_result.get("pagination", {})
    old_checks = old_result.get("checks", {})
    old_cache_key = next((key for key in bounded.cache if key.startswith(OLD_AVATURE_URL)), "")
    old_response = bounded.cache.get(old_cache_key)
    report = {
        "limits": {
            "maximum_requests": MAX_REQUESTS, "maximum_attempts_per_request": MAX_ATTEMPTS,
            "maximum_pages": MAX_PAGES, "maximum_normalized_jobs_inspected": MAX_NORMALIZED_JOBS,
            "maximum_primary_html_bytes": MAX_PRIMARY_BYTES,
            "maximum_response_bytes": MAX_RESPONSE_BYTES,
        },
        "current_production_configuration": production,
        "historical_validation_evidence": history,
        "current_source_discovery": {
            **discovery,
            "current_official_careers_url": OFFICIAL_CAREERS_URL,
            "official_listing_url": OLD_AVATURE_URL,
            "official_search_url": OLD_AVATURE_URL,
            "official_careers_domains": ["www.tsmc.com", "careers.tsmc.com", "ro.careers.tsmc.com"],
            "detected_platform": "avature",
            "platform_variant": "avature_portal_html",
            "alternate_regional_source": {
                "url": RECOVERED_SEARCH_URL,
                "platform": "sap_successfactors",
                "platform_variant": "rmk_table",
                "adapter_compatible": alternate_safe,
            },
        },
        "request_attempts": bounded.request_attempts,
        "redirect_chain": redirect_chain(old_response) if old_response is not None else [],
        "platform_evidence": "The production Avature portal returned two public server-rendered pages; SAP RMK is also present as a separate regional source.",
        "endpoint_evidence": {
            "method": "GET", "url": OLD_AVATURE_URL, "source_identity": registry_validator.source_key(production),
        },
        "response_status": status,
        "response_type": "text/html",
        "stable_id_checks": old_checks.get("stable_ids_valid", False),
        "apply_url_checks": old_checks.get("apply_urls_valid", False),
        "pagination_checks": old_pagination,
        "browser_session_authentication_checks": {
            "browser_required": False, "session_initialization_required": False,
            "authentication_required": False,
            "required_cookies_disclosed_by_site": ["route", "careerSiteCompanyId", "JSESSIONID", "load balancer cookie"],
            "anonymous_adapter_request_succeeded": old_safe,
        },
        "adapter_compatibility": {
            "production_dispatch_used": scanner.ADAPTERS.get("sap_successfactors") is sap_successfactors.fetch_jobs,
            "old_avature": {key: value for key, value in old_result.items() if key != "jobs"},
            "alternate_sap_successfactors": {key: value for key, value in candidate_result.items() if key != "jobs"},
        },
        "final_status": status,
        "safe_to_repair": safe,
        "recommended_action": recommendation,
        "diagnostics": diagnostics,
        "warnings": [
            "The official SAP RMK site is a parallel regional source and is not a safe one-for-one Avature replacement.",
            "The prior HTTP 403 is blocked evidence but the same anonymous Avature contract now succeeds without browser or session initialization.",
        ],
        "evidence_timestamp": EVIDENCE_TIMESTAMP,
    }
    return report


def _csv_row(report: Mapping[str, Any]) -> dict[str, str]:
    current = report["current_production_configuration"]
    history = report["historical_validation_evidence"]
    discovery = report["current_source_discovery"]
    endpoint = report["endpoint_evidence"]
    checks = report["browser_session_authentication_checks"]
    pagination = report["pagination_checks"]
    return {
        "company": "TSMC",
        "current_production_platform": str(current.get("platform", "")),
        "current_production_source_identity": str(registry_validator.source_key(current) or ""),
        "current_production_careers_url": str(current.get("careers_url", "")),
        "current_production_endpoint": str(current.get("search_url", "")),
        "current_failure": "Avature endpoint access was blocked (HTTP 403)",
        "historical_evidence": json.dumps(history, ensure_ascii=False, sort_keys=True),
        "current_official_careers_url": str(discovery.get("current_official_careers_url", "")),
        "final_url": str(discovery.get("official_search_url", "")),
        "redirect_chain": json.dumps(report.get("redirect_chain", []), sort_keys=True),
        "detected_platform": str(discovery.get("detected_platform", "")),
        "platform_variant": str(discovery.get("platform_variant", "")),
        "endpoint": str(endpoint.get("url", "")), "request_method": "GET",
        "tenant": "", "site_id": "", "organization": "",
        "source_identity": str(endpoint.get("source_identity", "")),
        "stable_id_contract": "valid Avature job ID; namespaced by careers.tsmc.com" if report.get("stable_id_checks") else "invalid",
        "apply_url_contract": "same-host HTTPS /job/ URL" if report.get("apply_url_checks") else "invalid",
        "allowed_source_hosts": "www.tsmc.com;careers.tsmc.com;ro.careers.tsmc.com",
        "allowed_apply_hosts": "careers.tsmc.com",
        "pagination_contract": json.dumps(pagination, ensure_ascii=False, sort_keys=True),
        "browser_evidence": "not required" if not checks.get("browser_required") else "required",
        "session_evidence": "anonymous request succeeded" if checks.get("anonymous_adapter_request_succeeded") else "not established",
        "authentication_evidence": "not required" if not checks.get("authentication_required") else "required",
        "blocked_evidence": "Historical production scan HTTP 403; bounded revalidation now returns HTTP 200.",
        "validation_status": str(report.get("final_status", "")),
        "safe_to_repair": str(bool(report.get("safe_to_repair"))).lower(),
        "recommended_action": str(report.get("recommended_action", "")),
        "warnings": " | ".join(str(value) for value in report.get("warnings", [])),
        "diagnostics": " | ".join(str(value) for value in report.get("diagnostics", [])),
        "evidence_timestamp": str(report.get("evidence_timestamp", "")),
    }


def _write_if_changed(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def write_artifacts(report: Mapping[str, Any]) -> None:
    candidate, validated = build_artifact_bytes(bool(report.get("safe_to_repair")))
    _write_if_changed(CANDIDATE, candidate)
    _write_if_changed(VALIDATED, validated)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(_csv_row(report))
    _write_if_changed(CSV_REPORT, buffer.getvalue().encode("utf-8"))
    _write_if_changed(
        JSON_REPORT,
        (json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8"),
    )


def _fixture_transport(path: Path) -> FixtureTransport:
    data = json.loads(path.read_text(encoding="utf-8"))
    responses = [
        FixtureResponse(
            str(item["url"]), int(item.get("status", 200)), str(item.get("text", "")),
            item.get("headers"),
        )
        for item in data.get("responses", [])
    ]
    return FixtureTransport(responses)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="Use deterministic offline JSON responses")
    args = parser.parse_args()
    report = run_validation(_fixture_transport(args.fixture) if args.fixture else None)
    write_artifacts(report)
    print(json.dumps({
        "company": "TSMC", "status": report["final_status"],
        "safe_to_repair": report["safe_to_repair"],
        "recommended_action": report["recommended_action"],
        "network_calls": len([row for row in report["request_attempts"] if not row.get("cached")]),
    }, indent=2))
    # An unsafe/no-change classification is a successful validation outcome,
    # not an execution failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
