from __future__ import annotations

import json
import unittest

from src.integrations.supabase_job_store import generate_source_key
from src.scraper.company_careers.adapters import apple
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


class Response:
    def __init__(
        self,
        payload=None,
        *,
        body: bytes | None = None,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        chunk_size: int = 37,
    ):
        self.body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.chunk_size = chunk_size
        self.closed = False

    def iter_content(self, chunk_size):
        size = min(chunk_size, self.chunk_size)
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected POST: {url}")
        return self.responses.pop(0)


def target(**overrides):
    values = {
        "name": "Apple",
        "platform": "apple",
        "endpoint_url": apple.SEARCH_ENDPOINT,
        "endpoint_variant": apple.ENDPOINT_VARIANT,
        "locale": "en-us",
        "allowed_apply_hosts": ["jobs.apple.com"],
        "max_pages": 3,
        "max_jobs": 20,
        "max_response_bytes": 100_000,
    }
    values.update(overrides)
    return CompanyTarget(**values)


def job(
    job_id="200700001-0157",
    *,
    title="Software Engineering Intern",
    slug="software-engineering-intern",
    location="Austin",
    summary="Build products at Apple.",
):
    return {
        "id": job_id,
        "reqId": job_id,
        "positionId": job_id.split("-", 1)[0],
        "postingTitle": title,
        "transformedPostingTitle": slug,
        "locations": [{"name": location, "countryID": "iso-country-USA"}],
        "postingDate": "Aug 30, 2026",
        "postDateInGMT": "2026-08-30T12:00:00Z",
        "localeInfo": {"defaultLocaleCode": "en-us"},
        "team": {
            "teamName": "Software and Services",
            "teamCode": "SFTWR",
        },
        "type": "REQ",
        "homeOffice": False,
        "postExternal": True,
        "jobSummary": summary,
    }


def page(items, total=None):
    return {
        "res": {
            "searchResults": list(items),
            "totalRecords": len(items) if total is None else total,
            "talentLimitDetails": {},
            "talentRolesInfoById": {},
        }
    }


