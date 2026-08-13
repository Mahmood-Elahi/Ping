from __future__ import annotations

import ast
import csv
import hashlib
import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import validate_source_expansion_v2_batch_a as batch
from src.scraper.company_careers.adapters import browser_rendered_html
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PROMOTED = [
    "Infineon Technologies",
    "MediaTek",
    "GlobalFoundries",
    "Beckhoff Automation",
    "Lucid Motors",
    "Sanctuary AI",
    "Instacart",
    "Faire",
    "Deliveroo",
    "ByteDance",
    "Substack",
]
EXCLUDED = {"Varjo", "Clearpath Robotics", "OTTO Motors", "Vimeo"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def milestone_prefix_bytes(path: Path) -> bytes:
    """Derive the historical registry prefix from this milestone's artifact."""
    content = path.read_bytes()
    artifact_entries = entries(path)
    promoted = {" ".join(name.casefold().split()) for name in PROMOTED}
    first_promotion = next(
        index
        for index, entry in enumerate(artifact_entries)
        if " ".join(entry["name"].casefold().split()) in promoted
    )
    entry_offsets = []
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.startswith(b"  - name: "):
            entry_offsets.append(offset)
        offset += len(line)
    if len(entry_offsets) != len(artifact_entries):
        raise ValueError(f"Could not locate every YAML entry boundary in {path}")
    return content[:entry_offsets[first_promotion]]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


def job(job_id: str = "1", url: str = "https://jobs.example.test/job/1") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class SourceExpansionV2BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.candidate_prefix_bytes = milestone_prefix_bytes(batch.CANDIDATE_REGISTRY)
        cls.validated_prefix_bytes = milestone_prefix_bytes(batch.VALIDATED_REGISTRY)
        cls.historical = entries_from_bytes(cls.candidate_prefix_bytes)
        cls.historical_count = len(cls.historical)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_exact_160_entry_production_prefix_is_preserved(self):
        self.assertEqual(self.historical_count, 160)
        self.assertEqual(self.candidate_prefix_bytes, self.validated_prefix_bytes)
        self.assertTrue(self.candidate_bytes.startswith(self.candidate_prefix_bytes))
        self.assertTrue(self.validated_bytes.startswith(self.validated_prefix_bytes))
        self.assertTrue(self.report["candidate_registry_has_exact_160_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_160_entry_prefix"])
        self.assertEqual(self.candidates[:self.historical_count], self.historical)
        self.assertEqual(self.validated[:self.historical_count], self.historical)
        assert_historical_prefix(self, self.production, self.historical)

    def test_02_selection_is_historical_uncovered_master_order(self):
        with tempfile.TemporaryDirectory() as directory:
            historical = Path(directory) / "historical.yaml"
            historical.write_bytes(self.candidate_prefix_bytes)
            master = batch.load_master_workbook(batch.MASTER_WORKBOOK)
            targets = load_target_companies(str(historical))
            mappings = load_parent_source_mappings(batch.PARENT_MAPPINGS)
            reconciliation = batch.reconcile_master(master, targets, mappings)
        uncovered, total = reconciliation.uncovered_names, reconciliation.total
        self.assertEqual((total, len(uncovered)), (246, 91))
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertTrue(set(batch.REVIEWED_COMPANIES).issubset(uncovered))
        ranks = [int(row["master_rank"]) for row in self.inventory]
        self.assertEqual(ranks, sorted(ranks))

    def test_03_candidate_registry_is_append_only(self):
        self.assertEqual(len(self.candidates), self.historical_count + len(batch.CANDIDATE_ENTRIES))
        self.assertEqual(
            [entry["name"] for entry in self.candidates[self.historical_count:]],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )

    def test_04_validated_registry_is_append_only_success_only(self):
        safe = [result["company"] for result in self.report["results"] if result["safe_to_activate"]]
        validated_promotions = self.validated[self.historical_count:]
        promotion_end = self.historical_count + len(validated_promotions)
        production_promotions = self.production[self.historical_count:promotion_end]
        self.assertEqual([entry["name"] for entry in validated_promotions], safe)
        self.assertEqual([entry["name"] for entry in validated_promotions], PROMOTED)
        self.assertEqual(production_promotions, validated_promotions)
        self.assertEqual(len(self.validated), self.historical_count + len(safe))
        self.assertGreaterEqual(len(self.production), promotion_end)
        production_names = [entry["name"] for entry in self.production]
        for name in PROMOTED:
            self.assertEqual(production_names.count(name), 1)
        self.assertEqual(len(safe), 11)

    def test_05_exclusions_never_enter_validated_registry(self):
        excluded = {result["company"] for result in self.report["results"] if not result["safe_to_activate"]}
        validated_names = {entry["name"] for entry in self.validated[self.historical_count:]}
        production_names = {entry["name"] for entry in self.production}
        self.assertEqual(excluded, EXCLUDED)
        self.assertTrue(excluded.isdisjoint(validated_names))
        self.assertTrue(excluded.isdisjoint(production_names))

    def test_06_every_company_has_requested_report_fields(self):
        required = {
            "company", "industry", "careers_url", "detected_platform", "result",
            "adapter_used", "jobs_normalized", "reason_if_excluded",
        }
        self.assertEqual(len(self.report["results"]), 15)
        for result in self.report["results"]:
            self.assertTrue(required.issubset(result))
            self.assertIn(result["result"], {"validated", "excluded"})
            self.assertLessEqual(result["jobs_normalized"], 20)

    def test_07_successes_are_repeatable_and_bounded(self):
        for result in self.report["results"]:
            self.assertEqual(len(result["runs"]), 2)
            for run in result["runs"]:
                self.assertLessEqual(run["jobs_normalized"], 20)
            if result["safe_to_activate"]:
                first, second = result["runs"]
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["apply_urls"], second["apply_urls"])

    def test_08_platform_and_adapter_classifications(self):
        expected = {
            "Infineon Technologies": "eightfold",
            "MediaTek": "browser_rendered_html",
            "GlobalFoundries": "eightfold",
            "Beckhoff Automation": "browser_rendered_html",
            "Lucid Motors": "greenhouse",
            "Varjo": "none",
            "Sanctuary AI": "static_html",
            "Clearpath Robotics": "none",
            "OTTO Motors": "none",
            "Instacart": "greenhouse",
            "Faire": "greenhouse",
            "Deliveroo": "browser_rendered_html",
            "ByteDance": "browser_rendered_html",
            "Vimeo": "none",
            "Substack": "ashby",
        }
        self.assertEqual(
            {result["company"]: result["adapter_used"] for result in self.report["results"]},
            expected,
        )

    def test_09_production_and_parent_mappings_are_unchanged(self):
        self.assertEqual(
            hashlib.sha256(self.candidate_prefix_bytes).hexdigest().upper(),
            self.report["production_registry_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
            self.report["parent_mappings_sha256"],
        )
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)

    def test_10_projection_uses_only_validated_sources(self):
        totals = self.report["totals"]
        self.assertEqual((totals["validated"], totals["excluded"]), (11, 4))
        self.assertEqual(totals["projected_production_sources"], 171)
        self.assertEqual(totals["projected_original_company_coverage"], 166)

    def test_11_sources_are_unique(self):
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

    def test_13_no_persistence_or_dashboard_imports(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))

    def test_14_browser_use_is_declarative_and_bounded(self):
        browsers = [entry for entry in batch.CANDIDATE_ENTRIES if entry["platform"] == "browser_rendered_html"]
        self.assertEqual(
            [entry["name"] for entry in browsers],
            ["MediaTek", "Beckhoff Automation", "Deliveroo", "ByteDance"],
        )
        for entry in browsers:
            self.assertTrue(entry["browser_wait_selector"])
            self.assertLessEqual(entry["max_pages"], 2)
            self.assertLessEqual(entry["max_jobs"], 20)
            self.assertLessEqual(entry["browser_scroll_steps"], 3)
        collector_source = inspect.getsource(browser_rendered_html).casefold()
        self.assertFalse(any(name.casefold() in collector_source for name in batch.REVIEWED_COMPANIES))


