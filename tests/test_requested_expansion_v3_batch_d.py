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

from scripts import validate_requested_expansion_v3_batch_d as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {"Hugging Face", "Blackline Safety"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class RequestedExpansionV3BatchDArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: IBM")]
        cls.historical = (yaml.safe_load(cls.historical_bytes) or {})["target_companies"]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_exact_230_source_baseline(self):
        self.assertEqual(len(self.production), 339)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), 230)
        self.assertEqual(self.production[:230], self.historical)
        self.assertEqual(self.candidates[:230], self.historical)
        self.assertEqual(self.validated[:230], self.historical)
        self.assertTrue(self.production_bytes.startswith(self.historical_bytes))
        self.assertTrue(self.production_bytes.startswith(self.validated_bytes))
        self.assertEqual(self.candidate_bytes, self.validated_bytes)
        self.assertTrue(self.report["candidate_registry_has_exact_230_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_230_entry_prefix"])

    def test_02_candidate_and_validated_registries_contain_only_safe_additions(self):
        self.assertEqual(len(self.candidates), 243)
        self.assertEqual(len(self.validated), 243)
        self.assertEqual([item["name"] for item in self.candidates[230:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[230:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[230:243]], VALIDATED)
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.production}))
        self.assertEqual(len({item["name"].casefold() for item in self.candidates}), 243)

    def test_03_review_results_and_required_report_fields(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([item["company"] for item in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {
            "company", "official_careers_url", "canadian_identity_location_evidence",
            "canada_us_remote_student_compatibility", "platform", "adapter", "result",
            "jobs_normalized", "current_student_intern_hits", "current_student_role_evidence",
            "canadian_locations_represented", "exclusion_reason", "original_246_master",
            "stable_id_result", "apply_url_result",
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
            "IBM": 20, "Applied Intuition": 20, "Apptronik": 20, "1X Technologies": 20,
            "Nuro": 20, "Kodiak Robotics": 20, "Gatik": 20, "Physical Intelligence": 20,
            "Serve Robotics": 20, "Scale AI": 20, "Renesas Electronics": 6,
            "Silicon Labs": 20, "TEKTELIC Communications": 2,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        expected_hits = {
            "IBM": 20, "Applied Intuition": 5, "Apptronik": 1, "1X Technologies": 1,
            "Nuro": 1, "Kodiak Robotics": 0, "Gatik": 0, "Physical Intelligence": 1,
            "Serve Robotics": 0, "Scale AI": 2, "Renesas Electronics": 0,
            "Silicon Labs": 0, "TEKTELIC Communications": 0,
        }
        self.assertEqual({name: by_name[name]["current_student_intern_hits"] for name in VALIDATED}, expected_hits)
        titles = {item["title"] for item in by_name["Applied Intuition"]["current_student_role_evidence"]}
        self.assertIn("Research Intern - Reinforcement Learning, Robotics", titles)
        self.assertEqual(
            [item["title"] for item in by_name["Physical Intelligence"]["current_student_role_evidence"]],
            ["Research Internships"],
        )

    def test_06_canadian_and_calgary_evidence_is_exact(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(
            by_name["Gatik"]["canadian_locations_represented"],
            ["Ajax, Ontario, Canada", "Mississauga, Ontario, Canada"],
        )
        ibm_titles = " | ".join(item["title"] for item in by_name["IBM"]["current_student_role_evidence"])
        self.assertIn("Toronto, Calgary, Montreal", ibm_titles)
        self.assertIn("Canada and Calgary filters", batch.CANADA_RELEVANCE["TEKTELIC Communications"])
        self.assertIn("two current roles are in Kyiv and Germany", batch.CANADA_RELEVANCE["TEKTELIC Communications"])
        self.assertIn("Calgary, Canada as headquarters", batch.CANADA_RELEVANCE["Blackline Safety"])

    def test_07_master_membership_and_projections(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 208)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 13)
        self.assertEqual(totals["validated"], 13)
        self.assertEqual(totals["excluded"], 2)
        self.assertEqual(totals["projected_canonical_production_count"], 243)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "186/246")
        self.assertEqual(totals["original_master_companies_recovered_names"], [])

    def test_08_platforms_reuse_existing_registered_adapters(self):
        expected = {
            "IBM": "ibm_search", "Applied Intuition": "ashby", "Apptronik": "greenhouse",
            "1X Technologies": "ashby", "Nuro": "greenhouse", "Kodiak Robotics": "greenhouse",
            "Gatik": "greenhouse", "Physical Intelligence": "ashby", "Serve Robotics": "ashby",
            "Scale AI": "greenhouse", "Renesas Electronics": "static_html",
            "Silicon Labs": "workday", "TEKTELIC Communications": "static_html",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            CompanyTarget.from_dict(item)

    def test_09_shared_ibm_source_preserves_exact_attribution(self):
        ibm = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "IBM")
        self.assertEqual(ibm["search_terms"], ["intern"])
        self.assertEqual(ibm["allowed_apply_hosts"], ["careers.ibm.com"])
        self.assertIn("HashiCorp-only coverage is not substituted", batch.CANADA_RELEVANCE["IBM"])
        result = next(item for item in self.report["results"] if item["company"] == "IBM")
        self.assertTrue(all(url.startswith("https://careers.ibm.com/careers/JobDetail?jobId=")
                            for url in result["runs"][0]["apply_urls"]))
        production = {item["name"]: item for item in self.production}
        self.assertEqual(production["HashiCorp"]["search_terms"], ["hashicorp"])
        self.assertNotIn("IBM", batch.PARENT_MAPPINGS.read_text(encoding="utf-8"))

    def test_10_gatik_covers_exact_canada_and_us_board(self):
        item = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Gatik")
        self.assertEqual(item["slug"], "gatikaiinc")
        self.assertEqual(item["identity_url"], "https://archive.gatik.ai/careers/")
        result = next(row for row in self.report["results"] if row["company"] == "Gatik")
        self.assertTrue(all("/gatikaiinc/jobs/" in url for url in result["runs"][0]["apply_urls"]))
        evidence = batch.CANADA_RELEVANCE["Gatik"]
        for token in ("Ontario", "California", "Texas", "Arkansas", "Arizona"):
            self.assertIn(token, evidence)

    def test_11_physical_intelligence_and_serve_sources_are_company_specific(self):
        physical = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Physical Intelligence")
        serve = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Serve Robotics")
        self.assertEqual(physical["identity_url"], "https://www.pi.website/join-us")
        self.assertEqual(physical["slug"], "physicalintelligence")
        self.assertEqual(serve["identity_url"], "https://www.serverobotics.com/careers/index.html")
        self.assertEqual(serve["slug"], "serverobotics")
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertTrue(all("/physicalintelligence/" in url for url in by_name["Physical Intelligence"]["runs"][0]["apply_urls"]))
        self.assertTrue(all("/serverobotics/" in url for url in by_name["Serve Robotics"]["runs"][0]["apply_urls"]))

    def test_12_tektelic_and_blackline_company_boundaries_are_explicit(self):
        tektelic = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "TEKTELIC Communications")
        self.assertEqual(tektelic["listing_url"], "https://tektelic.com/career/vacancies/")
        result = next(item for item in self.report["results"] if item["company"] == "TEKTELIC Communications")
        self.assertEqual(
            {url.rsplit("/", 2)[-2] for url in result["runs"][0]["apply_urls"]},
            {"lead-generation-manager-position", "director-of-sales-dach"},
        )
        blackline = next(item for item in self.report["results"] if item["company"] == "Blackline Safety")
        self.assertIn("exact blacklinesafety Workable account", blackline["exclusion_reason"])
        self.assertIn("Calgary and US jobs", blackline["exclusion_reason"])
        self.assertNotIn("Blackline Safety", batch.PARENT_MAPPINGS.read_text(encoding="utf-8"))

    def test_13_workable_challenge_safeguards_remain_enabled(self):
        reasons = {item["company"]: item["exclusion_reason"] for item in self.report["results"]}
        for name in EXCLUDED:
            self.assertIn("challenge", reasons[name].casefold())
            self.assertIn("not", reasons[name].casefold())
        self.assertNotIn("workable", batch.scanner.ADAPTERS)

    def test_14_hashes_ignore_and_publication_state(self):
        self.assertEqual(hashlib.sha256(self.historical_bytes).hexdigest().upper(), self.report["production_registry_sha256"])
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


class RequestedExpansionV3BatchDContractTests(unittest.TestCase):
    def test_16_duplicate_detection_and_build_preserve_baseline(self):
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

    def test_17_stable_ids_official_apply_urls_and_unique_sources(self):
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
        keys = [batch.source_key(item) for item in batch.CANDIDATE_ENTRIES if batch.source_key(item)]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
