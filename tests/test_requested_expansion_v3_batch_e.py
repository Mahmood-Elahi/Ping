from __future__ import annotations

import ast
import csv
import json
import subprocess
import tempfile
import unittest

from tests.company_careers_activation_milestones import (
    historical_parent_mapping_sha256,
)
from tests.text_bytes import (
    canonical_sha256_for_legacy_text_sha256,
    canonical_text_sha256,
)
from pathlib import Path
from types import SimpleNamespace

import yaml

from tests.company_careers_reliability_assertions import (
    assert_historical_prefix,
    with_reliability_repairs,
)

from scripts import validate_requested_expansion_v3_batch_e as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.adapters import bamboohr, startdate, ukg_pro
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class Response:
    def __init__(self, payload: object, *, url: str = "https://example.test", status: int = 200):
        self._payload = payload
        self.url = url
        self.status_code = status
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self) -> object:
        return self._payload


class Session:
    def __init__(self, responses: list[Response]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append((url, dict(kwargs)))
        return self.responses.pop(0)


class RequestedExpansionV3BatchEArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: Gibson Energy")]
        cls.historical = (yaml.safe_load(cls.historical_bytes) or {})["target_companies"]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_exact_243_entry_production_prefix(self):
        self.assertGreaterEqual(len(self.production), 452)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 5)
        self.assertEqual(len(self.historical), 243)
        assert_historical_prefix(self, self.production, self.historical)
        self.assertEqual(self.candidates[:243], self.historical)
        self.assertEqual(self.validated[:243], self.historical)
        self.assertEqual(self.production[:249], with_reliability_repairs(self.validated))
        self.assertEqual(self.candidate_bytes, self.validated_bytes)
        self.assertTrue(self.report["candidate_registry_has_exact_243_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_243_entry_prefix"])

    def test_02_registries_contain_only_six_safe_additions(self):
        self.assertEqual(len(self.candidates), 249)
        self.assertEqual(len(self.validated), 249)
        self.assertEqual([item["name"] for item in self.candidates[243:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[243:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[243:249]], VALIDATED)
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))
        self.assertTrue(
            EXCLUDED.isdisjoint(
                {item["name"] for item in self.production[:249]}
            )
        )
        for name in VALIDATED:
            self.assertEqual([item["name"] for item in self.production].count(name), 1)

    def test_03_report_and_inventory_cover_every_requested_company(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {
            "company", "fresh_vs_recovery_target", "official_careers_url", "platform", "adapter",
            "result", "jobs_normalized", "current_student_intern_hits", "canadian_locations_represented",
            "original_246_master", "validation_status", "stable_id_result", "apply_url_result",
            "canadian_identity_location_evidence", "canada_us_remote_student_compatibility", "exclusion_reason",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_repeat_runs_have_stable_ids_and_official_job_urls(self):
        for item in self.report["results"]:
            self.assertEqual(len(item["runs"]), 2)
            if item["safe_to_activate"]:
                first, second = item["runs"]
                self.assertTrue(first["success"] and second["success"])
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["apply_urls"], second["apply_urls"])
                self.assertEqual(first["student_hits"], second["student_hits"])
                self.assertEqual(first["canadian_locations"], second["canadian_locations"])

    def test_05_live_counts_student_hits_and_canadian_locations(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "Gibson Energy": 9, "Strathcona Resources": 9, "SensorUp": 2,
            "Semtech": 20, "Pason Systems": 3, "Trican Well Service": 20,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(by_name["Gibson Energy"]["current_student_intern_hits"], 2)
        self.assertTrue(all(by_name[name]["current_student_intern_hits"] == 0 for name in VALIDATED if name != "Gibson Energy"))
        self.assertIn("Calgary, AB", " | ".join(by_name["Strathcona Resources"]["canadian_locations_represented"]))
        self.assertIn("CAN - Richmond, BC", by_name["Semtech"]["canadian_locations_represented"])
        self.assertEqual(by_name["Pason Systems"]["canadian_locations_represented"], ["Calgary"])
        self.assertIn("Calgary, AB", by_name["Trican Well Service"]["canadian_locations_represented"])

    def test_06_master_membership_and_projections(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertGreaterEqual(len(master.canonical_names) + len(master.mapped_names), 208)
        self.assertTrue(all(not item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual((totals["validated"], totals["excluded"]), (6, 9))
        self.assertEqual(totals["projected_canonical_production_count"], 249)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "186/246")
        self.assertEqual((totals["fresh_targets"], totals["recovery_targets"]), (11, 4))

    def test_07_platforms_and_adapters_are_exact(self):
        expected = {
            "Gibson Energy": "sap_successfactors", "Strathcona Resources": "workday",
            "SensorUp": "bamboohr", "Semtech": "workday", "Pason Systems": "ukg_pro",
            "Trican Well Service": "startdate",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            CompanyTarget.from_dict(item)

    def test_08_recovery_and_attribution_safeguards(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual({name for name, row in by_name.items() if row["fresh_vs_recovery_target"] == "recovery"}, batch.RECOVERY_TARGETS)
        pason = next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "Pason Systems")
        self.assertIn("job_attribution_regex", pason)
        self.assertIn("job_attribution_exclude_regex", pason)
        self.assertTrue(all("recruiting.ultipro.ca" in url for url in by_name["Pason Systems"]["runs"][0]["apply_urls"]))
        self.assertTrue(all("#/career/info/job-list/" in url for url in by_name["Trican Well Service"]["runs"][0]["apply_urls"]))
        self.assertIn("Cloudflare", by_name["Parkland Corporation"]["exclusion_reason"])
        self.assertIn("Turnstile", by_name["Blackline Safety"]["exclusion_reason"])
        self.assertNotIn("workable", batch.scanner.ADAPTERS)

    def test_09_identity_acquisition_and_shared_board_boundaries(self):
        evidence = batch.CANADA_RELEVANCE
        self.assertIn("former nVent Thermal Management", evidence["Chemelex"])
        self.assertIn("acquired by IFS", evidence["Copperleaf"])
        self.assertIn("wholly owned subsidiary", evidence["BeWhere"])
        self.assertIn("rebranded as RSG Telecom", evidence["Rigstar Industrial Telecom"])
        self.assertIn("sensemetrics", evidence["SensorUp"])
        self.assertIn("Sierra Wireless", evidence["Semtech"])
        mappings = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        for name in batch.REVIEWED_COMPANIES:
            self.assertNotIn(name, mappings)

    def test_10_hash_ignore_and_publication_state(self):
        self.assertEqual(
            canonical_text_sha256(self.historical_bytes),
            canonical_sha256_for_legacy_text_sha256(
                self.report["production_registry_sha256"]
            ),
        )
        self.assertEqual(historical_parent_mapping_sha256(batch.PARENT_MAPPINGS), self.report["parent_mappings_sha256"])
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_11_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class RequestedExpansionV3BatchEAdapterTests(unittest.TestCase):
    def test_12_bamboohr_uses_stable_public_share_urls(self):
        listing = {"meta": {"totalCount": 1}, "result": [{"id": "46", "jobOpeningName": "Engineer"}]}
        detail = {"result": {"jobOpening": {
            "jobOpeningShareUrl": "https://sensorup.bamboohr.com/careers/46",
            "jobOpeningName": "Engineer", "jobOpeningStatus": "Open", "departmentLabel": "Engineering",
            "atsLocation": {"city": "Remote", "state": "Texas", "country": "United States"},
            "datePosted": "2026-04-20", "description": "Build systems.",
        }}}
        session = Session([Response(listing), Response(detail)])
        target = CompanyTarget.from_dict(next(item for item in batch.CANDIDATE_ENTRIES if item["name"] == "SensorUp"))
        jobs = bamboohr.fetch_jobs(session, target)
        self.assertEqual([(job.job_id, job.url) for job in jobs], [("46", "https://sensorup.bamboohr.com/careers/46")])
        self.assertEqual(jobs[0].location, "Remote, Texas, United States")

    def test_13_startdate_sends_only_public_origin_and_builds_stable_route(self):
        item = {
            "id": 3088, "name": {"en": "Mechanic"}, "summary": {"en": "Trican role"},
            "description": {"en": "Trican Well Service"}, "start_date": "2026-07-23T14:49:00.000Z",
            "is_internal": False, "status": "active", "requisition_number": "",
            "location": {"city": "Calgary", "province": "AB"},
            "category_division": [{"name": {"en": "Trican Well Service"}}],
            "category_display": [{"name": {"en": "Trades"}}],
        }
        session = Session([Response([item])])
        target = CompanyTarget.from_dict(next(value for value in batch.CANDIDATE_ENTRIES if value["name"] == "Trican Well Service"))
        jobs = startdate.fetch_jobs(session, target)
        self.assertEqual(jobs[0].url, "https://tricanwellservice.startdate.ca/#/career/info/job-list/3088?language=en")
        headers = session.calls[0][1]["headers"]
        self.assertEqual(headers["Origin"], "https://tricanwellservice.startdate.ca")
        self.assertNotIn("Cookie", headers)

    def test_14_startdate_and_ukg_reject_mixed_per_job_attribution(self):
        target = CompanyTarget.from_dict(next(value for value in batch.CANDIDATE_ENTRIES if value["name"] == "Trican Well Service"))
        mixed = {
            "id": 1, "name": {"en": "Other role"}, "summary": {"en": "Other employer"},
            "description": {"en": "Other employer"}, "category_division": [{"name": {"en": "Other"}}],
        }
        with self.assertRaisesRegex(AdapterError, "mixed or ambiguous"):
            startdate.fetch_jobs(Session([Response([mixed])]), target)
        ukg_target = CompanyTarget.from_dict(next(value for value in batch.CANDIDATE_ENTRIES if value["name"] == "Pason Systems"))
        self.assertTrue(ukg_pro._has_exact_job_attribution({"BriefDescription": "A Pason Systems role"}, ukg_target))
        self.assertFalse(ukg_pro._has_exact_job_attribution({"BriefDescription": "An IWS role"}, ukg_target))

    def test_15_contract_helpers_preserve_duplicate_and_url_checks(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(
            batch._verify_jobs([fake_job("2"), fake_job("1", "https://jobs.example.test/job/1")], entry),
            [("1", "https://jobs.example.test/job/1"), ("2", "https://jobs.example.test/job/2")],
        )
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([fake_job(), fake_job(url="https://jobs.example.test/job/3")], entry)
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory) / "production.yaml"
            destination = Path(directory) / "candidate.yaml"
            production.write_text("target_companies:\n  - name: Existing\n    platform: greenhouse\n    slug: shared\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(production, destination, [{"name": "Existing", "platform": "greenhouse", "slug": "other"}])


if __name__ == "__main__":
    unittest.main()
