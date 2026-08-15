import csv
import json
from pathlib import Path
import unittest

import yaml

from scripts import validate_alberta_energy_expansion_v1_batch_a as batch
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import static_html, workday
from src.scraper.company_careers.models import CompanyTarget


class _Response:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, url: str, content: bytes):
        self.url = url
        self.content = content


class _Session:
    def __init__(self, response: _Response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


class AlbertaEnergyExpansionV1BatchATests(unittest.TestCase):
    def test_inventory_and_baselines_are_exact(self):
        self.assertEqual(len(batch.REVIEWED_COMPANIES), 20)
        self.assertEqual(len(set(batch.REVIEWED_COMPANIES)), 20)
        self.assertEqual(batch.BASELINE_SOURCES, 339)
        self.assertEqual(batch.BASELINE_MAPPINGS, 4)
        self.assertEqual((batch.BASELINE_COVERAGE, batch.MASTER_TOTAL), (208, 246))
        self.assertEqual(
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
            [
                "Athabasca Oil", "Paramount Resources", "STEP Energy Services",
                "Calfrac Well Services", "Weir Group",
            ],
        )
        self.assertEqual(
            set(batch.REVIEWED_COMPANIES),
            {entry["name"] for entry in batch.CANDIDATE_ENTRIES}
            | {entry["company"] for entry in batch.EXCLUDED},
        )

    def test_candidates_reuse_supported_adapters_and_are_bounded(self):
        self.assertEqual(
            {entry["platform"] for entry in batch.CANDIDATE_ENTRIES},
            {"static_html", "adp", "ukg_pro", "paycor", "workday"},
        )
        for entry in batch.CANDIDATE_ENTRIES:
            with self.subTest(company=entry["name"]):
                self.assertIsNone(registry_validator.configuration_error(entry))
                self.assertEqual(entry["max_pages"], 1)
                self.assertLessEqual(entry["max_jobs"], 20)
                self.assertIsNone(entry["original_workbook_rank"])

    def test_activation_preserves_validated_registry_exactly(self):
        production = yaml.safe_load(batch.PRODUCTION.read_bytes())
        candidate = yaml.safe_load(batch.CANDIDATE_REGISTRY.read_bytes())
        validated = yaml.safe_load(batch.VALIDATED_REGISTRY.read_bytes())
        self.assertEqual(production["target_companies"][:344], validated["target_companies"])
        self.assertEqual(candidate, validated)
        payload = production
        self.assertEqual(len(payload["target_companies"]), 418)
        self.assertEqual(
            [entry["name"] for entry in payload["target_companies"][339:344]],
            [entry["name"] for entry in batch.CANDIDATE_ENTRIES],
        )
        production_names = [entry["name"] for entry in payload["target_companies"]]
        for name in batch.REVIEWED_COMPANIES:
            self.assertEqual(production_names.count(name), int(name in {
                entry["name"] for entry in batch.CANDIDATE_ENTRIES
            }))

    def test_report_and_csv_preserve_validation_milestone(self):
        report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        self.assertEqual(report["validated_companies"], [entry["name"] for entry in batch.CANDIDATE_ENTRIES])
        self.assertEqual(len(report["excluded_companies"]), 15)
        self.assertEqual(report["projection"]["production_sources"], 344)
        self.assertEqual(report["projection"]["parent_mappings"], 4)
        self.assertEqual(report["projection"]["original_master_coverage"], "208/246")
        self.assertFalse(report["production_registry_activated"])
        self.assertFalse(any(report["publication"].values()))
        self.assertEqual(report["safe_to_stage_files"], batch.SAFE_TO_STAGE_FILES)
        self.assertNotIn("config/target_companies.yaml", report["safe_to_stage_files"])
        with batch.CANDIDATE_CSV.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["company"] for row in rows], batch.REVIEWED_COMPANIES)
        self.assertEqual(sum(row["production_ready"] == "True" for row in rows), 5)

    def test_workday_path_is_the_stable_fallback_not_presentation_metadata(self):
        self.assertEqual(
            workday._stable_job_id(["Ontario"], "/job/Toronto/Engineer_R0039000"),
            "R0039000",
        )
        self.assertEqual(
            workday._stable_job_id(["Ontario"], "/job/Toronto/Engineer_Ontario"),
            "Engineer_Ontario",
        )
        self.assertEqual(
            workday._location("", ["Ontario"], "Engineer_Ontario", "/job/Toronto/Engineer_Ontario"),
            "Ontario",
        )

    def test_static_html_fixed_location_is_used_only_when_listing_has_none(self):
        url = "https://careers.example.test/jobs"
        response = _Response(
            url,
            b"<html><body><main class='jobs'><article data-id='7'>"
            b"<h2><a href='/jobs/7'>Project Engineer</a></h2>"
            b"</article></main></body></html>",
        )
        target = CompanyTarget.from_dict({
            "name": "Example Energy", "platform": "static_html", "careers_url": url,
            "listing_url": url, "endpoint_variant": "server_rendered_cards",
            "listing_selector": ".jobs", "job_card_selector": "article",
            "title_selector": "h2 a", "detail_link_selector": "h2 a",
            "external_id_attribute": "data-id", "fixed_location": "Calgary, Alberta, Canada",
            "allowed_apply_hosts": ["careers.example.test"], "max_pages": 1, "max_jobs": 20,
        })
        jobs = static_html.fetch_jobs(_Session(response), target)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Calgary, Alberta, Canada")
        self.assertEqual(jobs[0].offices, ["Calgary, Alberta, Canada"])


if __name__ == "__main__":
    unittest.main()
