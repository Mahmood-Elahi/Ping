from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import company_watchlist_common
from scripts import investigate_remaining_companies
from scripts import investigate_unknown_companies
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import hawksearch_v2, smartrecruiters
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "config" / "company_careers_adapter_expansion_v1_batch_b_validation.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
BASELINE_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_f_validated.yaml"


class Response:
    def __init__(self, value, *, status=200, url="https://jobs.test"):
        self.status_code = status
        self.url = url
        self._value = value
        self.text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        self.content = self.text.encode("utf-8")
        self.headers = {
            "content-type": (
                "application/json; charset=utf-8"
                if isinstance(value, (dict, list))
                else "text/html"
            )
        }

    def json(self):
        if isinstance(self._value, Exception):
            raise self._value
        if isinstance(self._value, (dict, list)):
            return self._value
        return json.loads(self.text)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request")
        return self.responses.pop(0)


def smartrecruiters_target(**overrides):
    values = {
        "name": "Example Group",
        "platform": "smartrecruiters",
        "slug": "ExampleGroup",
        "careers_url": "https://careers.example.com/jobs",
        "endpoint_url": (
            "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings"
        ),
        "endpoint_variant": "companies_postings_v1",
        "identity_regex": r"^Example Group$",
        "allowed_apply_hosts": ["jobs.smartrecruiters.com"],
        "page_size": 1,
        "max_pages": 1,
        "max_jobs": 1,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def smartrecruiters_item(job_id="744000142609254", *, company="Example Group"):
    return {
        "id": job_id,
        "name": "Software Engineering Intern",
        "refNumber": "REF123",
        "company": {"identifier": "ExampleGroup", "name": company},
        "releasedDate": "2026-08-10T10:43:36.683Z",
        "location": {"fullLocation": "Calgary, AB, Canada"},
        "department": {"id": "engineering", "label": "Engineering"},
        "function": {"id": "it", "label": "Information Technology"},
        "ref": (
            "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings/"
            + job_id
        ),
    }


def smartrecruiters_listing(job_id="744000142609254", *, total=1, offset=0):
    return Response(
        {
            "offset": offset,
            "limit": 1,
            "totalFound": total,
            "content": [smartrecruiters_item(job_id)],
        }
    )


def smartrecruiters_detail(
    job_id="744000142609254", *, company="Example Group", apply_host="jobs.smartrecruiters.com"
):
    item = smartrecruiters_item(job_id, company=company)
    item.update(
        {
            "applyUrl": (
                f"https://{apply_host}/ExampleGroup/{job_id}-software-engineering-intern"
                "?oga=true"
            ),
            "jobAd": {
                "sections": {
                    "jobDescription": {
                        "title": "Job Description",
                        "text": "<p>Build reliable software.</p>",
                    }
                }
            },
        }
    )
    return Response(item)


def hawk_target(**overrides):
    values = {
        "name": "Example Mobility",
        "platform": "hawksearch_v2",
        "careers_url": "https://careers.example.com/jobs/search",
        "endpoint_url": "https://example.searchapi-na.hawksearch.com/api/v2/search/",
        "endpoint_variant": "hawksearch_v2_search",
        "client_guid": "28fad22cfe584b879917858203dd97ce",
        "identity_regex": r"\bExample Mobility\b",
        "locale": "en",
        "allowed_source_hosts": [
            "careers.example.com",
            "example.searchapi-na.hawksearch.com",
        ],
        "allowed_apply_hosts": ["example.wd5.myworkdayjobs.com"],
        "page_size": 1,
        "max_pages": 1,
        "max_jobs": 1,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def hawk_identity():
    return Response(
        """
        <html><title>Example Mobility careers</title><body>
        Example Mobility
        28fad22cfe584b879917858203dd97ce
        https://example.searchapi-na.hawksearch.com/api/v2/search/
        </body></html>
        """,
        url="https://careers.example.com/jobs/search",
    )


def hawk_result(
    job_id="J000702285",
    *,
    doc_id=None,
    apply_host="example.wd5.myworkdayjobs.com",
):
    return {
        "DocId": doc_id or job_id,
        "Document": {
            "jobrequisitionid": [job_id],
            "title": ["Embedded Software Intern"],
            "location": ["Troy, Michigan, United States"],
            "primarylocation": ["Troy, Michigan, United States"],
            "link": [f"https://careers.example.com/jobs/search/open-positions/{job_id}"],
            "externalapplyurl": [
                f"https://{apply_host}/CAREERS/job/Troy/Embedded-Software-Intern_{job_id}/apply"
            ],
            "created_moment": ["2026-08-07T05:32:27.200-07:00"],
            "category": ["Engineering"],
            "jobfamily": ["Software Engineering"],
            "jobdescription": ["<p>Develop embedded software.</p>"],
        },
    }


def hawk_page(
    job_id="J000702285", *, total=1, page=1, pages=None, doc_id=None, apply_host=None
):
    expected_pages = total if pages is None else pages
    kwargs = {"doc_id": doc_id}
    if apply_host is not None:
        kwargs["apply_host"] = apply_host
    return Response(
        {
            "Results": [hawk_result(job_id, **kwargs)],
            "Pagination": {
                "NofResults": total,
                "CurrentPage": page,
                "MaxPerPage": 1,
                "NofPages": expected_pages,
            },
        }
    )


class SmartRecruitersAdapterTests(unittest.TestCase):
    def test_normalizes_stable_identity_location_detail_and_apply_contract(self):
        session = Session([smartrecruiters_listing(), smartrecruiters_detail()])
        jobs = smartrecruiters.fetch_jobs(session, smartrecruiters_target())

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "744000142609254")
        self.assertEqual(job.title, "Software Engineering Intern")
        self.assertEqual(job.location, "Calgary, AB, Canada")
        self.assertEqual(job.updated_at, "2026-08-10")
        self.assertEqual(job.departments, ["Engineering", "Information Technology"])
        self.assertIn(job.job_id, job.url)
        self.assertEqual(job.raw_metadata["detail_url"], job.url)
        self.assertIn(job.job_id, job.raw_metadata["api_detail_url"])
        self.assertEqual(session.calls[0][2]["params"], {"limit": 1, "offset": 0})

    def test_identity_crossing_and_unofficial_apply_urls_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "mixed or ambiguous"):
            smartrecruiters.fetch_jobs(
                Session([smartrecruiters_listing(), smartrecruiters_detail(company="Other")]),
                smartrecruiters_target(),
            )
        with self.assertRaisesRegex(AdapterError, "official job-specific apply"):
            smartrecruiters.fetch_jobs(
                Session(
                    [
                        smartrecruiters_listing(),
                        smartrecruiters_detail(apply_host="evil.test"),
                    ]
                ),
                smartrecruiters_target(),
            )

    @patch.object(smartrecruiters.time, "sleep", return_value=None)
    def test_pagination_is_bounded_and_total_must_not_change(self, _sleep):
        first_id = "744000142609254"
        second_id = "744000142609255"
        responses = [
            smartrecruiters_listing(first_id, total=2, offset=0),
            smartrecruiters_detail(first_id),
            smartrecruiters_listing(second_id, total=3, offset=1),
        ]
        with self.assertRaisesRegex(AdapterError, "changed during pagination"):
            smartrecruiters.fetch_jobs(
                Session(responses),
                smartrecruiters_target(max_pages=2, max_jobs=2),
            )

    def test_captcha_shape_and_invalid_limits_fail_without_fallback(self):
        with self.assertRaisesRegex(AdapterError, "CAPTCHA"):
            smartrecruiters.fetch_jobs(
                Session([Response("verify you are human CAPTCHA")]),
                smartrecruiters_target(),
            )
        with self.assertRaisesRegex(AdapterError, "max_pages"):
            smartrecruiters.fetch_jobs(Session([]), smartrecruiters_target(max_pages=6))


