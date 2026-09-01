import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from gspread.exceptions import APIError
from requests import Response

from src.integrations.google_sheets_publisher import (
    ACTIVE_JOBS_HEADERS,
    COMPANIES_HEADERS,
    MANAGED_TABS,
    NEW_JOBS_HEADERS,
    SYSTEM_STATUS_HEADERS,
    TAB_FORMATS,
    GoogleSheetsAccessError,
    GoogleSheetsConfigurationError,
    GoogleSheetsPublisher,
)


class FakeWorksheet:
    def __init__(self, title, sheet_id, *, protected=False):
        self.title = title
        self.id = sheet_id
        self.protected = protected
        self.clear_calls = 0
        self.update_calls = []

    def clear(self):
        if self.protected:
            raise AssertionError("Protected worksheet was cleared")
        self.clear_calls += 1

    def update(self, **kwargs):
        if self.protected:
            raise AssertionError("Protected worksheet was updated")
        self.update_calls.append(kwargs)

    @property
    def values(self):
        return self.update_calls[-1]["values"]


class FakeSpreadsheet:
    def __init__(self, titles=()):
        self.sheets = {
            title: FakeWorksheet(
                title,
                sheet_id,
                protected=title == "Applications",
            )
            for sheet_id, title in enumerate(titles, start=1)
        }
        self.add_calls = []
        self.batch_update_calls = []
        self.basic_filters = {}
        self.worksheet_calls = []
        self._next_sheet_id = len(self.sheets) + 1

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, *, title, rows, cols):
        self.add_calls.append((title, rows, cols))
        worksheet = FakeWorksheet(title, self._next_sheet_id)
        self._next_sheet_id += 1
        self.sheets[title] = worksheet
        return worksheet

    def worksheet(self, title):
        self.worksheet_calls.append(title)
        if title == "Applications":
            raise AssertionError("Protected worksheet was selected")
        return self.sheets[title]

    def batch_update(self, body):
        applications_id = next(
            (
                worksheet.id
                for worksheet in self.sheets.values()
                if worksheet.title == "Applications"
            ),
            None,
        )
        for request in body["requests"]:
            sheet_id = request_sheet_id(request)
            if applications_id is not None and sheet_id == applications_id:
                raise AssertionError("Protected worksheet was formatted")
            if "setBasicFilter" in request:
                self.basic_filters[sheet_id] = request["setBasicFilter"]["filter"]
        self.batch_update_calls.append(body)


def google_api_error(status, message, *, error_status=None, reason=None):
    response = Response()
    response.status_code = status
    error = {
        "code": status,
        "message": message,
    }
    if error_status is not None:
        error["status"] = error_status
    if reason is not None:
        error["details"] = [{"reason": reason}]
    response._content = json.dumps({"error": error}).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return APIError(response)


def request_sheet_id(request):
    if "updateSheetProperties" in request:
        return request["updateSheetProperties"]["properties"]["sheetId"]
    request_body = next(iter(request.values()))
    range_body = request_body.get("range") or request_body.get("filter", {}).get(
        "range"
    )
    return range_body["sheetId"]


def requests_for_sheet(body, sheet_id):
    return [
        request
        for request in body["requests"]
        if request_sheet_id(request) == sheet_id
    ]


class GoogleSheetsPublisherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        self.spreadsheet = FakeSpreadsheet(("New Jobs", "Applications"))
        self.publisher = GoogleSheetsPublisher(
            self.spreadsheet,
            new_job_window_days=7,
            now_factory=lambda: self.now,
        )
        self.jobs = [
            {
                "source_key": "job-newer",
                "company": "Beta",
                "title": "Software Intern",
                "location": None,
                "apply_url": "https://example.com/beta",
                "primary_category": None,
                "matched_categories": ["software_engineering", "embedded_firmware"],
                "relevance_score": 75,
                "relevance_band": "good",
                "positive_signals": ["intern", "software"],
                "location_status": "eligible_canada",
                "source_updated_at": "2026-08-02T08:30:45-06:00",
                "source_posted_at": "2026-08-02T14:30:45Z",
                "source_closes_at": "2026-08-06",
                "first_seen_at": "2026-08-03T10:00:00Z",
                "last_seen_at": datetime(2026, 8, 3, 11, 0),
                "status": "active",
                "lifecycle_status": "active",
                "freshness_band": "new",
                "age_days": 1,
                "first_seen_age_hours": 2,
                "urgency_band": "high",
                "urgency_reasons": ["deadline_in_3_days"],
            },
            {
                "source_key": "job-higher-score",
                "company": "Alpha",
                "title": "ML Co-op",
                "location": "Toronto, ON",
                "apply_url": "https://example.com/alpha",
                "primary_category": "machine_learning_ai",
                "matched_categories": ["machine_learning_ai"],
                "relevance_score": 90,
                "relevance_band": "high",
                "positive_signals": ["co_op", "ml"],
                "location_status": "eligible_canada",
                "source_updated_at": "2026-08-01",
                "first_seen_at": "2026-08-01T10:00:00+00:00",
                "last_seen_at": "2026-08-03T11:00:00Z",
                "status": "active",
            },
            {
                "source_key": "job-old",
                "company": "Old Co",
                "title": "Hardware Student",
                "location": "Remote",
                "relevance_score": 99,
                "first_seen_at": "2026-07-01T00:00:00Z",
                "last_seen_at": "2026-08-03T00:00:00Z",
                "status": "active",
            },
            {
                "source_key": "job-closed",
                "company": "Closed Co",
                "title": "Closed Intern",
                "relevance_score": 100,
                "first_seen_at": "2026-08-03T00:00:00Z",
                "status": "closed",
            },
        ]
        self.scan = {
            "success": True,
            "started_at": "2026-08-03T09:00:00-06:00",
            "completed_at": "2026-08-03T16:00:00Z",
            "companies_checked": 2,
            "jobs_fetched": 8,
            "jobs_retained": 3,
            "jobs_inserted": 1,
            "jobs_updated": 1,
            "jobs_unchanged": 1,
            "exclusion_counts": {"location": 2, "seniority": 1},
            "coverage_report_path": "jobs/coverage.md",
            "ranked_artifact_path": "jobs/ranked.json",
        }
        self.companies = [
            {"name": "Beta", "platform": "greenhouse", "enabled": False},
            {"name": "Alpha", "platform": "ashby", "enabled": True},
        ]

    def publish(self):
        return self.publisher.publish(
            active_jobs=self.jobs,
            scan_run=self.scan,
            companies=self.companies,
            company_statuses={"Alpha": "fetched", "Beta": "failed_technical"},
        )

    def test_creates_missing_tabs_and_selects_existing_managed_tab(self):
        existing = self.spreadsheet.sheets["New Jobs"]

        self.publish()

        self.assertIs(self.spreadsheet.sheets["New Jobs"], existing)
        self.assertEqual(set(self.spreadsheet.sheets), set(MANAGED_TABS) | {"Applications"})
        self.assertEqual(
            {title for title, _rows, _cols in self.spreadsheet.add_calls},
            set(MANAGED_TABS) - {"New Jobs"},
        )

    def test_writes_new_and_active_job_headers_and_rows(self):
        counts = self.publish()

        new_values = self.spreadsheet.sheets["New Jobs"].values
        active_values = self.spreadsheet.sheets["Active Jobs"].values
        self.assertEqual(new_values[0], NEW_JOBS_HEADERS)
        new_source_key = NEW_JOBS_HEADERS.index("Source Key")
        self.assertEqual(
            [row[new_source_key] for row in new_values[1:]],
            ["job-higher-score", "job-newer"],
        )
        self.assertEqual(active_values[0], ACTIVE_JOBS_HEADERS)
        active_source_key = ACTIVE_JOBS_HEADERS.index("Source Key")
        self.assertEqual(
            [row[active_source_key] for row in active_values[1:]],
            ["job-old", "job-higher-score", "job-newer"],
        )
        self.assertEqual(counts, {"new_jobs": 2, "active_jobs": 3, "companies": 2})

    def test_writes_system_status_and_company_rows(self):
        self.publish()

        status_values = self.spreadsheet.sheets["System Status"].values
        company_values = self.spreadsheet.sheets["Companies"].values
        self.assertEqual(status_values[0], SYSTEM_STATUS_HEADERS)
        status = dict(status_values[1:])
        self.assertEqual(status["Most Recent Scan Status"], "success")
        self.assertEqual(status["Companies Checked"], 2)
        self.assertEqual(status["Exclusion Counts"], "location=2; seniority=1")
        self.assertEqual(status["Collector Status: Beta"], "failed_technical")
        self.assertEqual(company_values[0], COMPANIES_HEADERS)
        self.assertEqual(
            company_values[1:],
            [
                ["Alpha", "ashby", "yes", "fetched", "canonical_source", "", ""],
                [
                    "Beta",
                    "greenhouse",
                    "no",
                    "failed_technical",
                    "canonical_source",
                    "",
                    "",
                ],
            ],
        )

    def test_never_touches_applications(self):
        applications = self.spreadsheet.sheets["Applications"]

        self.publish()

        self.assertEqual(applications.clear_calls, 0)
        self.assertEqual(applications.update_calls, [])
        self.assertNotIn("Applications", self.spreadsheet.worksheet_calls)
        self.assertNotIn(
            "Applications",
            [title for title, _rows, _cols in self.spreadsheet.add_calls],
        )
        formatted_sheet_ids = {
            request_sheet_id(request)
            for call in self.spreadsheet.batch_update_calls
            for request in call["requests"]
        }
        self.assertNotIn(applications.id, formatted_sheet_ids)

    def test_empty_jobs_still_write_headers(self):
        self.publisher.publish(
            active_jobs=[],
            scan_run=None,
            companies=[],
        )

        self.assertEqual(self.spreadsheet.sheets["New Jobs"].values, [NEW_JOBS_HEADERS])
        self.assertEqual(
            self.spreadsheet.sheets["Active Jobs"].values,
            [ACTIVE_JOBS_HEADERS],
        )
        self.assertEqual(
            self.spreadsheet.sheets["Companies"].values,
            [COMPANIES_HEADERS],
        )
        self.assertEqual(
            self.spreadsheet.sheets["System Status"].values[0],
            SYSTEM_STATUS_HEADERS,
        )
        formatted_sheet_ids = {
            request_sheet_id(request)
            for request in self.spreadsheet.batch_update_calls[-1]["requests"]
        }
        self.assertEqual(
            formatted_sheet_ids,
            {self.spreadsheet.sheets[title].id for title in MANAGED_TABS},
        )
        body = self.spreadsheet.batch_update_calls[-1]
        for title in ("New Jobs", "Active Jobs", "Companies"):
            requests = requests_for_sheet(body, self.spreadsheet.sheets[title].id)
            filters = [
                request["setBasicFilter"]["filter"]["range"]
                for request in requests
                if "setBasicFilter" in request
            ]
            self.assertEqual(filters[0]["endRowIndex"], 1, title)

    def test_formats_each_managed_tab_with_frozen_bold_filtered_headers(self):
        self.publish()

        body = self.spreadsheet.batch_update_calls[-1]
        for title in MANAGED_TABS:
            sheet_id = self.spreadsheet.sheets[title].id
            requests = requests_for_sheet(body, sheet_id)
            self.assertTrue(requests, title)
            self.assertTrue(
                any(
                    request.get("updateSheetProperties", {})
                    .get("properties", {})
                    .get("gridProperties", {})
                    .get("frozenRowCount")
                    == 1
                    for request in requests
                ),
                title,
            )
            self.assertTrue(
                any(
                    request.get("repeatCell", {})
                    .get("cell", {})
                    .get("userEnteredFormat", {})
                    .get("textFormat", {})
                    .get("bold")
                    is True
                    for request in requests
                ),
                title,
            )
            filters = [
                request["setBasicFilter"]
                for request in requests
                if "setBasicFilter" in request
            ]
            self.assertEqual(len(filters), 1, title)
            self.assertEqual(filters[0]["filter"]["range"]["startRowIndex"], 0)
            self.assertEqual(
                filters[0]["filter"]["range"]["endRowIndex"],
                len(self.spreadsheet.sheets[title].values),
            )
            header_heights = [
                request["updateDimensionProperties"]["properties"]["pixelSize"]
                for request in requests
                if request.get("updateDimensionProperties", {})
                .get("range", {})
                .get("dimension")
                == "ROWS"
            ]
            self.assertEqual(header_heights, [32], title)

    def test_requests_expected_fixed_column_widths(self):
        self.publish()

        body = self.spreadsheet.batch_update_calls[-1]
        headers_by_title = {
            "New Jobs": NEW_JOBS_HEADERS,
            "Active Jobs": ACTIVE_JOBS_HEADERS,
            "System Status": SYSTEM_STATUS_HEADERS,
            "Companies": COMPANIES_HEADERS,
        }
        for title, headers in headers_by_title.items():
            requests = requests_for_sheet(body, self.spreadsheet.sheets[title].id)
            requested_widths = {
                request["updateDimensionProperties"]["range"]["startIndex"]:
                    request["updateDimensionProperties"]["properties"]["pixelSize"]
                for request in requests
                if request.get("updateDimensionProperties", {})
                .get("range", {})
                .get("dimension")
                == "COLUMNS"
            }
            self.assertEqual(
                requested_widths,
                {
                    index: TAB_FORMATS[title]["widths"][header]
                    for index, header in enumerate(headers)
                },
                title,
            )

    def test_wraps_long_text_columns_and_top_aligns_populated_cells(self):
        self.publish()

        body = self.spreadsheet.batch_update_calls[-1]
        headers_by_title = {
            "New Jobs": NEW_JOBS_HEADERS,
            "Active Jobs": ACTIVE_JOBS_HEADERS,
            "System Status": SYSTEM_STATUS_HEADERS,
            "Companies": COMPANIES_HEADERS,
        }
        for title, headers in headers_by_title.items():
            requests = requests_for_sheet(body, self.spreadsheet.sheets[title].id)
            wrapped_indexes = {
                request["repeatCell"]["range"]["startColumnIndex"]
                for request in requests
                if request.get("repeatCell", {})
                .get("cell", {})
                .get("userEnteredFormat", {})
                .get("wrapStrategy")
                == "WRAP"
            }
            self.assertEqual(
                wrapped_indexes,
                {
                    index
                    for index, header in enumerate(headers)
                    if header in TAB_FORMATS[title]["wrapped"]
                },
                title,
            )
            self.assertTrue(
                any(
                    request.get("repeatCell", {})
                    .get("cell", {})
                    .get("userEnteredFormat", {})
                    .get("verticalAlignment")
                    == "TOP"
                    for request in requests
                ),
                title,
            )

    def test_repeated_publication_reuses_worksheets_and_replaces_filters(self):
        self.publish()
        first_add_calls = list(self.spreadsheet.add_calls)

        self.publish()

        self.assertEqual(self.spreadsheet.add_calls, first_add_calls)
        self.assertEqual(len(self.spreadsheet.sheets), len(MANAGED_TABS) + 1)
        self.assertEqual(len(self.spreadsheet.basic_filters), len(MANAGED_TABS))
        for body in self.spreadsheet.batch_update_calls:
            filter_sheet_ids = [
                request_sheet_id(request)
                for request in body["requests"]
                if "setBasicFilter" in request
            ]
            self.assertEqual(len(filter_sheet_ids), len(set(filter_sheet_ids)))

    def test_freshness_deadline_and_lifecycle_columns_are_visible(self):
        self.publish()
        values = self.spreadsheet.sheets["Active Jobs"].values
        row = next(
            item for item in values[1:]
            if item[ACTIVE_JOBS_HEADERS.index("Source Key")] == "job-newer"
        )
        self.assertEqual(row[ACTIVE_JOBS_HEADERS.index("Freshness")], "new")
        self.assertEqual(row[ACTIVE_JOBS_HEADERS.index("Age Days")], 1)
        self.assertEqual(
            row[ACTIVE_JOBS_HEADERS.index("Urgency")],
            "high",
        )
        self.assertEqual(
            row[ACTIVE_JOBS_HEADERS.index("Closes At")],
            "2026-08-06T00:00:00Z",
        )
        self.assertEqual(row[ACTIVE_JOBS_HEADERS.index("Lifecycle")], "active")

    def test_lists_timestamps_and_nulls_serialize_consistently(self):
        self.publish()

        new_values = self.spreadsheet.sheets["New Jobs"].values
        newer = next(
            row for row in new_values[1:]
            if row[NEW_JOBS_HEADERS.index("Source Key")] == "job-newer"
        )
        self.assertEqual(newer[NEW_JOBS_HEADERS.index("Primary Category")], "")
        self.assertEqual(
            newer[NEW_JOBS_HEADERS.index("Matched Categories")],
            "software_engineering; embedded_firmware",
        )
        self.assertEqual(newer[NEW_JOBS_HEADERS.index("Location")], "")
        self.assertEqual(
            newer[NEW_JOBS_HEADERS.index("First Seen")],
            "2026-08-03T10:00:00Z",
        )
        self.assertEqual(
            newer[NEW_JOBS_HEADERS.index("Source Posted")],
            "2026-08-02T14:30:45Z",
        )
        self.assertEqual(
            newer[NEW_JOBS_HEADERS.index("Source Updated")],
            "2026-08-02T14:30:45Z",
        )
        self.assertEqual(
            newer[NEW_JOBS_HEADERS.index("Positive Signals")],
            "intern; software",
        )
        active = self.spreadsheet.sheets["Active Jobs"].values
        active_newer = next(
            row for row in active[1:]
            if row[ACTIVE_JOBS_HEADERS.index("Source Key")] == "job-newer"
        )
        self.assertEqual(
            active_newer[ACTIVE_JOBS_HEADERS.index("Last Seen")],
            "2026-08-03T11:00:00Z",
        )

    def test_source_posted_does_not_mislabel_source_updated_fallback(self):
        self.publish()

        values = self.spreadsheet.sheets["Active Jobs"].values
        row = next(
            item for item in values[1:]
            if item[ACTIVE_JOBS_HEADERS.index("Source Key")] == "job-higher-score"
        )
        self.assertEqual(row[ACTIVE_JOBS_HEADERS.index("Source Posted")], "")
        self.assertEqual(
            row[ACTIVE_JOBS_HEADERS.index("Source Updated")],
            "2026-08-01T00:00:00Z",
        )

    def test_human_and_malformed_source_dates_serialize_safely(self):
        self.jobs[0]["source_updated_at"] = "December  3, 2025"
        self.jobs[1]["source_updated_at"] = "not-a-date"

        self.publish()

        values = self.spreadsheet.sheets["Active Jobs"].values
        rows = {
            row[ACTIVE_JOBS_HEADERS.index("Source Key")]: row
            for row in values[1:]
        }
        self.assertEqual(
            rows["job-newer"][ACTIVE_JOBS_HEADERS.index("Source Updated")],
            "2025-12-03T00:00:00Z",
        )
        self.assertEqual(
            rows["job-higher-score"][ACTIVE_JOBS_HEADERS.index("Source Updated")],
            "",
        )

    def test_missing_google_credentials_raise_clear_runtime_error(self):
        with self.assertRaisesRegex(
            GoogleSheetsConfigurationError,
            "GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_PATH",
        ):
            GoogleSheetsPublisher.from_env(environ={})

    def test_missing_google_credential_file_raises_clear_runtime_error(self):
        with self.assertRaisesRegex(
            GoogleSheetsConfigurationError,
            "credential file was not found",
        ):
            GoogleSheetsPublisher.from_env(
                environ={
                    "GOOGLE_SHEET_ID": "sheet-id",
                    "GOOGLE_CREDENTIALS_PATH": "missing-service-account.json",
                }
            )

    def test_invalid_google_credential_path_raises_clear_runtime_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                GoogleSheetsConfigurationError,
                "must point to a service-account JSON file",
            ):
                GoogleSheetsPublisher.from_env(
                    environ={
                        "GOOGLE_SHEET_ID": "sheet-id",
                        "GOOGLE_CREDENTIALS_PATH": directory,
                    }
                )

    def test_initialization_error_preserves_sanitized_root_cause_diagnostics(self):
        client = Mock()
        api_error = google_api_error(
            403,
            "The caller does not have permission",
            error_status="PERMISSION_DENIED",
            reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        )
        permission_error = PermissionError()
        permission_error.__cause__ = api_error
        client.open_by_key.side_effect = permission_error
        with tempfile.NamedTemporaryFile(suffix=".json") as credentials_file:
            with self.assertRaises(GoogleSheetsConfigurationError) as raised:
                GoogleSheetsPublisher.from_env(
                    environ={
                        "GOOGLE_SHEET_ID": "sheet-id",
                        "GOOGLE_CREDENTIALS_PATH": credentials_file.name,
                    },
                    credentials_factory=Mock(return_value=object()),
                    authorize_factory=Mock(return_value=client),
                    sleep_func=Mock(),
                )

        diagnostic = str(raised.exception)
        self.assertIn("operation=open_by_key", diagnostic)
        self.assertIn("exception=PermissionError", diagnostic)
        self.assertIn("cause=APIError", diagnostic)
        self.assertIn("http_status=403", diagnostic)
        self.assertIn("google_code=403", diagnostic)
        self.assertIn("google_error=PERMISSION_DENIED", diagnostic)
        self.assertIn(
            "google_reason=ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            diagnostic,
        )
        self.assertIn("message=The caller does not have permission", diagnostic)
        self.assertIn("attempts=1", diagnostic)

    def test_publication_error_identifies_failing_operation(self):
        spreadsheet = FakeSpreadsheet((*MANAGED_TABS, "Applications"))
        spreadsheet.sheets["Active Jobs"].update = Mock(
            side_effect=google_api_error(
                403,
                "Permission denied for requested range",
                error_status="PERMISSION_DENIED",
            )
        )
        publisher = GoogleSheetsPublisher(
            spreadsheet,
            now_factory=lambda: self.now,
            sleep_func=Mock(),
        )

        with self.assertRaises(GoogleSheetsAccessError) as raised:
            publisher.publish(
                active_jobs=[],
                scan_run=None,
                companies=[],
            )

        diagnostic = str(raised.exception)
        self.assertIn("operation=update: Active Jobs", diagnostic)
        self.assertIn("exception=APIError", diagnostic)
        self.assertIn("http_status=403", diagnostic)
        self.assertIn("google_error=PERMISSION_DENIED", diagnostic)
        self.assertIn("attempts=1", diagnostic)

    def test_transient_status_retries_with_bounded_attempt_count(self):
        spreadsheet = FakeSpreadsheet()
        spreadsheet.worksheets = Mock(
            side_effect=google_api_error(
                503,
                "Backend temporarily unavailable",
                error_status="UNAVAILABLE",
                reason="backendError",
            )
        )
        sleep_calls = []
        publisher = GoogleSheetsPublisher(
            spreadsheet,
            sleep_func=sleep_calls.append,
            retry_delays=(0.01, 0.02),
        )

        with self.assertLogs(
            "src.integrations.google_sheets_publisher",
            level="WARNING",
        ) as logs:
            with self.assertRaises(GoogleSheetsAccessError) as raised:
                publisher.publish(
                    active_jobs=[],
                    scan_run=None,
                    companies=[],
                )

        self.assertEqual(spreadsheet.worksheets.call_count, 3)
        self.assertEqual(sleep_calls, [0.01, 0.02])
        self.assertIn("operation=list_worksheets", str(raised.exception))
        self.assertIn("http_status=503", str(raised.exception))
        self.assertIn("attempts=3", str(raised.exception))
        self.assertEqual(len(logs.output), 2)

    def test_permanent_status_does_not_retry(self):
        spreadsheet = FakeSpreadsheet()
        spreadsheet.worksheets = Mock(
            side_effect=google_api_error(
                404,
                "Requested spreadsheet was not found",
                error_status="NOT_FOUND",
            )
        )
        sleep_func = Mock()
        publisher = GoogleSheetsPublisher(
            spreadsheet,
            sleep_func=sleep_func,
            retry_delays=(0.01, 0.02),
        )

        with self.assertRaises(GoogleSheetsAccessError) as raised:
            publisher.publish(
                active_jobs=[],
                scan_run=None,
                companies=[],
            )

        self.assertEqual(spreadsheet.worksheets.call_count, 1)
        sleep_func.assert_not_called()
        self.assertIn("http_status=404", str(raised.exception))
        self.assertIn("attempts=1", str(raised.exception))

    def test_credentials_and_tokens_are_redacted_from_diagnostics_and_retry_logs(self):
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "PRIVATEKEYMATERIALSHOULDNEVERAPPEAR\n"
            "-----END PRIVATE KEY-----"
        )
        bearer_token = "ya29." + "A" * 48
        jwt_token = "eyJheader.eyJpayload.signaturevalue"
        short_token = "short-access-token-secret"
        opaque_value = "Z" * 48
        secret_message = (
            f"private_key={private_key} Bearer {bearer_token} jwt={jwt_token} "
            f"access_token={short_token} opaque={opaque_value} "
            "service-account@example.iam.gserviceaccount.com"
        )
        spreadsheet = FakeSpreadsheet()
        spreadsheet.worksheets = Mock(
            side_effect=google_api_error(
                503,
                secret_message,
                error_status="UNAVAILABLE",
            )
        )
        publisher = GoogleSheetsPublisher(
            spreadsheet,
            sleep_func=Mock(),
            retry_delays=(0.0, 0.0),
        )

        with self.assertLogs(
            "src.integrations.google_sheets_publisher",
            level="WARNING",
        ) as logs:
            with self.assertRaises(GoogleSheetsAccessError) as raised:
                publisher.publish(
                    active_jobs=[],
                    scan_run=None,
                    companies=[],
                )

        emitted = "\n".join(logs.output + [str(raised.exception)])
        for secret in (
            "PRIVATEKEYMATERIALSHOULDNEVERAPPEAR",
            bearer_token,
            jwt_token,
            short_token,
            opaque_value,
            "service-account@example.iam.gserviceaccount.com",
        ):
            self.assertNotIn(secret, emitted)
        self.assertIn("[REDACTED", emitted)

    def test_failed_sheets_write_raises_safe_access_error(self):
        self.spreadsheet.sheets["New Jobs"].protected = True

        with self.assertRaisesRegex(
            GoogleSheetsAccessError,
            "Google Sheets publication failed",
        ):
            self.publish()


if __name__ == "__main__":
    unittest.main()
