"""Publish the opt-in student-job dashboard to Google Sheets."""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


DEFAULT_NEW_JOB_WINDOW_DAYS = 7
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

NEW_JOBS_HEADERS = [
    "Score",
    "Band",
    "Company",
    "Position",
    "Primary Category",
    "Matched Categories",
    "Location",
    "Location Status",
    "First Seen",
    "Source Updated",
    "Positive Signals",
    "Apply URL",
    "Source Key",
]

ACTIVE_JOBS_HEADERS = [
    "Score",
    "Band",
    "Company",
    "Position",
    "Primary Category",
    "Matched Categories",
    "Location",
    "Location Status",
    "First Seen",
    "Last Seen",
    "Source Updated",
    "Apply URL",
    "Source Key",
]

SYSTEM_STATUS_HEADERS = ["Metric", "Value"]
COMPANIES_HEADERS = ["Company", "Platform", "Enabled", "Latest Scan Status"]
MANAGED_TABS = ("New Jobs", "Active Jobs", "System Status", "Companies")
PROTECTED_TABS = ("Applications",)

TAB_FORMATS = {
    "New Jobs": {
        "widths": {
            "Score": 70,
            "Band": 90,
            "Company": 150,
            "Position": 320,
            "Primary Category": 180,
            "Matched Categories": 220,
            "Location": 240,
            "Location Status": 150,
            "First Seen": 160,
            "Source Updated": 160,
            "Positive Signals": 300,
            "Apply URL": 240,
            "Source Key": 220,
        },
        "wrapped": {
            "Position",
            "Matched Categories",
            "Location",
            "Positive Signals",
        },
    },
    "Active Jobs": {
        "widths": {
            "Score": 70,
            "Band": 90,
            "Company": 150,
            "Position": 320,
            "Primary Category": 180,
            "Matched Categories": 220,
            "Location": 240,
            "Location Status": 150,
            "First Seen": 160,
            "Last Seen": 160,
            "Source Updated": 160,
            "Apply URL": 240,
            "Source Key": 220,
        },
        "wrapped": {"Position", "Matched Categories", "Location"},
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
        },
        "wrapped": {"Latest Scan Status"},
    },
}

HEADER_ROW_HEIGHT_PIXELS = 32


class GoogleSheetsConfigurationError(RuntimeError):
    """Raised when the Google Sheets publisher cannot be configured safely."""


class GoogleSheetsAccessError(RuntimeError):
    """Raised when the configured spreadsheet cannot be updated."""


class GoogleSheetsPublisher:
    """Replace managed dashboard tabs while leaving application data untouched."""

    def __init__(
        self,
        spreadsheet: Any,
        *,
        new_job_window_days: int = DEFAULT_NEW_JOB_WINDOW_DAYS,
        now_factory: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if new_job_window_days < 1:
            raise GoogleSheetsConfigurationError(
                "NEW_JOB_WINDOW_DAYS must be an integer of at least 1"
            )
        self.spreadsheet = spreadsheet
        self.new_job_window_days = new_job_window_days
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_env(
        cls,
        *,
        environ: Optional[Mapping[str, str]] = None,
        authorize_factory: Optional[Callable[[Any], Any]] = None,
        credentials_factory: Optional[Callable[..., Any]] = None,
        now_factory: Optional[Callable[[], datetime]] = None,
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

        try:
            credentials = credentials_factory(
                str(credentials_path),
                scopes=[GOOGLE_SHEETS_SCOPE],
            )
            client = authorize_factory(credentials)
            spreadsheet = client.open_by_key(sheet_id)
        except Exception:
            raise GoogleSheetsConfigurationError(
                "Unable to initialize Google Sheets; verify the sheet ID, "
                "service-account credentials, and sheet sharing"
            ) from None

        return cls(
            spreadsheet,
            new_job_window_days=window_days,
            now_factory=now_factory,
        )

    def publish(
        self,
        *,
        active_jobs: Iterable[Mapping[str, Any] | Any],
        scan_run: Optional[Mapping[str, Any]],
        companies: Iterable[Mapping[str, Any] | Any],
        company_statuses: Optional[Mapping[str, str]] = None,
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
            + _system_status_rows(scan_run or {}, statuses),
            "Companies": [COMPANIES_HEADERS]
            + _company_rows(companies, statuses),
        }

        try:
            worksheets = {
                worksheet.title: worksheet for worksheet in self.spreadsheet.worksheets()
            }
            managed_worksheets = {}
            for title in MANAGED_TABS:
                values = tab_values[title]
                worksheet = worksheets.get(title)
                if worksheet is None:
                    worksheet = self.spreadsheet.add_worksheet(
                        title=title,
                        rows=max(100, len(values) + 10),
                        cols=len(values[0]),
                    )
                managed_worksheets[title] = worksheet
                worksheet.clear()
                worksheet.update(
                    range_name="A1",
                    values=values,
                    value_input_option="RAW",
                )
            self.spreadsheet.batch_update(
                {
                    "requests": _formatting_requests(
                        managed_worksheets,
                        tab_values,
                    )
                }
            )
        except Exception:
            raise GoogleSheetsAccessError(
                "Google Sheets publication failed; verify spreadsheet access, "
                "service-account Editor sharing, and the Google Sheets API"
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
        _cell(job.get("company")),
        _cell(job.get("title", job.get("position"))),
        _cell(job.get("primary_category")),
        _cell(job.get("matched_categories")),
        _cell(job.get("location")),
        _cell(job.get("location_status")),
        _timestamp_cell(job.get("first_seen_at")),
        _timestamp_cell(job.get("source_updated_at", job.get("updated_at"))),
        _cell(job.get("positive_signals")),
        _cell(job.get("apply_url", job.get("url"))),
        _cell(job.get("source_key")),
    ]


def _active_job_row(job: Mapping[str, Any]) -> list[Any]:
    return [
        _cell(job.get("relevance_score", job.get("score"))),
        _cell(job.get("relevance_band", job.get("band"))),
        _cell(job.get("company")),
        _cell(job.get("title", job.get("position"))),
        _cell(job.get("primary_category")),
        _cell(job.get("matched_categories")),
        _cell(job.get("location")),
        _cell(job.get("location_status")),
        _timestamp_cell(job.get("first_seen_at")),
        _timestamp_cell(job.get("last_seen_at")),
        _timestamp_cell(job.get("source_updated_at", job.get("updated_at"))),
        _cell(job.get("apply_url", job.get("url"))),
        _cell(job.get("source_key")),
    ]


def _system_status_rows(
    scan_run: Mapping[str, Any],
    company_statuses: Mapping[str, str],
) -> list[list[Any]]:
    success = scan_run.get("success")
    status = "success" if success is True else "failed" if success is False else "unknown"
    rows = [
        ["Most Recent Scan Status", status],
        ["Start Time", _timestamp_cell(scan_run.get("started_at"))],
        ["Completion Time", _timestamp_cell(scan_run.get("completed_at"))],
        ["Companies Checked", _cell(scan_run.get("companies_checked"))],
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
                _cell(company_statuses.get(name)),
            ]
        )
    return sorted(rows, key=lambda row: str(row[0]).casefold())


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
        return "" if value is None or value == "" else str(value).strip()
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
