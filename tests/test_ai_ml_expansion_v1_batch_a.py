from __future__ import annotations

import ast
import copy
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
from urllib.parse import urlparse

import yaml

from scripts import validate_ai_ml_expansion_v1_batch_a as batch
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class AiMlExpansionV1BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_matches_validated_registry_exactly(self):
        self.assertGreaterEqual(len(self.production), 452)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 5)
        self.assertEqual(len(self.candidates), 437)
        self.assertEqual(self.candidates, self.validated)
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
        self.assertNotIn("Groq", production_names)

    def test_02_all_requested_companies_and_outcomes_are_reported(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        self.assertEqual(len(self.report["validated_companies"]), 19)
        self.assertEqual(self.report["excluded_companies"], ["Groq"])
        self.assertEqual(self.report["blockers"], {
            "Groq": batch.EXCLUDED[0]["exclusion_reason"],
        })

    def test_03_sources_are_unique_bounded_and_use_existing_adapters(self):
        additions = self.candidates[418:]
        self.assertEqual(
            [item["name"] for item in additions],
            [item["name"] for item in batch.CANDIDATE_ENTRIES],
        )
        keys = [registry_validator.source_key(item) for item in additions]
        self.assertTrue(all(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self.report["adapter_reuse"], ["ashby", "greenhouse", "static_html"])
        self.assertEqual(self.report["generic_adapter_improvements"], [])
        for item in additions:
            CompanyTarget.from_dict(item)
            self.assertTrue(item["source_identity_regex"])
            self.assertTrue(item["allowed_apply_hosts"])
            self.assertEqual(item["max_pages"], 1)
            self.assertLessEqual(item["max_jobs"], 20)
            self.assertIsNone(registry_validator.configuration_error(item))

    def test_04_repeat_runs_have_stable_ids_titles_locations_and_urls(self):
        additions = {item["name"]: item for item in self.candidates[418:]}
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
        self.assertEqual(jobs["normalized_total"], 237)
        self.assertEqual(jobs["student_intern_hits_total"], 19)
        self.assertEqual(jobs["summer_2027_hits_total"], 0)
        self.assertEqual(jobs["focus_role_hits_total"], 83)
        self.assertEqual(jobs["by_company"]["Etched"]["student_intern_hits"], 17)
        self.assertEqual(jobs["by_company"]["Together AI"]["student_intern_hits"], 2)
        self.assertEqual(jobs["by_company"]["Modal"]["student_intern_hits"], 0)
        self.assertEqual(jobs["by_company"]["Harvey"]["student_intern_hits"], 0)

    def test_06_student_metrics_match_production_filters_and_exact_jobs(self):
        by_name = {row["company"]: row for row in self.report["results"]}
        etched = by_name["Etched"]["current_student_candidate_evidence"]
        self.assertEqual(len(etched), 17)
        self.assertTrue(all(item["production_match"] for item in etched))
        self.assertEqual({item["location"] for item in etched}, {"San Jose"})
        self.assertEqual(
            {item["job_id"] for item in etched},
            {
                "27e5bd6b-9357-45f0-9e79-cfa2bf4eeba8",
                "f32908bd-8abc-495c-ab69-ac893b6c924d",
                "5f1f5739-3b58-467c-b351-ff183c94d96d",
                "dacedaca-c4ca-4964-85a7-8df1738005bb",
                "904ddf46-55fc-4a8f-8b49-f32cfe88116a",
                "a0a1671e-15d4-474c-8227-97fe16ade553",
                "699f3ab2-07e4-466c-9d76-3d4a3abb4ebc",
                "0321523b-f0c3-44cd-9c46-cf318a1cad9e",
                "6f23713f-5409-45b7-aae8-adb8710cdbc3",
                "1b073af4-6764-45ca-a22d-40a4823f0877",
                "f05e3218-5ec7-41d1-bc99-bb7014422229",
                "bd8c5768-7efa-4a18-9e56-485ccaf4ec77",
                "f02e8035-7dc9-4b0c-aab7-75bbb4e975b8",
                "157ed4f4-6e3b-4ec9-b93f-3e363e92041e",
                "ce683632-421a-4ef9-8664-b253356969a3",
                "b45e357c-07ea-4499-9911-1d3cc9b9ac71",
                "639fe410-56a4-44aa-ac93-8ee7c10c7d75",
            },
        )

        modal = next(row for row in self.report["results"] if row["company"] == "Modal")
        self.assertEqual(modal["current_student_role_evidence"], [])
        self.assertEqual(modal["current_student_candidate_evidence"], [{
            "job_id": "38888294-6bc7-4dab-b072-6d0f0c2ed79a",
            "title": "ML Research Intern",
            "location": "New York | San Francisco",
            "matching_rule": "strong_title:research_intern",
            "role_exclusion": "graduate_only",
            "location_exclusion": None,
            "production_match": False,
        }])

        together = by_name["Together AI"]["current_student_candidate_evidence"]
        self.assertEqual(
            [(item["job_id"], item["title"], item["location"]) for item in together],
            [
                (
                    "5157661007",
                    "Research Intern, Model Shaping (Fall 2026)",
                    "San Francisco, Amsterdam",
                ),
                (
                    "5157559007",
                    "Systems Research Engineer Intern - GPU Programming (Fall 2026)",
                    "San Francisco",
                ),
            ],
        )
        self.assertTrue(all(item["production_match"] for item in together))

    def test_07_acquisition_rebrand_stale_board_and_parent_routing_review(self):
        routing = self.report["routing_review"]
        self.assertEqual(set(routing), {"Groq", "Anysphere", "Runway", "Writer", "Hebbia"})
        self.assertIn("non-exclusive technology license", routing["Groq"])
        self.assertIn("remains independent", routing["Groq"])
        self.assertIn("Anysphere, Inc.", routing["Anysphere"])
        self.assertIn("runway.com", routing["Runway"])
        self.assertIn("stale Greenhouse", routing["Hebbia"])
        mapping_text = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        self.assertNotIn("child_company: Groq", mapping_text)

    def test_07_anysphere_uses_first_party_cursor_contract(self):
        anysphere = next(item for item in self.candidates if item["name"] == "Anysphere")
        self.assertEqual(anysphere["platform"], "static_html")
        self.assertEqual(anysphere["listing_url"], "https://cursor.com/careers")
        self.assertEqual(anysphere["allowed_apply_hosts"], ["cursor.com"])
        self.assertEqual(anysphere["source_identity_regex"], r"Anysphere, Inc\.")
        row = next(row for row in self.report["results"] if row["company"] == "Anysphere")
        for run in row["runs"]:
            self.assertTrue(all(url.startswith("https://cursor.com/careers/") for url in run["detail_apply_urls"]))

    def test_08_official_contracts_are_exact_and_anonymous(self):
        for row in self.report["results"]:
            if not row["production_ready"]:
                self.assertEqual(row["official_contract"], {})
                continue
            contract = row["official_contract"]
            self.assertEqual(contract["status_code"], 200)
            self.assertEqual(contract["identity_result"], "exact_match")
            self.assertIn(contract["source_marker_result"], {"exact_match", "stable_id_overlap"})
        harvey = next(row for row in self.report["results"] if row["company"] == "Harvey")
        self.assertEqual(harvey["official_contract"]["endpoint_result"], "stable_id_overlap")
        for name in ("ElevenLabs", "Synthesia"):
            row = next(row for row in self.report["results"] if row["company"] == name)
            self.assertEqual(row["official_contract"]["source_marker_result"], "exact_match")

    def test_09_projection_and_original_master_coverage(self):
        self.assertEqual(len(load_target_companies(str(batch.CANDIDATE_REGISTRY))), 437)
        self.assertEqual(self.report["projection"], {
            "production_sources": 437,
            "source_delta": 19,
            "parent_mappings": 4,
            "mapping_delta": 0,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })
        self.assertTrue(all(not row["original_246_master"] for row in self.report["results"]))

    def test_10_hashes_ignore_and_safe_stage_manifest(self):
        validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        marker = b"  - name: Together AI"
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
