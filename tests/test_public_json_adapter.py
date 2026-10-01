from __future__ import annotations

import json
import unittest

from src.scraper.company_careers.adapters import public_json
from src.scraper.company_careers.models import AdapterError, CompanyTarget, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(self, payload, *, headers=None, status_code=200, url=""):
        self.payload = payload if isinstance(payload, (dict, list)) else None
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.content = (
            json.dumps(payload).encode()
            if self.payload is not None
            else str(payload).encode()
        )
        self.text = self.content.decode()
        self.url = url

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


def shazamme_target(**overrides):
    site_id = "a5bcebfa-981b-46cd-b46f-ad91a50505b8"
    values = dict(
        name="Zoom",
        platform="public_json",
        careers_url="https://www.careers.zoom.us/job-results",
        endpoint_url=f"https://shazamme.io/job-results/{site_id}",
        endpoint_variant=public_json.SHAZAMME_JOB_RESULTS,
        site_id=site_id,
        identity_regex=r"\bZoom(?: Careers)?\b",
        job_attribution_regex=r"^Zoom$",
        apply_url_template="https://www.careers.zoom.us{path}",
        allowed_source_hosts=["shazamme.io"],
        allowed_apply_hosts=["www.careers.zoom.us"],
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def shazamme_row(index=1, **overrides):
    data = {
        "jobID": f"00000000-0000-4000-8000-{index:012d}",
        "referenceNumber": f"R{index}",
        "jobName": f"Engineer {index}",
        "activeStatus": True,
        "siteID": "a5bcebfa-981b-46cd-b46f-ad91a50505b8",
        "advertiserName": "Zoom",
        "siteName": "Zoom Careers",
        "jobURL": f"https://careers.zoom.com/job-details/engineer-{index}-jobs-{index}",
        "city": "Calgary",
        "country": "Canada",
    }
    data.update(overrides)
    return {"data": data}


def shazamme_session(payload, *, headers=None):
    target = shazamme_target()
    identity = Response(
        f"<html><title>Zoom Careers</title>{target.site_id}</html>",
        headers={"content-type": "text/html"},
        url=target.careers_url,
    )
    endpoint = Response(
        payload,
        headers=headers,
        url=target.endpoint_url,
    )
    return target, Session([identity, endpoint])


class PublicJsonAdapterTests(unittest.TestCase):
    def test_shazamme_exact_atomic_collection_contract(self):
        target, session = shazamme_session([shazamme_row(1), shazamme_row(2)])

        jobs = public_json.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["atomic_collection_contract"])
        self.assertFalse(metadata["authoritative_total_present"])
        self.assertEqual(
            metadata["terminal_evidence_type"],
            "shazamme_atomic_collection_contract",
        )

    def test_shazamme_matching_source_total_is_authoritative(self):
        target, session = shazamme_session(
            [shazamme_row(1)], headers={"x-total-count": "1"}
        )

        public_json.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["authoritative_total_present"])
        self.assertEqual(metadata["total_available"], 1)

    def test_shazamme_pagination_or_truncation_headers_fail_closed(self):
        for headers in (
            {"x-has-more": "true"},
            {"link": '<https://shazamme.io/job-results/site?page=2>; rel="next"'},
            {"content-range": "items 0-0/2"},
        ):
            with self.subTest(headers=headers):
                target, session = shazamme_session(
                    [shazamme_row(1)], headers=headers
                )
                with self.assertRaisesRegex(
                    AdapterError, "pagination or truncation signals"
                ):
                    public_json.fetch_jobs(session, target)

    def test_shazamme_conflicting_source_total_fails_closed(self):
        target, session = shazamme_session(
            [shazamme_row(1)], headers={"x-total-count": "2"}
        )
        with self.assertRaisesRegex(AdapterError, "total conflicts"):
            public_json.fetch_jobs(session, target)

    def test_shazamme_wrong_site_or_advertiser_fails_closed(self):
        cases = (
            (shazamme_row(1, siteID="00000000-0000-4000-8000-000000000999"), "site identity"),
            (shazamme_row(1, advertiserName="Other"), "advertiser attribution"),
        )
        for row, error in cases:
            with self.subTest(error=error):
                target, session = shazamme_session([row])
                with self.assertRaisesRegex(AdapterError, error):
                    public_json.fetch_jobs(session, target)

    def test_shazamme_duplicate_ids_or_urls_fail_closed(self):
        duplicate_id = [
            shazamme_row(1),
            shazamme_row(
                2, jobID="00000000-0000-4000-8000-000000000001"
            ),
        ]
        duplicate_url = [
            shazamme_row(1),
            shazamme_row(
                2,
                jobURL="https://careers.zoom.com/job-details/engineer-1-jobs-1",
            ),
        ]
        for rows in (duplicate_id, duplicate_url):
            with self.subTest(rows=rows):
                target, session = shazamme_session(rows)
                with self.assertRaisesRegex(AdapterError, "repeated a stable ID"):
                    public_json.fetch_jobs(session, target)

    def test_shazamme_nonterminal_or_row_pagination_shape_fails_closed(self):
        cases = (
            ({"items": [shazamme_row(1)], "hasMore": True}, "unwrapped array"),
            ([shazamme_row(1, nextPage=2)], "pagination metadata"),
        )
        for payload, error in cases:
            with self.subTest(error=error):
                target, session = shazamme_session(payload)
                with self.assertRaisesRegex(AdapterError, error):
                    public_json.fetch_jobs(session, target)

    def test_shazamme_unreviewed_site_id_is_rejected_without_network(self):
        site_id = "00000000-0000-4000-8000-000000000999"
        target = shazamme_target(
            site_id=site_id,
            endpoint_url=f"https://shazamme.io/job-results/{site_id}",
        )
        session = Session([])

        with self.assertRaisesRegex(AdapterError, "one reviewed site"):
            public_json.fetch_jobs(session, target)
        self.assertEqual(session.calls, [])

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
        self.assertEqual(targets["ByteDance"].platform, "first_party_json")


if __name__ == "__main__":
    unittest.main()
