import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.integrations.supabase_job_store import (
    SupabaseAccessError,
    SupabaseConfigurationError,
    SupabaseJobStore,
    generate_source_key,
    normalize_url,
    tracked_job_payload,
)
from src.utils.job_search_pipeline import JobSearchPipeline


class FakeResponse:
    def __init__(self, data, *, count=None):
        self.data = data
        self.count = count


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return FakeResponse(self.callback())


class FailingQuery:
    def __init__(self, error):
        self.error = error

    def select(self, _columns, **_kwargs):
        return self

    def eq(self, _field, _value):
        return self

    def order(self, _field, desc=False):
        return self

    def range(self, _start, _end):
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
        self.filters = []
        self.row_limit = None
        self.row_range = None
        self.orderings = []
        self.request_count = False
        self.updated_values = None
        self.selected_columns = None

    def select(self, columns, *, count=None):
        self.selected_columns = columns
        self.request_count = count == "exact"
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def gt(self, field, value):
        self.filters.append((f"{field}__gt", value))
        return self

    def order(self, field, desc=False):
        self.orderings.append((field, desc))
        return self

    def limit(self, count):
        self.row_limit = count
        return self

    def range(self, start, end):
        self.row_range = (start, end)
        self.client.range_calls.setdefault(self.name, []).append((start, end))
        return self

    def execute(self):
        self.client.select_calls.append(
            {
                "table": self.name,
                "columns": self.selected_columns,
                "count_exact": self.request_count,
                "filters": list(self.filters),
                "orderings": list(self.orderings),
                "limit": self.row_limit,
                "range": self.row_range,
            }
        )
        if self.name == "tracked_jobs" and self.client.tracked_query_failures:
            failure = self.client.tracked_query_failures.pop(0)
            if failure is not None:
                raise failure
        if self.updated_values is not None:
            if self.name != "scan_runs":
                raise AssertionError(f"Unexpected update table: {self.name}")
            rows = []
            for row in self.client.scan_runs:
                if all(row.get(field) == value for field, value in self.filters):
                    row.update(copy.deepcopy(self.updated_values))
                    rows.append(copy.deepcopy(row))
            return FakeResponse(rows)
        if self.name == "tracked_jobs":
            rows = [copy.deepcopy(row) for row in self.client.tracked_jobs.values()]
        elif self.name == "scan_runs":
            rows = copy.deepcopy(self.client.scan_runs)
        elif self.name == "applications":
            rows = [copy.deepcopy(row) for row in self.client.applications.values()]
        else:
            rows = []
        for field, value in self.filters:
            if field.endswith("__gt"):
                actual_field = field.removesuffix("__gt")
                rows = [row for row in rows if row.get(actual_field) > value]
            else:
                rows = [row for row in rows if row.get(field) == value]
        total_count = len(rows)
        for field, desc in reversed(self.orderings):
            rows.sort(
                key=lambda row: (row.get(field) is not None, row.get(field)),
                reverse=desc,
            )
        if self.row_range is not None:
            start, end = self.row_range
            rows = rows[start : end + 1]
        elif self.name == "tracked_jobs":
            rows = rows[:1000]
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        return FakeResponse(rows, count=total_count if self.request_count else None)

    def insert(self, row):
        def perform_insert():
            if self.name != "scan_runs":
                raise AssertionError(f"Unexpected insert table: {self.name}")
            self.client.scan_runs.append(copy.deepcopy(row))
            return [copy.deepcopy(row)]

        return FakeRequest(perform_insert)

    def upsert(self, row, on_conflict=None):
        def perform_upsert():
            if self.name != "scan_runs":
                raise AssertionError(f"Unexpected upsert table: {self.name}")
            existing = next(
                (item for item in self.client.scan_runs if item.get("id") == row.get("id")),
                None,
            )
            if existing is None:
                self.client.scan_runs.append(copy.deepcopy(row))
            else:
                existing.update(copy.deepcopy(row))
            return [copy.deepcopy(row)]

        return FakeRequest(perform_upsert)

    def update(self, values):
        self.updated_values = copy.deepcopy(values)
        return self


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
        "source_posted_at",
        "source_closes_at",
        "first_seen_basis",
        "freshness_age_source",
        "age_days",
        "first_seen_age_hours",
        "freshness_band",
        "deadline_days_remaining",
        "urgency_band",
        "urgency_score",
        "urgency_reasons",
        "date_parse_errors",
        "is_ranked_output",
        "raw_data",
    }

    def __init__(self):
        self.tracked_jobs = {}
        self.scan_runs = []
        self.applications = {}
        self.source_syncs = {}
        self.rpc_calls = []
        self.range_calls = {}
        self.select_calls = []
        self.tracked_query_failures = []

    def rpc(self, name, params):
        if name != "sync_student_job_source":
            raise AssertionError(f"Unexpected RPC: {name}")
        self.rpc_calls.append((name, copy.deepcopy(params)))
        return FakeRequest(lambda: self._sync(params))

    def table(self, name):
        return FakeTable(self, name)

    def _sync(self, params):
        ledger_key = (params["p_scan_id"], params["p_company"])
        if ledger_key in self.source_syncs:
            return {**copy.deepcopy(self.source_syncs[ledger_key]), "replayed": True}
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
                any(
                    existing.get(field) != incoming_row.get(field)
                    for field in self.MUTABLE_FIELDS
                )
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
            existing["lifecycle_status"] = "active"
            existing["missing_successful_runs"] = 0

        missing_incremented = 0
        closed = 0
        threshold = params["p_close_after_missing_runs"]
        for source_key, row in self.tracked_jobs.items():
            if not params["p_reconcile_absence"] or row["company"] != params["p_company"]:
                continue
            if row["status"] == "closed" or source_key in incoming:
                continue
            row["missing_successful_runs"] += 1
            missing_incremented += 1
            if row["missing_successful_runs"] >= threshold:
                row["status"] = "closed"
                row["lifecycle_status"] = "removed"
                row["removed_at"] = params["p_seen_at"]
                closed += 1
            else:
                row["lifecycle_status"] = "possibly_missing"

        result = {
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "missing_incremented": missing_incremented,
            "closed": closed,
            "replayed": False,
        }
        self.source_syncs[ledger_key] = copy.deepcopy(result)
        return result


