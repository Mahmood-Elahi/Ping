from __future__ import annotations

import ast
import csv
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

REVIEWED_CANDIDATES = [
    "AMD",
    "TSMC",
    "ARM",
    "Synopsys",
    "Lattice Semiconductor",
    "GitHub",
    "Intuit",
    "Two Sigma",
    "SIG",
    "Bank of America",
    "Siemens",
    "Rivian",
    "Lenovo",
    "bp",
    "Wayfair",
]
VALIDATED_SAFE_CANDIDATES = [
    "AMD",
    "TSMC",
    "ARM",
    "Synopsys",
    "Lattice Semiconductor",
    "GitHub",
    "Intuit",
    "Two Sigma",
    "SIG",
    "Bank of America",
    "Rivian",
    "Lenovo",
]
EXCLUDED_CANDIDATES = {"Siemens", "bp", "Wayfair"}
EXPECTED_PLATFORMS = {
    "AMD": "icims",
    "TSMC": "avature",
    "ARM": "icims",
    "Synopsys": "avature",
    "Lattice Semiconductor": "icims",
    "GitHub": "icims",
    "Intuit": "avature",
    "Two Sigma": "avature",
    "SIG": "icims",
    "Bank of America": "avature",
    "Rivian": "icims",
    "Lenovo": "avature",
}


