from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scripts.validate_company_registry import source_key
from src.scraper.company_careers.adapters import paycor, smartrecruiters
from src.scraper.company_careers.listing_completion import finish_authoritative_total
from src.scraper.company_careers.models import SourceScanMetadata
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "target_companies.yaml"
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"

EXPECTED = {
    "Assent": {
        "platform": "smartrecruiters",
        "source": "smartrecruiters:assent",
        "slug": "Assent",
        "page_size": 20,
        "max_pages": 1,
        "max_jobs": 30,
        "retained_total": 22,
        "ceiling": smartrecruiters.MAX_RESULTS,
    },
    "Calfrac Well Services": {
        "platform": "paycor",
        "source": "paycor:8a7883d0676d10a101678af01334303a",
        "slug": None,
        "page_size": 10,
        "max_pages": 1,
        "max_jobs": 30,
        "retained_total": 24,
        "ceiling": paycor.MAX_RESULTS,
    },
    "Arista Networks": {
        "platform": "smartrecruiters",
        "source": "smartrecruiters:aristanetworks",
        "slug": "AristaNetworks",
        "page_size": 20,
        "max_pages": 3,
        "max_jobs": 300,
        "retained_total": 247,
        "ceiling": smartrecruiters.MAX_RESULTS,
    },
    "Gameloft": {
        "platform": "smartrecruiters",
        "source": "smartrecruiters:gameloft",
        "slug": "Gameloft",
        "page_size": 20,
        "max_pages": 1,
        "max_jobs": 75,
        "retained_total": 56,
        "ceiling": smartrecruiters.MAX_RESULTS,
    },
    "Ubisoft": {
        "platform": "smartrecruiters",
        "source": "smartrecruiters:ubisoft2",
        "slug": "Ubisoft2",
        "page_size": 20,
        "max_pages": 4,
        "max_jobs": 350,
        "retained_total": 298,
        "ceiling": smartrecruiters.MAX_RESULTS,
    },
}


class CanadaPrioritySourceRecoveryBatch1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))[
            "target_companies"
        ]
        repairs = yaml.safe_load(REPAIRS.read_text(encoding="utf-8"))[
            "company_careers_reliability_repairs"
        ]
        cls.raw_entries = {item["name"]: item for item in registry}
        cls.repair_entries = {item["name"]: item for item in repairs}
        cls.raw_targets = {item.name: item for item in load_target_companies()}
        cls.effective_targets = {
            item.name: item
            for item in load_target_companies(apply_reliability_repairs=True)
        }

    def test_effective_bounds_cover_retained_totals_within_adapter_ceilings(self):
        for company, expected in EXPECTED.items():
            with self.subTest(company=company):
                target = self.effective_targets[company]
                self.assertEqual(target.platform, expected["platform"])
                self.assertEqual(target.max_jobs, expected["max_jobs"])
                self.assertEqual(target.max_pages, expected["max_pages"])
                self.assertGreaterEqual(target.max_jobs, expected["retained_total"])
                self.assertLessEqual(target.max_jobs, expected["ceiling"])
                if target.platform == "smartrecruiters":
                    self.assertGreaterEqual(
                        target.max_pages * smartrecruiters.MAX_PAGE_SIZE,
                        target.max_jobs,
                    )
                    smartrecruiters.validate_target(target)
                else:
                    paycor.validate_target(target)

    def test_production_and_reliability_overlay_are_synchronized(self):
        for company in EXPECTED:
            with self.subTest(company=company):
                self.assertEqual(
                    self.raw_entries[company], self.repair_entries[company]
                )
                self.assertEqual(
                    self.raw_targets[company], self.effective_targets[company]
                )

    def test_source_keys_slugs_and_page_sizes_are_unchanged(self):
        for company, expected in EXPECTED.items():
            with self.subTest(company=company):
                entry = self.raw_entries[company]
                target = self.effective_targets[company]
                self.assertEqual(source_key(entry), expected["source"])
                self.assertEqual(target.slug, expected["slug"])
                self.assertEqual(target.page_size, expected["page_size"])

    def test_unrelated_smartrecruiters_and_paycor_sources_are_unchanged(self):
        bosch = self.effective_targets["Bosch"]
        self.assertEqual(
            (source_key(self.raw_entries["Bosch"]), bosch.max_pages, bosch.max_jobs),
            ("smartrecruiters:boschgroup", 50, 5_000),
        )
        ballard = self.effective_targets["Ballard Power Systems"]
        self.assertEqual(
            (
                source_key(self.raw_entries["Ballard Power Systems"]),
                ballard.max_pages,
                ballard.max_jobs,
            ),
            ("paycor:8a7883d06937f5f7016978fec55861f7", 1, 50),
        )

    def test_max_jobs_is_not_terminal_evidence(self):
        for company, expected in EXPECTED.items():
            with self.subTest(company=company):
                metadata = SourceScanMetadata(
                    maximum_results=expected["max_jobs"],
                    jobs_normalized=expected["max_jobs"],
                    scan_completeness_reason="safety_limit_reached",
                    stop_reason="maximum_results_reached",
                    safety_limit_reached=True,
                )
                self.assertFalse(
                    finish_authoritative_total(
                        metadata,
                        expected["retained_total"],
                        expected["retained_total"] - 1,
                    )
                )
                self.assertFalse(metadata["scan_complete"])
                self.assertTrue(metadata["safety_limit_reached"])

    def test_authoritative_total_completion_contract_is_unchanged(self):
        for company, expected in EXPECTED.items():
            with self.subTest(company=company):
                metadata = SourceScanMetadata(safety_limit_reached=True)
                self.assertTrue(
                    finish_authoritative_total(
                        metadata,
                        expected["retained_total"],
                        expected["retained_total"],
                    )
                )
                self.assertTrue(metadata["scan_complete"])
                self.assertEqual(
                    metadata["scan_completeness_reason"],
                    "reported_total_reached",
                )
                self.assertEqual(
                    metadata["terminal_evidence_type"], "authoritative_total"
                )


if __name__ == "__main__":
    unittest.main()
