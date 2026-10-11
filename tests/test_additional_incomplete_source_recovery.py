from __future__ import annotations

import unittest
from unittest.mock import patch

from src.scraper.company_careers.adapters import static_html, talentbrew
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion
from src.scraper.company_careers.models import (
    CompanyTarget,
    SourceScanMetadata,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_health import finalize_source_scan


class Response:
    def __init__(self, body: str, *, url: str) -> None:
        self.text = body
        self.content = body.encode("utf-8")
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.history = []


class Session:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected GET {url}")
        response = self.responses.pop(0)
        response.url = url
        return response


def static_target(**overrides) -> CompanyTarget:
    values = dict(
        name="Example",
        platform="static_html",
        listing_url="https://jobs.example.test/jobs",
        endpoint_variant="server_rendered_cards",
        listing_selector="main.listing",
        job_card_selector="article.job",
        title_selector="a.title",
        detail_link_selector="a.title",
        external_id_attribute="data-id",
        location_selector="span.location",
        allowed_apply_hosts=["jobs.example.test"],
        pagination_url_template="https://jobs.example.test/jobs?page={page}",
        max_pages=1,
        max_jobs=20,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def static_page(*job_ids: str, terminal: bool = False) -> str:
    cards = "".join(
        "<article class='job' data-id='{0}'><a class='title' "
        "href='/jobs/{0}'>Engineer {0}</a><span class='location'>Calgary</span>"
        "</article>".format(job_id)
        for job_id in job_ids
    )
    pagination = (
        "<nav class='pagination'><a class='next disabled' aria-disabled='true' "
        "href='?page=2'>Next</a></nav>"
        if terminal
        else "<nav class='pagination'><a class='next' href='?page=2'>Next</a></nav>"
    )
    return f"<html><body><main class='listing'>{cards}</main>{pagination}</body></html>"


def talentbrew_page(company: str, start: int, count: int, *, next_href: str | None) -> str:
    cards = "".join(
        "<a data-job-id='{0}' href='/job/engineer/1/{0}'><h2>Engineer {0}</h2>"
        "<span class='job-location'>Calgary</span></a>".format(start + index)
        for index in range(count)
    )
    next_link = f"<a class='next' href='{next_href}'>Next</a>" if next_href else ""
    return (
        f"<html><title>{company} careers</title><section id='search-results'>"
        f"{cards}</section><nav class='pagination'>{next_link}</nav></html>"
    )


class AdditionalIncompleteSourceRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    @patch.object(static_html.time, "sleep", return_value=None)
    def test_applied_materials_can_reach_explicit_terminal_beyond_normal_page_budget(
        self, _sleep
    ):
        target = self.targets["Applied Materials"]
        self.assertEqual(target.max_response_bytes, 4_000_000)
        first = (
            "<html><body><li class='search-results-list__item'><h2 "
            "class='search-results-list__job-title'><a href='/en/job/engineer/101'>"
            "Engineer 101</a></h2><span class='search-results-list__location'>"
            "Calgary</span></li><a class='next' href='?p=2'>Next</a></body></html>"
        )
        final = (
            "<html><body><li class='search-results-list__item'><h2 "
            "class='search-results-list__job-title'><a href='/en/job/engineer/102'>"
            "Engineer 102</a></h2><span class='search-results-list__location'>"
            "Edmonton</span></li><a class='next disabled' aria-disabled='true' "
            "href='?p=3'>Next</a></body></html>"
        )
        session = Session(
            Response(first, url=str(target.listing_url)),
            Response(final, url=str(target.listing_url)),
        )

        jobs = static_html.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertGreater(metadata["pages_completed"], target.max_pages)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertTrue(metadata["disabled_next_control"])

    @patch.object(talentbrew.time, "sleep", return_value=None)
    def test_palo_alto_reaches_reviewed_next_link_exhaustion(self, _sleep):
        target = self.targets["Palo Alto Networks"]
        session = Session(
            Response(
                talentbrew_page(target.name, 100, 1, next_href="/page/2"),
                url=str(target.listing_url),
            ),
            Response(
                talentbrew_page(target.name, 200, 1, next_href=None),
                url=str(target.listing_url),
            ),
        )

        jobs = talentbrew.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertGreater(metadata["pages_completed"], target.max_pages)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "no_next_page")

    @patch.object(talentbrew.time, "sleep", return_value=None)
    def test_rakuten_can_cross_normal_result_budget_only_until_exhaustion(self, _sleep):
        target = self.targets["Rakuten"]
        session = Session(
            Response(
                talentbrew_page(target.name, 100, 10, next_href="/page/2"),
                url=str(target.listing_url),
            ),
            Response(
                talentbrew_page(target.name, 200, 10, next_href="/page/3"),
                url=str(target.listing_url),
            ),
            Response(
                talentbrew_page(target.name, 300, 5, next_href=None),
                url=str(target.listing_url),
            ),
        )

        jobs = talentbrew.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 25)
        self.assertGreater(len(jobs), target.max_jobs)
        self.assertGreater(metadata["pages_completed"], target.max_pages)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "no_next_page")

    def test_unresolved_static_source_remains_without_terminal_evidence(self):
        intuitive = self.targets["Intuitive Surgical"]
        coupang = self.targets["Coupang"]
        self.assertEqual(intuitive.platform, "smartrecruiters")
        self.assertEqual((intuitive.slug, intuitive.max_pages, intuitive.max_jobs), ("intuitive", 8, 800))
        self.assertIsNotNone(coupang.pagination_url_template)
        self.assertFalse(coupang.single_page_collection)

    def test_source_health_reconciles_only_independently_proven_terminal_state(self):
        proven = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="explicit_terminal_page",
            safety_limit_reached=True,
            terminal_evidence_type="html_pagination_contract",
            terminal_page_observed=True,
            terminal_page_fully_processed=True,
            has_more=False,
        )
        finalize_source_scan(proven, jobs_collected=25)
        self.assertTrue(proven["scan_complete"])
        self.assertFalse(proven["safety_limit_reached"])

        unproven = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="maximum_results_reached",
            safety_limit_reached=True,
        )
        finalize_source_scan(unproven, jobs_collected=25)
        self.assertFalse(unproven["scan_complete"])
        self.assertTrue(unproven["safety_limit_reached"])

        partial = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="explicit_terminal_page",
            safety_limit_reached=True,
            terminal_evidence_type="html_pagination_contract",
            terminal_page_observed=True,
            terminal_page_fully_processed=False,
            has_more=False,
        )
        finalize_source_scan(partial, jobs_collected=25)
        self.assertFalse(partial["scan_complete"])

        malformed = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="explicit_terminal_page",
            safety_limit_reached=True,
            terminal_evidence_type="html_pagination_contract",
            terminal_page_observed=True,
            terminal_page_fully_processed=True,
            has_more=False,
            malformed_records=1,
        )
        finalize_source_scan(malformed, jobs_collected=25)
        self.assertFalse(malformed["scan_complete"])
        self.assertEqual(malformed["source_health_code"], "malformed_records")

        mutation = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="explicit_terminal_page",
            safety_limit_reached=True,
            terminal_evidence_type="html_pagination_contract",
            terminal_page_observed=True,
            terminal_page_fully_processed=True,
            has_more=False,
            collection_changed_during_scan=True,
        )
        finalize_source_scan(mutation, jobs_collected=25)
        self.assertFalse(mutation["scan_complete"])
        self.assertEqual(mutation["source_health_code"], "collection_changed")

        continuing = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="explicit_terminal_page",
            safety_limit_reached=True,
            terminal_evidence_type="html_pagination_contract",
            terminal_page_observed=True,
            terminal_page_fully_processed=True,
            has_more=False,
            next_page=26,
        )
        finalize_source_scan(continuing, jobs_collected=25)
        self.assertFalse(continuing["scan_complete"])
        self.assertEqual(continuing["source_health_code"], "terminal_contradiction")

        repeated = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="pagination_failure",
            stop_reason="repeated_stable_ids",
            repeated_stable_ids=True,
        )
        finalize_source_scan(repeated, jobs_collected=25)
        self.assertFalse(repeated["scan_complete"])
        self.assertEqual(repeated["source_health_code"], "pagination_loop")

    def test_static_terminal_page_truncated_by_hard_job_ceiling_is_incomplete(self):
        real_policy = AdaptiveListingCompletion
        session = Session(
            Response(
                static_page("1", "2", terminal=True),
                url="https://jobs.example.test/jobs?page=1",
            )
        )
        with patch.object(
            static_html,
            "AdaptiveListingCompletion",
            side_effect=lambda pages, jobs: real_policy(
                pages, jobs, hard_job_ceiling=1
            ),
        ):
            jobs = static_html.fetch_jobs(session, static_target(max_jobs=1))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_results_reached")

    def test_static_terminal_page_with_malformed_record_is_incomplete(self):
        valid = (
            "<article class='job' data-id='1'><a class='title' href='/jobs/1'>"
            "Engineer 1</a><span class='location'>Calgary</span>"
            "<span class='brand'>Example</span></article>"
        )
        malformed = (
            "<article class='job' data-id='2'><a class='title' href='/jobs/2'>"
            "Engineer 2</a><span class='location'>Calgary</span>"
            "<span class='brand'>Other</span></article>"
        )
        body = static_page(terminal=True).replace(
            "</main>", f"{valid}{malformed}</main>"
        )
        session = Session(
            Response(body, url="https://jobs.example.test/jobs?page=1")
        )

        jobs = static_html.fetch_jobs(
            session,
            static_target(
                job_attribution_selector="span.brand",
                job_attribution_regex="Example",
            ),
        )
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["malformed_records"], 1)
        self.assertEqual(metadata["stop_reason"], "malformed_listing_records")

    @patch.object(static_html.time, "sleep", return_value=None)
    def test_static_cross_page_repeated_id_cannot_be_terminal(self, _sleep):
        session = Session(
            Response(
                static_page("1"),
                url="https://jobs.example.test/jobs?page=1",
            ),
            Response(
                static_page("1", "2", terminal=True),
                url="https://jobs.example.test/jobs?page=2",
            ),
        )

        jobs = static_html.fetch_jobs(session, static_target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["repeated_stable_ids"])
        self.assertEqual(metadata["stop_reason"], "repeated_stable_ids")

    def test_talentbrew_terminal_page_truncated_by_hard_job_ceiling_is_incomplete(self):
        real_policy = AdaptiveListingCompletion
        target = CompanyTarget.from_dict(
            {
                "name": "Example Networks",
                "platform": "talentbrew",
                "listing_url": "https://jobs.example.test/search-jobs/",
                "endpoint_variant": talentbrew.ENDPOINT_VARIANT,
                "identity_regex": "Example Networks",
                "allowed_apply_hosts": ["jobs.example.test"],
                "max_pages": 1,
                "max_jobs": 1,
            }
        )
        session = Session(
            Response(
                talentbrew_page(target.name, 100, 2, next_href=None),
                url=str(target.listing_url),
            )
        )
        with patch.object(
            talentbrew,
            "AdaptiveListingCompletion",
            side_effect=lambda pages, jobs: real_policy(
                pages, jobs, hard_job_ceiling=1
            ),
        ):
            jobs = talentbrew.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_results_reached")

    @patch.object(talentbrew.time, "sleep", return_value=None)
    def test_talentbrew_cross_page_repeated_id_cannot_be_terminal(self, _sleep):
        target = CompanyTarget.from_dict(
            {
                "name": "Example Networks",
                "platform": "talentbrew",
                "listing_url": "https://jobs.example.test/search-jobs/",
                "endpoint_variant": talentbrew.ENDPOINT_VARIANT,
                "identity_regex": "Example Networks",
                "allowed_apply_hosts": ["jobs.example.test"],
                "max_pages": 1,
                "max_jobs": 20,
            }
        )
        session = Session(
            Response(
                talentbrew_page(target.name, 100, 1, next_href="/page/2"),
                url=str(target.listing_url),
            ),
            Response(
                talentbrew_page(target.name, 100, 2, next_href=None),
                url="https://jobs.example.test/page/2",
            ),
        )

        jobs = talentbrew.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["repeated_stable_ids"])
        self.assertEqual(metadata["stop_reason"], "repeated_stable_ids")


if __name__ == "__main__":
    unittest.main()
