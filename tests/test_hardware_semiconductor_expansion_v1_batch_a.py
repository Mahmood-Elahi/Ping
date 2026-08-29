from __future__ import annotations

import ast
import csv
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import validate_company_registry as registry_validator
from scripts import validate_hardware_semiconductor_expansion_v1_batch_a as batch
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings


def _entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class HardwareSemiconductorExpansionV1BatchATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.production = _entries(batch.PRODUCTION)
        cls.candidates = _entries(batch.CANDIDATE_REGISTRY)
        cls.validated = _entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(encoding="utf-8", newline="") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_baseline_inventory_and_projection_are_exact(self):
        self.assertEqual((batch.BASELINE_SOURCES, batch.BASELINE_MAPPINGS), (353, 4))
        self.assertEqual((batch.BASELINE_COVERAGE, batch.MASTER_TOTAL), (208, 246))
        self.assertGreaterEqual(len(self.production), 452)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 5)
        self.assertEqual(len(batch.REVIEWED_COMPANIES), 20)
        self.assertEqual(len(set(batch.REVIEWED_COMPANIES)), 20)
        self.assertEqual(
            set(batch.REVIEWED_COMPANIES),
            {entry["name"] for entry in batch.CANDIDATE_ENTRIES}
            | {entry["company"] for entry in batch.EXCLUDED},
        )
        self.assertEqual(self.report["projection"], {
            "production_sources": 370,
            "source_delta": 17,
            "parent_mappings": 4,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })

    def test_activation_matches_validated_registry_exactly(self):
        self.assertEqual(self.candidates, self.validated)
        baseline_prefix = self.validated[:353]
        assert_historical_prefix(self, self.production, baseline_prefix)
        assert_historical_prefix(self, self.production, self.validated)
        self.assertEqual(len(self.candidates), 370)
        self.assertEqual(
            [entry["name"] for entry in self.candidates[353:]],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )
        production_names = [entry["name"] for entry in self.production]
        promoted = {entry["name"] for entry in batch.CANDIDATE_ENTRIES}
        for name in batch.REVIEWED_COMPANIES:
            self.assertEqual(production_names.count(name), int(name in promoted))

    def test_all_candidates_reuse_supported_bounded_anonymous_contracts(self):
        self.assertEqual(
            {entry["platform"] for entry in batch.CANDIDATE_ENTRIES},
            {
                "eightfold", "greenhouse", "hibob", "icims", "oracle_recruiting",
                "phenom", "sap_successfactors", "smartrecruiters", "static_html",
                "workday",
            },
        )
        keys = []
        for entry in batch.CANDIDATE_ENTRIES:
            with self.subTest(company=entry["name"]):
                CompanyTarget.from_dict(entry)
                self.assertIsNone(registry_validator.configuration_error(entry))
                self.assertEqual(entry["max_pages"], 1)
                self.assertLessEqual(entry["max_jobs"], 20)
                self.assertTrue(entry["identity_url"].startswith("https://"))
                self.assertTrue(entry["source_identity_regex"])
                self.assertTrue(entry["allowed_apply_hosts"])
                keys.append(registry_validator.source_key(entry))
        self.assertNotIn(None, keys)
        self.assertEqual(len(keys), len(set(keys)))

    def test_reported_outcomes_counts_and_prioritization_are_exact(self):
        self.assertEqual(
            self.report["validated_companies"],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )
        self.assertEqual(
            self.report["excluded_companies"],
            ["Dell", "GoPro", "Kingston Technology"],
        )
        self.assertEqual(self.report["jobs"]["normalized_total"], 194)
        self.assertEqual(self.report["jobs"]["student_intern_hits_total"], 2)
        self.assertEqual(self.report["jobs"]["focus_role_hits_total"], 45)
        self.assertEqual(self.report["jobs"]["student_focus_hits_total"], 2)
        self.assertEqual(self.report["prioritization"]["target_season"], "Summer 2027")
        self.assertEqual(self.report["prioritization"]["candidate_level"], "undergraduate")
        self.assertEqual(self.report["prioritization"]["geography"], ["Canada", "United States", "Remote"])

    def test_every_validated_source_has_an_exact_repeatable_contract(self):
        entries = {entry["name"]: entry for entry in batch.CANDIDATE_ENTRIES}
        for result in self.report["results"]:
            if not result["production_ready"]:
                continue
            with self.subTest(company=result["company"]):
                self.assertEqual(result["validation_status"], "repeatable_anonymous")
                self.assertEqual(result["stable_id_result"], "valid")
                self.assertEqual(result["title_result"], "valid")
                self.assertEqual(result["location_result"], "valid_in_scope")
                self.assertEqual(result["apply_url_result"], "valid")
                self.assertEqual(result["company_attribution_result"], "exact")
                self.assertLessEqual(result["repeat_validation_attempts"], 3)
                self.assertEqual(len(result["runs"]), 2)
                first, second = result["runs"]
                self.assertTrue(first["success"] and second["success"])
                self.assertEqual(first["signature"], second["signature"])
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["titles"], second["titles"])
                self.assertEqual(first["locations"], second["locations"])
                self.assertEqual(first["detail_apply_urls"], second["detail_apply_urls"])
                self.assertLessEqual(first["jobs_normalized"], 20)
                allowed = {value.casefold() for value in entries[result["company"]]["allowed_apply_hosts"]}
                for value in first["detail_apply_urls"]:
                    host = (urlparse(value).hostname or "").casefold()
                    self.assertTrue(any(host == item or host.endswith("." + item) for item in allowed))

    def test_acquisitions_splits_rebrands_and_stale_sources_are_explicit(self):
        reviews = {item["company"]: item for item in self.report["ownership_reviews"]}
        self.assertIn("separated", reviews["Western Digital"]["relationship"])
        self.assertIn("II-VI", reviews["Coherent Corp"]["relationship"])
        self.assertIn("Toshiba Memory", reviews["Kioxia"]["relationship"])
        self.assertEqual(reviews["GoPro"]["disposition"], "excluded_stale_board")
        self.assertEqual(
            reviews["HP Inc."]["disposition"], "validated_exact_hp_inc_board"
        )
        self.assertEqual(reviews["HPE"]["disposition"], "validated_exact_hpe_board")

    def test_blocked_sources_fail_closed_without_bypass_or_substitution(self):
        excluded = {
            row["company"]: row for row in self.report["results"]
            if not row["production_ready"]
        }
        for name in ("Dell", "GoPro", "Kingston Technology"):
            reason = excluded[name]["exclusion_reason"].casefold()
            self.assertTrue("challenge" in reason or "bypass" in reason)
            self.assertEqual(excluded[name]["jobs_normalized"], 0)
        self.assertIn("retired", excluded["GoPro"]["exclusion_reason"])
        self.assertIn("Taiwan", excluded["Kingston Technology"]["exclusion_reason"])

    def test_two_read_retry_still_requires_one_exact_pair(self):
        failed = {"production_ready": False}
        passed = {"production_ready": True}
        with patch.object(batch, "_ORIGINAL_VALIDATE_TWICE", side_effect=[failed, passed]) as mocked:
            result = batch._validate_twice_stabilized({"name": "Drifting"})
        self.assertTrue(result["production_ready"])
        self.assertEqual(result["repeat_validation_attempts"], 2)
        self.assertEqual(mocked.call_count, 2)

    def test_inventory_safe_files_and_non_activation_flags_are_exact(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual(sum(row["production_ready"] == "True" for row in self.inventory), 17)
        self.assertEqual(self.report["safe_to_stage_files"], batch.SAFE_TO_STAGE_FILES)
        self.assertEqual(self.report["generic_adapter_improvements"], [])
        self.assertFalse(self.report["production_registry_activated"])
        self.assertFalse(any(self.report["publication"].values()))
        self.assertNotIn("config/target_companies.yaml", batch.SAFE_TO_STAGE_FILES)
        self.assertNotIn("config/company_parent_source_mappings.yaml", batch.SAFE_TO_STAGE_FILES)
        for relative in batch.SAFE_TO_STAGE_FILES:
            self.assertTrue((batch.ROOT / relative).exists(), relative)
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_validator_has_no_persistence_publication_or_production_runner_imports(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = (
            "supabase", "gspread", "sqlite3", "update_student_dashboard",
            "run_student_job_scan",
        )
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


if __name__ == "__main__":
    unittest.main()
