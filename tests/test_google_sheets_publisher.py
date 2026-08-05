import tempfile
import unittest
from datetime import datetime, timezone

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
                "first_seen_at": "2026-08-03T10:00:00Z",
                "last_seen_at": datetime(2026, 8, 3, 11, 0),
                "status": "active",
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
        self.assertEqual([row[12] for row in new_values[1:]], ["job-higher-score", "job-newer"])
        self.assertEqual(active_values[0], ACTIVE_JOBS_HEADERS)
        self.assertEqual(
            [row[12] for row in active_values[1:]],
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

    def test_lists_timestamps_and_nulls_serialize_consistently(self):
        self.publish()

        new_values = self.spreadsheet.sheets["New Jobs"].values
        newer = next(row for row in new_values[1:] if row[12] == "job-newer")
        self.assertEqual(newer[4], "")
        self.assertEqual(newer[5], "software_engineering; embedded_firmware")
        self.assertEqual(newer[6], "")
        self.assertEqual(newer[8], "2026-08-03T10:00:00Z")
        self.assertEqual(newer[9], "2026-08-02T14:30:45Z")
        self.assertEqual(newer[10], "intern; software")
        active = self.spreadsheet.sheets["Active Jobs"].values
        active_newer = next(row for row in active[1:] if row[12] == "job-newer")
        self.assertEqual(active_newer[9], "2026-08-03T11:00:00Z")

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

    def test_failed_sheets_write_raises_safe_access_error(self):
        self.spreadsheet.sheets["New Jobs"].protected = True

        with self.assertRaisesRegex(
            GoogleSheetsAccessError,
            "Google Sheets publication failed",
        ):
            self.publish()


if __name__ == "__main__":
    unittest.main()
