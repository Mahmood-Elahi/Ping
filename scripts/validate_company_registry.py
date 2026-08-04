"""Read-only live validation for proposed company-registry additions.

This command deliberately imports only the registry model and production ATS
adapters.  It does not initialize scanning persistence, Supabase, SQLite, or
Google Sheets.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
import yaml

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.company_careers.adapters import ashby, greenhouse, lever, workday
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


DEFAULT_CURRENT_CONFIG = Path("config/target_companies.yaml")
DEFAULT_CANDIDATE_CONFIG = Path("config/target_companies.generated.yaml")
DEFAULT_OUTPUT = Path("output/company_registry_validation.json")
DEFAULT_VALIDATED_CONFIG = Path("config/target_companies.validated.yaml")
MAX_BATCH_SIZE = 5
DEFAULT_TIMEOUT = 20
DEFAULT_ATTEMPTS = 2
DEFAULT_RETRY_DELAY = 0.5
DEFAULT_BATCH_DELAY = 1.0

VALIDATED_WITH_JOBS = "validated_with_jobs"
VALIDATED_ZERO_JOBS = "validated_zero_jobs"
INVALID_CONFIGURATION = "invalid_configuration"
BLOCKED = "blocked"
TEMPORARY_FAILURE = "temporary_failure"
DUPLICATE_SOURCE = "duplicate_source"
UNSUPPORTED_RESPONSE = "unsupported_response"

VALIDATION_STATUSES = (
    VALIDATED_WITH_JOBS,
    VALIDATED_ZERO_JOBS,
    INVALID_CONFIGURATION,
    BLOCKED,
    TEMPORARY_FAILURE,
    DUPLICATE_SOURCE,
    UNSUPPORTED_RESPONSE,
)

AdapterFn = Callable[[Any, CompanyTarget, int], list[JobRecord]]

ADAPTERS: dict[str, AdapterFn] = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
    "workday": workday.fetch_jobs,
}


def load_registry(path: Path | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry_path = Path(path)
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    entries = data.get("target_companies")
    if not isinstance(entries, list):
        raise ValueError(f"Registry has no target_companies list: {registry_path}")
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"Registry entry {index} is not a mapping: {registry_path}")
        normalized.append(dict(entry))
    return data, normalized


def _name_key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("name") or "").casefold().split())


def detect_additions(
    current_entries: Iterable[Mapping[str, Any]],
    candidate_entries: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return candidate entries whose normalized company name is not current."""
    current_names = {_name_key(entry) for entry in current_entries}
    return [copy.deepcopy(dict(entry)) for entry in candidate_entries if _name_key(entry) not in current_names]


def source_key(entry: Mapping[str, Any]) -> Optional[str]:
    platform = str(entry.get("platform") or "").strip().casefold()
    if platform in {"greenhouse", "lever", "ashby"}:
        slug = str(entry.get("slug") or "").strip().casefold()
        return f"{platform}:{slug}" if slug else None
    if platform == "workday":
        tenant = str(entry.get("tenant") or "").strip().casefold()
        # The production Workday adapter emits source=workday:<tenant>, so a
        # repeated tenant would create colliding source identifiers.
        return f"workday:{tenant}" if tenant else None
    return None


def configured_identifiers(entry: Mapping[str, Any]) -> dict[str, Any]:
    platform = str(entry.get("platform") or "").strip().casefold()
    if platform in {"greenhouse", "lever", "ashby"}:
        return {"slug": entry.get("slug")}
    if platform == "workday":
        identifiers: dict[str, Any] = {
            "tenant": entry.get("tenant"),
            "wd_subdomain": entry.get("wd_subdomain"),
        }
        if entry.get("site_id") is not None:
            identifiers["site_id"] = entry.get("site_id")
        if entry.get("site_ids"):
            identifiers["site_ids"] = list(entry.get("site_ids") or [])
        return identifiers
    return {}


