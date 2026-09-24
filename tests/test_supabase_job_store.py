import copy
import json
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
        self.sync_sessions = {}
        self.sync_chunks = {}
        self.sync_stage = {}
        self.rpc_calls = []
        self.range_calls = {}
        self.select_calls = []
        self.tracked_query_failures = []

    def rpc(self, name, params):
        self.rpc_calls.append((name, copy.deepcopy(params)))
        handlers = {
            "begin_student_job_source_sync": self._begin_sync,
            "stage_student_job_source_chunk": self._stage_chunk,
            "finalize_student_job_source_sync": self._finalize_sync,
            "abort_student_job_source_sync": self._abort_sync,
        }
        if name not in handlers:
            raise AssertionError(f"Unexpected RPC: {name}")
        return FakeRequest(lambda: handlers[name](params))

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

    def _begin_sync(self, params):
        sync_id = params["p_sync_id"]
        existing = self.sync_sessions.get(sync_id)
        if existing is not None:
            return {
                "status": existing["status"],
                "result": copy.deepcopy(existing.get("result")),
                "replayed": True,
            }
        ledger_key = (params["p_scan_id"], params["p_company"])
        prior = self.source_syncs.get(ledger_key)
        self.sync_sessions[sync_id] = {
            **copy.deepcopy(params),
            "status": "finalized" if prior is not None else "staging",
            "result": copy.deepcopy(prior),
        }
        self.sync_chunks[sync_id] = {}
        self.sync_stage[sync_id] = {}
        return {
            "status": self.sync_sessions[sync_id]["status"],
            "result": copy.deepcopy(prior),
            "replayed": False,
        }

    def _stage_chunk(self, params):
        sync_id = params["p_sync_id"]
        session = self.sync_sessions[sync_id]
        chunk_number = params["p_chunk_number"]
        existing = self.sync_chunks[sync_id].get(chunk_number)
        metadata = (
            params["p_chunk_rows"],
            params["p_chunk_bytes"],
            params["p_payload_hash"],
        )
        if existing is not None:
            if existing != metadata:
                raise RuntimeError("chunk retry mismatch")
            return {"status": "staged", "replayed": True}
        if session["status"] != "staging":
            return {"status": "finalized", "replayed": True}
        for row in params["p_jobs"]:
            if row["company"] != session["p_company"]:
                raise RuntimeError("company mismatch")
            if row["source_key"] in self.sync_stage[sync_id]:
                raise RuntimeError("duplicate staged source key")
            self.sync_stage[sync_id][row["source_key"]] = copy.deepcopy(row)
        self.sync_chunks[sync_id][chunk_number] = metadata
        return {"status": "staged", "replayed": False}

    def _finalize_sync(self, params):
        sync_id = params["p_sync_id"]
        session = self.sync_sessions[sync_id]
        if session["status"] == "finalized":
            return {**copy.deepcopy(session["result"]), "replayed": True}
        chunks = self.sync_chunks[sync_id]
        if (
            len(chunks) != session["p_expected_chunks"]
            or sum(item[0] for item in chunks.values()) != session["p_expected_rows"]
            or sum(item[1] for item in chunks.values()) != session["p_expected_bytes"]
            or len(self.sync_stage[sync_id]) != session["p_expected_rows"]
        ):
            raise RuntimeError("incomplete staging")
        result = self._sync(
            {
                "p_scan_id": session["p_scan_id"],
                "p_company": session["p_company"],
                "p_jobs": list(self.sync_stage[sync_id].values()),
                "p_reconcile_absence": session["p_reconcile_absence"],
                "p_close_after_missing_runs": session["p_close_after_missing_runs"],
                "p_seen_at": session["p_seen_at"],
            }
        )
        session["status"] = "finalized"
        session["result"] = copy.deepcopy(result)
        self.sync_chunks[sync_id] = {}
        self.sync_stage[sync_id] = {}
        return result

    def _abort_sync(self, params):
        sync_id = params["p_sync_id"]
        session = self.sync_sessions.get(sync_id)
        if session is None or session["status"] == "finalized":
            return False
        del self.sync_sessions[sync_id]
        self.sync_chunks.pop(sync_id, None)
        self.sync_stage.pop(sync_id, None)
        return True


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
        self.rpc_calls.append((name, copy.deepcopy(params)))

        def execute():
            failure = self.failures.pop(0) if self.failures else None
            if failure is not None and self.commit_before_failure:
                self._rpc_handler(name, params)
            if failure is not None:
                raise failure
            return self._rpc_handler(name, params)

        return FakeRequest(execute)

    def _rpc_handler(self, name, params):
        handlers = {
            "begin_student_job_source_sync": self._begin_sync,
            "stage_student_job_source_chunk": self._stage_chunk,
            "finalize_student_job_source_sync": self._finalize_sync,
            "abort_student_job_source_sync": self._abort_sync,
        }
        if name not in handlers:
            raise AssertionError(f"Unexpected RPC: {name}")
        return handlers[name](params)


