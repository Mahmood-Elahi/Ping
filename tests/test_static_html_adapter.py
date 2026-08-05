from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget


class Response:
    def __init__(self, body: str, status: int = 200, url: str = "https://jobs.example.test/jobs", content_type: str = "text/html"):
        self.text = body
        self.content = body.encode()
        self.status_code = status
        self.url = url
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


def page(cards: str = "", extra: str = "") -> str:
    return f"<html><head><title>Jobs</title></head><body><main class='listing'>{cards}</main>{extra}</body></html>"


def card(job_id="123", title="Engineer", href="/jobs/123", location="Remote | Calgary", date="2026-08-01", department="Engineering"):
    return (f"<article class='job' data-id='{job_id}'><h2><a class='title' href='{href}'>{title}</a></h2>"
            f"<span class='location'>{location}</span><time>{date}</time><span class='department'>{department}</span></article>")


def target(**overrides):
    values = dict(name="Example", platform="static_html", listing_url="https://jobs.example.test/jobs",
                  endpoint_variant="server_rendered_cards", listing_selector="main.listing",
                  job_card_selector="article.job", title_selector="a.title", detail_link_selector="a.title",
                  external_id_attribute="data-id", location_selector="span.location", date_selector="time",
                  department_selector="span.department", allowed_apply_hosts=["jobs.example.test"],
                  max_pages=1, max_jobs=20)
    values.update(overrides)
    return CompanyTarget(**values)


