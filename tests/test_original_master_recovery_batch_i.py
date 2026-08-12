from __future__ import annotations

import csv
import html as html_module
import json
import unittest
from pathlib import Path

import yaml

from scripts import refingerprint_uncovered_companies as reconciliation
from scripts import validate_original_master_recovery_batch_i as batch
from src.scraper.company_careers.adapters import embedded_data, icims
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.scanner import ADAPTERS


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


class Response:
    def __init__(
        self,
        body: str,
        *,
        url: str = "https://jobs.example.com/board",
        content_type: str = "text/html",
    ) -> None:
        self.status_code = 200
        self.url = url
        self.content = body.encode("utf-8")
        self.text = body
        self.headers = {"content-type": content_type}
        self.history: list[object] = []


class OriginalMasterRecoveryBatchIArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: CATL")]
        cls.historical = entries_from_bytes(cls.historical_bytes)
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.rows = list(csv.DictReader(handle))

    def test_activation_preserves_exact_330_entry_production_prefix(self) -> None:
        self.assertEqual(336, len(self.production))
        self.assertEqual(333, len(self.candidates))
        self.assertEqual(333, len(self.validated))
        self.assertEqual(330, len(self.historical))
        self.assertEqual(self.historical, self.production[:330])
        self.assertTrue(self.production_bytes.startswith(self.historical_bytes))
        self.assertEqual(VALIDATED, [item["name"] for item in self.candidates[-3:]])
        self.assertEqual(VALIDATED, [item["name"] for item in self.validated[-3:]])
        self.assertEqual(self.candidates[-3:], self.validated[-3:])
        self.assertEqual(
            4,
            len(reconciliation.load_parent_source_mappings(batch.PARENT_MAPPINGS)),
        )
        self.assertEqual(VALIDATED, [item["name"] for item in self.production[330:333]])
        self.assertEqual(self.validated[-3:], self.production[330:333])
        production_names = [item["name"] for item in self.production]
        for name in VALIDATED:
            self.assertEqual(1, production_names.count(name))
        self.assertTrue(EXCLUDED.isdisjoint(production_names))

    def test_report_projects_only_three_validated_master_recoveries(self) -> None:
        totals = self.report["totals"]
        self.assertEqual(3, totals["validated"])
        self.assertEqual(12, totals["excluded"])
        self.assertEqual(333, totals["projected_production_sources"])
        self.assertEqual(204, totals["projected_original_master_coverage"])
        self.assertEqual(VALIDATED, totals["original_master_companies_recovered_names"])
        self.assertEqual(
            {
                "ambiguous_attribution": 3,
                "blocked": 2,
                "browser_required": 2,
                "network_inaccessible": 1,
                "stale_source": 4,
                "validated_with_jobs": 2,
                "validated_zero_jobs": 1,
            },
            totals["statuses"],
        )

        master = reconciliation.reconcile_master(
            reconciliation.load_master_workbook(batch.MASTER_WORKBOOK),
            reconciliation.load_target_companies(str(batch.PRODUCTION)),
            reconciliation.load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(246, master.total)
        self.assertEqual(207, len(master.canonical_names) + len(master.mapped_names))

    def test_all_requested_companies_have_one_result_and_inventory_row(self) -> None:
        self.assertEqual(
            batch.REVIEWED_COMPANIES,
            [item["company"] for item in self.report["results"]],
        )
        self.assertEqual(batch.REVIEWED_COMPANIES, [row["company"] for row in self.rows])
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(
            set(VALIDATED),
            {name for name, item in by_name.items() if item["safe_to_activate"]},
        )
        self.assertEqual(
            EXCLUDED,
            {name for name, item in by_name.items() if not item["safe_to_activate"]},
        )

    def test_live_results_are_repeatable_and_zero_contract_is_explicit(self) -> None:
        by_name = {item["company"]: item for item in self.report["results"]}
        for name in ("CATL", "DJI"):
            item = by_name[name]
            self.assertEqual(15, item["jobs_normalized"])
            self.assertEqual(0, item["current_student_intern_hits"])
            self.assertEqual(2, len(item["runs"]))
            self.assertEqual(item["runs"][0]["stable_ids"], item["runs"][1]["stable_ids"])
            self.assertEqual(
                "stable_official_and_job_specific_across_two_runs",
                item["apply_url_result"],
            )

        htc = by_name["HTC Vive"]
        self.assertEqual("validated_zero_jobs", htc["validation_status"])
        self.assertEqual(0, htc["jobs_normalized"])
        self.assertTrue(all(run["zero_job_contract"] for run in htc["runs"]))
        self.assertTrue(
            all(run["pagination_stop_reason"] == "validated_zero_jobs" for run in htc["runs"])
        )

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

    def test_existing_adapters_are_reused_and_only_generic_variant_is_added(self) -> None:
        self.assertIs(ADAPTERS["icims"], icims.fetch_jobs)
        self.assertIs(
            ADAPTERS["javascript_embedded_data"], embedded_data.fetch_jobs
        )
        by_name = {item["name"]: item for item in self.candidates[-3:]}
        self.assertEqual("javascript_embedded_data", by_name["CATL"]["platform"])
        self.assertEqual("html_attribute_json", by_name["CATL"]["endpoint_variant"])
        self.assertEqual("icims", by_name["HTC Vive"]["platform"])
        self.assertEqual("javascript_embedded_data", by_name["DJI"]["platform"])
        self.assertNotIn(
            "browser_rendered_html", {item["platform"] for item in self.candidates[-3:]}
        )


class HtmlAttributeJsonAdapterTests(unittest.TestCase):
    @staticmethod
    def target(**changes: object) -> CompanyTarget:
        values: dict[str, object] = {
            "name": "Example",
            "platform": "javascript_embedded_data",
            "listing_url": "https://jobs.example.com/board",
            "endpoint_variant": "html_attribute_json",
            "embedded_script_selector": "input#init-data",
            "reference_url_attribute": "value",
            "embedded_json_path": "jobs",
            "id_field": "id",
            "title_field": "title",
            "location_fields": ["location.address", "location.country"],
            "url_field": "id",
            "date_field": "updatedAt",
            "department_field": "department.name",
            "job_attribution_field": "orgId",
            "job_attribution_regex": r"^example$",
            "allowed_source_hosts": ["jobs.example.com"],
            "allowed_apply_hosts": ["jobs.example.com"],
            "apply_url_template": "https://jobs.example.com/board?hash=%23%2Fjob%2F{id}",
            "max_pages": 1,
            "max_jobs": 2,
        }
        values.update(changes)
        return CompanyTarget(**values)

    @staticmethod
    def page(jobs: list[dict], *, duplicate: bool = False) -> str:
        value = html_module.escape(json.dumps({"jobs": jobs}), quote=True)
        element = f'<input id="init-data" value="{value}">'
        return f"<html><body>{element}{element if duplicate else ''}</body></html>"

    @staticmethod
    def job(identifier: str = "stable-1", organization: str = "example") -> dict:
        return {
            "orgId": organization,
            "id": identifier,
            "title": "Engineering Intern",
            "updatedAt": "2026-08-11T00:00:00Z",
            "location": {"address": "Waterloo", "country": "Canada"},
            "department": {"name": "Engineering"},
        }

    def test_reviewed_attribute_normalizes_stable_job_fields(self) -> None:
        body = self.page([self.job()])

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(body, url=url)

        session = Session()
        jobs = embedded_data.fetch_jobs(session, self.target(), 5)  # type: ignore[arg-type]
        self.assertEqual(1, len(jobs))
        self.assertTrue(jobs[0].job_id.endswith(":stable-1"))
        self.assertEqual("Waterloo | Canada", jobs[0].location)
        self.assertEqual(["Engineering"], jobs[0].departments)
        self.assertIn("hash=%23%2Fjob%2Fstable-1", jobs[0].url)

    def test_every_row_requires_exact_attribution(self) -> None:
        body = self.page([self.job(organization="unrelated")])

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(body, url=url)

        with self.assertRaisesRegex(AdapterError, "exact source attribution"):
            embedded_data.fetch_jobs(Session(), self.target(), 5)  # type: ignore[arg-type]

    def test_selector_must_match_exactly_once(self) -> None:
        body = self.page([self.job()], duplicate=True)

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(body, url=url)

        with self.assertRaisesRegex(AdapterError, "match exactly once"):
            embedded_data.fetch_jobs(Session(), self.target(), 5)  # type: ignore[arg-type]

    def test_redirect_to_unreviewed_source_host_fails_closed(self) -> None:
        body = self.page([self.job()])

        class Session:
            def get(self, _url: str, **_kwargs: object) -> Response:
                return Response(body, url="https://other.example.net/board")

        with self.assertRaisesRegex(AdapterError, "outside allowed source hosts"):
            embedded_data.fetch_jobs(Session(), self.target(), 5)  # type: ignore[arg-type]

    def test_empty_reviewed_job_list_proves_zero_collection(self) -> None:
        body = self.page([])

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(body, url=url)

        session = Session()
        self.assertEqual([], embedded_data.fetch_jobs(session, self.target(), 5))  # type: ignore[arg-type]
        self.assertTrue(session.embedded_data_pagination["zero_job_contract"])  # type: ignore[attr-defined]
        self.assertEqual(
            "validated_zero_jobs",
            session.embedded_data_pagination["stop_reason"],  # type: ignore[attr-defined]
        )

    def test_variant_requires_attribute_source_hosts_and_attribution(self) -> None:
        for changes in (
            {"reference_url_attribute": None},
            {"allowed_source_hosts": []},
            {"job_attribution_field": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(AdapterError):
                    embedded_data.validate_target(self.target(**changes))


if __name__ == "__main__":
    unittest.main()
