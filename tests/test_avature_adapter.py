from __future__ import annotations

import unittest
from unittest.mock import patch

from src.scraper.company_careers.adapters import avature
from src.scraper.company_careers.models import AdapterError, CompanyTarget


class Response:
    def __init__(self, text: str, status: int = 200, content_type: str = "text/html"):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.url = "https://jobs.example.com/search"


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
        platform="avature",
        tenant="example.avature.net",
        board_id="42",
        search_url="https://jobs.example.com/search",
        endpoint_variant=avature.PORTAL_HTML,
        allowed_apply_hosts=["jobs.example.com"],
        max_pages=2,
        max_jobs=20,
        page_size=2,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def page(*jobs, total=None):
    total_attr = f' data-total-job-results="{total}"' if total is not None else ""
    articles = "".join(
        f'''<article class="article article--result">
          <h3><a href="https://jobs.example.com/en_US/careers/JobDetail/Role/{job_id}">{title}</a></h3>
          <span class="list-item-location">{location}</span>
          <span class="list-item-location">Remote</span>
          <span class="list-item-family">{category}</span>
          <span class="list-item-posted">Posted: {posted}</span>
        </article>'''
        for job_id, title, location, category, posted in jobs
    )
    return Response(f"<html><body><section{total_attr}>{articles}</section></body></html>")


def radancy_page(job_ids, *, total):
    items = "".join(
        f'''<li class="search-results-list__list-item" data-intuit-jobid="{job_id}">
          <a data-job-id="{job_id}" href="https://jobs.example.com/job/role/{job_id}">
            <h2>Role {job_id}</h2>
            <span class="job-location">Calgary</span>
            <span class="job-date-posted">Posted: 08/01/2026</span>
          </a>
        </li>'''
        for job_id in job_ids
    )
    return Response(
        f'<html><body><section data-total-job-results="{total}"><ul>{items}</ul></section></body></html>'
    )


JOB1 = ("101", "Engineer", "Calgary", "Engineering", "08/01/2026")
JOB2 = ("102", "Analyst", "Toronto", "Finance", "Aug 02, 2026")


