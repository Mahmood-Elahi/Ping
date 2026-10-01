from __future__ import annotations

import unittest
from unittest.mock import patch

from src.scraper.company_careers.adapters import first_party_json
from src.scraper.company_careers.models import AdapterError, CompanyTarget, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(self, payload, *, url, body: str | None = None, status: int = 200):
        self.payload = payload
        self.url = url
        self.status_code = status
        self.content = (body if body is not None else "json").encode()
        self.text = body or ""
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.responses.pop(0)


def target(**overrides):
    values = dict(
        name="ByteDance",
        platform="first_party_json",
        careers_url="https://jobs.bytedance.com/en/",
        endpoint_url="https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts",
        endpoint_variant=first_party_json.BYTEDANCE_SUPPLIER,
        identity_regex=r"\bByteDance\b",
        apply_url_template="https://joinbytedance.com/search/{id}",
        allowed_source_hosts=["jobs.bytedance.com"],
        allowed_apply_hosts=["joinbytedance.com"],
        page_size=1,
        max_pages=2,
        max_jobs=2,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def row(external_id: str):
    return {
        "id": external_id,
        "title": f"Engineer {external_id}",
        "description": "Build systems.",
        "requirement": "Use Python.",
        "city_info": {
            "i18n_name": "Calgary",
            "parent": {"i18n_name": "Canada", "parent": None},
        },
        "job_category": {"i18n_name": "Engineering", "parent": None},
    }


def payload(rows, total: int):
    return {"code": 0, "message": "ok", "data": {"job_post_list": rows, "count": total}}


def identity():
    return Response({}, url="https://joinbytedance.com/search", body="ByteDance Careers")


class FirstPartyJSONAdapterTests(unittest.TestCase):
    @patch.object(first_party_json.time, "sleep", return_value=None)
    def test_authoritative_total_pagination_and_normalization(self, _sleep):
        endpoint = target().endpoint_url
        session = Session(
            identity(),
            Response(payload([row("7678213736330561797")], 2), url=endpoint),
            Response(payload([row("7673470671408072965")], 2), url=endpoint),
        )

        jobs = first_party_json.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(len({job.job_id for job in jobs}), 2)
        self.assertEqual(len({job.url for job in jobs}), 2)
        self.assertEqual(jobs[0].location, "Calgary | Canada")
        self.assertEqual(jobs[0].departments, ["Engineering"])
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")
        self.assertEqual(metadata["total_available"], 2)
        self.assertEqual(session.calls[2][2]["json"]["offset"], 1)
        self.assertEqual(session.calls[1][2]["headers"]["Website-Path"], "en")

    def test_zero_collection_is_conclusive(self):
        session = Session(identity(), Response(payload([], 0), url=target().endpoint_url))
        self.assertEqual(first_party_json.fetch_jobs(session, target()), [])
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "validated_zero_jobs")

    @patch.object(first_party_json.time, "sleep", return_value=None)
    def test_repeated_ids_restart_once_and_remain_incomplete(self, _sleep):
        endpoint = target().endpoint_url
        page_one = lambda: Response(payload([row("7678213736330561797")], 2), url=endpoint)
        page_two = lambda: Response(payload([row("7678213736330561797")], 2), url=endpoint)
        session = Session(identity(), page_one(), page_two(), identity(), page_one(), page_two())

        jobs = first_party_json.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["collection_changed_during_scan"])
        self.assertTrue(metadata["collection_retry_exhausted"])
        self.assertEqual(metadata["collection_restart_count"], 1)

    @patch.object(first_party_json.time, "sleep", return_value=None)
    def test_changed_total_restarts_once_and_accepts_only_stable_retry(self, _sleep):
        endpoint = target().endpoint_url
        first = [
            identity(),
            Response(payload([row("7678213736330561797")], 2), url=endpoint),
            Response(payload([row("7673470671408072965")], 1), url=endpoint),
        ]
        stable = [
            identity(),
            Response(payload([row("7678213736330561797")], 2), url=endpoint),
            Response(payload([row("7673470671408072965")], 2), url=endpoint),
        ]
        session = Session(*first, *stable)

        jobs = first_party_json.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 2)
        self.assertTrue(metadata["scan_complete"])
        self.assertTrue(metadata["stabilized_after_collection_retry"])
        self.assertEqual(metadata["collection_restart_count"], 1)

    def test_limits_identity_endpoint_and_redirects_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "max_jobs"):
            first_party_json.validate_target(target(max_jobs=2001))
        with self.assertRaisesRegex(AdapterError, "max_pages"):
            first_party_json.validate_target(target(max_pages=21))
        with self.assertRaisesRegex(AdapterError, "reviewed public route"):
            first_party_json.validate_target(target(endpoint_url="https://jobs.bytedance.com/api/other"))
        with self.assertRaisesRegex(AdapterError, "did not match"):
            first_party_json.fetch_jobs(
                Session(Response({}, url="https://joinbytedance.com/search", body="Another company")),
                target(),
            )
        session = Session(
            identity(),
            Response(payload([row("7678213736330561797")], 1), url="https://evil.test/api"),
        )
        with self.assertRaisesRegex(AdapterError, "outside allowed"):
            first_party_json.fetch_jobs(session, target())

    def test_registry_uses_reviewed_bytedance_contract_only(self):
        targets = {item.name: item for item in load_target_companies(apply_reliability_repairs=True)}
        self.assertEqual(targets["ByteDance"].endpoint_variant, first_party_json.BYTEDANCE_SUPPLIER)
        self.assertEqual(targets["ByteDance"].page_size, 100)
        self.assertEqual(targets["ASML"].platform, "browser_rendered_html")


if __name__ == "__main__":
    unittest.main()
