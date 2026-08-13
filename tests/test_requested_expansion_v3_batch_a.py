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
    assert_historical_prefix,
    assert_historical_registry_bytes,
)

from scripts import validate_requested_expansion_v3_batch_a as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {
    "Parkland Corporation", "MEG Energy", "Baytex Energy", "Trican Well Service",
    "Baker Hughes", "Netflix",
}
MASTER_MEMBERS = {"Precision Drilling", "SLB", "Baker Hughes", "Netflix"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def normalized_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class RequestedExpansionV3BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: Precision Drilling")]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_exact_198_entry_production_prefix_is_preserved(self):
        self.assertEqual(len(self.production), 339)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        historical_bytes = normalized_newlines(self.historical_bytes)
        assert_historical_registry_bytes(self, self.production_bytes, self.historical_bytes)
        self.assertTrue(normalized_newlines(self.candidate_bytes).startswith(historical_bytes))
        self.assertTrue(self.report["candidate_registry_has_exact_198_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_198_entry_prefix"])
        assert_historical_prefix(self, self.production, self.candidates[:198])
        assert_historical_prefix(self, self.production, self.validated[:198])

    def test_02_candidate_and_validated_counts(self):
        self.assertEqual(len(self.candidates), 207)
        self.assertEqual(len(self.validated), 207)
        self.assertEqual([item["name"] for item in self.candidates[198:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[198:]], VALIDATED)
        self.assertEqual(self.production[198:len(self.validated)], self.validated[198:])
        production_names = {item["name"] for item in self.production}
        for name in VALIDATED:
            self.assertEqual([item["name"] for item in self.production].count(name), 1)
        self.assertTrue((EXCLUDED - {"Trican Well Service"}).isdisjoint(production_names))
        self.assertEqual([item["name"] for item in self.production].count("Trican Well Service"), 1)

    def test_03_review_results_and_report_fields(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([item["company"] for item in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {
            "company", "official_careers_url", "calgary_alberta_relevance",
            "canada_us_remote_student_compatibility", "platform", "adapter", "result",
            "jobs_normalized", "current_student_intern_hits", "exclusion_reason",
            "original_246_master", "stable_id_result", "apply_url_result",
        }
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

    def test_05_normalized_counts_and_current_student_hits(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "Precision Drilling": 20, "AltaGas": 20, "Whitecap Resources": 6,
            "Vermilion Energy": 5, "SECURE Energy Services": 20, "ExxonMobil": 20,
            "SLB": 10, "BlackBerry/QNX": 19, "CAE Inc.": 20,
        }
        self.assertEqual(
            {name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts,
        )
        expected_hits = {
            "Vermilion Energy": ["Finance student"],
            "SLB": ["Neurodiversity Internship Program", "Supply Chain Internship"],
            "CAE Inc.": ["Student", "Mechanical Engineering Intern"],
        }
        for name, item in by_name.items():
            titles = [hit["title"] for hit in item["current_student_role_evidence"]]
            self.assertEqual(titles, expected_hits.get(name, []))
            self.assertEqual(item["current_student_intern_hits"], len(titles))

    def test_06_master_membership_and_projection(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        requested = {row["company"] for row in master.rows}.intersection(batch.REVIEWED_COMPANIES)
        self.assertEqual(requested, MASTER_MEMBERS)
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 208)
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 9)
        self.assertEqual(totals["validated"], 9)
        self.assertEqual(totals["excluded"], 6)
        self.assertEqual(totals["projected_canonical_production_count"], 207)
        self.assertEqual(totals["original_master_companies_recovered_names"], ["Precision Drilling", "SLB"])
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "186/246")

    def test_07_platforms_use_existing_adapters(self):
        expected = {
            "Precision Drilling": "sap_successfactors", "AltaGas": "workday",
            "Whitecap Resources": "workday", "Vermilion Energy": "workday",
            "SECURE Energy Services": "icims", "ExxonMobil": "sap_successfactors",
            "SLB": "eightfold", "BlackBerry/QNX": "workday", "CAE Inc.": "workday",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)
            CompanyTarget.from_dict(item)

    def test_08_qnx_attribution_is_exact(self):
        qnx = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "BlackBerry/QNX")
        self.assertEqual(qnx["site_id"], "QNX")
        self.assertEqual(qnx["tenant"], "bb")
        self.assertEqual(qnx["identity_url"], "https://bb.wd3.myworkdayjobs.com/QNX")
        self.assertIn("subsidiary of BlackBerry", qnx["source_identity_regex"])
        qnx_result = next(item for item in self.report["results"] if item["company"] == "BlackBerry/QNX")
        self.assertTrue(all(run["identity"]["status"] == 200 for run in qnx_result["runs"]))
        self.assertTrue(all(run["identity"]["url"].endswith("/QNX") for run in qnx_result["runs"]))

    def test_09_exxon_is_independent_from_imperial(self):
        exxon = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "ExxonMobil")
        imperial = next(item for item in self.production if item["name"] == "Imperial Oil")
        self.assertNotEqual(batch.source_key(exxon), batch.source_key(imperial))
        self.assertEqual(exxon["allowed_apply_hosts"], ["jobs.exxonmobil.com"])
        self.assertNotIn("Imperial", exxon["search_url"])
        self.assertIn("Imperial is not substituted", batch.COMPATIBILITY["ExxonMobil"])

    def test_10_slb_and_baker_hughes_are_independent(self):
        slb = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "SLB")
        baker = next(item for item in batch.EXCLUDED_REVIEWS if item["company"] == "Baker Hughes")
        self.assertEqual(slb["allowed_apply_hosts"], ["apply.slb.com"])
        self.assertIn("bakerhughes.com", baker["listing_url"])
        self.assertIn("not inferred", baker["exclusion_reason"])

    def test_11_exclusions_and_safety_guards(self):
        candidate_names = {item["name"] for item in self.candidates + self.validated}
        self.assertTrue(EXCLUDED.isdisjoint(candidate_names))
        reasons = {item["company"]: item["exclusion_reason"] for item in self.report["results"]}
        self.assertIn("Cloudflare", reasons["Parkland Corporation"])
        self.assertIn("Cenovus subsidiary", reasons["MEG Energy"])
        self.assertIn("Incapsula", reasons["Baker Hughes"])
        self.assertIn("reCAPTCHA", reasons["Netflix"])
        guard_source = (
            batch.ROOT / "src" / "scraper" / "company_careers" / "adapters" / "embedded_data.py"
        ).read_text(encoding="utf-8")
        self.assertIn("captcha", guard_source.casefold())

    def test_12_hashes_report_ignore_and_publication_state(self):
        self.assertEqual(
            hashlib.sha256(self.historical_bytes).hexdigest().upper(),
            self.report["production_registry_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
            self.report["parent_mappings_sha256"],
        )
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_13_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class RequestedExpansionV3BatchAContractTests(unittest.TestCase):
    def test_14_duplicate_detection_and_build_preserve_baseline(self):
        baseline = [{"name": "Existing", "platform": "greenhouse", "slug": "shared"}]
        names, sources = batch._duplicates(
            baseline,
            [{"name": " existing ", "platform": "greenhouse", "slug": "other"},
             {"name": "Other", "platform": "greenhouse", "slug": "SHARED"}],
        )
        self.assertEqual(names, {0: "Existing"})
        self.assertEqual(sources, {1: "Existing"})
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

    def test_15_stable_ids_and_official_apply_urls(self):
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


if __name__ == "__main__":
    unittest.main()
