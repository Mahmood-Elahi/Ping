import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts import validate_supabase_dashboard_reads as preflight
from src.integrations.supabase_job_store import DashboardData


class ValidateSupabaseDashboardReadsTests(unittest.TestCase):
    def test_validation_reports_expected_counts_and_timings(self):
        state = {
            "jobs": {
                "one": {"source_key": "one"},
                "two": {"source_key": "two"},
            }
        }
        dashboard = DashboardData(
            active_jobs=[{"source_key": "one", "status": "active"}],
            latest_scan={"id": "scan"},
        )

        diagnostics, failures = preflight.validate_read_results(
            state,
            dashboard,
            setup_seconds=0.1119,
            lifecycle_seconds=1.2349,
            dashboard_seconds=2.3459,
        )

        self.assertEqual(failures, [])
        self.assertEqual(
            diagnostics,
            {
                "lifecycle_rows": 2,
                "lifecycle_unique_keys": 2,
                "lifecycle_duplicates": 0,
                "lifecycle_seconds": 1.235,
                "active_jobs": 1,
                "active_unique_keys": 1,
                "active_duplicates": 0,
                "dashboard_seconds": 2.346,
                "latest_scan_present": True,
                "setup_seconds": 0.112,
            },
        )

    def test_validation_rejects_every_requested_invariant_failure(self):
        state = {
            "jobs": [
                {"source_key": ""},
                {"source_key": "duplicate"},
                {"source_key": "duplicate"},
            ]
        }
        dashboard = DashboardData(
            active_jobs=[
                {"source_key": "active-duplicate", "status": "active"},
                {"source_key": "active-duplicate", "status": "closed"},
                {"source_key": "", "status": "active"},
            ],
            latest_scan=None,
        )

        diagnostics, failures = preflight.validate_read_results(
            state,
            dashboard,
            setup_seconds=0,
            lifecycle_seconds=0,
            dashboard_seconds=0,
        )

        self.assertEqual(diagnostics["lifecycle_duplicates"], 1)
        self.assertEqual(diagnostics["active_duplicates"], 1)
        self.assertEqual(
            failures,
            [
                "lifecycle source_keys must be nonempty",
                "lifecycle source_keys must be unique",
                "active-job source_keys must be nonempty",
                "active-job source_keys must be unique",
                "every dashboard job must have status == 'active'",
                "latest_scan must be present for a populated dashboard",
            ],
        )

    def test_main_is_read_only_and_emits_json(self):
        class FakeStore:
            def __init__(self):
                self.calls = []

            def validate_setup(self):
                self.calls.append("validate_setup")

            def fetch_lifecycle_state(self):
                self.calls.append("fetch_lifecycle_state")
                return {"jobs": {"one": {"source_key": "one"}}}

            def fetch_dashboard_data(self):
                self.calls.append("fetch_dashboard_data")
                return DashboardData(
                    active_jobs=[{"source_key": "one", "status": "active"}],
                    latest_scan={"id": "scan"},
                )

        store = FakeStore()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(preflight, "load_dotenv") as load_dotenv,
            patch.object(preflight.SupabaseJobStore, "from_env", return_value=store),
            patch.object(
                preflight.time,
                "perf_counter",
                side_effect=[0.0, 0.1, 0.1, 0.3, 0.3, 0.6],
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = preflight.main()

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            store.calls,
            ["validate_setup", "fetch_lifecycle_state", "fetch_dashboard_data"],
        )
        load_dotenv.assert_called_once_with(dotenv_path=preflight.REPOSITORY_ROOT / ".env")
        diagnostics = json.loads(stdout.getvalue())
        self.assertEqual(diagnostics["setup_seconds"], 0.1)
        self.assertEqual(diagnostics["lifecycle_seconds"], 0.2)
        self.assertEqual(diagnostics["dashboard_seconds"], 0.3)

    def test_main_exits_nonzero_on_an_invariant_failure(self):
        class InvalidStore:
            def validate_setup(self):
                return None

            def fetch_lifecycle_state(self):
                return {"jobs": {"one": {"source_key": "one"}}}

            def fetch_dashboard_data(self):
                return DashboardData(
                    active_jobs=[{"source_key": "one", "status": "closed"}],
                    latest_scan=None,
                )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(preflight, "load_dotenv"),
            patch.object(
                preflight.SupabaseJobStore,
                "from_env",
                return_value=InvalidStore(),
            ),
            patch.object(
                preflight.time,
                "perf_counter",
                side_effect=[0.0, 0.1, 0.1, 0.2, 0.2, 0.3],
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = preflight.main()

        self.assertEqual(result, 1)
        self.assertFalse(json.loads(stdout.getvalue())["latest_scan_present"])
        self.assertIn("status == 'active'", stderr.getvalue())
        self.assertIn("latest_scan must be present", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
