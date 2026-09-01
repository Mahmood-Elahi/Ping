import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from scripts import update_student_dashboard as dashboard
from src.integrations.google_sheets_publisher import GoogleSheetsAccessError
from src.integrations.supabase_job_store import DashboardData, SupabaseJobStore
from src.scraper.company_careers.models import (
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
)


class FakePipeline:
    def __init__(self, report, summary, *, search_error=None):
        self.company_careers_report = report
        self.company_careers_summary = summary
        self.search_error = search_error
        self.search_calls = 0

    def search_jobs(self):
        self.search_calls += 1
        if self.search_error is not None:
            raise self.search_error
        return []


class FakeStore:
    def __init__(self, *, sync_error=None):
        self.sync_error = sync_error
        self.sync_calls = []
        self.validate_calls = 0
        self.fetch_calls = 0
        self.failed_scan_calls = []
        self.dashboard_data = DashboardData(
            active_jobs=[
                {
                    "source_key": "persisted-job",
                    "company": "Example",
                    "title": "Software Intern",
                    "status": "active",
                    "first_seen_at": "2026-08-03T00:00:00Z",
                }
            ],
            latest_scan={"success": True},
        )

    def validate_setup(self):
        self.validate_calls += 1

    def sync_student_jobs(self, jobs, **kwargs):
        self.sync_calls.append((list(jobs), kwargs))
        if self.sync_error is not None:
            raise self.sync_error
        return SimpleNamespace(
            inserted=1,
            updated=0,
            unchanged=0,
            missing_incremented=0,
            closed=0,
        )

    def fetch_dashboard_data(self):
        self.fetch_calls += 1
        return self.dashboard_data

    def record_failed_scan(self, **kwargs):
        self.failed_scan_calls.append(kwargs)


class FakePublisher:
    def __init__(self, *, publish_error=None):
        self.publish_calls = []
        self.publish_error = publish_error

    def publish(self, **kwargs):
        self.publish_calls.append(kwargs)
        if self.publish_error is not None:
            raise self.publish_error
        return {"new_jobs": 1, "active_jobs": 1, "companies": 1}


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeSelectQuery:
    def __init__(self, table, data):
        self.table = table
        self.data = data
        self.calls = []

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def eq(self, column, value):
        self.calls.append(("eq", column, value))
        return self

    def order(self, column, *, desc=False):
        self.calls.append(("order", column, desc))
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        return self

    def execute(self):
        self.calls.append(("execute",))
        return FakeResponse(self.data)


class FakeDashboardClient:
    def __init__(self):
        self.requested_tables = []
        self.queries = {
            "tracked_jobs": FakeSelectQuery(
                "tracked_jobs", [{"source_key": "one", "status": "active"}]
            ),
            "scan_runs": FakeSelectQuery("scan_runs", [{"success": True}]),
        }

    def table(self, name):
        self.requested_tables.append(name)
        return self.queries[name]


