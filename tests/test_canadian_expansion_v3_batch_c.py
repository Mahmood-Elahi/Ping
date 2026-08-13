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

from tests.company_careers_reliability_assertions import (
    assert_historical_registry_bytes,
    with_reliability_repairs,
)

from scripts import validate_canadian_expansion_v3_batch_c as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {"SMART Technologies", "CoolIT Systems", "Attabotics", "Ballard Power Systems"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def normalized_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def historical_prefix_bytes(path: Path) -> bytes:
    marker = b"  - name: Clio"
    content = path.read_bytes()
    for line in content.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == marker:
            return content[:content.index(line)]
    raise AssertionError(f"Historical boundary 'Clio' not found in {path}")


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class CanadianExpansionV3BatchCArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.historical_prefix_bytes = historical_prefix_bytes(batch.CANDIDATE_REGISTRY)
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_exact_216_source_baseline(self):
        self.assertEqual(len(self.production), 344)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(self.production[:216], with_reliability_repairs(self.candidates[:216]))
        self.assertEqual(self.production[:216], with_reliability_repairs(self.validated[:216]))
        self.assertEqual(self.production[216:227], self.validated[216:])
        self.assertEqual(batch.PRODUCTION.read_bytes(), self.production_bytes)
        historical_prefix = normalized_newlines(self.historical_prefix_bytes)
        assert_historical_registry_bytes(
            self, self.production_bytes, self.historical_prefix_bytes
        )
        self.assertTrue(
            normalized_newlines(batch.CANDIDATE_REGISTRY.read_bytes()).startswith(
                historical_prefix
            )
        )
        self.assertTrue(
            normalized_newlines(batch.VALIDATED_REGISTRY.read_bytes()).startswith(
                historical_prefix
            )
        )
        self.assertTrue(self.report["candidate_registry_has_exact_216_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_216_entry_prefix"])

    def test_02_candidate_and_validated_registries_contain_only_safe_additions(self):
        self.assertEqual(len(self.candidates), 227)
        self.assertEqual(len(self.validated), 227)
        self.assertEqual([item["name"] for item in self.candidates[216:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[216:]], VALIDATED)
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))
        self.assertEqual(len({item["name"].casefold() for item in self.candidates}), 227)

    def test_03_review_results_and_required_report_fields(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([item["company"] for item in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {"company", "official_careers_url", "canadian_identity_location_evidence",
                    "canada_us_remote_student_compatibility", "platform", "adapter", "result",
                    "jobs_normalized", "current_student_intern_hits", "canadian_locations_represented",
                    "exclusion_reason", "original_246_master", "stable_id_result", "apply_url_result"}
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_repeat_runs_are_bounded_and_stable(self):
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
                self.assertEqual(first["student_hits"], second["student_hits"])
                self.assertEqual(first["canadian_locations"], second["canadian_locations"])

    def test_05_normalized_counts_and_current_student_hits(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {"Clio": 20, "Benevity": 16, "Hexagon Autonomy & Positioning / NovAtel": 20,
                           "Geotab": 20, "Miovision": 12, "ATS Corporation": 20, "Linamar": 20,
                           "1Password": 20, "D2L": 20, "Hootsuite": 20, "PointClickCare": 20}
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(by_name["Geotab"]["current_student_intern_hits"], 5)
        self.assertEqual([hit["title"] for hit in by_name["ATS Corporation"]["current_student_role_evidence"]],
                         ["HR Business Partner Intern"])
        self.assertEqual([hit["title"] for hit in by_name["Hootsuite"]["current_student_role_evidence"]],
                         ["Co-op/Intern, IT Operations - Fall 2026", "Co-op/Intern, Product Design - Fall 2026"])

    def test_06_canadian_locations_are_explicit(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected = {"Clio": "Vancouver", "Benevity": "Calgary", "Hexagon Autonomy & Positioning / NovAtel": "CA-AB-Calgary",
                    "Geotab": "Remote - Canada", "Miovision": "Kitchener", "ATS Corporation": "Cambridge, ON, CA",
                    "Linamar": "Guelph, ON, Canada", "1Password": "United States | Canada", "D2L": "Remote - Canada",
                    "Hootsuite": "Vancouver, British Columbia, Canada", "PointClickCare": "Mississauga"}
        for name, token in expected.items():
            self.assertIn(token, " | ".join(by_name[name]["canadian_locations_represented"]))

    def test_07_master_membership_and_projections(self):
        master = reconcile_master(load_master_workbook(batch.MASTER_WORKBOOK),
                                  load_target_companies(str(batch.PRODUCTION)),
                                  load_parent_source_mappings(batch.PARENT_MAPPINGS))
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 208)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 11)
        self.assertEqual(totals["validated"], 11)
        self.assertEqual(totals["excluded"], 4)
        self.assertEqual(totals["projected_canonical_production_count"], 227)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "186/246")
        self.assertEqual(totals["original_master_companies_recovered_names"], [])

    def test_08_platforms_reuse_existing_adapters(self):
        expected = {"Clio": "workday", "Benevity": "ashby", "Hexagon Autonomy & Positioning / NovAtel": "icims",
                    "Geotab": "greenhouse", "Miovision": "ashby", "ATS Corporation": "sap_successfactors",
                    "Linamar": "oracle_recruiting", "1Password": "ashby", "D2L": "greenhouse",
                    "Hootsuite": "greenhouse", "PointClickCare": "static_html"}
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            CompanyTarget.from_dict(item)

    def test_09_hexagon_division_and_novatel_attribution_is_exact(self):
        item = next(item for item in batch.CANDIDATE_ENTRIES if item["name"].startswith("Hexagon"))
        self.assertEqual(item["search_url"], "https://careers-hexagonpositioning.icims.com/jobs/search")
        self.assertEqual(item["allowed_apply_hosts"], ["careers-hexagonpositioning.icims.com"])
        evidence = batch.CANADA_RELEVANCE[item["name"]]
        for brand in ("NovAtel", "AutonomouStuff", "Veripos", "Antcom"):
            self.assertIn(brand, evidence)
        result = next(row for row in self.report["results"] if row["company"] == item["name"])
        self.assertTrue(all(run["identity"]["url"] == batch.OFFICIAL_CAREERS_URLS[item["name"]] for run in result["runs"]))
        self.assertTrue(all("careers-hexagonpositioning.icims.com/jobs/" in url for url in result["runs"][0]["apply_urls"]))

    def test_10_current_identity_and_parent_boundaries_are_not_inferred(self):
        reasons = {item["company"]: item["exclusion_reason"] for item in self.report["results"]}
        self.assertIn("SMART Technologies ULC", reasons["SMART Technologies"])
        self.assertIn("Foxconn ownership is not used", reasons["SMART Technologies"])
        self.assertIn("Ecolab's July 2026 acquisition", reasons["CoolIT Systems"])
        self.assertIn("current legal and public brand is ATS Corporation", batch.CANADA_RELEVANCE["ATS Corporation"])
        mapping_text = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        for name in batch.REVIEWED_COMPANIES:
            self.assertNotIn(name, mapping_text)

    def test_11_attabotics_operating_source_is_not_promoted_without_current_jobs(self):
        result = next(item for item in self.report["results"] if item["company"] == "Attabotics")
        self.assertEqual(result["validation_status"], "no_current_jobs_for_id_apply_validation")
        self.assertIn("official careers page and exact Lever board are live", result["exclusion_reason"])
        self.assertIn("zero postings", result["exclusion_reason"])
        self.assertFalse(result["safe_to_activate"])

    def test_12_remote_first_canadian_identity_and_pointclickcare_fallback(self):
        one = next(item for item in self.report["results"] if item["company"] == "1Password")
        self.assertIn("remote-first", one["canadian_identity_location_evidence"])
        self.assertIn("Canada", " | ".join(one["canadian_locations_represented"]))
        pcc = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "PointClickCare")
        self.assertEqual(pcc["platform"], "static_html")
        self.assertEqual(pcc["listing_url"], "https://jobs.lever.co/pointclickcare")
        pcc_result = next(item for item in self.report["results"] if item["company"] == "PointClickCare")
        self.assertTrue(all(url.startswith("https://jobs.lever.co/pointclickcare/") for url in pcc_result["runs"][0]["apply_urls"]))

    def test_13_hashes_ignore_and_publication_state(self):
        self.assertEqual(hashlib.sha256(self.historical_prefix_bytes).hexdigest().upper(), self.report["production_registry_sha256"])
        self.assertEqual(hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(), self.report["parent_mappings_sha256"])
        ignored = subprocess.run(["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))], cwd=batch.ROOT,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_14_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class CanadianExpansionV3BatchCContractTests(unittest.TestCase):
    def test_15_duplicate_detection_and_build_preserve_baseline(self):
        baseline = [{"name": "Existing", "platform": "greenhouse", "slug": "shared"}]
        names, sources = batch._duplicates(baseline, [{"name": " existing ", "platform": "greenhouse", "slug": "other"},
                                                       {"name": "Other", "platform": "greenhouse", "slug": "SHARED"}])
        self.assertEqual(names, {0: "Existing"})
        self.assertEqual(sources, {1: "Existing"})
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory) / "production.yaml"; destination = Path(directory) / "candidate.yaml"
            production.write_text("target_companies:\n  - name: Existing\n    platform: greenhouse\n    slug: shared\n", encoding="utf-8")
            before = production.read_bytes()
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(production, destination,
                                               [{"name": "Existing", "platform": "greenhouse", "slug": "other"}])
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())

    def test_16_stable_ids_official_apply_urls_and_unique_sources(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(batch._verify_jobs([fake_job("2", "https://jobs.example.test/job/2"),
                                             fake_job("1", "https://jobs.example.test/job/1")], entry),
                         [("1", "https://jobs.example.test/job/1"), ("2", "https://jobs.example.test/job/2")])
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([fake_job(), fake_job(url="https://jobs.example.test/job/3")], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([fake_job(url="https://evil.test/job/2")], entry)
        keys = [batch.source_key(item) for item in batch.CANDIDATE_ENTRIES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn(None, keys)


if __name__ == "__main__":
    unittest.main()
