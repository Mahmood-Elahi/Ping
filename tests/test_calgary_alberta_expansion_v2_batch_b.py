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

from scripts import validate_calgary_alberta_expansion_v2_batch_b as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.adapters import bamboohr
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def fake_job(
    job_id: str = "2",
    title: str = "Software Engineering Intern",
    location: str = "Calgary, Alberta, Canada",
    url: str = "https://jobs.example.test/job/2",
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        title=title,
        company="Example",
        location=location,
        url=url,
    )


class CalgaryAlbertaExpansionV2BatchBArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_artifacts_preserve_exact_298_entry_production_prefix(self):
        self.assertEqual(len(self.production), 298)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.candidates), 302)
        self.assertEqual(len(self.validated), 302)
        self.assertEqual(self.candidates[:298], self.production)
        self.assertEqual(self.validated[:298], self.production)
        self.assertTrue(batch.CANDIDATE_REGISTRY.read_bytes().startswith(self.production_bytes))
        self.assertTrue(batch.VALIDATED_REGISTRY.read_bytes().startswith(self.production_bytes))
        self.assertTrue(self.report["candidate_registry_has_exact_298_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_298_entry_prefix"])

    def test_02_candidate_and_validated_artifacts_add_only_the_safe_four(self):
        self.assertEqual([item["name"] for item in self.candidates[298:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[298:]], VALIDATED)
        self.assertEqual(
            VALIDATED,
            ["Enerflex", "Symend", "Circle Cardiovascular Imaging", "ATB Financial"],
        )
        candidate_names = [item["name"] for item in self.candidates]
        self.assertTrue(EXCLUDED.isdisjoint(candidate_names))
        self.assertEqual(len({name.casefold() for name in candidate_names}), 302)

    def test_03_report_and_inventory_cover_the_exact_requested_order(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual(
            [row["company"] for row in self.report["results"]],
            batch.REVIEWED_COMPANIES,
        )
        safe = [row["company"] for row in self.report["results"] if row["safe_to_activate"]]
        excluded = {
            row["company"] for row in self.report["results"] if not row["safe_to_activate"]
        }
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)

    def test_04_report_has_exact_attribution_and_location_contract_fields(self):
        required = {
            "company", "official_careers_url", "company_attribution_evidence",
            "calgary_alberta_relevance", "student_compatibility", "platform", "adapter",
            "result", "jobs_normalized", "current_student_intern_hits",
            "current_focus_role_hits", "current_student_focus_hits",
            "calgary_alberta_locations_represented", "canadian_locations_represented",
            "validation_status", "stable_id_result", "title_result", "location_result",
            "apply_url_result", "exclusion_reason",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item), item["company"])
            self.assertTrue(item["official_careers_url"].startswith("https://"))
            self.assertTrue(item["company_attribution_evidence"])
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_05_two_runs_are_bounded_and_all_normalized_fields_repeat(self):
        for item in self.report["results"]:
            self.assertEqual(len(item["runs"]), 2, item["company"])
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
            self.assertEqual(first_jobs, second_jobs, item["company"])
            self.assertEqual(len(first_jobs), item["jobs_normalized"])
            for job_id, title, location, url in first_jobs:
                self.assertTrue(job_id and title and location)
                self.assertTrue(url.startswith("https://"))

    def test_06_current_counts_student_hits_and_focus_hits(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(
            {name: by_name[name]["jobs_normalized"] for name in VALIDATED},
            {
                "Enerflex": 20,
                "Symend": 2,
                "Circle Cardiovascular Imaging": 10,
                "ATB Financial": 20,
            },
        )
        self.assertEqual(
            {name: by_name[name]["current_focus_role_hits"] for name in VALIDATED},
            {
                "Enerflex": 5,
                "Symend": 1,
                "Circle Cardiovascular Imaging": 5,
                "ATB Financial": 1,
            },
        )
        self.assertEqual(by_name["Enerflex"]["current_student_intern_hits"], 1)
        self.assertEqual(
            by_name["Enerflex"]["current_student_role_evidence"][0]["title"],
            "Estagiário em Mecânica",
        )
        self.assertTrue(
            all(by_name[name]["current_student_intern_hits"] == 0 for name in set(VALIDATED) - {"Enerflex"})
        )

    def test_07_calgary_and_alberta_location_evidence_is_preserved(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(
            by_name["Enerflex"]["calgary_alberta_locations_represented"],
            ["Calgary, AB, CA, T2G 0K3"],
        )
        self.assertEqual(
            by_name["Symend"]["calgary_alberta_locations_represented"],
            ["Calagary, AB, Canada"],
        )
        self.assertEqual(
            by_name["Circle Cardiovascular Imaging"]["calgary_alberta_locations_represented"],
            ["Calgary, Alberta, Canada"],
        )
        atb_locations = by_name["ATB Financial"]["calgary_alberta_locations_represented"]
        self.assertTrue(any("CALGARY, Alberta, Canada" in value for value in atb_locations))
        self.assertTrue(any("EDMONTON, Alberta, Canada" in value for value in atb_locations))

    def test_08_existing_registered_adapters_are_reused_with_strict_bounds(self):
        expected = {
            "Enerflex": "sap_successfactors",
            "Symend": "static_html",
            "Circle Cardiovascular Imaging": "bamboohr",
            "ATB Financial": "eightfold",
        }
        self.assertEqual(
            {item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES},
            expected,
        )
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.base.scanner.ADAPTERS)
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)
            self.assertEqual(item["max_response_bytes"], 3_000_000)
            CompanyTarget.from_dict(item)

    def test_09_exclusions_fail_closed_on_current_official_contracts(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_statuses = {
            "WestJet": "session_or_waf_dependency",
            "Eavor Technologies": "no_job_specific_apply_urls",
            "Attabotics": "no_current_jobs_for_id_apply_validation",
            "SMART Technologies": "session_or_waf_dependency",
            "Blackline Safety": "captcha_dependency",
            "Parkland Corporation": "waf_dependency",
            "AltaLink": "captcha_dependency",
            "Critical Mass": "structured_attribution_required",
            "Summit Nanotech": "no_current_job_contract",
            "Carbon Upcycling": "no_current_jobs_and_unsupported_platform",
            "E3 Lithium": "no_job_specific_apply_urls",
        }
        self.assertEqual(
            {name: by_name[name]["validation_status"] for name in EXCLUDED},
            expected_statuses,
        )
        for name in EXCLUDED:
            self.assertFalse(by_name[name]["safe_to_activate"])
            self.assertTrue(by_name[name]["exclusion_reason"])
        for name, marker in {
            "WestJet": "HTTP 403",
            "SMART Technologies": "HTTP 403",
            "Blackline Safety": "Turnstile",
            "Parkland Corporation": "Cloudflare",
            "AltaLink": "Turnstile",
            "Critical Mass": "misattribute",
        }.items():
            self.assertIn(marker, by_name[name]["exclusion_reason"])

    def test_10_projection_and_original_master_coverage_match_baseline(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 196)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual((totals["production_sources"], totals["parent_mappings"]), (298, 4))
        self.assertEqual((totals["reviewed"], totals["validated"], totals["excluded"]), (15, 4, 11))
        self.assertEqual(totals["projected_canonical_production_count"], 302)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "196/246")
        self.assertEqual((totals["fresh_targets"], totals["recovery_targets"]), (7, 8))

    def test_11_sources_are_unique_and_apply_urls_are_exact_and_allowlisted(self):
        keys = [batch.source_key(item) for item in self.candidates]
        present = [key for key in keys if key]
        self.assertEqual(len(present), len(set(present)))
        for result in self.report["results"]:
            if not result["safe_to_activate"]:
                continue
            self.assertEqual(result["stable_id_result"], "stable_across_two_runs")
            self.assertEqual(result["title_result"], "present_across_two_runs")
            self.assertEqual(result["location_result"], "present_across_two_runs")
            self.assertEqual(
                result["apply_url_result"],
                "stable_official_and_job_specific_across_two_runs",
            )

    def test_12_hash_ignore_publication_state_and_opportunities(self):
        self.assertEqual(
            hashlib.sha256(self.production_bytes).hexdigest().upper(),
            self.report["production_registry_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
            self.report["parent_mappings_sha256"],
        )
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))
        opportunities = {item["platform"] for item in self.report["adapter_opportunities"]}
        self.assertEqual(
            opportunities,
            {
                "BambooHR location fallback", "Dayforce", "Workable",
                "Critical Mass shared Workday attribution", "Deel",
            },
        )

    def test_13_validator_imports_no_persistence_publisher_browser_or_bypass(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = (
            "supabase", "gspread", "sqlite3", "update_student_dashboard",
            "run_student_job_scan", "browser_rendered_html", "selenium", "playwright",
        )
        self.assertFalse(
            any(any(token in module for token in forbidden) for module in modules)
        )


class CalgaryAlbertaExpansionV2BatchBContractTests(unittest.TestCase):
    def test_14_duplicate_detection_and_build_preserve_baseline_and_module_state(self):
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
        inherited_production = batch.base.PRODUCTION
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
                    production,
                    destination,
                    [{"name": "Existing", "platform": "greenhouse", "slug": "other"}],
                )
            self.assertEqual(production.read_bytes(), before)
            self.assertFalse(destination.exists())
        self.assertIs(batch.base.PRODUCTION, inherited_production)

    def test_15_complete_fields_company_attribution_and_official_hosts_are_required(self):
        entry = {"name": "Example", "allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(
            batch._verify_jobs(
                [
                    fake_job("2", url="https://jobs.example.test/job/2"),
                    fake_job("1", url="https://jobs.example.test/job/1"),
                ],
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

    def test_16_bamboohr_falls_back_to_the_exact_public_location_object(self):
        target = CompanyTarget.from_dict(
            next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Circle Cardiovascular Imaging")
        )
        contract = {
            "slug": "circlecardiovascularimaging",
            "careers_url": "https://circlecardiovascularimaging.bamboohr.com/careers",
        }
        job = bamboohr._normalize(
            {"atsLocation": {}},
            {
                "jobOpeningName": "Software Developer",
                "atsLocation": {},
                "location": {
                    "city": "Calgary",
                    "state": "Alberta",
                    "addressCountry": "Canada",
                },
            },
            target,
            contract,
            "215",
        )
        self.assertEqual(job.location, "Calgary, Alberta, Canada")
        self.assertEqual(job.offices, ["Calgary, Alberta, Canada"])
        self.assertEqual(
            job.url,
            "https://circlecardiovascularimaging.bamboohr.com/careers/215",
        )


if __name__ == "__main__":
    unittest.main()
