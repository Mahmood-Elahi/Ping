from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.scraper.company_careers.adapters import embedded_data
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)


class Response:
    def __init__(self, body: str, status: int = 200, content_type: str = "text/html"):
        self.text = body
        self.content = body.encode()
        self.status_code = status
        self.url = "https://careers.example.test/jobs"
        self.headers = {"content-type": content_type}
        self.history = []


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        value.url = url
        return value


def item(job_id="1", title="Engineer", url="https://apply.example.test/jobs/1", **extra):
    return {"id": job_id, "title": title, "url": url, "locations": ["Remote", "Calgary"],
            "date": "2026-08-01", "departments": ["Engineering"], "mode": "Hybrid", **extra}


def document(payload, selector_id="state", script_type="application/json", extra=""):
    body = json.dumps(payload)
    return f"<html><title>Jobs</title><body><script id='{selector_id}' type='{script_type}'>{body}</script>{extra}</body></html>"


def target(**overrides):
    values = dict(name="Example", platform="javascript_embedded_data", listing_url="https://careers.example.test/jobs",
                  endpoint_variant="inline_json", embedded_script_selector="script#state",
                  embedded_json_path="data.jobs", id_field="id", title_field="title", url_field="url",
                  location_field="locations", date_field="date", department_field="departments",
                  remote_field="mode", allowed_apply_hosts=["apply.example.test"], max_pages=1, max_jobs=20)
    values.update(overrides)
    return CompanyTarget(**values)


