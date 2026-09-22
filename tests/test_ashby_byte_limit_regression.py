import json
import unittest

from src.scraper.company_careers.adapters import ashby
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)


class StreamingResponse:
    def __init__(self, body, *, status_code=200, chunk_size=71):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status_code
        self.chunk_size = chunk_size
        self.closed = False

    def iter_content(self, chunk_size):
        size = min(chunk_size, self.chunk_size)
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]

    def close(self):
        self.closed = True


class StreamingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected GET: {url}")
        return self.responses.pop(0)


def html_page(data):
    payload = json.dumps(data, separators=(",", ":"))
    return f"<!doctype html><script>window.__appData = {payload};</script>"


def summary(
    job_id,
    *,
    title=None,
    location="Remote, Canada",
    employment_type="FullTime",
    secondary=None,
):
    return {
        "id": job_id,
        "title": title or f"Role {job_id}",
        "teamId": "team-1",
        "locationId": "location-1",
        "locationName": location,
        "workplaceType": "Remote",
        "employmentType": employment_type,
        "secondaryLocations": secondary or [],
        "compensationTierSummary": None,
    }


def board(slug, jobs):
    return html_page(
        {
            "organization": {"hostedJobsPageSlug": slug, "name": "Example"},
            "posting": None,
            "jobBoard": {
                "teams": [
                    {
                        "id": "team-1",
                        "name": "Engineering",
                        "externalName": None,
                        "parentTeamId": None,
                    }
                ],
                "jobPostings": jobs,
            },
        }
    )


def detail(
    slug,
    job_id,
    *,
    title=None,
    location="Remote, Canada",
    description=None,
):
    return html_page(
        {
            "organization": {"hostedJobsPageSlug": slug, "name": "Example"},
            "posting": {
                "id": job_id,
                "title": title or f"Changed detail title {job_id}",
                "departmentName": "Engineering",
                "departmentExternalName": "Product Engineering",
                "locationName": location,
                "workplaceType": "Remote",
                "isListed": True,
                "isConfidential": False,
                "employmentType": "Intern",
                "descriptionHtml": description or f"<p>Description {job_id}</p>",
                "secondaryLocationNames": [],
                "teamNames": ["Engineering"],
            },
            "jobBoard": None,
        }
    )


