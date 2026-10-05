from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.scraper.company_careers.adapters import (
    eightfold,
    icims,
    sap_successfactors,
    smartrecruiters,
    workday,
)
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "jobs" / "company_careers_coverage_20261001_142550.json"

EXPECTED_BASELINE = {
    "General Motors": ("static_html", "terminal_contradiction", "no_pagination_configured"),
    "Caterpillar": ("static_html", "terminal_contradiction", "no_pagination_configured"),
    "Intuitive Surgical": ("static_html", "terminal_contradiction", "no_pagination_configured"),
    "Coupang": ("static_html", "pagination_loop", "repeated_page"),
    "ASML": ("browser_rendered_html", "terminal_contradiction", "browser_exhaustion_not_proven"),
    "Stantec": ("browser_rendered_html", "terminal_contradiction", "browser_exhaustion_not_proven"),
    "Dyson": ("static_html", "pagination_unproven", "no_pagination_configured"),
    "Athabasca Oil": ("static_html", "pagination_unproven", "no_pagination_configured"),
    "Bosch": ("smartrecruiters", "authoritative_total_not_reached", "maximum_results_reached"),
    "SGS": ("smartrecruiters", "authoritative_total_not_reached", "maximum_results_reached"),
    "CN Rail": ("sap_successfactors", "collection_changed", "collection_changed_during_scan"),
    "Scotiabank": ("sap_successfactors", "collection_changed", "collection_changed_during_scan"),
    "AstraZeneca": ("workday", "collection_changed", "collection_changed_during_scan"),
    "Capital One": ("workday", "collection_changed", "collection_changed_during_scan"),
    "HP Inc.": ("eightfold", "collection_changed", "collection_changed_during_scan"),
    "GlobalFoundries": ("eightfold", "collection_changed", "collection_changed_during_scan"),
    "Atlassian": ("atlassian", "collection_changed", "collection_changed_during_scan"),
    "Meta": ("meta", "terminal_contradiction", "maximum_results_reached"),
    "Rivian": ("icims", "collection_changed", "collection_changed_during_scan"),
    "Google": ("google", "identity_failure", "identity_failure"),
}


class Batch9RecoveryEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        audit = json.loads(AUDIT.read_text(encoding="utf-8"))
        cls.rows = {
            row["company"]: row
            for row in audit["coverage"]
            if row.get("company") in EXPECTED_BASELINE
        }
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
            if target.name in EXPECTED_BASELINE
        }

    def test_exact_october_1_baseline_matrix_is_preserved(self) -> None:
        self.assertEqual(set(self.rows), set(EXPECTED_BASELINE))
        self.assertEqual(set(self.targets), set(EXPECTED_BASELINE))
        for company, expected in EXPECTED_BASELINE.items():
            with self.subTest(company=company):
                row = self.rows[company]
                self.assertEqual(
                    (
                        row["platform"],
                        row["source_health_code"],
                        row["stop_reason"],
                    ),
                    expected,
                )
                self.assertFalse(row["scan_complete"])
                self.assertFalse(row["authoritative_for_absence"])

    def test_static_sources_do_not_use_false_single_page_completion(self) -> None:
        names = {
            "General Motors",
            "Caterpillar",
            "Intuitive Surgical",
            "Coupang",
            "Dyson",
            "Athabasca Oil",
        }
        for company in names:
            with self.subTest(company=company):
                self.assertFalse(self.targets[company].single_page_collection)
                self.assertEqual(self.rows[company]["source_identity_status"], "unproven")

        for company in {"General Motors", "Caterpillar", "Intuitive Surgical"}:
            self.assertIsNone(self.targets[company].pagination_url_template)
            self.assertTrue(self.rows[company]["next_url_present"])
        self.assertEqual(self.rows["Coupang"]["stop_reason"], "repeated_page")
        self.assertTrue(self.rows["Coupang"]["has_more"])

    def test_browser_sources_keep_identity_separate_from_exhaustion(self) -> None:
        for company in {"ASML", "Stantec"}:
            with self.subTest(company=company):
                target = self.targets[company]
                row = self.rows[company]
                self.assertEqual(target.endpoint_variant, "browser_rendered_html_v1")
                self.assertEqual(row["source_identity_status"], "unproven")
                self.assertTrue(row["has_more"])
                self.assertFalse(row["scan_complete"])

    def test_smartrecruiters_boards_exceed_the_reviewed_adapter_ceiling(self) -> None:
        for company in {"Bosch", "SGS"}:
            with self.subTest(company=company):
                target = self.targets[company]
                row = self.rows[company]
                smartrecruiters.validate_target(target)
                self.assertGreater(row["total_available"], smartrecruiters.MAX_RESULTS)
                self.assertEqual((target.max_pages, target.max_jobs), (1, 20))
                self.assertEqual(row["source_identity_status"], "validated")
                self.assertTrue(row["authoritative_total_present"])
                self.assertFalse(row["scan_complete"])

    def test_mutable_ats_sources_remain_fail_closed(self) -> None:
        groups = {
            sap_successfactors: {"CN Rail", "Scotiabank"},
            workday: {"AstraZeneca", "Capital One"},
            eightfold: {"HP Inc.", "GlobalFoundries"},
            icims: {"Rivian"},
        }
        for adapter, companies in groups.items():
            self.assertGreater(adapter.MAX_PAGES, 0)
            for company in companies:
                with self.subTest(company=company):
                    row = self.rows[company]
                    self.assertTrue(row["collection_changed_during_scan"])
                    self.assertEqual(
                        row["scan_completeness_reason"],
                        "collection_changed_during_scan",
                    )
                    self.assertFalse(row["complete_collection_response"])
                    self.assertFalse(row["scan_complete"])

    def test_specialized_sources_retain_precise_blockers(self) -> None:
        atlassian = self.rows["Atlassian"]
        self.assertEqual(
            atlassian["total_available"] - atlassian["fetched_count"],
            18,
        )
        self.assertTrue(atlassian["collection_changed_during_scan"])

        meta = self.rows["Meta"]
        self.assertTrue(meta["has_more"])
        self.assertTrue(meta["safety_limit_reached"])
        self.assertEqual(meta["terminal_evidence_type"], "search_terms_incomplete")

        rivian = self.rows["Rivian"]
        self.assertEqual(rivian["rejected_record_count"], 3)
        self.assertTrue(rivian["collection_changed_during_scan"])

        google = self.rows["Google"]
        self.assertEqual(google["source_identity_status"], "failed")
        self.assertEqual(google["error_type"], "adapter_schema_mismatch")
        self.assertEqual(google["error"], "Google Careers data contract is malformed")

    def test_parent_source_mappings_are_unchanged(self) -> None:
        mappings = load_parent_source_mappings()
        by_child = {mapping.child_company: mapping for mapping in mappings}
        self.assertEqual(by_child["Instagram"].parent_company, "Meta")
        self.assertEqual(by_child["WhatsApp"].parent_company, "Meta")
        self.assertEqual(by_child["Google DeepMind"].parent_company, "Google")
        self.assertEqual(by_child["Instagram"].canonical_source, "custom:meta")
        self.assertEqual(by_child["Google DeepMind"].canonical_source, "custom:google")


if __name__ == "__main__":
    unittest.main()
