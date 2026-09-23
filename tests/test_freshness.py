from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.scraper.company_careers.freshness import (
    ACTIVE,
    POSSIBLY_MISSING,
    REMOVED,
    FreshnessConfigurationError,
    FreshnessRuntime,
    classify_freshness,
    classify_urgency,
    empty_freshness_state,
    load_freshness_config,
    parse_reference_time,
    parse_source_date,
    reconcile_lifecycle,
    stable_job_key,
)
from src.scraper.company_careers.models import (
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
)
from src.scraper.company_careers.student_roles import (
    rank_student_jobs,
    write_student_ranked_jobs_artifacts,
)


AS_OF = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def job(
    job_id: str,
    *,
    company: str = "Example",
    title: str = "Software Engineering Intern",
    posted: str | None = None,
    closes: str | None = None,
    metadata: dict | None = None,
) -> JobRecord:
    raw_metadata = dict(metadata or {})
    if posted is not None:
        raw_metadata["source_posted_at"] = posted
    if closes is not None:
        raw_metadata["application_deadline"] = closes
    return JobRecord(
        job_id=job_id,
        title=title,
        company=company,
        location="Calgary, Canada",
        url=f"https://jobs.example.test/{company}/{job_id}",
        updated_at="",
        source=f"greenhouse:{company.casefold()}",
        raw_metadata=raw_metadata,
    )


class FreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_freshness_config()

    def reconcile(
        self, jobs, state=None, *, complete_companies=("Example",), as_of=AS_OF,
        first_seen_basis="observed_at_scan",
    ):
        return reconcile_lifecycle(
            jobs,
            state or empty_freshness_state(),
            complete_companies=complete_companies,
            as_of=as_of,
            config=self.config,
            first_seen_basis=first_seen_basis,
        )

    def annotation(self, current: JobRecord, **kwargs):
        result = self.reconcile([current], **kwargs)
        return result.annotations_by_key[stable_job_key(current)]

    def test_first_observation_and_stable_identity(self):
        current = job("123")
        result = self.reconcile([current])
        entry = result.state["jobs"][stable_job_key(current)]
        self.assertEqual(stable_job_key(current), "id:greenhouse:example:123")
        self.assertEqual(entry["first_seen_at"], "2026-08-31T12:00:00Z")
        self.assertEqual(entry["last_seen_at"], "2026-08-31T12:00:00Z")
        self.assertEqual(entry["lifecycle_status"], ACTIVE)

    def test_repeated_observation_preserves_first_seen_and_updates_last_seen(self):
        first = self.reconcile([job("123")])
        second = self.reconcile(
            [job("123")], first.state, as_of=AS_OF + timedelta(days=2)
        )
        entry = second.state["jobs"][stable_job_key(job("123"))]
        self.assertEqual(entry["first_seen_at"], "2026-08-31T12:00:00Z")
        self.assertEqual(entry["last_seen_at"], "2026-09-02T12:00:00Z")

    def test_source_posted_precedence_and_first_seen_fallback(self):
        posted = self.annotation(job("posted", posted="2026-08-21T12:00:00Z"))
        self.assertEqual(posted["age_days"], 10)
        self.assertEqual(posted["freshness_age_source"], "source_posted_at")

        fallback = self.annotation(job("fallback"))
        self.assertEqual(fallback["age_days"], 0)
        self.assertEqual(fallback["first_seen_age_hours"], 0)
        self.assertEqual(fallback["freshness_age_source"], "first_seen_at")

    def test_each_freshness_boundary_is_inclusive(self):
        expected = {
            24: "new",
            24.001: "fresh",
            72: "fresh",
            72.001: "normal",
            240: "normal",
            240.001: "aging",
            504: "aging",
            504.001: "stale",
        }
        for hours, band in expected.items():
            with self.subTest(hours=hours):
                self.assertEqual(classify_freshness(hours, self.config), band)

    def test_deadline_today_tomorrow_two_days_seven_days_and_passed(self):
        expected = {
            "2026-08-30": "closed",
            "2026-08-31": "apply_now",
            "2026-09-01": "apply_now",
            "2026-09-02": "apply_now",
            "2026-09-07": "high",
        }
        for closes, band in expected.items():
            with self.subTest(closes=closes):
                annotation = self.annotation(job(closes, closes=closes))
                self.assertEqual(annotation["urgency_band"], band)

    def test_stale_posting_with_imminent_deadline_is_apply_now(self):
        annotation = self.annotation(
            job("stale", posted="2026-07-01", closes="2026-09-01")
        )
        self.assertEqual(annotation["freshness_band"], "stale")
        self.assertEqual(annotation["urgency_band"], "apply_now")
        self.assertTrue(annotation["urgency_reasons"][0].startswith("deadline_in_"))

    def test_new_posting_without_deadline_is_high(self):
        annotation = self.annotation(job("new"))
        self.assertEqual(annotation["freshness_band"], "new")
        self.assertEqual(annotation["urgency_band"], "high")
        self.assertEqual(annotation["urgency_reasons"], ["first_seen_0_hours_ago"])

    def test_timezone_boundary_and_naive_reference_rejected(self):
        parsed = parse_reference_time("2026-08-31T05:00:00-07:00")
        self.assertEqual(parsed, AS_OF)
        annotation = self.annotation(
            job("tz", posted="2026-08-30T05:00:00-07:00")
        )
        self.assertEqual(annotation["age_days"], 1)
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_reference_time("2026-08-31T12:00:00")

    def test_source_date_allowlist_and_malformed_dates_are_reported(self):
        self.assertEqual(parse_source_date("Posted 3 days ago", as_of=AS_OF), "2026-08-28")
        self.assertEqual(parse_source_date("August 28, 2026", as_of=AS_OF), "2026-08-28")
        self.assertIsNone(parse_source_date("late summer-ish", as_of=AS_OF))
        current = job("bad", posted="late summer-ish", closes="whenever")
        result = self.reconcile([current])
        self.assertEqual(len(result.date_parse_failures), 2)
        annotation = result.annotations_by_key[stable_job_key(current)]
        self.assertEqual(annotation["freshness_age_source"], "first_seen_at")
        self.assertEqual(
            annotation["date_parse_errors"],
            ["source_closes_at:unparseable", "source_posted_at:unparseable"],
        )
        self.assertEqual(
            result.date_diagnostic_counts,
            {
                "source_posted_at_missing": 0,
                "source_posted_at_malformed": 1,
                "source_posted_at_approximate": 0,
                "source_closes_at_missing": 0,
                "source_closes_at_malformed": 1,
                "source_closes_at_approximate": 0,
                "lifecycle_failures": 0,
            },
        )

    def test_workday_thirty_plus_days_is_a_conservative_age_lower_bound(self):
        current = job("bounded", posted="Posted 30+ Days Ago")
        result = self.reconcile([current])
        annotation = result.annotations_by_key[stable_job_key(current)]

        self.assertEqual(
            parse_source_date("Posted 30+ Days Ago", as_of=AS_OF),
            "2026-08-01",
        )
        self.assertEqual(annotation["source_posted_at"], "2026-08-01")
        self.assertEqual(annotation["age_days"], 30.5)
        self.assertEqual(annotation["freshness_band"], "stale")
        self.assertEqual(result.date_parse_failures, ())
        self.assertEqual(
            result.date_diagnostic_counts["source_posted_at_approximate"],
            1,
        )
        self.assertEqual(
            result.date_diagnostic_counts["source_closes_at_missing"],
            1,
        )

    def test_missing_and_malformed_dates_never_remove_a_present_job(self):
        jobs = [
            job("missing"),
            job("malformed", posted="late summer-ish", closes="whenever"),
        ]
        result = self.reconcile(jobs)

        self.assertTrue(
            all(
                annotation["lifecycle_status"] == ACTIVE
                for annotation in result.annotations_by_key.values()
            )
        )
        self.assertEqual(result.date_diagnostic_counts["source_posted_at_missing"], 1)
        self.assertEqual(result.date_diagnostic_counts["source_posted_at_malformed"], 1)
        self.assertEqual(result.date_diagnostic_counts["source_closes_at_missing"], 1)
        self.assertEqual(result.date_diagnostic_counts["source_closes_at_malformed"], 1)

    def test_failed_or_partial_scan_does_not_advance_missing_evidence(self):
        initial = self.reconcile([job("123")])
        unchanged = self.reconcile(
            [], initial.state, complete_companies=(), as_of=AS_OF + timedelta(days=1)
        )
        entry = unchanged.state["jobs"][stable_job_key(job("123"))]
        self.assertEqual(entry["lifecycle_status"], ACTIVE)
        self.assertEqual(entry["missing_scan_count"], 0)

    def test_two_successful_complete_absences_remove_job(self):
        initial = self.reconcile([job("123")])
        first_miss = self.reconcile([], initial.state, as_of=AS_OF + timedelta(days=1))
        key = stable_job_key(job("123"))
        self.assertEqual(first_miss.state["jobs"][key]["lifecycle_status"], POSSIBLY_MISSING)
        second_miss = self.reconcile([], first_miss.state, as_of=AS_OF + timedelta(days=2))
        self.assertEqual(second_miss.state["jobs"][key]["lifecycle_status"], REMOVED)
        self.assertEqual(
            second_miss.state["jobs"][key]["removed_at"], "2026-09-02T12:00:00Z"
        )

    def test_reappearance_preserves_history_and_resets_missing_count(self):
        initial = self.reconcile([job("123")])
        missed = self.reconcile([], initial.state, as_of=AS_OF + timedelta(days=1))
        removed = self.reconcile([], missed.state, as_of=AS_OF + timedelta(days=2))
        reappeared = self.reconcile(
            [job("123")], removed.state, as_of=AS_OF + timedelta(days=3)
        )
        entry = reappeared.state["jobs"][stable_job_key(job("123"))]
        self.assertEqual(entry["first_seen_at"], "2026-08-31T12:00:00Z")
        self.assertEqual(entry["lifecycle_status"], ACTIVE)
        self.assertEqual(entry["missing_scan_count"], 0)
        self.assertEqual(entry["reappearance_count"], 1)
        self.assertEqual(reappeared.transitions[0]["transition"], "reappeared")

    def test_explicit_source_closure_removes_immediately(self):
        annotation = self.annotation(job("closed", metadata={"status": "closed"}))
        self.assertFalse(annotation["is_active"])
        self.assertEqual(annotation["urgency_band"], "closed")
        self.assertEqual(annotation["urgency_reasons"], ["confirmed_removed"])

    def test_runtime_requires_success_and_completeness_for_absence(self):
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")
        initial = self.reconcile([job("123")]).state
        reports = [
            CompanyCareersReport(results=[CompanyScanResult(
                target=target, status=CoverageStatus.FAILED_TECHNICAL,
                scan_complete=False,
            )]),
            CompanyCareersReport(results=[CompanyScanResult(
                target=target, status=CoverageStatus.FETCHED_NO_MATCHING_ROLES,
                scan_complete=False,
            )]),
            CompanyCareersReport(results=[CompanyScanResult(
                target=target, status=CoverageStatus.FETCHED_NO_MATCHING_ROLES,
                scan_complete=True,
            )]),
        ]
        state = initial
        for index, report in enumerate(reports, start=1):
            runtime = FreshnessRuntime(
                config=self.config, previous_state=state,
                as_of=AS_OF + timedelta(days=index),
            )
            state = runtime.process_report(report).state
        entry = state["jobs"][stable_job_key(job("123"))]
        self.assertEqual(entry["lifecycle_status"], POSSIBLY_MISSING)
        self.assertEqual(entry["missing_scan_count"], 1)

    def test_freshness_annotations_do_not_change_ranking_semantics_or_order(self):
        jobs = [
            job("a", title="Software Engineering Intern", posted="2026-01-01"),
            job("b", title="Marketing Intern", posted="2026-08-31"),
        ]
        lifecycle = self.reconcile(jobs)
        before = rank_student_jobs(jobs, now=AS_OF)
        after = rank_student_jobs(
            jobs, now=AS_OF, freshness_by_key=lifecycle.annotations_by_key
        )
        self.assertEqual(
            [(item["job_id"], item["score"], item["relevance_score"]) for item in before],
            [(item["job_id"], item["score"], item["relevance_score"]) for item in after],
        )
        self.assertIn("urgency_band", after[0])

    def test_ranked_json_and_csv_are_backward_compatible_and_explainable(self):
        current = job("artifact", posted="2026-08-29", closes="2026-09-02")
        lifecycle = self.reconcile([current])
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "ranked.json"
            csv_path = Path(directory) / "ranked.csv"
            write_student_ranked_jobs_artifacts(
                [current], json_path=str(json_path), csv_path=str(csv_path),
                now=AS_OF, freshness_by_key=lifecycle.annotations_by_key,
            )
            row = json.loads(json_path.read_text(encoding="utf-8"))[0]
            for field in (
                "score", "relevance_score", "first_seen_at", "last_seen_at",
                "source_posted_at", "source_closes_at", "age_days",
                "first_seen_age_hours", "freshness_band", "urgency_band",
                "urgency_reasons", "lifecycle_status", "is_active",
            ):
                self.assertIn(field, row)
            with csv_path.open(encoding="utf-8", newline="") as file:
                csv_row = next(csv.DictReader(file))
            self.assertEqual(csv_row["freshness_band"], "fresh")
            self.assertEqual(csv_row["urgency_band"], "apply_now")

    def test_backfill_basis_is_explicit_and_replay_is_deterministic(self):
        kwargs = dict(
            current_jobs=[job("legacy")], previous_state=empty_freshness_state(),
            complete_companies=("Example",), as_of=AS_OF, config=self.config,
            first_seen_basis="backfilled_at_reference_time",
        )
        first = reconcile_lifecycle(**kwargs)
        second = reconcile_lifecycle(**kwargs)
        self.assertEqual(first, second)
        entry = first.state["jobs"][stable_job_key(job("legacy"))]
        self.assertEqual(entry["first_seen_basis"], "backfilled_at_reference_time")

    def test_configuration_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "freshness.yaml"
            path.write_text("freshness:\n  unexpected: true\n", encoding="utf-8")
            with self.assertRaises(FreshnessConfigurationError):
                load_freshness_config(str(path))

    def test_urgency_rule_boundaries_directly(self):
        cases = [
            (-0.1, "closed"), (0, "apply_now"), (48, "apply_now"),
            (48.1, "high"), (168, "high"),
        ]
        for deadline_hours, expected in cases:
            with self.subTest(deadline_hours=deadline_hours):
                band, _ = classify_urgency(
                    lifecycle_status=ACTIVE,
                    deadline_hours_remaining=deadline_hours,
                    first_seen_age_hours=100,
                    age_days=4,
                    freshness_band="normal",
                    config=self.config,
                )
                self.assertEqual(band, expected)


if __name__ == "__main__":
    unittest.main()
