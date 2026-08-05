"""Supabase persistence for retained, student-ranked company-careers jobs."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_CLOSE_AFTER_MISSING_RUNS = 2
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


@dataclass(frozen=True)
class SyncResult:
    inserted: int
    updated: int
    unchanged: int
    missing_incremented: int = 0
    closed: int = 0


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
        "first_seen_at": _iso_timestamp(seen_at),
        "last_seen_at": _iso_timestamp(seen_at),
        "status": "active",
        "missing_successful_runs": 0,
        "raw_data": _json_safe(values),
    }


class SupabaseJobStore:
    """Thin Supabase client wrapper for atomic student-job scan persistence."""

    def __init__(
        self,
        client: Any,
        *,
        close_after_missing_runs: int = DEFAULT_CLOSE_AFTER_MISSING_RUNS,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        if close_after_missing_runs < 1:
            raise SupabaseConfigurationError("CLOSE_AFTER_MISSING_RUNS must be at least 1")
        self.client = client
        self.close_after_missing_runs = close_after_missing_runs
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_env(
        cls,
        *,
        environ: Optional[Mapping[str, str]] = None,
        client_factory: Optional[Callable[[str, str], Any]] = None,
        now_factory: Callable[[], datetime] | None = None,
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
            environment.get(
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
            except ImportError as exc:
                raise SupabaseConfigurationError(
                    "The 'supabase' package is required to create the Supabase client"
                ) from exc
            client_factory = create_client

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
        )

    def validate_setup(self) -> None:
        """Verify dashboard tables and backend access without writing any data."""
        checks = (
            ("tracked_jobs", "id"),
            ("scan_runs", "id"),
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
            raise _supabase_access_error(exc, operation="setup validation") from None

    def sync_student_jobs(
        self,
        jobs: Iterable[Mapping[str, Any] | Any],
        *,
        successful_companies: Iterable[str],
        started_at: datetime,
        companies_checked: int,
        jobs_fetched: int,
        exclusion_counts: Optional[Mapping[str, int]] = None,
        coverage_report_path: Optional[str] = None,
        ranked_artifact_path: Optional[str] = None,
    ) -> SyncResult:
        """Atomically upsert jobs, advance missing counts, and record a successful run."""
        seen_at = _ensure_aware(self._now_factory())
        jobs_by_key: MutableMapping[str, dict[str, Any]] = {}
        for job in jobs:
            payload = tracked_job_payload(job, seen_at=seen_at)
            jobs_by_key.setdefault(payload["source_key"], payload)

        scan = {
            "started_at": _iso_timestamp(started_at),
            "completed_at": _iso_timestamp(seen_at),
            "companies_checked": int(companies_checked),
            "jobs_fetched": int(jobs_fetched),
            "jobs_retained": len(jobs_by_key),
            "exclusion_counts": {
                str(key): int(value)
                for key, value in (exclusion_counts or {}).items()
            },
            "coverage_report_path": coverage_report_path,
            "ranked_artifact_path": ranked_artifact_path,
        }
        try:
            response = self.client.rpc(
                "sync_student_job_scan",
                {
                    "p_jobs": list(jobs_by_key.values()),
                    "p_successful_companies": sorted(
                        {
                            str(company).strip()
                            for company in successful_companies
                            if str(company).strip()
                        }
                    ),
                    "p_close_after_missing_runs": self.close_after_missing_runs,
                    "p_seen_at": _iso_timestamp(seen_at),
                    "p_scan": scan,
                },
            ).execute()
        except Exception as exc:
            raise _supabase_access_error(exc, operation="job synchronization") from None
        data = getattr(response, "data", None)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
            data = data[0]
        if not isinstance(data, Mapping):
            raise RuntimeError("Supabase sync returned an invalid response")
        return SyncResult(
            inserted=int(data.get("inserted", 0)),
            updated=int(data.get("updated", 0)),
            unchanged=int(data.get("unchanged", 0)),
            missing_incremented=int(data.get("missing_incremented", 0)),
            closed=int(data.get("closed", 0)),
        )

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
    ) -> None:
        """Record an overall failed scan without changing any tracked job state."""
        completed_at = _ensure_aware(self._now_factory())
        self.client.table("scan_runs").insert(
            {
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
                "error_message": str(error_message),
            }
        ).execute()

    def fetch_dashboard_data(self) -> DashboardData:
        """Read active jobs and the latest scan without touching applications."""
        try:
            jobs_response = (
                self.client.table("tracked_jobs")
                .select(
                    "source_key,company,title,location,apply_url,primary_category,"
                    "matched_categories,relevance_score,relevance_band,positive_signals,"
                    "location_status,source_updated_at,first_seen_at,last_seen_at,status,raw_data"
                )
                .eq("status", "active")
                .order("relevance_score", desc=True)
                .order("first_seen_at", desc=True)
                .execute()
            )
        except Exception as exc:
            raise _supabase_access_error(exc, operation="active-jobs query") from None
        jobs_data = getattr(jobs_response, "data", None)
        if not isinstance(jobs_data, list) or not all(
            isinstance(row, Mapping) for row in jobs_data
        ):
            raise RuntimeError("Supabase active-jobs query returned an invalid response")

        try:
            scan_response = (
                self.client.table("scan_runs")
                .select("*")
                .order("completed_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise _supabase_access_error(exc, operation="latest-scan query") from None
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


def _supabase_access_error(exc: Exception, *, operation: str) -> SupabaseAccessError:
    """Return a credential-safe, actionable error for backend failures."""
    message = str(exc).casefold()
    schema_markers = (
        "pgrst202",
        "pgrst205",
        "could not find the function",
        "could not find the table",
        "does not exist",
        "schema cache",
    )
    if any(marker in message for marker in schema_markers):
        return SupabaseAccessError(
            "Supabase dashboard schema is unavailable; run sql/supabase_schema.sql "
            "in the Supabase SQL editor and retry"
        )
    return SupabaseAccessError(
        f"Supabase access failed during {operation}; verify SUPABASE_URL, the "
        "service-role key, and network access"
    )


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