class TargetedFailureClient(FakeSupabaseClient):
    def __init__(self, failures, *, commit_before_failure=False):
        super().__init__()
        self.failures = {key: list(values) for key, values in failures.items()}
        self.commit_before_failure = commit_before_failure

    def rpc(self, name, params):
        self.rpc_calls.append((name, copy.deepcopy(params)))
        key = (name, params.get("p_chunk_number"))

        def execute():
            failures = self.failures.get(key, [])
            failure = failures.pop(0) if failures else None
            handler = {
                "begin_student_job_source_sync": self._begin_sync,
                "stage_student_job_source_chunk": self._stage_chunk,
                "finalize_student_job_source_sync": self._finalize_sync,
                "abort_student_job_source_sync": self._abort_sync,
            }[name]
            if failure is not None and self.commit_before_failure:
                handler(params)
            if failure is not None:
                raise failure
            return handler(params)

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
        migration = (
            Path(__file__).resolve().parents[1]
            / "sql"
            / "migrations"
            / "20260924_staged_source_sync.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("create table if not exists public.job_sync_stage", migration)
        self.assertIn("create or replace function public.finalize_student_job_source_sync", migration)
        self.assertIn("for update skip locked", migration)
        self.assertNotIn("create temporary table", migration)

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
        sync_diagnostics = self.client.scan_runs[-1]["diagnostics"]["source_sync"]
        self.assertEqual(sync_diagnostics["source_count"], 1)
        self.assertEqual(sync_diagnostics["payload_rows"], 1)
        self.assertEqual(sync_diagnostics["chunk_row_limit"], 200)
        self.assertEqual(sync_diagnostics["chunk_byte_limit"], 512 * 1024)

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

    def test_partial_or_safety_limited_source_cannot_reconcile_absence(self):
        self.sync([self.job()])
        # The caller deliberately omits incomplete/safety-limited companies
        # from successful_companies, even when partial rows were collected.
        partial = self.job(job_id="partial-new", title="Partial result")
        self.sync([partial], successful_companies=())
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
        self.assertEqual(
            sum(name == "begin_student_job_source_sync" for name, _ in self.client.rpc_calls),
            533,
        )
        stage_calls = [
            params for name, params in self.client.rpc_calls
            if name == "stage_student_job_source_chunk"
        ]
        self.assertEqual(len(stage_calls), 533)
        self.assertEqual(
            sum(name == "finalize_student_job_source_sync" for name, _ in self.client.rpc_calls),
            533,
        )
        self.assertLessEqual(
            max(len(params["p_jobs"]) for params in stage_calls),
            22,
        )
        self.assertEqual(self.store.last_sync_diagnostics.payload_rows, 11669)

    def test_large_source_over_2000_jobs_and_legacy_14mb_payload_is_bounded(self):
        jobs = [
            self.job(
                job_id=f"abb-{index}",
                company="ABB",
                source="phenom:abb",
                url=f"https://careers.abb/jobs/{index}",
                description="D" * 7000,
                raw_metadata={"descriptionHtml": "M" * 1000},
            )
            for index in range(2101)
        ]
        legacy_bytes = len(
            json.dumps(jobs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        self.assertGreater(legacy_bytes, 14_000_000)

        result = self.store.sync_student_jobs(
            [], lifecycle_jobs=jobs, successful_companies=("ABB",),
            started_at=self.started_at, companies_checked=1, jobs_fetched=len(jobs),
        )

        self.assertEqual(result.inserted, 2101)
        stage_calls = [
            params for name, params in self.client.rpc_calls
            if name == "stage_student_job_source_chunk"
        ]
        self.assertGreater(len(stage_calls), 10)
        self.assertTrue(all(len(call["p_jobs"]) <= 200 for call in stage_calls))
        self.assertTrue(all(call["p_chunk_bytes"] <= 512 * 1024 for call in stage_calls))
        staged_keys = [row["source_key"] for call in stage_calls for row in call["p_jobs"]]
        self.assertEqual(len(staged_keys), 2101)
        self.assertEqual(len(set(staged_keys)), 2101)
        self.assertTrue(
            all("description" not in row["raw_data"] for call in stage_calls for row in call["p_jobs"])
        )

    def test_chunking_is_bounded_by_row_count(self):
        store = SupabaseJobStore(
            self.client, now_factory=self.clock, sync_chunk_rows=3, sync_chunk_bytes=100_000
        )
        jobs = [self.job(job_id=f"row-{index}") for index in range(8)]
        store.sync_student_jobs(
            jobs, successful_companies=("Example",), started_at=self.started_at,
            companies_checked=1, jobs_fetched=8,
        )
        stage_calls = [
            params for name, params in self.client.rpc_calls
            if name == "stage_student_job_source_chunk"
        ]
        self.assertEqual([len(call["p_jobs"]) for call in stage_calls], [3, 3, 2])

    def test_chunking_is_bounded_by_serialized_byte_size(self):
        store = SupabaseJobStore(
            self.client, now_factory=self.clock, sync_chunk_rows=100, sync_chunk_bytes=3000
        )
        jobs = [
            self.job(job_id=f"byte-{index}", title=f"Intern {index} " + "T" * 900)
            for index in range(7)
        ]
        store.sync_student_jobs(
            jobs, successful_companies=("Example",), started_at=self.started_at,
            companies_checked=1, jobs_fetched=7,
        )
        stage_calls = [
            params for name, params in self.client.rpc_calls
            if name == "stage_student_job_source_chunk"
        ]
        self.assertGreater(len(stage_calls), 1)
        self.assertTrue(all(call["p_chunk_bytes"] <= 3000 for call in stage_calls))
        self.assertTrue(all(len(call["p_jobs"]) < 100 for call in stage_calls))

    def test_payload_audit_removes_unconsumed_bulk_but_keeps_child_brands(self):
        job = self.job(
            description="description" * 5000,
            offices=["Calgary", "Toronto"],
            raw_metadata={"opaque": "metadata" * 5000, "child_brands": ["ABB Robotics"]},
            child_brands=["ABB Robotics"],
        )
        payload = tracked_job_payload(job, seen_at=self.clock())
        old_shape = {**payload, "raw_data": job}
        old_bytes = len(json.dumps(old_shape, separators=(",", ":")).encode("utf-8"))
        new_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        self.assertGreater(old_bytes, new_bytes * 50)
        self.assertEqual(payload["raw_data"], {"child_brands": ["ABB Robotics"]})
        self.assertNotIn("description", payload["raw_data"])
        self.assertNotIn("raw_metadata", payload["raw_data"])

    def test_middle_chunk_timeout_retries_same_chunk_and_sync_id(self):
        client = TargetedFailureClient(
            {("stage_student_job_source_chunk", 1): [TimeoutError("timed out"), None]}
        )
        delays = []
        store = SupabaseJobStore(
            client, now_factory=self.clock, sync_chunk_rows=2,
            sync_retry_delay_seconds=0.1, sleep=delays.append,
        )
        jobs = [self.job(job_id=f"retry-{index}") for index in range(5)]
        result = store.sync_student_jobs(
            jobs, successful_companies=("Example",), started_at=self.started_at,
            companies_checked=1, jobs_fetched=5,
        )
        calls = [
            params for name, params in client.rpc_calls
            if name == "stage_student_job_source_chunk" and params["p_chunk_number"] == 1
        ]
        self.assertEqual(result.inserted, 5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["p_sync_id"], calls[1]["p_sync_id"])
        self.assertEqual(calls[0]["p_payload_hash"], calls[1]["p_payload_hash"])
        self.assertEqual(delays, [0.1])

    def test_response_lost_after_chunk_write_does_not_duplicate_stage_rows(self):
        client = TargetedFailureClient(
            {("stage_student_job_source_chunk", 1): [TimeoutError("response timed out"), None]},
            commit_before_failure=True,
        )
        store = SupabaseJobStore(
            client, now_factory=self.clock, sync_chunk_rows=2,
            sync_retry_delay_seconds=0, sleep=lambda _delay: None,
        )
        jobs = [self.job(job_id=f"lost-{index}") for index in range(5)]
        result = store.sync_student_jobs(
            jobs, successful_companies=("Example",), started_at=self.started_at,
            companies_checked=1, jobs_fetched=5,
        )
        self.assertEqual(result.inserted, 5)
        self.assertEqual(len(client.tracked_jobs), 5)
        chunk_calls = [
            params for name, params in client.rpc_calls
            if name == "stage_student_job_source_chunk" and params["p_chunk_number"] == 1
        ]
        self.assertEqual(len(chunk_calls), 2)

    def test_staging_timeout_exhaustion_never_finalizes_and_is_actionable(self):
        failures = [TimeoutError("timed out") for _ in range(3)]
        client = TargetedFailureClient(
            {("stage_student_job_source_chunk", 1): failures}
        )
        store = SupabaseJobStore(
            client, now_factory=self.clock, sync_chunk_rows=2,
            sync_max_attempts=3, sync_retry_delay_seconds=0, sleep=lambda _delay: None,
        )
        jobs = [self.job(job_id=f"fail-{index}") for index in range(5)]
        with self.assertRaises(SupabaseAccessError) as raised:
            store.sync_student_jobs(
                jobs, successful_companies=("Example",), started_at=self.started_at,
                companies_checked=1, jobs_fetched=5,
            )
        self.assertFalse(any(
            name == "finalize_student_job_source_sync" for name, _ in client.rpc_calls
        ))
        self.assertEqual(client.sync_sessions, {})
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["phase"], "staging")
        self.assertEqual(diagnostics["chunk_number"], 1)
        self.assertEqual(diagnostics["chunk_rows"], 2)
        self.assertEqual(diagnostics["total_rows"], 5)
        self.assertIn("sync_id", diagnostics)

    def test_repeated_finalize_is_idempotent(self):
        self.sync([self.job()])
        begin = next(
            params for name, params in self.client.rpc_calls
            if name == "begin_student_job_source_sync"
        )
        before = copy.deepcopy(self.tracked())
        replay = self.client.rpc(
            "finalize_student_job_source_sync", {"p_sync_id": begin["p_sync_id"]}
        ).execute().data
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.tracked(), before)

    def test_successful_finalize_cleans_staged_rows_but_keeps_replay_result(self):
        self.sync([self.job()])
        begin = next(
            params for name, params in self.client.rpc_calls
            if name == "begin_student_job_source_sync"
        )
        sync_id = begin["p_sync_id"]
        self.assertEqual(self.client.sync_stage[sync_id], {})
        self.assertEqual(self.client.sync_chunks[sync_id], {})
        self.assertEqual(self.client.sync_sessions[sync_id]["status"], "finalized")
        self.assertIsNotNone(self.client.sync_sessions[sync_id]["result"])

    def test_stale_cleanup_sql_is_bounded_and_skips_locked_in_progress_sessions(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "sql"
            / "migrations"
            / "20260924_staged_source_sync.sql"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn("where updated_at < p_stale_before", migration)
        self.assertIn("for update skip locked", migration)
        self.assertIn("limit p_limit", migration)
        self.assertIn("references public.job_sync_sessions(sync_id) on delete cascade", migration)
        self.assertIn("delete from public.job_sync_stage where sync_id = p_sync_id", migration)

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
        self.assertEqual(self.client.range_calls.get("tracked_jobs", []), [])

    def test_lifecycle_keyset_treats_server_cursor_as_opaque(self):
        first_page = FakeResponse(
            [{"source_key": "aardvark"}, {"source_key": "alpha"}]
        )
        server_collated_continuation = FakeResponse([{"source_key": "Beta"}])
        self.assertLess("Beta", "alpha")

        with patch.object(
            self.store,
            "_fetch_lifecycle_page",
            side_effect=[
                (first_page, 2),
                (server_collated_continuation, 2),
            ],
        ) as fetch_page:
            state = self.store.fetch_lifecycle_state()

        self.assertEqual(list(state["jobs"]), ["aardvark", "alpha", "Beta"])
        self.assertEqual(fetch_page.call_args_list[1].kwargs["cursor"], "alpha")

    def test_lifecycle_keyset_rejects_an_equality_stall(self):
        first_page = FakeResponse(
            [{"source_key": "alpha"}, {"source_key": "cursor"}]
        )
        repeated_cursor = FakeResponse(
            [{"source_key": "other"}, {"source_key": "cursor"}]
        )

        with patch.object(
            self.store,
            "_fetch_lifecycle_page",
            side_effect=[(first_page, 2), (repeated_cursor, 2)],
        ) as fetch_page:
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                self.store.fetch_lifecycle_state()

        self.assertEqual(fetch_page.call_args_list[1].kwargs["cursor"], "cursor")

    def test_lifecycle_keyset_rejects_empty_and_duplicate_keys(self):
        cases = (
            ([{"source_key": ""}], "empty source_key"),
            (
                [{"source_key": "duplicate"}, {"source_key": "duplicate"}],
                "duplicate rows",
            ),
        )
        for rows, message in cases:
            with self.subTest(message=message), patch.object(
                self.store,
                "_fetch_lifecycle_page",
                return_value=(FakeResponse(rows), 500),
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.store.fetch_lifecycle_state()

    def test_lifecycle_keyset_rejects_duplicates_across_pages(self):
        first_page = FakeResponse(
            [{"source_key": "alpha"}, {"source_key": "zulu"}]
        )
        duplicate_continuation = FakeResponse([{"source_key": "alpha"}])

        with patch.object(
            self.store,
            "_fetch_lifecycle_page",
            side_effect=[(first_page, 2), (duplicate_continuation, 2)],
        ):
            with self.assertRaisesRegex(RuntimeError, "duplicate rows"):
                self.store.fetch_lifecycle_state()

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
        self.assertEqual(error.diagnostics["page"], 2)
        self.assertEqual(error.diagnostics["attempt"], 3)
        self.assertEqual(error.diagnostics["payload_rows"], 125)
        self.assertTrue(error.diagnostics["cursor"].startswith("sha256:"))
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual([call["limit"] for call in calls], [500, 500, 250, 125])
        self.assertEqual(
            [call["filters"] for call in calls[1:]],
            [[("source_key__gt", "key-00499")]] * 3,
        )

    def test_dashboard_data_keyset_paginates_all_active_ranked_rows(self):
        for row_count in (7, 1001, 3007):
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
                self.assertEqual(keys, sorted(client.tracked_jobs))
                calls = [
                    call for call in client.select_calls
                    if call["table"] == "tracked_jobs"
                ]
                self.assertEqual(
                    calls[0]["filters"][:2],
                    [
                        ("status", "active"),
                        ("is_ranked_output", True),
                    ],
                )
                self.assertTrue(
                    all(call["orderings"] == [("source_key", False)] for call in calls)
                )
                self.assertTrue(all(call["limit"] == 500 for call in calls))
                self.assertTrue(all(call["range"] is None for call in calls))
                self.assertEqual(client.range_calls.get("tracked_jobs", []), [])
                cursors = [call["filters"][-1] for call in calls[1:]]
                self.assertTrue(
                    all(field == "source_key__gt" for field, _value in cursors)
                )
                self.assertEqual(
                    [value for _field, value in cursors],
                    sorted(value for _field, value in cursors),
                )

    def test_dashboard_data_excludes_inactive_and_unranked_rows(self):
        base = {
            "company": "Example",
            "title": "Intern",
            "relevance_score": 80,
            "first_seen_at": "2026-08-01T00:00:00Z",
        }
        self.client.tracked_jobs = {
            "active": {
                **base,
                "source_key": "active",
                "status": "active",
                "lifecycle_status": "active",
                "is_ranked_output": True,
            },
            "removed": {
                **base,
                "source_key": "removed",
                "status": "closed",
                "lifecycle_status": "removed",
                "removed_at": "2026-08-02T00:00:00Z",
                "is_ranked_output": True,
            },
            "unranked": {
                **base,
                "source_key": "unranked",
                "status": "active",
                "lifecycle_status": "active",
                "is_ranked_output": False,
            },
        }

        dashboard = self.store.fetch_dashboard_data()

        self.assertEqual(
            [row["source_key"] for row in dashboard.active_jobs],
            ["active"],
        )

    def test_dashboard_data_requests_only_sheet_fields_without_count_or_raw_data(self):
        self.client.tracked_jobs["key-00000"] = {
            "source_key": "key-00000",
            "company": "Example",
            "title": "Intern",
            "status": "active",
            "is_ranked_output": True,
            "relevance_score": 90,
            "first_seen_at": "2026-08-01T00:00:00Z",
            "raw_data": {"description": "x" * 100_000},
        }

        self.store.fetch_dashboard_data()

        call = next(
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        )
        self.assertEqual(
            call["columns"],
            "source_key,company,title,location,apply_url,relevance_score,"
            "positive_signals,first_seen_at,status,freshness_band,urgency_band",
        )
        self.assertNotIn("raw_data", call["columns"])
        self.assertNotIn("description", call["columns"])
        self.assertFalse(call["count_exact"])
        self.assertIsNone(call["range"])

    def test_dashboard_keyset_rejects_duplicate_keys(self):
        duplicate_page = FakeResponse(
            [
                {"source_key": "duplicate"},
                {"source_key": "duplicate"},
            ]
        )
        with patch.object(
            self.store,
            "_fetch_active_jobs_page",
            return_value=(duplicate_page, 500),
        ):
            with self.assertRaisesRegex(RuntimeError, "duplicate rows"):
                self.store.fetch_dashboard_data()

    def test_dashboard_keyset_rejects_an_empty_key(self):
        with patch.object(
            self.store,
            "_fetch_active_jobs_page",
            return_value=(FakeResponse([{"source_key": ""}]), 500),
        ):
            with self.assertRaisesRegex(RuntimeError, "empty source_key"):
                self.store.fetch_dashboard_data()

    def test_dashboard_keyset_rejects_duplicates_across_pages(self):
        first_page = FakeResponse(
            [{"source_key": "alpha"}, {"source_key": "zulu"}]
        )
        duplicate_continuation = FakeResponse([{"source_key": "alpha"}])

        with patch.object(
            self.store,
            "_fetch_active_jobs_page",
            side_effect=[(first_page, 2), (duplicate_continuation, 2)],
        ):
            with self.assertRaisesRegex(RuntimeError, "duplicate rows"):
                self.store.fetch_dashboard_data()

    def test_dashboard_keyset_rejects_a_nonprogressing_cursor(self):
        first_page = FakeResponse(
            [{"source_key": f"key-{index:05d}"} for index in range(500)]
        )
        repeated_page = FakeResponse(
            [{"source_key": f"other-{index:05d}"} for index in range(499)]
            + [{"source_key": "key-00499"}]
        )
        with patch.object(
            self.store,
            "_fetch_active_jobs_page",
            side_effect=[(first_page, 500), (repeated_page, 500)],
        ) as fetch_page:
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                self.store.fetch_dashboard_data()

        self.assertEqual(fetch_page.call_args_list[1].kwargs["cursor"], "key-00499")

    def test_dashboard_keyset_treats_server_cursor_as_opaque(self):
        first_page = FakeResponse(
            [{"source_key": "aardvark"}, {"source_key": "alpha"}]
        )
        server_collated_continuation = FakeResponse([{"source_key": "Beta"}])
        self.assertLess("Beta", "alpha")

        with patch.object(
            self.store,
            "_fetch_active_jobs_page",
            side_effect=[
                (first_page, 2),
                (server_collated_continuation, 2),
            ],
        ) as fetch_page:
            dashboard = self.store.fetch_dashboard_data()

        self.assertEqual(
            [row["source_key"] for row in dashboard.active_jobs],
            ["aardvark", "alpha", "Beta"],
        )
        self.assertEqual(fetch_page.call_args_list[1].kwargs["cursor"], "alpha")

    def test_dashboard_middle_page_timeout_retries_same_cursor_at_smaller_size(self):
        for index in range(1201):
            source_key = f"key-{index:05d}"
            self.client.tracked_jobs[source_key] = {
                "source_key": source_key,
                "company": "Example",
                "title": f"Job {index}",
                "status": "active",
                "is_ranked_output": True,
                "relevance_score": 90,
                "first_seen_at": "2026-08-01T00:00:00Z",
            }
        self.client.tracked_query_failures = [
            None,
            ProviderFailure("canceling statement due to statement timeout", code="57014"),
            None,
        ]
        delays = []
        self.store._sleep = delays.append

        dashboard = self.store.fetch_dashboard_data()

        keys = [row["source_key"] for row in dashboard.active_jobs]
        self.assertEqual(keys, sorted(self.client.tracked_jobs))
        self.assertEqual(len(keys), len(set(keys)))
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual(
            [(call["filters"][-1:], call["limit"]) for call in calls[:3]],
            [
                ([("is_ranked_output", True)], 500),
                ([("source_key__gt", "key-00499")], 500),
                ([("source_key__gt", "key-00499")], 250),
            ],
        )
        self.assertTrue(all(call["limit"] == 250 for call in calls[2:]))
        self.assertEqual(delays, [0.5])

    def test_dashboard_page_timeout_exhaustion_is_actionable_and_secret_safe(self):
        for index in range(501):
            source_key = f"key-{index:05d}"
            self.client.tracked_jobs[source_key] = {
                "source_key": source_key,
                "company": "Example",
                "title": "Intern",
                "status": "active",
                "is_ranked_output": True,
            }
        self.client.tracked_query_failures = [
            None,
            *[
                ProviderFailure(
                    "canceling statement due to statement timeout secret-token",
                    code="57014",
                )
                for _ in range(3)
            ],
        ]
        self.store._redaction_secrets = ("secret-token",)
        self.store._sleep = lambda _delay: None

        with self.assertRaises(SupabaseAccessError) as raised:
            self.store.fetch_dashboard_data()

        error = raised.exception
        self.assertIn("active-jobs", str(error))
        self.assertNotIn("secret-token", str(error))
        self.assertEqual(error.diagnostics["operation"], "active-jobs")
        self.assertEqual(error.diagnostics["classification"], "timeout")
        self.assertEqual(error.diagnostics["provider_code"], "57014")
        self.assertEqual(error.diagnostics["page"], 2)
        self.assertEqual(error.diagnostics["attempt"], 3)
        self.assertEqual(error.diagnostics["requested_page_size"], 125)
        self.assertEqual(error.diagnostics["payload_rows"], 125)
        self.assertTrue(error.diagnostics["cursor"].startswith("sha256:"))
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual([call["limit"] for call in calls], [500, 500, 250, 125])
        self.assertEqual(
            [call["filters"][-1] for call in calls[1:]],
            [("source_key__gt", "key-00499")] * 3,
        )

    def test_dashboard_other_transient_error_retries_without_reducing_page(self):
        self.client.tracked_jobs["key-00000"] = {
            "source_key": "key-00000",
            "company": "Example",
            "title": "Intern",
            "status": "active",
            "is_ranked_output": True,
        }
        self.client.tracked_query_failures = [
            ProviderFailure("service unavailable", code="503"),
            None,
        ]
        delays = []
        self.store._sleep = delays.append

        dashboard = self.store.fetch_dashboard_data()

        self.assertEqual(len(dashboard.active_jobs), 1)
        calls = [
            call for call in self.client.select_calls
            if call["table"] == "tracked_jobs"
        ]
        self.assertEqual([call["limit"] for call in calls], [500, 500])
        self.assertEqual([call["filters"] for call in calls], [calls[0]["filters"]] * 2)
        self.assertEqual(delays, [0.5])

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
        begin_calls = [
            params for name, params in client.rpc_calls
            if name == "begin_student_job_source_sync"
        ]
        self.assertEqual(len(begin_calls), 2)
        self.assertEqual(delays, [0.25])
        self.assertEqual(
            begin_calls[0]["p_sync_id"],
            begin_calls[1]["p_sync_id"],
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
        self.assertEqual(client.rpc_calls[0][0], "begin_student_job_source_sync")
        self.assertEqual(client.rpc_calls[-1][0], "abort_student_job_source_sync")
        self.assertEqual(raised.exception.diagnostics["classification"], "permanent")
        self.assertEqual(raised.exception.diagnostics["provider_code"], "23505")

    def test_response_loss_retry_does_not_increment_missing_twice(self):
        client = TargetedFailureClient({})
        seed_store = SupabaseJobStore(client, now_factory=self.clock)
        seed_store.sync_student_jobs(
            [self.job()], successful_companies=("Example",),
            started_at=self.started_at, companies_checked=1, jobs_fetched=1,
        )
        client.failures[("finalize_student_job_source_sync", None)] = [
            TimeoutError("response timed out"),
            None,
        ]
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
        finalize_calls = [
            params for name, params in client.rpc_calls
            if name == "finalize_student_job_source_sync"
        ]
        self.assertEqual(len(finalize_calls), 3)  # seed plus two retry-run calls
        self.assertEqual(finalize_calls[-2]["p_sync_id"], finalize_calls[-1]["p_sync_id"])

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
        self.assertEqual(raised.exception.diagnostics["phase"], "staging")
        self.assertEqual(raised.exception.diagnostics["source"], "Example")
        self.assertIn("sync_id", raised.exception.diagnostics)

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
