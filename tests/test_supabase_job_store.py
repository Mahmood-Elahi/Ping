import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.integrations.supabase_job_store import (
    SupabaseAccessError,
    SupabaseConfigurationError,
    SupabaseJobStore,
    generate_source_key,
    normalize_url,
)
from src.utils.job_search_pipeline import JobSearchPipeline


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return FakeResponse(self.callback())


class FailingQuery:
    def __init__(self, error):
        self.error = error

    def select(self, _columns):
        return self

    def limit(self, _count):
        return self

    def execute(self):
        raise self.error


class FailingClient:
    def __init__(self, error):
        self.error = error

    def table(self, _name):
        return FailingQuery(self.error)

    def rpc(self, _name, _params):
        return FailingQuery(self.error)


class FakeTable:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def insert(self, row):
        def perform_insert():
            if self.name != "scan_runs":
                raise AssertionError(f"Unexpected insert table: {self.name}")
            self.client.scan_runs.append(copy.deepcopy(row))
            return [copy.deepcopy(row)]

        return FakeRequest(perform_insert)


class FakeSupabaseClient:
    MUTABLE_FIELDS = {
        "company",
        "title",
        "location",
        "department",
        "apply_url",
        "source_url",
        "source_platform",
        "primary_category",
        "matched_categories",
        "relevance_score",
        "relevance_band",
        "positive_signals",
        "negative_signals",
        "location_status",
        "source_updated_at",
        "raw_data",
    }

    def __init__(self):
        self.tracked_jobs = {}
        self.scan_runs = []
        self.applications = {}
        self.rpc_calls = []

    def rpc(self, name, params):
        if name != "sync_student_job_scan":
            raise AssertionError(f"Unexpected RPC: {name}")
        self.rpc_calls.append((name, copy.deepcopy(params)))
        return FakeRequest(lambda: self._sync(params))

    def table(self, name):
        return FakeTable(self, name)

    def _sync(self, params):
        incoming = {
            row["source_key"]: copy.deepcopy(row)
            for row in params["p_jobs"]
        }
        inserted = 0
        updated = 0
        unchanged = 0

        for source_key, incoming_row in incoming.items():
            existing = self.tracked_jobs.get(source_key)
            if existing is None:
                inserted += 1
                incoming_row["id"] = f"job-{len(self.tracked_jobs) + 1}"
                self.tracked_jobs[source_key] = incoming_row
                continue

            changed = (
                any(existing.get(field) != incoming_row.get(field) for field in self.MUTABLE_FIELDS)
                or existing.get("status") != "active"
                or existing.get("missing_successful_runs") != 0
            )
            if changed:
                updated += 1
            else:
                unchanged += 1
            first_seen_at = existing["first_seen_at"]
            row_id = existing["id"]
            existing.update(incoming_row)
            existing["first_seen_at"] = first_seen_at
            existing["id"] = row_id
            existing["status"] = "active"
            existing["missing_successful_runs"] = 0

        successful_companies = set(params["p_successful_companies"])
        missing_incremented = 0
        closed = 0
        threshold = params["p_close_after_missing_runs"]
        for source_key, row in self.tracked_jobs.items():
            if row["company"] not in successful_companies:
                continue
            if row["status"] == "closed" or source_key in incoming:
                continue
            row["missing_successful_runs"] += 1
            missing_incremented += 1
            if row["missing_successful_runs"] >= threshold:
                row["status"] = "closed"
                closed += 1

        result = {
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "missing_incremented": missing_incremented,
            "closed": closed,
        }
        scan = copy.deepcopy(params["p_scan"])
        scan.update(
            {
                "success": True,
                "jobs_inserted": inserted,
                "jobs_updated": updated,
                "jobs_unchanged": unchanged,
                "error_message": None,
            }
        )
        self.scan_runs.append(scan)
        return result


class MutableClock:
    def __init__(self):
        self.current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self):
        self.current += timedelta(hours=1)


class SupabaseJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabaseClient()
        self.clock = MutableClock()
        self.store = SupabaseJobStore(self.client, now_factory=self.clock)
        self.started_at = datetime(2026, 8, 3, 11, 59, tzinfo=timezone.utc)

    def job(self, **overrides):
        job = {
            "job_id": "job-123",
            "title": "Embedded Software Engineering Intern",
            "company": "Example",
            "location": "Calgary, Canada",
            "url": "https://jobs.example.com/postings/job-123",
            "updated_at": "2026-08-01T10:00:00Z",
            "source": "greenhouse:example",
            "departments": ["Engineering"],
            "offices": ["Calgary"],
            "description": "Build embedded systems.",
            "relevance_score": 91,
            "relevance_band": "high",
            "matched_categories": ["embedded_firmware"],
            "positive_signals": ["intern", "eligible_canada"],
            "negative_signals": [],
            "location_status": "eligible_canada",
        }
        job.update(overrides)
        return job

    def sync(self, jobs, successful_companies=("Example",)):
        return self.store.sync_student_jobs(
            jobs,
            successful_companies=successful_companies,
            started_at=self.started_at,
            companies_checked=len(successful_companies),
            jobs_fetched=len(jobs),
            exclusion_counts={"seniority": 2},
            coverage_report_path="jobs/coverage.md",
            ranked_artifact_path="jobs/ranked.json",
        )

    def tracked(self, job=None):
        source_key = generate_source_key(job or self.job())
        return self.client.tracked_jobs[source_key]

    def test_stable_source_key_prefers_namespaced_external_id(self):
        first = generate_source_key(self.job())
        changed_url = generate_source_key(
            self.job(url="https://jobs.example.com/postings/job-123?utm_source=email")
        )
        self.assertEqual(first, "id:greenhouse:example:job-123")
        self.assertEqual(first, changed_url)

    def test_repeated_calls_produce_the_same_fallback_key(self):
        job = self.job(job_id="", url="", title="  Firmware   Intern ")
        self.assertEqual(generate_source_key(job), generate_source_key(copy.deepcopy(job)))
        self.assertTrue(generate_source_key(job).startswith("sha256:"))

    def test_url_normalization_removes_inconsequential_differences(self):
        first = normalize_url(
            "HTTPS://Jobs.Example.com/roles/123/?utm_source=email&team=firmware#apply"
        )
        second = normalize_url("https://jobs.example.com/roles/123?team=firmware")
        self.assertEqual(first, second)

    def test_distinct_ids_and_material_urls_are_not_merged(self):
        self.assertNotEqual(
            generate_source_key(self.job(job_id="one")),
            generate_source_key(self.job(job_id="two")),
        )
        first = self.job(job_id="", url="https://jobs.example.com/roles/one")
        second = self.job(job_id="", url="https://jobs.example.com/roles/two")
        self.assertNotEqual(generate_source_key(first), generate_source_key(second))

    def test_first_discovery_is_an_insert(self):
        result = self.sync([self.job()])
        self.assertEqual((result.inserted, result.updated, result.unchanged), (1, 0, 0))
        self.assertEqual(len(self.client.tracked_jobs), 1)
        self.assertTrue(self.client.scan_runs[-1]["success"])

    def test_repeated_identical_discovery_is_unchanged_and_preserves_first_seen(self):
        self.sync([self.job()])
        original_first_seen = self.tracked()["first_seen_at"]
        original_last_seen = self.tracked()["last_seen_at"]
        self.clock.advance()
        result = self.sync([self.job()])
        self.assertEqual((result.inserted, result.updated, result.unchanged), (0, 0, 1))
        self.assertEqual(self.tracked()["first_seen_at"], original_first_seen)
        self.assertNotEqual(self.tracked()["last_seen_at"], original_last_seen)

    def test_changed_score_or_metadata_is_an_update(self):
        self.sync([self.job()])
        original_first_seen = self.tracked()["first_seen_at"]
        self.clock.advance()
        result = self.sync([self.job(relevance_score=77, relevance_band="medium")])
        self.assertEqual((result.inserted, result.updated, result.unchanged), (0, 1, 0))
        self.assertEqual(self.tracked()["relevance_score"], 77)
        self.assertEqual(self.tracked()["first_seen_at"], original_first_seen)

    def test_rediscovery_resets_missing_count_and_reopens_job(self):
        self.sync([self.job()])
        self.tracked()["missing_successful_runs"] = 2
        self.tracked()["status"] = "closed"
        result = self.sync([self.job()])
        self.assertEqual(result.updated, 1)
        self.assertEqual(self.tracked()["missing_successful_runs"], 0)
        self.assertEqual(self.tracked()["status"], "active")

    def test_one_missing_successful_run_does_not_close_job(self):
        self.sync([self.job()])
        result = self.sync([])
        self.assertEqual(result.missing_incremented, 1)
        self.assertEqual(result.closed, 0)
        self.assertEqual(self.tracked()["missing_successful_runs"], 1)
        self.assertEqual(self.tracked()["status"], "active")

    def test_two_missing_successful_runs_close_job(self):
        self.sync([self.job()])
        self.sync([])
        result = self.sync([])
        self.assertEqual(result.closed, 1)
        self.assertEqual(self.tracked()["missing_successful_runs"], 2)
        self.assertEqual(self.tracked()["status"], "closed")

    def test_failed_company_scan_does_not_increment_missing_count(self):
        self.sync([self.job()])
        self.sync([], successful_companies=())
        self.assertEqual(self.tracked()["missing_successful_runs"], 0)
        self.assertEqual(self.tracked()["status"], "active")

    def test_unscanned_companies_remain_untouched(self):
        other = self.job(
            job_id="other-1",
            company="Other",
            source="lever:other",
            url="https://jobs.other.example/other-1",
        )
        self.sync([self.job(), other], successful_companies=("Example", "Other"))
        other_before = copy.deepcopy(self.tracked(other))
        self.sync([self.job()], successful_companies=("Example",))
        self.assertEqual(self.tracked(other), other_before)

    def test_applications_are_never_overwritten(self):
        self.sync([self.job()])
        row = self.tracked()
        application = {
            "job_id": row["id"],
            "application_status": "interview",
            "notes": "Manual note",
        }
        self.client.applications[row["id"]] = copy.deepcopy(application)
        self.sync([self.job(relevance_score=80)])
        self.assertEqual(self.client.applications[row["id"]], application)

    def test_failed_overall_scan_is_recorded_without_changing_jobs(self):
        self.sync([self.job()])
        before = copy.deepcopy(self.tracked())
        self.store.record_failed_scan(
            started_at=self.started_at,
            error_message="collector failed",
            companies_checked=1,
        )
        self.assertEqual(self.tracked(), before)
        self.assertFalse(self.client.scan_runs[-1]["success"])

    def test_missing_credentials_raise_only_when_client_is_created(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                SupabaseConfigurationError,
                "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY",
            ):
                SupabaseJobStore.from_env()

    def test_setup_validation_reports_schema_not_applied(self):
        store = SupabaseJobStore(
            FailingClient(RuntimeError("PGRST205 table not in schema cache"))
        )
        with self.assertRaisesRegex(
            SupabaseAccessError,
            r"sql/supabase_schema\.sql",
        ):
            store.validate_setup()

    def test_supabase_access_error_does_not_expose_backend_details(self):
        store = SupabaseJobStore(
            FailingClient(RuntimeError("network failure with secret-value"))
        )
        with self.assertRaises(SupabaseAccessError) as raised:
            store.sync_student_jobs(
                [self.job()],
                successful_companies=("Example",),
                started_at=self.started_at,
                companies_checked=1,
                jobs_fetched=1,
            )

        self.assertIn("Supabase access failed", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))

    def test_normal_non_student_pipeline_does_not_require_supabase_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=True):
                pipeline = JobSearchPipeline(
                    keywords="Software Engineer",
                    output_dir=tmpdir,
                    scrapers=["company_careers"],
                    use_database=False,
                )
        self.assertIsNone(pipeline.company_careers_report)


if __name__ == "__main__":
    unittest.main()
