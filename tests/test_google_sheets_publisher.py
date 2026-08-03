import unittest
from datetime import datetime, timezone

from src.integrations.google_sheets_publisher import (
    ACTIVE_JOBS_HEADERS,
    COMPANIES_HEADERS,
    MANAGED_TABS,
    NEW_JOBS_HEADERS,
    SYSTEM_STATUS_HEADERS,
    GoogleSheetsConfigurationError,
    GoogleSheetsPublisher,
)


class FakeWorksheet:
    def __init__(self, title, *, protected=False):
        self.title = title
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
            title: FakeWorksheet(title, protected=title == "Applications")
            for title in titles
        }
        self.add_calls = []

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, *, title, rows, cols):
        self.add_calls.append((title, rows, cols))
        worksheet = FakeWorksheet(title)
        self.sheets[title] = worksheet
        return worksheet


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
                ["Alpha", "ashby", "yes", "fetched"],
                ["Beta", "greenhouse", "no", "failed_technical"],
            ],
        )

    def test_never_touches_applications(self):
        applications = self.spreadsheet.sheets["Applications"]

        self.publish()

        self.assertEqual(applications.clear_calls, 0)
        self.assertEqual(applications.update_calls, [])

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


if __name__ == "__main__":
    unittest.main()
