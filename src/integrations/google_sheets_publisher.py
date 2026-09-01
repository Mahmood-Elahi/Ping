"""Publish the opt-in student-job dashboard to Google Sheets."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


DEFAULT_NEW_JOB_WINDOW_DAYS = 7
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GOOGLE_SHEETS_RETRY_DELAYS = (0.5, 1.0)
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

LOGGER = logging.getLogger(__name__)

_PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_TOKEN_PATTERN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>['\"]?(?:private[_ -]?key(?:[_ -]?id)?|client[_ -]?secret|"
    r"access[_ -]?token|refresh[_ -]?token|id[_ -]?token|authorization|"
    r"api[_ -]?key|password)['\"]?)\s*[:=]\s*"
    r"(?P<value>['\"][^'\"]*['\"]|[^\s,;]+)",
    re.IGNORECASE,
)
_SECRET_LIKE_TOKEN_PATTERN = re.compile(
    r"\S*(?:private[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|credential[-_]?secret|secret[-_]?credential)\S*",
    re.IGNORECASE,
)
_OPAQUE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9])"
)
_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

NEW_JOBS_HEADERS = [
    "Score",
    "Band",
    "Freshness",
    "Age Days",
    "First Seen Age Hours",
    "Urgency",
    "Urgency Reasons",
    "Closes At",
    "Lifecycle",
    "Company",
    "Position",
    "Primary Category",
    "Matched Categories",
    "Location",
    "Location Status",
    "First Seen",
    "Source Posted",
    "Source Updated",
    "Positive Signals",
    "Apply URL",
    "Source Key",
    "Child Brands",
]

ACTIVE_JOBS_HEADERS = [
    "Score",
    "Band",
    "Freshness",
    "Age Days",
    "First Seen Age Hours",
    "Urgency",
    "Urgency Reasons",
    "Closes At",
    "Lifecycle",
    "Company",
    "Position",
    "Primary Category",
    "Matched Categories",
    "Location",
    "Location Status",
    "First Seen",
    "Last Seen",
    "Source Posted",
    "Source Updated",
    "Apply URL",
    "Source Key",
    "Child Brands",
]

SYSTEM_STATUS_HEADERS = ["Metric", "Value"]
COMPANIES_HEADERS = [
    "Company",
    "Platform",
    "Enabled",
    "Latest Scan Status",
    "Coverage Relationship",
    "Canonical Parent",
    "Mapped Jobs",
]
MANAGED_TABS = ("New Jobs", "Active Jobs", "System Status", "Companies")
PROTECTED_TABS = ("Applications",)

TAB_FORMATS = {
    "New Jobs": {
        "widths": {
            "Score": 70,
            "Band": 90,
            "Freshness": 100,
            "Age Days": 80,
            "First Seen Age Hours": 130,
            "Urgency": 110,
            "Urgency Reasons": 220,
            "Closes At": 130,
            "Lifecycle": 120,
            "Company": 150,
            "Position": 320,
            "Primary Category": 180,
            "Matched Categories": 220,
            "Location": 240,
            "Location Status": 150,
            "First Seen": 160,
            "Source Posted": 160,
            "Source Updated": 160,
            "Positive Signals": 300,
            "Apply URL": 240,
            "Source Key": 220,
            "Child Brands": 180,
        },
        "wrapped": {
            "Position",
            "Matched Categories",
            "Location",
            "Positive Signals",
            "Urgency Reasons",
            "Child Brands",
        },
    },
    "Active Jobs": {
        "widths": {
            "Score": 70,
            "Band": 90,
            "Freshness": 100,
            "Age Days": 80,
            "First Seen Age Hours": 130,
            "Urgency": 110,
            "Urgency Reasons": 220,
            "Closes At": 130,
            "Lifecycle": 120,
            "Company": 150,
            "Position": 320,
            "Primary Category": 180,
            "Matched Categories": 220,
            "Location": 240,
            "Location Status": 150,
            "First Seen": 160,
            "Last Seen": 160,
            "Source Posted": 160,
            "Source Updated": 160,
            "Apply URL": 240,
            "Source Key": 220,
            "Child Brands": 180,
        },
        "wrapped": {
            "Position", "Matched Categories", "Location", "Urgency Reasons", "Child Brands"
        },
    },
    "System Status": {
        "widths": {"Metric": 220, "Value": 320},
        "wrapped": {"Metric", "Value"},
    },
    "Companies": {
        "widths": {
            "Company": 170,
            "Platform": 130,
            "Enabled": 90,
            "Latest Scan Status": 240,
            "Coverage Relationship": 190,
            "Canonical Parent": 170,
            "Mapped Jobs": 100,
        },
        "wrapped": {"Latest Scan Status", "Coverage Relationship"},
    },
}

HEADER_ROW_HEIGHT_PIXELS = 32


class GoogleSheetsConfigurationError(RuntimeError):
    """Raised when the Google Sheets publisher cannot be configured safely."""


class GoogleSheetsAccessError(RuntimeError):
    """Raised when the configured spreadsheet cannot be updated."""


class _GoogleSheetsOperationError(RuntimeError):
    """Internal sanitized failure for one named Google Sheets operation."""


class GoogleSheetsPublisher:
    """Replace managed dashboard tabs while leaving application data untouched."""

    def __init__(
        self,
        spreadsheet: Any,
        *,
        new_job_window_days: int = DEFAULT_NEW_JOB_WINDOW_DAYS,
        now_factory: Optional[Callable[[], datetime]] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
        retry_delays: tuple[float, ...] = GOOGLE_SHEETS_RETRY_DELAYS,
    ) -> None:
        if new_job_window_days < 1:
            raise GoogleSheetsConfigurationError(
                "NEW_JOB_WINDOW_DAYS must be an integer of at least 1"
            )
        self.spreadsheet = spreadsheet
        self.new_job_window_days = new_job_window_days
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._sleep_func = sleep_func or time.sleep
        self._retry_delays = retry_delays

    @classmethod
    def from_env(
        cls,
        *,
        environ: Optional[Mapping[str, str]] = None,
        authorize_factory: Optional[Callable[[Any], Any]] = None,
        credentials_factory: Optional[Callable[..., Any]] = None,
        now_factory: Optional[Callable[[], datetime]] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
        retry_delays: tuple[float, ...] = GOOGLE_SHEETS_RETRY_DELAYS,
    ) -> "GoogleSheetsPublisher":
        """Create the publisher only when explicitly requested at runtime."""
        environment = environ if environ is not None else os.environ
        sheet_id = str(environment.get("GOOGLE_SHEET_ID") or "").strip()
        credentials_path_text = str(
            environment.get("GOOGLE_CREDENTIALS_PATH") or ""
        ).strip()
        missing = [
            name
            for name, value in (
                ("GOOGLE_SHEET_ID", sheet_id),
                ("GOOGLE_CREDENTIALS_PATH", credentials_path_text),
            )
            if not value
        ]
        if missing:
            raise GoogleSheetsConfigurationError(
                "Missing required Google Sheets environment variable(s): "
                + ", ".join(missing)
            )

        credentials_path = Path(credentials_path_text).expanduser()
        if not credentials_path.exists():
            raise GoogleSheetsConfigurationError(
                "Google credential file was not found at GOOGLE_CREDENTIALS_PATH"
            )
        if not credentials_path.is_file():
            raise GoogleSheetsConfigurationError(
                "GOOGLE_CREDENTIALS_PATH must point to a service-account JSON file"
            )

        window_text = str(
            environment.get("NEW_JOB_WINDOW_DAYS", DEFAULT_NEW_JOB_WINDOW_DAYS)
        ).strip()
        try:
            window_days = int(window_text)
        except ValueError as exc:
            raise GoogleSheetsConfigurationError(
                "NEW_JOB_WINDOW_DAYS must be an integer of at least 1"
            ) from exc
        if window_days < 1:
            raise GoogleSheetsConfigurationError(
                "NEW_JOB_WINDOW_DAYS must be an integer of at least 1"
            )

        if credentials_factory is None or authorize_factory is None:
            try:
                import gspread
                from google.oauth2.service_account import Credentials
            except ImportError as exc:
                raise GoogleSheetsConfigurationError(
                    "The 'gspread' and 'google-auth' packages are required "
                    "to publish Google Sheets"
                ) from exc
            credentials_factory = (
                credentials_factory or Credentials.from_service_account_file
            )
            authorize_factory = authorize_factory or gspread.authorize

        sleeper = sleep_func or time.sleep
        try:
            credentials = _run_google_operation(
                "load_credentials",
                lambda: credentials_factory(
                    str(credentials_path),
                    scopes=[GOOGLE_SHEETS_SCOPE],
                ),
                sleep_func=sleeper,
                retry_delays=retry_delays,
                allow_retry=False,
            )
            client = _run_google_operation(
                "authorize",
                lambda: authorize_factory(credentials),
                sleep_func=sleeper,
                retry_delays=retry_delays,
                allow_retry=False,
            )
            spreadsheet = _run_google_operation(
                "open_by_key",
                lambda: client.open_by_key(sheet_id),
                sleep_func=sleeper,
                retry_delays=retry_delays,
                allow_retry=True,
            )
        except _GoogleSheetsOperationError as exc:
            raise GoogleSheetsConfigurationError(
                "Unable to initialize Google Sheets; " + str(exc)
            ) from None

        return cls(
            spreadsheet,
            new_job_window_days=window_days,
            now_factory=now_factory,
            sleep_func=sleeper,
            retry_delays=retry_delays,
        )

    def publish(
        self,
        *,
        active_jobs: Iterable[Mapping[str, Any] | Any],
        scan_run: Optional[Mapping[str, Any]],
        companies: Iterable[Mapping[str, Any] | Any],
        company_statuses: Optional[Mapping[str, str]] = None,
        coverage_summary: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, int]:
        """Replace all managed tabs and return the number of data rows written."""
        jobs = [_as_mapping(job) for job in active_jobs]
        jobs = [job for job in jobs if str(job.get("status") or "active") == "active"]
        statuses = {
            str(name): str(status)
            for name, status in (company_statuses or {}).items()
        }

        new_jobs = self._new_jobs(jobs)
        active_jobs_sorted = sorted(jobs, key=_job_sort_key)
        tab_values = {
            "New Jobs": [NEW_JOBS_HEADERS]
            + [_new_job_row(job) for job in new_jobs],
            "Active Jobs": [ACTIVE_JOBS_HEADERS]
            + [_active_job_row(job) for job in active_jobs_sorted],
            "System Status": [SYSTEM_STATUS_HEADERS]
            + _system_status_rows(scan_run or {}, statuses, coverage_summary or {}),
            "Companies": [COMPANIES_HEADERS]
            + _company_rows(companies, statuses),
        }

        try:
            worksheets = {
                worksheet.title: worksheet
                for worksheet in _run_google_operation(
                    "list_worksheets",
                    self.spreadsheet.worksheets,
                    sleep_func=self._sleep_func,
                    retry_delays=self._retry_delays,
                    allow_retry=True,
                )
            }
            managed_worksheets = {}
            for title in MANAGED_TABS:
                values = tab_values[title]
                worksheet = worksheets.get(title)
                if worksheet is None:
                    worksheet = _run_google_operation(
                        f"add_worksheet: {title}",
                        lambda title=title, values=values: self.spreadsheet.add_worksheet(
                            title=title,
                            rows=max(100, len(values) + 10),
                            cols=len(values[0]),
                        ),
                        sleep_func=self._sleep_func,
                        retry_delays=self._retry_delays,
                        allow_retry=False,
                    )
                managed_worksheets[title] = worksheet
                _run_google_operation(
                    f"clear: {title}",
                    worksheet.clear,
                    sleep_func=self._sleep_func,
                    retry_delays=self._retry_delays,
                    allow_retry=True,
                )
                _run_google_operation(
                    f"update: {title}",
                    lambda worksheet=worksheet, values=values: worksheet.update(
                        range_name="A1",
                        values=values,
                        value_input_option="RAW",
                    ),
                    sleep_func=self._sleep_func,
                    retry_delays=self._retry_delays,
                    allow_retry=True,
                )
            formatting_requests = _formatting_requests(
                managed_worksheets,
                tab_values,
            )
            _run_google_operation(
                "batch_format",
                lambda: self.spreadsheet.batch_update(
                    {"requests": formatting_requests}
                ),
                sleep_func=self._sleep_func,
                retry_delays=self._retry_delays,
                allow_retry=True,
            )
        except _GoogleSheetsOperationError as exc:
            raise GoogleSheetsAccessError(
                "Google Sheets publication failed; " + str(exc)
            ) from None

        return {
            "new_jobs": len(new_jobs),
            "active_jobs": len(active_jobs_sorted),
            "companies": len(tab_values["Companies"]) - 1,
        }

    def _new_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = _ensure_aware(self._now_factory())
        cutoff = now - timedelta(days=self.new_job_window_days)
        recent = []
        for job in jobs:
            first_seen = _parse_timestamp(job.get("first_seen_at"))
            if first_seen is not None and first_seen >= cutoff:
                recent.append(job)
        return sorted(recent, key=_job_sort_key)


def _run_google_operation(
    operation: str,
    callback: Callable[[], Any],
    *,
    sleep_func: Callable[[float], None],
    retry_delays: tuple[float, ...],
    allow_retry: bool,
) -> Any:
    total_attempts = len(retry_delays) + 1 if allow_retry else 1
    for attempt in range(1, total_attempts + 1):
        try:
            return callback()
        except Exception as exc:
            diagnostic = _google_exception_diagnostic(operation, exc)
            should_retry = (
                allow_retry
                and _is_transient_google_error(exc)
                and attempt < total_attempts
            )
            if not should_retry:
                raise _GoogleSheetsOperationError(
                    f"{diagnostic}; attempts={attempt}"
                ) from None

            delay = retry_delays[attempt - 1]
            LOGGER.warning(
                "Retrying Google Sheets operation after transient failure: "
                "%s; attempt=%d/%d; delay_seconds=%g",
                diagnostic,
                attempt,
                total_attempts,
                delay,
            )
            sleep_func(delay)

    raise AssertionError("Google Sheets retry loop exited unexpectedly")


def _google_exception_diagnostic(operation: str, exc: Exception) -> str:
    chain = list(_exception_chain(exc))
    parts = [
        f"operation={operation}",
        f"exception={exc.__class__.__name__}",
    ]
    cause_names = []
    for cause in chain[1:]:
        name = cause.__class__.__name__
        if name not in cause_names:
            cause_names.append(name)
    if cause_names:
        parts.append("cause=" + ",".join(cause_names))

    payload = _google_error_payload(chain)
    http_status = _http_status(chain, payload)
    if http_status is not None:
        parts.append(f"http_status={http_status}")

    google_code = _safe_error_code(payload.get("code")) if payload else None
    if google_code is not None:
        parts.append(f"google_code={google_code}")

    google_error = _safe_error_code(payload.get("status")) if payload else None
    if google_error is None and payload:
        google_error = _safe_error_code(payload.get("error"))
    if google_error is not None:
        parts.append(f"google_error={google_error}")

    reasons = _google_error_reasons(payload)
    if reasons:
        parts.append("google_reason=" + ",".join(reasons))

    parts.append(f"message={_safe_exception_message(operation, exc, chain, payload)}")
    return "; ".join(parts)


def _exception_chain(exc: Exception) -> Iterable[Exception]:
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _google_error_payload(chain: Iterable[Exception]) -> Mapping[str, Any]:
    for current in chain:
        error = getattr(current, "error", None)
        if isinstance(error, Mapping):
            return error
        for argument in getattr(current, "args", ()):
            if isinstance(argument, Mapping) and any(
                key in argument
                for key in ("code", "error", "error_description", "message", "status")
            ):
                nested = argument.get("error")
                if isinstance(nested, Mapping):
                    return nested
                return argument
    return {}


def _http_status(
    chain: Iterable[Exception],
    payload: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    for current in chain:
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    code = (payload or {}).get("code")
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    return None


def _google_error_reasons(payload: Mapping[str, Any]) -> list[str]:
    reasons = []
    direct_reason = _safe_error_code(payload.get("reason"))
    if direct_reason:
        reasons.append(direct_reason)
    for key in ("errors", "details"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            for reason_key in ("reason", "errorCode"):
                reason = _safe_error_code(item.get(reason_key))
                if reason and reason not in reasons:
                    reasons.append(reason)
    return reasons


def _safe_error_code(value: Any) -> Optional[str]:
    if not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text or len(text) > 100:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", text):
        return None
    return text


def _safe_exception_message(
    operation: str,
    exc: Exception,
    chain: Iterable[Exception],
    payload: Mapping[str, Any],
) -> str:
    if operation == "load_credentials":
        return "service-account credentials are invalid or malformed"

    raw_message = payload.get("message") or payload.get("error_description")
    if not isinstance(raw_message, str):
        class_names = {current.__class__.__name__ for current in chain}
        if any("Timeout" in name for name in class_names):
            raw_message = "request timed out"
        elif class_names & {"ConnectionError", "TransportError"}:
            raw_message = "temporary connection or transport error"
        elif "PermissionError" in class_names:
            raw_message = "spreadsheet permission was denied"
        elif "SpreadsheetNotFound" in class_names:
            raw_message = "spreadsheet was not found or is not accessible"
        elif operation == "authorize":
            raw_message = "Google Sheets client authorization failed"
        else:
            raw_message = str(exc) or "details unavailable"
    return _sanitize_message(raw_message)


def _sanitize_message(message: str) -> str:
    text = _PEM_PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", message)
    text = _BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED]", text)
    text = _JWT_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('key')}=[REDACTED]",
        text,
    )
    text = _SECRET_LIKE_TOKEN_PATTERN.sub("[REDACTED]", text)
    text = _OPAQUE_VALUE_PATTERN.sub("[REDACTED_OPAQUE_VALUE]", text)
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = " ".join(text.split())
    if not text:
        return "details unavailable"
    if len(text) > 300:
        return text[:297] + "..."
    return text


def _is_transient_google_error(exc: Exception) -> bool:
    chain = list(_exception_chain(exc))
    status = _http_status(chain, _google_error_payload(chain))
    if status is not None:
        return status in TRANSIENT_HTTP_STATUSES

    class_names = {current.__class__.__name__ for current in chain}
    if class_names & {"SSLError", "ProxyError"}:
        return False
    if any(getattr(current, "retryable", False) is True for current in chain):
        return True
    if any(isinstance(current, (TimeoutError, ConnectionError)) for current in chain):
        return True
    return bool(
        class_names
        & {
            "Timeout",
            "ConnectTimeout",
            "ReadTimeout",
            "TimeoutError",
            "ConnectionError",
            "ConnectionResetError",
            "ConnectionAbortedError",
            "ConnectionRefusedError",
            "TransportError",
        }
    )


def _formatting_requests(
    worksheets: Mapping[str, Any],
    tab_values: Mapping[str, list[list[Any]]],
) -> list[dict[str, Any]]:
    requests = []
    for title in MANAGED_TABS:
        worksheet = worksheets[title]
        values = tab_values[title]
        headers = values[0]
        sheet_id = worksheet.id
        populated_range = {
            "sheetId": sheet_id,
            "startRowIndex": 0,
            "endRowIndex": len(values),
            "startColumnIndex": 0,
            "endColumnIndex": len(headers),
        }

        requests.extend(
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(headers),
                        },
                        "cell": {
                            "userEnteredFormat": {"textFormat": {"bold": True}}
                        },
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                },
                {
                    "setBasicFilter": {
                        "filter": {"range": populated_range},
                    }
                },
                {
                    "repeatCell": {
                        "range": populated_range,
                        "cell": {
                            "userEnteredFormat": {"verticalAlignment": "TOP"}
                        },
                        "fields": "userEnteredFormat.verticalAlignment",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"pixelSize": HEADER_ROW_HEIGHT_PIXELS},
                        "fields": "pixelSize",
                    }
                },
            ]
        )

        tab_format = TAB_FORMATS[title]
        for column_index, header in enumerate(headers):
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": column_index,
                            "endIndex": column_index + 1,
                        },
                        "properties": {
                            "pixelSize": tab_format["widths"][header]
                        },
                        "fields": "pixelSize",
                    }
                }
            )
            if header in tab_format["wrapped"]:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": len(values),
                                "startColumnIndex": column_index,
                                "endColumnIndex": column_index + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {"wrapStrategy": "WRAP"}
                            },
                            "fields": "userEnteredFormat.wrapStrategy",
                        }
                    }
                )

    return requests


def _new_job_row(job: Mapping[str, Any]) -> list[Any]:
    return [
        _cell(job.get("relevance_score", job.get("score"))),
        _cell(job.get("relevance_band", job.get("band"))),
        _cell(job.get("freshness_band")),
        _cell(job.get("age_days")),
        _cell(job.get("first_seen_age_hours")),
        _cell(job.get("urgency_band")),
        _cell(job.get("urgency_reasons")),
        _timestamp_cell(job.get("source_closes_at")),
        _cell(job.get("lifecycle_status", job.get("status"))),
        _cell(job.get("company")),
        _cell(job.get("title", job.get("position"))),
        _cell(job.get("primary_category")),
        _cell(job.get("matched_categories")),
        _cell(job.get("location")),
        _cell(job.get("location_status")),
        _timestamp_cell(job.get("first_seen_at")),
        _timestamp_cell(job.get("source_posted_at")),
        _timestamp_cell(job.get("source_updated_at", job.get("updated_at"))),
        _cell(job.get("positive_signals")),
        _cell(job.get("apply_url", job.get("url"))),
        _cell(job.get("source_key")),
        _cell(_child_brands(job)),
    ]


def _active_job_row(job: Mapping[str, Any]) -> list[Any]:
    return [
        _cell(job.get("relevance_score", job.get("score"))),
        _cell(job.get("relevance_band", job.get("band"))),
        _cell(job.get("freshness_band")),
        _cell(job.get("age_days")),
        _cell(job.get("first_seen_age_hours")),
        _cell(job.get("urgency_band")),
        _cell(job.get("urgency_reasons")),
        _timestamp_cell(job.get("source_closes_at")),
        _cell(job.get("lifecycle_status", job.get("status"))),
        _cell(job.get("company")),
        _cell(job.get("title", job.get("position"))),
        _cell(job.get("primary_category")),
        _cell(job.get("matched_categories")),
        _cell(job.get("location")),
        _cell(job.get("location_status")),
        _timestamp_cell(job.get("first_seen_at")),
        _timestamp_cell(job.get("last_seen_at")),
        _timestamp_cell(job.get("source_posted_at")),
        _timestamp_cell(job.get("source_updated_at", job.get("updated_at"))),
        _cell(job.get("apply_url", job.get("url"))),
        _cell(job.get("source_key")),
        _cell(_child_brands(job)),
    ]


def _system_status_rows(
    scan_run: Mapping[str, Any],
    company_statuses: Mapping[str, str],
    coverage_summary: Mapping[str, Any],
) -> list[list[Any]]:
    success = scan_run.get("success")
    status = "success" if success is True else "failed" if success is False else "unknown"
    rows = [
        ["Most Recent Scan Status", status],
        ["Start Time", _timestamp_cell(scan_run.get("started_at"))],
        ["Completion Time", _timestamp_cell(scan_run.get("completed_at"))],
        ["Companies Checked", _cell(scan_run.get("companies_checked"))],
        ["Canonical Sources Checked", _cell(scan_run.get("companies_checked"))],
        [
            "Mapped Child Brands Covered",
            _cell(coverage_summary.get("mapped_child_brands_covered")),
        ],
        [
            "Total Original Companies Covered",
            _cell(coverage_summary.get("total_original_companies_covered")),
        ],
        ["Jobs Fetched", _cell(scan_run.get("jobs_fetched"))],
        ["Jobs Retained", _cell(scan_run.get("jobs_retained"))],
        ["Inserted Count", _cell(scan_run.get("jobs_inserted"))],
        ["Updated Count", _cell(scan_run.get("jobs_updated"))],
        ["Unchanged Count", _cell(scan_run.get("jobs_unchanged"))],
        ["Exclusion Counts", _cell(scan_run.get("exclusion_counts"))],
        ["Coverage Report Path", _cell(scan_run.get("coverage_report_path"))],
        ["Ranked Artifact Path", _cell(scan_run.get("ranked_artifact_path"))],
    ]
    rows.extend(
        [f"Collector Status: {company}", status]
        for company, status in sorted(company_statuses.items())
    )
    return rows


def _company_rows(
    companies: Iterable[Mapping[str, Any] | Any],
    company_statuses: Mapping[str, str],
) -> list[list[Any]]:
    rows = []
    for company in companies:
        values = _as_mapping(company)
        name = str(values.get("name") or values.get("company") or "").strip()
        enabled = values.get("enabled", True)
        rows.append(
            [
                name,
                _cell(values.get("platform")),
                "yes" if bool(enabled) else "no",
                _cell(company_statuses.get(name, values.get("status"))),
                _cell(values.get("coverage_relationship") or "canonical_source"),
                _cell(values.get("canonical_parent")),
                _cell(values.get("mapped_jobs")),
            ]
        )
    return sorted(rows, key=lambda row: str(row[0]).casefold())


def _child_brands(job: Mapping[str, Any]) -> list[str]:
    values = job.get("child_brands")
    if not isinstance(values, list):
        raw_data = job.get("raw_data")
        if isinstance(raw_data, Mapping):
            values = raw_data.get("child_brands")
            if not isinstance(values, list):
                metadata = raw_data.get("raw_metadata")
                values = metadata.get("child_brands") if isinstance(metadata, Mapping) else []
    if not isinstance(values, list):
        return []
    unique: dict[str, str] = {}
    for value in values:
        cleaned = str(value).strip()
        if cleaned:
            unique.setdefault(cleaned.casefold(), cleaned)
    return [unique[key] for key in sorted(unique)]


def _job_sort_key(job: Mapping[str, Any]) -> tuple[Any, ...]:
    score = int(job.get("relevance_score", job.get("score", 0)) or 0)
    first_seen = _parse_timestamp(job.get("first_seen_at"))
    timestamp = first_seen.timestamp() if first_seen is not None else float("-inf")
    return (-score, -timestamp, str(job.get("company") or "").casefold())


def _as_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError("dashboard values must be mappings or mapping-like objects")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime) or isinstance(value, date):
        return _timestamp_cell(value)
    if isinstance(value, Mapping):
        return "; ".join(
            f"{key}={_cell(item)}" for key, item in sorted(value.items())
        )
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item).strip() for item in value if item is not None)
    return value


def _timestamp_cell(value: Any) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            normalized_text = " ".join(text.split())
            for date_format in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
                try:
                    parsed_date = datetime.strptime(
                        normalized_text, date_format
                    ).date()
                    break
                except ValueError:
                    continue
            else:
                return None
        parsed = datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=timezone.utc,
        )
    return _ensure_aware(parsed)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