class AshbyByteLimitRegressionTests(unittest.TestCase):
    def target(self, **overrides):
        values = {
            "name": "Example",
            "platform": "ashby",
            "slug": "example",
            "max_jobs": ashby.MAX_RESULTS,
            "max_response_bytes": 50_000,
            "allowed_apply_hosts": ["jobs.ashbyhq.com"],
        }
        values.update(overrides)
        return CompanyTarget(**values)

    def test_complete_collection_above_500_is_retained_with_bounded_enrichment(self):
        listings = [summary(f"id-{index:04}") for index in range(751)]
        session = StreamingSession(
            [StreamingResponse(board("example", listings))]
            + [
                StreamingResponse(detail("example", f"id-{index:04}"))
                for index in range(20)
            ]
        )

        jobs = ashby.fetch_jobs(
            session,
            self.target(max_response_bytes=1_000_000),
        )
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 751)
        self.assertEqual(len({job.job_id for job in jobs}), 751)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "complete_collection_response")
        self.assertEqual(
            session.ashby_pagination["detail_requests"],
            ashby.MAX_DETAIL_REQUESTS,
        )
        self.assertEqual(len(session.get_calls), 1 + ashby.MAX_DETAIL_REQUESTS)
        self.assertEqual(sum(job.description is not None for job in jobs), 20)

    def test_tiny_collection_bound_retains_partial_jobs_and_is_incomplete(self):
        listings = [summary(f"id-{index}") for index in range(3)]
        session = StreamingSession(
            [
                StreamingResponse(board("example", listings)),
                StreamingResponse(detail("example", "id-0")),
                StreamingResponse(detail("example", "id-1")),
            ]
        )

        jobs = ashby.fetch_jobs(session, self.target(max_jobs=2))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_results_reached")

    def test_collection_equal_to_safety_ceiling_is_not_declared_complete(self):
        listings = [summary(f"id-{index}") for index in range(2)]
        session = StreamingSession(
            [StreamingResponse(board("example", listings))]
            + [
                StreamingResponse(detail("example", f"id-{index}"))
                for index in range(2)
            ]
        )

        jobs = ashby.fetch_jobs(session, self.target(max_jobs=2))
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])

    def test_large_board_scans_compact_index_and_fetches_only_bounded_details(self):
        listings = [summary(f"id-{index:02}") for index in range(40)]
        listings[31] = summary(
            "id-31", title="Engineering Opportunity", employment_type="Intern"
        )
        listings[38] = summary(
            "id-38", title="Hardware Engineering Co-op", location="San Francisco, CA"
        )
        session = StreamingSession(
            [
                StreamingResponse(board("example", listings)),
                StreamingResponse(detail("example", "id-31", description="<p>First</p>")),
                StreamingResponse(detail("example", "id-38", description="<p>Second</p>")),
            ]
        )

        jobs = ashby.fetch_jobs(session, self.target(max_jobs=2))

        self.assertEqual([job.job_id for job in jobs], ["id-31", "id-38"])
        self.assertEqual(jobs[0].title, "Engineering Opportunity")
        self.assertEqual(jobs[0].url, "https://jobs.ashbyhq.com/example/id-31")
        self.assertEqual(jobs[0].source, "ashby:example")
        self.assertEqual(jobs[0].description, "<p>First</p>")
        self.assertEqual(jobs[0].departments, ["Product Engineering"])
        self.assertEqual(
            [call[0] for call in session.get_calls],
            [
                "https://jobs.ashbyhq.com/example",
                "https://jobs.ashbyhq.com/example/id-31",
                "https://jobs.ashbyhq.com/example/id-38",
            ],
        )
        self.assertTrue(all(call[1]["stream"] for call in session.get_calls))
        self.assertEqual(session.ashby_pagination["jobs_scanned"], 40)
        self.assertEqual(session.ashby_pagination["candidate_jobs"], 2)
        self.assertEqual(session.ashby_pagination["detail_requests"], 2)
        self.assertEqual(
            session.ashby_pagination["transport"], "hosted_compact_index"
        )
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["total_available"], 40)

    def test_small_board_enriches_every_job_and_preserves_listing_identity(self):
        listings = [
            summary(
                "stable-1",
                title="Original Intern title",
                location="London, UK",
                secondary=[
                    {"locationId": "loc-2", "locationName": "Toronto, Canada"}
                ],
            )
        ]
        session = StreamingSession(
            [
                StreamingResponse(board("example", listings)),
                StreamingResponse(detail("example", "stable-1")),
            ]
        )

        jobs = ashby.fetch_jobs(session, self.target())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "stable-1")
        self.assertEqual(jobs[0].title, "Original Intern title")
        self.assertEqual(
            jobs[0].location, "London, UK | Toronto, Canada"
        )
        self.assertEqual(jobs[0].url, "https://jobs.ashbyhq.com/example/stable-1")
        self.assertEqual(
            session.ashby_pagination["stop_reason"],
            "complete_collection_response",
        )
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "complete_collection_response")
        self.assertEqual(metadata["pages_requested"], 1)
        self.assertEqual(metadata["pages_completed"], 1)

    def test_valid_empty_board_is_complete(self):
        session = StreamingSession([StreamingResponse(board("example", []))])

        self.assertEqual(ashby.fetch_jobs(session, self.target()), [])

        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 0)
        self.assertEqual(metadata["stop_reason"], "complete_collection_response")

    def test_missing_board_is_failed_not_complete_zero(self):
        session = StreamingSession([StreamingResponse("", status_code=404)])

        with self.assertRaisesRegex(AdapterError, "HTTP 404"):
            ashby.fetch_jobs(session, self.target())

        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["pages_requested"], 1)
        self.assertEqual(metadata["pages_completed"], 0)
        self.assertEqual(metadata["stop_reason"], "adapter_failure")

    def test_entire_index_is_validated_before_any_detail_fetch(self):
        listings = [
            summary("student-1", title="Software Intern"),
            summary("student-2", title="Hardware Intern"),
            {"id": "broken", "title": ""},
        ]
        session = StreamingSession([StreamingResponse(board("example", listings))])

        with self.assertRaisesRegex(AdapterError, "missing a stable ID or title"):
            ashby.fetch_jobs(session, self.target(max_jobs=1))

        self.assertEqual(len(session.get_calls), 1)

    def test_oversized_index_stops_streaming_and_fails_closed(self):
        response = StreamingResponse(b"x" * 201, chunk_size=25)
        session = StreamingSession([response])

        with self.assertRaisesRegex(AdapterError, "response exceeded the byte limit"):
            ashby.fetch_jobs(session, self.target(max_response_bytes=200))

        self.assertTrue(response.closed)
        self.assertEqual(len(session.get_calls), 1)

    def test_oversized_detail_stops_streaming_and_fails_closed(self):
        listing = StreamingResponse(board("example", [summary("id-1")]))
        oversized = StreamingResponse(b"x" * 1001, chunk_size=31)
        session = StreamingSession([listing, oversized])

        with self.assertRaisesRegex(
            AdapterError, "detail response exceeded the byte limit"
        ):
            ashby.fetch_jobs(session, self.target(max_response_bytes=1000))

        self.assertTrue(oversized.closed)
        self.assertEqual(len(session.get_calls), 2)

    def test_malformed_index_and_detail_fail_closed(self):
        with self.subTest("index"):
            session = StreamingSession(
                [StreamingResponse("<script>window.__appData = {;</script>")]
            )
            with self.assertRaisesRegex(AdapterError, "malformed application data"):
                ashby.fetch_jobs(session, self.target())

        with self.subTest("detail"):
            session = StreamingSession(
                [
                    StreamingResponse(board("example", [summary("id-1")])),
                    StreamingResponse(html_page({"organization": {"hostedJobsPageSlug": "example"}})),
                ]
            )
            with self.assertRaisesRegex(AdapterError, "missing its posting"):
                ashby.fetch_jobs(session, self.target())

    def test_cross_board_and_mismatched_detail_identity_fail_closed(self):
        with self.subTest("board"):
            session = StreamingSession(
                [StreamingResponse(board("different", [summary("id-1")]))]
            )
            with self.assertRaisesRegex(AdapterError, "requested board"):
                ashby.fetch_jobs(session, self.target())

        with self.subTest("detail"):
            session = StreamingSession(
                [
                    StreamingResponse(board("example", [summary("id-1")])),
                    StreamingResponse(detail("example", "id-2")),
                ]
            )
            with self.assertRaisesRegex(AdapterError, "stable job ID"):
                ashby.fetch_jobs(session, self.target())

    def test_duplicate_stable_ids_fail_before_detail_fetch(self):
        session = StreamingSession(
            [StreamingResponse(board("example", [summary("same"), summary("same")]))]
        )

        with self.assertRaisesRegex(AdapterError, "duplicate stable job IDs"):
            ashby.fetch_jobs(session, self.target())

        self.assertEqual(len(session.get_calls), 1)


if __name__ == "__main__":
    unittest.main()
