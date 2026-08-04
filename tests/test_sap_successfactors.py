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
    def __init__(self, text, status_code=200, url="https://jobs.example.com/search/"):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": "text/html;charset=UTF-8"}
        self.history = []


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


def tile_html(jobs, *, total=None):
    jobs = list(jobs)
    total = len(jobs) if total is None else total
    tiles = "".join(tile_markup(item, index) for index, item in enumerate(jobs, start=1))
    return f'''<!doctype html><html><body>
      <span id="tile-search-results-label">Showing 1 to {len(jobs)} of {total} Jobs</span>
      <ul id="job-tile-list" data-per-page="25" data-record-returned="{len(jobs)}">{tiles}</ul>
      <script>j2w.SearchResults.init({{apiEndpoint: "tile-search-results", searchQuery: "?q=&sortColumn=referencedate&sortDirection=desc", jobRecordsPerPage: parseInt("25"), jobRecordsFound: parseInt("{total}")}});</script>
    </body></html>'''


class SapSuccessFactorsTests(unittest.TestCase):
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
        for flag in ("browser_required", "session_required"):
            result = validator.validate_company(
                registry_entry(**{flag: True}),
                session_factory=FailingSessionFactory(),
            )
            self.assertEqual(result["validation_status"], validator.BROWSER_REQUIRED)
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
