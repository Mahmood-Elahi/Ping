"""Bounded Playwright rendering delegated to the static HTML adapter.

Playwright is deliberately imported only when a browser batch is entered.
Every target gets a new context and page; a batch reuses only the browser
process.  The browser does not inject JavaScript or preserve browser state.
"""

from __future__ import annotations

import importlib
import re
import time
from dataclasses import replace
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from lxml import html

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.listing_completion import ABSOLUTE_LISTING_JOB_CEILING


MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_RESULTS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000
MAX_SCROLL_STEPS = 3
SCROLL_PIXELS = 1_600
SCROLL_SETTLE_MS = 250
BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}
SUPPORTED_VARIANTS = {"browser_rendered_html_v1"}
_REJECTED_URL_PARTS = ("/login", "/signin", "/session", "/captcha", "/challenge")
_REJECTED_TEXT = (
    "access denied",
    "request blocked",
    "cloudflare challenge",
    "verify you are human",
    "consent required",
    "cookie consent required",
    "session required",
    "session expired",
)


class _RenderedResponse:
    def __init__(self, body: bytes, url: str) -> None:
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.status_code = 200
        self.url = url
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.history: list[Any] = []


def _load_sync_playwright() -> Any:
    """Import Playwright only when a browser-backed batch actually starts."""
    try:
        return importlib.import_module("playwright.sync_api").sync_playwright
    except (ImportError, AttributeError) as exc:
        raise AdapterError("Browser collector requires Playwright 1.57.0 and Chromium") from exc


