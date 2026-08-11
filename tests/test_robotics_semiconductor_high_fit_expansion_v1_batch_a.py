from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

import yaml

from scripts import validate_robotics_semiconductor_high_fit_expansion_v1_batch_a as batch
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {
    "Innoviz Technologies", "Einride", "Skyworks Solutions",
    "Monolithic Power Systems", "Ambarella",
}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


class RoboticsSemiconductorHighFitExpansionV1BatchAArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        marker = b"  - name: Motional"
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(marker)]
        cls.historical = entries_from_bytes(cls.historical_bytes)
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_candidate_artifacts_preserve_exact_315_entry_production_prefix(self):
        self.assertEqual(len(self.production), 315)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), 315)
        self.assertEqual(self.production, self.historical)
        self.assertEqual(self.candidates[:315], self.historical)
        self.assertEqual(self.validated[:315], self.historical)
        self.assertEqual(self.production_bytes, self.historical_bytes)
        self.assertTrue(self.report["candidate_registry_has_exact_315_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_315_entry_prefix"])

    def test_02_candidate_and_validated_add_safe_sources_without_activation(self):
        self.assertEqual(len(self.candidates), 325)
        self.assertEqual(len(self.validated), 325)
        self.assertEqual([item["name"] for item in self.candidates[315:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[315:]], VALIDATED)
        production_names = [item["name"] for item in self.production]
        self.assertTrue(set(VALIDATED).isdisjoint(production_names))
        self.assertTrue(EXCLUDED.isdisjoint(production_names))
        self.assertEqual(len({name.casefold() for name in production_names}), 315)

    def test_03_all_requested_companies_and_outcomes_are_reported(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([item["company"] for item in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {
            "company", "official_careers_url", "platform", "adapter", "result",
            "jobs_normalized", "current_student_intern_hits", "current_focus_role_hits",
            "current_student_focus_hits", "canada_us_remote_locations_represented",
            "exclusion_reason", "stable_id_result", "title_result", "location_result",
            "apply_url_result",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_two_runs_are_bounded_and_repeat_all_normalized_fields(self):
        by_name = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
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
            allowed = {host.casefold() for host in by_name[item["company"]]["allowed_apply_hosts"]}
            for job_id, title, location, url in first_jobs:
                host = (urlparse(url).hostname or "").casefold()
                self.assertTrue(job_id and title and location and url.startswith("https://"))
                self.assertTrue(any(host == value or host.endswith("." + value) for value in allowed))

    def test_05_normalized_student_and_technical_focus_counts(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "Motional": 20, "Ouster": 20, "Aeva Technologies": 20,
            "Plus AI": 20, "Gecko Robotics": 18, "Locus Robotics": 9,
            "Qorvo": 20, "Diodes Incorporated": 17,
            "Allegro MicroSystems": 20, "indie Semiconductor": 20,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(by_name["Motional"]["current_student_intern_hits"], 1)
        self.assertEqual(by_name["Plus AI"]["current_student_intern_hits"], 7)
        self.assertEqual(by_name["Plus AI"]["current_student_focus_hits"], 3)
        self.assertEqual(by_name["indie Semiconductor"]["current_focus_role_hits"], 12)
        self.assertGreater(sum(by_name[name]["current_focus_role_hits"] for name in VALIDATED), 0)

    def test_06_canada_us_and_remote_location_evidence_is_explicit(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_tokens = {
            "Motional": "Remote U.S.", "Ouster": "San Francisco, CA",
            "Aeva Technologies": "Remote", "Plus AI": "Santa Clara, CA",
            "Locus Robotics": "United States", "Qorvo": "Richardson, TX, US, 75080",
            "Diodes Incorporated": "South Portland, ME, US",
            "Allegro MicroSystems": "Richardson, TX", "indie Semiconductor": "Toronto, Canada",
        }
        for name, token in expected_tokens.items():
            values = by_name[name]["canada_us_remote_locations_represented"]
            self.assertTrue(any(token in value for value in values), name)
        self.assertEqual(by_name["Gecko Robotics"]["canada_us_remote_locations_represented"], [])

    def test_07_projections_and_original_master_coverage(self):
        self.assertEqual(len(load_target_companies(str(batch.PRODUCTION))), 315)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual(totals["candidate_sources"], 10)
        self.assertEqual(totals["validated"], 10)
        self.assertEqual(totals["excluded"], 5)
        self.assertEqual(totals["projected_canonical_production_count"], 325)
        self.assertEqual(totals["projected_parent_mappings"], 4)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "196/246")

    def test_08_existing_adapters_and_exact_platform_contracts_are_used(self):
        expected = {
            "Motional": "greenhouse", "Ouster": "static_html",
            "Aeva Technologies": "lever", "Plus AI": "lever",
            "Gecko Robotics": "ashby", "Locus Robotics": "greenhouse",
            "Qorvo": "sap_successfactors", "Diodes Incorporated": "adp",
            "Allegro MicroSystems": "workday", "indie Semiconductor": "greenhouse",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            CompanyTarget.from_dict(item)
        by_name = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
        self.assertEqual(by_name["Motional"]["slug"], "motional")
        self.assertEqual(by_name["Plus AI"]["slug"], "plus-2")
        self.assertEqual(by_name["Locus Robotics"]["slug"], "locusrobotics")
        self.assertEqual(by_name["Qorvo"]["endpoint_variant"], "rmk_table")
        self.assertEqual(by_name["Diodes Incorporated"]["endpoint_variant"], "workforce_now_requisitions_v1")
        self.assertEqual(by_name["Allegro MicroSystems"]["site_id"], "AllegroCareers")

    def test_09_ouster_static_contract_has_stable_jazzhr_ids(self):
        entry = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Ouster")
        self.assertEqual(entry["listing_url"], "https://ouster.applytojob.com/apply")
        self.assertEqual(entry["external_id_regex"], r"/apply/([^/]+)/")
        result = next(item for item in self.report["results"] if item["company"] == "Ouster")
        first = result["runs"][0]
        self.assertTrue(all(job_id.startswith("static_html:ouster.applytojob.com:") for job_id in first["stable_ids"]))

    def test_10_exclusions_fail_closed_with_specific_reasons(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(by_name["Skyworks Solutions"]["validation_status"], "unsupported_platform_variant")
        self.assertIn("Unify", by_name["Skyworks Solutions"]["exclusion_reason"])
        self.assertEqual(by_name["Ambarella"]["validation_status"], "ambiguous_company_attribution")
        self.assertIn("different company", by_name["Ambarella"]["exclusion_reason"])
        for name in {"Innoviz Technologies", "Einride", "Monolithic Power Systems"}:
            self.assertEqual(by_name[name]["validation_status"], "blocked_official_source")
            reason = by_name[name]["exclusion_reason"].casefold()
            self.assertTrue("bypass" in reason or "challenge" in reason)
        self.assertEqual(len(self.report["adapter_opportunities"]), 4)

    def test_11_hashes_ignore_and_publication_state(self):
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


class RoboticsSemiconductorHighFitExpansionV1BatchAContractTests(unittest.TestCase):
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
        keys = [batch.source_key(item) for item in batch.CANDIDATE_ENTRIES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn(None, keys)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertTrue(item["source_identity_regex"])
            self.assertTrue(item["allowed_apply_hosts"])
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)


if __name__ == "__main__":
    unittest.main()