class EmbeddedDataAdapterTests(unittest.TestCase):
    def test_01_json_ld_job_posting_normalization(self):
        posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "ML Engineer",
                   "identifier": {"value": "abc"}, "url": "https://apply.example.test/jobs/abc",
                   "datePosted": "2026-08-01", "jobLocationType": "TELECOMMUTE",
                   "jobLocation": [{"address": {"addressLocality": "Calgary", "addressRegion": "AB", "addressCountry": "CA"}}]}
        html = document(posting, selector_id="", script_type="application/ld+json")
        t = target(endpoint_variant="json_ld_job_posting", embedded_script_selector=None,
                   embedded_json_path=None, id_field=None, title_field=None, url_field=None)
        job = embedded_data.fetch_jobs(Session(Response(html)), t)[0]
        self.assertEqual((job.title, job.location), ("ML Engineer", "Calgary, AB, CA | TELECOMMUTE"))

    def test_02_inline_json_normalization(self):
        job = embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": [item()]}}))), target())[0]
        self.assertEqual(job.departments, ["Engineering"])

    def test_03_next_data_normalization(self):
        t = target(endpoint_variant="next_data", embedded_script_selector="script#__NEXT_DATA__", embedded_json_path="props.pageProps.jobs")
        html = document({"props": {"pageProps": {"jobs": [item()]}}}, selector_id="__NEXT_DATA__")
        self.assertEqual(embedded_data.fetch_jobs(Session(Response(html)), t)[0].title, "Engineer")

    def test_03b_next_data_prismic_document_schema_falls_back_from_legacy_fields(self):
        current = {
            "id": "aojBphEAAC4ADcNf",
            "url": "/jobs/production-superintendent",
            "type": "job_posting",
            "data": {"title": "Production Superintendent"},
        }
        t = target(
            endpoint_variant="next_data",
            embedded_script_selector="script#__NEXT_DATA__",
            embedded_json_path="props.pageProps.jobs",
            id_field="link.id",
            title_field="label",
            url_field="link.url",
            location_field=None,
            date_field=None,
            department_field=None,
            remote_field=None,
            listing_url="https://careers.example.test/jobs",
            allowed_apply_hosts=["careers.example.test"],
        )
        html = document(
            {"props": {"pageProps": {"jobs": [current]}}},
            selector_id="__NEXT_DATA__",
        )
        job = embedded_data.fetch_jobs(Session(Response(html)), t)[0]
        self.assertEqual(job.title, "Production Superintendent")
        self.assertTrue(job.job_id.endswith(":aojBphEAAC4ADcNf"))
        self.assertEqual(job.url, "https://careers.example.test/jobs/production-superintendent")

    def test_03c_next_data_legacy_prismic_schema_remains_supported(self):
        legacy = {
            "label": "Legacy Engineer",
            "link": {"id": "legacy-1", "url": "https://apply.example.test/jobs/legacy-1"},
        }
        t = target(
            endpoint_variant="next_data",
            embedded_script_selector="script#__NEXT_DATA__",
            embedded_json_path="props.pageProps.jobs",
            id_field="link.id",
            title_field="label",
            url_field="link.url",
            location_field=None,
            date_field=None,
            department_field=None,
            remote_field=None,
        )
        html = document(
            {"props": {"pageProps": {"jobs": [legacy]}}},
            selector_id="__NEXT_DATA__",
        )
        self.assertEqual(embedded_data.fetch_jobs(Session(Response(html)), t)[0].title, "Legacy Engineer")

    def test_04_reviewed_json_path_enforcement(self):
        with self.assertRaisesRegex(AdapterError, "missing component"):
            embedded_data.fetch_jobs(Session(Response(document({"other": []}))), target())

    def test_05_stable_id_and_apply_host(self):
        job = embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": [item("stable-7")]}}))), target())[0]
        self.assertTrue(job.job_id.endswith(":stable-7"))
        with self.assertRaisesRegex(AdapterError, "allowed-host"):
            embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": [item(url="https://evil.test/1")]}}))), target())

    def test_06_multiple_locations_date_department_remote(self):
        job = embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": [item()]}}))), target())[0]
        self.assertEqual(job.offices, ["Remote", "Calgary", "Hybrid"])
        self.assertEqual((job.updated_at, job.departments), ("2026-08-01", ["Engineering"]))
        self.assertEqual(job.raw_metadata["work_modes"], ["Remote", "Hybrid"])

    @patch("src.scraper.company_careers.adapters.embedded_data.time.sleep", lambda _: None)
    def test_07_deterministic_pagination(self):
        session = Session(
            Response(document({"data": {"jobs": [item("1")]}})),
            Response(document({"data": {"jobs": [item("2", url="https://apply.example.test/jobs/2")]}})),
            Response(document({"data": {"jobs": []}})),
        )
        jobs = embedded_data.fetch_jobs(session, target(max_pages=2, pagination_url_template="https://careers.example.test/jobs?page={page}"))
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(session.calls), 3)

    @patch("src.scraper.company_careers.adapters.embedded_data.time.sleep", lambda _: None)
    def test_08_repeated_data_termination(self):
        body = document({"data": {"jobs": [item()]}})
        session = Session(Response(body), Response(body))
        embedded_data.fetch_jobs(session, target(max_pages=2, pagination_url_template="https://careers.example.test/jobs?page={page}"))
        self.assertEqual(session.embedded_data_pagination["stop_reason"], "repeated_data")

    @patch("src.scraper.company_careers.adapters.embedded_data.time.sleep", lambda _: None)
    def test_09_no_new_id_termination(self):
        a = document({"data": {"jobs": [item("1", url="https://apply.example.test/jobs/a")]}})
        b = document({"data": {"jobs": [item("1", url="https://apply.example.test/jobs/b")]}})
        session = Session(Response(a), Response(b))
        with self.assertRaisesRegex(AdapterError, "repeated stable job ID"):
            embedded_data.fetch_jobs(session, target(max_pages=2, pagination_url_template="https://careers.example.test/jobs?page={page}"))
        self.assertEqual(session.embedded_data_pagination["stop_reason"], "repeated_job_id")

    def test_10_valid_empty_embedded_collection(self):
        session = Session(Response(document({"data": {"jobs": []}})))
        self.assertEqual(embedded_data.fetch_jobs(session, target()), [])
        self.assertTrue(session.embedded_data_pagination["zero_job_contract"])

    def test_11_malformed_json_rejection(self):
        with self.assertRaisesRegex(AdapterError, "malformed JSON"):
            embedded_data.fetch_jobs(Session(Response("<html><script id='state' type='application/json'>{bad</script></html>")), target())

    def test_12_generic_script_bundle_rejection(self):
        body = "<html><script id='state' src='/bundle.js'></script></html>"
        with self.assertRaisesRegex(AdapterError, "external script bundles"):
            embedded_data.fetch_jobs(Session(Response(body)), target())

    def test_13_script_count_limit(self):
        scripts = "".join(f"<script class='state' type='application/json'>{{\"data\":{{\"jobs\":[]}}}}</script>" for _ in range(3))
        with self.assertRaisesRegex(AdapterError, "script-count"):
            embedded_data.fetch_jobs(Session(Response(f"<html>{scripts}</html>")), target(embedded_script_selector="script.state", max_scripts=2))

    def test_14_script_and_response_byte_limits(self):
        with self.assertRaisesRegex(AdapterError, "script byte"):
            embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": [], "pad": "x" * 100}}))), target(max_script_bytes=20))
        with self.assertRaisesRegex(AdapterError, "response exceeded"):
            embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": []}}))), target(max_response_bytes=10))

    def test_15_authentication_browser_session_blocked_rejection(self):
        bodies = [("<html><title>Login</title></html>", "authentication"),
                  ("<html>Enable JavaScript to continue</html>", "browser"),
                  ("<html><title>Session Expired</title></html>", "session"),
                  ("<html>Cloudflare challenge</html>", "blocked")]
        for body, message in bodies:
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                embedded_data.fetch_jobs(Session(Response(body)), target())

    @patch("src.scraper.company_careers.adapters.embedded_data.time.sleep", lambda _: None)
    def test_16_retry_bounds(self):
        session = Session(Response("", 503), Response(document({"data": {"jobs": []}})))
        self.assertEqual(embedded_data.fetch_jobs(session, target()), [])
        self.assertEqual(len(session.calls), 2)

    def test_17_page_limit_and_complete_collection_adaptation(self):
        with self.assertRaisesRegex(AdapterError, "hard maximum"):
            embedded_data.validate_target(target(max_pages=3))
        jobs = [item(str(i), url=f"https://apply.example.test/jobs/{i}") for i in range(10)]
        session = Session(Response(document({"data": {"jobs": jobs}})))
        self.assertEqual(len(embedded_data.fetch_jobs(session, target(max_jobs=3))), 10)
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["complete_collection_response"])
        self.assertEqual(metadata["terminal_evidence_type"], "complete_collection_response")

    def test_18_unsupported_schema_and_path_rejection(self):
        with self.assertRaisesRegex(AdapterError, "unsupported variant"):
            embedded_data.validate_target(target(endpoint_variant="nuxt_reference_state"))
        with self.assertRaisesRegex(AdapterError, "JSON path is invalid"):
            embedded_data.validate_target(target(embedded_json_path="data.*"))

    def test_19_fallback_id_and_apply_template(self):
        raw = item(job_id="", url="slug", slug="job-slug")
        t = target(id_field=None, allow_url_id_fallback=True, apply_url_template="https://apply.example.test/jobs/{slug}")
        job = embedded_data.fetch_jobs(Session(Response(document({"data": {"jobs": [raw]}}))), t)[0]
        self.assertEqual(job.url, "https://apply.example.test/jobs/job-slug")
        self.assertTrue(job.job_id)

    def test_20_no_javascript_execution_surface(self):
        import inspect
        source = inspect.getsource(embedded_data)
        for forbidden in ("eval(", "exec(", "playwright", "selenium"):
            self.assertNotIn(forbidden, source.casefold())

    def test_21_non_html_and_missing_script_rejected(self):
        with self.assertRaises(AdapterError):
            embedded_data.fetch_jobs(Session(Response("{}", content_type="application/json")), target())
        with self.assertRaisesRegex(AdapterError, "did not match"):
            embedded_data.fetch_jobs(Session(Response("<html><title>Jobs</title></html>")), target())

    def test_22_explicit_public_json_reference(self):
        page = "<html><title>Jobs</title><script data-jobs-url='https://careers.example.test/api/jobs'></script></html>"
        payload = json.dumps({"data": {"jobs": [item()]}})
        session = Session(Response(page), Response(payload, content_type="application/json"))
        t = target(endpoint_variant="public_json_reference", embedded_script_selector="script[data-jobs-url]",
                   endpoint_url="https://careers.example.test/api/jobs", reference_url_attribute="data-jobs-url")
        self.assertEqual(embedded_data.fetch_jobs(session, t)[0].title, "Engineer")

    def test_23_next_flight_collection_normalization(self):
        jobs = [item("flight-7", url="/company/careers/engineer-flight-7/")]
        stream = "127:[\"$\",\"section\",null,{\"positions\":" + json.dumps(jobs) + "}]\n"
        wrapper = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        page = f"<html><title>Jobs</title><script>{wrapper}</script></html>"
        flight_target = target(
            endpoint_variant="next_flight_collection",
            embedded_script_selector="script",
            embedded_json_path="positions",
            id_field=None,
            external_id_regex=r"-([a-z0-9-]+)/$",
            listing_url="https://apply.example.test/company/careers/",
        )
        job = embedded_data.fetch_jobs(Session(Response(page)), flight_target)[0]
        self.assertEqual(job.title, "Engineer")
        self.assertEqual(job.url, "https://apply.example.test/company/careers/engineer-flight-7/")
        self.assertTrue(job.job_id.endswith(":flight-7"))

    def test_24_next_flight_is_bounded_and_fail_closed(self):
        duplicate = "self.__next_f.push(" + json.dumps([1, '\"positions\":[]']) + ")"
        page = f"<html><script>{duplicate}</script><script>{duplicate}</script></html>"
        flight_target = target(
            endpoint_variant="next_flight_collection",
            embedded_script_selector="script",
            embedded_json_path="positions",
        )
        with self.assertRaisesRegex(AdapterError, "exactly once"):
            embedded_data.fetch_jobs(Session(Response(page)), flight_target)
        with self.assertRaisesRegex(AdapterError, "top-level field"):
            embedded_data.validate_target(
                target(
                    endpoint_variant="next_flight_collection",
                    embedded_script_selector="script",
                    embedded_json_path="props.positions",
                )
            )

    def test_25_public_json_uses_only_declared_result_pagination(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.list",
            pagination_url_template="https://careers.example.test/jobs?page={page}",
            pagination_contract="numeric_page_index",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        job = item(client="example")
        payload = {
            "result": {
                "list": [job],
                "total": 1,
                "pagination": {"currentPage": 1, "nextPage": None, "lastPage": 1},
            },
            "facets": {"count": 999},
            "aggregations": {"hits": 888},
            "analytics": {"total": 777},
        }
        session = Session(
            Response(json.dumps(payload), content_type="application/json")
        )

        self.assertEqual(len(embedded_data.fetch_jobs(session, public_target)), 1)
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 1)
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")

    def test_26_public_json_conflicting_declared_totals_stays_incomplete(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.list",
            pagination_url_template="https://careers.example.test/jobs?page={page}",
            pagination_contract="numeric_page_index",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        first = {
            "result": {
                "list": [item(client="example")],
                "total": 1,
                "totalCount": 2,
                "pagination": {"currentPage": 1, "nextPage": None, "lastPage": 1},
            }
        }
        second = {
            "result": {
                "list": [],
                "total": 1,
                "totalCount": 2,
                "pagination": {"currentPage": 2, "nextPage": None, "lastPage": 2},
            }
        }
        session = Session(
            Response(json.dumps(first), content_type="application/json"),
            Response(json.dumps(second), content_type="application/json"),
        )

        self.assertEqual(len(embedded_data.fetch_jobs(session, public_target)), 1)
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "conflicting_pagination_evidence",
        )

    def test_27_public_json_string_page_number_is_diagnosed_not_coerced(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.list",
            pagination_url_template="https://careers.example.test/jobs?page={page}",
            pagination_contract="numeric_page_index",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        payload = {
            "result": {
                "list": [item(client="example")],
                "pagination": {"currentPage": "1", "nextPage": 2, "totalPages": 65},
            }
        }
        session = Session(
            Response(json.dumps(payload), content_type="application/json"),
            Response(
                json.dumps(
                    {
                        "result": {
                            "list": [],
                            "pagination": {
                                "currentPage": "2",
                                "nextPage": 3,
                                "totalPages": 65,
                            },
                        }
                    }
                ),
                content_type="application/json",
            ),
        )

        self.assertEqual(len(embedded_data.fetch_jobs(session, public_target)), 1)
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["page_count"], 65)
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "malformed_pagination_evidence",
        )
        self.assertIn(
            "result.pagination.currentPage",
            metadata["pagination_malformed_fields"],
        )
        self.assertIn(
            {
                "field": "result.pagination.currentPage",
                "type": "str",
                "value": "2",
            },
            metadata["pagination_field_diagnostics"],
        )

    def test_28_public_json_numeric_page_contract_reaches_explicit_final_page(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.list",
            pagination_url_template="https://careers.example.test/jobs?page={page}",
            pagination_contract="numeric_page_index",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        first = {
            "result": {
                "list": [item("1", client="example")],
                "pagination": {"currentPage": 1, "nextPage": 2, "lastPage": 2},
            }
        }
        second = {
            "result": {
                "list": [
                    item("2", url="https://apply.example.test/jobs/2", client="example")
                ],
                "pagination": {"currentPage": 2, "nextPage": None, "lastPage": 2},
            }
        }
        session = Session(
            Response(json.dumps(first), content_type="application/json"),
            Response(json.dumps(second), content_type="application/json"),
        )

        jobs = embedded_data.fetch_jobs(session, public_target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "explicit_terminal_page")
        self.assertEqual(metadata["next_page"], None)

    def test_29_public_json_malformed_final_record_blocks_terminal_completion(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.list",
            pagination_url_template="https://careers.example.test/jobs?page={page}",
            pagination_contract="numeric_page_index",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        malformed = item(
            "2", title="", url="https://apply.example.test/jobs/2", client="example"
        )
        payload = {
            "result": {
                "list": [item("1", client="example"), malformed],
                "pagination": {"currentPage": 1, "nextPage": None, "lastPage": 1},
            }
        }
        session = Session(Response(json.dumps(payload), content_type="application/json"))

        jobs = embedded_data.fetch_jobs(session, public_target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertEqual(metadata["stop_reason"], "terminal_reached_with_rejected_records")

    def test_30_complete_collection_total_and_record_filter_are_authoritative(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.data.jobs.edges",
            complete_collection_total_field="result.data.jobs.totalCount",
            id_field="node.strapiId",
            title_field="node.title",
            url_field="node.strapiId",
            apply_url_template="https://apply.example.test/jobs/{id}",
            location_field="node.cities",
            date_field="node.start_date",
            department_field="node.job_fields",
            remote_field=None,
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="node.is_public",
            job_attribution_regex=r"^True$",
            item_filter_field="node.publish",
            item_filter_regex=r"^True$",
        )
        edges = [
            {
                "node": {
                    "strapiId": index,
                    "title": f"Engineer {index}",
                    "publish": published,
                    "is_public": True,
                    "start_date": "2026-09-01",
                    "cities": [{"name": "Calgary"}],
                    "job_fields": [{"name": "Engineering"}],
                }
            }
            for index, published in ((1, True), (2, False), (3, True))
        ]
        payload = {
            "result": {"data": {"jobs": {"totalCount": 3, "edges": edges}}}
        }
        session = Session(
            Response(json.dumps(payload), content_type="application/json")
        )

        jobs = embedded_data.fetch_jobs(session, public_target)
        metadata = source_scan_metadata(session)
        self.assertEqual([job.title for job in jobs], ["Engineer 1", "Engineer 3"])
        self.assertEqual(jobs[0].offices, ["Calgary"])
        self.assertEqual(jobs[0].departments, ["Engineering"])
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["source_collection_total"], 3)
        self.assertEqual(metadata["filtered_collection_total"], 2)
        self.assertTrue(metadata["complete_collection_response"])

    def test_30a_filtered_collection_accepts_valid_matching_and_nonmatching_rows(self):
        filtered_target = target(
            item_filter_field="metadata[3].value",
            item_filter_regex=r"^us$",
        )
        metadata = [{"name": str(index), "value": "unused"} for index in range(3)]
        matching = item("1", metadata=[*metadata, {"name": "locale", "value": "us"}])
        nonmatching = item("2", metadata=[*metadata, {"name": "locale", "value": "eu"}])
        session = Session(
            Response(document({"data": {"jobs": [matching, nonmatching]}}))
        )

        jobs = embedded_data.fetch_jobs(session, filtered_target)
        scan = source_scan_metadata(session)

        self.assertEqual([job.title for job in jobs], ["Engineer"])
        self.assertTrue(scan["scan_complete"])
        self.assertEqual(scan["source_collection_total"], 2)
        self.assertEqual(scan["filtered_collection_total"], 1)

    def test_30b_filtered_collection_rejects_missing_filter_field(self):
        filtered_target = target(
            item_filter_field="metadata[3].value",
            item_filter_regex=r"^us$",
        )
        with self.assertRaisesRegex(AdapterError, "filter evidence is structurally invalid"):
            embedded_data.fetch_jobs(
                Session(Response(document({"data": {"jobs": [item()]}}))),
                filtered_target,
            )

    def test_30c_filtered_collection_rejects_null_and_empty_filter_evidence(self):
        filtered_target = target(
            item_filter_field="metadata[3].value",
            item_filter_regex=r"^us$",
        )
        prefix = [{"value": "unused"} for _ in range(3)]
        for value in (None, "", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(
                AdapterError, "filter evidence is null or empty"
            ):
                embedded_data.fetch_jobs(
                    Session(
                        Response(
                            document(
                                {
                                    "data": {
                                        "jobs": [
                                            item(
                                                metadata=[
                                                    *prefix,
                                                    {"value": value},
                                                ]
                                            )
                                        ]
                                    }
                                }
                            )
                        )
                    ),
                    filtered_target,
                )

    def test_30d_filtered_collection_rejects_malformed_metadata_structure(self):
        filtered_target = target(
            item_filter_field="metadata[3].value",
            item_filter_regex=r"^us$",
        )
        malformed_values = ({"3": {"value": "us"}}, "not-a-list", [1, 2, 3, "us"])
        for metadata in malformed_values:
            with self.subTest(metadata=metadata), self.assertRaisesRegex(
                AdapterError, "filter evidence is structurally invalid"
            ):
                embedded_data.fetch_jobs(
                    Session(
                        Response(
                            document({"data": {"jobs": [item(metadata=metadata)]}})
                        )
                    ),
                    filtered_target,
                )

    def test_30e_filtered_collection_rejects_ambiguous_filter_evidence(self):
        filtered_target = target(
            item_filter_field="metadata[3].value",
            item_filter_regex=r"^us$",
        )
        prefix = [{"value": "unused"} for _ in range(3)]
        for value in (["us"], {"locale": "us"}):
            with self.subTest(value=value), self.assertRaisesRegex(
                AdapterError, "unsupported or ambiguous type"
            ):
                embedded_data.fetch_jobs(
                    Session(
                        Response(
                            document(
                                {
                                    "data": {
                                        "jobs": [
                                            item(metadata=[*prefix, {"value": value}])
                                        ]
                                    }
                                }
                            )
                        )
                    ),
                    filtered_target,
                )

    def test_30f_filtered_collection_rejects_mixture_of_valid_and_malformed_rows(self):
        filtered_target = target(
            item_filter_field="metadata[3].value",
            item_filter_regex=r"^us$",
        )
        prefix = [{"value": "unused"} for _ in range(3)]
        valid = item("1", metadata=[*prefix, {"value": "us"}])
        malformed = item("2", metadata=[*prefix, {}])
        session = Session(
            Response(document({"data": {"jobs": [valid, malformed]}}))
        )

        with self.assertRaisesRegex(AdapterError, "filter evidence is structurally invalid"):
            embedded_data.fetch_jobs(session, filtered_target)
        scan = source_scan_metadata(session)
        self.assertFalse(scan["scan_complete"])
        self.assertEqual(scan["source_collection_total"], 2)
        self.assertNotIn("filtered_collection_total", scan)

    def test_31_complete_collection_total_mismatch_fails_closed(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.jobs",
            complete_collection_total_field="result.totalCount",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        payload = {"result": {"totalCount": 2, "jobs": [item(client="example")]}}
        with self.assertRaisesRegex(AdapterError, "total does not match"):
            embedded_data.fetch_jobs(
                Session(Response(json.dumps(payload), content_type="application/json")),
                public_target,
            )

    def test_32_complete_collection_with_rejected_record_remains_incomplete(self):
        public_target = target(
            endpoint_variant="public_json_endpoint",
            embedded_script_selector=None,
            embedded_json_path="result.jobs",
            complete_collection_total_field="result.totalCount",
            allowed_source_hosts=["careers.example.test"],
            job_attribution_field="client",
            job_attribution_regex=r"^example$",
        )
        payload = {
            "result": {
                "totalCount": 2,
                "jobs": [item("1", client="example"), item("2", title="", client="example")],
            }
        }
        session = Session(Response(json.dumps(payload), content_type="application/json"))

        jobs = embedded_data.fetch_jobs(session, public_target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertEqual(metadata["stop_reason"], "terminal_reached_with_rejected_records")

    def test_33_next_flight_complete_collection_uses_reviewed_expanded_ceiling(self):
        jobs = [
            {
                "id": f"00000000-0000-4000-8000-{index:012d}",
                "header": {
                    "roleTitle": f"Engineer {index}",
                    "parentTeamTitle": "Engineering",
                    "locations": [{"city": "Tallinn", "country": "Estonia"}],
                },
                "body": {
                    "applyLinkProps": {
                        "href": f"/en/careers/positions/00000000-0000-4000-8000-{index:012d}/"
                    },
                    "jobPostedDate": "2026-09-30",
                },
            }
            for index in range(25)
        ]
        stream = '127:["$","section",null,{"parsedJobs":' + json.dumps(jobs) + "}]\n"
        wrapper = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        page = (
            "<html><body><h1>Open roles</h1>"
            "<script>const translations={captcha:'captcha'};</script>"
            f"<script>{wrapper}</script>"
            "</body></html>"
        )
        bolt = target(
            name="Bolt",
            endpoint_variant="next_flight_collection",
            listing_url="https://bolt.eu/en/careers/positions/",
            embedded_script_selector="script",
            embedded_json_path="parsedJobs",
            id_field="id",
            title_field="header.roleTitle",
            url_field="body.applyLinkProps.href",
            location_field="header.locations",
            department_field="header.parentTeamTitle",
            date_field="body.jobPostedDate",
            remote_field=None,
            allowed_apply_hosts=["bolt.eu"],
            max_jobs=1000,
        )
        session = Session(Response(page))

        normalized = embedded_data.fetch_jobs(session, bolt)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(normalized), 25)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(normalized[0].location, "Tallinn, Estonia")
        self.assertEqual(normalized[0].departments, ["Engineering"])

    def test_34_next_flight_ceiling_is_variant_specific(self):
        embedded_data.validate_target(
            target(
                endpoint_variant="next_flight_collection",
                embedded_script_selector="script",
                embedded_json_path="jobs",
                max_jobs=embedded_data.MAX_NEXT_FLIGHT_RESULTS,
            )
        )
        with self.assertRaisesRegex(AdapterError, "hard maximum"):
            embedded_data.validate_target(
                target(
                    endpoint_variant="next_flight_collection",
                    embedded_script_selector="script",
                    embedded_json_path="jobs",
                    max_jobs=embedded_data.MAX_NEXT_FLIGHT_RESULTS + 1,
                )
            )
        with self.assertRaisesRegex(AdapterError, "hard maximum"):
            embedded_data.validate_target(target(max_jobs=21))


if __name__ == "__main__":
    unittest.main()
