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
BATCH_B_VALIDATED_REGISTRY = ROOT / "config" / "target_companies.adapter_extensions_v4b_validated.yaml"
PILOT_VALIDATED_REGISTRY = ROOT / "config" / "target_companies.browser_pilot_v1_validated.yaml"
BROWSER_EXPANSION_V1A_VALIDATED_REGISTRY = ROOT / "config" / "target_companies.browser_expansion_v1a_validated.yaml"
BROWSER_EXPANSION_V1B_VALIDATED_REGISTRY = ROOT / "config" / "target_companies.browser_expansion_v1b_validated.yaml"
LATER_PROMOTION_ARTIFACTS = {
    BATCH_B_VALIDATED_REGISTRY: {"IMC Trading", "Unitree Robotics"},
    PILOT_VALIDATED_REGISTRY: {"Figure AI", "LINE"},
    BROWSER_EXPANSION_V1A_VALIDATED_REGISTRY: {
        "ASML",
        "Adyen",
        "Aurora Innovation",
        "Dassault Systèmes",
    },
    BROWSER_EXPANSION_V1B_VALIDATED_REGISTRY: {"Tourmaline Oil", "Patreon"},
}

EXPECTED_REVIEWED_COMPANIES = [
    "Waymo",
    "ASML",
    "Applied Materials",
    "Adyen",
    "Ripple",
    "DRW",
    "IMC Trading",
    "Yaskawa Electric",
    "General Motors",
    "Aptiv",
    "Mobileye",
    "Aurora Innovation",
    "Dassault Systèmes",
    "Skydio",
    "Figure AI",
    "Unitree Robotics",
    "Caterpillar",
    "Intuitive Surgical",
    "Tourmaline Oil",
    "Coupang",
    "Etsy",
    "Zalando",
    "Flexport",
    "LinkedIn",
    "LINE",
    "Patreon",
    "Bluesky",
]
EXPECTED_SAFE_COMPANIES = [
    "Waymo",
    "Applied Materials",
    "Ripple",
    "DRW",
    "General Motors",
    "Mobileye",
    "Skydio",
    "Caterpillar",
    "Intuitive Surgical",
    "Coupang",
    "Etsy",
    "Zalando",
    "Flexport",
    "Bluesky",
]
EXPECTED_EXCLUDED_COMPANIES = [
    "ASML",
    "Adyen",
    "IMC Trading",
    "Yaskawa Electric",
    "Aptiv",
    "Aurora Innovation",
    "Dassault Systèmes",
    "Figure AI",
    "Unitree Robotics",
    "Tourmaline Oil",
    "LinkedIn",
    "LINE",
    "Patreon",
]
EXPECTED_SAFE_PLATFORMS = {
    name: "static_html" for name in EXPECTED_SAFE_COMPANIES
}
EXPECTED_SAFE_PLATFORMS.update({"DRW": "javascript_embedded_data", "Bluesky": "javascript_embedded_data"})


