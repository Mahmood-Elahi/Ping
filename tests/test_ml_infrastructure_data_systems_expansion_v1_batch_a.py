from __future__ import annotations

import ast
import csv
import json
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlparse

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix
from tests.text_bytes import (
    canonical_sha256_for_legacy_text_sha256,
    canonical_text_bytes,
    canonical_text_sha256,
)

from scripts import validate_company_registry as registry_validator
from scripts import validate_ml_infrastructure_data_systems_expansion_v1_batch_a as batch
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class MlInfrastructureDataSystemsExpansionV1BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_matches_validated_artifacts_exactly(self):
        self.assertGreaterEqual(len(self.production), len(self.validated))
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 5)
        self.assertEqual(len(self.candidates), 452)
        self.assertEqual(self.candidates, self.validated)
        assert_historical_prefix(self, self.production, self.validated)
        historical = entries(batch.VALIDATED_REGISTRY)
        assert_historical_prefix(self, self.production, historical)
        self.assertEqual(
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
            load_parent_source_mappings(batch.VALIDATED_MAPPINGS),
        )
        self.assertEqual(len(load_parent_source_mappings(batch.CANDIDATE_MAPPINGS)), 5)
        self.assertEqual(
            batch.CANDIDATE_MAPPINGS.read_bytes(),
            batch.VALIDATED_MAPPINGS.read_bytes(),
        )
        self.assertFalse(self.report["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_02_all_requested_companies_have_one_fail_closed_outcome(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual(
            [row["company"] for row in self.report["results"]],
            batch.REVIEWED_COMPANIES,
        )
        self.assertEqual(len(self.report["validated_companies"]), 16)
        self.assertEqual(
            self.report["excluded_companies"],
            ["Replicate", "dbt Labs", "Elementl / Dagster", "Tecton"],
        )
        self.assertEqual(set(self.report["blockers"]), set(self.report["excluded_companies"]))

    def test_03_direct_sources_are_unique_bounded_and_reuse_adapters(self):
        additions = self.candidates[-15:]
        self.assertEqual(
            [item["name"] for item in additions],
            [item["name"] for item in batch.CANDIDATE_ENTRIES],
        )
        existing_keys = {
            key for item in self.production[:437]
            if (key := registry_validator.source_key(item)) is not None
        }
        keys = [registry_validator.source_key(item) for item in additions]
        self.assertTrue(all(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(existing_keys.intersection(keys))
        self.assertEqual(
            self.report["adapter_reuse"],
            ["ashby", "greenhouse", "javascript_embedded_data", "lever", "static_html"],
        )
        for item in additions:
            CompanyTarget.from_dict(item)
            self.assertTrue(item["source_identity_regex"])
            self.assertTrue(item["allowed_apply_hosts"])
            self.assertEqual(item["max_pages"], 1)
            self.assertLessEqual(item["max_jobs"], 20)
            self.assertIsNone(registry_validator.configuration_error(item))

    def test_04_repeat_runs_have_stable_ids_titles_locations_and_urls(self):
        configs = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
        configs["Weights & Biases"] = batch.WEIGHTS_BIASES_ENTRY
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
            for url in first["detail_apply_urls"]:
                host = (urlparse(url).hostname or "").casefold()
                self.assertTrue(any(
                    host == value.casefold() or host.endswith("." + value.casefold())
                    for value in configs[row["company"]]["allowed_apply_hosts"]
                ))

    def test_05_current_job_student_summer_and_focus_counts(self):
        self.assertEqual(self.report["jobs"]["normalized_total"], 171)
        self.assertEqual(self.report["jobs"]["student_intern_hits_total"], 1)
        self.assertEqual(self.report["jobs"]["summer_2027_hits_total"], 0)
        self.assertEqual(self.report["jobs"]["focus_role_hits_total"], 98)
        labelbox = self.report["jobs"]["by_company"]["Labelbox"]
        self.assertEqual(labelbox["student_intern_hits"], 1)
        self.assertEqual(labelbox["summer_2027_hits"], 0)
        evidence = next(
            row for row in self.report["results"] if row["company"] == "Labelbox"
        )["current_student_candidate_evidence"]
        self.assertEqual(evidence, [{
            "job_id": "5177341007",
            "title": "Cyber Security Intern",
            "location": "San Francisco Bay Area",
            "matching_rule": "strong_title:intern",
            "exclusion_reason": None,
            "production_match": True,
        }])

    def test_06_weights_and_biases_uses_exact_parent_metadata(self):
        coreweave = next(item for item in self.candidates if item["name"] == "CoreWeave")
        self.assertEqual(
            coreweave["job_attribution_field"],
            "greenhouse_metadata.Acquisition Company",
        )
        self.assertEqual(coreweave["job_attribution_regex"], "CoreWeave|Weights & Biases")
        mapping = next(
            item for item in load_parent_source_mappings(batch.CANDIDATE_MAPPINGS)
            if item.child_company == "Weights & Biases"
        )
        self.assertEqual(mapping.parent_company, "CoreWeave")
        self.assertEqual(
            mapping.match_field,
            "raw_metadata.greenhouse_metadata.Acquisition Company",
        )
        validation = self.report["parent_mapping_validation"]
        self.assertEqual(validation["canonical_source"], "greenhouse:coreweave")
        self.assertGreaterEqual(validation["mapped_jobs_observed"], 1)
        self.assertEqual(validation["mapped_stable_ids"], ["4658736006"])

    def test_07_redis_uses_bounded_next_flight_contract(self):
        redis = next(item for item in self.candidates if item["name"] == "Redis")
        self.assertEqual(redis["platform"], "javascript_embedded_data")
        self.assertEqual(redis["endpoint_variant"], "next_flight_collection")
        self.assertEqual(redis["embedded_json_path"], "positions")
        row = next(item for item in self.report["results"] if item["company"] == "Redis")
        for run in row["runs"]:
            self.assertTrue(all(
                job_id.startswith("embedded_data:redis.io:")
                for job_id in run["stable_ids"]
            ))
            self.assertLessEqual(run["pagination"]["scripts_inspected"], 20)
            self.assertLessEqual(run["pagination"]["maximum_results"], 20)

    def test_08_acquisition_rebrand_and_stale_board_review(self):
        routing = self.report["routing_review"]
        self.assertEqual(
            set(routing),
            {"Weights & Biases", "Replicate", "dbt Labs", "Elementl / Dagster", "Tecton"},
        )
        self.assertIn("CoreWeave", routing["Weights & Biases"])
        self.assertIn("Cloudflare", routing["Replicate"])
        self.assertIn("Fivetran", routing["dbt Labs"])
        self.assertIn("Prefect", routing["Elementl / Dagster"])
        self.assertIn("Databricks", routing["Tecton"])

    def test_09_official_contracts_are_anonymous_and_exact(self):
        for row in self.report["results"]:
            if not row["production_ready"]:
                self.assertEqual(row["official_contract"], {})
                continue
            contract = row["official_contract"]
            self.assertEqual(contract["status_code"], 200)
            self.assertEqual(contract["identity_result"], "exact_match")
            self.assertIn(
                contract["board_identity_result"],
                {"exact_normalized_descriptions", "official_integration_marker"},
            )

    def test_10_projection_and_original_master_coverage(self):
        self.assertEqual(len(load_target_companies(str(batch.CANDIDATE_REGISTRY))), 452)
        self.assertEqual(self.report["projection"], {
            "production_sources": 452,
            "source_delta": 15,
            "parent_mappings": 5,
            "mapping_delta": 1,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })
        self.assertTrue(all(not row["original_246_master"] for row in self.report["results"]))

    def test_11_hashes_ignore_and_safe_stage_manifest(self):
        validated_bytes = canonical_text_bytes(batch.VALIDATED_REGISTRY.read_bytes())
        marker = b"  - name: Anyscale\n"
        historical_bytes = validated_bytes[:validated_bytes.index(marker)].replace(
            b"    job_attribution_regex: CoreWeave|Weights & Biases\n",
            b"    job_attribution_regex: CoreWeave\n",
        )
        self.assertEqual(
            canonical_text_sha256(historical_bytes),
            canonical_sha256_for_legacy_text_sha256(
                self.report["production_registry_sha256"]
            ),
        )
        mapping_bytes = canonical_text_bytes(batch.PARENT_MAPPINGS.read_bytes())
        mapping_marker = b"  - child_company: Weights & Biases\n"
        historical_mapping_bytes = mapping_bytes[:mapping_bytes.index(mapping_marker)]
        self.assertEqual(
            canonical_text_sha256(historical_mapping_bytes),
            self.report["parent_mappings_sha256"],
        )
        self.assertEqual(self.report["safe_to_stage_files"], batch.SAFE_TO_STAGE_FILES)
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_12_validator_imports_no_persistence_or_publisher(self):
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