def _route_resources(route: Any, request: Optional[Any] = None) -> None:
    request = request or route.request
    if str(getattr(request, "resource_type", "")).casefold() in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("listing_url", target.listing_url),
            ("browser_wait_selector", target.browser_wait_selector),
            ("browser_static_variant", target.browser_static_variant),
            ("browser_identity_selector", target.browser_identity_selector),
            ("browser_identity_regex", target.browser_identity_regex),
        )
        if not value
    ]
    if missing:
        raise AdapterError(f"Browser-rendered target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"Browser-rendered unsupported variant: {target.endpoint_variant}")
    if not 0 <= int(target.browser_scroll_steps) <= MAX_SCROLL_STEPS:
        raise AdapterError("Browser-rendered scroll steps exceed the hard maximum")
    if not 1 <= int(target.max_pages) <= MAX_PAGES or not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Browser-rendered page or result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= MAX_RENDERED_HTML_BYTES:
        raise AdapterError("Browser-rendered HTML byte limit exceeds the hard maximum")
    static_target = _static_target(target)
    static_html.validate_target(static_target)
    try:
        re.compile(str(target.browser_identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"Browser-rendered identity regex is invalid: {exc}") from exc


def _static_target(target: CompanyTarget) -> CompanyTarget:
    return replace(
        target,
        platform="static_html",
        endpoint_variant=str(target.browser_static_variant or ""),
        max_pages=min(int(target.max_pages), MAX_PAGES),
        max_jobs=min(int(target.max_jobs), MAX_RESULTS),
        max_response_bytes=min(int(target.max_response_bytes), MAX_RENDERED_HTML_BYTES),
    )


def _validate_document(content: bytes, url: str, target: CompanyTarget) -> None:
    if not content or len(content) > min(int(target.max_response_bytes), MAX_RENDERED_HTML_BYTES):
        raise AdapterError("Browser-rendered HTML is empty or exceeded the 3 MB limit")
    stripped = content.lstrip().lower()
    if not (stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html")) or b"</html>" not in stripped:
        raise AdapterError("Browser-rendered HTML is malformed")
    parsed_url = urlparse(url)
    if any(part in parsed_url.path.casefold() for part in _REJECTED_URL_PARTS):
        raise AdapterError("Browser-rendered login, CAPTCHA, or session page rejected")
    try:
        root = html.fromstring(content, base_url=url)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Browser-rendered HTML is malformed: {exc}") from exc
    for node in root.xpath("//script|//style|//noscript"):
        node.drop_tree()
    text = " ".join(root.text_content().split()).casefold()
    title = " ".join(root.xpath("string(//title)").split()).casefold()
    rejected_marker = next((marker for marker in _REJECTED_TEXT if marker in text or marker in title), None)
    if rejected_marker:
        raise AdapterError(
            f"Browser-rendered blocked, consent, CAPTCHA, or session page rejected ({rejected_marker})"
        )
    if "captcha" in title or ("captcha" in text and len(text) < 5_000):
        raise AdapterError("Browser-rendered CAPTCHA page rejected")
    if re.search(r"\b(sign in|log in|login)\b", title):
        raise AdapterError("Browser-rendered login page rejected")
    identity_nodes = static_html.select(root, str(target.browser_identity_selector))
    identities = [" ".join(node.text_content().split()) for node in identity_nodes]
    matches = [value for value in identities if re.search(str(target.browser_identity_regex), value, re.IGNORECASE)]
    if not identities or len(matches) != len(identities):
        raise AdapterError("Browser-rendered company identity is missing, mixed, or ambiguous")


class _RenderedSession:
    def __init__(self, page: Any, target: CompanyTarget, timeout: int) -> None:
        self.page = page
        self.target = target
        self.timeout_seconds = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
        self.pages_rendered = 0
        self.rendered_urls: list[str] = []
        self.rendered_bytes = 0

    def get(self, url: str, **_: Any) -> _RenderedResponse:
        if self.pages_rendered >= min(int(self.target.max_pages), MAX_PAGES):
            raise AdapterError("Browser-rendered page limit exceeded")
        started = time.monotonic()
        budget_ms = self.timeout_seconds * 1000
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=budget_ms)
            status = getattr(response, "status", 200) if response is not None else 200
            if status in {401, 407}:
                raise AdapterError(f"Browser-rendered authentication required (HTTP {status})")
            if status in {403, 429, 451}:
                raise AdapterError(f"Browser-rendered blocked response (HTTP {status})")
            if status >= 400:
                raise AdapterError(f"Browser-rendered HTTP {status}")
            elapsed_ms = int((time.monotonic() - started) * 1000)
            remaining_ms = max(1, budget_ms - elapsed_ms)
            self.page.wait_for_selector(
                str(self.target.browser_wait_selector), state="attached", timeout=remaining_ms
            )
            for _step in range(int(self.target.browser_scroll_steps)):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if elapsed_ms + SCROLL_SETTLE_MS >= budget_ms:
                    raise AdapterError("Browser-rendered page exceeded the 30-second timeout")
                self.page.mouse.wheel(0, SCROLL_PIXELS)
                self.page.wait_for_timeout(SCROLL_SETTLE_MS)
            body = str(self.page.content()).encode("utf-8")
        except AdapterError:
            raise
        except Exception as exc:
            name = exc.__class__.__name__.casefold()
            if "timeout" in name or "timeout" in str(exc).casefold():
                raise AdapterError("Browser-rendered timeout or wait selector failure") from exc
            raise AdapterError(f"Browser-rendered navigation failed: {exc}") from exc
        final_url = str(getattr(self.page, "url", url) or url)
        _validate_document(body, final_url, self.target)
        if self.rendered_bytes + len(body) > min(
            int(self.target.max_response_bytes), MAX_RENDERED_HTML_BYTES
        ):
            raise AdapterError("Browser-rendered batch HTML exceeded the 3 MB limit")
        self.pages_rendered += 1
        self.rendered_urls.append(final_url)
        self.rendered_bytes += len(body)
        return _RenderedResponse(body, final_url)


class BrowserBatch:
    """Own one Chromium process and isolate each company in a fresh context."""

    def __init__(self, timeout: int = MAX_TIMEOUT_SECONDS, loader: Optional[Callable[[], Any]] = None) -> None:
        self.timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
        self._loader = loader or _load_sync_playwright
        self._manager: Optional[Any] = None
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self.last_run: dict[str, Any] = {}

    def __enter__(self) -> "BrowserBatch":
        try:
            self._manager = self._loader()()
            self._playwright = self._manager.start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except AdapterError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise AdapterError("Browser collector could not start Playwright Chromium") from exc
        return self

    def close(self) -> None:
        browser, playwright, manager = self._browser, self._playwright, self._manager
        self._browser = None
        self._playwright = None
        self._manager = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if playwright is not None:
                playwright.stop()
            elif manager is not None and hasattr(manager, "__exit__"):
                manager.__exit__(None, None, None)

    def __exit__(self, *_: Any) -> None:
        self.close()

    def fetch_jobs(self, target: CompanyTarget, timeout: Optional[int] = None) -> list[JobRecord]:
        validate_target(target)
        if self._browser is None:
            raise AdapterError("Browser batch is not active")
        context = page = None
        started = time.monotonic()
        try:
            context = self._browser.new_context()
            context.route("**/*", _route_resources)
            page = context.new_page()
            rendered_session = _RenderedSession(page, target, timeout or self.timeout)
            jobs = static_html.fetch_jobs(rendered_session, _static_target(target), timeout or self.timeout)
            if len(jobs) > ABSOLUTE_LISTING_JOB_CEILING:
                raise AdapterError("Browser-rendered normalized job hard limit exceeded")
            for job in jobs:
                job.job_id = job.job_id.replace("static_html:", "browser_rendered_html:", 1)
                job.source = job.source.replace("static_html:", "browser_rendered_html:", 1)
                job.raw_metadata["platform"] = "browser_rendered_html"
            self.last_run = {
                "company": target.name,
                "runtime_seconds": round(time.monotonic() - started, 3),
                "pages_rendered": rendered_session.pages_rendered,
                "rendered_bytes": rendered_session.rendered_bytes,
                "rendered_urls": list(rendered_session.rendered_urls),
                "jobs_normalized": len(jobs),
                "static_parser": getattr(rendered_session, "static_html_pagination", {}),
            }
            return jobs
        finally:
            try:
                if page is not None:
                    page.close()
            finally:
                if context is not None:
                    context.close()


def fetch_jobs(_session: Any, target: CompanyTarget, timeout: int = MAX_TIMEOUT_SECONDS) -> list[JobRecord]:
    """Compatibility entry point; batch callers should reuse :class:`BrowserBatch`."""
    with BrowserBatch(timeout=timeout) as batch:
        return batch.fetch_jobs(target, timeout)