class AvatureAdapterTests(unittest.TestCase):
    def test_01_valid_listing_normalization(self):
        jobs = avature.fetch_jobs(Session([page(JOB1, total=1)]), target(page_size=10))
        self.assertEqual((jobs[0].title, jobs[0].company), ("Engineer", "Example"))

    def test_02_stable_id_preservation(self):
        job = avature.fetch_jobs(Session([page(JOB1, total=1)]), target(page_size=10))[0]
        self.assertEqual(job.job_id, "avature:jobs.example.com:101")
        self.assertEqual(job.raw_metadata["external_job_id"], "101")

    def test_03_apply_url_validation(self):
        bad = page(JOB1, total=1)
        bad.text = bad.text.replace("jobs.example.com", "evil.example")
        with self.assertRaisesRegex(AdapterError, "none were valid"):
            avature.fetch_jobs(Session([bad]), target(page_size=10))

    def test_04_multiple_location_normalization(self):
        job = avature.fetch_jobs(Session([page(JOB1, total=1)]), target(page_size=10))[0]
        self.assertEqual(job.offices, ["Calgary", "Remote"])
        self.assertIn("Remote", job.location)

    def test_05_date_and_category_preservation(self):
        job = avature.fetch_jobs(Session([page(JOB1, total=1)]), target(page_size=10))[0]
        self.assertEqual(job.updated_at, "2026-08-01")
        self.assertEqual(job.departments, ["Engineering"])

    @patch.object(avature.time, "sleep", return_value=None)
    def test_06_deterministic_pagination(self, _sleep):
        session = Session([page(JOB1, JOB2), page(("103", "Third", "Remote", "IT", "08/03/2026"))])
        avature.fetch_jobs(session, target())
        self.assertEqual(session.calls[0][1]["params"]["jobOffset"], "0")
        self.assertEqual(session.calls[1][1]["params"]["jobOffset"], "2")

    @patch.object(avature.time, "sleep", return_value=None)
    def test_07_repeated_page_detection(self, _sleep):
        session = Session([page(JOB1, JOB2), page(JOB1, JOB2)])
        with self.assertRaisesRegex(AdapterError, "repeated a complete page"):
            avature.fetch_jobs(session, target())
        self.assertEqual(session.avature_pagination["stop_reason"], "repeated_page")
        self.assertTrue(session.avature_pagination["repeated_page"])

    @patch.object(avature.time, "sleep", return_value=None)
    def test_08_no_new_id_termination(self, _sleep):
        second = page(JOB1, ("101", "Engineer duplicate", "Calgary", "Engineering", "08/01/2026"))
        session = Session([page(JOB1, JOB2), second])
        with self.assertRaisesRegex(AdapterError, "no new stable job IDs"):
            avature.fetch_jobs(session, target())
        self.assertEqual(session.avature_pagination["stop_reason"], "no_new_stable_ids")

    def test_09_valid_zero_job_response(self):
        response = Response('<html><body><section data-total-job-results="0">0 jobs found</section></body></html>')
        session = Session([response])
        self.assertEqual(avature.fetch_jobs(session, target()), [])
        self.assertTrue(session.avature_pagination["zero_job_contract"])

    def test_10_malformed_http_200_rejection(self):
        with self.assertRaisesRegex(AdapterError, "did not prove"):
            avature.fetch_jobs(Session([Response("<html><body>Welcome</body></html>")]), target())

    def test_11_authentication_rejection(self):
        with self.assertRaisesRegex(AdapterError, "authentication"):
            avature.fetch_jobs(Session([Response("<html><title>Login</title></html>")]), target())

    def test_12_browser_and_session_rejection(self):
        for body, message in [
            ("<html><body>Enable JavaScript to continue</body></html>", "browser"),
            ("<html><title>Session required</title></html>", "session"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                avature.fetch_jobs(Session([Response(body)]), target())

    def test_13_blocked_response_rejection(self):
        with self.assertRaisesRegex(AdapterError, "blocked"):
            avature.fetch_jobs(Session([Response("<html><body>Access denied</body></html>")]), target())

    @patch.object(avature.time, "sleep", return_value=None)
    def test_14_retry_limit(self, _sleep):
        session = Session([Response("", 503), Response("", 503)])
        with self.assertRaisesRegex(AdapterError, "HTTP 503"):
            avature.fetch_jobs(session, target())
        self.assertEqual(len(session.calls), 2)

    def test_15_page_and_job_hard_limits(self):
        with self.assertRaisesRegex(AdapterError, "max_pages"):
            avature.fetch_jobs(Session([]), target(max_pages=3))
        with self.assertRaisesRegex(AdapterError, "max_jobs"):
            avature.fetch_jobs(Session([]), target(max_jobs=21))

    def test_16_unsupported_variant_rejection(self):
        with self.assertRaisesRegex(AdapterError, "endpoint_variant"):
            avature.fetch_jobs(Session([]), target(endpoint_variant="unknown"))

    @patch.object(avature.time, "sleep", return_value=None)
    def test_17_short_page_before_total_uses_effective_size_and_continues(
        self, _sleep
    ):
        session = Session(
            [
                radancy_page(range(100, 115), total=30),
                radancy_page(range(115, 130), total=30),
            ]
        )
        jobs = avature.fetch_jobs(
            session,
            target(
                endpoint_variant=avature.RADANCY_HTML,
                page_size=10,
                max_pages=2,
                max_jobs=20,
            ),
        )

        self.assertEqual(len(jobs), 30)
        self.assertEqual([call[1]["params"]["p"] for call in session.calls], ["1", "2"])
        self.assertEqual(session.avature_pagination["requested_page_size"], 20)
        self.assertEqual(session.avature_pagination["effective_server_page_size"], 15)
        self.assertTrue(session.avature_pagination["page_size_was_clamped"])
        self.assertTrue(session.avature_pagination["scan_complete"])


if __name__ == "__main__":
    unittest.main()
