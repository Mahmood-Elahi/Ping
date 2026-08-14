from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

import yaml

from scripts import refingerprint_uncovered_companies as reconciliation
from scripts import validate_original_master_recovery_batch_j as batch
from src.scraper.company_careers.adapters import greenhouse, static_html, workday
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.scanner import ADAPTERS


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}
TARGET_FACET = "daccab9f1d25018677ebcc363457460e"


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


class OriginalMasterRecoveryBatchJArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[: cls.validated_bytes.index(b"  - name: Rakuten")]
        cls.historical = entries_from_bytes(cls.historical_bytes)
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.rows = list(csv.DictReader(handle))

    def test_activation_preserves_exact_333_entry_production_prefix(self) -> None:
        self.assertEqual(401, len(self.production))
        self.assertEqual(336, len(self.candidates))
        self.assertEqual(336, len(self.validated))
        self.assertEqual(333, len(self.historical))
        self.assertEqual(
            [item["name"] for item in self.historical],
            [item["name"] for item in self.production[:333]],
        )
        self.assertEqual(VALIDATED, [item["name"] for item in self.candidates[-3:]])
        self.assertEqual(self.candidates[-3:], self.validated[-3:])
        self.assertEqual(
            4,
            len(reconciliation.load_parent_source_mappings(batch.PARENT_MAPPINGS)),
        )
        self.assertEqual(VALIDATED, [item["name"] for item in self.production[333:336]])
        self.assertEqual(
            [item["name"] for item in self.validated[-3:]],
            [item["name"] for item in self.production[333:336]],
        )
        production_names = [item["name"] for item in self.production]
        for name in VALIDATED:
            self.assertEqual(1, production_names.count(name))
        self.assertTrue(EXCLUDED.isdisjoint(production_names))

    def test_report_projects_three_recoveries_without_activation(self) -> None:
        totals = self.report["totals"]
        self.assertEqual(3, totals["validated"])
        self.assertEqual(12, totals["excluded"])
        self.assertEqual(336, totals["projected_production_sources"])
        self.assertEqual(207, totals["projected_original_master_coverage"])
        self.assertEqual("207/246", totals["projected_original_master_coverage_ratio"])
        self.assertEqual(VALIDATED, totals["original_master_companies_recovered_names"])
        self.assertEqual(
            {
                "ambiguous_attribution": 4,
                "blocked": 2,
                "session_required": 3,
                "stale_source": 1,
                "unstable_contract": 1,
                "unsupported_platform": 1,
                "validated_with_jobs": 3,
            },
            totals["statuses"],
        )
        self.assertTrue(self.report["candidate_registry_has_exact_333_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_333_entry_prefix"])

        master = reconciliation.reconcile_master(
            reconciliation.load_master_workbook(batch.MASTER_WORKBOOK),
            reconciliation.load_target_companies(str(batch.PRODUCTION)),
            reconciliation.load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(246, master.total)
        self.assertEqual(208, len(master.canonical_names) + len(master.mapped_names))

    def test_all_requested_companies_have_one_result_and_inventory_row(self) -> None:
        self.assertEqual(batch.REVIEWED_COMPANIES, [item["company"] for item in self.report["results"]])
        self.assertEqual(batch.REVIEWED_COMPANIES, [row["company"] for row in self.rows])
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(set(VALIDATED), {name for name, item in by_name.items() if item["safe_to_activate"]})
        self.assertEqual(EXCLUDED, {name for name, item in by_name.items() if not item["safe_to_activate"]})

    def test_live_results_are_repeatable_and_have_no_student_hits(self) -> None:
        by_name = {item["company"]: item for item in self.report["results"]}
        for name in VALIDATED:
            item = by_name[name]
            self.assertEqual(20, item["jobs_normalized"])
            self.assertEqual(0, item["current_student_intern_hits"])
            self.assertEqual(2, len(item["runs"]))
            self.assertEqual(item["runs"][0]["stable_ids"], item["runs"][1]["stable_ids"])
            self.assertEqual("stable_across_two_runs", item["stable_id_result"])
            self.assertEqual(
                "stable_official_and_job_specific_across_two_runs",
                item["apply_url_result"],
            )

    def test_reuses_existing_adapters_and_records_exact_target_facet(self) -> None:
        self.assertIs(ADAPTERS["static_html"], static_html.fetch_jobs)
        self.assertIs(ADAPTERS["workday"], workday.fetch_jobs)
        self.assertIs(ADAPTERS["greenhouse"], greenhouse.fetch_jobs)
        by_name = {item["name"]: item for item in self.candidates[-3:]}
        self.assertEqual("server_rendered_cards", by_name["Rakuten"]["endpoint_variant"])
        self.assertEqual({"jobFamilyGroup": [TARGET_FACET]}, by_name["Target Tech"]["applied_facets"])
        self.assertEqual("twitch", by_name["Twitch"]["slug"])
        self.assertNotIn("browser_rendered_html", {item["platform"] for item in self.candidates[-3:]})

    def test_no_publication_or_mapping_mutation_is_authorized(self) -> None:
        self.assertEqual(
            {
                "production_activated": False,
                "parent_mappings_changed": False,
                "student_filters_changed": False,
                "live_publication": False,
                "dashboard_write": False,
                "supabase": False,
                "google_sheets": False,
                "sqlite": False,
            },
            self.report["publication"],
        )
        self.assertEqual(batch.SAFE_TO_STAGE_FILES, self.report["safe_to_stage_files"])


class JsonResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self.payload


class WorkdayExactFacetTests(unittest.TestCase):
    @staticmethod
    def target(**changes: object) -> CompanyTarget:
        values: dict[str, object] = {
            "name": "Target Tech",
            "platform": "workday",
            "tenant": "target",
            "wd_subdomain": "wd5",
            "site_id": "targetcareers",
            "applied_facets": {"jobFamilyGroup": [TARGET_FACET]},
            "max_pages": 1,
        }
        values.update(changes)
        return CompanyTarget(**values)  # type: ignore[arg-type]

    @staticmethod
    def payload(*, total: int = 1, count: int = 1, facet: str = TARGET_FACET) -> dict:
        return {
            "total": total,
            "facets": [
                {
                    "facetParameter": "jobFamilyGroup",
                    "values": [{"id": facet, "count": count}],
                }
            ],
            "jobPostings": [
                {
                    "title": "Software Engineer",
                    "externalPath": "/job/Minneapolis-MN/Software-Engineer_R0001",
                    "bulletFields": ["R0001"],
                    "locationsText": "Minneapolis, MN",
                    "postedOn": "Posted Today",
                }
            ],
        }

    def test_exact_facet_is_sent_and_corroborated(self) -> None:
        class Session:
            def __init__(self, payload: dict) -> None:
                self.payload = payload
                self.calls: list[dict] = []

            def post(self, _url: str, *, json: dict, **_kwargs: object) -> JsonResponse:
                self.calls.append(json)
                return JsonResponse(self.payload)

        session = Session(self.payload())
        jobs = workday.fetch_jobs(session, self.target(), 5)  # type: ignore[arg-type]
        self.assertEqual(1, len(jobs))
        self.assertEqual({"jobFamilyGroup": [TARGET_FACET]}, session.calls[0]["appliedFacets"])
        self.assertEqual("R0001", jobs[0].job_id)
        self.assertEqual("Target Tech", jobs[0].company)

    def test_unrecognized_or_ignored_facet_fails_closed(self) -> None:
        class Session:
            def __init__(self, payload: dict) -> None:
                self.payload = payload

            def post(self, *_args: object, **_kwargs: object) -> JsonResponse:
                return JsonResponse(self.payload)

        for payload in (
            self.payload(total=2, count=1),
            self.payload(facet="different"),
            {"total": 1, "facets": [], "jobPostings": []},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(AdapterError):
                    workday.fetch_jobs(Session(payload), self.target(), 5)  # type: ignore[arg-type]

    def test_filtered_request_has_no_unfiltered_fallback(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.calls = 0

            def post(self, *_args: object, **_kwargs: object) -> JsonResponse:
                self.calls += 1
                return JsonResponse({}, status_code=500)

        session = Session()
        with self.assertRaisesRegex(AdapterError, "Workday HTTP 500"):
            workday.fetch_jobs(session, self.target(), 5)  # type: ignore[arg-type]
        self.assertEqual(1, session.calls)

    def test_only_one_safe_exact_facet_value_is_accepted(self) -> None:
        invalid = (
            {"one": ["a"], "two": ["b"]},
            {"jobFamilyGroup": ["a", "b"]},
            {"job family": ["a"]},
            {"jobFamilyGroup": ["../unsafe"]},
        )
        for facets in invalid:
            with self.subTest(facets=facets):
                with self.assertRaisesRegex(AdapterError, "applied_facets"):
                    workday.fetch_jobs(object(), self.target(applied_facets=facets), 5)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
