from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

import yaml

from scripts import validate_alberta_canada_employer_expansion_v1_batch_a as batch
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import load_parent_source_mappings


def _entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class AlbertaCanadaEmployerExpansionV1BatchATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.production = _entries(batch.PRODUCTION)
        cls.candidates = _entries(batch.CANDIDATE_REGISTRY)
        cls.validated = _entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(encoding="utf-8", newline="") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_inventory_and_baselines_are_exact(self):
        self.assertEqual(len(batch.REVIEWED_COMPANIES), 20)
        self.assertEqual(len(set(batch.REVIEWED_COMPANIES)), 20)
        self.assertEqual((batch.BASELINE_SOURCES, batch.BASELINE_MAPPINGS), (344, 4))
        self.assertEqual((batch.BASELINE_COVERAGE, batch.MASTER_TOTAL), (208, 246))
        self.assertEqual(len(self.production), 353)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(
            set(batch.REVIEWED_COMPANIES),
            {entry["name"] for entry in batch.CANDIDATE_ENTRIES}
            | {entry["company"] for entry in batch.EXCLUDED},
        )

    def test_activation_matches_validated_registry_exactly(self):
        self.assertEqual(self.candidate_bytes, self.validated_bytes)
        baseline_prefix = self.validated_bytes[:self.validated_bytes.index(b"  - name: Air Canada")]
        self.assertEqual(self.production_bytes[:len(baseline_prefix)], baseline_prefix)
        self.assertEqual(
            self.production_bytes.replace(b"\r\n", b"\n"),
            self.validated_bytes.replace(b"\r\n", b"\n"),
        )
        self.assertEqual(self.production, self.validated)
        self.assertEqual(len(self.candidates), 353)
        self.assertEqual(
            [entry["name"] for entry in self.candidates[344:]],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )
        production_names = [entry["name"] for entry in self.production]
        for name in batch.REVIEWED_COMPANIES:
            self.assertEqual(production_names.count(name), int(name in {
                entry["name"] for entry in batch.CANDIDATE_ENTRIES
            }))

    def test_candidates_reuse_supported_bounded_anonymous_contracts(self):
        self.assertEqual(
            {entry["platform"] for entry in batch.CANDIDATE_ENTRIES},
            {
                "ashby", "bamboohr", "greenhouse", "phenom",
                "sap_successfactors", "static_html", "workday",
            },
        )
        for entry in batch.CANDIDATE_ENTRIES:
            with self.subTest(company=entry["name"]):
                self.assertIsNone(registry_validator.configuration_error(entry))
                self.assertEqual(entry["max_pages"], 1)
                self.assertLessEqual(entry["max_jobs"], 20)
                self.assertTrue(entry["identity_url"].startswith("https://"))
                self.assertTrue(entry["allowed_apply_hosts"])
                self.assertIsNone(entry["original_workbook_rank"])

    def test_report_has_exact_results_jobs_and_projection(self):
        self.assertEqual(
            self.report["validated_companies"],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )
        self.assertEqual(len(self.report["validated_companies"]), 9)
        self.assertEqual(len(self.report["excluded_companies"]), 11)
        self.assertEqual(self.report["jobs"]["normalized_total"], 101)
        self.assertEqual(self.report["jobs"]["student_intern_hits_total"], 0)
        self.assertEqual(self.report["projection"], {
            "production_sources": 353,
            "source_delta": 9,
            "parent_mappings": 4,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })
        self.assertFalse(self.report["production_registry_activated"])
        self.assertFalse(any(self.report["publication"].values()))

    def test_every_validated_result_has_repeatable_ids_urls_and_exact_attribution(self):
        for result in self.report["results"]:
            if not result["production_ready"]:
                continue
            with self.subTest(company=result["company"]):
                self.assertEqual(result["validation_status"], "repeatable_anonymous")
                self.assertEqual(result["stable_id_result"], "valid")
                self.assertEqual(result["apply_url_result"], "valid")
                self.assertEqual(result["company_attribution_result"], "exact")
                self.assertEqual(result["deterministic_result"], "stable_ordered_signature")
                self.assertEqual(len(result["runs"]), 2)
                self.assertEqual(result["runs"][0]["signature"], result["runs"][1]["signature"])
                self.assertEqual(result["runs"][0]["stable_ids"], result["runs"][1]["stable_ids"])
                self.assertEqual(result["runs"][0]["detail_apply_urls"], result["runs"][1]["detail_apply_urls"])

    def test_acquisitions_rebrands_and_parent_relationships_fail_closed(self):
        excluded = {row["company"]: row for row in self.report["results"] if not row["production_ready"]}
        for name in (
            "Shaw Communications", "Canadian Western Bank", "Solium Capital",
            "Latitude Technologies",
        ):
            self.assertIn(name, self.report["stale_acquired_companies"])
        self.assertIn("amalgamated", excluded["Shaw Communications"]["exclusion_reason"])
        self.assertIn("National Bank", excluded["Canadian Western Bank"]["exclusion_reason"])
        self.assertIn("Shareworks", excluded["Solium Capital"]["exclusion_reason"])
        self.assertIn("SKYTRAC", excluded["Latitude Technologies"]["exclusion_reason"])
        self.assertIn("autonomously", excluded["Constellation Software"]["exclusion_reason"])
        self.assertIn("mixes", excluded["Loblaw"]["exclusion_reason"])
        helm = next(item for item in self.report["ownership_reviews"] if item["company"] == "Helm Operations")
        self.assertEqual(helm["disposition"], "validated_exact_child_board")

    def test_captcha_and_waf_sources_remain_excluded_without_registration(self):
        excluded = {row["company"]: row for row in self.report["results"] if not row["production_ready"]}
        self.assertEqual(excluded["CGI"]["validation_status"], "captcha_blocked")
        self.assertIn("hCaptcha", excluded["CGI"]["exclusion_reason"])
        self.assertEqual(excluded["Nuvei"]["validation_status"], "captcha_blocked")
        self.assertIn("Turnstile", excluded["Nuvei"]["exclusion_reason"])
        self.assertNotIn("workable", batch.ADAPTERS)

    def test_nested_workday_facets_are_verified_structurally_and_by_count(self):
        target = CompanyTarget(
            name="Nested", platform="workday",
            applied_facets={"locationCountry": ["canada-id"]},
        )
        payload = {
            "total": 6,
            "facets": [{
                "facetParameter": "locationMainGroup",
                "values": [{
                    "facetParameter": "locationCountry",
                    "values": [{"id": "canada-id", "descriptor": "Canada", "count": 6}],
                }],
            }],
        }
        workday._verify_applied_facets(payload, target)
        payload["total"] = 7
        with self.assertRaisesRegex(AdapterError, "conclusively apply"):
            workday._verify_applied_facets(payload, target)

    def test_scope_logic_accepts_exact_canada_facets_and_rejects_other_geography(self):
        canada_facet = next(entry for entry in batch.CANDIDATE_ENTRIES if entry["name"] == "Solera Holdings")
        ambiguous_workday_location = JobRecord(
            job_id="1", title="Developer", company="Solera Holdings",
            location="3 Locations", url="https://solera.wd5.myworkdayjobs.com/job/1",
            updated_at="", source="workday:solera",
        )
        self.assertTrue(batch._in_scope(ambiguous_workday_location, canada_facet))
        uk_job = JobRecord(
            job_id="2", title="Developer", company="Showbie",
            location="United Kingdom", url="https://showbie.bamboohr.com/careers/2",
            updated_at="", source="bamboohr:showbie",
        )
        showbie = next(entry for entry in batch.CANDIDATE_ENTRIES if entry["name"] == "Showbie")
        self.assertFalse(batch._in_scope(uk_job, showbie))

    def test_inventory_and_safe_to_stage_files_are_exact(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual(sum(row["production_ready"] == "True" for row in self.inventory), 9)
        self.assertEqual(self.report["safe_to_stage_files"], batch.SAFE_TO_STAGE_FILES)
        self.assertNotIn("config/target_companies.yaml", batch.SAFE_TO_STAGE_FILES)
        self.assertNotIn("config/company_parent_source_mappings.yaml", batch.SAFE_TO_STAGE_FILES)
        for relative in batch.SAFE_TO_STAGE_FILES:
            self.assertTrue((batch.ROOT / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
