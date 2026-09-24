from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.analyze_incomplete_sources import build_report
from scripts.validate_remaining_ats_failures import representative_targets
from scripts.validate_targeted_terminal_evidence import (
    representative_targets as targeted_representative_targets,
)
from scripts.validate_large_board_recovery import (
    MAX_HTTP_REQUESTS_PER_REPRESENTATIVE,
    REPRESENTATIVES as LARGE_BOARD_REPRESENTATIVES,
    representative_targets as large_board_representative_targets,
)


def row(company: str, **overrides):
    value = {
        "company": company,
        "platform": "example",
        "status": "fetched",
        "fetched_count": 2,
        "scan_complete": False,
        "scan_health": "incomplete",
        "scan_completeness_reason": "adapter_completeness_not_proven",
        "total_available": None,
        "stop_reason": "not_started",
        "safety_limit_reached": False,
    }
    value.update(overrides)
    return value


class IncompleteSourceParetoTests(unittest.TestCase):
    def test_report_excludes_hard_failures_and_has_deterministic_bucket_metrics(self):
        coverage = {
            "coverage": [
                row("Alpha"),
                row(
                    "Beta",
                    platform="paged",
                    fetched_count=5,
                    total_available=8,
                    scan_completeness_reason="safety_limit_reached",
                    stop_reason="maximum_pages_reached",
                    safety_limit_reached=True,
                ),
                row(
                    "Complete",
                    scan_complete=True,
                    scan_health="complete",
                    scan_completeness_reason="reported_total_reached",
                    stop_reason="reported_total_reached",
                ),
                row("Failed", status="failed_technical", scan_health="failed"),
                {"company": "Mapped child", "status": "covered_via_parent_source"},
            ]
        }
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "company_careers_coverage_fixture.json"
            path.write_text(json.dumps(coverage), encoding="utf-8")
            first = build_report(path)
            second = build_report(path)

        self.assertEqual(first, second)
        self.assertEqual(first["summary"]["complete_sources"], 1)
        self.assertEqual(first["summary"]["incomplete_nonfailed_sources"], 2)
        self.assertEqual(first["summary"]["hard_failed_sources"], 1)
        platforms = first["groupings"]["platform"]
        paged = next(bucket for bucket in platforms if bucket["key"] == "paged")
        self.assertEqual(paged["known_unfetched_jobs"], 3)
        self.assertEqual(paged["max_pages_hit"], 1)

    def test_representatives_are_sorted_and_capped_at_ten(self):
        coverage = {"coverage": [row(f"Company {index:02d}") for index in range(12, 0, -1)]}
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "company_careers_coverage_fixture.json"
            path.write_text(json.dumps(coverage), encoding="utf-8")
            report = build_report(path)

        bucket = report["groupings"]["platform"][0]
        self.assertEqual(len(bucket["representative_sources"]), 10)
        self.assertEqual(bucket["representative_sources"][0], "Company 01")
        self.assertEqual(bucket["representative_sources"][-1], "Company 10")

    def test_saved_report_uses_latest_actual_incomplete_denominator(self):
        report = build_report()
        self.assertEqual(report["summary"]["incomplete_nonfailed_sources"], 82)
        self.assertEqual(sum(report["summary"]["primary_category_counts"].values()), 82)
        self.assertEqual(
            [item["strategy"] for item in report["budget_strategies"]],
            [
                "through_10_total_requests",
                "through_25_total_requests",
                "through_50_total_requests",
                "through_100_total_requests",
            ],
        )
        self.assertEqual(
            report["selected_strategy"],
            "absolute_100_total_listing_request_ceiling",
        )
        self.assertEqual(
            sum(
                item["source_count"]
                for item in report["groupings"]["primary_category"]
            ),
            82,
        )
        self.assertEqual(report["projected_after"]["incomplete"], 67)
        self.assertEqual(
            sum(report["projected_after"]["remaining_breakdown"].values()), 67
        )
        self.assertEqual(report["runtime_model"]["latest_actual_runtime_seconds"], 711.0)
        for grouping in report["groupings"].values():
            self.assertTrue(
                all("percentage_of_incomplete" in bucket for bucket in grouping)
            )
            self.assertTrue(
                all("percentage_of_138" not in bucket for bucket in grouping)
            )

    def test_exact_large_board_selection_and_cost_bands_are_deterministic(self):
        report = build_report()
        recovery = report["large_board_recovery"]
        self.assertEqual(report["summary"]["large_api_incomplete_sources"], 33)
        self.assertEqual(recovery["selected_source_count"], 15)
        self.assertEqual(recovery["additional_listing_requests"], 982)
        self.assertEqual(recovery["additional_jobs_enumerated"], 15_811)
        self.assertEqual(
            recovery["selected_sources"],
            sorted(
                [
                    "Blue Origin",
                    "Capital One",
                    "Ericsson Canada",
                    "GlobalFoundries",
                    "HP Inc.",
                    "Hitachi",
                    "Honeywell",
                    "Mercado Libre",
                    "Nvidia",
                    "Oracle",
                    "Rivian",
                    "SLB",
                    "Salesforce",
                    "Stellantis",
                    "TD Bank",
                ]
            ),
        )
        self.assertEqual(
            sum(len(names) for names in recovery["cost_bands"].values()),
            33,
        )
        self.assertEqual(len(recovery["cost_bands"]["<=25"]), 1)
        self.assertEqual(len(recovery["cost_bands"]["26-50"]), 0)
        self.assertEqual(len(recovery["cost_bands"]["51-75"]), 4)
        self.assertEqual(len(recovery["cost_bands"]["76-100"]), 10)
        self.assertEqual(len(recovery["cost_bands"][">100"]), 18)

    def test_runtime_is_extracted_from_current_artifact_when_present(self):
        coverage = {"runtime_seconds": 123.4, "coverage": [row("Alpha")]}
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "coverage.json"
            path.write_text(json.dumps(coverage), encoding="utf-8")
            report = build_report(path, diagnostics_path=None)
        self.assertEqual(report["runtime_model"]["latest_actual_runtime_seconds"], 123.4)
        self.assertEqual(
            report["runtime_model"]["latest_actual_runtime_source"],
            "coverage_artifact",
        )

    def test_primary_categories_are_mutually_exclusive(self):
        report = build_report()
        sources = report["sources"]
        self.assertEqual(len(sources), 82)
        self.assertEqual(
            len(sources),
            sum(report["summary"]["primary_category_counts"].values()),
        )
        self.assertTrue(all(isinstance(row["primary_category"], str) for row in sources))

    def test_detail_coupled_target_remains_excluded_from_recovery(self):
        report = build_report()
        assent = next(row for row in report["sources"] if row["company"] == "Assent")
        self.assertTrue(assent["detail_coupled"])
        self.assertEqual(assent["primary_category"], "remaining_arbitrary_cap_cases")
        self.assertFalse(assent["source_would_become_complete"])

    def test_page_size_optimizations_are_safe_and_deterministic(self):
        report = build_report()
        families = {item["platform"]: item for item in report["page_size_families"]}
        self.assertEqual(
            families["mcloud_cws"]["maximum_safely_supported_page_sizes"],
            [20],
        )
        self.assertGreater(
            report["runtime_model"]["requests_avoided_through_larger_page_sizes"],
            0,
        )

    def test_manual_validation_selects_one_source_per_migrated_family(self):
        targets = representative_targets()

        self.assertEqual(len(targets), 29)
        self.assertEqual(len({target.platform for target in targets}), 29)
        self.assertNotIn("browser_rendered_html", {target.platform for target in targets})

    def test_targeted_manual_validator_covers_every_changed_family(self):
        targets = targeted_representative_targets()
        self.assertEqual(len(targets), 13)
        self.assertEqual(
            {target.platform for target in targets},
            {
                "adp",
                "amazon",
                "avature",
                "bamboohr",
                "google",
                "ibm_search",
                "icims",
                "javascript_embedded_data",
                "lever",
                "meta",
                "paycor",
                "peoplesoft",
                "smartrecruiters",
            },
        )

    def test_large_board_validator_has_all_required_controls_and_hard_cap(self):
        self.assertEqual(MAX_HTTP_REQUESTS_PER_REPRESENTATIVE, 100)
        self.assertEqual(
            set(LARGE_BOARD_REPRESENTATIVES),
            {
                "<=25",
                "26-50",
                "51-75",
                "76-100",
                ">100_control",
                ">5000_jobs_control",
                "detail_coupled_control",
                "mutable_control",
            },
        )
        self.assertEqual(len(large_board_representative_targets()), 8)


if __name__ == "__main__":
    unittest.main()