class UpdateStudentDashboardTests(unittest.TestCase):
    def setUp(self):
        self.target = CompanyTarget(
            name="Example",
            platform="greenhouse",
            slug="example",
        )
        self.job = JobRecord(
            job_id="123",
            title="Software Intern",
            company="Example",
            location="Canada",
            url="https://example.com/123",
            updated_at="2026-08-01T00:00:00Z",
            source="greenhouse",
        )
        result = CompanyScanResult(
            target=self.target,
            jobs=[self.job],
            status=CoverageStatus.FETCHED,
            fetched_count=2,
            exclusion_counts={"job_type": 1},
        )
        self.report = CompanyCareersReport(results=[result])
        self.summary = {
            "companies_checked": 1,
            "jobs_fetched": 2,
            "jobs_retained": 1,
            "exclusion_counts": {"job_type": 1},
            "coverage_report_path": "jobs/coverage.md",
            "ranked_results_path": "jobs/ranked.json",
        }

    def test_dry_run_performs_no_supabase_google_pipeline_or_sqlite_work(self):
        output = io.StringIO()
        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline") as build_pipeline,
            patch.object(dashboard.SupabaseJobStore, "from_env") as store_factory,
            patch.object(dashboard.GoogleSheetsPublisher, "from_env") as publisher_factory,
            redirect_stdout(output),
        ):
            exit_code = dashboard.main(["--dry-run"])

        self.assertEqual(exit_code, 0)
        build_pipeline.assert_not_called()
        store_factory.assert_not_called()
        publisher_factory.assert_not_called()
        text = output.getvalue()
        self.assertIn("SUPABASE_URL", text)
        self.assertIn("GOOGLE_SHEET_ID", text)
        self.assertIn("SQLite writes: disabled", text)
        self.assertIn("No scan, artifacts, SQLite writes, Supabase calls, or Google calls", text)

    def test_audit_scans_without_supabase_sheets_or_sqlite(self):
        audit_summary = dict(
            self.summary,
            freshness={
                "new": 1,
                "fresh": 0,
                "normal": 0,
                "aging": 0,
                "stale": 0,
            },
            date_parse_error_count=2,
            date_parse_failures=[
                {"field": "source_posted_at", "reason": "unparseable"},
                {"field": "source_closes_at", "reason": "unparseable"},
            ],
            diagnostics={
                "total_normalized_jobs": 2,
                "before_retained_jobs": 1,
                "after_retained_jobs": 1,
                "exclusions_by_reason": {"job_type": 1},
                "newly_accepted_jobs_by_matching_rule": {},
                "location_distribution": {"eligible_canada": 1},
                "title_signal_distribution": {"intern": 1},
                "season_distribution": {"unspecified": 1},
            },
            diagnostics_path="jobs/diagnostics.json",
        )
        pipeline = FakePipeline(self.report, audit_summary)
        output = io.StringIO()
        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env") as store_factory,
            patch.object(dashboard.GoogleSheetsPublisher, "from_env") as publisher_factory,
            redirect_stdout(output),
        ):
            exit_code = dashboard.main(["--audit", "--no-freshness"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(pipeline.search_calls, 1)
        store_factory.assert_not_called()
        publisher_factory.assert_not_called()
        self.assertIn("Supabase: disabled", output.getvalue())
        self.assertIn("SQLite: disabled", output.getvalue())
        self.assertIn("Lifecycle/date parse errors: 2", output.getvalue())

    def test_skip_sheets_updates_supabase_without_google_calls(self):
        pipeline = FakePipeline(self.report, self.summary)
        store = FakeStore()
        output = io.StringIO()
        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            patch.object(dashboard.GoogleSheetsPublisher, "from_env") as publisher_factory,
            redirect_stdout(output),
        ):
            exit_code = dashboard.main(["--skip-sheets", "--no-freshness"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(store.validate_calls, 1)
        self.assertEqual(pipeline.search_calls, 1)
        self.assertEqual(len(store.sync_calls), 1)
        self.assertEqual(store.fetch_calls, 0)
        publisher_factory.assert_not_called()
        self.assertIn("Google Sheets: skipped", output.getvalue())

    def test_one_company_scan_feeds_supabase_and_sheets(self):
        pipeline = FakePipeline(self.report, self.summary)
        store = FakeStore()
        publisher = FakePublisher()
        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            patch.object(
                dashboard.GoogleSheetsPublisher,
                "from_env",
                return_value=publisher,
            ),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = dashboard.main(["--no-freshness"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(store.validate_calls, 1)
        self.assertEqual(pipeline.search_calls, 1)
        self.assertEqual(len(store.sync_calls), 1)
        synced_jobs, sync_options = store.sync_calls[0]
        self.assertEqual(len(synced_jobs), 1)
        self.assertEqual(sync_options["successful_companies"], ["Example"])
        self.assertEqual(store.fetch_calls, 1)
        self.assertEqual(len(publisher.publish_calls), 1)
        self.assertIs(
            publisher.publish_calls[0]["active_jobs"],
            store.dashboard_data.active_jobs,
        )

    def test_fatal_integration_error_returns_nonzero(self):
        pipeline = FakePipeline(self.report, self.summary)
        store = FakeStore(sync_error=RuntimeError("Supabase unavailable"))
        error_output = io.StringIO()
        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            redirect_stdout(io.StringIO()),
            redirect_stderr(error_output),
        ):
            exit_code = dashboard.main(["--skip-sheets", "--no-freshness"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(store.failed_scan_calls), 1)
        self.assertIn("Supabase unavailable", error_output.getvalue())

    def test_fatal_sheets_publication_error_returns_nonzero(self):
        pipeline = FakePipeline(self.report, self.summary)
        store = FakeStore()
        publisher = FakePublisher(
            publish_error=GoogleSheetsAccessError(
                "Google Sheets publication failed; operation=batch_format; "
                "exception=APIError; http_status=503; attempts=3"
            )
        )
        error_output = io.StringIO()
        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            patch.object(
                dashboard.GoogleSheetsPublisher,
                "from_env",
                return_value=publisher,
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(error_output),
        ):
            exit_code = dashboard.main(["--no-freshness"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(publisher.publish_calls), 1)
        self.assertIn("operation=batch_format", error_output.getvalue())
        self.assertIn("attempts=3", error_output.getvalue())

    def test_partial_company_failure_advances_only_successful_company(self):
        failed_target = CompanyTarget(
            name="Failed Co",
            platform="greenhouse",
            slug="failed",
        )
        partial_report = CompanyCareersReport(
            results=self.report.results
            + [
                CompanyScanResult(
                    target=failed_target,
                    status=CoverageStatus.FAILED_TECHNICAL,
                    error="collector unavailable",
                )
            ]
        )
        partial_summary = dict(self.summary, companies_checked=2)
        pipeline = FakePipeline(partial_report, partial_summary)
        store = FakeStore()

        with (
            patch.object(
                dashboard,
                "load_target_companies",
                return_value=[self.target, failed_target],
            ),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = dashboard.main(["--skip-sheets", "--no-freshness"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(store.sync_calls[0][1]["successful_companies"], ["Example"])

    def test_real_pipeline_builder_routes_timing_to_ranked_writer_not_filter(self):
        args = dashboard.parse_args(["--audit"])
        as_of = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        runtime = dashboard.FreshnessRuntime(
            config=dashboard.load_freshness_config(),
            previous_state=dashboard.empty_freshness_state(),
            as_of=as_of,
        )
        pipeline = dashboard.build_pipeline(
            args, [self.target], candidate_profile=None,
            freshness_runtime=runtime, as_of=as_of,
        )
        self.assertIs(
            pipeline.company_careers_ranked_writer.keywords["freshness_by_key"],
            runtime.annotations,
        )
        self.assertEqual(
            pipeline.company_careers_ranked_writer.keywords["now"], as_of
        )

    def test_complete_fatal_scan_does_not_call_sync(self):
        pipeline = FakePipeline(
            self.report,
            self.summary,
            search_error=RuntimeError("fatal collector failure"),
        )
        store = FakeStore()

        with (
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline", return_value=pipeline),
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = dashboard.main(["--skip-sheets", "--no-freshness"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(pipeline.search_calls, 1)
        self.assertEqual(store.sync_calls, [])
        self.assertEqual(len(store.failed_scan_calls), 1)

    def test_missing_sheets_setup_fails_before_scan(self):
        store = FakeStore()
        error_output = io.StringIO()

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(dashboard, "load_target_companies", return_value=[self.target]),
            patch.object(dashboard, "build_pipeline") as build_pipeline,
            patch.object(dashboard.SupabaseJobStore, "from_env", return_value=store),
            redirect_stdout(io.StringIO()),
            redirect_stderr(error_output),
        ):
            exit_code = dashboard.main(["--no-freshness"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(store.validate_calls, 1)
        build_pipeline.assert_not_called()
        self.assertIn("GOOGLE_SHEET_ID", error_output.getvalue())

    def test_dashboard_query_reads_only_jobs_and_scan_status(self):
        client = FakeDashboardClient()
        store = SupabaseJobStore(
            client,
            now_factory=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
        )

        data = store.fetch_dashboard_data()

        self.assertEqual(client.requested_tables, ["tracked_jobs", "scan_runs"])
        self.assertEqual(data.active_jobs[0]["source_key"], "one")
        self.assertTrue(data.latest_scan["success"])
        self.assertIn(("eq", "status", "active"), client.queries["tracked_jobs"].calls)


if __name__ == "__main__":
    unittest.main()
