from __future__ import annotations

import ast
import csv
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import validate_company_registry as validator
from scripts import validate_static_embedded_batch as rollout
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import embedded_data, static_html
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]


def entries(path: Path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def additions(path: Path):
    baseline = {validator._name_key(item) for item in entries(rollout.PRODUCTION)}
    return [item for item in entries(path) if validator._name_key(item) not in baseline]


class StaticEmbeddedBulkActivationTests(unittest.TestCase):
    def test_01_exact_candidate_set_derived_from_research(self):
        self.assertEqual(rollout._derived_research_candidates(), rollout.EXPECTED_COMPANIES)
        self.assertEqual((len(rollout.STATIC_COMPANIES), len(rollout.EMBEDDED_COMPANIES)), (18, 9))

    def test_02_production_and_parent_covered_companies_excluded(self):
        production = {validator._name_key(item) for item in entries(rollout.PRODUCTION)}
        mappings = yaml.safe_load(rollout.PARENT_MAPPINGS.read_text(encoding="utf-8"))
        mapped = {" ".join(item["child_company"].casefold().split()) for item in mappings["company_parent_source_mappings"] if item["enabled"] is True}
        candidates = {validator._name_key(item) for item in rollout.CANDIDATE_ENTRIES}
        self.assertFalse(candidates & (production | mapped))

    def test_03_candidate_order_follows_master_workbook(self):
        ranks = [item["original_workbook_rank"] for item in rollout.CANDIDATE_ENTRIES]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual([item["name"] for item in additions(rollout.CANDIDATE_REGISTRY)], rollout.EXPECTED_COMPANIES)

    def test_04_production_bytes_preserved_as_prefix(self):
        production = rollout.PRODUCTION.read_bytes()
        self.assertTrue(rollout.CANDIDATE_REGISTRY.read_bytes().startswith(production))
        self.assertTrue(rollout.VALIDATED_REGISTRY.read_bytes().startswith(production))

    def test_05_candidate_loader_compatibility(self):
        self.assertEqual(len(load_target_companies(str(rollout.CANDIDATE_REGISTRY))), 132)

    def test_06_validated_registry_contains_only_safe_candidates(self):
        with rollout.CANDIDATE_INVENTORY.open(newline="", encoding="utf-8") as handle:
            safe = [row["company"] for row in csv.DictReader(handle) if row["safe_to_activate"] == "true"]
        self.assertEqual([item["name"] for item in additions(rollout.VALIDATED_REGISTRY)], safe)
        self.assertTrue(safe)

    def test_07_synthetic_baseline_delta_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "base.yaml"
            baseline.write_text("target_companies:\n  - name: Base\n    platform: greenhouse\n    slug: base\n", encoding="utf-8")
            content = validator.build_candidate_registry_bytes(baseline, [{"name": "Delta", "platform": "lever", "slug": "delta"}])
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_bytes(content)
            self.assertEqual(len(load_target_companies(str(candidate))) - len(load_target_companies(str(baseline))), 1)

    def test_08_duplicate_normalized_company_rejection(self):
        self.assertEqual(validator._duplicate_company_owners([{"name": "Acme"}], [{"name": " acme "}]), {0: "Acme"})

    def test_09_duplicate_normalized_source_rejection(self):
        first = {"name": "One", "platform": "static_html", "listing_url": "https://jobs.test/jobs"}
        second = {"name": "Two", "platform": "static_html", "listing_url": "https://jobs.test/other"}
        self.assertEqual(validator._duplicate_owners([first], [second]), {0: "One"})

    def test_10_production_scanner_dispatch_used(self):
        self.assertIs(scanner.ADAPTERS["static_html"], static_html.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["javascript_embedded_data"], embedded_data.fetch_jobs)
        self.assertIn("adapters=scanner.ADAPTERS", Path(rollout.__file__).read_text(encoding="utf-8"))

    def test_11_no_company_name_branches(self):
        source = Path(static_html.__file__).read_text(encoding="utf-8") + Path(embedded_data.__file__).read_text(encoding="utf-8")
        for company in rollout.EXPECTED_COMPANIES:
            self.assertNotIn(company, source)

    def test_12_validation_batch_capped_at_five(self):
        self.assertEqual(rollout.MAX_BATCH_SIZE, 5)
        for start in range(0, 27, rollout.MAX_BATCH_SIZE):
            self.assertLessEqual(len(rollout.CANDIDATE_ENTRIES[start:start + rollout.MAX_BATCH_SIZE]), 5)

    def test_13_two_page_limit_enforced(self):
        self.assertEqual((rollout.MAX_PAGES, static_html.MAX_PAGES, embedded_data.MAX_PAGES), (2, 2, 2))
        self.assertTrue(all(item["max_pages"] <= 2 for item in rollout.CANDIDATE_ENTRIES))

    def test_14_twenty_job_limit_enforced(self):
        self.assertEqual((rollout.MAX_JOBS, static_html.MAX_RESULTS, embedded_data.MAX_RESULTS), (20, 20, 20))
        self.assertTrue(all(item["max_jobs"] <= 20 for item in rollout.CANDIDATE_ENTRIES))

    def test_15_script_and_byte_limits_enforced(self):
        self.assertEqual((rollout.MAX_SCRIPTS, rollout.MAX_RESPONSE_BYTES, rollout.MAX_SCRIPT_BYTES), (20, 3_000_000, 1_000_000))

    def test_16_no_persistence_or_dashboard_imports(self):
        tree = ast.parse(Path(rollout.__file__).read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        for forbidden in ("supabase", "gspread", "google_sheets", "sqlite3", "update_student_dashboard"):
            self.assertFalse(any(forbidden in module for module in imports), imports)

    def test_17_production_registry_unchanged_and_counted(self):
        self.assertEqual(len(entries(rollout.PRODUCTION)), 105)
        self.assertEqual(len(entries(rollout.CANDIDATE_REGISTRY)), 132)

    def test_18_inventory_and_registry_have_no_duplicates(self):
        names = [validator._name_key(item) for item in entries(rollout.CANDIDATE_REGISTRY)]
        sources = [validator.source_key(item) for item in entries(rollout.CANDIDATE_REGISTRY)]
        sources = [item for item in sources if item]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(sources), len(set(sources)))

    def test_19_inventory_order_and_report_schema(self):
        with rollout.CANDIDATE_INVENTORY.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["company"] for row in rows], rollout.EXPECTED_COMPANIES)
        report = __import__("json").loads(rollout.REPORT.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["reviewed_candidates"], 27)
        self.assertEqual([row["company"] for row in report["results"]], rollout.EXPECTED_COMPANIES)

    def test_20_only_declarative_contract_fields(self):
        forbidden = ("python", "xpath", "eval", "script_code", "executable")
        for entry in rollout.CANDIDATE_ENTRIES:
            self.assertFalse(set(entry) & set(forbidden))

    def test_21_registry_artifacts_are_distinct_from_production(self):
        self.assertNotEqual(rollout.CANDIDATE_REGISTRY, rollout.PRODUCTION)
        self.assertNotEqual(rollout.VALIDATED_REGISTRY, rollout.PRODUCTION)

    def test_22_unsupported_candidates_never_enter_validated_registry(self):
        validated = {item["name"] for item in additions(rollout.VALIDATED_REGISTRY)}
        unsupported = {item["name"] for item in rollout.CANDIDATE_ENTRIES if item.get("expected_validation_status")}
        self.assertFalse(validated & unsupported)


if __name__ == "__main__":
    unittest.main()
