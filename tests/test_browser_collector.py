from __future__ import annotations

import inspect
import unittest
from unittest.mock import Mock, patch

from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import browser_rendered_html, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


def rendered_page(body: str) -> str:
    return f"<!doctype html><html><head><title>Example Careers</title></head><body>{body}</body></html>"


def target(**overrides):
    values = {
        "name": "Example",
        "platform": "browser_rendered_html",
        "listing_url": "https://jobs.example.test/jobs",
        "endpoint_variant": "browser_rendered_html_v1",
        "browser_wait_selector": "a.job",
        "browser_scroll_steps": 0,
        "browser_static_variant": "server_rendered_links",
        "browser_identity_selector": "title",
        "browser_identity_regex": "Example Careers",
        "listing_selector": "a.job",
        "job_card_selector": "a.job",
        "title_selector": ":self",
        "detail_link_selector": ":self",
        "external_id_regex": r"/jobs/([0-9]+)$",
        "allowed_apply_hosts": ["jobs.example.test"],
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


class Response:
    status = 200


class Mouse:
    def __init__(self):
        self.calls = []

    def wheel(self, x, y):
        self.calls.append((x, y))


class Page:
    def __init__(self, content=None, wait_error=None, status=200):
        self._content = content or rendered_page("<a class='job' href='/jobs/123'>Engineer</a>")
        self.wait_error = wait_error
        self.response = Response()
        self.response.status = status
        self.url = "https://jobs.example.test/jobs"
        self.closed = False
        self.goto_calls = []
        self.wait_calls = []
        self.mouse = Mouse()

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        return self.response

    def wait_for_selector(self, selector, **kwargs):
        self.wait_calls.append((selector, kwargs))
        if self.wait_error:
            raise self.wait_error

    def wait_for_timeout(self, _milliseconds):
        return None

    def content(self):
        return self._content

    def close(self):
        self.closed = True


class StaticSession:
    def __init__(self, body):
        self.body = body

    def get(self, url, **_kwargs):
        return Mock(
            status_code=200,
            content=self.body.encode(),
            headers={"content-type": "text/html"},
            url=url,
            history=[],
        )


class Context:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.routes = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class Browser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.contexts = []
        self.closed = False

    def new_context(self):
        context = Context(self.pages.pop(0))
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class Chromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_calls = []

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self.browser


class Playwright:
    def __init__(self, browser):
        self.chromium = Chromium(browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class Manager:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


def batch_for(*pages):
    browser = Browser(pages)
    playwright = Playwright(browser)
    manager = Manager(playwright)
    batch = browser_rendered_html.BrowserBatch(loader=lambda: lambda: manager)
    return batch, browser, playwright


class BrowserCollectorTests(unittest.TestCase):
    def test_01_playwright_import_is_lazy_for_normal_adapters(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            content=rendered_page("<a class='job' href='/jobs/123'>Engineer</a>").encode(),
            headers={"content-type": "text/html"},
            url="https://jobs.example.test/jobs",
        )
        static_target = target(platform="static_html", endpoint_variant="server_rendered_links")
        with patch.object(browser_rendered_html, "_load_sync_playwright") as loader:
            jobs = static_html.fetch_jobs(session, static_target)
        self.assertEqual(len(jobs), 1)
        loader.assert_not_called()

    def test_02_one_browser_and_fresh_context_page_per_company(self):
        first, second = Page(), Page()
        batch, browser, playwright = batch_for(first, second)
        with batch:
            batch.fetch_jobs(target(name="One"))
            batch.fetch_jobs(target(name="Two"))
        self.assertEqual(len(browser.contexts), 2)
        self.assertTrue(all(context.closed for context in browser.contexts))
        self.assertTrue(first.closed and second.closed and browser.closed and playwright.stopped)

    def test_03_cleanup_is_guaranteed_on_parser_failure(self):
        page = Page()
        batch, browser, playwright = batch_for(page)
        with self.assertRaises(AdapterError):
            with batch:
                batch.fetch_jobs(target(allowed_apply_hosts=["official.example.test"]))
        self.assertTrue(page.closed and browser.contexts[0].closed)
        self.assertTrue(browser.closed and playwright.stopped)

    def test_04_wait_selector_failure_is_closed_and_cleanup_runs(self):
        page = Page(wait_error=TimeoutError("selector timeout"))
        batch, browser, _playwright = batch_for(page)
        with batch:
            with self.assertRaisesRegex(AdapterError, "timeout or wait selector"):
                batch.fetch_jobs(target(), timeout=99)
        self.assertEqual(page.goto_calls[0][1]["timeout"], 30_000)
        self.assertTrue(page.closed and browser.contexts[0].closed)

    def test_05_hard_configuration_limits(self):
        for overrides, message in (
            ({"max_pages": 3}, "page or result"),
            ({"max_jobs": 21}, "page or result"),
            ({"max_response_bytes": 3_000_001}, "byte limit"),
            ({"browser_scroll_steps": 4}, "scroll steps"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(AdapterError, message):
                    browser_rendered_html.validate_target(target(**overrides))

    def test_06_rendered_html_size_and_malformed_limits(self):
        oversized = Page(rendered_page("x" * 3_000_000))
        malformed = Page("<main>not a complete document</main>")
        for page, message in ((oversized, "3 MB"), (malformed, "malformed")):
            batch, _browser, _playwright = batch_for(page)
            with batch:
                with self.assertRaisesRegex(AdapterError, message):
                    batch.fetch_jobs(target())

    def test_07_rejects_blocked_login_session_consent_and_captcha_pages(self):
        bodies = [
            "Access denied",
            "Consent required",
            "Session expired",
            "Verify you are human to continue",
        ]
        for body in bodies:
            with self.subTest(body=body):
                page = Page(rendered_page(body))
                batch, _browser, _playwright = batch_for(page)
                with batch:
                    with self.assertRaises(AdapterError):
                        batch.fetch_jobs(target())
        login = Page(rendered_page("Please sign in"))
        login._content = login._content.replace("Example Careers", "Log In")
        batch, _browser, _playwright = batch_for(login)
        with batch:
            with self.assertRaisesRegex(AdapterError, "login"):
                batch.fetch_jobs(target())

    def test_08_rendered_html_delegates_to_static_parser(self):
        page = Page()
        batch, _browser, _playwright = batch_for(page)
        delegated_job = JobRecord("static_html:x:1", "Role", "Example", "", "https://jobs.example.test/jobs/1", "", "static_html:x")

        def delegated(session, static_target, timeout):
            response = session.get(static_target.listing_url, timeout=timeout)
            self.assertIn("Engineer", response.text)
            self.assertEqual(static_target.platform, "static_html")
            return [delegated_job]

        with batch, patch.object(static_html, "fetch_jobs", side_effect=delegated) as parser:
            jobs = batch.fetch_jobs(target())
        parser.assert_called_once()
        self.assertEqual(jobs[0].job_id, "browser_rendered_html:x:1")
        self.assertEqual(jobs[0].raw_metadata["platform"], "browser_rendered_html")

    def test_09_static_parser_enforces_ids_and_official_apply_hosts(self):
        valid_page = Page()
        batch, _browser, _playwright = batch_for(valid_page)
        with batch:
            jobs = batch.fetch_jobs(target())
        self.assertEqual(jobs[0].job_id, "browser_rendered_html:jobs.example.test:123")
        self.assertEqual(jobs[0].url, "https://jobs.example.test/jobs/123")

        invalid = Page(rendered_page("<a class='job' href='https://evil.test/jobs/123'>Engineer</a>"))
        batch, _browser, _playwright = batch_for(invalid)
        with batch:
            with self.assertRaisesRegex(AdapterError, "official allowed-host"):
                batch.fetch_jobs(target())

    def test_10_mixed_or_ambiguous_identity_fails_closed(self):
        page = Page(rendered_page("<h1>Other Company</h1><a class='job' href='/jobs/123'>Engineer</a>"))
        page._content = page._content.replace("<title>Example Careers</title>", "<title>Example Careers</title><title>Other Careers</title>")
        batch, _browser, _playwright = batch_for(page)
        with batch:
            with self.assertRaisesRegex(AdapterError, "mixed, or ambiguous"):
                batch.fetch_jobs(target())

    def test_11_resource_blocking_and_declarative_scroll(self):
        route = Mock()
        route.request.resource_type = "image"
        browser_rendered_html._route_resources(route)
        route.abort.assert_called_once()
        route = Mock()
        route.request.resource_type = "script"
        browser_rendered_html._route_resources(route)
        route.continue_.assert_called_once()

        page = Page()
        batch, _browser, _playwright = batch_for(page)
        with batch:
            batch.fetch_jobs(target(browser_scroll_steps=3))
        self.assertEqual(page.mouse.calls, [(0, 1600)] * 3)

    def test_12_missing_runtime_fails_closed(self):
        with patch.object(browser_rendered_html.importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(AdapterError, "Playwright 1.57.0"):
                browser_rendered_html._load_sync_playwright()

    def test_13_no_injected_javascript_or_company_branches(self):
        source = inspect.getsource(browser_rendered_html)
        self.assertNotIn(".evaluate(", source)
        self.assertNotIn("add_init_script", source)
        self.assertNotIn("storage_state", source)
        self.assertNotIn("proxy=", source)
        for company in ("Apple", "Figure", "LINE"):
            self.assertNotIn(company, source)

    def test_14_scanner_registers_generic_platform(self):
        self.assertIs(scanner.ADAPTERS["browser_rendered_html"], browser_rendered_html.fetch_jobs)

    def test_15_static_and_rendered_paths_share_listing_resolution(self):
        body = rendered_page(
            "<main class='listing'><article>"
            "<h4>Software Engineer</h4>"
            "<a class='job' href='/jobs/123?utm_source=mail'>Apply</a>"
            "</article></main>"
        )
        configured = target(
            listing_selector="main.listing",
            job_card_selector="a.job",
            title_selector="h4",
        )
        static_jobs = static_html.fetch_jobs(
            StaticSession(body), browser_rendered_html._static_target(configured)
        )
        batch, _browser, _playwright = batch_for(Page(body))
        with batch:
            rendered_jobs = batch.fetch_jobs(configured)

        self.assertEqual(
            [
                (
                    job.title,
                    job.url,
                    job.location,
                    job.raw_metadata["external_job_id"],
                )
                for job in static_jobs
            ],
            [
                (
                    job.title,
                    job.url,
                    job.location,
                    job.raw_metadata["external_job_id"],
                )
                for job in rendered_jobs
            ],
        )

    def test_16_rendered_complete_snapshot_retains_all_observed_jobs(self):
        links = "".join(
            f"<a class='job' href='/jobs/{index}'>Engineer {index}</a>"
            for index in range(25)
        )
        batch, _browser, _playwright = batch_for(Page(rendered_page(links)))
        with batch:
            jobs = batch.fetch_jobs(target())
        self.assertEqual(len(jobs), 25)
        self.assertEqual(batch.last_run["static_parser"]["total_available"], 25)


if __name__ == "__main__":
    unittest.main()