def entries(path: Path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def historical_baseline_bytes(path: Path) -> bytes:
    """Return the artifact prefix before its first reviewed milestone candidate."""
    content = path.read_bytes()
    artifact_entries = entries(path)
    reviewed_names = {" ".join(name.casefold().split()) for name in REVIEWED_CANDIDATES}
    first_candidate = next(
        index
        for index, item in enumerate(artifact_entries)
        if validator._name_key(item) in reviewed_names
    )
    entry_offsets = []
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.startswith(b"  - name: "):
            entry_offsets.append(offset)
        offset += len(line)
    if len(entry_offsets) != len(artifact_entries):
        raise ValueError(f"Could not locate every YAML entry boundary in {path}")
    return content[: entry_offsets[first_candidate]]


def historical_baseline_entries():
    data = yaml.safe_load(historical_baseline_bytes(CANDIDATES).decode("utf-8")) or {}
    return data["target_companies"]


def additions(path: Path):
    baseline_names = {validator._name_key(item) for item in historical_baseline_entries()}
    return [item for item in entries(path) if validator._name_key(item) not in baseline_names]


class AvatureICIMSBulkActivationTests(unittest.TestCase):
    def test_33_exact_candidate_company_set_derived_from_research(self):
        self.assertEqual(rollout._derived_research_candidates(), rollout.EXPECTED_COMPANIES)
        self.assertEqual(len(rollout.EXPECTED_COMPANIES), 15)

    def test_34_historical_candidate_inventory_was_uncovered_and_is_exact(self):
        baseline_names = {validator._name_key(item) for item in historical_baseline_entries()}
        production = {validator._name_key(item) for item in entries(PRODUCTION)}
        mappings = yaml.safe_load(rollout.PARENT_MAPPINGS.read_text(encoding="utf-8"))
        mapped = {
            " ".join(item["child_company"].casefold().split())
            for item in mappings["company_parent_source_mappings"]
            if item["enabled"] is True
        }
        candidate_names = [item["name"] for item in rollout.CANDIDATE_ENTRIES]
        candidate_keys = [validator._name_key(item) for item in rollout.CANDIDATE_ENTRIES]
        with INVENTORY.open(newline="", encoding="utf-8") as handle:
            inventory_names = [row["company"] for row in csv.DictReader(handle)]

        self.assertEqual(candidate_names, REVIEWED_CANDIDATES)
        self.assertEqual(inventory_names, REVIEWED_CANDIDATES)
        self.assertEqual(len(candidate_keys), len(set(candidate_keys)))
        self.assertFalse(set(candidate_keys) & baseline_names)
        self.assertFalse(set(candidate_keys) & mapped)
        self.assertTrue(
            {" ".join(name.casefold().split()) for name in VALIDATED_SAFE_CANDIDATES}
            <= production
        )
        self.assertFalse(
            {" ".join(name.casefold().split()) for name in EXCLUDED_CANDIDATES}
            & production
        )

    def test_35_historical_registry_prefixes_and_promoted_registry_order(self):
        candidate_baseline = historical_baseline_bytes(CANDIDATES)
        validated_baseline = historical_baseline_bytes(VALIDATED)

        self.assertTrue(CANDIDATES.read_bytes().startswith(candidate_baseline))
        self.assertTrue(VALIDATED.read_bytes().startswith(validated_baseline))
        self.assertEqual(candidate_baseline, validated_baseline)
        self.assertEqual(len(historical_baseline_entries()), 93)
        self.assertTrue(PRODUCTION.read_bytes().startswith(VALIDATED.read_bytes()))

    def test_36_master_workbook_candidate_order(self):
        ranks = [item["original_workbook_rank"] for item in rollout.CANDIDATE_ENTRIES]
        candidate_additions = additions(CANDIDATES)

        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(candidate_additions), 15)
        self.assertEqual([item["name"] for item in candidate_additions], REVIEWED_CANDIDATES)
        self.assertEqual([item["name"] for item in candidate_additions], rollout.EXPECTED_COMPANIES)
        self.assertTrue(EXCLUDED_CANDIDATES <= {item["name"] for item in candidate_additions})

    def test_37_candidate_loader_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "historical_baseline.yaml"
            baseline_path.write_bytes(historical_baseline_bytes(CANDIDATES))
            baseline_count = len(load_target_companies(str(baseline_path)))

        self.assertEqual(baseline_count, 93)
        self.assertEqual(len(load_target_companies(str(CANDIDATES))), baseline_count + 15)
        self.assertEqual(len(load_target_companies(str(VALIDATED))), baseline_count + 12)
        self.assertTrue(load_target_companies(str(PRODUCTION)))

    def test_38_validated_registry_contains_only_safe_candidates(self):
        with INVENTORY.open(newline="", encoding="utf-8") as handle:
            safe = [row["company"] for row in csv.DictReader(handle) if row["safe_to_activate"] == "true"]
        candidate_additions = additions(CANDIDATES)
        validated_additions = additions(VALIDATED)
        candidate_by_name = {validator._name_key(item): item for item in candidate_additions}
        production_entries = entries(PRODUCTION)
        production_names = [validator._name_key(item) for item in production_entries]

        self.assertEqual(safe, VALIDATED_SAFE_CANDIDATES)
        self.assertEqual(len(validated_additions), 12)
        self.assertEqual(
            [item["name"] for item in validated_additions], VALIDATED_SAFE_CANDIDATES
        )
        self.assertTrue(
            {validator._name_key(item) for item in validated_additions}
            <= set(candidate_by_name)
        )
        for item in validated_additions:
            expected = candidate_by_name[validator._name_key(item)]
            self.assertEqual(item["platform"], expected["platform"])
            self.assertIsNotNone(validator.source_key(item))
            self.assertEqual(validator.source_key(item), validator.source_key(expected))
            self.assertEqual(production_names.count(validator._name_key(item)), 1)
        self.assertFalse(EXCLUDED_CANDIDATES & set(safe))
        self.assertFalse(
            {" ".join(name.casefold().split()) for name in EXCLUDED_CANDIDATES}
            & set(production_names)
        )

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

    def test_50_promoted_production_registry_integrity(self):
        production_entries = entries(PRODUCTION)
        production_names = [validator._name_key(item) for item in production_entries]
        production_by_name = {
            validator._name_key(item): item for item in production_entries
        }
        validated_additions = additions(VALIDATED)
        validated_by_name = {
            validator._name_key(item): item for item in validated_additions
        }

        for name, platform in EXPECTED_PLATFORMS.items():
            key = " ".join(name.casefold().split())
            self.assertEqual(production_names.count(key), 1)
            self.assertEqual(production_by_name[key]["platform"], platform)
            self.assertIsNotNone(validator.source_key(production_by_name[key]))
            self.assertEqual(
                validator.source_key(production_by_name[key]),
                validator.source_key(validated_by_name[key]),
            )
        self.assertFalse(
            {" ".join(name.casefold().split()) for name in EXCLUDED_CANDIDATES}
            & set(production_names)
        )
        self.assertEqual(len(production_names), len(set(production_names)))
        source_keys = [validator.source_key(item) for item in production_entries]
        source_keys = [key for key in source_keys if key]
        self.assertEqual(len(source_keys), len(set(source_keys)))
        self.assertTrue(load_target_companies(str(PRODUCTION)))
        self.assertTrue(PRODUCTION.read_bytes().startswith(VALIDATED.read_bytes()))

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
