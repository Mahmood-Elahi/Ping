from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.analyze_incomplete_sources import build_report
from scripts.validate_remaining_ats_failures import representative_targets


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

    def test_saved_report_accounts_for_exact_138_and_models_cost_strategies(self):
        report = build_report()
        self.assertEqual(report["summary"]["incomplete_nonfailed_sources"], 138)
        self.assertEqual(sum(report["summary"]["primary_category_counts"].values()), 138)
        self.assertEqual(
            [item["strategy"] for item in report["budget_strategies"]],
            ["plus_10", "plus_25", "plus_50", "plus_100"],
        )
        self.assertEqual(
            report["selected_strategy"],
            "plus_50_with_absolute_100_listing_request_ceiling",
        )
        self.assertEqual(
            sum(
                item["source_count"]
                for item in report["groupings"]["primary_category"]
            ),
            138,
        )
        self.assertEqual(report["projected_after"]["incomplete"], 88)
        self.assertEqual(
            sum(report["projected_after"]["remaining_breakdown"].values()), 88
        )

    def test_page_size_optimizations_are_safe_and_deterministic(self):
        report = build_report()
        families = {item["platform"]: item for item in report["page_size_families"]}
        self.assertEqual(
            families["hawksearch_v2"]["maximum_safely_supported_page_sizes"],
            [100],
        )
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


if __name__ == "__main__":
    unittest.main()
