from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import requests

from src.scraper.company_careers.adapters import lever
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)


class Response:
    def __init__(self, payload, status=200, content_type="application/json"):
        self.payload = payload
        self.status_code = status
        self.headers = {"content-type": content_type}
        if isinstance(payload, bytes):
            self.content = payload
        elif isinstance(payload, Exception):
            self.content = b"{}"
        else:
            self.content = json.dumps(payload).encode()

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def target(**overrides):
    values = dict(
        name="Example", platform="lever", slug="example",
        endpoint_variant="postings_api_v0", allowed_apply_hosts=["jobs.lever.co"],
        max_pages=1, max_jobs=20, max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def posting(job_id="abc", **overrides):
    value = {
        "id": job_id, "text": "Engineer", "hostedUrl": f"https://jobs.lever.co/example/{job_id}",
        "createdAt": 1, "categories": {
            "department": "Corporate", "team": "Engineering", "location": "Remote",
        },
        "descriptionPlain": "Build reliable systems.",
    }
    value.update(overrides)
    return value


class LeverAdapterTests(unittest.TestCase):
    def test_01_normalizes_stable_identity_and_official_url(self):
        session = Session(Response([posting()]))
        job = lever.fetch_jobs(session, target())[0]
        self.assertEqual((job.job_id, job.url, job.source),
                         ("abc", "https://jobs.lever.co/example/abc", "lever:example"))
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "natural_terminal_page")

    def test_02_exact_department_allowlist(self):
        payload = [posting("one"), posting("two", categories={"department": "Tinder", "location": "Remote"})]
        jobs = lever.fetch_jobs(Session(Response(payload)), target(department_allowlist=["Corporate"]))
        self.assertEqual([job.job_id for job in jobs], ["one"])

    def test_03_filtered_empty_is_a_valid_zero_job_contract(self):
        session = Session(Response([posting(categories={"department": "Brand"})]))
        self.assertEqual(lever.fetch_jobs(session, target(department_allowlist=["Corporate"])), [])
        self.assertTrue(session.lever_pagination["zero_job_contract"])
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

    def test_04_apply_host_board_and_id_are_validated(self):
        cases = [
            posting(hostedUrl="https://evil.test/example/abc"),
            posting(hostedUrl="https://jobs.lever.co/other/abc"),
            posting(hostedUrl="https://jobs.lever.co/example/other"),
        ]
        for value in cases:
            with self.subTest(url=value["hostedUrl"]), self.assertRaisesRegex(AdapterError, "apply host|apply URL"):
                lever.fetch_jobs(Session(Response([value])), target())

    def test_05_malformed_http_200_rejected(self):
        for payload in ({"jobs": []}, ValueError("bad json")):
            with self.subTest(payload=payload), self.assertRaisesRegex(AdapterError, "malformed"):
                lever.fetch_jobs(Session(Response(payload)), target())

    def test_06_non_json_http_200_rejected(self):
        with self.assertRaisesRegex(AdapterError, "expected JSON"):
            lever.fetch_jobs(Session(Response([], content_type="text/html")), target())

    def test_07_response_size_limit(self):
        with self.assertRaisesRegex(AdapterError, "byte limit"):
            lever.fetch_jobs(Session(Response(b"x" * 30)), target(max_response_bytes=10))

    @patch("src.scraper.company_careers.adapters.lever.time.sleep", lambda _: None)
    def test_08_bounded_http_retry(self):
        session = Session(Response([], 503), Response([]))
        self.assertEqual(lever.fetch_jobs(session, target()), [])
        self.assertEqual(len(session.calls), 2)

    @patch("src.scraper.company_careers.adapters.lever.time.sleep", lambda _: None)
    def test_09_bounded_network_retry(self):
        session = Session(requests.ConnectionError("offline"), requests.ConnectionError("offline"))
        with self.assertRaises(requests.ConnectionError):
            lever.fetch_jobs(session, target())
        self.assertEqual(len(session.calls), 2)

    def test_10_authentication_and_blocked_responses(self):
        for status, message in ((401, "authentication"), (403, "blocked")):
            with self.subTest(status=status), self.assertRaisesRegex(AdapterError, message):
                lever.fetch_jobs(Session(Response([], status)), target())

    def test_11_twenty_job_limit(self):
        payload = [posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}") for index in range(25)]
        session = Session(Response(payload))
        self.assertEqual(len(lever.fetch_jobs(session, target())), 20)
        self.assertEqual(session.lever_pagination["stop_reason"], "maximum_results_reached")
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])

    def test_12_duplicate_ids_are_skipped_deterministically(self):
        session = Session(Response([posting(), posting()]))
        jobs = lever.fetch_jobs(session, target())
        self.assertEqual(len(jobs), 1)
        self.assertIn("Duplicate", session.lever_pagination["diagnostics"][0])

    def test_13_unsupported_variant_and_invalid_slug_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "unsupported variant"):
            lever.validate_target(target(endpoint_variant="future_api"))
        with self.assertRaisesRegex(AdapterError, "valid slug"):
            lever.validate_target(target(slug="bad/slug"))

    def test_14_legacy_targets_get_safe_defaults(self):
        job = lever.fetch_jobs(Session(Response([posting()])), target(endpoint_variant=None, allowed_apply_hosts=[]))[0]
        self.assertEqual(job.source, "lever:example")

    def test_15_server_side_limit_and_department_filter_bound_large_boards(self):
        session = Session(Response([posting()]))
        lever.fetch_jobs(
            session,
            target(max_jobs=7, department_allowlist=["Match Group", "Corporate"]),
        )

        self.assertEqual(
            session.calls[0][0],
            "https://api.lever.co/v0/postings/example?mode=json&limit=7&"
            "department=Match+Group&department=Corporate",
        )
        self.assertEqual(session.lever_pagination["server_result_limit"], 7)

    def test_16_no_company_specific_or_browser_logic(self):
        import inspect
        source = inspect.getsource(lever).casefold()
        for forbidden in ("match group", "zoox", "playwright", "selenium", "eval(", "exec("):
            self.assertNotIn(forbidden, source)

    def test_17_explicit_eu_api_endpoint_is_bounded_and_region_matched(self):
        session = Session(Response([]))
        eu_target = target(
            slug="mobileye",
            endpoint_url="https://api.eu.lever.co/v0/postings/mobileye",
            allowed_apply_hosts=["jobs.eu.lever.co"],
        )
        self.assertEqual(lever.fetch_jobs(session, eu_target), [])
        self.assertEqual(
            session.calls[0][0],
            "https://api.eu.lever.co/v0/postings/mobileye?mode=json&limit=20",
        )
        with self.assertRaisesRegex(AdapterError, "region"):
            lever.validate_target(
                target(
                    endpoint_url="https://api.eu.lever.co/v0/postings/example",
                    allowed_apply_hosts=["jobs.lever.co"],
                )
            )

    def test_18_multi_page_scan_completes_on_short_terminal_page(self):
        first = [
            posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}")
            for index in range(20)
        ]
        second = [
            posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}")
            for index in range(20, 40)
        ]
        third = [
            posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}")
            for index in range(40, 45)
        ]
        session = Session(Response(first), Response(second), Response(third))

        jobs = lever.fetch_jobs(session, target(max_jobs=60, max_pages=4))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 45)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["pages_requested"], 3)
        self.assertEqual(metadata["pages_completed"], 3)
        self.assertEqual(metadata["total_available"], 45)
        self.assertIn("skip=20", session.calls[1][0])
        self.assertIn("skip=40", session.calls[2][0])

    def test_19_page_budget_is_incomplete(self):
        page = [
            posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}")
            for index in range(20)
        ]
        session = Session(Response(page))

        jobs = lever.fetch_jobs(session, target(max_jobs=60, max_pages=1))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 20)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_pages_reached")

    def test_20_failure_after_first_page_is_not_complete(self):
        page = [
            posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}")
            for index in range(20)
        ]
        session = Session(Response(page), Response([], status=404))

        with self.assertRaisesRegex(AdapterError, "HTTP 404"):
            lever.fetch_jobs(session, target(max_jobs=60, max_pages=3))

        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["pages_requested"], 2)
        self.assertEqual(metadata["pages_completed"], 1)
        self.assertEqual(metadata["stop_reason"], "pagination_failure")

    def test_21_repeated_page_is_detected(self):
        page = [
            posting(str(index), hostedUrl=f"https://jobs.lever.co/example/{index}")
            for index in range(20)
        ]
        session = Session(Response(page), Response(page))

        with self.assertRaisesRegex(AdapterError, "repeated"):
            lever.fetch_jobs(session, target(max_jobs=60, max_pages=3))

        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "repeated_page")


if __name__ == "__main__":
    unittest.main()