def entries(path: Path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def normalized_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def historical_prefix_bytes(path: Path) -> bytes:
    first_reviewed = validator._name_key({"name": EXPECTED_REVIEWED_COMPANIES[0]})
    lines = path.read_bytes().splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.decode("utf-8").strip()
        if not stripped.startswith("- name:"):
            continue
        parsed = yaml.safe_load(stripped)
        if isinstance(parsed, list) and validator._name_key(parsed[0]) == first_reviewed:
            return b"".join(lines[:index])
    raise AssertionError(f"Historical boundary {EXPECTED_REVIEWED_COMPANIES[0]!r} not found in {path}")


def historical_entries():
    data = yaml.safe_load(historical_prefix_bytes(rollout.CANDIDATE_REGISTRY).decode("utf-8")) or {}
    return data["target_companies"]


def additions(path: Path):
    baseline = {validator._name_key(item) for item in historical_entries()}
    return [item for item in entries(path) if validator._name_key(item) not in baseline]


class StaticEmbeddedBulkActivationTests(unittest.TestCase):
    def assert_exclusions_respect_proven_later_activations(self, production):
        production_by_name = {validator._name_key(item): item for item in production}
        allowed_by_name = {}
        for artifact, companies in LATER_PROMOTION_ARTIFACTS.items():
            artifact_entries = entries(artifact)
            for company in companies:
                name_key = validator._name_key({"name": company})
                matches = [item for item in artifact_entries if validator._name_key(item) == name_key]
                self.assertEqual(len(matches), 1)
                allowed_by_name[name_key] = matches[0]
        excluded = {
            validator._name_key({"name": name}) for name in EXPECTED_EXCLUDED_COMPANIES
        }

        self.assertEqual(excluded & set(production_by_name), set(allowed_by_name))
        for name_key, proven_entry in allowed_by_name.items():
            self.assertEqual(
                sum(validator._name_key(item) == name_key for item in production),
                1,
            )
            self.assertEqual(production_by_name[name_key], proven_entry)
            self.assertEqual(
                validator.source_key(production_by_name[name_key]),
                validator.source_key(proven_entry),
            )
        self.assertNotIn("niantic", production_by_name)

    def test_01_exact_candidate_set_derived_from_research(self):
        self.assertEqual(rollout.EXPECTED_COMPANIES, EXPECTED_REVIEWED_COMPANIES)
        self.assertEqual(rollout._derived_research_candidates(), rollout.EXPECTED_COMPANIES)
        self.assertEqual((len(rollout.STATIC_COMPANIES), len(rollout.EMBEDDED_COMPANIES)), (18, 9))

    def test_02_reviewed_candidates_were_absent_from_historical_inventory(self):
        historical = {validator._name_key(item) for item in historical_entries()}
        mappings = yaml.safe_load(rollout.PARENT_MAPPINGS.read_text(encoding="utf-8"))
        mapped = {" ".join(item["child_company"].casefold().split()) for item in mappings["company_parent_source_mappings"] if item["enabled"] is True}
        candidate_names = [validator._name_key(item) for item in rollout.CANDIDATE_ENTRIES]
        candidate_set = set(candidate_names)
        with rollout.CANDIDATE_INVENTORY.open(newline="", encoding="utf-8") as handle:
            inventory = [row["company"] for row in csv.DictReader(handle)]
        production = [validator._name_key(item) for item in entries(rollout.PRODUCTION)]

        self.assertFalse(candidate_set & historical)
        self.assertFalse(candidate_set & mapped)
        self.assertEqual(len(candidate_names), len(candidate_set))
        self.assertEqual(inventory, EXPECTED_REVIEWED_COMPANIES)
        self.assertTrue({validator._name_key({"name": name}) for name in EXPECTED_SAFE_COMPANIES} <= set(production))
        self.assert_exclusions_respect_proven_later_activations(entries(rollout.PRODUCTION))

    def test_03_candidate_order_follows_master_workbook(self):
        candidate_additions = additions(rollout.CANDIDATE_REGISTRY)
        addition_names = [item["name"] for item in candidate_additions]
        ranks = [item["original_workbook_rank"] for item in rollout.CANDIDATE_ENTRIES]
        workbook_ranks = rollout._master_ranks()
        workbook_order = sorted(EXPECTED_REVIEWED_COMPANIES, key=workbook_ranks.__getitem__)

        self.assertEqual(len(candidate_additions), 27)
        self.assertEqual(addition_names, EXPECTED_REVIEWED_COMPANIES)
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(addition_names, workbook_order)
        self.assertEqual(set(addition_names), set(EXPECTED_SAFE_COMPANIES) | set(EXPECTED_EXCLUDED_COMPANIES))

    def test_04_historical_prefix_and_promotion_bytes_are_preserved(self):
        candidate = normalized_newlines(rollout.CANDIDATE_REGISTRY.read_bytes())
        validated = normalized_newlines(rollout.VALIDATED_REGISTRY.read_bytes())
        production = normalized_newlines(rollout.PRODUCTION.read_bytes())
        candidate_prefix = normalized_newlines(historical_prefix_bytes(rollout.CANDIDATE_REGISTRY))
        validated_prefix = normalized_newlines(historical_prefix_bytes(rollout.VALIDATED_REGISTRY))

        self.assertEqual(candidate_prefix, validated_prefix)
        self.assertTrue(candidate.startswith(candidate_prefix))
        self.assertTrue(validated.startswith(validated_prefix))
        self.assertEqual(len(yaml.safe_load(candidate_prefix.decode("utf-8"))["target_companies"]), 105)
        self.assertTrue(production == validated or production.startswith(validated))

    def test_05_candidate_loader_compatibility(self):
        self.assertEqual(len(load_target_companies(str(rollout.CANDIDATE_REGISTRY))), 132)

    def test_06_validated_registry_contains_only_safe_candidates(self):
        with rollout.CANDIDATE_INVENTORY.open(newline="", encoding="utf-8") as handle:
            safe = [row["company"] for row in csv.DictReader(handle) if row["safe_to_activate"] == "true"]
        validated_additions = additions(rollout.VALIDATED_REGISTRY)
        validated_names = [item["name"] for item in validated_additions]
        reviewed_names = {item["name"] for item in rollout.CANDIDATE_ENTRIES}
        production = entries(rollout.PRODUCTION)
        production_by_name = {validator._name_key(item): item for item in production}

        self.assertEqual(safe, EXPECTED_SAFE_COMPANIES)
        self.assertEqual(len(validated_additions), 14)
        self.assertEqual(validated_names, EXPECTED_SAFE_COMPANIES)
        self.assertTrue(set(validated_names) <= reviewed_names)
        self.assertFalse(set(validated_names) & set(EXPECTED_EXCLUDED_COMPANIES))
        self.assertEqual(len(load_target_companies(str(rollout.VALIDATED_REGISTRY))), 119)
        for validated_entry in validated_additions:
            name = validated_entry["name"]
            name_key = validator._name_key(validated_entry)
            self.assertEqual(sum(validator._name_key(item) == name_key for item in production), 1)
            self.assertEqual(validated_entry["platform"], EXPECTED_SAFE_PLATFORMS[name])
            self.assertEqual(production_by_name[name_key]["platform"], validated_entry["platform"])
            self.assertEqual(validator.source_key(production_by_name[name_key]), validator.source_key(validated_entry))
        self.assert_exclusions_respect_proven_later_activations(production)

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

    def test_17_promoted_static_embedded_sources_preserve_production_integrity(self):
        production = entries(rollout.PRODUCTION)
        validated_additions = additions(rollout.VALIDATED_REGISTRY)
        validated_by_name = {validator._name_key(item): item for item in validated_additions}
        production_names = [validator._name_key(item) for item in production]
        production_by_name = {validator._name_key(item): item for item in production}
        production_sources = [validator.source_key(item) for item in production]
        production_sources = [item for item in production_sources if item]

        self.assertEqual(len(load_target_companies(str(rollout.PRODUCTION))), len(production))
        self.assertEqual(len(production_names), len(set(production_names)))
        self.assertEqual(len(production_sources), len(set(production_sources)))
        for name in EXPECTED_SAFE_COMPANIES:
            name_key = validator._name_key({"name": name})
            self.assertEqual(production_names.count(name_key), 1)
            self.assertEqual(production_by_name[name_key]["platform"], EXPECTED_SAFE_PLATFORMS[name])
            self.assertEqual(validator.source_key(production_by_name[name_key]), validator.source_key(validated_by_name[name_key]))
        self.assert_exclusions_respect_proven_later_activations(production)
        production_bytes = normalized_newlines(rollout.PRODUCTION.read_bytes())
        validated_bytes = normalized_newlines(rollout.VALIDATED_REGISTRY.read_bytes())
        self.assertTrue(production_bytes == validated_bytes or production_bytes.startswith(validated_bytes))

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
