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

from scripts import validate_requested_expansion_v3_batch_b as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {"CGI", "Constellation Software", "Sierra Wireless"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class RequestedExpansionV3BatchBArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: Cohere")]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_exact_207_entry_production_prefix_is_preserved(self):
        self.assertEqual(len(self.production), 274)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertTrue(self.production_bytes.startswith(self.historical_bytes))
        self.assertEqual(self.production[:216], self.validated)
        self.assertEqual(self.candidates, self.production[:216])
        self.assertTrue(self.report["candidate_registry_has_exact_207_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_207_entry_prefix"])
        self.assertEqual(self.production[:207], self.validated[:207])

    def test_02_candidate_and_validated_counts(self):
        self.assertEqual(len(self.candidates), 216)
        self.assertEqual(len(self.validated), 216)
        self.assertEqual([item["name"] for item in self.candidates[207:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[207:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[207:216]], VALIDATED)
        production_names = {item["name"] for item in self.production}
        for name in VALIDATED:
            self.assertEqual([item["name"] for item in self.production].count(name), 1)
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))
        self.assertTrue(EXCLUDED.isdisjoint(production_names))

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
            "jobs_normalized", "current_student_intern_hits", "canadian_locations_represented",
            "exclusion_reason", "original_246_master", "stable_id_result", "apply_url_result",
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
                self.assertEqual(first["student_hits"], second["student_hits"])
                self.assertEqual(first["canadian_locations"], second["canadian_locations"])

    def test_05_normalized_counts_and_current_student_hits(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "Cohere": 20, "Waabi": 20, "OpenText": 10, "Celestica": 20,
            "Kinaxis": 20, "Descartes Systems Group": 20, "TELUS": 20,
            "BCE / Bell": 10, "Absolute Software": 20,
        }
        self.assertEqual(
            {name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts,
        )
        expected_hits = {
            "Cohere": [
                "Software Engineer Intern (Fall / Winter 2026)",
                "Research Internship (Fall, Winter 2026)",
                "Machine Learning Intern/Co-op  (Winter 2027)",
            ],
            "Waabi": ["2026 Intern, PhD Research Scientist"],
            "Descartes Systems Group": ["Software R&D Intern"],
        }
        for name, item in by_name.items():
            titles = [hit["title"] for hit in item["current_student_role_evidence"]]
            self.assertEqual(titles, expected_hits.get(name, []))
            self.assertEqual(item["current_student_intern_hits"], len(titles))

    def test_06_canadian_locations_are_reported_without_foreign_false_positives(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_tokens = {
            "Cohere": "Calgary", "Waabi": "Toronto, ON", "OpenText": "Waterloo",
            "Celestica": "Toronto, ON, CA", "Kinaxis": "CA-Remote",
            "Descartes Systems Group": "Waterloo, ON, CA", "TELUS": "Edmonton, AB, CA",
            "BCE / Bell": "Calgary, Alberta", "Absolute Software": "Vancouver, British Columbia",
        }
        for name, token in expected_tokens.items():
            self.assertIn(token, " | ".join(by_name[name]["canadian_locations_represented"]))
        descartes = " | ".join(by_name["Descartes Systems Group"]["canadian_locations_represented"])
        self.assertNotIn("Zilina", descartes)
        self.assertNotIn("Amersfoort", descartes)

    def test_07_master_membership_and_projection(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        requested = {row["company"] for row in master.rows}.intersection(batch.REVIEWED_COMPANIES)
        self.assertEqual(requested, set())
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 196)
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 9)
        self.assertEqual(totals["validated"], 9)
        self.assertEqual(totals["excluded"], 3)
        self.assertEqual(totals["projected_canonical_production_count"], 216)
        self.assertEqual(totals["original_master_companies_recovered_names"], [])
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "186/246")
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))

    def test_08_platforms_reuse_existing_adapters(self):
        expected = {
            "Cohere": "ashby", "Waabi": "lever", "OpenText": "phenom",
            "Celestica": "sap_successfactors", "Kinaxis": "icims",
            "Descartes Systems Group": "sap_successfactors", "TELUS": "sap_successfactors",
            "BCE / Bell": "phenom", "Absolute Software": "static_html",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)
            CompanyTarget.from_dict(item)

    def test_09_waabi_attribution_is_exact(self):
        waabi = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Waabi")
        self.assertEqual(waabi["slug"], "waabi")
        self.assertEqual(waabi["identity_url"], "https://jobs.lever.co/waabi")
        self.assertEqual(waabi["allowed_apply_hosts"], ["jobs.lever.co"])
        self.assertIn("Waabi Innovation Inc.", batch.CANADA_CALGARY_RELEVANCE["Waabi"])
        result = next(item for item in self.report["results"] if item["company"] == "Waabi")
        self.assertTrue(all(run["identity"]["url"] == waabi["identity_url"] for run in result["runs"]))
        self.assertTrue(all("/waabi/" in url for url in result["runs"][0]["apply_urls"]))

    def test_10_bce_bell_relationship_preserves_bell_attribution(self):
        bell = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "BCE / Bell")
        self.assertEqual(bell["ref_num"], "BECACA")
        self.assertEqual(bell["allowed_apply_hosts"], ["jobs.bell.ca"])
        self.assertIn("100%-owned BCE Inc. subsidiary", batch.CANADA_CALGARY_RELEVANCE["BCE / Bell"])
        self.assertIn("only Bell-attributed jobs", batch.CANADA_CALGARY_RELEVANCE["BCE / Bell"])
        result = next(item for item in self.report["results"] if item["company"] == "BCE / Bell")
        self.assertTrue(all("jobs.bell.ca/ca/en/job/" in url for url in result["runs"][0]["apply_urls"]))
        mapping_text = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        self.assertNotIn("BCE / Bell", mapping_text)
        self.assertNotIn("Sierra Wireless", mapping_text)

    def test_11_acquisition_and_shared_source_exclusions_are_safe(self):
        reasons = {item["company"]: item["exclusion_reason"] for item in self.report["results"]}
        self.assertIn("Radware hCaptcha", reasons["CGI"])
        self.assertIn("operate autonomously", reasons["Constellation Software"])
        self.assertIn("Subsidiary boards are not inferred", reasons["Constellation Software"])
        self.assertIn("January 12, 2023", reasons["Sierra Wireless"])
        self.assertIn("no Semtech parent mapping is inferred", reasons["Sierra Wireless"])
        absolute = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Absolute Software")
        self.assertEqual(absolute["listing_url"], "https://jobs.jobvite.com/absolute/jobs")
        self.assertIn("Absolute Security", batch.CANADA_CALGARY_RELEVANCE["Absolute Software"])
        self.assertIn("Crosspoint", batch.CANADA_CALGARY_RELEVANCE["Absolute Software"])

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


class RequestedExpansionV3BatchBContractTests(unittest.TestCase):
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

    def test_16_candidate_sources_are_unique_and_exclusions_never_enter_yaml(self):
        keys = [batch.source_key(item) for item in batch.CANDIDATE_ENTRIES]
        self.assertNotIn(None, keys)
        self.assertEqual(len(keys), len(set(keys)))
        candidate_names = {item["name"] for item in entries(batch.CANDIDATE_REGISTRY)}
        self.assertTrue(EXCLUDED.isdisjoint(candidate_names))


if __name__ == "__main__":
    unittest.main()
