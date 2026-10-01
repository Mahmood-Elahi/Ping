from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.scraper.company_careers.adapters import icims
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)


class Response:
    def __init__(self, payload=None, status=200, content_type="application/json", text=None):
        self.payload = payload
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.text = json.dumps(payload) if text is None else text
        self.url = "https://jobs.example.com/api/jobs"

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def target(**overrides):
    values = dict(
        name="Example",
        platform="icims",
        tenant="example",
        board_id="example-public",
        endpoint_url="https://jobs.example.com/api/jobs",
        search_url="https://jobs.example.com/jobs",
        endpoint_variant=icims.JIBE_API,
        allowed_apply_hosts=["apply.example.com"],
        max_pages=2,
        max_jobs=20,
        page_size=2,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def item(job_id="101", title="Engineer", apply_host="apply.example.com"):
    return {
        "data": {
            "slug": job_id,
            "req_id": job_id,
            "title": title,
            "apply_url": f"https://{apply_host}/jobs/{job_id}/login",
            "full_location": "Calgary, Alberta; Toronto, Ontario",
            "additional_locations": [{"city": "Toronto", "state": "Ontario", "country": "Canada"}],
            "location_type": "ANY",
            "tags2": ["Hybrid"],
            "categories": [{"name": "Engineering"}],
            "department": "Product",
            "posted_date": "2026-08-01T10:00:00+0000",
            "description": "Build systems",
        }
    }


def page(*items, total=None):
    count = len(items) if total is None else total
    return Response({"jobs": list(items), "totalCount": count, "count": count})


class ICIMSAdapterTests(unittest.TestCase):
    def test_17_valid_listing_normalization(self):
        job = icims.fetch_jobs(Session([page(item(), total=1)]), target(page_size=10))[0]
        self.assertEqual((job.title, job.company), ("Engineer", "Example"))

    def test_18_stable_id_preservation(self):
        job = icims.fetch_jobs(Session([page(item(), total=1)]), target(page_size=10))[0]
        self.assertEqual(job.job_id, "icims:example:101")
        self.assertEqual(job.raw_metadata["external_job_id"], "101")

    def test_19_apply_url_validation(self):
        with self.assertRaisesRegex(AdapterError, "none were valid"):
            icims.fetch_jobs(Session([page(item(apply_host="evil.example"), total=1)]), target(page_size=10))

    def test_20_multiple_location_normalization(self):
        job = icims.fetch_jobs(Session([page(item(), total=1)]), target(page_size=10))[0]
        self.assertEqual(job.offices, ["Calgary, Alberta", "Toronto, Ontario", "Toronto, Ontario, Canada"])
        self.assertIn("Remote", job.location)
        self.assertIn("Hybrid", job.location)

    def test_21_date_and_category_preservation(self):
        job = icims.fetch_jobs(Session([page(item(), total=1)]), target(page_size=10))[0]
        self.assertEqual(job.updated_at, "2026-08-01")
        self.assertEqual(job.departments, ["Engineering", "Product"])

    @patch.object(icims.time, "sleep", return_value=None)
    def test_22_deterministic_pagination(self, _sleep):
        session = Session([page(item("1"), item("2"), total=4), page(item("3"), item("4"), total=4)])
        icims.fetch_jobs(session, target())
        self.assertEqual([call[1]["params"]["page"] for call in session.calls], ["1", "2"])

    @patch.object(icims.time, "sleep", return_value=None)
    def test_23_repeated_page_detection(self, _sleep):
        response = page(item("1"), item("2"), total=4)
        session = Session([response, page(item("1"), item("2"), total=4)])
        with self.assertRaisesRegex(AdapterError, "repeated a complete page"):
            icims.fetch_jobs(session, target())
        self.assertEqual(session.icims_pagination["stop_reason"], "repeated_page")

    @patch.object(icims.time, "sleep", return_value=None)
    def test_24_no_new_id_termination(self, _sleep):
        session = Session([
            page(item("1"), item("2"), total=4),
            page(item("1"), item("1", "Duplicate"), total=4),
        ])
        with self.assertRaisesRegex(AdapterError, "duplicate stable job IDs"):
            icims.fetch_jobs(session, target())
        self.assertEqual(session.icims_pagination["stop_reason"], "duplicate_stable_ids")

    @patch.object(icims.time, "sleep", return_value=None)
    def test_24a_overlap_and_total_decrease_stabilizes_on_one_restart(self, _sleep):
        session = Session([
            page(item("1"), item("2"), total=4),
            page(item("2"), item("3"), total=3),
            page(item("1"), item("2"), total=3),
            page(item("3"), total=3),
        ])
        jobs = icims.fetch_jobs(session, target())
        self.assertEqual([job.raw_metadata["external_job_id"] for job in jobs], ["1", "2", "3"])
        metadata = session.icims_pagination
        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["stabilized_after_collection_retry"])
        self.assertEqual(metadata["collection_restart_count"], 1)
        self.assertTrue(metadata["stable_id_set_match_across_restart"])
        self.assertEqual(metadata["total_pages_requested_across_traversals"], 4)

    @patch.object(icims.time, "sleep", return_value=None)
    def test_24b_repeated_mutation_after_restart_remains_incomplete(self, _sleep):
        session = Session([
            page(item("1"), item("2"), total=4),
            page(item("2"), item("3"), total=3),
            page(item("1"), item("2"), total=4),
            page(item("2"), item("3"), total=3),
        ])
        jobs = icims.fetch_jobs(session, target())
        metadata = session.icims_pagination

        self.assertEqual([job.raw_metadata["external_job_id"] for job in jobs], ["1", "2", "3"])
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["collection_retry_exhausted"])
        self.assertEqual(metadata["scan_completeness_reason"], "collection_changed_during_scan")

    def test_25_valid_zero_job_response(self):
        session = Session([Response({"jobs": [], "totalCount": 0, "count": 0})])
        self.assertEqual(icims.fetch_jobs(session, target()), [])
        self.assertTrue(session.icims_pagination["zero_job_contract"])

    def test_26_malformed_http_200_rejection(self):
        with self.assertRaisesRegex(AdapterError, "jobs must be"):
            icims.fetch_jobs(Session([Response({"jobs": {}, "totalCount": 0, "count": 0})]), target())

    def test_26a_jibe_total_fields_cannot_contradict(self):
        with self.assertRaisesRegex(AdapterError, "contradicted"):
            icims.fetch_jobs(
                Session([Response({"jobs": [item()], "totalCount": 2, "count": 1})]),
                target(),
            )

    def test_26b_html_total_attributes_cannot_contradict(self):
        body = """
        <html><body>
          <div data-results-count="10"></div>
          <div data-results-count="11"></div>
          <ul class="search-results-list"></ul>
        </body></html>
        """
        with self.assertRaisesRegex(AdapterError, "contradicted"):
            icims.fetch_jobs(
                Session([Response(text=body, content_type="text/html")]),
                target(
                    endpoint_variant=icims.RADANCY_HTML,
                    endpoint_url=None,
                    search_url="https://jobs.example.com/en/search-jobs",
                ),
            )

    def test_27_authentication_rejection(self):
        response = Response(None, content_type="text/html", text="<html><title>Login</title></html>")
        with self.assertRaisesRegex(AdapterError, "authentication"):
            icims.fetch_jobs(Session([response]), target())

    def test_28_browser_and_session_rejection(self):
        for body, message in [
            ("<html><body>Enable JavaScript to continue</body></html>", "browser"),
            ("<html><title>Session required</title></html>", "session"),
        ]:
            response = Response(None, content_type="text/html", text=body)
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                icims.fetch_jobs(Session([response]), target())

    def test_29_blocked_response_rejection(self):
        response = Response(None, content_type="text/html", text="<html>Access denied</html>")
        with self.assertRaisesRegex(AdapterError, "blocked"):
            icims.fetch_jobs(Session([response]), target())

    @patch.object(icims.time, "sleep", return_value=None)
    def test_30_retry_limit(self, _sleep):
        session = Session([Response({}, status=503), Response({}, status=503)])
        with self.assertRaisesRegex(AdapterError, "HTTP 503"):
            icims.fetch_jobs(session, target())
        self.assertEqual(len(session.calls), 2)

    def test_31_page_and_job_hard_limits(self):
        with self.assertRaisesRegex(AdapterError, "max_pages"):
            icims.fetch_jobs(Session([]), target(max_pages=3))
        with self.assertRaisesRegex(AdapterError, "max_jobs"):
            icims.fetch_jobs(Session([]), target(max_jobs=21))

    def test_32_unsupported_variant_rejection(self):
        with self.assertRaisesRegex(AdapterError, "endpoint_variant"):
            icims.fetch_jobs(Session([]), target(endpoint_variant="unknown"))

    def test_33_allowlisted_query_parameter_apply_id(self):
        record = item("009JY5")
        record["data"]["apply_url"] = (
            "https://legacy.example.com/careersection/2/jobdetail.ftl?job=009JY5&lang=en_US"
        )
        job = icims.fetch_jobs(
            Session([page(record, total=1)]),
            target(
                page_size=10,
                allowed_apply_hosts=["legacy.example.com"],
                apply_url_id_query_param="job",
            ),
        )[0]
        self.assertEqual(job.job_id, "icims:example:009JY5")
        self.assertIn("job=009JY5", job.url)

    def test_34_query_parameter_apply_id_requires_explicit_configuration(self):
        record = item("009JY5")
        record["data"]["apply_url"] = "https://legacy.example.com/jobdetail.ftl?job=009JY5"
        with self.assertRaisesRegex(AdapterError, "none were valid"):
            icims.fetch_jobs(
                Session([page(record, total=1)]),
                target(page_size=10, allowed_apply_hosts=["legacy.example.com"]),
            )

    def test_35_invalid_query_parameter_contract_fails_before_network(self):
        session = Session([])
        with self.assertRaisesRegex(AdapterError, "apply_url_id_query_param is invalid"):
            icims.fetch_jobs(session, target(apply_url_id_query_param="job[]"))
        self.assertEqual(session.calls, [])

    def test_36_page_number_terminal_without_authoritative_total(self):
        body = """
        <html><body>
          <ul><li class="iCIMS_JobCardItem">
            <div class="header left"><span>CA-AB-Calgary</span></div>
            <div class="title"><a href="https://apply.example.com/jobs/101/engineer/job">
              <h3>Engineer</h3></a></div>
          </li></ul>
          <nav class="pagination">Page 1 of 1</nav>
        </body></html>
        """
        session = Session([Response(text=body, content_type="text/html")])
        jobs = icims.fetch_jobs(
            session,
            target(
                endpoint_variant=icims.LEGACY_HTML,
                search_url="https://jobs.example.com/jobs/search",
                endpoint_url=None,
                page_size=20,
                max_pages=1,
                allowed_apply_hosts=["apply.example.com"],
            ),
        )
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["has_more"])
        self.assertTrue(metadata["terminal_page_observed"])
        self.assertEqual(metadata["total_available"], 1)
        self.assertEqual(metadata["source_identity_status"], "validated")

    def test_36a_response_cannot_leave_configured_source_host(self):
        body = """
        <html><body><ul><li class="iCIMS_JobCardItem">
          <div class="title"><a href="https://apply.example.com/jobs/101/engineer/job">
            <h3>Engineer</h3></a></div>
        </li></ul><nav class="pagination">Page 1 of 1</nav></body></html>
        """
        response = Response(text=body, content_type="text/html")
        response.url = "https://evil.example/jobs/search?pr=0"
        with self.assertRaisesRegex(AdapterError, "configured source host"):
            icims.fetch_jobs(
                Session([response]),
                target(
                    endpoint_variant=icims.LEGACY_HTML,
                    search_url="https://jobs.example.com/jobs/search",
                    endpoint_url=None,
                    page_size=20,
                    max_pages=1,
                    allowed_apply_hosts=["apply.example.com"],
                ),
            )

    def test_37_unknown_terminal_remains_incomplete(self):
        body = """
        <html><body><ul><li class="iCIMS_JobCardItem">
          <div class="header left"><span>CA-AB-Calgary</span></div>
          <div class="title"><a href="https://apply.example.com/jobs/101/engineer/job">
            <h3>Engineer</h3></a></div>
        </li></ul></body></html>
        """
        session = Session([Response(text=body, content_type="text/html")])
        icims.fetch_jobs(
            session,
            target(
                endpoint_variant=icims.LEGACY_HTML,
                search_url="https://jobs.example.com/jobs/search",
                endpoint_url=None,
                page_size=20,
                max_pages=1,
                allowed_apply_hosts=["apply.example.com"],
            ),
        )
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "adapter_completeness_not_proven",
        )

    @patch.object(icims.time, "sleep", return_value=None)
    def test_38_full_legacy_page_then_exact_empty_probe_proves_exhaustion(self, _sleep):
        first = Response(
            text="""
            <html><body><ul class="iCIMS_JobsList"><li class="iCIMS_JobCardItem">
              <div class="title"><a href="https://apply.example.com/jobs/101/engineer/job">
                <h3>Engineer</h3></a></div>
            </li></ul></body></html>
            """,
            content_type="text/html",
        )
        first.url = "https://jobs.example.com/jobs/search?pr=0"
        terminal = Response(
            text='<html><body><ul class="iCIMS_JobsList"></ul></body></html>',
            content_type="text/html",
        )
        terminal.url = "https://jobs.example.com/jobs/search?pr=1"
        session = Session([first, terminal])

        jobs = icims.fetch_jobs(
            session,
            target(
                endpoint_variant=icims.LEGACY_HTML,
                search_url="https://jobs.example.com/jobs/search",
                endpoint_url=None,
                page_size=1,
                max_pages=1,
                max_jobs=1,
                allowed_apply_hosts=["apply.example.com"],
            ),
        )

        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "validated_terminal_empty_page")
        self.assertEqual(metadata["source_identity_status"], "validated")


if __name__ == "__main__":
    unittest.main()
