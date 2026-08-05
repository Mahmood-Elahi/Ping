from __future__ import annotations

import ast
import csv
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import validate_avature_icims_batch as rollout
from scripts import validate_company_registry as validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import avature, icims
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.avature_icims_candidates.yaml"
VALIDATED = ROOT / "config" / "target_companies.avature_icims_validated.yaml"
INVENTORY = ROOT / "config" / "company_avature_icims_candidates.csv"


def entries(path: Path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def additions(path: Path):
    production_names = {validator._name_key(item) for item in entries(PRODUCTION)}
    return [item for item in entries(path) if validator._name_key(item) not in production_names]


class AvatureICIMSBulkActivationTests(unittest.TestCase):
    def test_33_exact_candidate_company_set_derived_from_research(self):
        self.assertEqual(rollout._derived_research_candidates(), rollout.EXPECTED_COMPANIES)
        self.assertEqual(len(rollout.EXPECTED_COMPANIES), 15)

    def test_34_already_covered_companies_are_excluded(self):
        production = {validator._name_key(item) for item in entries(PRODUCTION)}
        mappings = yaml.safe_load(rollout.PARENT_MAPPINGS.read_text(encoding="utf-8"))
        mapped = {
            " ".join(item["child_company"].casefold().split())
            for item in mappings["company_parent_source_mappings"]
            if item["enabled"] is True
        }
        candidates = {validator._name_key(item) for item in rollout.CANDIDATE_ENTRIES}
        self.assertFalse(candidates & (production | mapped))

    def test_35_production_registry_is_exact_candidate_prefix(self):
        self.assertTrue(CANDIDATES.read_bytes().startswith(PRODUCTION.read_bytes()))

    def test_36_master_workbook_candidate_order(self):
        ranks = [item["original_workbook_rank"] for item in rollout.CANDIDATE_ENTRIES]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual([item["name"] for item in additions(CANDIDATES)], rollout.EXPECTED_COMPANIES)

    def test_37_candidate_loader_compatibility(self):
        baseline = len(load_target_companies(str(PRODUCTION)))
        self.assertEqual(len(load_target_companies(str(CANDIDATES))), baseline + 15)

    def test_38_validated_registry_contains_only_safe_candidates(self):
        with INVENTORY.open(newline="", encoding="utf-8") as handle:
            safe = [row["company"] for row in csv.DictReader(handle) if row["safe_to_activate"] == "true"]
        self.assertEqual([item["name"] for item in additions(VALIDATED)], safe)
        self.assertNotIn("Siemens", safe)
        self.assertNotIn("bp", safe)
        self.assertNotIn("Wayfair", safe)

    def test_39_synthetic_baseline_delta_calculation(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.yaml"
            baseline.write_text("target_companies:\n  - name: Synthetic\n    platform: greenhouse\n    slug: synthetic\n", encoding="utf-8")
            content = validator.build_candidate_registry_bytes(
                baseline,
                [{"name": "Delta", "platform": "lever", "slug": "delta"}],
            )
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_bytes(content)
            self.assertEqual(len(load_target_companies(str(candidate))) - len(load_target_companies(str(baseline))), 1)

    def test_40_duplicate_normalized_company_rejection(self):
        duplicates = validator._duplicate_company_owners(
            [{"name": "Example Company"}], [{"name": " example   company "}]
        )
        self.assertEqual(duplicates, {0: "Example Company"})

    def test_41_duplicate_normalized_source_rejection(self):
        duplicates = validator._duplicate_owners(
            [{"name": "One", "platform": "icims", "endpoint_url": "https://x.test/api/jobs", "board_id": "board"}],
            [{"name": "Two", "platform": "icims", "endpoint_url": "https://x.test/api/jobs", "board_id": "BOARD"}],
        )
        self.assertEqual(duplicates, {0: "One"})

    def test_42_production_scanner_dispatch_is_used(self):
        self.assertIs(scanner.ADAPTERS["avature"], avature.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["icims"], icims.fetch_jobs)
        source = Path(rollout.__file__).read_text(encoding="utf-8")
        self.assertIn("adapters=scanner.ADAPTERS", source)

    def test_43_no_company_specific_adapter_branches(self):
        source = Path(avature.__file__).read_text(encoding="utf-8") + Path(icims.__file__).read_text(encoding="utf-8")
        for company in rollout.EXPECTED_COMPANIES:
            self.assertNotIn(company, source)

    def test_44_validation_batch_size_is_capped_at_five(self):
        self.assertEqual(rollout.MAX_BATCH_SIZE, 5)
        for start in range(0, len(rollout.CANDIDATE_ENTRIES), rollout.MAX_BATCH_SIZE):
            self.assertLessEqual(len(rollout.CANDIDATE_ENTRIES[start : start + rollout.MAX_BATCH_SIZE]), 5)

    def test_45_maximum_two_pages_enforced(self):
        self.assertEqual(rollout.MAX_PAGES, avature.MAX_PAGES)
        self.assertEqual(rollout.MAX_PAGES, icims.MAX_PAGES)
        self.assertTrue(all(item["max_pages"] <= 2 for item in rollout.CANDIDATE_ENTRIES))

    def test_46_maximum_twenty_jobs_enforced(self):
        self.assertEqual(rollout.MAX_JOBS, avature.MAX_RESULTS)
        self.assertEqual(rollout.MAX_JOBS, icims.MAX_RESULTS)
        self.assertTrue(all(item["max_jobs"] <= 20 for item in rollout.CANDIDATE_ENTRIES))

    def test_47_no_supabase_initialization(self):
        self._assert_no_imports("supabase")

    def test_48_no_google_sheets_initialization(self):
        self._assert_no_imports("gspread", "google_sheets_publisher")

    def test_49_no_sqlite_initialization(self):
        self._assert_no_imports("sqlite3")

    def test_50_production_config_remains_unchanged(self):
        committed = subprocess.check_output(
            ["git", "show", "HEAD:config/target_companies.yaml"], cwd=ROOT
        )
        self.assertEqual(PRODUCTION.read_bytes().replace(b"\r\n", b"\n"), committed.replace(b"\r\n", b"\n"))

    def test_51_inventory_and_registries_have_no_duplicates(self):
        with INVENTORY.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["company"] for row in rows], rollout.EXPECTED_COMPANIES)
        self.assertEqual(len({validator._name_key(item) for item in entries(CANDIDATES)}), len(entries(CANDIDATES)))
        keys = [validator.source_key(item) for item in entries(CANDIDATES)]
        keys = [key for key in keys if key]
        self.assertEqual(len(keys), len(set(keys)))

    def _assert_no_imports(self, *forbidden: str):
        tree = ast.parse(Path(rollout.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in forbidden:
            self.assertFalse(any(name in module for module in imported), imported)


if __name__ == "__main__":
    unittest.main()
