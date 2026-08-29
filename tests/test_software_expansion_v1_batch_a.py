from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import unittest

from tests.company_careers_activation_milestones import (
    historical_parent_mapping_sha256,
)
from tests.company_careers_reliability_assertions import assert_historical_prefix
from pathlib import Path

import yaml

from scripts import validate_software_expansion_v1_batch_a as batch
from src.scraper.company_careers.adapters import ashby
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class FakeResponse:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse(self.payload)


class SoftwareExpansionV1BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_validated_registry_as_exact_prefix(self):
        self.assertGreaterEqual(len(self.production), 452)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 5)
        assert_historical_prefix(self, self.production, self.validated[:382])
        self.assertEqual(self.candidates, self.validated)
        self.assertEqual(len(self.candidates), 401)
        assert_historical_prefix(self, self.production, self.validated)
        self.assertFalse(self.report["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

        production_names = [item["name"] for item in self.production]
        for item in batch.CANDIDATE_ENTRIES:
            self.assertEqual(production_names.count(item["name"]), 1)
        self.assertNotIn("Grammarly", production_names)

    def test_02_all_requested_companies_and_outcomes_are_reported(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        self.assertEqual(len(self.report["validated_companies"]), 19)
        self.assertEqual(self.report["excluded_companies"], ["Grammarly"])
        self.assertNotIn("Grammarly", {item["name"] for item in self.candidates})

    def test_03_exact_sources_are_unique_and_configured(self):
        additions = self.candidates[382:]
        self.assertEqual([item["name"] for item in additions], [item["name"] for item in batch.CANDIDATE_ENTRIES])
        keys = [batch.registry_validator.source_key(item) for item in additions]
        self.assertTrue(all(keys))
        self.assertEqual(len(set(keys)), len(keys))
        for item in additions:
            CompanyTarget.from_dict(item)
            self.assertTrue(item["source_identity_regex"])
            self.assertTrue(item["allowed_apply_hosts"])
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)

    def test_04_repeat_runs_have_stable_ids_titles_locations_and_urls(self):
        additions = {item["name"]: item for item in self.candidates[382:]}
        for row in self.report["results"]:
            if not row["production_ready"]:
                self.assertEqual(row["runs"], [])
                continue
            self.assertEqual(len(row["runs"]), 2)
            first, second = row["runs"]
            self.assertTrue(first["success"] and second["success"])
            self.assertEqual(first["signature"], second["signature"])
            self.assertEqual(first["stable_ids"], second["stable_ids"])
            self.assertEqual(first["titles"], second["titles"])
            self.assertEqual(first["locations"], second["locations"])
            self.assertEqual(first["detail_apply_urls"], second["detail_apply_urls"])
            self.assertLessEqual(first["jobs_observed"], 20)
            for url in first["detail_apply_urls"]:
                host = (urlparse_host(url)).casefold()
                self.assertTrue(any(
                    host == value.casefold() or host.endswith("." + value.casefold())
                    for value in additions[row["company"]]["allowed_apply_hosts"]
                ))

    def test_05_current_job_student_summer_and_focus_counts(self):
        jobs = self.report["jobs"]
        self.assertEqual(jobs["normalized_total"], 275)
        self.assertEqual(jobs["student_intern_hits_total"], 6)
        self.assertEqual(jobs["summer_2027_hits_total"], 0)
        self.assertEqual(jobs["focus_role_hits_total"], 26)
        self.assertEqual(jobs["by_company"]["Appian"]["student_intern_hits"], 4)
        self.assertEqual(jobs["by_company"]["Samsara"]["student_intern_hits"], 1)
        self.assertEqual(jobs["by_company"]["Notion"]["student_intern_hits"], 1)

    def test_06_rebrand_parent_and_stale_routing_are_explicit(self):
        routing = self.report["routing_review"]
        self.assertEqual(set(routing), {"Grammarly", "Zoom", "Okta", "Miro"})
        self.assertIn("Superhuman", routing["Grammarly"])
        self.assertIn("Workvivo", routing["Zoom"])
        self.assertIn("Auth0", routing["Okta"])
        self.assertIn("stale", routing["Miro"])
        grammarly = next(row for row in self.report["results"] if row["company"] == "Grammarly")
        self.assertEqual(
            grammarly["validation_status"],
            "rebranded_parent_source_attribution_ambiguous",
        )
        self.assertFalse(grammarly["production_ready"])

    def test_07_zoom_contract_uses_exact_requisition_and_brand_fields(self):
        zoom = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Zoom")
        self.assertEqual(zoom["external_id_selector"], "li.job-component-requisition-identifier span")
        self.assertEqual(zoom["job_attribution_selector"], "li.job-component-dropdown-field-1 span")
        self.assertEqual(zoom["job_attribution_regex"], "Zoom")

    def test_08_projection_and_original_master_coverage(self):
        self.assertGreaterEqual(len(load_target_companies(str(batch.PRODUCTION))), 452)
        self.assertEqual(self.report["projection"], {
            "production_sources": 401,
            "source_delta": 19,
            "parent_mappings": 4,
            "mapping_delta": 0,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })
        self.assertTrue(all(not row["original_246_master"] for row in self.report["results"]))

    def test_09_hashes_ignore_and_safe_stage_manifest(self):
        validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        marker = b"  - name: HubSpot"
        historical_bytes = validated_bytes[:validated_bytes.index(marker)]
        self.assertEqual(
            hashlib.sha256(historical_bytes).hexdigest().upper(),
            self.report["production_registry_sha256"],
        )
        self.assertEqual(
            historical_parent_mapping_sha256(batch.PARENT_MAPPINGS),
            self.report["parent_mappings_sha256"],
        )
        self.assertEqual(self.report["safe_to_stage_files"], batch.SAFE_TO_STAGE_FILES)
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_10_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class SoftwareExpansionV1BatchAAdapterTests(unittest.TestCase):
    def test_11_ashby_bounds_and_prioritizes_late_student_roles(self):
        payload = {"jobs": [
            {
                "id": f"id-{index}",
                "title": (
                    "Software Engineering Intern"
                    if index == 24 else f"Software Engineer {index}"
                ),
                "location": "San Francisco, California",
                "jobUrl": f"https://jobs.ashbyhq.com/example/id-{index}",
                "publishedDate": "2026-08-14",
            }
            for index in range(25)
        ]}
        session = FakeSession(payload)
        target = CompanyTarget(
            name="Example", platform="ashby", slug="example", max_jobs=2,
            max_response_bytes=100_000, allowed_apply_hosts=["jobs.ashbyhq.com"],
        )
        jobs = ashby.fetch_jobs(session, target)
        self.assertEqual([job.job_id for job in jobs], ["id-24", "id-0"])
        self.assertEqual(session.ashby_pagination["candidate_jobs"], 1)
        self.assertEqual(session.ashby_pagination["jobs_returned"], 25)

    def test_12_ashby_rejects_cross_board_urls_and_unbounded_results(self):
        payload = {"jobs": [{
            "id": "one", "title": "Engineer", "location": "Remote",
            "jobUrl": "https://jobs.ashbyhq.com/other/one",
        }]}
        target = CompanyTarget(
            name="Example", platform="ashby", slug="example", max_jobs=20,
            allowed_apply_hosts=["jobs.ashbyhq.com"],
        )
        with self.assertRaisesRegex(AdapterError, "exact board-owned"):
            ashby.fetch_jobs(FakeSession(payload), target)
        with self.assertRaisesRegex(AdapterError, "cannot exceed"):
            ashby.fetch_jobs(FakeSession({"jobs": []}), CompanyTarget(
                name="Example", platform="ashby", slug="example", max_jobs=21,
                allowed_apply_hosts=["jobs.ashbyhq.com"],
            ))


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""


if __name__ == "__main__":
    unittest.main()
