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

from scripts import validate_canada_high_fit_expansion_v1_batch_a as batch
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {"CN Rail", "Groq"}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


def normalized_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def fake_job(
    job_id: str = "jr1",
    title: str = "Software Engineering Intern",
    location: str = "Toronto, Ontario, Canada",
    url: str | None = None,
    company: str = "ecobee",
    departments: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        url=url or f"https://careers.generac.com/en/jobs/{job_id}/software-engineering-intern/",
        departments=["ecobee"] if departments is None else departments,
    )


class CanadaHighFitExpansionV1BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: ecobee")]
        cls.historical = entries_from_bytes(cls.historical_bytes)
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_exact_302_entry_production_prefix(self):
        self.assertEqual(len(self.production), 339)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), 302)
        assert_historical_prefix(self, self.production, self.historical)
        self.assertEqual(self.candidates[:302], self.historical)
        self.assertEqual(self.validated[:302], self.historical)
        assert_historical_registry_bytes(self, self.production_bytes, self.historical_bytes)
        self.assertEqual(
            [item["name"] for item in self.production[:315]],
            [item["name"] for item in self.validated],
        )
        self.assertTrue(self.report["candidate_registry_has_exact_302_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_302_entry_prefix"])

    def test_02_candidate_validated_and_production_add_safe_sources_once(self):
        self.assertEqual(len(self.candidates), 315)
        self.assertEqual(len(self.validated), 315)
        self.assertEqual([item["name"] for item in self.candidates[302:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[302:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[302:315]], VALIDATED)
        production_names = [item["name"] for item in self.production]
        later_promotions = {"CN Rail"}
        self.assertTrue((EXCLUDED - later_promotions).isdisjoint(production_names))
        for name in later_promotions:
            self.assertEqual(production_names.count(name), 1)
        for name in VALIDATED:
            self.assertEqual(production_names.count(name), 1)
        self.assertEqual(len({name.casefold() for name in production_names}), 339)

    def test_03_all_requested_companies_and_outcomes_are_reported(self):
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
            "current_student_focus_hits", "canadian_locations_represented",
            "exclusion_reason", "stable_id_result", "title_result", "location_result",
            "apply_url_result",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_two_runs_are_bounded_and_repeat_all_normalized_fields(self):
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
            self.assertTrue(all(
                job_id and title and location and url.startswith("https://")
                for job_id, title, location, url in first_jobs
            ))

    def test_05_normalized_counts_and_current_student_hits(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "ecobee": 5, "Jobber": 20, "Cerebras Systems": 20,
            "Cisco": 20, "GE Aerospace": 10, "Bombardier": 20,
            "Fortinet": 20, "Zebra Technologies": 20, "SAP": 20,
            "ServiceNow": 5, "Affirm": 20, "Palo Alto Networks": 15,
            "Nordic Semiconductor": 13,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(by_name["GE Aerospace"]["current_student_intern_hits"], 2)
        self.assertEqual(by_name["GE Aerospace"]["runs"][0]["pages_requested"], 1)
        self.assertTrue(all(
            by_name[name]["current_student_intern_hits"] == 0
            for name in VALIDATED if name != "GE Aerospace"
        ))

    def test_06_canadian_location_evidence_is_explicit(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected = {
            "ecobee": "Toronto, ON, Canada",
            "Jobber": "Toronto",
            "Cerebras Systems": "Toronto Office",
            "Cisco": "Kanata, Ontario, Canada",
            "Bombardier": "Dorval, QC, CAN, H4S 1Y9",
            "Fortinet": "Burnaby, BC, Canada",
            "Zebra Technologies": "Mississauga, Canada",
            "SAP": "Toronto, ON, CA, M5K 1B7",
            "ServiceNow": "Toronto",
            "Affirm": "Remote Canada",
            "Palo Alto Networks": "Calgary, Alberta, Canada",
        }
        for name, token in expected.items():
            self.assertIn(token, by_name[name]["canadian_locations_represented"], name)
        self.assertEqual(by_name["GE Aerospace"]["canadian_locations_represented"], [])
        self.assertEqual(by_name["Nordic Semiconductor"]["canadian_locations_represented"], [])

    def test_07_projections_and_original_master_coverage(self):
        self.assertEqual(len(load_target_companies(str(batch.PRODUCTION))), 339)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 13)
        self.assertEqual(totals["validated"], 13)
        self.assertEqual(totals["excluded"], 2)
        self.assertEqual(totals["projected_canonical_production_count"], 315)
        self.assertEqual(totals["projected_parent_mappings"], 4)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "196/246")

    def test_08_existing_adapters_and_exact_platform_contracts_are_used(self):
        expected = {
            "ecobee": "static_html", "Jobber": "ashby", "Cerebras Systems": "ashby",
            "Cisco": "phenom", "GE Aerospace": "phenom",
            "Bombardier": "sap_successfactors", "Fortinet": "oracle_recruiting",
            "Zebra Technologies": "workday", "SAP": "sap_successfactors",
            "ServiceNow": "static_html", "Affirm": "greenhouse",
            "Palo Alto Networks": "static_html", "Nordic Semiconductor": "static_html",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            CompanyTarget.from_dict(item)
        by_name = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
        self.assertEqual(by_name["Jobber"]["slug"], "jobber")
        self.assertEqual(by_name["Cerebras Systems"]["slug"], "cerebras")
        self.assertEqual(by_name["Fortinet"]["site_number"], "CX_2001")
        self.assertEqual(by_name["Bombardier"]["endpoint_variant"], "rmk_columnized")
        self.assertEqual(by_name["GE Aerospace"]["max_pages"], 1)

    def test_09_ecobee_requires_exact_structured_generac_brand_attribution(self):
        ecobee = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "ecobee")
        self.assertEqual(ecobee["listing_url"], "https://careers.generac.com/en/brands/ecobee/")
        self.assertIn("nth-child(3)", ecobee["department_selector"])
        self.assertEqual(
            batch._verify_jobs([fake_job()], ecobee),
            [("jr1", "https://careers.generac.com/en/jobs/jr1/software-engineering-intern/")],
        )
        with self.assertRaisesRegex(AdapterError, "exact structured Generac brand attribution"):
            batch._verify_jobs([fake_job(departments=["Engineering"])], ecobee)

    def test_10_cn_and_groq_fail_closed_with_specific_reasons(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(by_name["CN Rail"]["validation_status"], "unsupported_platform")
        self.assertIn("Cornerstone", by_name["CN Rail"]["exclusion_reason"])
        self.assertIn("no CAPTCHA, WAF, or session bypass", by_name["CN Rail"]["exclusion_reason"])
        self.assertEqual(by_name["Groq"]["validation_status"], "no_current_official_collection")
        self.assertIn("Historical", by_name["Groq"]["exclusion_reason"])
        self.assertEqual(len(self.report["adapter_opportunities"]), 5)

    def test_11_hashes_ignore_and_publication_state(self):
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

    def test_12_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class CanadaHighFitExpansionV1BatchAContractTests(unittest.TestCase):
    def test_13_duplicate_detection_and_build_preserve_baseline(self):
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

    def test_14_complete_fields_official_hosts_and_unique_sources_are_required(self):
        entry = {"name": "ecobee", "allowed_apply_hosts": ["careers.generac.com"]}
        jobs = [fake_job("jr2"), fake_job("jr1")]
        self.assertEqual(
            batch._verify_jobs(jobs, entry),
            [
                ("jr1", "https://careers.generac.com/en/jobs/jr1/software-engineering-intern/"),
                ("jr2", "https://careers.generac.com/en/jobs/jr2/software-engineering-intern/"),
            ],
        )
        with self.assertRaisesRegex(AdapterError, "missing title"):
            batch._verify_jobs([fake_job(title="")], entry)
        with self.assertRaisesRegex(AdapterError, "missing location"):
            batch._verify_jobs([fake_job(location="")], entry)
        with self.assertRaisesRegex(AdapterError, "ambiguous company attribution"):
            batch._verify_jobs([fake_job(company="Other")], entry)
        with self.assertRaisesRegex(AdapterError, "unofficial"):
            batch._verify_jobs([fake_job(url="https://evil.test/job/jr1")], entry)
        keys = [batch.source_key(item) for item in batch.CANDIDATE_ENTRIES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn(None, keys)


if __name__ == "__main__":
    unittest.main()
