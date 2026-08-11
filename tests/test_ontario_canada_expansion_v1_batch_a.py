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

from scripts import validate_ontario_canada_expansion_v1_batch_a as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {"Ross Video"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


def fake_job(
    job_id: str = "2",
    title: str = "Software Engineering Intern",
    location: str = "Toronto, Ontario, Canada",
    url: str = "https://jobs.example.test/job/2",
) -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, title=title, company="Example", location=location, url=url)


class OntarioCanadaExpansionV1BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: RBC")]
        cls.historical = entries_from_bytes(cls.historical_bytes)
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_the_exact_274_entry_production_prefix(self):
        self.assertEqual(len(self.production), 315)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), 274)
        self.assertEqual(self.production[:274], self.historical)
        self.assertEqual(self.candidates[:274], self.historical)
        self.assertEqual(self.validated[:274], self.historical)
        self.assertTrue(self.production_bytes.startswith(self.historical_bytes))
        self.assertEqual(self.production[:288], self.validated)
        self.assertTrue(self.report["candidate_registry_has_exact_274_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_274_entry_prefix"])

    def test_02_candidate_validated_and_production_contain_safe_additions_once(self):
        self.assertEqual(len(self.candidates), 288)
        self.assertEqual(len(self.validated), 288)
        self.assertEqual([item["name"] for item in self.candidates[274:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[274:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[274:288]], VALIDATED)
        production_names = [item["name"] for item in self.production]
        self.assertTrue(EXCLUDED.isdisjoint(production_names))
        for name in VALIDATED:
            self.assertEqual(production_names.count(name), 1)
        self.assertEqual(len({item["name"].casefold() for item in self.candidates}), 288)

    def test_03_all_requested_companies_and_report_fields_are_present(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([item["company"] for item in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {
            "company", "official_careers_url", "ontario_canada_relevance",
            "canada_student_compatibility", "platform", "adapter", "result",
            "jobs_normalized", "current_student_intern_hits", "current_focus_role_hits",
            "current_student_focus_hits", "ontario_locations_represented",
            "canadian_locations_represented", "exclusion_reason", "original_246_master",
            "stable_id_result", "title_result", "location_result", "apply_url_result",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_two_runs_are_bounded_and_all_normalized_fields_repeat(self):
        for item in self.report["results"]:
            self.assertEqual(len(item["runs"]), 2)
            for run in item["runs"]:
                self.assertLessEqual(run["jobs_normalized"], 20)
                self.assertLessEqual(run["pages_requested"], 2)
            if not item["safe_to_activate"]:
                continue
            first, second = item["runs"]
            self.assertTrue(first["success"] and second["success"])
            first_jobs = sorted(
                (job["job_id"], job["title"], job["location"], job["url"])
                for job in first["normalized_job_evidence"]
            )
            second_jobs = sorted(
                (job["job_id"], job["title"], job["location"], job["url"])
                for job in second["normalized_job_evidence"]
            )
            self.assertEqual(first_jobs, second_jobs)
            self.assertEqual(len(first_jobs), item["jobs_normalized"])
            for job_id, title, location, url in first_jobs:
                self.assertTrue(job_id and title and location)
                self.assertTrue(url.startswith("https://"))

    def test_05_normalized_counts_and_current_student_hits(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "RBC": 20, "TD Bank": 20, "Scotiabank": 20, "BMO": 10,
            "CIBC": 5, "Wealthsimple": 20, "Thomson Reuters": 20,
            "Manulife": 10, "Sun Life": 2, "Ciena": 20, "Nokia Canada": 20,
            "Ericsson Canada": 10, "Xanadu": 20, "ApplyBoard": 20,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(by_name["CIBC"]["runs"][0]["incomplete_jobs_rejected"], 1)
        self.assertEqual(by_name["Sun Life"]["current_student_intern_hits"], 1)
        self.assertEqual(by_name["Ciena"]["current_student_intern_hits"], 3)
        self.assertEqual(by_name["Ciena"]["current_student_focus_hits"], 3)
        self.assertEqual(by_name["ApplyBoard"]["current_student_intern_hits"], 0)

    def test_06_ontario_and_canadian_location_evidence_is_explicit(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected = {
            "RBC": "TORONTO, Ontario, Canada", "TD Bank": "Ottawa, Ontario",
            "Scotiabank": "Toronto, ON, CA", "BMO": "Toronto, Ontario, Canada",
            "CIBC": "Toronto, ON", "Wealthsimple": "Remote (Canada)",
            "Thomson Reuters": "Toronto, Ontario", "Manulife": "Work At Home, Ontario, Canada",
            "Sun Life": "Toronto, Ontario", "Ciena": "Ottawa", "Nokia Canada": "Canada",
            "Ericsson Canada": "Toronto,Ontario,Canada", "Xanadu": "Toronto, ON, Canada",
        }
        for name, token in expected.items():
            combined = item_locations = (
                by_name[name]["ontario_locations_represented"]
                + by_name[name]["canadian_locations_represented"]
            )
            self.assertIn(token, combined, name)
        self.assertEqual(by_name["ApplyBoard"]["canadian_locations_represented"], [])

    def test_07_projections_and_original_master_membership(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 196)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 14)
        self.assertEqual(totals["validated"], 14)
        self.assertEqual(totals["excluded"], 1)
        self.assertEqual(totals["projected_canonical_production_count"], 288)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "196/246")

    def test_08_existing_adapters_are_reused_and_configs_are_exact(self):
        expected = {
            "RBC": "workday", "TD Bank": "workday", "Scotiabank": "sap_successfactors",
            "BMO": "phenom", "CIBC": "workday", "Wealthsimple": "ashby",
            "Thomson Reuters": "workday", "Manulife": "phenom", "Sun Life": "workday",
            "Ciena": "workday", "Nokia Canada": "oracle_recruiting",
            "Ericsson Canada": "eightfold", "Xanadu": "static_html", "ApplyBoard": "workday",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            CompanyTarget.from_dict(item)
        nokia = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Nokia Canada")
        self.assertEqual(nokia["site_number"], "CX_1")
        self.assertTrue(nokia["endpoint_url"].endswith("/recruitingCEJobRequisitions"))
        xanadu = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Xanadu")
        self.assertEqual(xanadu["listing_url"], "https://xanadu.applytojob.com/")
        self.assertEqual(xanadu["external_id_regex"], r"/apply/([^/]+)/")

    def test_09_ross_video_is_fail_closed_without_dayforce_support(self):
        result = next(item for item in self.report["results"] if item["company"] == "Ross Video")
        self.assertEqual(result["validation_status"], "unsupported_platform")
        self.assertIn("exact rossvideo Dayforce tenant", result["exclusion_reason"])
        self.assertIn("no CAPTCHA, WAF, or session path is used", result["exclusion_reason"])
        self.assertFalse(result["safe_to_activate"])
        self.assertNotIn("Ross Video", {item["name"] for item in self.validated})

    def test_10_hashes_ignore_and_publication_state(self):
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

    def test_11_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class OntarioCanadaExpansionV1BatchAContractTests(unittest.TestCase):
    def test_12_duplicate_detection_and_build_preserve_baseline(self):
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

    def test_13_complete_fields_official_hosts_and_unique_sources_are_required(self):
        entry = {"name": "Example", "allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(
            batch._verify_jobs(
                [fake_job("2", url="https://jobs.example.test/job/2"),
                 fake_job("1", url="https://jobs.example.test/job/1")],
                entry,
            ),
            [("1", "https://jobs.example.test/job/1"), ("2", "https://jobs.example.test/job/2")],
        )
        with self.assertRaisesRegex(AdapterError, "missing title"):
            batch._verify_jobs([fake_job(title="")], entry)
        with self.assertRaisesRegex(AdapterError, "missing location"):
            batch._verify_jobs([fake_job(location="")], entry)
        with self.assertRaisesRegex(AdapterError, "ambiguous company attribution"):
            job = fake_job()
            job.company = "Other"
            batch._verify_jobs([job], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([fake_job(url="https://evil.test/job/2")], entry)
        keys = [batch.source_key(item) for item in batch.CANDIDATE_ENTRIES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn(None, keys)


if __name__ == "__main__":
    unittest.main()
