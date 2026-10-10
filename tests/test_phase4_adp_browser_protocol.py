from __future__ import annotations

import unittest

import requests

from src.scraper.company_careers.adapters import adp, browser_rendered_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget, SourceScanMetadata
from src.scraper.company_careers.source_cooldown import source_health_key
from src.scraper.company_careers.source_health import finalize_source_scan
from tests.test_browser_collector import Page, batch_for, rendered_page, target as browser_target
from tests.test_company_careers_adapter_expansion_v1_batch_c import (
    Response,
    Session,
    myjobs_config,
    myjobs_item,
    myjobs_target,
    wfn_item,
    wfn_target,
)


def identity(name: str = "Example Space") -> Response:
    return Response({"contentLinks": [{"contentBody": {"contentTitle": name}}]})


def wfn_page(rows, *, offset=0, total=None) -> Response:
    return Response(
        {
            "jobRequisitions": list(rows),
            "meta": {
                "startSequence": offset,
                "totalNumber": len(rows) if total is None else total,
            },
        }
    )


class DynamicPage(Page):
    def __init__(self, pages, *, final_urls=None, failure_at=None):
        super().__init__(pages[0])
        self.pages = list(pages)
        self.final_urls = list(final_urls or [])
        self.failure_at = failure_at
        self.index = -1

    def goto(self, url, **kwargs):
        self.index += 1
        self.goto_calls.append((url, kwargs))
        if self.failure_at == self.index:
            raise TimeoutError("navigation timed out")
        self.url = self.final_urls[self.index] if self.index < len(self.final_urls) else url
        self._content = self.pages[min(self.index, len(self.pages) - 1)]
        return self.response


class AdpPhase4ProtocolTests(unittest.TestCase):
    def test_workforce_now_detail_timeout_protects_listed_inventory(self):
        row = wfn_item()
        result = adp.fetch_jobs(
            Session(gets=[identity(), wfn_page([row], total=1), requests.Timeout("detail timeout")]),
            wfn_target(protocol_request_budget=10),
        )
        self.assertEqual(result.jobs, [])
        self.assertEqual(len(result.inventory_items), 1)
        self.assertEqual(len(result.item_failures), 1)
        self.assertTrue(result.item_failures[0].retryable)
        self.assertTrue(result.scan["scan_complete"])
        self.assertEqual(result.scan["source_identity_status"], "validated")

    def test_workforce_now_budget_exhaustion_is_incomplete(self):
        row = wfn_item()
        result = adp.fetch_jobs(
            Session(gets=[identity(), wfn_page([row], total=1)]),
            wfn_target(protocol_request_budget=2),
        )
        self.assertFalse(result.scan["scan_complete"])
        self.assertEqual(result.scan["stop_reason"], "request_budget_exhausted")
        self.assertTrue(result.scan["safety_limit_reached"])
        self.assertEqual(len(result.inventory_items), 1)

    def test_workforce_now_total_mutation_discards_first_traversal(self):
        first = wfn_item(item_id="first_1", external_id="1001")
        changed = wfn_item(item_id="first_2", external_id="1002")
        stable = wfn_item(item_id="second_1", external_id="2001")
        session = Session(
            gets=[
                identity(), wfn_page([first], total=11), Response(first),
                wfn_page([changed], offset=10, total=10),
                identity(), wfn_page([stable], total=1), Response(stable),
            ]
        )
        result = adp.fetch_jobs(
            session,
            wfn_target(max_pages=2, max_jobs=20, protocol_request_budget=20),
        )
        self.assertEqual([job.raw_metadata["item_id"] for job in result.jobs], ["second_1"])
        self.assertEqual(result.scan["collection_restart_count"], 1)
        self.assertTrue(result.scan["stabilized_after_collection_retry"])

    def test_myjobs_later_transport_failure_remains_safe_partial_progress(self):
        rows = [myjobs_item(reqId=str(5000000000001 + index)) for index in range(20)]
        result = adp.fetch_jobs(
            Session(
                gets=[
                    Response(myjobs_config()),
                    Response({"count": 40, "jobRequisitions": rows}),
                    requests.Timeout("later page timeout"),
                ]
            ),
            myjobs_target(max_pages=2, max_jobs=40, protocol_request_budget=10),
        )
        self.assertEqual(len(result.jobs), 20)
        self.assertFalse(result.scan["scan_complete"])
        self.assertIn("partial_progress", result.capabilities_used)

    def test_identity_failure_cannot_become_authoritative_zero(self):
        with self.assertRaisesRegex(AdapterError, "tenant identity"):
            adp.fetch_jobs(Session(gets=[identity("Other")]), wfn_target())