class AppleAdapterTests(unittest.TestCase):
    def test_normal_response_preserves_stable_identity_and_canonical_url(self):
        session = Session([Response(page([job()]))])

        jobs = apple.fetch_jobs(session, target())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0].job_id,
            "browser_rendered_html:jobs.apple.com:200700001-0157",
        )
        self.assertEqual(jobs[0].source, "browser_rendered_html:jobs.apple.com")
        self.assertEqual(
            jobs[0].url,
            "https://jobs.apple.com/en-us/details/200700001-0157/"
            "software-engineering-intern?team=SFTWR",
        )
        self.assertEqual(jobs[0].location, "Austin")
        self.assertEqual(jobs[0].departments, ["Software and Services"])
        self.assertEqual(jobs[0].description, "Build products at Apple.")
        self.assertEqual(jobs[0].raw_metadata["requisition_id"], "200700001-0157")
        request_url, request = session.post_calls[0]
        self.assertEqual(request_url, apple.SEARCH_ENDPOINT)
        self.assertEqual(request["json"]["page"], 1)
        self.assertEqual(
            request["json"]["filters"]["locations"],
            ["postLocation-USA", "postLocation-CAN"],
        )
        self.assertTrue(request["stream"])
        self.assertEqual(session.apple_pagination["stop_reason"], "total_records_reached")

    def test_pipeline_role_uses_canonical_position_id(self):
        item = job("PIPE-114438158", title="US Specialist", slug="us-specialist")
        item["positionId"] = "114438158"
        jobs = apple.fetch_jobs(Session([Response(page([item]))]), target())
        self.assertEqual(
            jobs[0].job_id,
            "browser_rendered_html:jobs.apple.com:114438158",
        )
        self.assertIn("/details/114438158/us-specialist", jobs[0].url)

    def test_percent_encoded_official_slug_remains_a_safe_canonical_path(self):
        item = job(slug="%EF%A3%BF-speech-processing-ml-algorithm-engineer")
        jobs = apple.fetch_jobs(Session([Response(page([item]))]), target())
        self.assertIn(
            "/%EF%A3%BF-speech-processing-ml-algorithm-engineer?team=SFTWR",
            jobs[0].url,
        )

    def test_api_migration_preserves_existing_persistence_key(self):
        current = apple.fetch_jobs(Session([Response(page([job()]))]), target())[0]
        historical = JobRecord(
            job_id="browser_rendered_html:jobs.apple.com:200700001-0157",
            title=current.title,
            company="Apple",
            location="",
            url=current.url,
            updated_at="",
            source="browser_rendered_html:jobs.apple.com",
        )
        self.assertEqual(generate_source_key(current), generate_source_key(historical))

    def test_wrong_employer_and_unreviewed_endpoint_are_rejected(self):
        cases = (
            (target(name="Pear"), "another employer"),
            (target(endpoint_url="https://jobs.apple.com/api/v2/search"), "reviewed"),
            (target(allowed_apply_hosts=["example.com"]), "restricted"),
        )
        for candidate, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                apple.fetch_jobs(Session([]), candidate)

    def test_malformed_response_shapes_are_not_empty_successes(self):
        malformed = (
            ({}, "envelope"),
            ({"res": {"searchResults": {}}}, "searchResults"),
            (page([], total=1), "empty-looking"),
            (page([job()], total=0), "smaller"),
            ({"res": {"searchResults": [], "totalRecords": True}}, "non-negative"),
        )
        for payload, message in malformed:
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                apple.fetch_jobs(Session([Response(payload)]), target())

    def test_html_challenge_and_malformed_json_fail_closed(self):
        cases = (
            (
                Response(
                    body=b"<html><title>Access Denied</title></html>",
                    content_type="text/html",
                ),
                "blocked response",
            ),
            (Response(body=b"{truncated"), "malformed JSON"),
        )
        for response, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                apple.fetch_jobs(Session([response]), target())
            self.assertTrue(response.closed)

    def test_valid_empty_response_has_explicit_zero_job_contract(self):
        session = Session([Response(page([], total=0))])
        self.assertEqual(apple.fetch_jobs(session, target()), [])
        self.assertTrue(session.apple_pagination["zero_job_contract"])
        self.assertEqual(session.apple_pagination["stop_reason"], "validated_zero_jobs")

    def test_pagination_terminates_on_total_records(self):
        first = job("200700001-0157")
        second = job(
            "200700002-0157",
            title="Hardware Engineering Co-op",
            slug="hardware-engineering-co-op",
        )
        session = Session(
            [Response(page([first], total=2)), Response(page([second], total=2))]
        )

        jobs = apple.fetch_jobs(session, target(max_jobs=3))

        self.assertEqual(len(jobs), 2)
        self.assertEqual([call[1]["json"]["page"] for call in session.post_calls], [1, 2])
        self.assertEqual(session.apple_pagination["pages_fetched"], 2)
        self.assertEqual(session.apple_pagination["stop_reason"], "total_records_reached")

    def test_pagination_loop_is_rejected(self):
        repeated = page([job()], total=3)
        session = Session([Response(repeated), Response(repeated)])
        with self.assertRaisesRegex(AdapterError, "repeated"):
            apple.fetch_jobs(session, target(max_jobs=3))
        self.assertTrue(session.apple_pagination["repeated_page"])

    def test_conflicting_duplicate_stable_id_is_rejected(self):
        first = job()
        conflicting = job(title="Changed title", slug="changed-title")
        session = Session(
            [
                Response(page([first], total=2)),
                Response(page([conflicting], total=2)),
            ]
        )
        with self.assertRaisesRegex(AdapterError, "conflicting duplicate"):
            apple.fetch_jobs(session, target(max_jobs=3))

    def test_identical_duplicate_is_deduplicated_only_after_full_comparison(self):
        first = job()
        second = job(
            "200700002-0157",
            title="Product Design Intern",
            slug="product-design-intern",
        )
        session = Session(
            [
                Response(page([first], total=3)),
                Response(page([first, second], total=3)),
            ]
        )
        jobs = apple.fetch_jobs(session, target(max_jobs=3))
        self.assertEqual(len(jobs), 2)
        self.assertFalse(session.apple_pagination["scan_complete"])
        self.assertTrue(
            session.apple_pagination["collection_changed_during_scan"]
        )
        self.assertEqual(
            session.apple_pagination["scan_completeness_reason"],
            "collection_changed_during_scan",
        )

    def test_response_and_job_caps_are_hard_limits(self):
        with self.assertRaisesRegex(AdapterError, "max_jobs"):
            apple.fetch_jobs(Session([]), target(max_jobs=21))
        with self.assertRaisesRegex(AdapterError, "max_pages"):
            apple.fetch_jobs(Session([]), target(max_pages=4))

        oversized = Response(body=b"x" * 101, chunk_size=11)
        with self.assertRaisesRegex(AdapterError, "byte limit"):
            apple.fetch_jobs(
                Session([oversized]),
                target(max_response_bytes=100),
            )
        self.assertTrue(oversized.closed)

        too_many = [
            job(
                f"2007{index:05d}-0157",
                title=f"Role {index}",
                slug=f"role-{index}",
            )
            for index in range(apple.PAGE_SIZE + 1)
        ]
        with self.assertRaisesRegex(AdapterError, "page size"):
            apple.fetch_jobs(Session([Response(page(too_many))]), target())

    def test_job_contract_rejects_unstable_or_noncanonical_records(self):
        cases = []
        mismatch = job()
        mismatch["reqId"] = "different"
        cases.append((mismatch, "stable requisition"))
        external = job()
        external["postExternal"] = False
        cases.append((external, "externally posted"))
        bad_slug = job()
        bad_slug["transformedPostingTitle"] = "javascript:alert(1)"
        cases.append((bad_slug, "canonical URL slug"))
        missing_location = job()
        missing_location["locations"] = []
        cases.append((missing_location, "structured locations"))
        for item, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                apple.fetch_jobs(Session([Response(page([item]))]), target())

    def test_authoritative_total_can_cross_normal_budget_within_shared_ceiling(self):
        total = 261  # Fourteen pages: beyond the normal + first-stage allowance.
        items = [
            job(
                f"2008{index:05d}-0157",
                title=f"Role {index}",
                slug=f"role-{index}",
            )
            for index in range(total)
        ]
        responses = [
            Response(page(items[offset : offset + apple.PAGE_SIZE], total=total))
            for offset in range(0, total, apple.PAGE_SIZE)
        ]
        session = Session(responses)

        jobs = apple.fetch_jobs(session, target())

        self.assertEqual(len(jobs), total)
        self.assertTrue(session.apple_pagination["scan_complete"])
        self.assertEqual(
            session.apple_pagination["terminal_evidence_type"],
            "authoritative_total",
        )
        self.assertTrue(session.apple_pagination["listing_only_second_stage_eligible"])

    def test_total_above_shared_job_ceiling_remains_incomplete(self):
        items = [
            job(
                f"2009{index:05d}-0157",
                title=f"Role {index}",
                slug=f"role-{index}",
            )
            for index in range(apple.PAGE_SIZE)
        ]
        session = Session([Response(page(items, total=20_001))])

        jobs = apple.fetch_jobs(session, target())

        self.assertEqual(len(jobs), apple.MAX_RESULTS)
        self.assertFalse(session.apple_pagination["scan_complete"])
        self.assertTrue(session.apple_pagination["safety_limit_reached"])
        self.assertEqual(
            session.apple_pagination["stop_reason"], "maximum_results_reached"
        )


if __name__ == "__main__":
    unittest.main()