class SourceExpansionV2BatchAContractTests(unittest.TestCase):
    def test_15_duplicate_company_and_source_detection(self):
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
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(
                    production,
                    destination,
                    [{"name": "Existing", "platform": "greenhouse", "slug": "other"}],
                )
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())

    def test_17_stable_ids_and_official_apply_hosts_are_enforced(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(batch._verify_jobs([job()], entry), [("1", "https://jobs.example.test/job/1")])
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([job(), job(url="https://jobs.example.test/job/2")], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([job(url="https://evil.test/job/1")], entry)

    def test_18_repeated_validation_must_be_deterministic(self):
        entry = {
            "name": "Substack",
            "original_workbook_rank": 245,
            "careers_url": "https://substack.com/jobs",
        }
        run = {"success": True, "stable_ids": ["1"], "apply_urls": ["https://jobs.test/1"]}
        stable = batch._finalize_result(entry, [dict(run), dict(run)])
        changed = batch._finalize_result(
            entry,
            [dict(run), {"success": True, "stable_ids": ["2"], "apply_urls": ["https://jobs.test/2"]}],
        )
        self.assertTrue(stable["safe_to_activate"])
        self.assertEqual(changed["validation_status"], "non_deterministic")

    def test_19_direct_validation_does_not_initialize_playwright(self):
        entry = {
            "name": "Example",
            "platform": "ashby",
            "slug": "example",
            "careers_url": "https://example.test/careers",
            "source_identity_regex": "Example",
            "allowed_apply_hosts": ["jobs.example.test"],
        }
        fake_adapter = lambda _session, _target, _timeout: [job()]
        with (
            patch.object(batch.engine, "_identity_check", return_value={"status": 200}),
            patch.dict(batch.engine.scanner.ADAPTERS, {"ashby": fake_adapter}),
            patch.object(browser_rendered_html, "_load_sync_playwright") as loader,
        ):
            result = batch._run_direct(entry, 30)
        self.assertTrue(result["success"])
        loader.assert_not_called()

    def test_20_fail_closed_error_mapping(self):
        cases = {
            "authentication required": "authentication_required",
            "session required": "session_required",
            "captcha blocked": "blocked",
            "company identity ambiguous": "mixed_or_ambiguous_identity",
            "malformed html": "malformed_or_limit_failure",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(batch._status_for_error(AdapterError(message)), expected)


if __name__ == "__main__":
    unittest.main()
