"""Supabase persistence for retained, student-ranked company-careers jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_CLOSE_AFTER_MISSING_RUNS = 2
DEFAULT_SYNC_MAX_ATTEMPTS = 3
DEFAULT_SYNC_RETRY_DELAY_SECONDS = 0.5
DEFAULT_SUPABASE_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_LIFECYCLE_PAGE_SIZE = 1000
DEFAULT_DASHBOARD_PAGE_SIZE = 1000
SYNC_SCAN_NAMESPACE = uuid.UUID("fe27c6e0-e7a3-4d18-8e57-554e117f56c6")
TRACKING_QUERY_PARAMETERS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "gbraid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "twclid",
        "wbraid",
    }
)


class SupabaseConfigurationError(RuntimeError):
    """Raised when the Supabase backend cannot be configured safely."""


class SupabaseAccessError(RuntimeError):
    """Raised when the configured Supabase backend cannot be used safely."""

    def __init__(self, message: str, *, diagnostics: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class SupabaseProviderError(RuntimeError):
    """Credential-safe provider cause chained beneath SupabaseAccessError."""

    def __init__(self, diagnostics: Mapping[str, Any]):
        self.diagnostics = dict(diagnostics)
        super().__init__(json.dumps(self.diagnostics, sort_keys=True, ensure_ascii=False))


@dataclass(frozen=True)
class SyncResult:
    inserted: int
    updated: int
    unchanged: int
    missing_incremented: int = 0
    closed: int = 0


@dataclass(frozen=True)
class SyncDiagnostics:
    scan_id: str
    operation: str
    source_count: int
    payload_rows: int
    payload_bytes: int
    elapsed_seconds: float
    request_timeout_seconds: float


@dataclass(frozen=True)
class DashboardData:
    active_jobs: list[dict[str, Any]]
    latest_scan: Optional[dict[str, Any]]


def normalize_url(value: Any) -> str:
    """Normalize inconsequential URL variations without hiding material changes."""
    raw_url = str(value or "").strip()
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"

    user_info = ""
    if parsed.username:
        user_info = parsed.username
        if parsed.password:
            user_info += f":{parsed.password}"
        user_info += "@"
    netloc = f"{user_info}{hostname}"

    path = parsed.path.rstrip("/")
    query_items = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_PARAMETERS:
            continue
        query_items.append((key, item_value))
    query_items.sort(key=lambda item: (item[0].casefold(), item[1]))

    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


def generate_source_key(job: Mapping[str, Any] | Any) -> str:
    """Build a deterministic key using external ID, URL, then a SHA-256 fallback."""
    values = _job_mapping(job)
    external_id = str(values.get("job_id") or "").strip()
    source = _normalize_text(values.get("source"))

    if _is_genuine_external_id(external_id):
        namespace = source or _normalize_text(values.get("company")) or "unknown"
        return f"id:{namespace}:{unicodedata.normalize('NFKC', external_id)}"

    normalized_url = normalize_url(
        values.get("source_url")
        or values.get("apply_url")
        or values.get("url")
    )
    if normalized_url:
        return f"url:{normalized_url}"

    fallback_fields = [
        _normalize_text(values.get("company")),
        _normalize_text(values.get("title")),
        _normalize_text(values.get("location")),
        normalized_url,
    ]
    digest_input = json.dumps(
        fallback_fields,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(digest_input).hexdigest()}"


def tracked_job_payload(
    job: Mapping[str, Any] | Any,
    *,
    seen_at: datetime,
) -> dict[str, Any]:
    """Map one ranked in-memory job into the tracked_jobs RPC payload."""
    values = _job_mapping(job)
    matched_categories = _string_list(values.get("matched_categories"))
    source = str(values.get("source") or "company_careers").strip()
    departments = _string_list(values.get("departments"))
    job_url = str(
        values.get("source_url")
        or values.get("apply_url")
        or values.get("url")
        or ""
    ).strip()
    lifecycle_status = str(values.get("lifecycle_status") or "active").strip()
    first_seen_at = _normalized_timestamp(values.get("first_seen_at"))
    last_seen_at = _normalized_timestamp(values.get("last_seen_at"))

    return {
        "source_key": generate_source_key(values),
        "company": str(values.get("company") or "").strip(),
        "title": str(values.get("title") or "").strip(),
        "location": str(values.get("location") or "").strip() or None,
        "department": "; ".join(departments) or None,
        "apply_url": job_url or None,
        "source_url": job_url or None,
        "source_platform": source.split(":", 1)[0],
        "primary_category": matched_categories[0] if matched_categories else None,
        "matched_categories": matched_categories,
        "relevance_score": int(values.get("relevance_score", values.get("score", 0)) or 0),
        "relevance_band": str(values.get("relevance_band") or "").strip() or None,
        "positive_signals": _string_list(values.get("positive_signals")),
        "negative_signals": _string_list(values.get("negative_signals")),
        "location_status": str(values.get("location_status") or "").strip() or None,
        "source_updated_at": _normalized_timestamp(values.get("updated_at")),
        "first_seen_at": first_seen_at or _iso_timestamp(seen_at),
        "last_seen_at": last_seen_at or _iso_timestamp(seen_at),
        "first_seen_basis": _optional_text(values.get("first_seen_basis")) or "observed_at_scan",
        "source_posted_at": _optional_text(values.get("source_posted_at")),
        "source_closes_at": _optional_text(
            values.get("source_closes_at") or values.get("source_deadline_at")
        ),
        "removed_at": _normalized_timestamp(values.get("removed_at")),
        "status": "closed" if lifecycle_status == "removed" else "active",
        "lifecycle_status": lifecycle_status,
        "missing_successful_runs": int(values.get("missing_scan_count", 0) or 0),
        "freshness_age_source": _optional_text(values.get("freshness_age_source")),
        "age_days": _optional_float(values.get("age_days")),
        "first_seen_age_hours": _optional_float(values.get("first_seen_age_hours")),
        "freshness_band": _optional_text(values.get("freshness_band")),
        "deadline_days_remaining": _optional_float(values.get("deadline_days_remaining")),
        "urgency_band": _optional_text(values.get("urgency_band")),
        "urgency_score": int(values.get("urgency_score", 0) or 0),
        "urgency_reasons": _string_list(values.get("urgency_reasons")),
        "date_parse_errors": _string_list(values.get("date_parse_errors")),
        "is_ranked_output": bool(values.get("is_ranked_output", True)),
        # last_seen_at advances on every successful rediscovery and has a
        # dedicated column. Excluding it keeps otherwise identical jobs in the
        # RPC's unchanged count while the database still advances last_seen_at.
        "raw_data": _json_safe(
            {key: value for key, value in values.items() if key != "last_seen_at"}
        ),
    }


class SupabaseJobStore:
    """Supabase client wrapper for source-atomic student-job persistence."""

    def __init__(
        self,
        client: Any,
        *,
        close_after_missing_runs: int = DEFAULT_CLOSE_AFTER_MISSING_RUNS,
        now_factory: Callable[[], datetime] | None = None,
        request_timeout_seconds: float = DEFAULT_SUPABASE_REQUEST_TIMEOUT_SECONDS,
        sync_max_attempts: int = DEFAULT_SYNC_MAX_ATTEMPTS,
        sync_retry_delay_seconds: float = DEFAULT_SYNC_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        redaction_secrets: Iterable[str] = (),
    ) -> None:
        if close_after_missing_runs < 1:
            raise SupabaseConfigurationError("CLOSE_AFTER_MISSING_RUNS must be at least 1")
        self.client = client
        self.close_after_missing_runs = close_after_missing_runs
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.sync_max_attempts = int(sync_max_attempts)
        self.sync_retry_delay_seconds = float(sync_retry_delay_seconds)
        self._sleep = sleep
        self._redaction_secrets = tuple(str(value) for value in redaction_secrets if value)
        self.last_sync_diagnostics: Optional[SyncDiagnostics] = None
        self._last_scan_id: Optional[str] = None
        self._last_scan_diagnostics: dict[str, Any] = {}
        if self.request_timeout_seconds <= 0:
            raise SupabaseConfigurationError("SUPABASE_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.sync_max_attempts < 1:
            raise SupabaseConfigurationError("SUPABASE_SYNC_MAX_ATTEMPTS must be at least 1")
        if self.sync_retry_delay_seconds < 0:
            raise SupabaseConfigurationError(
                "SUPABASE_SYNC_RETRY_DELAY_SECONDS cannot be negative"
            )

    @classmethod
    def from_env(
        cls,
        *,
        environ: Optional[Mapping[str, str]] = None,
        client_factory: Optional[Callable[[str, str], Any]] = None,
        now_factory: Callable[[], datetime] | None = None,
        close_after_missing_runs: int | None = None,
    ) -> "SupabaseJobStore":
        """Create a client only when explicitly called, never during module import."""
        environment = environ if environ is not None else os.environ
        url = str(environment.get("SUPABASE_URL") or "").strip()
        service_role_key = str(
            environment.get("SUPABASE_SERVICE_ROLE_KEY") or ""
        ).strip()
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", url),
                ("SUPABASE_SERVICE_ROLE_KEY", service_role_key),
            )
            if not value
        ]
        if missing:
            raise SupabaseConfigurationError(
                "Missing required Supabase environment variable(s): "
                + ", ".join(missing)
            )

        threshold_text = str(
            close_after_missing_runs
            if close_after_missing_runs is not None
            else environment.get(
                "CLOSE_AFTER_MISSING_RUNS",
                DEFAULT_CLOSE_AFTER_MISSING_RUNS,
            )
        ).strip()
        try:
            threshold = int(threshold_text)
        except ValueError as exc:
            raise SupabaseConfigurationError(
                "CLOSE_AFTER_MISSING_RUNS must be an integer of at least 1"
            ) from exc
        if threshold < 1:
            raise SupabaseConfigurationError(
                "CLOSE_AFTER_MISSING_RUNS must be an integer of at least 1"
            )

        if client_factory is None:
            try:
                from supabase import create_client
                from supabase.lib.client_options import SyncClientOptions
            except ImportError as exc:
                raise SupabaseConfigurationError(
                    "The 'supabase' package is required to create the Supabase client"
                ) from exc
            request_timeout = _positive_float_setting(
                environment,
                "SUPABASE_REQUEST_TIMEOUT_SECONDS",
                DEFAULT_SUPABASE_REQUEST_TIMEOUT_SECONDS,
            )
            try:
                client = create_client(
                    url,
                    service_role_key,
                    options=SyncClientOptions(postgrest_client_timeout=request_timeout),
                )
            except Exception:
                raise SupabaseConfigurationError(
                    "Unable to create the Supabase client; verify the backend configuration"
                ) from None
        else:
            request_timeout = _positive_float_setting(
                environment,
                "SUPABASE_REQUEST_TIMEOUT_SECONDS",
                DEFAULT_SUPABASE_REQUEST_TIMEOUT_SECONDS,
            )
            try:
                client = client_factory(url, service_role_key)
            except Exception:
                raise SupabaseConfigurationError(
                    "Unable to create the Supabase client; verify the backend configuration"
                ) from None

        return cls(
            client,
            close_after_missing_runs=threshold,
            now_factory=now_factory,
            request_timeout_seconds=request_timeout,
            sync_max_attempts=_positive_integer_setting(
                environment, "SUPABASE_SYNC_MAX_ATTEMPTS", DEFAULT_SYNC_MAX_ATTEMPTS
            ),
            sync_retry_delay_seconds=_nonnegative_float_setting(
                environment,
                "SUPABASE_SYNC_RETRY_DELAY_SECONDS",
                DEFAULT_SYNC_RETRY_DELAY_SECONDS,
            ),
            redaction_secrets=(service_role_key,),
        )

    def validate_setup(self) -> None:
        """Verify dashboard tables and backend access without writing any data."""
        checks = (
            ("tracked_jobs", "id"),
            ("scan_runs", "id,failure_stage,diagnostics"),
            ("scan_source_syncs", "scan_id"),
            ("applications", "job_id"),
        )
        try:
            for table_name, column in checks:
                response = (
                    self.client.table(table_name)
                    .select(column)
                    .limit(1)
                    .execute()
                )
                if not isinstance(getattr(response, "data", None), list):
                    raise RuntimeError("Supabase setup check returned an invalid response")
        except Exception as exc:
            raise _supabase_access_error(
                exc, operation="setup validation", secrets=self._redaction_secrets
            ) from None

    def sync_student_jobs(
        self,
        jobs: Iterable[Mapping[str, Any] | Any],
        *,
        successful_companies: Iterable[str],
        started_at: datetime,
        companies_checked: int,
        jobs_fetched: int,
        lifecycle_jobs: Optional[Iterable[Mapping[str, Any] | Any]] = None,
        exclusion_counts: Optional[Mapping[str, int]] = None,
        coverage_report_path: Optional[str] = None,
        ranked_artifact_path: Optional[str] = None,
        normalized_jobs: int = 0,
        source_summary: Optional[Mapping[str, Any]] = None,
        scan_runtime_seconds: Optional[float] = None,
    ) -> SyncResult:
        """Synchronize each source atomically, then record the successful scan."""
        seen_at = _ensure_aware(self._now_factory())
        jobs_by_key: MutableMapping[str, dict[str, Any]] = {}
        for job in lifecycle_jobs or ():
            values = _job_mapping(job)
            payload = tracked_job_payload(
                {**values, "is_ranked_output": False}, seen_at=seen_at
            )
            self._merge_job_payload(jobs_by_key, payload, ranked=False)
        ranked_count = 0
        for job in jobs:
            values = _job_mapping(job)
            payload = tracked_job_payload(
                {**values, "is_ranked_output": True}, seen_at=seen_at
            )
            if not jobs_by_key.get(payload["source_key"], {}).get("is_ranked_output"):
                ranked_count += 1
            self._merge_job_payload(jobs_by_key, payload, ranked=True)

        scan = {
            "started_at": _iso_timestamp(started_at),
            "completed_at": _iso_timestamp(seen_at),
            "companies_checked": int(companies_checked),
            "jobs_fetched": int(jobs_fetched),
            "jobs_retained": ranked_count,
            "exclusion_counts": {
                str(key): int(value)
                for key, value in (exclusion_counts or {}).items()
            },
            "coverage_report_path": coverage_report_path,
            "ranked_artifact_path": ranked_artifact_path,
        }
        complete_companies = {
            str(company).strip()
            for company in successful_companies
            if str(company).strip()
        }
        rows_by_company: dict[str, list[dict[str, Any]]] = {}
        for payload in jobs_by_key.values():
            rows_by_company.setdefault(str(payload["company"]), []).append(payload)
        companies = sorted(set(rows_by_company) | complete_companies)
        scan_id = str(uuid.uuid5(SYNC_SCAN_NAMESPACE, scan["started_at"]))
        totals = SyncResult(0, 0, 0, 0, 0)
        total_bytes = 0
        sync_started = time.perf_counter()
        for company in companies:
            params = {
                "p_scan_id": scan_id,
                "p_company": company,
                "p_jobs": rows_by_company.get(company, []),
                "p_reconcile_absence": company in complete_companies,
                "p_close_after_missing_runs": self.close_after_missing_runs,
                "p_seen_at": _iso_timestamp(seen_at),
            }
            payload_bytes = _serialized_bytes(params)
            total_bytes += payload_bytes
            data = self._execute_source_sync(
                params,
                company=company,
                row_count=len(params["p_jobs"]),
                payload_bytes=payload_bytes,
            )
            totals = SyncResult(
                totals.inserted + int(data.get("inserted", 0)),
                totals.updated + int(data.get("updated", 0)),
                totals.unchanged + int(data.get("unchanged", 0)),
                totals.missing_incremented + int(data.get("missing_incremented", 0)),
                totals.closed + int(data.get("closed", 0)),
            )

        elapsed = time.perf_counter() - sync_started
        self.last_sync_diagnostics = SyncDiagnostics(
            scan_id=scan_id,
            operation="source-scoped job synchronization",
            source_count=len(companies),
            payload_rows=len(jobs_by_key),
            payload_bytes=total_bytes,
            elapsed_seconds=round(elapsed, 3),
            request_timeout_seconds=self.request_timeout_seconds,
        )
        run_diagnostics = {
            "scan_success": True,
            "publication_status": "pending",
            "normalized_jobs": int(normalized_jobs),
            "ranked_jobs": int(ranked_count),
            "source_summary": _json_safe(dict(source_summary or {})),
            "scan_runtime_seconds": (
                round(float(scan_runtime_seconds), 3)
                if scan_runtime_seconds is not None else None
            ),
            "missing_incremented": totals.missing_incremented,
            "closed": totals.closed,
        }
        self._record_scan_run(
            scan_id=scan_id,
            scan=scan,
            result=totals,
            success=True,
            diagnostics=run_diagnostics,
        )
        self._last_scan_id = scan_id
        self._last_scan_diagnostics = run_diagnostics
        return totals

    @staticmethod
    def _merge_job_payload(
        jobs_by_key: MutableMapping[str, dict[str, Any]],
        payload: dict[str, Any],
        *,
        ranked: bool,
    ) -> None:
        """Merge lifecycle/ranked views while rejecting ambiguous identities."""
        source_key = str(payload["source_key"])
        existing = jobs_by_key.get(source_key)
        if existing is not None:
            identity_fields = ("company", "title", "source_url")
            existing_identity = tuple(existing.get(field) for field in identity_fields)
            incoming_identity = tuple(payload.get(field) for field in identity_fields)
            if existing_identity != incoming_identity:
                raise ValueError(
                    "Conflicting jobs resolved to the same source identity: "
                    f"{source_key}"
                )
            if ranked and existing.get("is_ranked_output") and existing != payload:
                raise ValueError(
                    "Duplicate ranked jobs have inconsistent payloads for source identity: "
                    f"{source_key}"
                )
        if existing is None or ranked:
            jobs_by_key[source_key] = payload

    def _execute_source_sync(
        self,
        params: Mapping[str, Any],
        *,
        company: str,
        row_count: int,
        payload_bytes: int,
    ) -> Mapping[str, Any]:
        operation = "source-scoped job synchronization"
        for attempt in range(1, self.sync_max_attempts + 1):
            started = time.perf_counter()
            try:
                response = self.client.rpc("sync_student_job_source", dict(params)).execute()
            except Exception as exc:
                elapsed = time.perf_counter() - started
                error = _supabase_access_error(
                    exc,
                    operation=operation,
                    source=company,
                    payload_rows=row_count,
                    payload_bytes=payload_bytes,
                    elapsed_seconds=elapsed,
                    request_timeout_seconds=self.request_timeout_seconds,
                    attempt=attempt,
                    secrets=self._redaction_secrets,
                )
                if error.diagnostics.get("transient") and attempt < self.sync_max_attempts:
                    self._sleep(self.sync_retry_delay_seconds * (2 ** (attempt - 1)))
                    continue
                raise error from SupabaseProviderError(error.diagnostics)
            data = getattr(response, "data", None)
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
                data = data[0]
            if not isinstance(data, Mapping):
                raise RuntimeError("Supabase source sync returned an invalid response")
            return data
        raise AssertionError("unreachable")

    def record_failed_scan(
        self,
        *,
        started_at: datetime,
        error_message: str,
        companies_checked: int = 0,
        jobs_fetched: int = 0,
        jobs_retained: int = 0,
        exclusion_counts: Optional[Mapping[str, int]] = None,
        coverage_report_path: Optional[str] = None,
        ranked_artifact_path: Optional[str] = None,
        stage: str = "scan",
        normalized_jobs: int = 0,
        source_summary: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Record an overall failed scan without changing any tracked job state."""
        completed_at = _ensure_aware(self._now_factory())
        scan_id = str(uuid.uuid5(SYNC_SCAN_NAMESPACE, _iso_timestamp(started_at)))
        safe_message = _sanitize_text(
            error_message,
            secrets=(*_configured_secrets(), *self._redaction_secrets),
        )[:4000]
        diagnostics = {
            "stage": str(stage),
            "normalized_jobs": int(normalized_jobs),
            "ranked_jobs": int(jobs_retained),
            "source_summary": _json_safe(dict(source_summary or {})),
        }
        try:
            self.client.table("scan_runs").upsert(
                {
                    "id": scan_id,
                    "started_at": _iso_timestamp(started_at),
                    "completed_at": _iso_timestamp(completed_at),
                    "success": False,
                    "companies_checked": int(companies_checked),
                    "jobs_fetched": int(jobs_fetched),
                    "jobs_retained": int(jobs_retained),
                    "jobs_inserted": 0,
                    "jobs_updated": 0,
                    "jobs_unchanged": 0,
                    "exclusion_counts": {
                        str(key): int(value)
                        for key, value in (exclusion_counts or {}).items()
                    },
                    "coverage_report_path": coverage_report_path,
                    "ranked_artifact_path": ranked_artifact_path,
                    "error_message": safe_message,
                    "failure_stage": str(stage),
                    "diagnostics": diagnostics,
                },
                on_conflict="id",
            ).execute()
        except Exception as exc:
            error = _supabase_access_error(
                exc,
                operation="failed scan-run recording",
                secrets=self._redaction_secrets,
            )
            raise error from SupabaseProviderError(error.diagnostics)

    def _record_scan_run(
        self,
        *,
        scan_id: str,
        scan: Mapping[str, Any],
        result: SyncResult,
        success: bool,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> None:
        row = {
            "id": scan_id,
            **dict(scan),
            "success": bool(success),
            "jobs_inserted": result.inserted,
            "jobs_updated": result.updated,
            "jobs_unchanged": result.unchanged,
            "failure_stage": None,
            "diagnostics": _json_safe(
                dict(diagnostics or {
                    "missing_incremented": result.missing_incremented,
                    "closed": result.closed,
                })
            ),
            "error_message": None,
        }
        try:
            self.client.table("scan_runs").upsert(row, on_conflict="id").execute()
        except Exception as exc:
            error = _supabase_access_error(
                exc,
                operation="successful scan-run recording",
                secrets=self._redaction_secrets,
            )
            raise error from SupabaseProviderError(error.diagnostics)

    def record_publication_result(
        self,
        *,
        started_at: datetime,
        status: str,
        success: bool,
        runtime_seconds: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Finalize publication state without overwriting successful scan counts."""
        scan_id = str(uuid.uuid5(SYNC_SCAN_NAMESPACE, _iso_timestamp(started_at)))
        diagnostics = dict(self._last_scan_diagnostics)
        diagnostics.update(
            {
                "scan_success": True,
                "publication_status": str(status),
                "runtime_seconds": (
                    round(float(runtime_seconds), 3)
                    if runtime_seconds is not None else None
                ),
            }
        )
        safe_message = (
            _sanitize_text(
                error_message,
                secrets=(*_configured_secrets(), *self._redaction_secrets),
            )[:4000]
            if error_message else None
        )
        values = {
            "completed_at": _iso_timestamp(_ensure_aware(self._now_factory())),
            "success": bool(success),
            "failure_stage": None if success else "google_sheets_publication",
            "diagnostics": _json_safe(diagnostics),
            "error_message": safe_message,
        }
        try:
            response = (
                self.client.table("scan_runs")
                .update(values)
                .eq("id", scan_id)
                .execute()
            )
            data = getattr(response, "data", None)
            if not isinstance(data, list) or len(data) != 1:
                raise RuntimeError("Supabase publication status update matched no scan run")
        except Exception as exc:
            error = _supabase_access_error(
                exc,
                operation="publication status recording",
                secrets=self._redaction_secrets,
            )
            raise error from SupabaseProviderError(error.diagnostics)
        self._last_scan_id = scan_id
        self._last_scan_diagnostics = diagnostics

    def fetch_dashboard_data(self) -> DashboardData:
        """Read active jobs and the latest scan without touching applications."""
        columns = (
            "source_key,company,title,location,apply_url,primary_category,"
            "matched_categories,relevance_score,relevance_band,positive_signals,"
            "location_status,source_updated_at,source_posted_at,source_closes_at,"
            "first_seen_at,last_seen_at,first_seen_basis,first_seen_age_hours,"
            "removed_at,status,lifecycle_status,"
            "missing_successful_runs,freshness_age_source,age_days,freshness_band,"
            "deadline_days_remaining,urgency_band,urgency_score,urgency_reasons,"
            "date_parse_errors,is_ranked_output,raw_data"
        )
        jobs_data: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            try:
                jobs_response = (
                    self.client.table("tracked_jobs")
                    .select(columns)
                    .eq("status", "active")
                    .eq("is_ranked_output", True)
                    .order("relevance_score", desc=True)
                    .order("first_seen_at", desc=True)
                    .order("source_key", desc=False)
                    .range(offset, offset + DEFAULT_DASHBOARD_PAGE_SIZE - 1)
                    .execute()
                )
            except Exception as exc:
                raise _supabase_access_error(
                    exc, operation="active-jobs query", secrets=self._redaction_secrets
                ) from None
            page = getattr(jobs_response, "data", None)
            if not isinstance(page, list) or not all(
                isinstance(row, Mapping) for row in page
            ):
                raise RuntimeError("Supabase active-jobs query returned an invalid response")
            jobs_data.extend(page)
            if len(page) < DEFAULT_DASHBOARD_PAGE_SIZE:
                break
            offset += DEFAULT_DASHBOARD_PAGE_SIZE

        source_keys = [str(row.get("source_key") or "") for row in jobs_data]
        if len(source_keys) != len(set(source_keys)):
            raise RuntimeError("Supabase active-jobs pagination returned duplicate rows")

        try:
            scan_response = (
                self.client.table("scan_runs")
                .select("*")
                .order("completed_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise _supabase_access_error(
                exc, operation="latest-scan query", secrets=self._redaction_secrets
            ) from None
        scan_data = getattr(scan_response, "data", None)
        if not isinstance(scan_data, list) or not all(
            isinstance(row, Mapping) for row in scan_data
        ):
            raise RuntimeError("Supabase latest-scan query returned an invalid response")

        active_jobs = [dict(row) for row in jobs_data]
        for row in active_jobs:
            row["child_brands"] = _dashboard_child_brands(row)
        return DashboardData(
            active_jobs=active_jobs,
            latest_scan=dict(scan_data[0]) if scan_data else None,
        )

    def fetch_lifecycle_state(self) -> dict[str, Any]:
        """Load canonical lifecycle history from the existing tracked_jobs store."""
        columns = (
            "source_key,company,title,location,apply_url,source_url,source_platform,"
            "first_seen_at,last_seen_at,first_seen_basis,source_posted_at,source_closes_at,"
            "removed_at,lifecycle_status,missing_successful_runs,raw_data"
        )
        data: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            try:
                response = (
                    self.client.table("tracked_jobs")
                    .select(columns)
                    .range(offset, offset + DEFAULT_LIFECYCLE_PAGE_SIZE - 1)
                    .execute()
                )
            except Exception as exc:
                raise _supabase_access_error(
                    exc,
                    operation="lifecycle-state query",
                    secrets=self._redaction_secrets,
                ) from None
            page = getattr(response, "data", None)
            if not isinstance(page, list) or not all(
                isinstance(row, Mapping) for row in page
            ):
                raise RuntimeError("Supabase lifecycle-state query returned an invalid response")
            data.extend(page)
            if len(page) < DEFAULT_LIFECYCLE_PAGE_SIZE:
                break
            offset += DEFAULT_LIFECYCLE_PAGE_SIZE
        jobs: dict[str, dict[str, Any]] = {}
        latest: Optional[str] = None
        for raw_row in data:
            row = dict(raw_row)
            raw_data = row.get("raw_data")
            values = {**(dict(raw_data) if isinstance(raw_data, Mapping) else {}), **row}
            source_key = str(row.get("source_key") or "")
            if not source_key:
                continue
            values["source"] = values.get("source") or values.get("source_platform") or ""
            values["url"] = values.get("source_url") or values.get("apply_url") or ""
            values["missing_scan_count"] = int(row.get("missing_successful_runs", 0) or 0)
            values["reappearance_count"] = int(values.get("reappearance_count", 0) or 0)
            values["date_parse_errors"] = list(values.get("date_parse_errors") or [])
            jobs[source_key] = values
            last_seen = str(row.get("last_seen_at") or "")
            latest = max(latest or last_seen, last_seen) if last_seen else latest
        return {"schema_version": 1, "updated_at": latest, "jobs": jobs}


def _job_mapping(job: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(job, Mapping):
        return dict(job)
    to_dict = getattr(job, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if is_dataclass(job):
        return asdict(job)
    raise TypeError("job must be a mapping or expose to_dict()")


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.casefold().split())


def _supabase_access_error(
    exc: Exception,
    *,
    operation: str,
    source: Optional[str] = None,
    payload_rows: Optional[int] = None,
    payload_bytes: Optional[int] = None,
    elapsed_seconds: Optional[float] = None,
    request_timeout_seconds: Optional[float] = None,
    attempt: Optional[int] = None,
    secrets: Iterable[str] = (),
) -> SupabaseAccessError:
    """Return actionable provider diagnostics without credential-bearing values."""
    secrets = (*_configured_secrets(), *(str(value) for value in secrets if value))
    raw_message = getattr(exc, "message", None) or str(exc)
    message = _sanitize_text(raw_message, secrets=secrets)
    code = _sanitize_text(getattr(exc, "code", None), secrets=secrets) or None
    details = _sanitize_text(getattr(exc, "details", None), secrets=secrets) or None
    hint = _sanitize_text(getattr(exc, "hint", None), secrets=secrets) or None
    response = getattr(exc, "response", None)
    http_status = getattr(response, "status_code", None)
    if http_status is None and code and len(code) == 3 and code.isdigit():
        numeric_code = int(code)
        if 400 <= numeric_code <= 599:
            http_status = numeric_code
    response_body = None
    if response is not None:
        try:
            response_body = getattr(response, "text", None)
        except Exception:
            response_body = None
    excerpt = _sanitize_text(response_body or details, secrets=secrets)[:1000] or None
    classification, phase, transient = _classify_provider_failure(
        exc, code=code, http_status=http_status, message=message
    )
    diagnostics = {
        key: value
        for key, value in {
            "operation": operation,
            "source": source,
            "classification": classification,
            "phase": phase,
            "http_status": http_status,
            "provider_code": code,
            "provider_message": message[:500] or type(exc).__name__,
            "provider_hint": hint[:500] if hint else None,
            "response_excerpt": excerpt,
            "payload_rows": payload_rows,
            "payload_bytes": payload_bytes,
            "elapsed_seconds": round(float(elapsed_seconds), 3)
            if elapsed_seconds is not None else None,
            "request_timeout_seconds": request_timeout_seconds,
            "attempt": attempt,
            "transient": transient,
        }.items()
        if value is not None
    }
    folded = message.casefold()
    schema_markers = (
        "pgrst202",
        "pgrst205",
        "could not find the function",
        "could not find the table",
        "does not exist",
        "schema cache",
    )
    if any(marker in folded for marker in schema_markers):
        return SupabaseAccessError(
            "Supabase dashboard schema is unavailable; run sql/supabase_schema.sql "
            "in the Supabase SQL editor and retry",
            diagnostics=diagnostics,
        )
    summary_parts = [
        f"classification={classification}",
        f"phase={phase}",
    ]
    if source:
        summary_parts.append(f"source={_sanitize_text(source, secrets=secrets)!r}")
    if http_status is not None:
        summary_parts.append(f"http_status={http_status}")
    if code:
        summary_parts.append(f"provider_code={code}")
    if payload_rows is not None:
        summary_parts.append(f"payload_rows={payload_rows}")
    if payload_bytes is not None:
        summary_parts.append(f"payload_bytes={payload_bytes}")
    if elapsed_seconds is not None:
        summary_parts.append(f"elapsed_seconds={elapsed_seconds:.3f}")
    summary_parts.append(f"provider_message={message[:500]!r}")
    return SupabaseAccessError(
        f"Supabase access failed during {operation}; " + ", ".join(summary_parts),
        diagnostics=diagnostics,
    )


def _classify_provider_failure(
    exc: Exception,
    *,
    code: Optional[str],
    http_status: Optional[int],
    message: str,
) -> tuple[str, str, bool]:
    name = type(exc).__name__.casefold()
    folded = message.casefold()
    if code == "57014" or "timeout" in name or "timed out" in folded:
        return "timeout", "during_or_after_transmission", True
    if "connect" in name or "dns" in folded or "name resolution" in folded:
        return "network", "before_transmission", True
    if "network" in folded or "protocol" in name or "connection" in folded:
        return "network", "during_or_after_transmission", True
    if http_status is not None:
        if http_status == 429 or http_status >= 500:
            return "server", "after_server_response", True
        return "permanent", "after_server_response", False
    if code and (code.startswith("PGRST") or re.fullmatch(r"[0-9A-Z]{5}", code)):
        return "permanent", "after_server_response", False
    return "unknown", "unknown", False


def _configured_secrets() -> tuple[str, ...]:
    names = (
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_PASSWORD",
        "DATABASE_URL",
        "API_KEY",
        "GOOGLE_API_KEY",
    )
    return tuple(str(os.environ.get(name) or "") for name in names if os.environ.get(name))


def _sanitize_text(value: Any, *, secrets: Iterable[str]) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(apikey\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\bsecret[-_:=][^\s,;]+", "[REDACTED]", text)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(postgres(?:ql)?://[^:\s/]+:)[^@\s]+(@)",
        r"\1[REDACTED]\2",
        text,
    )
    return text


def _serialized_bytes(value: Any) -> int:
    encoded = json.dumps(
        _json_safe(value), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return len(encoded)


def _positive_integer_setting(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    raw = str(environment.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SupabaseConfigurationError(f"{name} must be an integer of at least 1") from exc
    if value < 1:
        raise SupabaseConfigurationError(f"{name} must be an integer of at least 1")
    return value


def _positive_float_setting(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    raw = str(environment.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise SupabaseConfigurationError(f"{name} must be positive") from exc
    if value <= 0:
        raise SupabaseConfigurationError(f"{name} must be positive")
    return value


def _nonnegative_float_setting(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    raw = str(environment.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise SupabaseConfigurationError(f"{name} cannot be negative") from exc
    if value < 0:
        raise SupabaseConfigurationError(f"{name} cannot be negative")
    return value


def _is_genuine_external_id(value: str) -> bool:
    normalized = value.strip().casefold()
    return bool(normalized) and normalized not in {
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
    } and not normalized.startswith(("http://", "https://"))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _dashboard_child_brands(row: Mapping[str, Any]) -> list[str]:
    raw_data = row.get("raw_data")
    if not isinstance(raw_data, Mapping):
        return []
    values = raw_data.get("child_brands")
    if not isinstance(values, list):
        metadata = raw_data.get("raw_metadata")
        values = metadata.get("child_brands") if isinstance(metadata, Mapping) else []
    unique: dict[str, str] = {}
    for value in values if isinstance(values, list) else []:
        cleaned = str(value).strip()
        if cleaned:
            unique.setdefault(cleaned.casefold(), cleaned)
    return [unique[key] for key in sorted(unique)]


def _normalized_timestamp(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _iso_timestamp(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        parsed = datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=timezone.utc,
        )
    return _iso_timestamp(parsed)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_timestamp(value: datetime) -> str:
    return _ensure_aware(value).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
