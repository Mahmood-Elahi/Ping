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

from scripts import validate_calgary_canada_expansion_v2_batch_a as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


CANDIDATES = [item["name"] for item in batch.CANDIDATE_ENTRIES]
VALIDATED = [name for name in CANDIDATES if name != "Enerflex"]
STATIC_EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}
EXCLUDED = STATIC_EXCLUDED | {"Enerflex"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class CalgaryCanadaExpansionV2BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_artifacts_preserve_exact_262_entry_production_prefix(self):
        self.assertEqual(len(self.production), 262)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(self.candidate_bytes[:len(self.production_bytes)], self.production_bytes)
        self.assertEqual(self.validated_bytes[:len(self.production_bytes)], self.production_bytes)
        self.assertEqual(self.candidates[:262], self.production)
        self.assertEqual(self.validated[:262], self.production)
        self.assertTrue(self.report["candidate_registry_has_exact_262_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_262_entry_prefix"])

    def test_02_candidate_has_eight_and_validated_has_seven_inactive_additions(self):
        self.assertEqual((len(self.candidates), len(self.validated)), (270, 269))
        self.assertEqual([item["name"] for item in self.candidates[262:]], CANDIDATES)
        self.assertEqual([item["name"] for item in self.validated[262:]], VALIDATED)
        production_names = {item["name"] for item in self.production}
        self.assertTrue(set(CANDIDATES).isdisjoint(production_names))
        self.assertTrue(STATIC_EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))

    def test_03_report_and_csv_cover_the_exact_requested_order(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [row["company"] for row in self.report["results"] if row["safe_to_activate"]]
        excluded = {row["company"] for row in self.report["results"] if not row["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)

    def test_04_report_has_attribution_contract_and_location_evidence(self):
        required = {
            "company", "official_careers_url", "company_attribution_evidence",
            "identity_ownership_location_evidence", "canada_us_remote_student_compatibility",
            "platform", "adapter", "result", "jobs_normalized",
            "current_student_intern_hits", "current_student_role_evidence",
            "canada_us_remote_locations_represented", "calgary_locations_found",
            "validation_status", "stable_id_result", "apply_url_result", "exclusion_reason",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertTrue(item["official_careers_url"].startswith("https://"))
            self.assertTrue(item["company_attribution_evidence"])
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_05_two_runs_have_stable_ids_titles_locations_and_urls(self):
        for item in self.report["results"]:
            self.assertEqual(len(item["runs"]), 2)
            if item["safe_to_activate"]:
                first, second = item["runs"]
                self.assertTrue(first["success"] and second["success"])
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["apply_urls"], second["apply_urls"])
                self.assertEqual(first["student_hits"], second["student_hits"])
                self.assertEqual(first["canadian_locations"], second["canadian_locations"])
                self.assertTrue(all(first["stable_ids"]))
                self.assertTrue(all(first["apply_urls"]))

    def test_06_live_counts_student_hits_and_calgary_locations(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(
            {name: by_name[name]["jobs_normalized"] for name in VALIDATED},
            {
                "NOVA Chemicals": 2, "Trans Mountain": 20,
                "Computer Modelling Group (CMG)": 7, "Neo Financial": 20,
                "MNP": 20, "Absorb Software": 20,
                "PurposeMed": 10,
            },
        )
        self.assertEqual(by_name["Enerflex"]["jobs_normalized"], 0)
        self.assertEqual(by_name["Enerflex"]["validation_status"], "non_deterministic")
        self.assertEqual([run["jobs_normalized"] for run in by_name["Enerflex"]["runs"]], [20, 20])
        self.assertTrue(all(len(run["student_hits"]) == 1 for run in by_name["Enerflex"]["runs"]))
        self.assertEqual(by_name["MNP"]["current_student_intern_hits"], 10)
        self.assertTrue(all(by_name[name]["current_student_intern_hits"] == 0 for name in set(VALIDATED) - {"MNP"}))
        self.assertTrue(by_name["Enerflex"]["runs"][0]["student_hits"][0]["title"].startswith("Estagi"))
        self.assertEqual(by_name["Trans Mountain"]["calgary_locations_found"], ["Calgary"])
        self.assertEqual(by_name["Enerflex"]["calgary_locations_found"], ["Calgary, AB, CA, T2G 0K3"])
        self.assertEqual(by_name["Neo Financial"]["calgary_locations_found"], ["Calgary, AB"])

    def test_07_platforms_reuse_registered_adapters(self):
        expected = {
            "NOVA Chemicals": "sap_successfactors",
            "Trans Mountain": "sap_successfactors",
            "Computer Modelling Group (CMG)": "static_html",
            "Enerflex": "sap_successfactors", "Neo Financial": "ashby",
            "MNP": "ukg_pro", "Absorb Software": "ashby", "PurposeMed": "greenhouse",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.prior.scanner.ADAPTERS)
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)
            CompanyTarget.from_dict(item)

    def test_08_exact_attribution_contracts_are_configured(self):
        by_name = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
        self.assertEqual(by_name["Neo Financial"]["slug"], "neofinancial")
        self.assertEqual(by_name["Absorb Software"]["slug"], "absorblms")
        self.assertEqual(by_name["PurposeMed"]["slug"], "purposemed")
        self.assertIn("MNP5000MNPL", by_name["MNP"]["careers_url"])
        self.assertEqual(by_name["MNP"]["identity_regex"], r"^Campus Career Opportunities$")
        self.assertEqual(by_name["MNP"]["source_identity_regex"], r"MNP")
        self.assertEqual(by_name["Trans Mountain"]["endpoint_variant"], "rmk_tile")
        self.assertEqual(by_name["Enerflex"]["endpoint_variant"], "rmk_tile")

    def test_09_exclusions_and_unchanged_recovery_contracts_fail_closed(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertIn("columnized RMK", by_name["CPKC"]["exclusion_reason"])
        self.assertIn("reCAPTCHA", by_name["WestJet"]["exclusion_reason"])
        self.assertIn("HiBob", by_name["Helcim"]["exclusion_reason"])
        self.assertIn("stable per-job IDs", by_name["Eavor Technologies"]["exclusion_reason"])
        self.assertIn("different stable IDs", by_name["Enerflex"]["exclusion_reason"])
        for name, marker in {
            "Parkland Corporation": "Cloudflare",
            "Blackline Safety": "Turnstile",
            "AltaLink": "Turnstile",
        }.items():
            self.assertEqual(by_name[name]["validation_status"], "unchanged_protected_contract")
            self.assertIn(marker, by_name[name]["exclusion_reason"])
            self.assertIn("No contract change", by_name[name]["exclusion_reason"])

    def test_10_projection_and_master_coverage_stay_at_requested_baseline(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 193)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual((totals["validated"], totals["excluded"]), (7, 8))
        self.assertEqual(totals["projected_canonical_production_count"], 269)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "193/246")
        self.assertEqual((totals["fresh_targets"], totals["recovery_targets"]), (12, 3))

    def test_11_sources_are_unique_and_urls_are_allowlisted(self):
        keys = [batch.source_key(item) for item in self.candidates]
        present = [key for key in keys if key]
        self.assertEqual(len(present), len(set(present)))
        for result in self.report["results"]:
            if result["safe_to_activate"]:
                self.assertEqual(result["stable_id_result"], "stable_across_two_runs")
                self.assertEqual(result["apply_url_result"], "stable_official_and_job_specific_across_two_runs")

    def test_12_hash_ignore_and_no_publication_state(self):
        self.assertEqual(hashlib.sha256(self.production_bytes).hexdigest().upper(), self.report["production_registry_sha256"])
        self.assertEqual(hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(), self.report["parent_mappings_sha256"])
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))], cwd=batch.ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_13_validator_imports_no_persistence_publisher_or_browser(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan", "browser_rendered_html")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class CalgaryCanadaExpansionV2BatchAContractTests(unittest.TestCase):
    def test_14_duplicate_company_and_source_detection(self):
        baseline = [{"name": "Existing", "platform": "ashby", "slug": "shared"}]
        names, sources = batch._duplicates(
            baseline,
            [{"name": " existing ", "platform": "ashby", "slug": "other"},
             {"name": "Other", "platform": "ashby", "slug": "SHARED"}],
        )
        self.assertEqual(names, {0: "Existing"})
        self.assertEqual(sources, {1: "Existing"})

    def test_15_duplicate_build_does_not_change_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory) / "production.yaml"
            destination = Path(directory) / "candidate.yaml"
            production.write_text("target_companies:\n  - name: Existing\n    platform: ashby\n    slug: shared\n", encoding="utf-8")
            before = production.read_bytes()
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(
                    production, destination,
                    [{"name": "Existing", "platform": "ashby", "slug": "other"}],
                )
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())

    def test_16_stable_ids_and_job_specific_official_urls(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(
            batch._verify_jobs(
                [fake_job("2"), fake_job("1", "https://jobs.example.test/job/1")], entry,
            ),
            [("1", "https://jobs.example.test/job/1"), ("2", "https://jobs.example.test/job/2")],
        )
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([fake_job(), fake_job(url="https://jobs.example.test/job/3")], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([fake_job(url="https://other.example.test/job/2")], entry)


if __name__ == "__main__":
    unittest.main()
