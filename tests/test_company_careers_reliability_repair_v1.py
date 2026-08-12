from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

from src.scraper.company_careers.adapters import exalead, meta, uber
from src.scraper.company_careers.models import AdapterError, CompanyTarget, CoverageStatus
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner


class Response:
    def __init__(self, payload=None, *, status=200, text=None, headers=None):
        self.payload = payload
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)
        self.headers = dict(headers or {})

    def json(self):
        return self.payload


class Session:
    def __init__(self, *, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.gets.pop(0)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.posts.pop(0)


class CompanyCareersReliabilityRepairV1Tests(unittest.TestCase):
    def test_uber_server_rendered_contract_fails_closed_on_unproven_empty_page(self):
        target = CompanyTarget(name="Uber", platform="uber", max_pages=1)
        session = Session(gets=[Response(text="<!doctype html><html><body>changed</body></html>")])
        with self.assertRaisesRegex(AdapterError, "did not prove an empty"):
            uber.fetch_jobs(session, target)

    def test_meta_reuses_one_context_for_all_parent_inventory_terms(self):
        page = (
            '["LSD",[],{"token":"token"}]'
            '{"client_revision":1,"hsi":"2"}'
        )
        empty = {
            "data": {"job_search_with_featured_jobs": {"all_jobs": [], "featured_jobs": []}}
        }
        session = Session(
            gets=[Response(text=page)],
            posts=[Response(empty), Response(empty)],
        )
        target = CompanyTarget(
            name="Meta", platform="meta", search_terms=["One", "Two"], max_pages=1
        )
        with patch.object(meta.time, "sleep"):
            self.assertEqual(meta.fetch_jobs(session, target), [])
        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(session.get_calls[0][0][0], meta.SEARCH_URL)

    def test_meta_retry_after_and_exponential_backoff_are_bounded(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10))
        self.assertEqual(
            meta._retry_delay(Response(status=429, headers={"Retry-After": "60"}), 0),
            meta.MAX_RETRY_AFTER_SECONDS,
        )
        self.assertEqual(
            meta._retry_delay(Response(status=429, headers={"Retry-After": future}), 0),
            meta.MAX_RETRY_AFTER_SECONDS,
        )
        self.assertEqual(meta._retry_delay(Response(status=429), 0), 1.0)
        self.assertEqual(meta._retry_delay(Response(status=429), 1), 2.0)

        session = Session(
            gets=[
                Response(status=429, headers={"Retry-After": "2"}),
                Response(status=429),
                Response(status=429),
            ]
        )
        with patch.object(meta.time, "sleep") as sleep:
            response = meta._request_with_backoff(
                session, "get", "https://example.test", pacer=meta._RequestPacer()
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(session.get_calls), meta.MAX_ATTEMPTS)
        self.assertIn(2.0, [call.args[0] for call in sleep.call_args_list])

    def test_exalead_public_inventory_normalizes_stable_official_job(self):
        payload = {
            "nhits": 1,
            "hits": [
                {
                    "metas": [
                        {"name": "card_id", "value": "549407"},
                        {"name": "content_title", "value": "Partner Sales Manager"},
                        {"name": "content_info_2_value", "value": "Turkey, Istanbul"},
                        {"name": "content_type_display_text", "value": "Sales"},
                        {"name": "content_start_datetime", "value": "2026/08/11 10:08:56"},
                        {
                            "name": "content_cta_1_url",
                            "value": "https://www.3ds.com/careers/jobs/partner-sales-manager-549407",
                        },
                        {
                            "name": "content_cta_2_url",
                            "value": "https://talentacquisition.3ds.com/careersection/qa/jobapply.ftl?job=549407&lang=en",
                        },
                    ]
                }
            ],
        }
        session = Session(gets=[Response(payload)])
        target = CompanyTarget(
            name="Dassault Systèmes",
            platform="exalead",
            endpoint_url="https://www.3ds.com/apisearch/card_search_api",
            endpoint_variant=exalead.SUPPORTED_VARIANT,
            allowed_apply_hosts=["www.3ds.com"],
            max_pages=1,
            max_jobs=20,
            page_size=15,
        )
        jobs = exalead.fetch_jobs(session, target)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "exalead:549407")
        self.assertEqual(jobs[0].departments, ["Sales"])
        self.assertEqual(jobs[0].raw_metadata["external_job_id"], "549407")

    def test_production_registry_contains_current_contracts_and_baselines(self):
        entries = load_target_companies(apply_reliability_repairs=True)
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(len(entries), 339)
        self.assertEqual(by_name["Lattice Semiconductor"].platform, "workday")
        self.assertEqual(by_name["Lattice Semiconductor"].site_id, "latticesemiconductorscareers")
        self.assertEqual(by_name["Dassault Systèmes"].platform, "exalead")
        self.assertEqual(len(load_parent_source_mappings()), 4)

    def test_technical_failure_is_not_a_successful_zero_inventory(self):
        target = CompanyTarget(name="Meta", platform="repair_failure")
        mappings = [
            item
            for item in load_parent_source_mappings()
            if item.parent_company == "Meta"
        ]
        with patch(
            "src.scraper.company_careers.scanner.ADAPTERS",
            {"repair_failure": Mock(side_effect=AdapterError("HTTP 429"))},
        ):
            report = CompanyCareersScanner(
                targets=[target], parent_source_mappings=mappings
            ).scan()
        self.assertEqual(report.results[0].status, CoverageStatus.FAILED_TECHNICAL)
        self.assertEqual(report.results[0].fetched_count, 0)
        self.assertTrue(
            all(
                row["status"] == CoverageStatus.PARENT_FAILED_TECHNICAL
                for row in report.mapped_coverage
            )
        )


if __name__ == "__main__":
    unittest.main()
