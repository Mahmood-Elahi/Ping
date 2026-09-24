from __future__ import annotations

import ast
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

import yaml

from scripts import validate_robotics_expansion_v1_batch_a as batch
from src.scraper.company_careers.adapters import adp, greenhouse
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200
        self.content = json.dumps(payload).encode("utf-8")

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse(self.payload)


class RoboticsExpansionV1BatchAArtifactTests(unittest.TestCase):
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
        assert_historical_prefix(self, self.production, self.validated[:370])
        self.assertEqual(len(self.candidates), 382)
        self.assertEqual(self.candidates, self.validated)
        assert_historical_prefix(self, self.production, self.validated)
        self.assertFalse(self.report["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

        production_names = [item["name"] for item in self.production]
        for item in batch.CANDIDATE_ENTRIES:
            self.assertEqual(production_names.count(item["name"]), 1)
        self.assertTrue(
            {item["company"] for item in batch.EXCLUDED}.isdisjoint(production_names)
        )

    def test_02_all_requested_companies_and_outcomes_are_reported(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        self.assertEqual(len(self.report["validated_companies"]), 12)
        self.assertEqual(len(self.report["excluded_companies"]), 11)
        self.assertEqual(
            set(self.report["validated_companies"]),
            {
                "Berkshire Grey", "Diligent Robotics", "Cobalt Robotics", "Zipline",
                "Bright Machines", "Symbotic", "Plus One Robotics", "Dexterity",
                "Built Robotics", "Chef Robotics", "Avidbots", "Novarc Technologies",
            },
        )

    def test_03_canonical_sources_are_unique_and_exact(self):
        additions = self.candidates[370:]
        self.assertEqual([item["name"] for item in additions], [item["name"] for item in batch.CANDIDATE_ENTRIES])
        self.assertEqual(len({registry_key(item) for item in additions}), len(additions))
        expected = {
            "Berkshire Grey": "bamboohr", "Diligent Robotics": "greenhouse",
            "Cobalt AI": "bamboohr", "Zipline": "greenhouse",
            "Bright Machines": "lever", "Symbotic": "workday",
            "Plus One Robotics": "bamboohr", "Dexterity": "lever",
            "Built Robotics": "ashby", "Chef Robotics": "lever",
            "Avidbots": "bamboohr", "Novarc Technologies": "adp",
        }
        self.assertEqual({item["name"]: item["platform"] for item in additions}, expected)
        for item in additions:
            CompanyTarget.from_dict(item)
            self.assertTrue(item["source_identity_regex"])
            self.assertTrue(item["allowed_apply_hosts"])
            self.assertLessEqual(item["max_pages"], 2)
            self.assertLessEqual(item["max_jobs"], 20)

    def test_04_two_runs_repeat_stable_ids_titles_locations_and_urls(self):
        additions = {item["name"]: item for item in self.candidates[370:]}
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
            canonical = row["canonical_company"]
            allowed = additions[canonical]["allowed_apply_hosts"]
            for url in first["detail_apply_urls"]:
                host = (urlparse_host(url)).casefold()
                self.assertTrue(any(host == value or host.endswith("." + value) for value in allowed))

    def test_05_current_job_student_and_focus_counts(self):
        totals = self.report["jobs"]
        self.assertEqual(totals["normalized_total"], 127)
        self.assertEqual(totals["student_intern_hits_total"], 6)
        self.assertEqual(totals["summer_2027_hits_total"], 2)
        self.assertEqual(totals["focus_role_hits_total"], 58)
        zipline = next(row for row in self.report["results"] if row["company"] == "Zipline")
        self.assertEqual(zipline["jobs_normalized"], 4)
        self.assertEqual(zipline["summer_2027_hits"], 2)
        self.assertTrue(any("Summer 2027" in title for title in zipline["current_student_role_evidence"]))
        symbotic = next(row for row in self.report["results"] if row["company"] == "Symbotic")
        self.assertEqual(symbotic["current_student_intern_hits"], 2)

    def test_06_acquisition_rebrand_and_stale_routing_is_explicit(self):
        evidence = self.report["stale_acquired_rebranded"]
        self.assertEqual(
            set(evidence),
            {"Franka Robotics", "Berkshire Grey", "6 River Systems", "Fetch Robotics", "Cobalt Robotics", "Covariant"},
        )
        by_name = {row["company"]: row for row in self.report["results"]}
        self.assertEqual(by_name["Cobalt Robotics"]["canonical_company"], "Cobalt AI")
        self.assertTrue(by_name["Cobalt Robotics"]["production_ready"])
        for name in {"6 River Systems", "Fetch Robotics"}:
            self.assertEqual(by_name[name]["validation_status"], "acquired_parent_source_required")
        self.assertIn("not an acquisition", by_name["Covariant"]["exclusion_reason"])

    def test_07_blockers_fail_closed_without_bypass(self):
        by_name = {row["company"]: row for row in self.report["results"]}
        self.assertEqual(by_name["Realtime Robotics"]["validation_status"], "captcha_protected_apply_flow")
        self.assertIn("no bypass", by_name["Realtime Robotics"]["exclusion_reason"].casefold())
        self.assertEqual(by_name["Formant"]["validation_status"], "unsupported_platform_variant")
        for name in {"Wing", "Vicarious Surgical", "Attabotics"}:
            self.assertEqual(by_name[name]["jobs_observed"], 0)
            self.assertFalse(by_name[name]["production_ready"])

    def test_08_projection_and_master_coverage(self):
        self.assertGreaterEqual(len(load_target_companies(str(batch.PRODUCTION))), 452)
        self.assertEqual(self.report["projection"], {
            "production_sources": 382, "source_delta": 12,
            "parent_mappings": 4, "mapping_delta": 0,
            "original_master_coverage": "208/246", "master_delta": 0,
        })
        self.assertTrue(all(not row["original_246_master"] for row in self.report["results"]))

    def test_09_hashes_ignore_and_safe_stage_manifest(self):
        validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        marker = b"  - name: Berkshire Grey"
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
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
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


class RoboticsExpansionV1BatchAAdapterTests(unittest.TestCase):
    def test_11_greenhouse_department_allowlist_and_result_bound(self):
        payload = {"jobs": [
            {
                "id": 1, "title": "Hardware Engineer", "location": {"name": "USA"},
                "absolute_url": "https://company.example/jobs/1",
                "departments": [{"name": "Hardware"}], "offices": [],
            },
            {
                "id": 2, "title": "Quality Intern (Summer 2027)", "location": {"name": "USA"},
                "absolute_url": "https://company.example/jobs/2",
                "departments": [{"name": "2027 Internships"}], "offices": [],
            },
            {
                "id": 3, "title": "Software Intern (Summer 2027)", "location": {"name": "USA"},
                "absolute_url": "https://company.example/jobs/3",
                "departments": [{"name": "2027 Internships"}], "offices": [],
            },
        ]}
        session = FakeSession(payload)
        target = CompanyTarget(
            name="Example", platform="greenhouse", slug="example", max_jobs=1,
            department_allowlist=["2027 Internships"],
        )
        jobs = greenhouse.fetch_jobs(session, target)
        self.assertEqual([job.job_id for job in jobs], ["2"])
        self.assertEqual(session.greenhouse_pagination["jobs_returned"], 3)
        self.assertEqual(session.greenhouse_pagination["maximum_results"], 1)

    def test_12_greenhouse_rejects_unbounded_configuration(self):
        target = CompanyTarget(
            name="Example",
            platform="greenhouse",
            slug="example",
            max_jobs=greenhouse.MAX_RESULTS + 1,
        )
        with self.assertRaisesRegex(AdapterError, "cannot exceed"):
            greenhouse.fetch_jobs(FakeSession({"jobs": []}), target)

    def test_13_adp_ignores_only_empty_location_placeholders(self):
        stable = {
            "nameCode": {"shortName": " Burnaby, BC, CA"},
            "address": {"cityName": "Burnaby"},
        }
        empty = {"nameCode": {}, "address": {"cityName": "", "countrySubdivisionLevel1": {}}}
        self.assertEqual(adp._locations([stable, empty]), "Burnaby, BC, CA")
        with self.assertRaisesRegex(AdapterError, "no stable display value"):
            adp._locations([empty])

    def test_14_adp_structured_address_variants_have_deterministic_precedence(self):
        location = {
            "nameCode": {},
            "formattedAddress": "Richmond, BC, Canada",
            "displayName": "Lower Mainland",
            "address": {
                "cityName": "Burnaby",
                "countrySubdivisionLevel1": {"codeValue": "BC"},
                "countryCode": "CA",
            },
        }
        self.assertEqual(adp._locations([location]), "Richmond, BC, Canada")

        address_only = dict(location)
        address_only.pop("formattedAddress")
        address_only.pop("displayName")
        self.assertEqual(adp._locations([address_only]), "Burnaby, BC, CA")

    def test_15_adp_multiple_locations_are_ordered_and_deduplicated(self):
        locations = [
            {"nameCode": {"longName": "Burnaby, BC, Canada"}},
            {"displayName": "Remote - Canada"},
            {"nameCode": {"shortName": "burnaby, bc, canada"}},
        ]
        self.assertEqual(
            adp._locations(locations),
            "Burnaby, BC, Canada | Remote - Canada",
        )

    def test_16_adp_missing_and_malformed_locations_fail_closed(self):
        for value, message in (
            (None, "missing requisition locations"),
            ([], "missing requisition locations"),
            (["Burnaby"], "location is malformed"),
            ([{"formattedAddress": {"value": "Burnaby"}}], "must be text"),
            ([{"nameCode": {"codeValue": "LOC-14987"}}], "no stable display value"),
            ([{"address": {"countryCode": 124}}], "is malformed"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AdapterError, message):
                    adp._locations(value)

    def test_17_adp_nested_work_location_and_bounded_diagnostics(self):
        nested = [{
            "nameCode": {"codeValue": "LOC-14987"},
            "workLocation": {
                "address": {
                    "addressLineOne": "123 Main St",
                    "city": "Burnaby",
                    "stateProvince": {"shortName": "BC"},
                    "country": {"longName": "Canada"},
                }
            },
        }]
        self.assertEqual(
            adp._locations(nested),
            "123 Main St, Burnaby, BC, Canada",
        )

        secret = "private description " + ("x" * 200)
        with self.assertRaisesRegex(AdapterError, "entries=1") as caught:
            adp._locations([{"description": secret, "opaqueId": "LOC-1"}])
        message = str(caught.exception)
        self.assertIn("fields=[description,opaqueId]", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("x" * 80, message)

    def test_18_adp_nested_structured_geography_regression_fixtures(self):
        cases = (
            (
                [{"address": {
                    "countrySubdivisionLevel1": {"longName": "British Columbia"}
                }}],
                "British Columbia",
            ),
            (
                [{"address": {
                    "cityName": {"nameCode": {"longName": "Burnaby"}},
                    "countrySubdivisionLevel1": {"codeValue": "BC"},
                }}],
                "Burnaby, BC",
            ),
            (
                [{"address": {
                    "countrySubdivisionLevel1": {
                        "displayValue": {"shortName": "AB"}
                    },
                    "country": {"longName": "Canada"},
                }}],
                "AB, Canada",
            ),
            (
                [{"address": {
                    "cityName": "Burnaby",
                    "countrySubdivisionLevel1": {"shortName": "BC"},
                    "postalCode": "V5J 1A1",
                    "countryCode": "CA",
                }}],
                "Burnaby, BC V5J 1A1, CA",
            ),
            (
                [
                    {"address": {"cityName": "Burnaby", "stateCode": "BC"}},
                    {"address": {"cityName": "Calgary", "stateCode": "AB"}},
                ],
                "Burnaby, BC | Calgary, AB",
            ),
        )
        for value, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(adp._locations(value), expected)

        rejected = (
            [{"address": {}}],
            [{"address": {"countrySubdivisionLevel1": {"codeValue": "LOC-14987"}}}],
            [{"aliasNames": ["Vancouver office"]}],
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaisesRegex(AdapterError, "no stable display value"):
                    adp._locations(value)

    def test_19_adp_subdivision_scalar_object_list_and_rejections(self):
        accepted = (
            ([{"address": {"countrySubdivisionLevel1": "British Columbia"}}], "British Columbia"),
            ([{"address": {"countrySubdivisionLevel1": "BC"}}], "BC"),
            ([{"address": {"countrySubdivisionLevel1": {"value": "Ontario"}}}], "Ontario"),
            ([{"address": {"countrySubdivisionLevel1": {"name": "California", "code": "CA"}}}], "California"),
            ([{"address": {"countrySubdivisionLevel1": [{"displayValue": "Alberta"}]}}], "Alberta"),
        )
        for value, expected in accepted:
            with self.subTest(expected=expected):
                self.assertEqual(adp._locations(value), expected)

        rejected = (
            [{"address": {"countrySubdivisionLevel1": "550e8400-e29b-41d4-a716-446655440000"}}],
            [{"address": {"countrySubdivisionLevel1": "14987"}}],
            [{"address": {"countrySubdivisionLevel1": '{"name":"Ontario"}'}}],
            [{"address": {"countrySubdivisionLevel1": ["Ontario", "California"]}}],
            [{"aliasNames": ["Vancouver office"]}],
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(AdapterError):
                    adp._locations(value)

    def test_20_adp_diagnostic_exposes_nested_subdivision_shape_boundedly(self):
        with self.assertRaises(AdapterError) as caught:
            adp._locations([{
                "address": {
                    "countrySubdivisionLevel1": {
                        "opaqueId": "550e8400-e29b-41d4-a716-446655440000"
                    }
                }
            }])
        message = str(caught.exception)
        self.assertIn("address.countrySubdivisionLevel1(opaqueId)", message)
        self.assertIn("address.countrySubdivisionLevel1.opaqueId=str", message)
        self.assertLess(len(message), 2000)


def registry_key(item: dict) -> str:
    key = registry_validator_key(item)
    if not key:
        raise AssertionError(f"missing source key for {item['name']}")
    return key


def registry_validator_key(item: dict) -> str | None:
    return batch.registry_validator.source_key(item)


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


if __name__ == "__main__":
    unittest.main()
