from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.analyze_incomplete_sources import build_report
from scripts.build_targeted_terminal_source_table import (
    DEFAULT_PARETO as TARGETED_PARETO,
    build_rows as build_targeted_rows,
)
from scripts.validate_remaining_ats_failures import representative_targets
from scripts.validate_targeted_terminal_evidence import (
    HARD_REQUEST_BUDGET_PER_SOURCE,
    SOURCE_REQUEST_BUDGETS,
    _RequestGuard,
    _validate_target,
    representative_targets as targeted_representative_targets,
    request_budget_for,
)
from scripts.validate_large_board_recovery import (
    MAX_HTTP_REQUESTS_PER_REPRESENTATIVE,
    REPRESENTATIVES as LARGE_BOARD_REPRESENTATIVES,
    representative_targets as large_board_representative_targets,
)
from src.scraper.company_careers.models import (
    CompanyTarget,
    SourceScanMetadata,
    publish_source_scan_metadata,
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
            "runtime_seconds": 123.4,
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
        coverage = {
            "runtime_seconds": 123.4,
            "coverage": [row(f"Company {index:02d}") for index in range(12, 0, -1)],
        }
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
        self.assertEqual(report["summary"]["incomplete_nonfailed_sources"], 56)
        self.assertEqual(sum(report["summary"]["primary_category_counts"].values()), 56)
        self.assertEqual(
            [item["strategy"] for item in report["budget_strategies"]],
            [
                "through_150_total_requests",
                "through_300_total_requests",
                "through_500_total_requests",
                "through_750_total_requests",
                "through_1000_total_requests",
            ],
        )
        self.assertEqual(
            report["selected_strategy"],
            "absolute_1000_total_listing_request_ceiling",
        )
        self.assertEqual(
            sum(
                item["source_count"]
                for item in report["groupings"]["primary_category"]
            ),
            56,
        )
        self.assertEqual(report["projected_after"]["incomplete"], 56)
        self.assertEqual(
            sum(report["projected_after"]["remaining_breakdown"].values()), 56
        )
        self.assertEqual(report["runtime_model"]["latest_actual_runtime_seconds"], 937.7)
        self.assertEqual(
            report["runtime_model"]["latest_actual_runtime_source"],
            "persisted_run_history",
        )
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
        self.assertEqual(report["summary"]["large_api_incomplete_sources"], 1)
        self.assertEqual(recovery["selected_source_count"], 0)
        self.assertEqual(recovery["additional_listing_requests"], 0)
        self.assertEqual(recovery["additional_jobs_enumerated"], 0)
        self.assertEqual(recovery["selected_sources"], [])
        self.assertEqual(
            sum(len(names) for names in recovery["cost_bands"].values()),
            1,
        )
        self.assertEqual(len(recovery["cost_bands"]["<=150"]), 0)
        self.assertEqual(recovery["cost_bands"]["151-300"], ["Apple"])
        self.assertEqual(len(recovery["cost_bands"]["301-500"]), 0)

    def test_cumulative_recovery_tiers_expose_cost_cliff_and_runtime(self):
        report = build_report()
        tiers = report["cumulative_recovery_tiers"]

        self.assertEqual(
            [tier["ceiling"] for tier in tiers],
            [150, 300, 500, 750, 1_000],
        )
        self.assertEqual(
            [tier["sources_recovered"] for tier in tiers],
            [0, 0, 0, 0, 0],
        )
        self.assertEqual(
            [tier["extra_requests"] for tier in tiers],
            [0, 0, 0, 0, 0],
        )
        self.assertEqual(
            [tier["extra_jobs"] for tier in tiers],
            [0, 0, 0, 0, 0],
        )
        self.assertEqual(
            tiers[3]["incremental_from_previous_tier"]["sources_recovered"],
            0,
        )
        self.assertEqual(
            tiers[4]["projected_total_audit_runtime_seconds"], 937.7
        )
        self.assertEqual(
            tiers[4]["projected_runtime_percentage_of_timeout"], 34.73
        )
        self.assertEqual(
            [
                row["company"]
                for row in report["runtime_model"]["request_cost_dominators"][:3]
            ],
            [],
        )

    def test_large_board_table_uses_effective_page_size_for_projection(self):
        report = build_report()
        table = report["large_board_recovery"]["table"]
        apple = next(row for row in table if row["company"] == "Apple")

        self.assertEqual(apple["effective_server_page_size"], 20)
        self.assertEqual(apple["authoritative_total"], 4_566)
        self.assertEqual(apple["exact_total_listing_requests_required"], 229)
        self.assertFalse(apple["projected_completion_possible_at_500"])

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
        self.assertEqual(len(sources), 56)
        self.assertEqual(
            len(sources),
            sum(report["summary"]["primary_category_counts"].values()),
        )
        self.assertTrue(all(isinstance(row["primary_category"], str) for row in sources))
        reasons = {row["structural_incomplete_reason"] for row in sources}
        self.assertTrue(
            {
                "static_terminal_unproven",
                "browser_terminal_unproven",
                "api_terminal_metadata_missing",
                "authoritative_total_not_reached",
                "pagination_contract_unknown",
            }.issubset(reasons)
        )

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
            families["avature"]["maximum_safely_supported_page_sizes"],
            [20],
        )
        self.assertEqual(
            report["runtime_model"]["requests_avoided_through_larger_page_sizes"],
            0,
        )

    def test_manual_validation_selects_one_source_per_migrated_family(self):
        targets = representative_targets()

        self.assertEqual(len(targets), 28)
        self.assertEqual(len({target.platform for target in targets}), 28)
        self.assertNotIn("browser_rendered_html", {target.platform for target in targets})

    def test_targeted_manual_validator_covers_every_changed_family(self):
        targets = targeted_representative_targets()
        self.assertEqual(len(targets), 9)
        self.assertEqual(
            {target.platform for target in targets},
            {
                "amazon",
                "avature",
                "browser_rendered_html",
                "icims",
                "javascript_embedded_data",
                "lever",
                "static_html",
                "talentbrew",
            },
        )

    def test_targeted_manual_validator_has_an_independent_hard_request_guard(self):
        guard = _RequestGuard()
        for _ in range(HARD_REQUEST_BUDGET_PER_SOURCE):
            guard.consume()
        self.assertEqual(guard.count, HARD_REQUEST_BUDGET_PER_SOURCE)
        with self.assertRaisesRegex(RuntimeError, "hard request budget exceeded"):
            guard.consume()

    def test_targeted_validator_uses_explicit_source_specific_budgets(self):
        targets = {target.name: target for target in targeted_representative_targets()}
        self.assertEqual(
            SOURCE_REQUEST_BUDGETS,
            {
                "Applied Materials": 140,
                "Amazon": 25,
                "Continental": 70,
                "Veeva Systems": 60,
            },
        )
        for company, budget in SOURCE_REQUEST_BUDGETS.items():
            self.assertEqual(request_budget_for(targets[company]), budget)
        self.assertEqual(
            request_budget_for(targets["Palo Alto Networks"]),
            HARD_REQUEST_BUDGET_PER_SOURCE,
        )

    def test_targeted_validator_budget_exhaustion_preserves_partial_state(self):
        target = CompanyTarget(
            name="Amazon",
            platform="amazon",
            search_terms=["bounded"],
            max_pages=30,
        )

        def interrupted_adapter(session, _target, _timeout):
            metadata = SourceScanMetadata(
                terminal_evidence_type="api_pagination_contract",
                terminal_evidence_value="continuation",
                has_more=True,
                pagination_evidence_status="continuation",
            )
            publish_source_scan_metadata(session, metadata)
            while True:
                session.guard.consume()
                metadata["pages_requested"] += 1
                metadata["pages_completed"] += 1
                metadata["jobs_normalized"] = metadata["pages_completed"] * 3

        with patch(
            "scripts.validate_targeted_terminal_evidence.ADAPTERS",
            {"amazon": interrupted_adapter},
        ):
            result = _validate_target(target)

        self.assertNotIn("error", result)
        self.assertEqual(result["request_budget"], 25)
        self.assertEqual(result["effective_request_count"], 25)
        self.assertEqual(result["jobs_fetched"], 75)
        self.assertEqual(result["pages_requested"], 25)
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["reason"], "validator_request_budget_reached")
        self.assertEqual(result["stop_reason"], "validator_request_budget_reached")
        self.assertTrue(result["validator_request_budget_reached"])
        self.assertEqual(
            result["terminal_evidence"]["type"], "api_pagination_contract"
        )
        self.assertTrue(result["terminal_evidence"]["has_more"])

    def test_exact_29_source_terminal_target_table(self):
        rows = build_targeted_rows(TARGETED_PARETO)
        grouped = {}
        for item in rows:
            grouped.setdefault(item["source_type"], []).append(item["company"])
        self.assertEqual(
            grouped,
            {
                "api_listing": [
                    "Amazon",
                    "Bank of America",
                    "Continental",
                    "Google",
                    "Hexagon Autonomy & Positioning / NovAtel",
                    "Kinaxis",
                    "Lenovo",
                    "MaxLinear",
                    "Meta",
                    "SECURE Energy Services",
                    "TSMC",
                    "Veeva Systems",
                ],
                "browser_rendered": [
                    "ASML",
                    "Aurora Innovation",
                    "ByteDance",
                    "Deliveroo",
                    "Figure AI",
                    "LINE",
                    "MDA Space",
                ],
                "static_html": [
                    "Applied Materials",
                    "BlackRock",
                    "Caterpillar",
                    "Coupang",
                    "General Motors",
                    "Intuitive Surgical",
                    "Palo Alto Networks",
                    "Rakuten",
                    "SharkNinja",
                    "Siemens",
                ],
            },
        )

    def test_large_board_validator_has_all_required_controls_and_hard_cap(self):
        self.assertEqual(MAX_HTTP_REQUESTS_PER_REPRESENTATIVE, 1_000)
        self.assertEqual(
            set(LARGE_BOARD_REPRESENTATIVES),
            {
                "200_to_300_requests",
                "300_to_500_requests",
                "beyond_old_ceiling_now_recoverable",
                "detail_coupled_control",
                "mutable_control",
            },
        )
        self.assertEqual(len(large_board_representative_targets()), 5)


if __name__ == "__main__":
    unittest.main()
