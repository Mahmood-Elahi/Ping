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

import yaml

from scripts import validate_calgary_student_employers_v1_batch_a as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {"Pason Systems", "AltaLink", "City of Calgary"}
STILL_EXCLUDED = {"AltaLink"}
MASTER_MEMBERS = {"Imperial Oil", "Cenovus Energy", "ARC Resources"}
HISTORICAL_COUNT = 186


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def historical_prefix_bytes(path: Path) -> bytes:
    content = path.read_bytes()
    marker = f"  - name: {VALIDATED[0]}".encode()
    offset = content.index(marker)
    return content[:offset]


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class CalgaryStudentEmployersBatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.candidate_prefix_bytes = historical_prefix_bytes(batch.CANDIDATE_REGISTRY)
        cls.validated_prefix_bytes = historical_prefix_bytes(batch.VALIDATED_REGISTRY)
        cls.historical = (yaml.safe_load(cls.validated_prefix_bytes) or {})["target_companies"]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_exact_production_prefix_and_counts(self):
        self.assertEqual(len(self.production), 315)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), HISTORICAL_COUNT)
        self.assertEqual(
            (yaml.safe_load(self.candidate_prefix_bytes) or {})["target_companies"],
            self.historical,
        )
        self.assertTrue(self.candidate_bytes.startswith(self.candidate_prefix_bytes))
        self.assertTrue(self.validated_bytes.startswith(self.validated_prefix_bytes))
        self.assertTrue(self.production_bytes.startswith(self.validated_prefix_bytes))
        self.assertTrue(self.report["candidate_registry_has_exact_186_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_186_entry_prefix"])
        self.assertEqual(self.candidates[:HISTORICAL_COUNT], self.historical)
        self.assertEqual(self.validated[:HISTORICAL_COUNT], self.historical)
        self.assertEqual(self.production[:HISTORICAL_COUNT], self.historical)
        self.assertEqual(
            self.production[HISTORICAL_COUNT:len(self.validated)],
            self.validated[HISTORICAL_COUNT:],
        )

    def test_02_production_contains_exact_promotions_and_exclusions(self):
        production_names = [item["name"] for item in self.production]
        self.assertEqual(production_names[HISTORICAL_COUNT:HISTORICAL_COUNT + len(VALIDATED)], VALIDATED)
        for name in VALIDATED:
            self.assertEqual(production_names.count(name), 1)
        self.assertTrue(STILL_EXCLUDED.isdisjoint(production_names))
        # The validation report remains immutable milestone provenance.
        self.assertEqual(self.report["totals"]["production_sources"], 186)
        self.assertFalse(self.report["publication"]["production_activated"])

    def test_03_candidate_and_validated_artifacts_are_append_only(self):
        self.assertEqual(len(self.candidates), HISTORICAL_COUNT + len(VALIDATED))
        self.assertEqual(len(self.validated), HISTORICAL_COUNT + len(VALIDATED))
        self.assertEqual([item["name"] for item in self.candidates[HISTORICAL_COUNT:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[HISTORICAL_COUNT:]], VALIDATED)

    def test_04_exact_review_set_and_results(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([item["company"] for item in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)

    def test_05_requested_company_report_fields(self):
        required = {
            "company", "official_careers_url", "calgary_alberta_relevance", "platform",
            "adapter", "result", "jobs_normalized", "current_student_intern_hits",
            "current_student_role_evidence", "exclusion_reason", "original_246_master",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertIn(item["result"], {"validated", "excluded"})
            self.assertLessEqual(item["jobs_normalized"], 20)
            self.assertTrue(item["official_careers_url"].startswith("https://"))
            self.assertTrue(item["calgary_alberta_relevance"])

    def test_06_repeat_runs_are_bounded_and_stable(self):
        for item in self.report["results"]:
            self.assertEqual(len(item["runs"]), 2)
            for run in item["runs"]:
                self.assertLessEqual(run["jobs_normalized"], 20)
                self.assertLessEqual(run["pages_requested"], 2)
            if item["safe_to_activate"]:
                first, second = item["runs"]
                self.assertTrue(first["success"] and second["success"])
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["apply_urls"], second["apply_urls"])

    def test_07_zero_job_sources_are_conclusive(self):
        zero = {
            item["company"] for item in self.report["results"]
            if item["validation_status"] == "validated_zero_jobs"
        }
        self.assertEqual(zero, {"ARC Resources", "Nutrien"})
        for item in self.report["results"]:
            if item["company"] in zero:
                self.assertTrue(all(run["zero_job_contract"] for run in item["runs"]))
                self.assertEqual(item["stable_id_result"], "not_observable_current_zero_collection")

    def test_08_current_student_hits_are_title_level_evidence(self):
        hits = {item["company"]: item["current_student_role_evidence"] for item in self.report["results"]}
        self.assertEqual([item["title"] for item in hits["YYC Calgary Airport Authority"]],
                         ["Co-op, Accounting (12 Months Term)"])
        self.assertEqual(hits["Garmin Canada"], [])
        for item in self.report["results"]:
            self.assertEqual(item["current_student_intern_hits"], len(item["current_student_role_evidence"]))

    def test_09_master_membership_and_projection(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        requested_master = {row["company"] for row in master.rows}.intersection(batch.REVIEWED_COMPANIES)
        self.assertEqual(requested_master, MASTER_MEMBERS)
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 196)
        self.assertEqual(master.total, 246)
        totals = self.report["totals"]
        self.assertEqual(totals["additional_canonical_production_sources_projected"], 12)
        self.assertEqual(totals["projected_production_sources"], 198)
        self.assertEqual(totals["original_master_companies_recovered"], 3)
        self.assertEqual(set(totals["original_master_companies_recovered_names"]), MASTER_MEMBERS)
        self.assertEqual(totals["projected_original_master_coverage"], 184)
        self.assertEqual(totals["projected_original_master_total"], 246)
        self.assertEqual(totals["calgary_focused_validated_outside_master"], 9)

    def test_10_platforms_and_existing_adapters(self):
        expected = {
            "Spartan Controls": "sap_successfactors", "Garmin Canada": "icims",
            "ENMAX": "workday", "ATCO": "phenom",
            "AESO (Alberta Electric System Operator)": "workday",
            "TransAlta": "sap_successfactors", "Cenovus Energy": "workday",
            "Imperial Oil": "static_html", "ARC Resources": "sap_successfactors",
            "Nutrien": "browser_rendered_html", "Stantec": "browser_rendered_html",
            "YYC Calgary Airport Authority": "static_html",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)
            CompanyTarget.from_dict(item)

    def test_11_source_keys_are_unique(self):
        keys = [batch.source_key(item) for item in self.candidates]
        present = [key for key in keys if key]
        self.assertEqual(len(present), len(set(present)))

    def test_12_exclusions_never_enter_candidate_registries(self):
        milestone_present = {item["name"] for item in self.candidates + self.validated}
        production_names = {item["name"] for item in self.production}
        self.assertTrue(EXCLUDED.isdisjoint(milestone_present))
        self.assertTrue(STILL_EXCLUDED.isdisjoint(production_names))
        reasons = {item["company"]: item["exclusion_reason"] for item in self.report["results"]}
        self.assertIn("Pason & IWS", reasons["Pason Systems"])
        self.assertIn("Turnstile", reasons["AltaLink"])
        self.assertIn("PeopleSoft", reasons["City of Calgary"])

    def test_13_production_and_mapping_hashes_match(self):
        self.assertEqual(hashlib.sha256(self.validated_prefix_bytes).hexdigest().upper(),
                         self.report["production_registry_sha256"])
        self.assertEqual(hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
                         self.report["parent_mappings_sha256"])

    def test_14_report_is_ignored_and_all_writes_are_disabled(self):
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_15_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class CalgaryStudentEmployersBatchAContractTests(unittest.TestCase):
    def test_16_duplicate_company_and_source_detection(self):
        baseline = [{"name": "Existing", "platform": "greenhouse", "slug": "shared"}]
        names, sources = batch._duplicates(
            baseline,
            [{"name": " existing ", "platform": "greenhouse", "slug": "other"},
             {"name": "Other", "platform": "greenhouse", "slug": "SHARED"}],
        )
        self.assertEqual(names, {0: "Existing"})
        self.assertEqual(sources, {1: "Existing"})

    def test_17_build_rejects_duplicate_without_changing_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory) / "production.yaml"
            destination = Path(directory) / "candidate.yaml"
            production.write_text(
                "target_companies:\n  - name: Existing\n    platform: greenhouse\n    slug: shared\n",
                encoding="utf-8",
            )
            before = production.read_bytes()
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(
                    production, destination,
                    [{"name": "Existing", "platform": "greenhouse", "slug": "other"}],
                )
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())

    def test_18_stable_ids_and_job_specific_official_urls(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(
            batch._verify_jobs(
                [fake_job("2", "https://jobs.example.test/job/2"),
                 fake_job("1", "https://jobs.example.test/job/1")], entry,
            ),
            [("1", "https://jobs.example.test/job/1"), ("2", "https://jobs.example.test/job/2")],
        )
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([fake_job(), fake_job(url="https://jobs.example.test/job/3")], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([fake_job(url="https://evil.test/job/2")], entry)

    def test_19_zero_job_finalization_requires_two_conclusive_runs(self):
        entry = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "ARC Resources")
        run = {"success": True, "stable_ids": [], "apply_urls": [], "student_hits": [],
               "zero_job_contract": True, "jobs_normalized": 0, "pages_requested": 1}
        result = batch._finalize(entry, [dict(run, run=1), dict(run, run=2)])
        self.assertTrue(result["safe_to_activate"])
        self.assertEqual(result["validation_status"], "validated_zero_jobs")
        result = batch._finalize(entry, [dict(run, run=1), dict(run, run=2, zero_job_contract=False)])
        self.assertFalse(result["safe_to_activate"])

    def test_20_student_detection_ignores_generic_benefit_copy(self):
        experienced = SimpleNamespace(
            title="Senior Software Engineer", description="Includes student loan repayment",
            location="Calgary", url="https://jobs.example.test/job/1",
        )
        coop = SimpleNamespace(
            title="Software Engineering Co-op", description="", location="Calgary",
            url="https://jobs.example.test/job/2",
        )
        self.assertEqual(batch._student_hits([experienced]), [])
        self.assertEqual([item["title"] for item in batch._student_hits([coop])],
                         ["Software Engineering Co-op"])

    def test_21_fail_closed_error_mapping(self):
        cases = {
            "authentication required": "authentication_required", "session required": "session_required",
            "captcha blocked": "blocked", "company identity ambiguous": "mixed_or_ambiguous_identity",
            "malformed html": "malformed_or_limit_failure",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(batch._status_for_error(AdapterError(message)), expected)


if __name__ == "__main__":
    unittest.main()
