import json
import unittest

from src.scraper.company_careers.adapters import greenhouse
from src.scraper.company_careers.models import AdapterError, CompanyTarget


class StreamingResponse:
    def __init__(self, payload=None, *, body=None, status_code=200, chunk_size=71):
        if body is None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.body = body
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


def summary(job_id, *, title=None, url=None, location="Remote", metadata=None):
    return {
        "id": job_id,
        "title": title or f"Role {job_id}",
        "location": {"name": location},
        "absolute_url": url or f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "updated_at": "2026-08-13T12:00:00Z",
        "metadata": metadata,
    }


def detail(job_id, *, department="Engineering", content=None):
    return {
        "id": job_id,
        "title": f"Changed detail title {job_id}",
        "absolute_url": f"https://different.example/jobs/{job_id}",
        "departments": [{"name": department}],
        "offices": [{"name": "Remote Office"}],
        "content": content or f"Description {job_id}",
    }


class GreenhouseByteLimitRegressionTests(unittest.TestCase):
    def target(self, **overrides):
        values = {
            "name": "Acme",
            "platform": "greenhouse",
            "slug": "acme",
            "max_jobs": 20,
            "max_response_bytes": 50_000,
        }
        values.update(overrides)
        return CompanyTarget(**values)

    def test_large_board_uses_compact_listing_and_only_bounded_details(self):
        listings = [summary(index) for index in range(1, 41)]
        listings[24]["title"] = "Software Engineer Intern (Winter 2027)"
        listings[38]["title"] = "Hardware Engineering Co-op"
        session = StreamingSession(
            [
                StreamingResponse({"jobs": listings, "meta": {"total": 40}}),
                StreamingResponse(detail(25, content="First description")),
                StreamingResponse(detail(39, content="Second description")),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, self.target(max_jobs=2))

        self.assertEqual([job.job_id for job in jobs], ["25", "39"])
        self.assertEqual(jobs[0].title, "Software Engineer Intern (Winter 2027)")
        self.assertEqual(jobs[0].url, "https://boards.greenhouse.io/acme/jobs/25")
        self.assertEqual(jobs[0].source, "greenhouse:acme")
        self.assertEqual(jobs[0].description, "First description")
        self.assertEqual(
            [call[0] for call in session.get_calls],
            [
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs/25",
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs/39",
            ],
        )
        self.assertNotIn("content=true", session.get_calls[0][0])
        self.assertEqual(session.greenhouse_pagination["jobs_returned"], 40)
        self.assertEqual(session.greenhouse_pagination["jobs_scanned"], 40)
        self.assertEqual(session.greenhouse_pagination["candidate_jobs"], 2)
        self.assertEqual(session.greenhouse_pagination["detail_requests"], 2)
        self.assertEqual(session.greenhouse_pagination["stop_reason"], "maximum_results_reached")

    def test_small_board_is_enriched_and_preserves_listing_identity(self):
        original_url = "http://company.example/careers?gh_jid=901"
        session = StreamingSession(
            [
                StreamingResponse({"jobs": [summary(901, title="Original Intern title", url=original_url)]}),
                StreamingResponse(detail(901)),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, self.target())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "901")
        self.assertEqual(jobs[0].title, "Original Intern title")
        self.assertEqual(jobs[0].url, "https://company.example/careers?gh_jid=901")
        self.assertEqual(jobs[0].departments, ["Engineering"])
        self.assertEqual(jobs[0].offices, ["Remote Office"])
        self.assertEqual(session.greenhouse_pagination["stop_reason"], "bounded_collection")

    def test_department_index_preselects_late_jobs_before_detail_fetches(self):
        listings = [summary(index) for index in range(1, 31)]
        listings[24]["title"] = "Software Intern (Summer 2027)"
        departments = {
            "departments": [
                {"id": 1, "name": "Engineering", "jobs": listings[:20]},
                {"id": 2, "name": "2027 Internships", "jobs": [listings[24], listings[28]]},
            ]
        }
        session = StreamingSession(
            [
                StreamingResponse({"jobs": listings}),
                StreamingResponse(departments),
                StreamingResponse(detail(25, department="2027 Internships")),
            ]
        )

        jobs = greenhouse.fetch_jobs(
            session,
            self.target(max_jobs=1, department_allowlist=["2027 Internships"]),
        )

        self.assertEqual([job.job_id for job in jobs], ["25"])
        self.assertEqual(len(session.get_calls), 3)
        self.assertTrue(session.get_calls[1][0].endswith("/departments"))
        self.assertTrue(session.get_calls[2][0].endswith("/jobs/25"))
        self.assertEqual(session.greenhouse_pagination["department_requests"], 1)
        self.assertEqual(session.greenhouse_pagination["detail_requests"], 1)

    def test_in_scope_candidates_are_selected_before_earlier_out_of_scope_candidates(self):
        listings = [
            summary(index, title=f"Engineering Intern {index}", location="Auckland, NZ")
            for index in range(1, 26)
        ]
        listings.extend(
            [
                summary(26, title="Manufacturing Engineering Intern", location="Long Beach, CA"),
                summary(27, title="Software Engineering Intern", location="Toronto, Canada"),
            ]
        )
        session = StreamingSession(
            [
                StreamingResponse({"jobs": listings}),
                StreamingResponse(detail(26)),
                StreamingResponse(detail(27)),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, self.target(max_jobs=2))

        self.assertEqual([job.job_id for job in jobs], ["26", "27"])
        self.assertEqual(session.greenhouse_pagination["candidate_jobs"], 27)
        self.assertEqual(session.greenhouse_pagination["detail_requests"], 2)

    def test_listing_metadata_can_identify_a_late_candidate(self):
        listings = [summary(index) for index in range(1, 24)]
        listings.append(
            summary(
                24,
                title="Engineering Opportunity",
                metadata=[{"id": 7, "name": "Job Type", "value": "Internship"}],
            )
        )
        session = StreamingSession(
            [StreamingResponse({"jobs": listings}), StreamingResponse(detail(24))]
        )

        jobs = greenhouse.fetch_jobs(session, self.target(max_jobs=1))

        self.assertEqual([job.job_id for job in jobs], ["24"])
        self.assertEqual(
            jobs[0].raw_metadata["greenhouse_metadata"]["Job Type"], "Internship"
        )

    def test_explicit_intern_titles_precede_weak_temporary_candidates(self):
        listings = [
            summary(
                index,
                title=f"Operations Associate - Temporary {index}",
                location="Hawthorne, CA",
                metadata=[{"name": "Job Type", "value": "Temporary"}],
            )
            for index in range(1, 26)
        ]
        listings.extend(
            [
                summary(26, title="Spring Software Engineering Internship"),
                summary(27, title="Summer Hardware Engineering Co-op"),
            ]
        )
        session = StreamingSession(
            [
                StreamingResponse({"jobs": listings}),
                StreamingResponse(detail(26)),
                StreamingResponse(detail(27)),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, self.target(max_jobs=2))

        self.assertEqual([job.job_id for job in jobs], ["26", "27"])
        self.assertEqual(session.greenhouse_pagination["candidate_jobs"], 27)

    def test_entire_listing_is_validated_even_after_enough_candidates_are_found(self):
        listings = [
            summary(1, title="Software Intern"),
            summary(2, title="Hardware Intern"),
            {"id": 3, "title": ""},
        ]
        session = StreamingSession([StreamingResponse({"jobs": listings})])

        with self.assertRaisesRegex(AdapterError, "missing a stable ID or title"):
            greenhouse.fetch_jobs(session, self.target(max_jobs=1))

        self.assertEqual(len(session.get_calls), 1)

    def test_oversized_listing_stops_streaming_and_fails_closed(self):
        response = StreamingResponse(body=b"x" * 201, chunk_size=25)
        session = StreamingSession([response])

        with self.assertRaisesRegex(AdapterError, "response exceeded the byte limit"):
            greenhouse.fetch_jobs(session, self.target(max_response_bytes=200))

        self.assertTrue(response.closed)
        self.assertEqual(len(session.get_calls), 1)

    def test_oversized_detail_fails_closed(self):
        listing = StreamingResponse({"jobs": [summary(1, title="Software Intern")]})
        detail_response = StreamingResponse(body=b"x" * 301, chunk_size=31)
        session = StreamingSession([listing, detail_response])

        with self.assertRaisesRegex(AdapterError, "detail response exceeded the byte limit"):
            greenhouse.fetch_jobs(session, self.target(max_response_bytes=300))

        self.assertTrue(detail_response.closed)
        self.assertEqual(len(session.get_calls), 2)

    def test_malformed_listing_and_detail_fail_closed(self):
        with self.subTest("listing"):
            session = StreamingSession([StreamingResponse(body=b"{")])
            with self.assertRaisesRegex(AdapterError, "response is malformed JSON"):
                greenhouse.fetch_jobs(session, self.target())

        with self.subTest("detail"):
            session = StreamingSession(
                [
                    StreamingResponse({"jobs": [summary(1, title="Software Intern")]}),
                    StreamingResponse([]),
                ]
            )
            with self.assertRaisesRegex(AdapterError, "detail must be a JSON object"):
                greenhouse.fetch_jobs(session, self.target())

    def test_mismatched_detail_id_fails_closed(self):
        session = StreamingSession(
            [
                StreamingResponse({"jobs": [summary(1, title="Software Intern")]}),
                StreamingResponse(detail(2)),
            ]
        )

        with self.assertRaisesRegex(AdapterError, "does not match its stable job ID"):
            greenhouse.fetch_jobs(session, self.target())

    def test_large_board_without_student_roles_uses_bounded_backfill(self):
        listings = [
            summary(index, title=f"Software Engineer {index}")
            for index in range(1, 25)
        ]
        session = StreamingSession([
            StreamingResponse({"jobs": listings}),
            StreamingResponse(detail(1)),
            StreamingResponse(detail(2)),
        ])

        jobs = greenhouse.fetch_jobs(session, self.target(max_jobs=2))

        self.assertEqual([job.job_id for job in jobs], ["1", "2"])
        self.assertEqual(session.greenhouse_pagination["candidate_jobs"], 0)

    def test_exact_metadata_attribution_filters_acquired_company_rows(self):
        listings = [
            summary(
                1,
                title="Core Platform Engineer",
                metadata=[{
                    "name": "Acquisition Company",
                    "value": "CoreWeave",
                }],
            ),
            summary(
                2,
                title="ML Platform Engineer",
                metadata=[{
                    "name": "Acquisition Company",
                    "value": "Weights & Biases",
                }],
            ),
        ]
        session = StreamingSession(
            [StreamingResponse({"jobs": listings}), StreamingResponse(detail(1))]
        )

        jobs = greenhouse.fetch_jobs(
            session,
            self.target(
                job_attribution_field="greenhouse_metadata.Acquisition Company",
                job_attribution_regex=r"CoreWeave",
            ),
        )

        self.assertEqual([job.job_id for job in jobs], ["1"])
        self.assertEqual(session.greenhouse_pagination["attribution_filtered"], 1)

    def test_accepted_attribution_groups_are_represented_within_bound(self):
        listings = [
            summary(
                index,
                title=f"Platform Engineer {index}",
                metadata=[{
                    "name": "Acquisition Company",
                    "value": "CoreWeave",
                }],
            )
            for index in range(1, 4)
        ]
        listings.append(
            summary(
                4,
                title="ML Infrastructure Engineer",
                metadata=[{
                    "name": "Acquisition Company",
                    "value": "Weights & Biases",
                }],
            )
        )
        session = StreamingSession([
            StreamingResponse({"jobs": listings}),
            StreamingResponse(detail(1)),
            StreamingResponse(detail(4)),
        ])

        jobs = greenhouse.fetch_jobs(
            session,
            self.target(
                max_jobs=2,
                job_attribution_field="greenhouse_metadata.Acquisition Company",
                job_attribution_regex=r"CoreWeave|Weights & Biases",
            ),
        )

        self.assertEqual([job.job_id for job in jobs], ["1", "4"])

    def test_metadata_attribution_contract_is_complete_and_regex_is_valid(self):
        for overrides, message in (
            ({"job_attribution_field": "Brand"}, "both field and regex"),
            ({"job_attribution_regex": "CoreWeave"}, "both field and regex"),
            (
                {
                    "job_attribution_field": "Brand",
                    "job_attribution_regex": "(",
                },
                "regex is invalid",
            ),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(AdapterError, message):
                    greenhouse.fetch_jobs(
                        StreamingSession([]), self.target(**overrides)
                    )


if __name__ == "__main__":
    unittest.main()
