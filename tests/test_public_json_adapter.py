from __future__ import annotations

import json
import unittest

from src.scraper.company_careers.adapters import public_json
from src.scraper.company_careers.models import AdapterError, CompanyTarget, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(self, payload, *, headers=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected GET")
        return self.responses.pop(0)


def aurora_target(**overrides):
    values = dict(
        name="Aurora Innovation",
        platform="public_json",
        careers_url="https://aurora.example/careers",
        endpoint_url="https://aurora.example/api/jobs-index",
        endpoint_variant=public_json.AURORA_JOBS_INDEX,
        allowed_apply_hosts=["aurora.example"],
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def aurora_row(index=1, **overrides):
    values = {
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "jobId": f"10000000-0000-4000-8000-{index:012d}",
        "title": f"Engineer {index}",
        "category": "Engineering",
        "locations": ["Pittsburgh, PA"],
        "isRemote": False,
        "employmentType": "FullTime",
        "applyLink": f"https://aurora.example/careers?ashby_jid=00000000-0000-4000-8000-{index:012d}",
        "publishedDate": "2026-09-28",
        "updatedAt": "2026-09-28T13:02:15.253Z",
    }
    values.update(overrides)
    return values


def aurora_payload(*rows, count=None):
    total = len(rows) if count is None else count
    return {
        "jobs": list(rows),
        "index": json.dumps(
            {
                "documentCount": total,
                "nextId": total,
                "documentIds": {str(index): row["id"] for index, row in enumerate(rows)},
            }
        ),
        "facets": {"locations": [], "departments": []},
    }


def wordpress_target(**overrides):
    values = dict(
        name="Deliveroo",
        platform="public_json",
        careers_url="https://careers.example/",
        endpoint_url="https://careers.example/wp-json/wp/v2/roles",
        endpoint_variant=public_json.WORDPRESS_ROLES,
        allowed_apply_hosts=["careers.example"],
        page_size=2,
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def wordpress_row(index=1, **overrides):
    values = {
        "id": index,
        "link": f"https://careers.example/role/engineer-{index}/",
        "title": {"rendered": f"Engineer &amp; Builder {index}"},
        "modified_gmt": "2026-09-28T00:00:36",
        "meta": {
            "ats_location": "Calgary, Canada",
            "ats_platform": "greenhouse",
            "ats_req_id": f"REQ-{index}",
            "ats_remote": False,
        },
    }
    values.update(overrides)
    return values


def wp_response(rows, *, total, pages):
    return Response(
        rows,
        headers={"x-wp-total": str(total), "x-wp-totalpages": str(pages)},
    )


class PublicJsonAdapterTests(unittest.TestCase):
    def test_aurora_complete_index_contract(self):
        rows = [aurora_row(1), aurora_row(2)]
        session = Session([Response(aurora_payload(*rows))])

        jobs = public_json.fetch_jobs(session, aurora_target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].raw_metadata["external_job_id"], rows[0]["id"])
        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["complete_collection_response"])
        self.assertEqual(metadata["total_available"], 2)

    def test_aurora_count_or_id_mismatch_fails_closed(self):
        row = aurora_row()
        with self.assertRaisesRegex(AdapterError, "complete unique collection"):
            public_json.fetch_jobs(
                Session([Response(aurora_payload(row, count=2))]), aurora_target()
            )

    def test_wordpress_explicit_final_page_and_total(self):
        session = Session(
            [
                wp_response([wordpress_row(1), wordpress_row(2)], total=3, pages=2),
                wp_response([wordpress_row(3)], total=3, pages=2),
            ]
        )

        jobs = public_json.fetch_jobs(session, wordpress_target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0].title, "Engineer & Builder 1")
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["terminal_evidence_type"], "wordpress_total_pages")
        self.assertTrue(metadata["terminal_page_fully_processed"])

    def test_wordpress_total_change_then_stable_restart_recovers(self):
        session = Session(
            [
                wp_response([wordpress_row(1), wordpress_row(2)], total=4, pages=2),
                wp_response([wordpress_row(3)], total=3, pages=2),
                wp_response([wordpress_row(1), wordpress_row(2)], total=3, pages=2),
                wp_response([wordpress_row(3)], total=3, pages=2),
            ]
        )

        jobs = public_json.fetch_jobs(session, wordpress_target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 3)
        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["stabilized_after_collection_retry"])

    def test_wordpress_duplicate_across_pages_is_incomplete(self):
        session = Session(
            [
                wp_response([wordpress_row(1), wordpress_row(2)], total=3, pages=2),
                wp_response([wordpress_row(2)], total=3, pages=2),
            ]
        )

        jobs = public_json.fetch_jobs(session, wordpress_target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["repeated_stable_ids"])

    def test_wordpress_malformed_final_page_is_not_complete(self):
        malformed = wordpress_row(3, title={"rendered": ""})
        session = Session(
            [
                wp_response([wordpress_row(1), wordpress_row(2)], total=3, pages=2),
                wp_response([malformed], total=3, pages=2),
            ]
        )

        jobs = public_json.fetch_jobs(session, wordpress_target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "pagination_failure")
        self.assertEqual(metadata["rejected_record_count"], 1)

    def test_wordpress_configured_page_ceiling_fails_closed(self):
        session = Session(
            [wp_response([wordpress_row(1), wordpress_row(2)], total=3, pages=2)]
        )

        jobs = public_json.fetch_jobs(session, wordpress_target(max_pages=1))
        metadata = source_scan_metadata(session)

        self.assertEqual(jobs, [])
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_pages_reached")
        self.assertEqual(len(session.calls), 1)

    def test_production_migrates_only_proven_browser_sources(self):
        targets = {target.name: target for target in load_target_companies()}
        self.assertEqual(targets["Aurora Innovation"].platform, "public_json")
        self.assertEqual(targets["Deliveroo"].platform, "public_json")
        self.assertEqual(targets["ASML"].platform, "browser_rendered_html")
        self.assertEqual(targets["ByteDance"].platform, "browser_rendered_html")


if __name__ == "__main__":
    unittest.main()
