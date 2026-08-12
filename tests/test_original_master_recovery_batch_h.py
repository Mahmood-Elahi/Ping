from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from scripts import validate_original_master_recovery_batch_h as batch
from scripts import refingerprint_uncovered_companies as reconciliation
from src.scraper.company_careers.adapters import beesite, embedded_data, mcloud_cws
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.scanner import ADAPTERS


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def entries_from_bytes(content: bytes) -> list[dict]:
    return (yaml.safe_load(content.decode("utf-8")) or {})["target_companies"]


class Response:
    def __init__(self, body: str, *, url: str, content_type: str) -> None:
        self.status_code = 200
        self.url = url
        self.content = body.encode("utf-8")
        self.text = body
        self.headers = {"content-type": content_type}
        self.history: list[object] = []


class OriginalMasterRecoveryBatchHArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: Bloomberg")]
        cls.historical = entries_from_bytes(cls.historical_bytes)
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.rows = list(csv.DictReader(handle))

    def test_activation_preserves_exact_325_entry_production_prefix(self) -> None:
        self.assertEqual(330, len(self.production))
        self.assertEqual(330, len(self.candidates))
        self.assertEqual(330, len(self.validated))
        self.assertEqual(325, len(self.historical))
        self.assertEqual(self.historical, self.production[:325])
        self.assertTrue(self.production_bytes.startswith(self.historical_bytes))
        self.assertEqual(4, len(reconciliation.load_parent_source_mappings(batch.PARENT_MAPPINGS)))
        self.assertEqual(VALIDATED, [item["name"] for item in self.candidates[-5:]])
        self.assertEqual(VALIDATED, [item["name"] for item in self.validated[-5:]])
        self.assertEqual(VALIDATED, [item["name"] for item in self.production[-5:]])
        self.assertEqual(self.validated[-5:], self.production[-5:])
        production_names = [item["name"] for item in self.production]
        self.assertTrue(EXCLUDED.isdisjoint(production_names))
        for name in VALIDATED:
            self.assertEqual(1, production_names.count(name))

    def test_report_projects_only_validated_master_recoveries(self) -> None:
        totals = self.report["totals"]
        self.assertEqual(5, totals["validated"])
        self.assertEqual(10, totals["excluded"])
        self.assertEqual(330, totals["projected_production_sources"])
        self.assertEqual(201, totals["projected_original_master_coverage"])
        self.assertEqual(VALIDATED, totals["original_master_companies_recovered_names"])
        self.assertEqual(
            {
                "anonymous_contract_disabled": 2,
                "blocked": 7,
                "network_inaccessible": 1,
                "validated_with_jobs": 5,
            },
            totals["statuses"],
        )

        master = reconciliation.reconcile_master(
            reconciliation.load_master_workbook(batch.MASTER_WORKBOOK),
            reconciliation.load_target_companies(str(batch.PRODUCTION)),
            reconciliation.load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(246, master.total)
        self.assertEqual(201, len(master.canonical_names) + len(master.mapped_names))

    def test_every_requested_company_has_one_result_and_inventory_row(self) -> None:
        self.assertEqual(batch.REVIEWED_COMPANIES, [item["company"] for item in self.report["results"]])
        self.assertEqual(batch.REVIEWED_COMPANIES, [row["company"] for row in self.rows])
        result_by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(set(VALIDATED), {name for name, item in result_by_name.items() if item["safe_to_activate"]})
        self.assertEqual(EXCLUDED, {name for name, item in result_by_name.items() if not item["safe_to_activate"]})

    def test_validation_was_bounded_repeatable_and_read_only(self) -> None:
        for item in self.report["results"]:
            if item["company"] not in VALIDATED:
                continue
            self.assertEqual(2, len(item["runs"]))
            self.assertEqual(
                item["runs"][0]["stable_ids"], item["runs"][1]["stable_ids"]
            )
            self.assertEqual(20, item["jobs_normalized"])
            self.assertEqual("stable_across_two_runs", item["stable_id_result"])
            self.assertEqual(
                "stable_official_and_job_specific_across_two_runs",
                item["apply_url_result"],
            )
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

    def test_new_platforms_are_registered_without_browser_adapter(self) -> None:
        self.assertIs(ADAPTERS["beesite"], beesite.fetch_jobs)
        self.assertIs(ADAPTERS["mcloud_cws"], mcloud_cws.fetch_jobs)
        candidate_by_name = {item["name"]: item for item in self.candidates[-5:]}
        self.assertEqual("avature", candidate_by_name["Bloomberg"]["platform"])
        self.assertEqual("beesite", candidate_by_name["Mercedes-Benz"]["platform"])
        self.assertEqual("beesite", candidate_by_name["Porsche"]["platform"])
        self.assertEqual(
            "javascript_embedded_data", candidate_by_name["Continental"]["platform"]
        )
        self.assertEqual("mcloud_cws", candidate_by_name["Stellantis"]["platform"])
        self.assertNotIn(
            "browser_rendered_html", {item["platform"] for item in self.candidates[-5:]}
        )


class BeeSiteAdapterTests(unittest.TestCase):
    @staticmethod
    def target(**changes: object) -> CompanyTarget:
        values: dict[str, object] = {
            "name": "Porsche",
            "platform": "beesite",
            "endpoint_url": "https://api.example.com/search/",
            "endpoint_variant": "gjb_search_v1",
            "locale": "en",
            "job_attribution_regex": r"^Porsche",
            "allowed_source_hosts": ["api.example.com"],
            "allowed_apply_hosts": ["jobs.example.com"],
            "page_size": 1,
            "max_pages": 2,
            "max_jobs": 2,
        }
        values.update(changes)
        return CompanyTarget(**values)

    @staticmethod
    def payload(identifier: int, organization: str = "Porsche AG") -> dict:
        return {
            "MatchedObjectId": str(identifier),
            "MatchedObjectDescriptor": {
                "ID": str(identifier),
                "PositionID": f"J{identifier}",
                "PositionTitle": f"Intern {identifier}",
                "PositionURI": f"https://jobs.example.com/job/{identifier}",
                "PublicationStartDate": "2026-08-11",
                "ParentOrganizationName": organization,
                "OrganizationName": "Porsche Test",
                "PositionLocation": [
                    {"CityName": "Atlanta", "CountryName": "United States"}
                ],
            },
        }

    def test_two_pages_normalize_stable_ids(self) -> None:
        parent = self

        class Session:
            def get(self, url: str, **kwargs: object) -> Response:
                data = json.loads(kwargs["params"]["data"])  # type: ignore[index]
                identifier = int(data["SearchParameters"]["FirstItem"])
                body = {
                    "LanguageCode": "EN",
                    "SearchResult": {
                        "SearchResultCount": 1,
                        "SearchResultCountAll": 2,
                        "SearchResultItems": [parent.payload(identifier)],
                        "UserArea": {},
                    },
                }
                return Response(json.dumps(body), url=url, content_type="application/json")

        session = Session()
        jobs = beesite.fetch_jobs(session, self.target(), 5)  # type: ignore[arg-type]
        self.assertEqual(2, len(jobs))
        self.assertEqual(2, len({job.job_id for job in jobs}))
        self.assertEqual("Atlanta, United States", jobs[0].location)
        self.assertEqual(2, session.beesite_pagination["pages_fetched"])  # type: ignore[attr-defined]

    def test_attribution_mismatch_fails_closed(self) -> None:
        body = {
            "LanguageCode": "EN",
            "SearchResult": {
                "SearchResultCount": 1,
                "SearchResultCountAll": 1,
                "SearchResultItems": [self.payload(1, "Unrelated Company")],
                "UserArea": {},
            },
        }

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(json.dumps(body), url=url, content_type="application/json")

        with self.assertRaisesRegex(AdapterError, "exact source attribution"):
            beesite.fetch_jobs(Session(), self.target(max_pages=1, max_jobs=1), 5)  # type: ignore[arg-type]


class PublicJsonEndpointTests(unittest.TestCase):
    @staticmethod
    def target(**changes: object) -> CompanyTarget:
        values: dict[str, object] = {
            "name": "Continental",
            "platform": "javascript_embedded_data",
            "listing_url": "https://jobs.example.com/api?page=1",
            "pagination_url_template": "https://jobs.example.com/api?page={page}",
            "endpoint_variant": "public_json_endpoint",
            "embedded_json_path": "result.list",
            "id_field": "uuid",
            "title_field": "title",
            "location_fields": ["city", "country", "mode"],
            "url_field": "url",
            "job_attribution_field": "client",
            "job_attribution_regex": r"^continental$",
            "allowed_source_hosts": ["jobs.example.com"],
            "allowed_apply_hosts": ["jobs.example.com"],
            "max_pages": 1,
            "max_jobs": 2,
        }
        values.update(changes)
        return CompanyTarget(**values)

    def test_direct_json_combines_reviewed_location_fields(self) -> None:
        body = {
            "result": {
                "list": [
                    {
                        "uuid": "stable-1",
                        "title": "Engineering Intern",
                        "city": "Fort Mill",
                        "country": "United States",
                        "mode": "Hybrid Job",
                        "url": "https://jobs.example.com/job/stable-1",
                        "client": "continental",
                    }
                ]
            }
        }

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(json.dumps(body), url=url, content_type="application/json")

        jobs = embedded_data.fetch_jobs(Session(), self.target(), 5)  # type: ignore[arg-type]
        self.assertEqual(1, len(jobs))
        self.assertEqual("Fort Mill | United States | Hybrid Job", jobs[0].location)

    def test_direct_json_requires_every_row_to_match_attribution(self) -> None:
        body = {
            "result": {
                "list": [
                    {
                        "uuid": "stable-1",
                        "title": "Role",
                        "url": "https://jobs.example.com/job/stable-1",
                        "client": "other",
                    }
                ]
            }
        }

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                return Response(json.dumps(body), url=url, content_type="application/json")

        with self.assertRaisesRegex(AdapterError, "exact source attribution"):
            embedded_data.fetch_jobs(Session(), self.target(), 5)  # type: ignore[arg-type]


class MCloudAdapterTests(unittest.TestCase):
    @staticmethod
    def target(**changes: object) -> CompanyTarget:
        values: dict[str, object] = {
            "name": "Stellantis",
            "platform": "mcloud_cws",
            "careers_url": "https://careers.example.com/",
            "endpoint_url": "https://api.example.com/api/job/search",
            "endpoint_variant": "google_jobs_v1",
            "organization_id": "companies/16115603-6c1b-4c45-b544-238a4e6c51b3",
            "identity_regex": r"Stellantis",
            "job_attribution_regex": r"FCA|Stellantis",
            "allowed_source_hosts": ["api.example.com"],
            "allowed_apply_hosts": ["careers.example.com"],
            "page_size": 1,
            "max_pages": 1,
            "max_jobs": 1,
        }
        values.update(changes)
        return CompanyTarget(**values)

    @staticmethod
    def identity(organization: str) -> str:
        config = {
            "org": organization,
            "api": "https://api.example.com/api/",
            "job_detail_path": "/job",
        }
        return f"<html><title>Stellantis Careers</title><script>var cws_opts = {json.dumps(config)};</script></html>"

    @staticmethod
    def search() -> str:
        return json.dumps(
            {
                "totalHits": 1,
                "searchResults": [
                    {
                        "job": {
                            "id": 123,
                            "title": "Manufacturing Intern",
                            "company_name": "FCA",
                            "brand": "FCA Group",
                            "primary_city": "Windsor",
                            "primary_state": "ON",
                            "primary_country": "CA",
                            "primary_category": "Manufacturing",
                            "open_date": "2026-08-11T00:00:00",
                            "url": "https://careers.example.com/job/123/manufacturing-intern/",
                        }
                    }
                ],
            }
        )

    def test_official_binding_is_verified_before_search(self) -> None:
        parent = self
        organization = str(self.target().organization_id)

        class Session:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get(self, url: str, **_kwargs: object) -> Response:
                self.calls.append(url)
                if "careers.example.com" in url:
                    return Response(parent.identity(organization), url=url, content_type="text/html")
                return Response(parent.search(), url=url, content_type="application/json")

        session = Session()
        jobs = mcloud_cws.fetch_jobs(session, self.target(), 5)  # type: ignore[arg-type]
        self.assertEqual(1, len(jobs))
        self.assertEqual(["https://careers.example.com/", "https://api.example.com/api/job/search"], session.calls)
        self.assertTrue(session.mcloud_cws_pagination["identity_binding_verified"])  # type: ignore[attr-defined]
        self.assertEqual("Windsor, ON, CA", jobs[0].location)

    def test_changed_official_organization_fails_before_api(self) -> None:
        parent = self

        class Session:
            def get(self, url: str, **_kwargs: object) -> Response:
                body = parent.identity("companies/00000000-0000-0000-0000-000000000000")
                return Response(body, url=url, content_type="text/html")

        with self.assertRaisesRegex(AdapterError, "configured organization"):
            mcloud_cws.fetch_jobs(Session(), self.target(), 5)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