class HawkSearchV2AdapterTests(unittest.TestCase):
    def test_normalizes_correlated_identity_detail_and_apply_urls(self):
        session = Session([hawk_identity(), hawk_page()])
        jobs = hawksearch_v2.fetch_jobs(session, hawk_target())

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "J000702285")
        self.assertEqual(job.title, "Embedded Software Intern")
        self.assertEqual(job.location, "Troy, Michigan, United States")
        self.assertEqual(job.updated_at, "2026-08-07")
        self.assertIn(job.job_id, job.url)
        self.assertIn(job.job_id, job.raw_metadata["detail_url"])
        call = session.calls[1]
        self.assertEqual(call[0], "post")
        self.assertEqual(call[2]["json"]["PageNo"], 1)
        self.assertEqual(call[2]["json"]["MaxPerPage"], 1)
        self.assertIsNone(call[2]["json"]["ClientData"]["VisitId"])

    def test_doc_identity_and_apply_host_mismatches_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "DocId"):
            hawksearch_v2.fetch_jobs(
                Session([hawk_identity(), hawk_page(doc_id="J999999999")]), hawk_target()
            )
        with self.assertRaisesRegex(AdapterError, "official job-specific apply"):
            hawksearch_v2.fetch_jobs(
                Session([hawk_identity(), hawk_page(apply_host="evil.test")]), hawk_target()
            )

    @patch.object(hawksearch_v2.time, "sleep", return_value=None)
    def test_pagination_bounds_must_remain_stable(self, _sleep):
        responses = [
            hawk_identity(),
            hawk_page("J000702285", total=2, page=1, pages=2),
            hawk_page("J000702286", total=3, page=2, pages=3),
        ]
        with self.assertRaisesRegex(AdapterError, "bounds changed"):
            hawksearch_v2.fetch_jobs(
                Session(responses), hawk_target(max_pages=2, max_jobs=2)
            )

    def test_captcha_shape_and_unreviewed_endpoint_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "CAPTCHA"):
            hawksearch_v2.fetch_jobs(
                Session(
                    [
                        Response(
                            "access denied CAPTCHA",
                            url="https://careers.example.com/jobs/search",
                        )
                    ]
                ),
                hawk_target(),
            )
        with self.assertRaisesRegex(AdapterError, "v2 search endpoint"):
            hawksearch_v2.fetch_jobs(
                Session([]), hawk_target(endpoint_url="https://evil.test/api/v2/search/")
            )


class BatchBRecoveryValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = yaml.safe_load(VALIDATION.read_text(encoding="utf-8"))
        cls.rows = {row["name"]: row for row in cls.data["recovery_targets"]}

    def test_adapter_decisions_and_recovery_projection(self):
        statuses = {row["platform"]: row["implementation_status"] for row in self.data["adapters"]}
        self.assertEqual(
            statuses,
            {
                "smartrecruiters": "implemented",
                "hawksearch_v2": "implemented",
                "continental_rest": "rejected",
            },
        )
        ready = {name for name, row in self.rows.items() if row["production_ready"]}
        self.assertEqual(ready, {"Bosch", "Aptiv"})
        self.assertEqual(self.data["projection"]["jobs_normalized"], 40)
        self.assertEqual(self.data["projection"]["current_student_intern_hits"], 0)
        self.assertEqual(
            (
                self.data["projection"]["production_sources"],
                self.data["projection"]["original_master_coverage"],
                self.data["projection"]["original_master_total"],
            ),
            (257, 188, 246),
        )

    def test_only_production_ready_adapters_are_registered(self):
        self.assertIs(scanner.ADAPTERS["smartrecruiters"], smartrecruiters.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["hawksearch_v2"], hawksearch_v2.fetch_jobs)
        self.assertNotIn("continental_rest", scanner.ADAPTERS)
        self.assertFalse(
            (ROOT / "src" / "scraper" / "company_careers" / "adapters" / "continental_rest.py").exists()
        )

    def test_registry_validator_knows_both_sources_and_contracts(self):
        expected = {
            "Bosch": "smartrecruiters:boschgroup",
            "Aptiv": (
                "hawksearch_v2:aptivcareers.searchapi-na.hawksearch.com:"
                "28fad22cfe584b879917858203dd97ce"
            ),
        }
        for name, source in expected.items():
            with self.subTest(name=name):
                row = self.rows[name]
                self.assertIsNone(registry_validator.configuration_error(row))
                self.assertEqual(registry_validator.source_key(row), source)

    def test_only_ready_targets_are_appended_to_the_exact_255_entry_prefix(self):
        # The validation artifact remains immutable milestone evidence.
        self.assertFalse(self.data["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.data["publication"].values()))

        production_data = yaml.safe_load(PRODUCTION.read_bytes())["target_companies"]
        baseline_count = self.data["baseline"]["production_sources"]
        baseline = yaml.safe_load(BASELINE_REGISTRY.read_bytes())["target_companies"]
        self.assertEqual(len(baseline), baseline_count)
        assert_historical_prefix(self, production_data, baseline)
        projection_count = self.data["projection"]["production_sources"]
        promoted = ["Bosch", "Aptiv"]
        self.assertGreaterEqual(len(production_data), projection_count)
        self.assertEqual([row["name"] for row in production_data[baseline_count:projection_count]], promoted)
        self.assertEqual(
            len(load_parent_source_mappings()),
            self.data["baseline"]["parent_mappings"] + 1,
        )

        runtime_fields = {
            "smartrecruiters": (
                "name", "platform", "slug", "careers_url", "endpoint_url",
                "endpoint_variant", "identity_regex", "allowed_apply_hosts",
                "page_size", "max_pages", "max_jobs",
            ),
            "hawksearch_v2": (
                "name", "platform", "careers_url", "endpoint_url",
                "endpoint_variant", "client_guid", "identity_regex", "locale",
                "allowed_source_hosts", "allowed_apply_hosts", "page_size",
                "max_pages", "max_jobs",
            ),
        }
        for production_row in production_data[baseline_count:projection_count]:
            validated_row = self.rows[production_row["name"]]
            fields = runtime_fields[production_row["platform"]]
            self.assertEqual(production_row, {field: validated_row[field] for field in fields})

        names = [row["name"] for row in production_data]
        for name in promoted:
            self.assertEqual(names.count(name), 1)
        self.assertEqual(names.count("Continental"), 1)
        self.assertNotIn("Copperleaf", names)
        self.assertNotIn("IFS1", {row.get("slug") for row in production_data})

        renesas = next(row for row in production_data if row["name"] == "Renesas Electronics")
        self.assertEqual(names.count("Renesas Electronics"), 1)
        self.assertEqual(renesas["platform"], "static_html")

    def test_additional_smartrecruiters_candidates_are_safely_classified(self):
        rows = {
            row["name"]: row for row in self.data["additional_smartrecruiters_candidates"]
        }
        self.assertTrue(rows["Renesas Electronics"]["production_ready"])
        self.assertEqual(rows["Renesas Electronics"]["jobs_normalized_each_run"], 20)
        self.assertEqual(rows["Renesas Electronics"]["current_student_intern_hits_each_run"], 1)
        self.assertFalse(rows["Copperleaf"]["production_ready"])
        self.assertIn("mixes", rows["Copperleaf"]["reason"])

    def test_generic_adapters_have_no_company_specific_branches(self):
        source = (
            inspect.getsource(smartrecruiters) + inspect.getsource(hawksearch_v2)
        ).casefold()
        for company in ("bosch", "aptiv", "continental", "renesas", "copperleaf"):
            self.assertNotIn(company, source)

    def test_research_inventory_no_longer_classifies_smartrecruiters_as_future(self):
        self.assertIn(
            "smartrecruiters", investigate_remaining_companies.EXISTING_ADAPTER_FAMILIES
        )
        self.assertIn("smartrecruiters", investigate_unknown_companies.EXISTING_ADAPTERS)
        self.assertNotIn(
            "smartrecruiters", investigate_unknown_companies.KNOWN_FUTURE_FAMILIES
        )
        self.assertNotIn(
            "smartrecruiters", company_watchlist_common.DETECTED_BUT_UNSUPPORTED
        )
        self.assertIn(
            "smartrecruiters", company_watchlist_common.DETECTED_REQUIRES_REVIEW
        )


if __name__ == "__main__":
    unittest.main()
