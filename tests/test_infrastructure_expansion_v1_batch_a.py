from __future__ import annotations

import ast
import copy
import csv
import json
import subprocess
import unittest

from tests.company_careers_activation_milestones import (
    historical_parent_mapping_sha256,
)
from tests.company_careers_reliability_assertions import assert_historical_prefix
from tests.text_bytes import (
    canonical_sha256_for_legacy_text_sha256,
    canonical_text_sha256,
)
from pathlib import Path
from urllib.parse import urlparse

import yaml

from scripts import validate_infrastructure_expansion_v1_batch_a as batch
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class InfrastructureExpansionV1BatchAArtifactTests(unittest.TestCase):
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
        assert_historical_prefix(self, self.production, self.validated[:401])
        self.assertEqual(self.candidates, self.validated)
        self.assertEqual(len(self.candidates), 418)
        historical = copy.deepcopy(self.validated)
        next(item for item in historical if item["name"] == "CoreWeave")[
            "job_attribution_regex"
        ] = "CoreWeave|Weights & Biases"
        assert_historical_prefix(self, self.production, historical)
        self.assertFalse(self.report["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

        production_names = [item["name"] for item in self.production]
        for item in batch.CANDIDATE_ENTRIES:
            self.assertEqual(production_names.count(item["name"]), 1)
        for name in ("Equinix", "Fly.io", "Neon"):
            self.assertNotIn(name, production_names)

    def test_02_all_requested_companies_and_outcomes_are_reported(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        self.assertEqual(len(self.report["validated_companies"]), 17)
        self.assertEqual(self.report["excluded_companies"], ["Equinix", "Fly.io", "Neon"])
        names = {item["name"] for item in self.candidates}
        self.assertNotIn("Equinix", names)
        self.assertNotIn("Fly.io", names)
        self.assertNotIn("Neon", names)

    def test_03_exact_sources_are_unique_bounded_and_configured(self):
        additions = self.candidates[401:]
        self.assertEqual(
            [item["name"] for item in additions],
            [item["name"] for item in batch.CANDIDATE_ENTRIES],
        )
        keys = [registry_validator.source_key(item) for item in additions]
        self.assertTrue(all(keys))
        self.assertEqual(len(keys), len(set(keys)))
        for item in additions:
            CompanyTarget.from_dict(item)
            self.assertTrue(item["source_identity_regex"])
            self.assertTrue(item["allowed_apply_hosts"])
            self.assertEqual(item["max_pages"], 1)
            if item["platform"] == "oracle_recruiting":
                self.assertEqual(item["max_jobs"], 24)
            else:
                self.assertLessEqual(item["max_jobs"], 20)

    def test_04_repeat_runs_have_stable_ids_titles_locations_and_urls(self):
        additions = {item["name"]: item for item in self.candidates[401:]}
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
            self.assertLessEqual(first["jobs_observed"], additions[row["company"]]["max_jobs"])
            for url in first["detail_apply_urls"]:
                host = (urlparse(url).hostname or "").casefold()
                self.assertTrue(any(
                    host == value.casefold() or host.endswith("." + value.casefold())
                    for value in additions[row["company"]]["allowed_apply_hosts"]
                ))

    def test_05_current_job_student_summer_and_focus_counts(self):
        jobs = self.report["jobs"]
        self.assertEqual(jobs["normalized_total"], 230)
        self.assertEqual(jobs["student_intern_hits_total"], 1)
        self.assertEqual(jobs["summer_2027_hits_total"], 1)
        self.assertEqual(jobs["focus_role_hits_total"], 73)
        self.assertEqual(jobs["by_company"]["Sentry"]["student_intern_hits"], 1)
        sentry = next(row for row in self.report["results"] if row["company"] == "Sentry")
        self.assertEqual(
            sentry["current_student_role_evidence"],
            ["Software Engineer, Intern (Summer 2027)"],
        )

    def test_06_acquisitions_rebrands_stale_boards_and_parent_routing(self):
        routing = self.report["routing_review"]
        self.assertEqual(
            set(routing),
            {"Akamai", "CoreWeave", "Lambda", "Temporal", "Equinix", "Fly.io", "Neon"},
        )
        self.assertIn("Oracle", routing["Akamai"])
        self.assertIn("Weights & Biases", routing["CoreWeave"])
        self.assertIn("former Lambda Labs", routing["Lambda"])
        self.assertIn("Ashby mirror", routing["Temporal"])
        self.assertIn("WAF", routing["Equinix"])
        self.assertIn("no open positions", routing["Fly.io"])
        self.assertIn("Databricks", routing["Neon"])
        mapping_text = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        self.assertNotIn("child_company: Neon", mapping_text)

    def test_07_coreweave_shared_board_requires_exact_metadata(self):
        coreweave = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "CoreWeave")
        self.assertEqual(
            coreweave["job_attribution_field"],
            "greenhouse_metadata.Acquisition Company",
        )
        self.assertEqual(coreweave["job_attribution_regex"], "CoreWeave")
        self.assertEqual(coreweave["department_allowlist"], ["Technology"])
        row = next(row for row in self.report["results"] if row["company"] == "CoreWeave")
        for run in row["runs"]:
            self.assertGreater(run["pagination"]["attribution_filtered"], 0)

    def test_08_akamai_uses_official_branded_oracle_contract(self):
        akamai = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Akamai")
        self.assertEqual(akamai["platform"], "oracle_recruiting")
        self.assertEqual(akamai["site_number"], "CX_1")
        self.assertEqual(urlparse(akamai["careers_url"]).hostname, "jobs.akamai.com")
        self.assertTrue(akamai["endpoint_url"].endswith("recruitingCEJobRequisitions"))

    def test_09_projection_and_original_master_coverage(self):
        self.assertGreaterEqual(len(load_target_companies(str(batch.PRODUCTION))), 452)
        self.assertEqual(self.report["projection"], {
            "production_sources": 418,
            "source_delta": 17,
            "parent_mappings": 4,
            "mapping_delta": 0,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })
        self.assertTrue(all(not row["original_246_master"] for row in self.report["results"]))

    def test_10_hashes_ignore_and_safe_stage_manifest(self):
        validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        marker = b"  - name: DigitalOcean"
        historical_bytes = validated_bytes[:validated_bytes.index(marker)]
        self.assertEqual(
            canonical_text_sha256(historical_bytes),
            canonical_sha256_for_legacy_text_sha256(
                self.report["production_registry_sha256"]
            ),
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

    def test_11_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = (
            "supabase", "gspread", "sqlite3", "update_student_dashboard",
            "run_student_job_scan",
        )
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


if __name__ == "__main__":
    unittest.main()
