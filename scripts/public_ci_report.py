"""Bounded public-safe reporting for the production GitHub Actions run.

The operational child process still sends ordinary stdout and stderr to a
private runner log.  This module provides the only public channel: versioned
JSON-lines events with fixed schemas, bounded numbers, allowlisted enums, and
single-line company names.  The CI wrapper validates the same schema again
before displaying anything.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


PUBLIC_REPORT_PATH_VARIABLE = "PING_PUBLIC_REPORT_PATH"
REPORT_VERSION = 1
MAX_EVENT_BYTES = 4096
MAX_EVENTS = 2048
MAX_COUNT = 100_000_000
MAX_DURATION_SECONDS = 172_800.0
MAX_COMPANY_NAME_LENGTH = 120

STAGE_LABELS = {
    "configuration": "Configuration",
    "supabase_initialization": "Supabase initialization",
    "lifecycle_state_retrieval": "Lifecycle-state retrieval",
    "google_sheets_initialization": "Google Sheets initialization",
    "company_scanning": "Company scanning",
    "job_ranking": "Job ranking",
    "supabase_synchronization": "Supabase synchronization",
    "source_health_persistence": "Source health persistence",
    "dashboard_data_retrieval": "Dashboard-data retrieval",
    "google_sheets_publication": "Google Sheets publication",
    "publication_status_persistence": "Publication-status persistence",
}
STAGE_STATUSES = frozenset({"started", "succeeded", "failed", "skipped"})
SCAN_STATUSES = frozenset({"complete", "incomplete", "failed"})
COVERAGE_HEALTH = frozenset({"healthy", "degraded", "failed", "unknown"})
FAILURE_CATEGORIES = frozenset(
    {
        "authentication_failure",
        "configuration_error",
        "connection_failure",
        "http_client_failure",
        "http_server_failure",
        "integration_access_failure",
        "internal_failure",
        "invalid_response",
        "rate_limited",
        "timeout",
        "unknown_failure",
    }
)
FAILURE_LABELS = {
    "authentication_failure": "authentication failure",
    "configuration_error": "configuration error",
    "connection_failure": "connection failure",
    "http_client_failure": "HTTP client failure",
    "http_server_failure": "HTTP server failure",
    "integration_access_failure": "integration access failure",
    "internal_failure": "internal failure",
    "invalid_response": "invalid response",
    "rate_limited": "rate limited",
    "timeout": "timeout",
    "unknown_failure": "unclassified failure",
}
SCAN_REASONS = frozenset(
    {
        "access_blocked",
        "browser_failure",
        "collection_changed",
        "complete",
        "cooldown_skipped",
        "discovery_only",
        "http_client_failure",
        "http_server_failure",
        "incomplete_other",
        "inventory_accounting_failure",
        "invalid_response",
        "manual_review_required",
        "missing_terminal_evidence",
        "network_failure",
        "pagination_safety_limit",
        "rate_limited",
        "scan_failure_other",
        "source_identity_failure",
        "source_unreachable",
        "timeout",
        "transient_http_failure",
        "unsupported_source",
    }
)
SCAN_REASON_LABELS = {
    "access_blocked": "access blocked",
    "browser_failure": "browser collection failure",
    "collection_changed": "collection changed during scan",
    "complete": "authoritative completion established",
    "cooldown_skipped": "source cooldown active",
    "discovery_only": "discovery-only source; no absence authority",
    "http_client_failure": "HTTP client failure",
    "http_server_failure": "HTTP server failure",
    "incomplete_other": "completion not established",
    "inventory_accounting_failure": "inventory accounting mismatch",
    "invalid_response": "invalid or malformed response",
    "manual_review_required": "manual review required",
    "missing_terminal_evidence": "missing terminal evidence",
    "network_failure": "network failure",
    "pagination_safety_limit": "pagination safety limit reached",
    "rate_limited": "rate limited",
    "scan_failure_other": "unclassified scan failure",
    "source_identity_failure": "source identity validation failed",
    "source_unreachable": "source unreachable",
    "timeout": "timeout",
    "transient_http_failure": "transient HTTP failure",
    "unsupported_source": "unsupported source",
}

_EVENT_FIELDS = {
    "run_start": frozenset({"v", "type", "configured_companies"}),
    "stage": frozenset(
        {"v", "type", "stage", "status", "failure_category", "duration_seconds"}
    ),
    "company": frozenset(
        {"v", "type", "name", "status", "jobs_fetched", "jobs_retained", "reason"}
    ),
    "scan_summary": frozenset(
        {
            "v",
            "type",
            "configured_companies",
            "attempted_companies",
            "complete_companies",
            "incomplete_companies",
            "failed_companies",
            "jobs_fetched",
            "normalized_jobs",
            "retained_jobs",
            "coverage_health",
            "scan_runtime_seconds",
        }
    ),
    "ranking_summary": frozenset({"v", "type", "final_ranked_jobs"}),
    "sync_summary": frozenset(
        {
            "v",
            "type",
            "inserted",
            "updated",
            "unchanged",
            "missing_incremented",
            "closed",
        }
    ),
    "sheets_summary": frozenset(
        {"v", "type", "new_jobs", "active_jobs", "companies"}
    ),
    "run_summary": frozenset({"v", "type", "runtime_seconds"}),
}


def _safe_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def _safe_duration(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= MAX_DURATION_SECONDS:
        return None
    return round(result, 3)


def _safe_company_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFC", value).strip()
    if not name or len(name) > MAX_COMPANY_NAME_LENGTH:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in name):
        return None
    if "\r" in name or "\n" in name:
        return None
    return name


def validate_public_event(value: Any) -> dict[str, Any] | None:
    """Return a canonical event or ``None`` without echoing rejected input."""

    if not isinstance(value, Mapping) or value.get("v") != REPORT_VERSION:
        return None
    event_type = value.get("type")
    expected_fields = _EVENT_FIELDS.get(event_type)
    if expected_fields is None or frozenset(value) != expected_fields:
        return None
    event = dict(value)

    count_fields = expected_fields - {
        "v",
        "type",
        "stage",
        "status",
        "failure_category",
        "duration_seconds",
        "name",
        "reason",
        "coverage_health",
        "scan_runtime_seconds",
        "runtime_seconds",
    }
    for field in count_fields:
        count = _safe_count(event[field])
        if count is None:
            return None
        event[field] = count

    if event_type == "stage":
        if event["stage"] not in STAGE_LABELS or event["status"] not in STAGE_STATUSES:
            return None
        category = event["failure_category"]
        duration = event["duration_seconds"]
        if event["status"] == "failed":
            if category not in FAILURE_CATEGORIES:
                return None
        elif category is not None:
            return None
        if event["status"] == "started":
            if duration is not None:
                return None
        else:
            duration = _safe_duration(duration)
            if duration is None:
                return None
            event["duration_seconds"] = duration
    elif event_type == "company":
        name = _safe_company_name(event["name"])
        if name is None or event["status"] not in SCAN_STATUSES:
            return None
        if event["reason"] not in SCAN_REASONS:
            return None
        if event["status"] == "complete" and event["reason"] != "complete":
            return None
        if event["status"] != "complete" and event["reason"] == "complete":
            return None
        event["name"] = name
    elif event_type == "scan_summary":
        if event["coverage_health"] not in COVERAGE_HEALTH:
            return None
        duration = _safe_duration(event["scan_runtime_seconds"])
        if duration is None:
            return None
        event["scan_runtime_seconds"] = duration
        attempted = event["attempted_companies"]
        states = (
            event["complete_companies"]
            + event["incomplete_companies"]
            + event["failed_companies"]
        )
        if attempted > event["configured_companies"] or states != attempted:
            return None
        if event["retained_jobs"] > event["normalized_jobs"]:
            return None
    elif event_type == "run_summary":
        duration = _safe_duration(event["runtime_seconds"])
        if duration is None:
            return None
        event["runtime_seconds"] = duration
    return event


def classify_scan_reason(result: Any) -> str:
    """Map authoritative result diagnostics to a fixed public reason code."""

    status = str(getattr(result, "scan_health", "failed") or "failed").casefold()
    if status == "complete":
        return "complete"
    tokens = " ".join(
        str(getattr(result, field, "") or "").casefold()
        for field in (
            "source_health_code",
            "scan_completeness_reason",
            "stop_reason",
            "error_type",
        )
    )
    if "cooldown" in tokens:
        return "cooldown_skipped"
    if "discovery_only" in tokens or "healthy_discovery_only" in tokens:
        return "discovery_only"
    if "timeout" in tokens or "timed_out" in tokens:
        return "timeout"
    if "429" in tokens or "rate_limit" in tokens:
        return "rate_limited"
    if "safety_limit" in tokens or "requires_partition" in tokens or "max_page" in tokens:
        return "pagination_safety_limit"
    if "collection_changed" in tokens or "mutation" in tokens:
        return "collection_changed"
    if "manual_review" in tokens:
        return "manual_review_required"
    if "not_implemented" in tokens or "unsupported" in tokens:
        return "unsupported_source"
    if any(marker in tokens for marker in ("authentication", "blocked", "captcha", "challenge")):
        return "access_blocked"
    if "browser" in tokens:
        return "browser_failure"
    if "transient_upstream_http" in tokens:
        return "transient_http_failure"
    if "identity" in tokens or "redirect" in tokens or "host_mismatch" in tokens:
        return "source_identity_failure"
    if any(marker in tokens for marker in ("inventory", "hydration", "item_failure", "accounting")):
        return "inventory_accounting_failure"
    if any(marker in tokens for marker in ("malformed", "schema", "invalid_response", "unexpected_response")):
        return "invalid_response"
    if any(marker in tokens for marker in ("terminal", "pagination_unproven", "unproven_empty", "not_established", "exhaustion_not_proven")):
        return "missing_terminal_evidence"
    if any(marker in tokens for marker in ("http_5", "http 5", "server_error")):
        return "http_server_failure"
    if any(marker in tokens for marker in ("http_4", "http 4", "protocol_failure")):
        return "http_client_failure"
    if any(marker in tokens for marker in ("network", "connection", "request_error")):
        return "network_failure"
    if "source_unreachable" in tokens or "source_stale" in tokens:
        return "source_unreachable"
    return "incomplete_other" if status == "incomplete" else "scan_failure_other"


def classify_stage_failure(exc: BaseException) -> str:
    """Classify an exception without inspecting or publishing its message."""

    name = type(exc).__name__.casefold()
    diagnostics = getattr(exc, "diagnostics", None)
    classification = (
        str(diagnostics.get("classification") or "").casefold()
        if isinstance(diagnostics, Mapping)
        else ""
    )
    if "configuration" in name:
        return "configuration_error"
    if "timeout" in name or classification == "timeout":
        return "timeout"
    if classification == "network" or "connection" in name:
        return "connection_failure"
    if classification == "server":
        return "http_server_failure"
    if classification == "permanent":
        return "http_client_failure"
    if "authentication" in name or "permission" in name:
        return "authentication_failure"
    if "accesserror" in name:
        return "integration_access_failure"
    if "json" in name or "decode" in name:
        return "invalid_response"
    if isinstance(exc, (AssertionError, RuntimeError, ValueError, TypeError)):
        return "internal_failure"
    return "unknown_failure"


class PublicCIReporter:
    """Emit validated public events to a private, wrapper-owned side channel."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._reported_sources: set[tuple[str, str]] = set()

    @classmethod
    def from_environment(cls) -> "PublicCIReporter":
        raw_path = os.environ.get(PUBLIC_REPORT_PATH_VARIABLE)
        return cls(Path(raw_path) if raw_path else None)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def emit(self, event_type: str, **fields: Any) -> bool:
        if self.path is None:
            return False
        event = validate_public_event({"v": REPORT_VERSION, "type": event_type, **fields})
        if event is None:
            return False
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            return False
        try:
            with self._lock:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND)
                with os.fdopen(descriptor, "ab") as report_file:
                    report_file.write(payload)
                    report_file.flush()
            return True
        except OSError:
            # Reporting is observational and must never mask the operational
            # result or change lifecycle/persistence behavior.
            return False

    def run_start(self, configured_companies: int) -> None:
        self.emit("run_start", configured_companies=int(configured_companies))

    @contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        started = time.monotonic()
        self.emit(
            "stage",
            stage=stage,
            status="started",
            failure_category=None,
            duration_seconds=None,
        )
        try:
            yield
        except BaseException as exc:
            self.emit(
                "stage",
                stage=stage,
                status="failed",
                failure_category=classify_stage_failure(exc),
                duration_seconds=time.monotonic() - started,
            )
            raise
        else:
            self.emit(
                "stage",
                stage=stage,
                status="succeeded",
                failure_category=None,
                duration_seconds=time.monotonic() - started,
            )

    def skipped_stage(self, stage: str) -> None:
        self.emit(
            "stage",
            stage=stage,
            status="skipped",
            failure_category=None,
            duration_seconds=0.0,
        )

    def company_result(self, result: Any) -> None:
        name = str(getattr(getattr(result, "target", None), "name", "") or "")
        scope = str(getattr(getattr(result, "target", None), "source_scope", "") or "")
        key = (name.casefold(), scope.casefold())
        with self._lock:
            if key in self._reported_sources:
                return
            self._reported_sources.add(key)
        status = str(getattr(result, "scan_health", "failed") or "failed").casefold()
        if status not in SCAN_STATUSES:
            status = "failed"
        self.emit(
            "company",
            name=name,
            status=status,
            jobs_fetched=max(
                0,
                int(
                    getattr(result, "fetched_count", 0)
                    or getattr(result, "jobs_collected", 0)
                    or 0
                ),
            ),
            jobs_retained=max(0, int(getattr(result, "matched_count", 0) or 0)),
            reason=classify_scan_reason(result),
        )


def read_public_event_line(line: bytes) -> dict[str, Any] | None:
    """Decode and validate one bounded JSON-lines record."""

    if not line or len(line) > MAX_EVENT_BYTES or not line.endswith(b"\n"):
        return None
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return validate_public_event(value)
