import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode

import requests

from scripts import validate_company_registry as validator
from src.scraper.company_careers.adapters import sap_successfactors as sf
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


class FakeResponse:
    def __init__(self, text, status_code=200, url="https://jobs.example.com/search/", payload=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self._payload = payload
        self.content = text.encode("utf-8")
        self.headers = {
            "content-type": "application/json" if payload is not None else "text/html;charset=UTF-8"
        }
        self.history = []

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON payload")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        params = kwargs.get("params")
        if params:
            response.url = f"{url}?{urlencode(params)}"
        else:
            response.url = url
        return response


class FailingSessionFactory:
    def __call__(self):
        raise AssertionError("Network session initialized")


class ColumnizedSession:
    def __init__(self, payload, detail_html, *, identity="Bombardier"):
        self.payload = payload
        self.detail_html = detail_html
        self.identity = identity
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse("{}", payload=self.payload, url=url)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if url == "https://jobs.example.com/search/":
            return FakeResponse(
                f'<html>{self.identity} BS3ColumnizedSearch j2w.searchManager '
                'var CSRFToken = "12345678-1234-1234-1234-123456789abc";</html>',
                url=url,
            )
        return FakeResponse(self.detail_html, url=url)


def target(variant=sf.TABLE_VARIANT, **overrides):
    values = {
        "name": "Example",
        "platform": "sap_successfactors",
        "careers_url": "https://www.example.com/careers/",
        "search_url": "https://jobs.example.com/search/",
        "endpoint_variant": variant,
        "max_pages": 1,
    }
    values.update(overrides)
    return CompanyTarget(**values)


def registry_entry(**overrides):
    item = {
        "name": "Example",
        "platform": "sap_successfactors",
        "careers_url": "https://www.example.com/careers/",
        "search_url": "https://jobs.example.com/search/",
        "endpoint_variant": sf.TABLE_VARIANT,
        "max_pages": 1,
    }
    item.update(overrides)
    return item


def table_job(index=1, **overrides):
    item = {
        "id": str(1000 + index),
        "requisition_id": f"REQ-{index}",
        "title": f"Controls Engineer {index}",
        "location": "Calgary, AB, CA",
        "additional": "Edmonton, AB, CA | Calgary, AB, CA",
        "date": "Aug 4, 2026",
        "department": "Engineering",
        "description": "&lt;p&gt;Build &amp;amp; test controls.&lt;/p&gt;",
    }
    item.update(overrides)
    return item


def table_html(jobs, *, total=None, offset=0, next_offset=None):
    jobs = list(jobs)
    total = len(jobs) if total is None else total
    next_link = (
        f'<a href="?q=&amp;sortColumn=referencedate&amp;sortDirection=desc&amp;startrow={next_offset}">Next</a>'
        if next_offset is not None
        else ""
    )
    rows = []
    for item in jobs:
        rows.append(
            f'''<tr class="data-row">
              <td headers="hdrTitle" class="colTitle"><a class="jobTitle-link" href="/job/Test/{item['id']}/">{item['title']}</a></td>
              <td headers="hdrRequisition">{item['requisition_id']}</td>
              <td headers="hdrLocation" class="colLocation">{item['location']}</td>
              <td headers="hdrAdditional">{item['additional']}</td>
              <td headers="hdrDate" class="colDate">{item['date']}</td>
              <td headers="hdrDepartment" class="colDepartment">{item['department']}</td>
              <td headers="hdrDescription">{item['description']}</td>
            </tr>'''
        )
    start = 0 if total == 0 else offset + 1
    end = offset + len(jobs)
    return f'''<!doctype html><html><body>
      <span class="paginationLabel">Results {start} – {end} of {total}</span>{next_link}
      <table id="searchresults"><thead><tr>
        <th id="hdrTitle">Title</th><th id="hdrRequisition">Requisition ID</th>
        <th id="hdrLocation">Location</th><th id="hdrAdditional">Additional Locations</th>
        <th id="hdrDate">Posting Date</th><th id="hdrDepartment">Department</th>
        <th id="hdrDescription">Description</th>
      </tr></thead><tbody>{''.join(rows)}</tbody></table>
    </body></html>'''


def tile_job(index=1, **overrides):
    item = {
        "id": str(2000 + index),
        "requisition_id": f"TILE-{index}",
        "title": f"Robotics Engineer {index}",
        "location": "Augsburg, DE",
        "date": "July 31, 2026",
        "category": "Research & Development",
    }
    item.update(overrides)
    return item


def tile_markup(item, row_index=1):
    return f'''<li class="job-tile job-id-{item['id']}" data-row-index="{row_index}">
      <a class="jobTitle-link" href="/job/Test/{item['id']}/">{item['title']}</a>
      <div class="section-field customfield1"><span class="section-label">Requisition ID</span><div id="job-{item['id']}-desktop-section-customfield1-value">{item['requisition_id']}</div></div>
      <div class="section-field location"><span class="section-label">Location</span><div id="job-{item['id']}-desktop-section-location-value">{item['location']}</div></div>
      <div class="section-field date"><span class="section-label">Date</span><div id="job-{item['id']}-desktop-section-date-value">{item['date']}</div></div>
      <div class="section-field category"><span class="section-label">Category</span><div id="job-{item['id']}-desktop-section-category-value">{item['category']}</div></div>
    </li>'''


def tile_html(jobs, *, total=None, page_size=25):
    jobs = list(jobs)
    total = len(jobs) if total is None else total
    tiles = "".join(tile_markup(item, index) for index, item in enumerate(jobs, start=1))
    return f'''<!doctype html><html><body>
      <span id="tile-search-results-label">Showing 1 to {len(jobs)} of {total} Jobs</span>
      <ul id="job-tile-list" data-per-page="{page_size}" data-record-returned="{len(jobs)}">{tiles}</ul>
      <script>j2w.SearchResults.init({{apiEndpoint: "tile-search-results", searchQuery: "?q=&sortColumn=referencedate&sortDirection=desc", jobRecordsPerPage: parseInt("{page_size}"), jobRecordsFound: parseInt("{total}")}});</script>
    </body></html>'''


class SapSuccessFactorsTests(unittest.TestCase):
    def test_kinross_current_columnized_contract_is_explicit_and_strict(self):
        external_id = "47942"
        detail_url = (
            "https://jobs.example.com/job/Mobile-Maintenance-Mechanic/"
            f"{external_id}-en_US"
        )
        payload = {
            "totalJobs": 1,
            "jobSearchResult": [{"response": {
                "id": external_id,
                "unifiedStandardTitle": "Mobile Maintenance Mechanic",
                "unifiedUrlTitle": "Mobile-Maintenance-Mechanic",
                "supportedLocales": ["en_US"],
                "jobLocationShort": ["Round Mountain, NV, USA"],
                "unifiedStandardStart": "3/18/25",
            }}],
        }
        detail_html = f'''<!doctype html><html><head>
          <link rel="canonical" href="{detail_url}">
          <meta property="og:title" content="Mobile Maintenance Mechanic">
        </head><body>Kinross<div>Req ID: {external_id}</div>
          <script>j2w.Apply.init({{jobID: {external_id}}});</script></body></html>'''
        job = sf.fetch_jobs(
            ColumnizedSession(payload, detail_html, identity="Kinross"),
            target(
                sf.COLUMNIZED_VARIANT,
                endpoint_url="https://jobs.example.com/services/recruiting/v1/jobs",
                locale="en_US",
                identity_regex=r"\bKinross\b",
                allowed_apply_hosts=["jobs.example.com"],
                max_jobs=10,
            ),
        )[0]
        self.assertEqual(job.raw_metadata["external_job_id"], external_id)
        self.assertEqual(job.url, detail_url)

    def test_table_normalization_ids_apply_url_locations_date_department_description(self):
        session = FakeSession([FakeResponse(table_html([table_job()]))])
        jobs = sf.fetch_jobs(session, target())
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "sap_successfactors:jobs.example.com:REQ-1")
        self.assertEqual(job.raw_metadata["url_job_id"], "1001")
        self.assertEqual(job.url, "https://jobs.example.com/job/Test/1001/")
        self.assertEqual(job.location, "Calgary, AB, CA | Edmonton, AB, CA")
        self.assertEqual(job.offices, ["Calgary, AB, CA", "Edmonton, AB, CA"])
        self.assertEqual(job.updated_at, "2026-08-04")
        self.assertEqual(job.departments, ["Engineering"])
        self.assertEqual(job.description, "Build & test controls.")

    def test_location_values_are_deduplicated_case_insensitively(self):
        item = table_job(additional="calgary, ab, ca")
        job = sf.fetch_jobs(FakeSession([FakeResponse(table_html([item]))]), target())[0]
        self.assertEqual(job.offices, ["Calgary, AB, CA"])

    def test_table_accepts_city_as_the_location_column(self):
        html = table_html([table_job(location="Toronto, ON, CA")]).replace(
            '<th id="hdrLocation">Location</th>', '<th id="hdrLocation">City</th>'
        )
        job = sf.fetch_jobs(FakeSession([FakeResponse(html)]), target())[0]
        self.assertEqual(job.location, "Toronto, ON, CA | Edmonton, AB, CA | Calgary, AB, CA")

    def test_columnized_url_and_requisition_id_are_canonicalized(self):
        external_id = "12345"
        encoded_title = "Engineering-Professional%2C-Avionics"
        detail_url = f"https://jobs.example.com/job/Engineering-Professional%2C-Avionics/{external_id}-en_US"
        payload = {
            "totalJobs": 1,
            "jobSearchResult": [{
                "response": {
                    "id": external_id,
                    "unifiedStandardTitle": "Engineering Professional, Avionics",
                    "unifiedUrlTitle": encoded_title,
                    "supportedLocales": ["en_US"],
                    "jobLocationShort": [{"value": "Dorval, QC, CAN"}],
                    "unifiedStandardStart": "2026-08-11",
                }
            }],
        }
        detail_html = f'''<!doctype html><html><head>
          <link rel="canonical" href="{detail_url}">
          <meta property="og:title" content="Engineering Professional, Avionics">
        </head><body>Bombardier
          <div>Requisition ID: {external_id}</div>
          <script>j2w.Apply.init({{jobID: {external_id}}});</script>
        </body></html>'''
        session = ColumnizedSession(payload, detail_html)
        columnized_target = target(
            sf.COLUMNIZED_VARIANT,
            endpoint_url="https://jobs.example.com/services/recruiting/v1/jobs",
            endpoint_variant=sf.COLUMNIZED_VARIANT,
            locale="en_US",
            identity_regex=r"Bombardier",
            allowed_apply_hosts=["jobs.example.com"],
            max_jobs=10,
        )
        job = sf.fetch_jobs(session, columnized_target)[0]
        self.assertEqual(job.job_id, "sap_successfactors:jobs.example.com:12345")
        self.assertEqual(job.url, detail_url)
        self.assertNotIn("%252C", job.url)
        self.assertEqual(job.location, "Dorval, QC, CAN")

    def test_columnized_nested_req_id_wrapper_preserves_exact_identity(self):
        external_id = "106197"
        detail_url = f"https://jobs.example.com/job/Manager-Public-Works/{external_id}-en_US"
        payload = {
            "totalJobs": 1,
            "jobSearchResult": [{"response": {
                "id": external_id,
                "unifiedStandardTitle": "Manager Public Works",
                "unifiedUrlTitle": "Manager-Public-Works",
                "supportedLocales": ["en_US"],
                "jobLocationShort": ["St. Paul, MN, USA"],
                "unifiedStandardStart": "2026-08-28",
            }}],
        }
        detail_html = f'''<!doctype html><html><head>
          <link rel="canonical" href="{detail_url}">
          <meta property="og:title" content="Manager Public Works">
        </head><body>Bombardier<ul><li>Req ID: {external_id}
          <ul><li>Req ID: {external_id}</li><li>Department: Engineering</li></ul>
        </li></ul><script>j2w.Apply.init({{jobID: {external_id}}});</script></body></html>'''
        job = sf.fetch_jobs(
            ColumnizedSession(payload, detail_html),
            target(
                sf.COLUMNIZED_VARIANT,
                endpoint_url="https://jobs.example.com/services/recruiting/v1/jobs",
                locale="en_US",
                identity_regex=r"Bombardier",
                allowed_apply_hosts=["jobs.example.com"],
                max_jobs=10,
            ),
        )[0]
        self.assertEqual(job.raw_metadata["external_job_id"], external_id)
        self.assertEqual(job.url, detail_url)

    def test_columnized_real_requisition_mismatch_is_still_rejected(self):
        external_id = "12345"
        detail_url = f"https://jobs.example.com/job/Engineer/{external_id}-en_US"
        payload = {
            "totalJobs": 1,
            "jobSearchResult": [{"response": {
                "id": external_id,
                "unifiedStandardTitle": "Engineer",
                "unifiedUrlTitle": "Engineer",
                "supportedLocales": ["en_US"],
                "jobLocationShort": ["Calgary, AB, CAN"],
                "unifiedStandardStart": "2026-08-28",
            }}],
        }
        detail_html = f'''<!doctype html><html><head>
          <link rel="canonical" href="{detail_url}">
          <meta property="og:title" content="Engineer">
        </head><body>Bombardier<div>Requisition ID: 99999</div>
          <script>j2w.Apply.init({{jobID: {external_id}}});</script></body></html>'''
        with self.assertRaisesRegex(AdapterError, "requisition ID did not match"):
            sf.fetch_jobs(
                ColumnizedSession(payload, detail_html),
                target(
                    sf.COLUMNIZED_VARIANT,
                    endpoint_url="https://jobs.example.com/services/recruiting/v1/jobs",
                    locale="en_US",
                    identity_regex=r"Bombardier",
                    allowed_apply_hosts=["jobs.example.com"],
                    max_jobs=10,
                ),
            )

    def test_columnized_missing_visible_requisition_field_uses_canonical_and_apply_ids(self):
        external_id = "12807"
        detail_url = f"https://jobs.example.com/job/Engineering-Specialist/{external_id}-en_US"
        payload = {
            "totalJobs": 1,
            "jobSearchResult": [{"response": {
                "id": external_id,
                "unifiedStandardTitle": "Engineering Specialist",
                "unifiedUrlTitle": "Engineering-Specialist",
                "supportedLocales": ["en_US"],
                "jobLocationShort": ["Longmont, CO, USA"],
                "unifiedStandardStart": "2026-08-28",
            }}],
        }
        detail_html = f'''<!doctype html><html><head>
          <link rel="canonical" href="{detail_url}">
          <meta property="og:title" content="Engineering Specialist">
        </head><body>Seagate
          <script>j2w.Apply.init({{jobID: {external_id}}});</script></body></html>'''
        job = sf.fetch_jobs(
            ColumnizedSession(payload, detail_html),
            target(
                sf.COLUMNIZED_VARIANT,
                endpoint_url="https://jobs.example.com/services/recruiting/v1/jobs",
                locale="en_US",
                identity_regex=r"Bombardier|Seagate",
                allowed_apply_hosts=["jobs.example.com"],
                max_jobs=10,
            ),
        )[0]
        self.assertEqual(job.raw_metadata["external_job_id"], external_id)

    def test_columnized_identical_duplicate_is_skipped_but_conflict_is_rejected(self):
        row = {
            "id": "15004",
            "unifiedStandardTitle": "Engineer",
            "unifiedUrlTitle": "Engineer",
            "supportedLocales": ["en_US"],
            "jobLocationShort": ["Montreal, QC, CAN"],
            "unifiedStandardStart": "2026-08-28",
        }
        detail_url = "https://jobs.example.com/job/Engineer/15004-en_US"
        detail_html = f'''<!doctype html><html><head>
          <link rel="canonical" href="{detail_url}">
          <meta property="og:title" content="Engineer">
        </head><body>Bombardier<div>Requisition ID: 15004</div>
          <script>j2w.Apply.init({{jobID: 15004}});</script></body></html>'''
        duplicate_payload = {
            "totalJobs": 2,
            "jobSearchResult": [{"response": dict(row)}, {"response": dict(row)}],
        }
        columnized_target = target(
            sf.COLUMNIZED_VARIANT,
            endpoint_url="https://jobs.example.com/services/recruiting/v1/jobs",
            locale="en_US",
            identity_regex=r"Bombardier",
            allowed_apply_hosts=["jobs.example.com"],
            max_jobs=10,
        )
        jobs = sf.fetch_jobs(ColumnizedSession(duplicate_payload, detail_html), columnized_target)
        self.assertEqual([job.raw_metadata["external_job_id"] for job in jobs], ["15004"])

        conflicting = dict(row)
        conflicting["unifiedStandardTitle"] = "Different Engineer"
        conflict_payload = {
            "totalJobs": 2,
            "jobSearchResult": [{"response": dict(row)}, {"response": conflicting}],
        }
        with self.assertRaisesRegex(AdapterError, "conflicting repeated stable job ID"):
            sf.fetch_jobs(ColumnizedSession(conflict_payload, detail_html), columnized_target)

    def test_html_description_uses_readable_text(self):
        self.assertEqual(sf._html_to_text("<p>Build <b>safe</b> robots.</p><ul><li>Test</li></ul>"), "Build safe robots.\nTest")

    def test_missing_optional_fields_do_not_discard_job(self):
        item = table_job(location="", additional="", date="", department="", description="")
        job = sf.fetch_jobs(FakeSession([FakeResponse(table_html([item]))]), target())[0]
        self.assertEqual(job.location, "")
        self.assertEqual(job.updated_at, "")
        self.assertEqual(job.departments, [])
        self.assertIsNone(job.description)

    @patch.object(sf.time, "sleep", return_value=None)
    def test_table_offset_pagination_is_deterministic(self, _sleep):
        first = [table_job(index) for index in range(1, 26)]
        second = [table_job(index) for index in range(26, 31)]
        session = FakeSession([
            FakeResponse(table_html(first, total=30, next_offset=25)),
            FakeResponse(table_html(second, total=30, offset=25)),
        ])
        jobs = sf.fetch_jobs(session, target(max_pages=2))
        self.assertEqual(len(jobs), 30)
        self.assertIn("startrow=25", session.calls[1][0])
        self.assertEqual(session.sap_successfactors_pagination["offsets"], [0, 25])
        self.assertEqual(session.sap_successfactors_pagination["stop_reason"], "total_jobs_reached")

    @patch.object(sf.time, "sleep", return_value=None)
    def test_observed_twenty_result_table_page_size(self, _sleep):
        first = [table_job(index) for index in range(1, 21)]
        second = [table_job(index) for index in range(21, 41)]
        session = FakeSession([
            FakeResponse(table_html(first, total=101, next_offset=20)),
            FakeResponse(table_html(second, total=101, offset=20, next_offset=40)),
        ])
        jobs = sf.fetch_jobs(session, target(max_pages=2))
        self.assertEqual(len(jobs), 40)
        self.assertIn("startrow=20", session.calls[1][0])
        self.assertEqual(session.sap_successfactors_pagination["page_size"], 20)

    @patch.object(sf.time, "sleep", return_value=None)
    def test_tile_fragment_pagination_is_deterministic_and_session_independent(self, _sleep):
        first = [tile_job(index) for index in range(1, 26)]
        second = [tile_job(index) for index in range(26, 31)]
        session = FakeSession([
            FakeResponse(tile_html(first, total=30)),
            FakeResponse("<!doctype html>" + "".join(tile_markup(item, index) for index, item in enumerate(second, start=26))),
        ])
        jobs = sf.fetch_jobs(session, target(sf.TILE_VARIANT, max_pages=2))
        self.assertEqual(len(jobs), 30)
        self.assertEqual(session.calls[1][0], "https://jobs.example.com/tile-search-results/")
        self.assertEqual(session.calls[1][1]["params"]["startrow"], "25")
        self.assertNotIn("X-CSRF-Token", session.calls[1][1]["headers"])

    def test_tile_accepts_bounded_source_declared_page_sizes(self):
        jobs = [tile_job(index) for index in range(1, 51)]
        session = FakeSession([FakeResponse(tile_html(jobs, total=75, page_size=50))])
        normalized = sf.fetch_jobs(session, target(sf.TILE_VARIANT))
        self.assertEqual(len(normalized), 50)
        self.assertEqual(session.sap_successfactors_pagination["page_size"], 50)

    def test_tile_rejects_source_page_size_above_hard_limit(self):
        with self.assertRaisesRegex(AdapterError, "unsupported page size"):
            sf.fetch_jobs(
                FakeSession([FakeResponse(tile_html([], total=0, page_size=101))]),
                target(sf.TILE_VARIANT),
            )

    @patch.object(sf.time, "sleep", return_value=None)
    def test_maximum_page_and_result_limits_are_enforced(self, _sleep):
        pages = []
        for page in range(5):
            jobs = [table_job(page * 25 + index) for index in range(1, 26)]
            pages.append(FakeResponse(table_html(jobs, total=200, offset=page * 25, next_offset=(page + 1) * 25)))
        session = FakeSession(pages)
        jobs = sf.fetch_jobs(session, target(max_pages=5))
        self.assertEqual(len(jobs), sf.MAX_RESULTS)
        self.assertEqual(len(session.calls), sf.MAX_PAGES)
        self.assertEqual(session.sap_successfactors_pagination["stop_reason"], "maximum_pages_reached")

    def test_max_pages_above_hard_limit_is_rejected_without_network(self):
        session = FakeSession([])
        with self.assertRaisesRegex(AdapterError, "max_pages cannot exceed 5"):
            sf.fetch_jobs(session, target(max_pages=6))
        self.assertEqual(session.calls, [])

    def test_valid_zero_job_table_and_tile_responses(self):
        table_jobs = sf.fetch_jobs(FakeSession([FakeResponse(table_html([], total=0))]), target())
        tile_jobs = sf.fetch_jobs(FakeSession([FakeResponse(tile_html([], total=0))]), target(sf.TILE_VARIANT))
        self.assertEqual(table_jobs, [])
        self.assertEqual(tile_jobs, [])

    def test_explicit_zero_job_message_without_table_is_valid(self):
        html = '<html><body>There are no matches. The 0 most recent jobs posted are listed below.</body></html>'
        self.assertEqual(sf.fetch_jobs(FakeSession([FakeResponse(html)]), target()), [])

    def test_malformed_http_200_is_rejected(self):
        with self.assertRaisesRegex(AdapterError, "missing searchresults"):
            sf.fetch_jobs(FakeSession([FakeResponse("<html><body>Welcome</body></html>")]), target())

    def test_malformed_entry_is_skipped_with_diagnostic(self):
        good = table_job(1)
        bad = table_job(2, id="not-stable")
        session = FakeSession([FakeResponse(table_html([good, bad], total=2))])
        jobs = sf.fetch_jobs(session, target())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(session.sap_successfactors_pagination["diagnostics"]), 1)

    def test_invalid_search_url_and_variant_are_rejected_without_network(self):
        session = FakeSession([])
        with self.assertRaisesRegex(AdapterError, "observed HTTPS /search/"):
            sf.fetch_jobs(session, target(search_url="https://jobs.example.com/api/jobs"))
        with self.assertRaisesRegex(AdapterError, "endpoint_variant"):
            sf.fetch_jobs(session, target(endpoint_variant="browser_json"))
        self.assertEqual(session.calls, [])

    @patch.object(sf.time, "sleep", return_value=None)
    def test_retryable_failure_is_bounded_and_recovers(self, sleep):
        session = FakeSession([FakeResponse("", 503), FakeResponse(table_html([], total=0))])
        self.assertEqual(sf.fetch_jobs(session, target()), [])
        self.assertEqual(len(session.calls), 2)
        sleep.assert_called_once_with(sf.RETRY_DELAY_SECONDS)

    def test_non_retryable_failure_is_not_retried(self):
        session = FakeSession([FakeResponse("", 404)])
        with self.assertRaisesRegex(AdapterError, "HTTP 404"):
            sf.fetch_jobs(session, target())
        self.assertEqual(len(session.calls), 1)

    def test_validator_rejects_speculative_company_and_career_site_ids(self):
        for field in ("company_id", "career_site_id"):
            result = validator.validate_company(
                registry_entry(**{field: "guessed"}),
                session_factory=FailingSessionFactory(),
            )
            self.assertEqual(result["validation_status"], validator.INVALID_CONFIGURATION)
            self.assertIn(field, result["exact_failure_reason"])

    def test_browser_and_session_dependent_sources_are_excluded(self):
        for flag, expected_status in (
            ("browser_required", validator.BROWSER_REQUIRED),
            ("session_required", validator.SESSION_REQUIRED),
        ):
            result = validator.validate_company(
                registry_entry(**{flag: True}),
                session_factory=FailingSessionFactory(),
            )
            self.assertEqual(result["validation_status"], expected_status)
            self.assertFalse(result["safe_to_activate"])

    def test_duplicate_source_is_rejected_without_network(self):
        result = validator.validate_company(
            registry_entry(),
            duplicate_of="Existing RMK source",
            session_factory=FailingSessionFactory(),
        )
        self.assertEqual(result["validation_status"], validator.DUPLICATE_SOURCE)

    def test_validator_reports_success_samples_and_pagination(self):
        session = FakeSession([FakeResponse(table_html([table_job()]))])
        result = validator.validate_company(registry_entry(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertTrue(result["safe_to_activate"])
        self.assertEqual(result["pages_requested"], 1)
        self.assertEqual(result["total_available_jobs"], 1)
        self.assertEqual(result["sample_location"], "Calgary, AB, CA | Edmonton, AB, CA")
        self.assertEqual(result["sample_posting_date"], "2026-08-04")

    def test_validator_classifies_malformed_http_200(self):
        session = FakeSession([FakeResponse("<html><body>Unexpected</body></html>")])
        result = validator.validate_company(registry_entry(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.MALFORMED_RESPONSE)
        self.assertFalse(result["safe_to_activate"])

    def test_registry_loader_compatibility_candidate_membership_and_byte_prefix(self):
        candidate_path = Path("config/target_companies.successfactors_candidates.yaml")
        expected = [
            "Lam Research", "KUKA", "Festo", "Hyundai", "Volkswagen Group",
            "Volvo Cars", "Ferrari", "ZF Friedrichshafen", "Halliburton",
            "John Deere", "Pembina Pipeline",
        ]
        _, candidates = validator.load_registry(candidate_path)
        successfactors_candidates = [
            entry for entry in candidates if entry["platform"] == "sap_successfactors"
        ]
        self.assertEqual([entry["name"] for entry in successfactors_candidates], expected)

        baseline_text = (
            "target_companies:\n"
            "  - name: Existing Greenhouse\n"
            "    platform: greenhouse\n"
            "    slug: existing\n"
            "  - name: Existing Workday\n"
            "    platform: workday\n"
            "    tenant: existing\n"
            "    wd_subdomain: wd1\n"
            "    site_id: Careers\n"
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "baseline.yaml"
            generated_path = root / "generated.yaml"
            baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
            generated_path.write_bytes(
                validator.build_validated_registry_bytes(
                    baseline_path,
                    successfactors_candidates,
                )
            )
            baseline_bytes = baseline_path.read_bytes()
            generated_bytes = generated_path.read_bytes()
            _, baseline = validator.load_registry(baseline_path)
            _, generated = validator.load_registry(generated_path)
            additions = validator.detect_additions(baseline, generated)
            targets = load_target_companies(str(generated_path))

        addition_names = [entry["name"] for entry in additions]
        normalized_names = [
            " ".join(str(entry["name"]).casefold().split()) for entry in generated
        ]
        self.assertEqual(generated_bytes[: len(baseline_bytes)], baseline_bytes)
        self.assertEqual(generated[: len(baseline)], baseline)
        self.assertEqual(len(additions), len(expected))
        self.assertEqual(addition_names, expected)
        self.assertTrue(all(entry["platform"] == "sap_successfactors" for entry in additions))
        self.assertEqual(len(normalized_names), len(set(normalized_names)))
        self.assertEqual([item.name for item in targets[-len(expected) :]], expected)
        self.assertTrue(
            all(item.platform == "sap_successfactors" for item in targets[-len(expected) :])
        )

    def test_validated_registry_builder_adds_only_safe_companies(self):
        with TemporaryDirectory() as temp_dir:
            current = Path(temp_dir) / "current.yaml"
            current.write_text("target_companies:\n  - name: Existing\n    platform: greenhouse\n    slug: existing\n", encoding="utf-8")
            safe = [registry_entry(name="Safe")]
            content = validator.build_validated_registry_bytes(current, safe)
            validated = Path(temp_dir) / "validated.yaml"
            validated.write_bytes(content)
            _, entries = validator.load_registry(validated)
        self.assertEqual([entry["name"] for entry in entries], ["Existing", "Safe"])

    @patch.object(sf.time, "sleep", return_value=None)
    def test_validation_initializes_no_persistence(self, _sleep):
        from src.integrations.google_sheets_publisher import GoogleSheetsPublisher
        from src.integrations.supabase_job_store import SupabaseJobStore

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current.yaml"
            candidate = root / "candidate.yaml"
            current.write_text("target_companies: []\n", encoding="utf-8")
            candidate.write_text(
                "target_companies:\n"
                "  - name: Example\n"
                "    platform: sap_successfactors\n"
                "    careers_url: https://www.example.com/careers/\n"
                "    search_url: https://jobs.example.com/search/\n"
                "    endpoint_variant: rmk_table\n"
                "    max_pages: 1\n",
                encoding="utf-8",
            )
            with (
                patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite initialized")) as sqlite_init,
                patch.object(SupabaseJobStore, "from_env", side_effect=AssertionError("Supabase initialized")) as supabase_init,
                patch.object(GoogleSheetsPublisher, "from_env", side_effect=AssertionError("Sheets initialized")) as sheets_init,
            ):
                validator.validate_registry(
                    current,
                    candidate,
                    only_additions=True,
                    output=root / "report.json",
                    validated_config=root / "validated.yaml",
                    session_factory=lambda: FakeSession([FakeResponse(table_html([], total=0))]),
                    sleep=lambda _: None,
                )
            sqlite_init.assert_not_called()
            supabase_init.assert_not_called()
            sheets_init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
