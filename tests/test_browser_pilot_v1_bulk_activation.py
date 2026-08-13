from __future__ import annotations

import ast
import csv
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import validate_browser_pilot_v1 as pilot
from src.scraper.company_careers.parent_sources import load_parent_source_mappings


def entries(path: Path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def historical_prefix_bytes(path: Path) -> bytes:
    first_pilot_company = pilot.registry_validator._name_key({"name": pilot.REVIEWED_COMPANIES[0]})
    lines = path.read_bytes().splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.decode("utf-8").strip()
        if not stripped.startswith("- name:"):
            continue
        parsed = yaml.safe_load(stripped)
        if isinstance(parsed, list) and pilot.registry_validator._name_key(parsed[0]) == first_pilot_company:
            return b"".join(lines[:index])
    raise AssertionError(f"Pilot boundary {pilot.REVIEWED_COMPANIES[0]!r} not found in {path}")


def entries_from_bytes(content: bytes):
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


class BrowserPilotBulkActivationTests(unittest.TestCase):
    def test_01_exact_reviewed_set_and_rationale(self):
        with pilot.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["company"] for row in rows], pilot.REVIEWED_COMPANIES)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["selection_rationale"] for row in rows))
        self.assertTrue(all(row["selected"] == "true" for row in rows))

    def test_02_candidate_has_exact_historical_145_entry_prefix(self):
        candidate_prefix = historical_prefix_bytes(pilot.CANDIDATE_REGISTRY)
        validated_prefix = historical_prefix_bytes(pilot.VALIDATED_REGISTRY)
        historical = entries_from_bytes(candidate_prefix)
        candidate = entries(pilot.CANDIDATE_REGISTRY)
        validated = entries(pilot.VALIDATED_REGISTRY)

        self.assertEqual(candidate_prefix, validated_prefix)
        self.assertEqual(len(historical), pilot.HISTORICAL_PRODUCTION_COUNT)
        self.assertEqual(candidate[:len(historical)], historical)
        self.assertEqual(validated[:len(historical)], historical)
        self.assertEqual(len(candidate), 148)
        self.assertEqual([item["name"] for item in candidate[len(historical):]], pilot.REVIEWED_COMPANIES)

    def test_03_validated_registry_only_contains_two_pass_successes(self):
        report = yaml.safe_load(pilot.REPORT.read_text(encoding="utf-8"))
        safe = [item["company"] for item in report["results"] if item["safe_to_activate"]]
        additions = entries(pilot.VALIDATED_REGISTRY)[145:]
        self.assertEqual([item["name"] for item in additions], safe)
        for result in report["results"]:
            if result["safe_to_activate"]:
                self.assertEqual(len(result["runs"]), 2)
                self.assertEqual(result["runs"][0]["stable_ids"], result["runs"][1]["stable_ids"])
                self.assertEqual(result["runs"][0]["apply_urls"], result["runs"][1]["apply_urls"])

    def test_04_duplicate_company_and_source_rejection(self):
        baseline = [{"name": "Existing", "platform": "browser_rendered_html", "listing_url": "https://jobs.example.test/a"}]
        names, sources = pilot._duplicates(
            baseline,
            [
                {"name": " existing ", "platform": "browser_rendered_html", "listing_url": "https://other.test/a"},
                {"name": "Other", "platform": "browser_rendered_html", "listing_url": "https://jobs.example.test/b"},
            ],
        )
        self.assertEqual(names, {0: "Existing"})
        self.assertEqual(sources, {1: "Existing"})

    def test_05_build_rejects_duplicate_company_without_touching_production(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = root / "production.yaml"
            destination = root / "candidate.yaml"
            production.write_text("target_companies:\n  - name: Apple\n    platform: greenhouse\n    slug: apple\n", encoding="utf-8")
            before = production.read_bytes()
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                pilot.build_candidate_registry(production, destination)
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())

    def test_06_production_and_parent_mappings_unchanged(self):
        report = yaml.safe_load(pilot.REPORT.read_text(encoding="utf-8"))
        historical_prefix = historical_prefix_bytes(pilot.VALIDATED_REGISTRY)
        validated = entries(pilot.VALIDATED_REGISTRY)
        production = entries(pilot.PRODUCTION)

        self.assertEqual(hashlib.sha256(historical_prefix).hexdigest().upper(), report["production_registry_sha256"])
        self.assertEqual(hashlib.sha256(pilot.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(), report["parent_mappings_sha256"])
        self.assertGreaterEqual(len(production), len(validated))
        assert_historical_prefix(self, production, validated)

        production_sources = [pilot.source_key(item) for item in production]
        for company in pilot.REVIEWED_COMPANIES:
            name_key = pilot.registry_validator._name_key({"name": company})
            expected = [item for item in validated if pilot.registry_validator._name_key(item) == name_key]
            observed = [item for item in production if pilot.registry_validator._name_key(item) == name_key]
            self.assertEqual(len(expected), 1)
            self.assertEqual(observed, expected)
            self.assertEqual(production_sources.count(pilot.source_key(expected[0])), 1)
        self.assertEqual(len(load_parent_source_mappings(pilot.PARENT_MAPPINGS)), 4)

    def test_07_no_persistence_or_dashboard_publication_imports(self):
        tree = ast.parse(Path(pilot.__file__).read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        forbidden = ("supabase", "google_sheets", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in imports))
        report = yaml.safe_load(pilot.REPORT.read_text(encoding="utf-8"))
        self.assertTrue(all(value is False for value in report["publication"].values()))

    def test_08_dependency_is_exactly_pinned_and_no_browser_binaries(self):
        requirements = (pilot.ROOT / "requirements.txt").read_text(encoding="utf-8")
        scraper_requirements = (pilot.ROOT / "scraper_requirements.txt").read_text(encoding="utf-8")
        pyproject = (pilot.ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("playwright==1.57.0", requirements)
        self.assertIn("playwright==1.57.0", scraper_requirements)
        self.assertIn('"playwright==1.57.0"', pyproject)
        tracked_names = subprocess.run(
            ["git", "ls-files"], cwd=pilot.ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines()
        tracked = [
            name for name in tracked_names
            if any(part in {"chromium", "chrome-win", "ms-playwright"} for part in Path(name).parts)
        ]
        self.assertEqual(tracked, [])

    def test_09_candidate_contracts_are_declarative_and_bounded(self):
        for entry in pilot.CANDIDATE_ENTRIES:
            self.assertEqual(entry["platform"], "browser_rendered_html")
            self.assertTrue(entry["browser_wait_selector"])
            self.assertLessEqual(entry["browser_scroll_steps"], 3)
            self.assertLessEqual(entry["max_pages"], 2)
            self.assertLessEqual(entry["max_jobs"], 20)
            self.assertLessEqual(entry["max_response_bytes"], 3_000_000)

    def test_10_projected_coverage_uses_only_validated_sources(self):
        report = yaml.safe_load(pilot.REPORT.read_text(encoding="utf-8"))
        self.assertEqual(report["totals"]["projected_original_coverage"], 140 + report["totals"]["validated"])
        self.assertEqual(report["totals"]["production_sources"], 145)
        self.assertEqual(report["totals"]["parent_mappings"], 4)


if __name__ == "__main__":
    unittest.main()
