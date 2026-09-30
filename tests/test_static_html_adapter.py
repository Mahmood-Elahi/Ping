from __future__ import annotations

import unittest
from unittest.mock import patch
import json

import requests

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


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


class StreamingResponse:
    def __init__(self, chunks, status=200):
        self.chunks = list(chunks)
        self.status_code = status
        self.headers = {"content-type": "text/html"}
        self.url = "https://jobs.example.test/jobs"
        self.yielded = 0
        self.closed = False

    def iter_content(self, **_kwargs):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def close(self):
        self.closed = True


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
    def test_repeated_page_sources_drop_only_the_disproven_page_template(self):
        repaired = {
            item.name: item
            for item in load_target_companies(apply_reliability_repairs=True)
        }
        fixtures = {
            "Caterpillar": {
                "external_id": "R0000380757",
                "card": (
                    "<div class='card-job' data-id='R0000380757'>"
                    "<h2 class='card-title'><a href='/en/jobs/r0000380757/engineer'>"
                    "Engineer</a></h2><ul class='job-meta'><li>Peoria, IL</li></ul>"
                    "</div>"
                ),
            },
            "Intuitive Surgical": {
                "external_id": "744000150742000",
                "card": (
                    "<div class='card-job' data-id='744000150742000'>"
                    "<h2 class='card-title'><a href='/en/jobs/744000150742000/engineer'>"
                    "Engineer</a></h2><ul class='job-meta'><li>Sunnyvale, CA</li>"
                    "<li>Engineering</li></ul></div>"
                ),
            },
        }

        self.assertIsNone(repaired["General Motors"].next_page_selector)

        for company, fixture in fixtures.items():
            with self.subTest(company=company):
                configured = repaired[company]
                self.assertIsNone(configured.pagination_url_template)
                self.assertIsNone(configured.next_page_selector)
                session = Session(Response(f"<html><body>{fixture['card']}</body></html>"))

                jobs = static_html.fetch_jobs(session, configured)
                metadata = source_scan_metadata(session)

                self.assertEqual(len(jobs), 1)
                self.assertEqual(
                    jobs[0].job_id,
                    f"static_html:{configured.allowed_apply_hosts[0]}:"
                    f"{fixture['external_id']}",
                )
                self.assertEqual(len(session.calls), 1)
                self.assertFalse(metadata["scan_complete"])
                self.assertEqual(
                    metadata["scan_completeness_reason"],
                    "no_pagination_configured",
                )

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
        session = Session(
            Response(page(card("1", href="/jobs/1"))),
            Response(page(card("2", href="/jobs/2"))),
            Response(page(extra="<p class='empty'>No jobs</p>")),
        )
        jobs = static_html.fetch_jobs(
            session,
            target(
                max_pages=2,
                pagination_url_template="https://jobs.example.test/jobs?page={page}",
                empty_selector="p.empty",
            ),
        )
        self.assertEqual([job.job_id.rsplit(":", 1)[-1] for job in jobs], ["1", "2"])
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(session.static_html_pagination["scan_complete"])

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
    def test_empty_followup_without_terminal_contract_remains_incomplete(self):
        session = Session(
            Response(page(card("1", href="/jobs/1"))),
            Response(page()),
        )
        static_html.fetch_jobs(
            session,
            target(
                max_pages=1,
                pagination_url_template="https://jobs.example.test/jobs?page={page}",
            ),
        )
        self.assertFalse(session.static_html_pagination["scan_complete"])
        self.assertEqual(
            session.static_html_pagination["stop_reason"],
            "empty_page_without_terminal_contract",
        )

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_11_repeated_stable_id_with_conflicting_url_fails_closed(self):
        session = Session(Response(page(card("1", href="/jobs/a"))), Response(page(card("1", href="/jobs/b"))))
        with self.assertRaisesRegex(AdapterError, "identity_conflict"):
            static_html.fetch_jobs(
                session,
                target(max_pages=2, pagination_url_template="https://jobs.example.test/jobs?page={page}"),
            )
        self.assertEqual(session.static_html_pagination["stop_reason"], "conflicting_stable_id")

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
        session = Session(Response(page(many)))
        self.assertEqual(len(static_html.fetch_jobs(session, target(max_jobs=4))), 30)
        self.assertFalse(session.static_html_pagination["scan_complete"])
        self.assertEqual(
            session.static_html_pagination["scan_completeness_reason"],
            "no_pagination_configured",
        )
        self.assertEqual(session.static_html_pagination["total_available"], 30)

    def test_no_pagination_config_with_active_load_more_is_incomplete(self):
        body = page(
            card("1", href="/jobs/1"),
            "<a class='load-more' href='/jobs?page=2'>Load More</a>",
        )
        session = Session(Response(body))

        self.assertEqual(len(static_html.fetch_jobs(session, target())), 1)
        self.assertFalse(session.static_html_pagination["scan_complete"])
        self.assertTrue(session.static_html_pagination["has_more"])

    def test_20_unsupported_selector_and_regex_contracts(self):
        with self.assertRaisesRegex(AdapterError, "unsupported construct"):
            static_html.validate_target(target(job_card_selector="article.job > div.job"))
        with self.assertRaisesRegex(AdapterError, "unsafe construct"):
            static_html.validate_target(target(external_id_attribute=None, external_id_regex="(?=x)"))

    def test_21_non_html_and_empty_success_rejected(self):
        for response in (Response("{}", content_type="application/json"), Response("")):
            with self.assertRaises(AdapterError):
                static_html.fetch_jobs(Session(response), target())

    def test_22_consent_page_rejected(self):
        with self.assertRaisesRegex(AdapterError, "consent"):
            static_html.fetch_jobs(Session(Response("<html><body>Cookie consent required</body></html>")), target())

    def test_23_text_identifier_and_exact_card_attribution(self):
        body = page(
            "<article class='job'><span class='brand'>Zoom</span>"
            "<span class='req'>R12345</span><h2><a class='title' href='/jobs/role'>Engineer</a></h2>"
            "<span class='location'>Remote, United States</span></article>"
            "<article class='job'><span class='brand'>Workvivo</span>"
            "<span class='req'>R12346</span><h2><a class='title' href='/jobs/other'>Engineer</a></h2>"
            "<span class='location'>Remote, United States</span></article>"
        )
        jobs = static_html.fetch_jobs(Session(Response(body)), target(
            external_id_attribute=None,
            external_id_selector="span.req",
            job_attribution_selector="span.brand",
            job_attribution_regex=r"Zoom",
        ))
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].job_id.endswith(":R12345"))

    def test_24_text_identifier_requires_one_match(self):
        body = page(card().replace("data-id='123'", "") + "<span class='req'>R2</span>")
        with self.assertRaisesRegex(AdapterError, "exactly once"):
            static_html.fetch_jobs(Session(Response(body)), target(
                external_id_attribute=None, external_id_selector="span.req"
            ))

    def test_25_json_ld_jobposting_precedes_changed_html_contract(self):
        payload = {
            "@type": "ItemList",
            "numberOfItems": 1,
            "itemListElement": [{
                "@type": "JobPosting",
                "title": "Controls Engineer",
                "identifier": {"value": "42"},
                "url": "https://jobs.example.test/jobs/42",
                "datePosted": "2026-09-01",
                "jobLocation": {
                    "address": {
                        "addressLocality": "Calgary",
                        "addressRegion": "AB",
                        "addressCountry": "Canada",
                    }
                },
            }],
        }
        body = (
            "<html><head><script type='application/ld+json'>"
            + json.dumps(payload)
            + "</script></head><body><div id='changed'></div></body></html>"
        )
        session = Session(Response(body))

        jobs = static_html.fetch_jobs(
            session,
            target(external_id_attribute=None, external_id_regex=r"/jobs/(\d+)$"),
        )

        self.assertEqual((jobs[0].title, jobs[0].location), ("Controls Engineer", "Calgary, AB, Canada"))
        self.assertTrue(session.static_html_pagination["scan_complete"])
        self.assertEqual(session.static_html_pagination["structured_contract"], "json_ld_job_posting")

    def test_26_json_ld_proves_zero_but_spa_shell_does_not(self):
        zero = (
            "<html><script type='application/ld+json'>"
            '{"@type":"ItemList","numberOfItems":0,"itemListElement":[]}'
            "</script></html>"
        )
        self.assertEqual(static_html.fetch_jobs(Session(Response(zero)), target()), [])

        shell = "<html><head><title>Jobs</title></head><body><div id='app'></div></body></html>"
        with self.assertRaisesRegex(AdapterError, "unsupported_listing_contract"):
            static_html.fetch_jobs(Session(Response(shell)), target())

    def test_27_malformed_structured_payload_has_bounded_classification(self):
        body = "<html><script type='application/ld+json'>{bad</script></html>"
        with self.assertRaisesRegex(AdapterError, "structured_payload_malformed"):
            static_html.fetch_jobs(Session(Response(body)), target())

    def test_28_large_html_uses_bounded_prefix_only_when_contract_is_present(self):
        relevant = page(card()) + ("x" * 5000)
        session = Session(Response(relevant))
        jobs = static_html.fetch_jobs(
            session, target(max_response_bytes=1000)
        )
        self.assertEqual(len(jobs), 1)
        self.assertTrue(session.static_html_pagination["html_truncated"])
        self.assertTrue(session.static_html_pagination["safety_limit_reached"])
        self.assertFalse(session.static_html_pagination["scan_complete"])
        self.assertLessEqual(session.static_html_pagination["bytes_inspected"], 1000)

        irrelevant = "<html><body>" + ("x" * 5000)
        with self.assertRaisesRegex(AdapterError, "html_too_large"):
            static_html.fetch_jobs(
                Session(Response(irrelevant)), target(max_response_bytes=1000)
            )

    def test_29_canonical_url_supplies_title_and_stable_id_but_conflicts_fail(self):
        body = page(
            "<a class='job' href='/role/software-engineer-abc123'></a>"
        )
        canonical_target = target(
            job_card_selector="a.job",
            title_selector="h2",
            detail_link_selector=":self",
            external_id_attribute=None,
            external_id_regex=r"/role/[^/?#]+-([a-z0-9]+)$",
        )
        job = static_html.fetch_jobs(Session(Response(body)), canonical_target)[0]
        self.assertEqual((job.title, job.raw_metadata["external_job_id"]), ("Software engineer", "abc123"))

        with self.assertRaisesRegex(AdapterError, "identity_missing"):
            static_html.fetch_jobs(
                Session(Response(page(card(job_id="one", href="/jobs/two")))),
                target(external_id_regex=r"/jobs/([^/?#]+)$"),
            )

    def test_30_access_blocked_and_identity_missing_codes_are_explicit(self):
        with self.assertRaisesRegex(AdapterError, "access_blocked"):
            static_html.fetch_jobs(Session(Response("denied", status=403)), target())
        with self.assertRaisesRegex(AdapterError, "identity_missing"):
            static_html.fetch_jobs(
                Session(Response(page("<article class='job'><a class='title' href='/jobs/no-id'>Engineer</a></article>"))),
                target(external_id_attribute=None, external_id_regex=r"/jobs/(\d+)$"),
            )

    def test_31_oversized_irrelevant_stream_stops_at_the_hard_prefix_bound(self):
        response = StreamingResponse(
            [b"<html><body>"] + [b"x" * 128 for _ in range(1000)]
        )
        with self.assertRaisesRegex(AdapterError, "html_too_large"):
            static_html.fetch_jobs(
                Session(response), target(max_response_bytes=1000)
            )
        self.assertLess(response.yielded, 20)
        self.assertTrue(response.closed)

    def test_32_duplicate_equivalent_title_nodes_are_accepted(self):
        body = page(
            "<article class='job' data-id='123'>"
            "<a class='title' href='/jobs/123'>  Controls   Engineer </a>"
            "<a class='title' href='/jobs/123'>Controls Engineer</a>"
            "</article>"
        )
        session = Session(Response(body))
        jobs = static_html.fetch_jobs(session, target())
        self.assertEqual([job.title for job in jobs], ["Controls Engineer"])
        self.assertEqual(
            jobs[0].raw_metadata["listing_resolution"]["candidate_title_count"], 2
        )
        self.assertEqual(
            jobs[0].raw_metadata["listing_resolution"]["normalized_equivalent_title_count"], 1
        )

    def test_33_desktop_mobile_duplicate_markup_is_equivalent(self):
        body = page(
            "<div class='desktop'><a class='job' href='/jobs/123'>Engineer</a></div>"
            "<div class='mobile'><a class='job' href='/jobs/123'> Engineer </a></div>"
        )
        jobs = static_html.fetch_jobs(
            Session(Response(body)),
            target(
                job_card_selector="a.job",
                title_selector=":self",
                detail_link_selector=":self",
                external_id_attribute=None,
                external_id_regex=r"/jobs/(\d+)$",
            ),
        )
        self.assertEqual([(job.title, job.raw_metadata["external_job_id"]) for job in jobs], [("Engineer", "123")])

    def test_34_conflicting_titles_in_one_record_are_rejected(self):
        body = page(
            "<article class='job' data-id='123'>"
            "<a class='title' href='/jobs/123'>Engineer</a>"
            "<a class='title' href='/jobs/123'>Engineering Manager</a>"
            "</article>"
        )
        session = Session(Response(body))
        with self.assertRaisesRegex(AdapterError, "title selector was ambiguous"):
            static_html.fetch_jobs(session, target())
        diagnostic = session.static_html_pagination["diagnostics"][0]
        self.assertEqual(diagnostic["candidate_title_count"], 2)
        self.assertEqual(diagnostic["normalized_equivalent_title_count"], 2)
        self.assertEqual(diagnostic["reason"], "conflicting_titles_within_precedence")

    def test_35_page_heading_does_not_contaminate_card_title(self):
        body = (
            "<html><body><h2>Global careers heading</h2><main class='listing'>"
            "<article class='job' data-id='123'><a class='title' href='/jobs/123'>Engineer</a></article>"
            "</main></body></html>"
        )
        jobs = static_html.fetch_jobs(
            Session(Response(body)), target(title_selector="h2")
        )
        self.assertEqual(jobs[0].title, "Engineer")

    def test_36_link_scoping_binds_associated_heading_and_splits_broad_container(self):
        body = page(
            "<article><h4>Robotics Engineer</h4><a class='job' href='/role/robotics-engineer-a1b2c3'>Apply</a></article>"
            "<article><h4>Controls Engineer</h4><a class='job' href='/role/controls-engineer-d4e5f6'>Apply</a></article>"
        )
        jobs = static_html.fetch_jobs(
            Session(Response(body)),
            target(
                job_card_selector="main.listing",
                title_selector="h4",
                detail_link_selector="a.job",
                external_id_attribute=None,
                external_id_regex=r"/role/[^/?#]+-([a-z0-9]+)$",
            ),
        )
        self.assertEqual(
            [(job.title, job.raw_metadata["external_job_id"]) for job in jobs],
            [("Robotics Engineer", "a1b2c3"), ("Controls Engineer", "d4e5f6")],
        )

    def test_37_canonical_url_anchor_title_generic_id_and_tracking_cleanup(self):
        body = page(
            "<a class='job' href='/role/software-engineer-abc123?utm_source=mail&amp;gclid=track'>Software Engineer</a>"
        )
        job = static_html.fetch_jobs(
            Session(Response(body)),
            target(
                job_card_selector="a.job",
                title_selector=":self",
                detail_link_selector=":self",
                external_id_attribute=None,
                external_id_regex=None,
                allow_url_id_fallback=True,
            ),
        )[0]
        self.assertEqual(job.title, "Software Engineer")
        self.assertEqual(job.raw_metadata["external_job_id"], "abc123")
        self.assertEqual(job.url, "https://jobs.example.test/role/software-engineer-abc123")

    def test_38_malformed_titled_record_is_skipped_but_marks_scan_incomplete(self):
        body = page(
            card("1", "Engineer", "/jobs/1")
            + "<article class='job' data-id='2'><a class='title' href='/jobs/2'></a></article>"
        )
        session = Session(Response(body))
        jobs = static_html.fetch_jobs(session, target())
        self.assertEqual([job.raw_metadata["external_job_id"] for job in jobs], ["1"])
        self.assertFalse(session.static_html_pagination["scan_complete"])
        self.assertEqual(
            session.static_html_pagination["scan_completeness_reason"],
            "malformed_listing_records",
        )
        self.assertEqual(session.static_html_pagination["malformed_records"], 1)

    def test_39_missing_identity_in_any_record_fails_closed(self):
        body = page(
            card("1", "Engineer", "/jobs/1")
            + "<article class='job'><span class='title'>Unknown role</span></article>"
        )
        with self.assertRaisesRegex(AdapterError, "canonical job URL"):
            static_html.fetch_jobs(Session(Response(body)), target())

    def test_40_duplicate_stable_ids_accept_identical_and_reject_conflicts(self):
        identical = page(card("1", "Engineer", "/jobs/1") + card("1", "Engineer", "/jobs/1"))
        session = Session(Response(identical))
        self.assertEqual(len(static_html.fetch_jobs(session, target())), 1)
        self.assertEqual(session.static_html_pagination["equivalent_duplicate_records"], 1)

        conflicting = page(card("1", "Engineer", "/jobs/1") + card("1", "Manager", "/jobs/1"))
        with self.assertRaisesRegex(AdapterError, "identity_conflict"):
            static_html.fetch_jobs(Session(Response(conflicting)), target())


if __name__ == "__main__":
    unittest.main()
