from __future__ import annotations

import ast
import csv
import hashlib
import inspect
import json
import subprocess
import tempfile
import unittest

from tests.company_careers_activation_milestones import (
    historical_parent_mapping_sha256,
)
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tests.company_careers_reliability_assertions import (
    assert_historical_prefix,
    assert_historical_registry_bytes,
    with_reliability_repairs,
)

from scripts import validate_browser_expansion_v1b as batch
from src.scraper.company_careers.adapters import browser_rendered_html
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.parent_sources import load_parent_source_mappings


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def normalized_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def historical_prefix_bytes(path: Path) -> bytes:
    first_candidate = batch.CANDIDATE_ENTRIES[0]["name"]
    lines = path.read_bytes().splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.decode("utf-8").strip()
        if not stripped.startswith("- name:"):
            continue
        parsed = yaml.safe_load(stripped)
        if isinstance(parsed, list) and parsed[0].get("name") == first_candidate:
            return b"".join(lines[:index])
    raise AssertionError(f"Historical boundary {first_candidate!r} not found in {path}")


def job(job_id: str = "1", url: str = "https://jobs.example.test/job/1") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class BrowserExpansionV1BArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.historical_bytes = historical_prefix_bytes(batch.CANDIDATE_REGISTRY)
        cls.historical = (yaml.safe_load(cls.historical_bytes.decode("utf-8")) or {})[
            "target_companies"
        ]
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_exact_156_entry_production_byte_prefix(self):
        validated_historical_bytes = historical_prefix_bytes(batch.VALIDATED_REGISTRY)
        self.assertEqual(len(self.historical), 156)
        self.assertEqual(validated_historical_bytes, self.historical_bytes)
        historical_bytes = normalized_newlines(self.historical_bytes)
        self.assertTrue(normalized_newlines(self.candidate_bytes).startswith(historical_bytes))
        self.assertTrue(normalized_newlines(self.validated_bytes).startswith(historical_bytes))
        assert_historical_registry_bytes(self, self.production_bytes, self.historical_bytes)
        self.assertEqual(self.candidates[:156], self.historical)
        self.assertEqual(self.validated[:156], self.historical)
        assert_historical_prefix(self, self.production, self.historical)

    def test_02_exact_seven_company_inventory_in_master_order(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        ranks = [int(row["master_rank"]) for row in self.inventory]
        self.assertEqual(ranks, sorted(ranks))

    def test_03_candidate_registry_is_append_only(self):
        self.assertEqual(len(self.candidates), 156 + len(batch.CANDIDATE_ENTRIES))
        self.assertEqual(
            [entry["name"] for entry in self.candidates[156:]],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )

    def test_04_validated_registry_is_append_only_and_success_only(self):
        safe = [result["company"] for result in self.report["results"] if result["safe_to_activate"]]
        self.assertEqual([entry["name"] for entry in self.validated[156:]], safe)
        self.assertEqual(len(self.validated), 156 + len(safe))

    def test_05_every_success_has_two_deterministic_fresh_runs(self):
        for result in self.report["results"]:
            self.assertEqual(len(result["runs"]), 2)
            if not result["safe_to_activate"]:
                continue
            first, second = result["runs"]
            self.assertEqual(first["stable_ids"], second["stable_ids"])
            self.assertEqual(first["apply_urls"], second["apply_urls"])
            self.assertEqual(first["jobs_normalized"], second["jobs_normalized"])
            self.assertLessEqual(first["jobs_normalized"], 20)
        limits = self.report["limits"]
        self.assertEqual(limits["fresh_http_sessions_per_direct_company"], 2)
        self.assertEqual(limits["fresh_contexts_per_browser_company"], 2)

    def test_06_excluded_sources_never_enter_validated_output(self):
        excluded = {result["company"] for result in self.report["results"] if not result["safe_to_activate"]}
        validated_names = {entry["name"] for entry in self.validated[156:]}
        self.assertEqual(excluded, {"Astrobotic", "Intuitive Machines", "Walmart Global Tech"})
        self.assertTrue(excluded.isdisjoint(validated_names))

    def test_07_final_platform_classifications(self):
        expected = {
            "Tourmaline Oil": "javascript_embedded_data",
            "Astrobotic": "none",
            "MDA Space": "browser_rendered_html",
            "Intuitive Machines": "none",
            "Walmart Global Tech": "none",
            "JD.com": "workday",
            "Patreon": "ashby",
        }
        self.assertEqual(
            {result["company"]: result["platform"] for result in self.report["results"]},
            expected,
        )

    def test_08_direct_sources_are_preferred_when_supported(self):
        by_name = {entry["name"]: entry for entry in batch.CANDIDATE_ENTRIES}
        self.assertEqual(by_name["Tourmaline Oil"]["platform"], "javascript_embedded_data")
        self.assertEqual(by_name["JD.com"]["platform"], "workday")
        self.assertEqual(by_name["Patreon"]["platform"], "ashby")
        self.assertEqual(
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES if entry["platform"] == "browser_rendered_html"],
            ["MDA Space"],
        )

    def test_09_production_and_parent_mappings_are_unchanged(self):
        self.assertEqual(
            hashlib.sha256(self.historical_bytes).hexdigest().upper(),
            self.report["production_registry_sha256"],
        )
        self.assertEqual(
            historical_parent_mapping_sha256(batch.PARENT_MAPPINGS),
            self.report["parent_mappings_sha256"],
        )
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 5)
        expected_promotions = ["Tourmaline Oil", "MDA Space", "JD.com", "Patreon"]
        validated_promotions = self.validated[156:]
        self.assertEqual([entry["name"] for entry in validated_promotions], expected_promotions)
        repaired_promotions = with_reliability_repairs(validated_promotions)
        self.assertEqual(self.production[156:160], repaired_promotions)
        for validated_entry in validated_promotions:
            matches = [entry for entry in self.production if entry["name"] == validated_entry["name"]]
            self.assertEqual(matches, with_reliability_repairs([validated_entry]))
        production_names = [entry["name"] for entry in self.production]
        self.assertNotIn("Walmart Global Tech", production_names)
        for name in ("Astrobotic", "Intuitive Machines"):
            self.assertEqual(production_names.count(name), 1)

    def test_10_projection_uses_only_validated_sources(self):
        totals = self.report["totals"]
        self.assertEqual(totals["production_sources"], 156)
        self.assertEqual(totals["projected_production_sources"], 156 + totals["validated"])
        self.assertEqual(totals["projected_original_company_coverage"], 151 + totals["validated"])
        self.assertEqual((totals["validated"], totals["excluded"]), (4, 3))

    def test_11_registry_sources_are_unique(self):
        keys = [batch.source_key(entry) for entry in self.candidates]
        present = [key for key in keys if key]
        self.assertEqual(len(present), len(set(present)))

    def test_12_report_is_ignored_and_publication_is_disabled(self):
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_13_no_persistence_or_dashboard_initialization(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        forbidden = (
            "supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan"
        )
        self.assertFalse(any(any(token in module for token in forbidden) for module in imports))

    def test_14_browser_candidate_is_declarative_and_bounded(self):
        browsers = [
            entry for entry in batch.CANDIDATE_ENTRIES if entry["platform"] == "browser_rendered_html"
        ]
        self.assertEqual(len(browsers), 1)
        entry = browsers[0]
        self.assertTrue(entry["browser_wait_selector"])
        self.assertLessEqual(entry["browser_scroll_steps"], 3)
        self.assertLessEqual(entry["max_pages"], 2)
        self.assertLessEqual(entry["max_jobs"], 20)
        self.assertLessEqual(entry["max_response_bytes"], 3_000_000)
        collector_source = inspect.getsource(browser_rendered_html)
        self.assertFalse(any(name.casefold() in collector_source.casefold() for name in batch.REVIEWED_COMPANIES))


class BrowserExpansionV1BContractTests(unittest.TestCase):
    def test_15_duplicate_company_and_source_rejection(self):
        baseline = [{"name": "Existing", "platform": "greenhouse", "slug": "shared"}]
        names, sources = batch._duplicates(
            baseline,
            [
                {"name": " existing ", "platform": "greenhouse", "slug": "other"},
                {"name": "Other", "platform": "greenhouse", "slug": "SHARED"},
            ],
        )
        self.assertEqual(names, {0: "Existing"})
        self.assertEqual(sources, {1: "Existing"})

    def test_16_build_rejects_duplicates_without_touching_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = root / "production.yaml"
            destination = root / "candidate.yaml"
            production.write_text(
                "target_companies:\n  - name: Existing\n    platform: greenhouse\n    slug: shared\n",
                encoding="utf-8",
            )
            before = production.read_bytes()
            duplicate = [{"name": "Existing", "platform": "greenhouse", "slug": "other"}]
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(production, destination, duplicate)
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())

    def test_17_stable_id_and_official_apply_host_enforcement(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(batch._verify_jobs([job()], entry), [("1", "https://jobs.example.test/job/1")])
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([job(), job(url="https://jobs.example.test/job/2")], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([job(url="https://evil.test/job/1")], entry)
        with self.assertRaisesRegex(AdapterError, "job-specific"):
            batch._verify_jobs([job("1"), job("2")], entry)

    def test_18_repeated_validation_must_be_deterministic(self):
        entry = {"name": "Patreon", "original_workbook_rank": 243}
        run = {"success": True, "stable_ids": ["1"], "apply_urls": ["https://jobs.test/1"]}
        stable = batch._finalize_result(entry, [dict(run), dict(run)])
        changed = batch._finalize_result(
            entry,
            [dict(run), {"success": True, "stable_ids": ["2"], "apply_urls": ["https://jobs.test/2"]}],
        )
        self.assertTrue(stable["safe_to_activate"])
        self.assertEqual(changed["validation_status"], "non_deterministic")
        self.assertFalse(changed["safe_to_activate"])

    def test_19_non_browser_validation_does_not_initialize_playwright(self):
        entry = {
            "name": "Example", "platform": "ashby", "slug": "example",
            "careers_url": "https://example.test/careers", "source_identity_regex": "Example",
            "allowed_apply_hosts": ["jobs.example.test"],
        }
        fake_adapter = lambda _session, _target, _timeout: [job()]
        with (
            patch.object(batch, "_identity_check", return_value={"status": 200}),
            patch.dict(batch.scanner.ADAPTERS, {"ashby": fake_adapter}),
            patch.object(browser_rendered_html, "_load_sync_playwright") as loader,
        ):
            result = batch._run_direct(entry, 30)
        self.assertTrue(result["success"])
        loader.assert_not_called()

    def test_20_fail_closed_status_mapping(self):
        cases = {
            "authentication required": "authentication_required",
            "session required": "session_required",
            "captcha blocked": "blocked",
            "consent required": "blocked",
            "malformed html": "malformed_or_limit_failure",
            "company identity ambiguous": "mixed_or_ambiguous_identity",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(batch._status_for_error(AdapterError(message)), expected)

    def test_21_one_browser_process_fresh_context_and_lazy_import_contract(self):
        limits = self.report_limits()
        self.assertEqual(limits["browser_processes"], 1)
        self.assertEqual(limits["fresh_contexts_per_browser_company"], 2)
        fetch_source = inspect.getsource(browser_rendered_html.BrowserBatch.fetch_jobs)
        self.assertIn("self._browser.new_context()", fetch_source)
        self.assertIn("context.new_page()", fetch_source)
        self.assertIn("context.close()", fetch_source)
        loader_source = inspect.getsource(browser_rendered_html._load_sync_playwright)
        module_prefix = inspect.getsource(browser_rendered_html).split("def _load_sync_playwright", 1)[0]
        self.assertIn('import_module("playwright.sync_api")', loader_source)
        self.assertNotIn("playwright.sync_api", module_prefix)

    @staticmethod
    def report_limits() -> dict:
        return json.loads(batch.REPORT.read_text(encoding="utf-8"))["limits"]


if __name__ == "__main__":
    unittest.main()