def endpoint_for(entry: Mapping[str, Any]) -> Optional[str]:
    platform = str(entry.get("platform") or "").strip().casefold()
    slug = str(entry.get("slug") or "").strip()
    if platform == "greenhouse" and slug:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if platform == "lever" and slug:
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    if platform == "ashby" and slug:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    if platform == "workday":
        tenant = str(entry.get("tenant") or "").strip()
        subdomain = str(entry.get("wd_subdomain") or "").strip()
        site_id = str(entry.get("site_id") or "").strip()
        if not site_id:
            site_ids = list(entry.get("site_ids") or [])
            site_id = str(site_ids[0]).strip() if site_ids else ""
        if tenant and subdomain and site_id:
            return (
                f"https://{tenant}.{subdomain}.myworkdayjobs.com"
                f"/wday/cxs/{tenant}/{site_id}/jobs"
            )
    return None


def configuration_error(entry: Mapping[str, Any]) -> Optional[str]:
    name = str(entry.get("name") or "").strip()
    platform = str(entry.get("platform") or "").strip().casefold()
    if not name:
        return "Missing company name"
    if platform not in ADAPTERS:
        return f"Unsupported validation platform: {platform or 'missing'}"
    if platform in {"greenhouse", "lever", "ashby"}:
        if not str(entry.get("slug") or "").strip():
            return f"{platform.title()} target is missing slug"
    elif platform == "workday":
        missing = [
            field
            for field in ("tenant", "wd_subdomain")
            if not str(entry.get(field) or "").strip()
        ]
        if not entry.get("site_id") and not entry.get("site_ids"):
            missing.append("site_id/site_ids")
        if missing:
            return f"Workday target is missing {', '.join(missing)}"
    try:
        CompanyTarget.from_dict(dict(entry))
    except (KeyError, TypeError, ValueError) as exc:
        return f"Invalid target fields: {exc}"
    return None


