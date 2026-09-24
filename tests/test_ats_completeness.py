from __future__ import annotations

import unittest

from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, *responses, get_responses=()):
        self.responses = list(responses)
        self.get_responses = list(get_responses)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected POST: {url}")
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.get_responses:
            raise AssertionError(f"Unexpected GET: {url}")
        return self.get_responses.pop(0)


def target(**overrides):
    values = {
        "name": "Example",
        "platform": "workday",
        "tenant": "example",
        "wd_subdomain": "wd1",
        "site_id": "External",
        "max_pages": 5,
    }
    values.update(overrides)
    return CompanyTarget(**values)


def postings(start, count):
    return [
        {
            "title": f"Role {index}",
            "locationsText": "Remote",
            "externalPath": f"/job/Remote/Role_R{10000 + index}",
            "bulletFields": [f"R{10000 + index}"],
        }
        for index in range(start, start + count)
    ]


class WorkdayCompletenessTests(unittest.TestCase):
    def test_listing_only_source_above_one_thousand_completes_under_hard_ceiling(
        self
    ):
        total = 1_020
        session = Session(
            *[
                Response(
                    {
                        "total": total,
                        "jobPostings": postings(
                            offset, min(workday.PAGE_SIZE, total - offset)
                        ),
                    }
                )
                for offset in range(0, total, workday.PAGE_SIZE)
            ]
        )
        jobs = workday.fetch_jobs(session, target(max_pages=1))
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), total)
        self.assertEqual(metadata["pages_requested"], 51)
        self.assertTrue(metadata["listing_only_second_stage_eligible"])
        self.assertTrue(metadata["scan_complete"])

    def test_stable_total_above_one_hundred_completes(self):
        total = 125
        session = Session(
            *[
                Response(
                    {
                        "total": total,
                        "jobPostings": postings(
                            offset, min(workday.PAGE_SIZE, total - offset)
                        ),
                    }
                )
                for offset in range(0, total, workday.PAGE_SIZE)
            ]
        )

        jobs = workday.fetch_jobs(
            session, target(max_pages=workday.MAX_PAGES)
        )
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), total)
        self.assertEqual(len({job.job_id for job in jobs}), total)
        self.assertEqual(metadata["pages_requested"], 7)
        self.assertEqual(metadata["pages_completed"], 7)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "reported_total_reached")

    def test_reported_total_completes_multi_page_enumeration(self):
        session = Session(
            Response({"total": 25, "jobPostings": postings(0, 20)}),
            Response({"total": 25, "jobPostings": postings(20, 5)}),
        )

        jobs = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 25)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "reported_total_reached")
        self.assertEqual(metadata["total_available"], 25)
        self.assertEqual(metadata["pages_requested"], 2)
        self.assertEqual(metadata["pages_completed"], 2)
        self.assertEqual(session.post_calls[1][1]["json"]["offset"], 20)

    def test_short_page_without_total_is_natural_terminal_evidence(self):
        session = Session(
            Response({"jobPostings": postings(0, 20)}),
            Response({"jobPostings": []}),
        )

        jobs = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 20)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "natural_terminal_page")
        self.assertEqual(metadata["total_available"], 20)
        self.assertEqual(metadata["pages_completed"], 2)

    def test_later_zero_total_sentinel_preserves_first_page_total(self):
        session = Session(
            Response({"total": 25, "jobPostings": postings(0, 20)}),
            Response({"total": 0, "jobPostings": postings(20, 5)}),
        )

        jobs = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 25)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 25)
        self.assertEqual(metadata["stop_reason"], "reported_total_reached")

    def test_one_page_beyond_normal_budget_completes_adaptively(self):
        session = Session(
            Response({"total": 25, "jobPostings": postings(0, 20)}),
            Response({"total": 25, "jobPostings": postings(20, 5)}),
        )

        jobs = workday.fetch_jobs(session, target(max_pages=1))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 25)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["pages_requested"], 2)
        self.assertEqual(metadata["normal_listing_request_budget"], 1)
        self.assertEqual(metadata["effective_listing_request_ceiling"], 2)
        self.assertEqual(session.get_calls, [])

    def test_source_beyond_adaptive_allowance_remains_incomplete(self):
        session = Session(
            Response({"total": 20_001, "jobPostings": postings(0, 20)})
        )
        jobs = workday.fetch_jobs(session, target(max_pages=1))
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 20)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_pages_reached")

    def test_page_budget_above_hard_emergency_ceiling_is_rejected(self):
        session = Session()

        with self.assertRaisesRegex(AdapterError, "between 1 and 25"):
            workday.fetch_jobs(session, target(max_pages=workday.MAX_PAGES + 1))

        self.assertEqual(session.post_calls, [])

    def test_changing_total_retains_jobs_as_incomplete(self):
        session = Session(
            Response({"total": 25, "jobPostings": postings(0, 20)}),
            Response({"total": 26, "jobPostings": postings(20, 6)}),
        )

        jobs = workday.fetch_jobs(session, target(max_pages=1))
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 26)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "collection_changed_during_scan",
        )
        self.assertEqual(metadata["initial_total_available"], 25)
        self.assertEqual(metadata["minimum_total_observed"], 25)
        self.assertEqual(metadata["maximum_total_observed"], 26)
        self.assertEqual(metadata["final_total_observed"], 26)
        self.assertTrue(metadata["adaptive_completion_eligible"])
        self.assertNotEqual(
            metadata["scan_completeness_reason"], "reported_total_reached"
        )

    def test_failure_during_pagination_is_not_a_terminal_page(self):
        session = Session(
            Response({"total": 25, "jobPostings": postings(0, 20)}),
            Response({}, status_code=500),
        )

        with self.assertRaisesRegex(AdapterError, "HTTP 500"):
            workday.fetch_jobs(session, target())

        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["pages_requested"], 2)
        self.assertEqual(metadata["pages_completed"], 1)
        self.assertEqual(metadata["stop_reason"], "http_error")

    def test_repeated_page_is_detected(self):
        first_page = postings(0, 20)
        session = Session(
            Response({"jobPostings": first_page}),
            Response({"jobPostings": first_page}),
        )

        with self.assertRaisesRegex(AdapterError, "repeated"):
            workday.fetch_jobs(session, target())

        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "repeated_page")

    def test_long_pagination_deduplicates_overlaps_and_keeps_advancing(self):
        pages = [postings(0, 20)]
        pages.extend(postings(19 * page, 20) for page in range(1, 7))
        pages.append(postings(133, 15))
        session = Session(
            *[
                Response({"total": 155, "jobPostings": page})
                for page in pages
            ]
        )

        jobs = workday.fetch_jobs(session, target(max_pages=8))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 148)
        self.assertEqual(len({job.job_id for job in jobs}), 148)
        self.assertEqual(metadata["overlapping_jobs_skipped"], 7)
        self.assertEqual(metadata["pages_requested"], 8)
        self.assertEqual(metadata["pages_completed"], 8)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "collection_changed_during_scan",
        )

    def test_stable_total_premature_terminal_is_conservative_incomplete(self):
        session = Session(
            Response({"total": 30, "jobPostings": postings(0, 20)}),
            Response({"total": 30, "jobPostings": postings(20, 5)}),
        )

        jobs = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 25)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "premature_terminal_page")

    def test_changing_total_plus_short_terminal_is_mutable_incomplete(self):
        session = Session(
            Response({"total": 30, "jobPostings": postings(0, 20)}),
            Response({"total": 28, "jobPostings": postings(20, 5)}),
        )

        jobs = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 25)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "collection_changed_during_scan",
        )

    def test_reordered_complete_page_is_still_non_advancing(self):
        first_page = postings(0, 20)
        session = Session(
            Response({"jobPostings": first_page}),
            Response({"jobPostings": list(reversed(first_page))}),
        )

        with self.assertRaisesRegex(AdapterError, "repeated a complete page"):
            workday.fetch_jobs(session, target())

        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "repeated_page")

    def test_different_page_with_no_new_identities_is_still_non_advancing(self):
        first_page = postings(0, 20)
        second_page = first_page[:19] + [first_page[0]]
        session = Session(
            Response({"jobPostings": first_page}),
            Response({"jobPostings": second_page}),
        )

        with self.assertRaisesRegex(AdapterError, "no new stable jobs"):
            workday.fetch_jobs(session, target())

        self.assertEqual(source_scan_metadata(session)["stop_reason"], "repeated_page")

    def test_reported_zero_is_complete_but_http_error_is_not(self):
        complete_session = Session(Response({"total": 0, "jobPostings": []}))
        self.assertEqual(workday.fetch_jobs(complete_session, target()), [])
        complete = source_scan_metadata(complete_session)
        self.assertTrue(complete["scan_complete"])
        self.assertEqual(complete["total_available"], 0)

        failed_session = Session(
            Response({}, status_code=404),
            Response({}, status_code=404),
            Response({}, status_code=404),
            get_responses=(Response({}, status_code=404),),
        )
        with self.assertRaisesRegex(AdapterError, "HTTP 404"):
            workday.fetch_jobs(failed_session, target())
        self.assertFalse(source_scan_metadata(failed_session)["scan_complete"])


if __name__ == "__main__":
    unittest.main()