class ProviderFailure(RuntimeError):
    def __init__(self, message, *, code=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class SequencedSupabaseClient(FakeSupabaseClient):
    def __init__(self, failures, *, commit_before_failure=False):
        super().__init__()
        self.failures = list(failures)
        self.commit_before_failure = commit_before_failure

    def rpc(self, name, params):
        if name != "sync_student_job_source":
            raise AssertionError(f"Unexpected RPC: {name}")
        self.rpc_calls.append((name, copy.deepcopy(params)))

        def execute():
            failure = self.failures.pop(0) if self.failures else None
            if failure is not None and self.commit_before_failure:
                self._sync(params)
            if failure is not None:
                raise failure
            return self._sync(params)

        return FakeRequest(execute)


class MutableClock:
    def __init__(self):
        self.current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self):
        self.current += timedelta(hours=1)


class SupabaseJobStoreTests(unittest.TestCase):
    def test_schema_migration_preserves_legacy_lifecycle_evidence(self):
        schema = (
            Path(__file__).resolve().parents[1] / "sql" / "supabase_schema.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("when status = 'closed' then 'removed'", schema)
        self.assertIn(
            "when missing_successful_runs > 0 then 'possibly_missing'",
            schema,
        )
        self.assertIn("tracked_jobs_lifecycle_status_check", schema)
        self.assertIn("tracked_jobs_urgency_reasons_array_check", schema)
        self.assertIn("create table if not exists public.scan_source_syncs", schema)
        self.assertIn("create or replace function public.sync_student_job_source", schema)
        self.assertIn("pg_advisory_xact_lock", schema)
        self.assertIn("where p_reconcile_absence", schema)

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
        started_at = self.started_at
        self.started_at += timedelta(minutes=1)
        return self.store.sync_student_jobs(
            jobs,
            successful_companies=successful_companies,
            started_at=started_at,
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

    def test_conflicting_duplicate_source_identity_is_rejected_before_sync(self):
        first = self.job()
        conflicting = self.job(title="Different role with reused external ID")

        with self.assertRaisesRegex(ValueError, "same source identity"):
            self.store.sync_student_jobs(
                [first, conflicting],
                successful_companies=("Example",),
                started_at=self.started_at,
                companies_checked=1,
                jobs_fetched=2,
            )

        self.assertEqual(self.client.rpc_calls, [])
        self.assertEqual(self.client.scan_runs, [])

    def test_publication_failure_preserves_scan_counts_and_marks_run_failed(self):
        self.store.sync_student_jobs(
            [self.job()],
            successful_companies=("Example",),
            started_at=self.started_at,
            companies_checked=1,
            jobs_fetched=1,
            normalized_jobs=4,
            source_summary={"complete": 1, "incomplete": 0, "failed": 0},
            scan_runtime_seconds=12.5,
        )

        self.store.record_publication_result(
            started_at=self.started_at,
            status="failed",
            success=False,
            runtime_seconds=13.0,
            error_message="Sheets unavailable",
        )

        row = self.client.scan_runs[-1]
        self.assertFalse(row["success"])
        self.assertEqual(row["jobs_inserted"], 1)
        self.assertEqual(row["failure_stage"], "google_sheets_publication")
        self.assertTrue(row["diagnostics"]["scan_success"])
        self.assertEqual(row["diagnostics"]["publication_status"], "failed")
        self.assertEqual(row["diagnostics"]["normalized_jobs"], 4)

    def test_freshness_lifecycle_fields_are_mirrored_without_resetting_first_seen(self):
        lifecycle_job = self.job(
            first_seen_at="2026-07-01T12:00:00Z",
            last_seen_at="2026-08-31T12:00:00Z",
            source_posted_at="2026-06-30",
            source_closes_at="2026-09-03",
            first_seen_basis="observed_at_scan",
            lifecycle_status="active",
            missing_scan_count=0,
            freshness_age_source="source_posted_at",
            age_days=62,
            freshness_band="stale",
            deadline_days_remaining=3,
            first_seen_age_hours=1476,
            urgency_band="apply_now",
            urgency_score=100,
            urgency_reasons=["deadline_in_2_days"],
        )
        self.sync([lifecycle_job])
        row = self.tracked(lifecycle_job)
        self.assertEqual(row["first_seen_at"], "2026-07-01T12:00:00+00:00")
        self.assertEqual(row["source_posted_at"], "2026-06-30")
        self.assertEqual(row["source_closes_at"], "2026-09-03")
        self.assertEqual(row["freshness_band"], "stale")
        self.assertEqual(row["urgency_band"], "apply_now")

        state = self.store.fetch_lifecycle_state()
        entry = state["jobs"][generate_source_key(lifecycle_job)]
        self.assertEqual(entry["first_seen_at"], "2026-07-01T12:00:00+00:00")
        self.assertEqual(entry["source_closes_at"], "2026-09-03")
        self.assertEqual(entry["first_seen_basis"], "observed_at_scan")

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
        self.assertEqual(self.tracked()["lifecycle_status"], "active")

    def test_one_missing_successful_run_does_not_close_job(self):
        self.sync([self.job()])
        result = self.sync([])
        self.assertEqual(result.missing_incremented, 1)
        self.assertEqual(result.closed, 0)
        self.assertEqual(self.tracked()["missing_successful_runs"], 1)
        self.assertEqual(self.tracked()["status"], "active")
        self.assertEqual(self.tracked()["lifecycle_status"], "possibly_missing")

    def test_two_missing_successful_runs_close_job(self):
        self.sync([self.job()])
        self.sync([])
        result = self.sync([])
        self.assertEqual(result.closed, 1)
        self.assertEqual(self.tracked()["missing_successful_runs"], 2)
        self.assertEqual(self.tracked()["status"], "closed")
        self.assertEqual(self.tracked()["lifecycle_status"], "removed")
        self.assertIsNotNone(self.tracked()["removed_at"])

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

    def test_production_scale_payload_is_partitioned_by_source(self):
        jobs = [
            self.job(
                job_id=f"job-{index}",
                company=f"Company {index % 533:03d}",
                source=f"greenhouse:company-{index % 533:03d}",
                url=f"https://jobs.example.com/{index}",
            )
            for index in range(11669)
        ]
        companies = {job["company"] for job in jobs}
        result = self.store.sync_student_jobs(
            [],
            lifecycle_jobs=jobs,
            successful_companies=companies,
            started_at=self.started_at,
            companies_checked=len(companies),
            jobs_fetched=len(jobs),
        )

        self.assertEqual(result.inserted, 11669)
        self.assertEqual(len(self.client.rpc_calls), 533)
        self.assertLessEqual(
            max(len(params["p_jobs"]) for _, params in self.client.rpc_calls),
            22,
        )
        self.assertEqual(self.store.last_sync_diagnostics.payload_rows, 11669)

    def test_lifecycle_state_read_uses_deterministic_keyset_pages_without_gaps(self):
        expected_keys = []
        for index in range(3507):
            source_key = f"key-{index:05d}"
            expected_keys.append(source_key)
            self.client.tracked_jobs[source_key] = {
                **tracked_job_payload(self.job(), seen_at=self.clock()),
                "source_key": source_key,
            }
        state = self.store.fetch_lifecycle_state()
        actual_keys = list(state["jobs"])
        self.assertEqual(actual_keys, expected_keys)
        self.assertEqual(len(actual_keys), len(set(actual_keys)))
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual(len(calls), 8)
        self.assertEqual(calls[0]["filters"], [])
        self.assertEqual(calls[1]["filters"], [("source_key__gt", "key-00499")])
        self.assertTrue(
            all(call["orderings"] == [("source_key", False)] for call in calls)
        )
        self.assertTrue(all(call["limit"] == 500 for call in calls))
        self.assertTrue(all(call["range"] is None for call in calls))

    def test_lifecycle_state_read_requests_only_required_columns_without_count(self):
        self.client.tracked_jobs["key-00000"] = {
            **tracked_job_payload(self.job(), seen_at=self.clock()),
            "source_key": "key-00000",
            "raw_data": {"description": "x" * 100_000},
        }

        self.store.fetch_lifecycle_state()

        call = self.client.select_calls[-1]
        self.assertEqual(
            call["columns"],
            "source_key,company,title,location,apply_url,source_url,source_platform,"
            "first_seen_at,last_seen_at,first_seen_basis,source_posted_at,"
            "source_closes_at,removed_at,lifecycle_status,missing_successful_runs,"
            "date_parse_errors",
        )
        self.assertNotIn("raw_data", call["columns"])
        self.assertNotIn("description", call["columns"])
        self.assertFalse(call["count_exact"])
        self.assertIsNone(call["range"])

    def test_lifecycle_middle_page_timeout_retries_same_cursor_at_smaller_size(self):
        for index in range(1201):
            source_key = f"key-{index:05d}"
            self.client.tracked_jobs[source_key] = {
                **tracked_job_payload(self.job(), seen_at=self.clock()),
                "source_key": source_key,
            }
        self.client.tracked_query_failures = [
            None,
            ProviderFailure("canceling statement due to statement timeout", code="57014"),
            None,
        ]
        delays = []
        self.store._sleep = delays.append

        state = self.store.fetch_lifecycle_state()

        self.assertEqual(len(state["jobs"]), 1201)
        self.assertEqual(len(state["jobs"]), len(set(state["jobs"])))
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual(
            [(call["filters"], call["limit"]) for call in calls[:3]],
            [
                ([], 500),
                ([("source_key__gt", "key-00499")], 500),
                ([("source_key__gt", "key-00499")], 250),
            ],
        )
        self.assertTrue(all(call["limit"] == 250 for call in calls[2:]))
        self.assertEqual(delays, [0.5])

    def test_lifecycle_page_timeout_exhaustion_is_actionable(self):
        for index in range(501):
            source_key = f"key-{index:05d}"
            self.client.tracked_jobs[source_key] = {
                **tracked_job_payload(self.job(), seen_at=self.clock()),
                "source_key": source_key,
            }
        timeouts = [
            ProviderFailure(
                "canceling statement due to statement timeout", code="57014"
            )
            for _ in range(3)
        ]
        self.client.tracked_query_failures = [None, *timeouts]
        self.store._sleep = lambda _delay: None

        with self.assertRaises(SupabaseAccessError) as raised:
            self.store.fetch_lifecycle_state()

        error = raised.exception
        self.assertIn("lifecycle-state query", str(error))
        self.assertEqual(error.diagnostics["classification"], "timeout")
        self.assertEqual(error.diagnostics["provider_code"], "57014")
        self.assertEqual(error.diagnostics["attempt"], 3)
        self.assertEqual(error.diagnostics["payload_rows"], 125)
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual([call["limit"] for call in calls], [500, 500, 250, 125])
        self.assertEqual(
            [call["filters"] for call in calls[1:]],
            [[("source_key__gt", "key-00499")]] * 3,
        )

    def test_dashboard_data_paginates_all_active_ranked_rows(self):
        for row_count in (999, 1000, 1001, 3007):
            with self.subTest(row_count=row_count):
                client = FakeSupabaseClient()
                store = SupabaseJobStore(client, now_factory=self.clock)
                for index in range(row_count):
                    client.tracked_jobs[f"key-{index:04d}"] = {
                        "source_key": f"key-{index:04d}",
                        "company": "Example",
                        "title": f"Job {index}",
                        "status": "active",
                        "is_ranked_output": True,
                        "relevance_score": 100 - (index % 5),
                        "first_seen_at": f"2026-08-{(index % 28) + 1:02d}T00:00:00Z",
                        "raw_data": {},
                    }

                dashboard = store.fetch_dashboard_data()

                keys = [row["source_key"] for row in dashboard.active_jobs]
                self.assertEqual(len(keys), row_count)
                self.assertEqual(len(set(keys)), row_count)
                expected = list(client.tracked_jobs.values())
                expected.sort(key=lambda row: row["source_key"])
                expected.sort(key=lambda row: row["first_seen_at"], reverse=True)
                expected.sort(key=lambda row: row["relevance_score"], reverse=True)
                self.assertEqual(
                    keys,
                    [row["source_key"] for row in expected],
                )
                self.assertEqual(
                    client.range_calls["tracked_jobs"],
                    [
                        (offset, offset + 999)
                        for offset in range(0, row_count + 1, 1000)
                    ],
                )

    def test_timeout_is_retried_with_same_idempotency_key(self):
        client = SequencedSupabaseClient([TimeoutError("read timed out"), None])
        delays = []
        store = SupabaseJobStore(
            client,
            now_factory=self.clock,
            sync_max_attempts=3,
            sync_retry_delay_seconds=0.25,
            sleep=delays.append,
        )
        result = store.sync_student_jobs(
            [self.job()],
            successful_companies=("Example",),
            started_at=self.started_at,
            companies_checked=1,
            jobs_fetched=1,
        )
        self.assertEqual(result.inserted, 1)
        self.assertEqual(len(client.rpc_calls), 2)
        self.assertEqual(delays, [0.25])
        self.assertEqual(
            client.rpc_calls[0][1]["p_scan_id"],
            client.rpc_calls[1][1]["p_scan_id"],
        )

    def test_permanent_provider_error_is_not_retried(self):
        client = SequencedSupabaseClient(
            [ProviderFailure("constraint violation", code="23505")]
        )
        store = SupabaseJobStore(client, now_factory=self.clock, sync_max_attempts=3)
        with self.assertRaises(SupabaseAccessError) as raised:
            store.sync_student_jobs(
                [self.job()],
                successful_companies=("Example",),
                started_at=self.started_at,
                companies_checked=1,
                jobs_fetched=1,
            )
        self.assertEqual(len(client.rpc_calls), 1)
        self.assertEqual(raised.exception.diagnostics["classification"], "permanent")
        self.assertEqual(raised.exception.diagnostics["provider_code"], "23505")

    def test_response_loss_retry_does_not_increment_missing_twice(self):
        client = SequencedSupabaseClient([])
        seed_store = SupabaseJobStore(client, now_factory=self.clock)
        seed_store.sync_student_jobs(
            [self.job()], successful_companies=("Example",),
            started_at=self.started_at, companies_checked=1, jobs_fetched=1,
        )
        client.failures = [TimeoutError("response timed out"), None]
        client.commit_before_failure = True
        retry_store = SupabaseJobStore(
            client, now_factory=self.clock, sync_retry_delay_seconds=0, sleep=lambda _: None
        )
        retry_store.sync_student_jobs(
            [], successful_companies=("Example",),
            started_at=self.started_at + timedelta(minutes=1),
            companies_checked=1, jobs_fetched=0,
        )
        row = client.tracked_jobs[generate_source_key(self.job())]
        self.assertEqual(row["missing_successful_runs"], 1)

    def test_failed_source_transaction_cannot_mark_that_source_missing(self):
        client = SequencedSupabaseClient([])
        store = SupabaseJobStore(client, now_factory=self.clock, sync_max_attempts=1)
        other = self.job(
            job_id="other-1", company="Other", source="lever:other",
            url="https://jobs.other.example/other-1",
        )
        store.sync_student_jobs(
            [self.job(), other], successful_companies=("Example", "Other"),
            started_at=self.started_at, companies_checked=2, jobs_fetched=2,
        )
        other_before = copy.deepcopy(client.tracked_jobs[generate_source_key(other)])
        client.failures = [None, ProviderFailure("bad request", code="23514")]
        with self.assertRaises(SupabaseAccessError):
            store.sync_student_jobs(
                [self.job(relevance_score=80)],
                successful_companies=("Example", "Other"),
                started_at=self.started_at + timedelta(minutes=1),
                companies_checked=2, jobs_fetched=1,
            )
        self.assertEqual(client.tracked_jobs[generate_source_key(other)], other_before)

    def test_diagnostics_redact_configured_secret_and_authorization(self):
        secret = "eyJheader.payload.signature-secret"
        failure = ProviderFailure(
            f"Authorization: Bearer {secret}",
            code="520",
            details=f"apikey={secret}",
        )
        store = SupabaseJobStore(
            SequencedSupabaseClient([failure]),
            now_factory=self.clock,
            sync_max_attempts=1,
        )
        with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": secret}):
            with self.assertRaises(SupabaseAccessError) as raised:
                store.sync_student_jobs(
                    [self.job()], successful_companies=("Example",),
                    started_at=self.started_at, companies_checked=1, jobs_fetched=1,
                )
        rendered = str(raised.exception) + str(raised.exception.__cause__)
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertEqual(raised.exception.diagnostics["http_status"], 520)

    def test_custom_environment_service_key_is_always_redacted(self):
        secret = "custom-environment-service-role-secret"
        store = SupabaseJobStore.from_env(
            environ={
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": secret,
            },
            client_factory=lambda _url, _key: FailingClient(
                RuntimeError(f"request failed with apikey={secret}")
            ),
        )
        with self.assertRaises(SupabaseAccessError) as raised:
            store.sync_student_jobs(
                [self.job()], successful_companies=("Example",),
                started_at=self.started_at, companies_checked=1, jobs_fetched=1,
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, str(raised.exception.__cause__))

    def test_failed_scan_diagnostic_is_persisted_independently(self):
        self.store.record_failed_scan(
            started_at=self.started_at,
            error_message="sync timed out",
            stage="supabase_synchronization",
            companies_checked=533,
            jobs_fetched=12117,
            jobs_retained=832,
            normalized_jobs=11669,
            source_summary={"complete": 156, "partial": 377},
        )
        row = self.client.scan_runs[-1]
        self.assertFalse(row["success"])
        self.assertEqual(row["failure_stage"], "supabase_synchronization")
        self.assertEqual(row["diagnostics"]["normalized_jobs"], 11669)
        self.assertEqual(row["diagnostics"]["ranked_jobs"], 832)

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
