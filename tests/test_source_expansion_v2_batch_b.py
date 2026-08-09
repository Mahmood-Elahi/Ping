from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from scripts import validate_source_expansion_v2_batch_b as batch
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [
    "Analog Devices",
    "Microchip Technology",
    "Omron",
    "Keyence",
    "Magna International",
    "Autodesk",
    "Quora",
    "Bumble",
    "Medium",
]
EXCLUDED = {
    "STMicroelectronics",
    "BMW",
    "Epic Games",
    "Ocado Technology",
    "LinkedIn",
    "Twitch",
}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def milestone_prefix_bytes(path: Path) -> bytes:
    """Derive the historical registry prefix from this milestone's artifact."""
    content = path.read_bytes()
    artifact_entries = entries(path)
    promoted = {" ".join(name.casefold().split()) for name in VALIDATED}
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


class SourceExpansionV2BatchBArtifactTests(unittest.TestCase):
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

    def test_01_exact_171_entry_production_prefix_is_preserved(self):
        self.assertEqual(self.historical_count, 171)
        self.assertEqual(self.candidate_prefix_bytes, self.validated_prefix_bytes)
        self.assertTrue(self.candidate_bytes.startswith(self.candidate_prefix_bytes))
        self.assertTrue(self.validated_bytes.startswith(self.validated_prefix_bytes))
        self.assertTrue(self.report["candidate_registry_has_exact_171_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_171_entry_prefix"])
        self.assertEqual(self.candidates[:self.historical_count], self.historical)
        self.assertEqual(self.validated[:self.historical_count], self.historical)
        self.assertEqual(self.production[:self.historical_count], self.historical)

    def test_02_selection_is_historical_uncovered_master_order(self):
        with tempfile.TemporaryDirectory() as directory:
            historical = Path(directory) / "historical.yaml"
            historical.write_bytes(self.candidate_prefix_bytes)
            reconciliation = batch.reconcile_master(
                batch.load_master_workbook(batch.MASTER_WORKBOOK),
                load_target_companies(str(historical)),
                load_parent_source_mappings(batch.PARENT_MAPPINGS),
            )
        uncovered, total = reconciliation.uncovered_names, reconciliation.total
        self.assertEqual((total, len(uncovered)), (246, 80))
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

    def test_04_validated_registry_is_append_only_success_only_and_in_production(self):
        safe = [result["company"] for result in self.report["results"] if result["safe_to_activate"]]
        validated_promotions = self.validated[self.historical_count:]
        promotion_end = self.historical_count + len(validated_promotions)
        production_promotions = self.production[self.historical_count:promotion_end]
        self.assertEqual(safe, VALIDATED)
        self.assertEqual([entry["name"] for entry in validated_promotions], VALIDATED)
        self.assertEqual(production_promotions, validated_promotions)
        self.assertEqual(len(self.validated), self.historical_count + len(safe))
        self.assertEqual(len(self.production), 180)
        production_names = [entry["name"] for entry in self.production]
        for name in VALIDATED:
            self.assertEqual(production_names.count(name), 1)

    def test_05_exclusions_never_enter_validated_or_production(self):
        excluded = {result["company"] for result in self.report["results"] if not result["safe_to_activate"]}
        self.assertEqual(excluded, EXCLUDED)
        present = {entry["name"] for entry in self.validated + self.production}
        self.assertTrue(EXCLUDED.isdisjoint(present))

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
                self.assertLessEqual(run.get("pages_requested", 0), 2)
            if result["safe_to_activate"]:
                first, second = result["runs"]
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["apply_urls"], second["apply_urls"])

    def test_08_platform_and_adapter_classifications(self):
        expected = {
            "Analog Devices": "workday",
            "STMicroelectronics": "eightfold",
            "Microchip Technology": "workday",
            "Omron": "sap_successfactors",
            "Keyence": "sap_successfactors",
            "BMW": "sap_successfactors",
            "Magna International": "workday",
            "Epic Games": "none",
            "Autodesk": "workday",
            "Ocado Technology": "none",
            "LinkedIn": "none",
            "Twitch": "none",
            "Quora": "ashby",
            "Bumble": "lever",
            "Medium": "greenhouse",
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
        self.assertEqual((totals["validated"], totals["excluded"]), (9, 6))
        self.assertEqual(totals["projected_production_sources"], 180)
        self.assertEqual(totals["projected_original_company_coverage"], 175)

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

    def test_13_no_persistence_dashboard_or_browser_imports(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = (
            "supabase", "gspread", "sqlite3", "update_student_dashboard",
            "run_student_job_scan", "browser_rendered_html",
        )
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))

    def test_14_candidates_use_only_existing_direct_adapters_and_bounds(self):
        self.assertNotIn("browser_rendered_html", {entry["platform"] for entry in batch.CANDIDATE_ENTRIES})
        for entry in batch.CANDIDATE_ENTRIES:
            self.assertLessEqual(entry["max_pages"], 2)
            self.assertLessEqual(entry["max_jobs"], 20)
            self.assertLessEqual(entry["max_response_bytes"], 3_000_000)


class SourceExpansionV2BatchBContractTests(unittest.TestCase):
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

    def test_18_workday_identity_requires_exact_structured_tenant_and_site(self):
        entry = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Autodesk")
        response = SimpleNamespace(
            status_code=200,
            content=b'window.workday = { tenant: "autodesk", siteId: "Ext" };',
            text='window.workday = { tenant: "autodesk", siteId: "Ext" };',
            url="https://autodesk.wd1.myworkdayjobs.com/Ext",
        )
        session = SimpleNamespace(get=lambda *args, **kwargs: response)
        result = batch._identity_check(entry, session, 30)
        self.assertEqual(result["status"], 200)
        response.text = response.text.replace('tenant: "autodesk"', 'tenant: "other"')
        with self.assertRaisesRegex(AdapterError, "tenant identity"):
            batch._identity_check(entry, session, 30)

    def test_19_direct_validation_does_not_initialize_browser(self):
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
        ):
            result = batch._run_direct(entry, 30)
        self.assertTrue(result["success"])

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