class StaticHTMLAdapterTests(unittest.TestCase):
    def test_01_listing_normalization(self):
        jobs = static_html.fetch_jobs(Session(Response(page(card()))), target())
        self.assertEqual((jobs[0].title, jobs[0].location), ("Engineer", "Remote | Calgary"))

    def test_02_relative_apply_link_resolution(self):
        self.assertEqual(static_html.fetch_jobs(Session(Response(page(card()))), target())[0].url, "https://jobs.example.test/jobs/123")

    def test_03_stable_id_extraction(self):
        self.assertTrue(static_html.fetch_jobs(Session(Response(page(card("A-7")))), target())[0].job_id.endswith(":A-7"))

    def test_04_deterministic_fallback_id(self):
        t = target(external_id_attribute=None, allow_url_id_fallback=True)
        first = static_html.fetch_jobs(Session(Response(page(card()))), t)[0].job_id
        second = static_html.fetch_jobs(Session(Response(page(card()))), t)[0].job_id
        self.assertEqual(first, second)

    def test_05_multiple_locations(self):
        body = page(card().replace("<span class='location'>Remote | Calgary</span>", "<span class='location'>Calgary</span><span class='location'>Toronto</span>"))
        self.assertEqual(static_html.fetch_jobs(Session(Response(body)), target())[0].offices, ["Calgary", "Toronto"])

    def test_06_date_and_department_preservation(self):
        job = static_html.fetch_jobs(Session(Response(page(card()))), target())[0]
        self.assertEqual((job.updated_at, job.departments), ("2026-08-01", ["Engineering"]))

    def test_07_remote_hybrid_preservation(self):
        job = static_html.fetch_jobs(Session(Response(page(card(location="Hybrid - Toronto")))), target())[0]
        self.assertEqual(job.raw_metadata["work_modes"], ["Hybrid - Toronto"])

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_08_page_number_pagination_and_limit(self):
        session = Session(Response(page(card("1", href="/jobs/1"))), Response(page(card("2", href="/jobs/2"))))
        jobs = static_html.fetch_jobs(session, target(max_pages=2, pagination_url_template="https://jobs.example.test/jobs?page={page}"))
        self.assertEqual([job.job_id.rsplit(":", 1)[-1] for job in jobs], ["1", "2"])

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_09_next_link_pagination(self):
        first = page(card("1", href="/jobs/1"), "<a class='next' href='/jobs?page=2'>Next</a>")
        session = Session(Response(first), Response(page(card("2", href="/jobs/2"))))
        self.assertEqual(len(static_html.fetch_jobs(session, target(max_pages=2, next_page_selector="a.next"))), 2)

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_10_repeated_page_termination(self):
        session = Session(Response(page(card())), Response(page(card())))
        static_html.fetch_jobs(session, target(max_pages=2, pagination_url_template="https://jobs.example.test/jobs?page={page}"))
        self.assertEqual(session.static_html_pagination["stop_reason"], "repeated_page")

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_11_no_new_id_termination(self):
        session = Session(Response(page(card("1", href="/jobs/a"))), Response(page(card("1", href="/jobs/b"))))
        static_html.fetch_jobs(session, target(max_pages=2, pagination_url_template="https://jobs.example.test/jobs?page={page}"))
        self.assertEqual(session.static_html_pagination["stop_reason"], "no_new_stable_ids")

    def test_12_valid_zero_job_page(self):
        t = target(empty_selector="p.empty")
        session = Session(Response(page(extra="<p class='empty'>No jobs</p>")))
        self.assertEqual(static_html.fetch_jobs(session, t), [])
        self.assertTrue(session.static_html_pagination["zero_job_contract"])

    def test_13_malformed_http_200_rejection(self):
        with self.assertRaisesRegex(AdapterError, "listing contract"):
            static_html.fetch_jobs(Session(Response("<html><title>Home</title></html>")), target())

    def test_14_login_rejection(self):
        with self.assertRaisesRegex(AdapterError, "authentication"):
            static_html.fetch_jobs(Session(Response("<html><title>Sign In</title></html>")), target())

    def test_15_browser_and_session_rejection(self):
        for body, expected in (("<html><body>Enable JavaScript to continue</body></html>", "browser"), ("<html><title>Session Required</title></html>", "session")):
            with self.subTest(expected=expected), self.assertRaisesRegex(AdapterError, expected):
                static_html.fetch_jobs(Session(Response(body)), target())

    def test_16_blocked_response_rejection(self):
        with self.assertRaisesRegex(AdapterError, "blocked"):
            static_html.fetch_jobs(Session(Response("<html><body>Cloudflare challenge</body></html>")), target())

    def test_17_apply_host_rejection(self):
        with self.assertRaisesRegex(AdapterError, "allowed-host"):
            static_html.fetch_jobs(Session(Response(page(card(href="https://evil.test/jobs/1")))), target())

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_18_retry_bounds(self):
        session = Session(Response("", 503), Response(page(card())))
        self.assertEqual(len(static_html.fetch_jobs(session, target())), 1)
        self.assertEqual(len(session.calls), 2)

    def test_19_page_and_result_limits(self):
        with self.assertRaisesRegex(AdapterError, "hard maximum"):
            static_html.validate_target(target(max_pages=3))
        many = "".join(card(str(i), href=f"/jobs/{i}") for i in range(30))
        self.assertEqual(len(static_html.fetch_jobs(Session(Response(page(many))), target(max_jobs=4))), 4)

    def test_20_unsupported_selector_and_regex_contracts(self):
        with self.assertRaisesRegex(AdapterError, "unsupported construct"):
            static_html.validate_target(target(job_card_selector="article.job, div.job"))
        with self.assertRaisesRegex(AdapterError, "unsafe construct"):
            static_html.validate_target(target(external_id_attribute=None, external_id_regex="(?=x)"))

    def test_21_non_html_and_empty_success_rejected(self):
        for response in (Response("{}", content_type="application/json"), Response("")):
            with self.assertRaises(AdapterError):
                static_html.fetch_jobs(Session(response), target())

    def test_22_consent_page_rejected(self):
        with self.assertRaisesRegex(AdapterError, "consent"):
            static_html.fetch_jobs(Session(Response("<html><body>Cookie consent required</body></html>")), target())


if __name__ == "__main__":
    unittest.main()