class RecordingRetrySession:
    """Duck-typed requests session that records final responses and retries."""

    TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.attempts = max(1, attempts)
        self.retry_delay = max(0.0, retry_delay)
        self.sleep = sleep
        self.responses: list[Any] = []
        self.requested_urls: list[str] = []
        self.attempt_count = 0

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("post", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        last_error: Optional[requests.RequestException] = None
        for attempt in range(self.attempts):
            self.attempt_count += 1
            self.requested_urls.append(url)
            try:
                response = getattr(self.session, method)(url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    self.sleep(self.retry_delay)
                    continue
                raise
            self.responses.append(response)
            status_code = getattr(response, "status_code", None)
            if status_code in self.TRANSIENT_STATUSES and attempt + 1 < self.attempts:
                self.sleep(self.retry_delay)
                continue
            return response
        if last_error is not None:  # pragma: no cover - loop always raises first
            raise last_error
        raise RuntimeError("HTTP retry loop finished without a response")

    @property
    def final_response(self) -> Optional[Any]:
        return self.responses[-1] if self.responses else None


def _response_shape_error(platform: str, response: Any) -> Optional[str]:
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        return f"Response is not valid JSON: {exc}"

    if platform == "lever":
        if not isinstance(payload, list):
            return "Lever response must be a JSON list"
        return None
    expected_key = {
        "greenhouse": "jobs",
        "ashby": "jobs",
        "workday": "jobPostings",
    }.get(platform)
    if not isinstance(payload, Mapping):
        return f"{platform.title()} response must be a JSON object"
    if expected_key not in payload or not isinstance(payload[expected_key], list):
        return f"{platform.title()} response must contain a {expected_key} list"
    return None


def _redirects(session: RecordingRetrySession) -> list[dict[str, Any]]:
    redirects: list[dict[str, Any]] = []
    for response in session.responses:
        for prior in getattr(response, "history", []) or []:
            redirects.append(
                {
                    "status": getattr(prior, "status_code", None),
                    "from": getattr(prior, "url", None),
                    "to": getattr(response, "url", None),
                }
            )
    return redirects


def _response_error_status(response: Optional[Any]) -> tuple[str, str]:
    status_code = getattr(response, "status_code", None) if response is not None else None
    if status_code in {401, 403, 407, 451}:
        return BLOCKED, f"Endpoint access was blocked (HTTP {status_code})"
    if status_code in RecordingRetrySession.TRANSIENT_STATUSES:
        return TEMPORARY_FAILURE, f"Endpoint returned a temporary HTTP {status_code} failure"
    if status_code in {400, 404, 410, 422}:
        return INVALID_CONFIGURATION, f"Endpoint rejected the configured ATS identifier (HTTP {status_code})"
    if status_code is not None and status_code != 200:
        return UNSUPPORTED_RESPONSE, f"Unexpected HTTP status {status_code}"
    return UNSUPPORTED_RESPONSE, "Adapter could not parse the endpoint response"


def _valid_job_error(jobs: list[JobRecord]) -> Optional[str]:
    for index, job in enumerate(jobs, start=1):
        if not job.job_id or not job.source:
            return f"Job {index} has no stable job_id/source identifier"
        parsed_url = urlparse(job.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return f"Job {index} has no valid apply URL"
    return None


def _base_result(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company": str(entry.get("name") or ""),
        "platform": str(entry.get("platform") or ""),
        "configured_identifiers": configured_identifiers(entry),
        "endpoint_tested": endpoint_for(entry),
        "http_status": None,
        "result_status": "not_started",
        "jobs_returned": 0,
        "sample_job_title": None,
        "sample_apply_url": None,
        "parse_success": False,
        "duplicate_source_status": "unique",
        "validation_status": INVALID_CONFIGURATION,
        "error": None,
        "warning": None,
        "safe_to_activate": False,
        "redirects": [],
        "attempts": 0,
    }


def validate_company(
    entry: Mapping[str, Any],
    *,
    duplicate_of: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    session_factory: Callable[[], Any] = requests.Session,
    sleep: Callable[[float], None] = time.sleep,
    adapters: Optional[Mapping[str, AdapterFn]] = None,
) -> dict[str, Any]:
    result = _base_result(entry)
    config_error = configuration_error(entry)
    if config_error:
        result.update(result_status="configuration_error", error=config_error)
        return result
    if duplicate_of:
        result.update(
            result_status="duplicate",
            duplicate_source_status=f"duplicates {duplicate_of}",
            validation_status=DUPLICATE_SOURCE,
            error=f"Source configuration duplicates {duplicate_of}",
        )
        return result

    platform = str(entry["platform"]).strip().casefold()
    target = CompanyTarget.from_dict(dict(entry))
    if platform == "workday":
        target.max_pages = 1
    session = RecordingRetrySession(
        session_factory(), attempts=attempts, retry_delay=retry_delay, sleep=sleep
    )
    adapter_map = adapters or ADAPTERS
    try:
        jobs = adapter_map[platform](session, target, timeout)
    except requests.RequestException as exc:
        response = session.final_response
        status, message = _response_error_status(response)
        if response is None:
            status, message = TEMPORARY_FAILURE, f"Request failed after retries: {exc}"
        result.update(validation_status=status, result_status="request_failed", error=message)
    except AdapterError as exc:
        status, message = _response_error_status(session.final_response)
        result.update(
            validation_status=status,
            result_status="adapter_failed",
            error=f"{message}: {exc}",
        )
    except (KeyError, TypeError, ValueError) as exc:
        result.update(
            validation_status=UNSUPPORTED_RESPONSE,
            result_status="parse_failed",
            error=f"Malformed or unsupported response: {exc}",
        )
    except Exception as exc:  # contain a single company's unexpected parser failure
        result.update(
            validation_status=UNSUPPORTED_RESPONSE,
            result_status="parse_failed",
            error=f"Unexpected {exc.__class__.__name__}: {exc}",
        )
    else:
        response = session.final_response
        shape_error = (
            _response_shape_error(platform, response)
            if response is not None
            else "Adapter made no HTTP request"
        )
        job_error = _valid_job_error(jobs)
        if shape_error or job_error:
            result.update(
                validation_status=UNSUPPORTED_RESPONSE,
                result_status="parse_failed",
                error=shape_error or job_error,
            )
        else:
            sample = jobs[0] if jobs else None
            result.update(
                result_status="success",
                jobs_returned=len(jobs),
                sample_job_title=sample.title if sample else None,
                sample_apply_url=sample.url if sample else None,
                parse_success=True,
                validation_status=VALIDATED_WITH_JOBS if jobs else VALIDATED_ZERO_JOBS,
                safe_to_activate=True,
            )

    response = session.final_response
    result["http_status"] = getattr(response, "status_code", None) if response is not None else None
    result["endpoint_tested"] = session.requested_urls[-1] if session.requested_urls else result["endpoint_tested"]
    result["redirects"] = _redirects(session)
    result["attempts"] = session.attempt_count
    if result["redirects"] and result["safe_to_activate"]:
        result["warning"] = "Endpoint redirected before returning a valid ATS response"
    return result


def _duplicate_owners(
    current_entries: Iterable[Mapping[str, Any]],
    additions: Iterable[Mapping[str, Any]],
) -> dict[int, str]:
    owner_by_source: dict[str, str] = {}
    for entry in current_entries:
        key = source_key(entry)
        if key:
            owner_by_source.setdefault(key, str(entry.get("name") or "existing entry"))
    duplicates: dict[int, str] = {}
    for index, entry in enumerate(additions):
        key = source_key(entry)
        if not key:
            continue
        owner = owner_by_source.get(key)
        if owner:
            duplicates[index] = owner
        else:
            owner_by_source[key] = str(entry.get("name") or f"candidate {index + 1}")
    return duplicates


def build_validated_registry_bytes(
    current_path: Path | str,
    safe_additions: Iterable[Mapping[str, Any]],
) -> bytes:
    """Append additions while leaving every byte of the current entries intact."""
    current_bytes = Path(current_path).read_bytes()
    newline = b"\r\n" if b"\r\n" in current_bytes else b"\n"
    additions = [dict(entry) for entry in safe_additions]
    if not additions:
        return current_bytes
    _, current_entries = load_registry(current_path)
    dumped = yaml.safe_dump(
        additions,
        sort_keys=False,
        allow_unicode=True,
        width=1000,
    )
    indented = "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))
    appended = indented.encode("utf-8").replace(b"\n", newline)
    if not current_entries:
        # There are no configuration-entry bytes to preserve in this special
        # case, and a compact `target_companies: []` cannot be appended to.
        return b"target_companies:" + newline + appended
    separator = b"" if current_bytes.endswith((b"\n", b"\r")) else newline
    return current_bytes + separator + appended


def _write_bytes_if_changed(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    path.write_bytes(content)


def _write_json_if_changed(path: Path, data: Mapping[str, Any]) -> None:
    content = (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
    _write_bytes_if_changed(path, content)


def validate_registry(
    current_config: Path | str,
    candidate_config: Path | str,
    *,
    only_additions: bool = False,
    batch_size: int = MAX_BATCH_SIZE,
    start: int = 0,
    limit: Optional[int] = None,
    output: Path | str = DEFAULT_OUTPUT,
    validated_config: Path | str = DEFAULT_VALIDATED_CONFIG,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    batch_delay: float = DEFAULT_BATCH_DELAY,
    session_factory: Callable[[], Any] = requests.Session,
    sleep: Callable[[float], None] = time.sleep,
    adapters: Optional[Mapping[str, AdapterFn]] = None,
) -> dict[str, Any]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if start < 0:
        raise ValueError("start must be at least 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be at least 0")

    _, current_entries = load_registry(current_config)
    _, candidate_entries = load_registry(candidate_config)
    selected_entries = (
        detect_additions(current_entries, candidate_entries)
        if only_additions
        else [copy.deepcopy(entry) for entry in candidate_entries]
    )
    total_selected = len(selected_entries)
    stop = None if limit is None else start + limit
    selected_entries = selected_entries[start:stop]

    if dry_run:
        return {
            "current_config": str(current_config),
            "candidate_config": str(candidate_config),
            "only_additions": only_additions,
            "dry_run": True,
            "batch_size": batch_size,
            "start": start,
            "limit": limit,
            "summary": {
                "candidate_entries": len(candidate_entries),
                "current_entries": len(current_entries),
                "additions_detected": len(detect_additions(current_entries, candidate_entries)),
                "companies_selected": len(selected_entries),
                "companies_tested": 0,
            },
            "planned_companies": [str(entry.get("name") or "") for entry in selected_entries],
            "results": [],
        }

    duplicates = _duplicate_owners(current_entries, selected_entries)
    results: list[dict[str, Any]] = []
    for batch_start in range(0, len(selected_entries), batch_size):
        batch = selected_entries[batch_start : batch_start + batch_size]
        for offset, entry in enumerate(batch):
            index = batch_start + offset
            results.append(
                validate_company(
                    entry,
                    duplicate_of=duplicates.get(index),
                    timeout=timeout,
                    attempts=attempts,
                    retry_delay=retry_delay,
                    session_factory=session_factory,
                    sleep=sleep,
                    adapters=adapters,
                )
            )
        if batch_start + batch_size < len(selected_entries) and batch_delay > 0:
            sleep(batch_delay)

    safe_names = [result["company"] for result in results if result["safe_to_activate"]]
    safe_name_keys = {" ".join(name.casefold().split()) for name in safe_names}
    safe_additions = [entry for entry in selected_entries if _name_key(entry) in safe_name_keys]
    status_counts = Counter(result["validation_status"] for result in results)
    validated_bytes = build_validated_registry_bytes(current_config, safe_additions)
    validated_data = yaml.safe_load(validated_bytes.decode("utf-8")) or {}
    validated_entries = validated_data.get("target_companies") or []
    report = {
        "current_config": str(current_config),
        "candidate_config": str(candidate_config),
        "validated_config": str(validated_config),
        "only_additions": only_additions,
        "dry_run": False,
        "batch_size": batch_size,
        "start": start,
        "limit": limit,
        "summary": {
            "candidate_entries": len(candidate_entries),
            "current_entries": len(current_entries),
            "additions_detected": len(detect_additions(current_entries, candidate_entries)),
            "companies_selected": len(selected_entries),
            "companies_tested": len(results),
            "status_counts": {status: status_counts.get(status, 0) for status in VALIDATION_STATUSES},
            "safe_to_activate": safe_names,
            "excluded": [
                {
                    "company": result["company"],
                    "status": result["validation_status"],
                    "reason": result["error"] or result["warning"],
                }
                for result in results
                if not result["safe_to_activate"]
            ],
            "validated_config_entries": len(validated_entries),
            "total_selected_before_window": total_selected,
        },
        "results": results,
    }
    _write_json_if_changed(Path(output), report)
    _write_bytes_if_changed(Path(validated_config), validated_bytes)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-config", type=Path, default=DEFAULT_CURRENT_CONFIG)
    parser.add_argument("--candidate-config", type=Path, default=DEFAULT_CANDIDATE_CONFIG)
    parser.add_argument("--only-additions", action="store_true")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--validated-config", type=Path, default=DEFAULT_VALIDATED_CONFIG)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_registry(
            args.current_config,
            args.candidate_config,
            only_additions=args.only_additions,
            batch_size=args.batch_size,
            start=args.start,
            limit=args.limit,
            output=args.output,
            validated_config=args.validated_config,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Validation setup failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
