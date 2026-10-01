from __future__ import annotations

from dataclasses import replace
import json
import unittest

from src.scraper.company_careers.adapters import hibob, public_json, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_health import finalize_source_scan


class Response:
    def __init__(self, value, *, url: str, status: int = 200):
        self.status_code = status
        self.url = url
        self._value = value
        self.text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        self.content = self.text.encode("utf-8")
        self.headers = {
            "content-type": "application/json" if isinstance(value, (dict, list)) else "text/html"
        }

    def json(self):
        if not isinstance(self._value, (dict, list)):
            raise ValueError("not JSON")
        return self._value


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.responses.pop(0)


def configured(name: str) -> CompanyTarget:
    return next(
        target
        for target in load_target_companies(apply_reliability_repairs=True)
        if target.name == name
    )


def roblox_row(job_id=123, **overrides):
    row = {
        "id": job_id,
        "title": "Software Engineer",
        "location": "San Mateo, CA, United States",
        "employment_type": "Salaried Employee",
        "groups": ["Engineering"],
        "department": ["Infrastructure"],
        "early_career": False,
    }
    row.update(overrides)
    return row


def roblox_responses(target: CompanyTarget, payload, *, page_url=None, script_url=None):
    script_url = script_url or "https://careers.roblox.com/_next/static/chunks/325-fixture.js"
    page = Response(
        f"<html><body>Roblox Careers<script src='{script_url}'></script></body></html>",
        url=page_url or str(target.careers_url),
    )
    script = Response(
        'let e="d32kbl9jppd7az.cloudfront.net/careers"; '
        'fetch(`https://${e}/jobs.json`)',
        url=script_url,
    )
    feed = Response(payload, url=str(target.endpoint_url))
    return [page, script, feed]


class RecoveryRegistryTests(unittest.TestCase):
    def test_recovered_sources_use_reviewed_contracts(self):
        expected = {
            "IMC Trading": ("javascript_embedded_data", "next_flight_collection"),
            "Roblox": ("public_json", public_json.ROBLOX_JOBS),
            "Xanadu": ("hibob", hibob.ENDPOINT_VARIANT),
            "YYC Calgary Airport Authority": ("static_html", "server_rendered_links"),
        }
        for name, contract in expected.items():
            with self.subTest(company=name):
                target = configured(name)
                self.assertEqual((target.platform, target.endpoint_variant), contract)
        self.assertEqual(configured("IMC Trading").max_jobs, 1000)
        self.assertIsNone(configured("IMC Trading").item_filter_field)
        self.assertFalse(configured("YYC Calgary Airport Authority").single_page_collection)


class RobloxPublicJsonTests(unittest.TestCase):
    def test_official_app_binding_proves_atomic_collection(self):
        target = configured("Roblox")
        session = Session(roblox_responses(target, [roblox_row()]))
        jobs = public_json.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(jobs[0].job_id, "public_json:d32kbl9jppd7az.cloudfront.net:123")
        self.assertEqual(jobs[0].url, "https://careers.roblox.com/jobs/123")
        self.assertEqual(metadata["terminal_evidence_type"], "roblox_official_atomic_feed")
        self.assertTrue(metadata["complete_collection_response"])
        self.assertEqual(metadata["source_identity_status"], "verified")

    def test_wrong_host_redirect_missing_binding_and_response_bound_fail_closed(self):
        target = configured("Roblox")
        wrong_host = replace(target, endpoint_url="https://evil.example/careers/jobs.json")
        with self.assertRaisesRegex(AdapterError, "reviewed official jobs feed"):
            public_json.fetch_jobs(Session([]), wrong_host)

        redirected = roblox_responses(target, [roblox_row()], page_url="https://evil.example/jobs")
        with self.assertRaisesRegex(AdapterError, "redirected"):
            public_json.fetch_jobs(Session(redirected), target)

        page, script, feed = roblox_responses(target, [roblox_row()])
        script.text = "no reviewed endpoint here"
        script.content = script.text.encode("utf-8")
        with self.assertRaisesRegex(AdapterError, "did not bind"):
            public_json.fetch_jobs(Session([page, script, feed]), target)

        bounded = replace(target, max_response_bytes=10)
        with self.assertRaisesRegex(AdapterError, "response bound"):
            public_json.fetch_jobs(Session(roblox_responses(bounded, [roblox_row()])), bounded)

    def test_malformed_duplicate_false_atomic_and_over_ceiling_fail_closed(self):
        target = configured("Roblox")
        cases = [
            [roblox_row(id=None)],
            [roblox_row(), roblox_row(title="Different")],
            {"jobs": [roblox_row()]},
            [roblox_row(index + 1) for index in range(public_json.ROBLOX_MAX_JOBS + 1)],
        ]
        for payload in cases:
            with self.subTest(kind=type(payload).__name__, size=len(payload)):
                with self.assertRaises(AdapterError):
                    public_json.fetch_jobs(Session(roblox_responses(target, payload)), target)


class HiBobCompletionTests(unittest.TestCase):
    def test_atomic_collection_publishes_identity_and_terminal_evidence(self):
        target = configured("Xanadu")
        identity = Response(
            {"companyName": "Xanadu", "isPublished": True, "isBobCareerSite": True},
            url="https://xanadu.careers.hibob.com/api/career-site",
        )
        row = {
            "id": "56ade5eb-5943-4c50-9c85-680c7aa83a3d",
            "title": "Quantum Software Developer",
            "site": "Toronto",
            "country": "Canada",
            "publishedAt": "2026-09-30T00:00:00Z",
            "department": "Software",
            "description": "<p>Build systems.</p>",
        }
        session = Session([identity, Response({"jobAdDetails": [row]}, url=str(target.endpoint_url))])
        jobs = hibob.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(metadata["pages_requested"], 2)
        self.assertEqual(metadata["source_identity_status"], "validated")
        self.assertEqual(metadata["terminal_evidence_type"], "hibob_atomic_collection")
        self.assertTrue(metadata["complete_collection_response"])


class YYCCompletenessTests(unittest.TestCase):
    BODY = """
    <html><body>
      <a data-job-id="123" href="/Opportunities?JobId=123">Systems Engineer</a>
    </body></html>
    """

    def test_single_page_flag_can_make_unproven_identity_fully_complete(self):
        target = replace(
            configured("YYC Calgary Airport Authority"),
            single_page_collection=True,
        )
        session = Session([Response(self.BODY, url=str(target.listing_url))])

        jobs = static_html.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["source_identity_status"], "unproven")
        self.assertTrue(metadata["scan_complete"])

        finalize_source_scan(metadata, jobs_collected=len(jobs))
        self.assertEqual(metadata["scan_state"], "complete")
        self.assertEqual(metadata["source_health_code"], "healthy")

    def test_effective_yyc_configuration_remains_incomplete_without_attribution(self):
        target = configured("YYC Calgary Airport Authority")
        session = Session([Response(self.BODY, url=str(target.listing_url))])

        jobs = static_html.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["source_identity_status"], "unproven")
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "no_pagination_configured")

        finalize_source_scan(metadata, jobs_collected=len(jobs))
        self.assertEqual(metadata["scan_state"], "incomplete")
        self.assertEqual(metadata["source_health_code"], "pagination_unproven")


if __name__ == "__main__":
    unittest.main()