class BrowserPhase4ProtocolTests(unittest.TestCase):
    def test_unexpected_redirect_fails_identity(self):
        with self.assertRaisesRegex(AdapterError, "unexpected redirect"):
            browser_rendered_html._validate_navigation(
                "https://jobs.example.test/jobs",
                "https://evil.test/jobs",
                browser_target(),
            )

    def test_reviewed_recruiting_host_redirect_is_accepted(self):
        browser_rendered_html._validate_navigation(
            "https://jobs.example.test/jobs",
            "https://recruit.example.test/openings",
            browser_target(allowed_source_hosts=["recruit.example.test"]),
        )

    def test_runtime_identity_and_ownership_are_explicit(self):
        page = Page()
        batch, _browser, _playwright = batch_for(page)
        with batch:
            result = batch.fetch_jobs(browser_target(single_page_collection=True))
        self.assertEqual(result.scan["source_identity_status"], "validated")
        self.assertEqual(result.scan["navigation_owner"], "browser_collector")
        self.assertEqual(result.scan["pagination_owner"], "static_html_adapter")
        self.assertEqual(result.scan["terminal_evidence_owner"], "static_html_adapter")

    def test_later_navigation_timeout_preserves_partial_discoveries(self):
        first = rendered_page(
            "<a class='job' href='/jobs/1'>One</a>"
            "<a class='next' href='/jobs?page=2'>Next</a>"
        )
        page = DynamicPage([first], failure_at=1)
        batch, _browser, _playwright = batch_for(page)
        configured = browser_target(
            max_pages=2,
            max_jobs=20,
            next_page_selector="a.next",
            pagination_contract="reviewed_native_pagination",
        )
        with batch:
            result = batch.fetch_jobs(configured)
        self.assertEqual(len(result.jobs), 1)
        self.assertFalse(result.scan["scan_complete"])
        self.assertEqual(result.scan["stop_reason"], "browser_transient_action_failure")
        self.assertIn("partial_progress", result.capabilities_used)

    def test_action_budget_exhaustion_is_not_terminal(self):
        page = Page()
        batch, _browser, _playwright = batch_for(page)
        with batch, self.assertRaisesRegex(AdapterError, "action budget exhausted"):
            batch.fetch_jobs(browser_target(browser_scroll_steps=1, protocol_request_budget=1))


class CrossCuttingPhase4Tests(unittest.TestCase):
    def test_identity_required_is_enforced_only_by_central_finalization(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="reported_total_reached",
            stop_reason="reported_total_reached",
            total_available=0,
            authoritative_total_present=True,
            identity_required=True,
        )
        finalize_source_scan(metadata, jobs_collected=0)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["source_health_code"], "identity_failure")

    def test_adp_and_browser_protocol_identity_fields_are_fingerprinted_not_budget(self):
        adp_target = wfn_target(protocol_request_budget=10)
        self.assertEqual(
            source_health_key(adp_target),
            source_health_key(CompanyTarget(**{**adp_target.__dict__, "protocol_request_budget": 11})),
        )
        self.assertNotEqual(
            source_health_key(adp_target),
            source_health_key(CompanyTarget(**{**adp_target.__dict__, "identity_regex": "Other"})),
        )
        rendered = browser_target(protocol_request_budget=10)
        self.assertEqual(
            source_health_key(rendered),
            source_health_key(CompanyTarget(**{**rendered.__dict__, "protocol_request_budget": 11})),
        )
        self.assertNotEqual(
            source_health_key(rendered),
            source_health_key(
                CompanyTarget(**{**rendered.__dict__, "browser_identity_regex": "Other"})
            ),
        )


if __name__ == "__main__":
    unittest.main()
