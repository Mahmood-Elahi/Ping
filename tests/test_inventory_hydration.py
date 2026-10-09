from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from src.scraper.company_careers.adapters import smartrecruiters
from src.scraper.company_careers.models import (
    AdapterResult,
    CompanyTarget,
    InventoryItem,
    ItemFailure,
    JobRecord,
    SourceScanMetadata,
)
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_health import finalize_source_scan


class Response:
    status_code = 200
    content = b"{}"
    text = ""
    headers = {"content-type": "application/json"}

    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        pass


def target(*, total: int, hydrate: bool) -> CompanyTarget:
    return CompanyTarget.from_dict(
        {
            "name": "Example Group",
            "platform": "smartrecruiters",
            "slug": "ExampleGroup",
            "careers_url": "https://careers.example.test/jobs",
            "endpoint_url": (
                "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings"
            ),
            "endpoint_variant": "companies_postings_v1",
            "identity_regex": r"^Example Group$",
            "allowed_apply_hosts": ["jobs.smartrecruiters.com"],
            "page_size": 100,
            "max_pages": 10,
            "max_jobs": total,
            "selective_detail_enrichment": hydrate,
        }
    )


def item(index: int):
    job_id = str(744000000000000 + index)
    return {
        "id": job_id,
        "name": f"Software Engineer {index}",
        "refNumber": f"REF-{index}",
        "company": {"identifier": "ExampleGroup", "name": "Example Group"},
        "releasedDate": "2026-08-10T10:43:36.683Z",
        "location": {"fullLocation": "Calgary, AB, Canada"},
        "department": {"label": "Engineering"},
        "function": {"label": "Software"},
        "ref": (
            "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings/"
            + job_id
        ),
    }


def listing_pages(total: int):
    rows = [item(index) for index in range(total)]
    return [
        Response(
            {
                "offset": offset,
                "limit": 100,
                "totalFound": total,
                "content": rows[offset : offset + 100],
            }
        )
        for offset in range(0, total, 100)
    ]


def detail(index: int):
    value = item(index)
    value.update(
        {
            "applyUrl": (
                "https://jobs.smartrecruiters.com/ExampleGroup/"
                f"{value['id']}-software-engineer"
            ),
            "jobAd": {"sections": {"description": {"text": "<p>Build.</p>"}}},
        }
    )
    return Response(value)


class InventoryHydrationTests(unittest.TestCase):
    @patch.object(smartrecruiters.time, "sleep", return_value=None)
    def test_service_now_shape_keeps_700_item_inventory_after_one_timeout(self, _sleep):
        total = 700
        details = []
        timeout_index = 346
        for index in range(total):
            if index == timeout_index:
                details.extend(
                    [requests.Timeout("detail timed out"), requests.Timeout("detail timed out")]
                )
            else:
                details.append(detail(index))
        session = Session([*listing_pages(total), *details])
        result = smartrecruiters.fetch_jobs(session, target(total=total, hydrate=True))

        self.assertEqual(len(result.inventory_items), total)
        self.assertEqual(len(result.jobs), total - 1)
        self.assertTrue(result.scan["scan_complete"])
        self.assertEqual(result.item_failures[0].item_id, item(timeout_index)["id"])
        self.assertEqual(result.item_failures[0].outcome, "unreadable")
        self.assertTrue(result.item_failures[0].retryable)
        self.assertEqual(len(session.calls), 7 + total + 1)

    def test_intuitive_shape_needs_only_listing_requests_for_inventory(self):
        total = 700
        session = Session(listing_pages(total))
        result = smartrecruiters.fetch_jobs(session, target(total=total, hydrate=False))

        self.assertEqual(len(result.inventory_items), total)
        self.assertEqual(len(result.jobs), total)
        self.assertEqual(result.item_failures, ())
        self.assertTrue(result.scan["scan_complete"])
        self.assertEqual(len(session.calls), 7)

    def test_phase3_servicenow_and_intuitive_canaries_are_listing_only(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "target_companies.phase3_smartrecruiters_canaries.yaml"
        )
        targets = load_target_companies(str(path))
        self.assertEqual(
            [target.name for target in targets],
            ["ServiceNow", "Intuitive Surgical"],
        )
        for candidate in targets:
            with self.subTest(company=candidate.name):
                rows = []
                for index in range(700):
                    row = item(index)
                    row["company"] = {
                        "identifier": candidate.slug,
                        "name": (
                            "ServiceNow"
                            if candidate.name == "ServiceNow"
                            else "Intuitive Surgical"
                        ),
                    }
                    row["ref"] = f"{candidate.endpoint_url}/{row['id']}"
                    rows.append(row)
                pages = [
                    Response(
                        {
                            "offset": offset,
                            "limit": 100,
                            "totalFound": 700,
                            "content": rows[offset : offset + 100],
                        }
                    )
                    for offset in range(0, 700, 100)
                ]
                session = Session(pages)
                result = smartrecruiters.fetch_jobs(session, candidate)

                self.assertFalse(candidate.selective_detail_enrichment)
                self.assertEqual(len(result.inventory_items), 700)
                self.assertEqual(len(result.jobs), 700)
                self.assertEqual(len(session.calls), 7)
                self.assertTrue(result.scan["scan_complete"])

    @patch.object(smartrecruiters.time, "sleep", return_value=None)
    def test_429_and_5xx_detail_failures_are_bounded_unreadable_outcomes(self, _sleep):
        for status in (429, 503):
            with self.subTest(status=status):
                session = Session(
                    [
                        *listing_pages(1),
                        Response({}, status=status),
                        Response({}, status=status),
                    ]
                )
                result = smartrecruiters.fetch_jobs(
                    session, target(total=1, hydrate=True)
                )
                self.assertTrue(result.scan["scan_complete"])
                self.assertEqual(result.jobs, [])
                self.assertEqual(len(result.inventory_items), 1)
                self.assertEqual(result.item_failures[0].outcome, "unreadable")
                self.assertTrue(result.item_failures[0].retryable)
                self.assertEqual(len(session.calls), 3)

    def test_authoritative_zero_inventory_needs_no_hydration(self):
        session = Session(
            [
                Response(
                    {
                        "offset": 0,
                        "limit": 100,
                        "totalFound": 0,
                        "content": [],
                    }
                )
            ]
        )
        result = smartrecruiters.fetch_jobs(session, target(total=1, hydrate=False))
        self.assertTrue(result.scan["scan_complete"])
        self.assertEqual(result.scan["total_available"], 0)
        self.assertEqual(result.inventory_items, ())
        self.assertEqual(result.jobs, [])

    @patch.object(smartrecruiters.time, "sleep", return_value=None)
    def test_later_listing_transport_failure_retains_discoveries_without_authority(self, _sleep):
        first_page = Response(
            {
                "offset": 0,
                "limit": 100,
                "totalFound": 101,
                "content": [item(index) for index in range(100)],
            }
        )
        session = Session([first_page, requests.Timeout("page 2 timed out")])

        result = smartrecruiters.fetch_jobs(
            session,
            target(total=101, hydrate=True),
        )

        self.assertEqual(len(result.jobs), 100)
        self.assertEqual(len(result.inventory_items), 100)
        self.assertFalse(result.scan["scan_complete"])
        self.assertEqual(result.scan["stop_reason"], "request_failure")
        self.assertEqual(result.item_failures, ())
        self.assertEqual(len(session.calls), 2)

    def test_central_finalization_uses_inventory_not_hydration_cardinality(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="reported_total_reached",
            total_available=2,
            authoritative_total_present=True,
            pages_requested=1,
            pages_completed=1,
            stop_reason="reported_total_reached",
            terminal_evidence_type="authoritative_total",
            terminal_evidence_value=2,
            terminal_page_observed=True,
            source_identity_status="validated",
        )
        job = JobRecord(
            job_id="100001",
            title="Engineer",
            company="Example",
            location="Calgary",
            url="https://jobs.smartrecruiters.com/Example/100001",
            updated_at="",
            source="smartrecruiters:example",
        )

        def adapter(_session, _target, _timeout):
            return AdapterResult(
                jobs=[job],
                scan=metadata,
                inventory_items=(
                    InventoryItem("100001", "smartrecruiters:example"),
                    InventoryItem("100002", "smartrecruiters:example"),
                ),
                item_failures=(
                    ItemFailure("100002", "timeout", retryable=True),
                ),
            )

        scanner = CompanyCareersScanner(targets=[], session=Session([]), parent_source_mappings=[])
        with patch.dict(
            "src.scraper.company_careers.scanner.ADAPTERS",
            {"phase3_test": adapter},
            clear=False,
        ):
            scan = scanner._scan_target(
                CompanyTarget(name="Example", platform="phase3_test"),
                keywords=None,
                locations=None,
                max_jobs_per_company=None,
                job_filter=None,
                location_filter=None,
                browser_batch=None,
                browser_batch_error=None,
            )

        self.assertTrue(scan.inventory_complete)
        self.assertFalse(scan.hydration_complete)
        self.assertEqual(scan.source_health_code, "hydration_degraded")
        self.assertTrue(scan.authoritative_for_absence)
        self.assertEqual(scan.inventory_items_collected, 2)
        self.assertEqual(scan.jobs_collected, 1)

    def test_degraded_hydration_without_validated_identity_fails_closed(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="reported_total_reached",
            total_available=2,
            authoritative_total_present=True,
            pages_requested=1,
            pages_completed=1,
            stop_reason="reported_total_reached",
            terminal_evidence_type="authoritative_total",
            terminal_evidence_value=2,
            terminal_page_observed=True,
        )

        finalize_source_scan(
            metadata,
            jobs_collected=1,
            inventory_items_collected=2,
            unreadable_items=1,
        )

        self.assertFalse(metadata["scan_complete"])
        self.assertFalse(metadata["inventory_complete"])
        self.assertEqual(metadata["scan_state"], "incomplete")
        self.assertEqual(metadata["source_health_code"], "identity_failure")

    def test_duplicate_inventory_ids_fail_closed(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="reported_total_reached",
            total_available=2,
            authoritative_total_present=True,
            pages_requested=1,
            pages_completed=1,
            stop_reason="reported_total_reached",
            terminal_evidence_type="authoritative_total",
            terminal_evidence_value=2,
            terminal_page_observed=True,
        )

        def adapter(_session, _target, _timeout):
            duplicate = InventoryItem("100001", "smartrecruiters:example")
            return AdapterResult(jobs=[], scan=metadata, inventory_items=(duplicate, duplicate))

        scanner = CompanyCareersScanner(targets=[], session=Session([]), parent_source_mappings=[])
        with patch.dict(
            "src.scraper.company_careers.scanner.ADAPTERS",
            {"duplicate_test": adapter},
            clear=False,
        ):
            scan = scanner._scan_target(
                CompanyTarget(name="Example", platform="duplicate_test"),
                keywords=None,
                locations=None,
                max_jobs_per_company=None,
                job_filter=None,
                location_filter=None,
                browser_batch=None,
                browser_batch_error=None,
            )
        self.assertFalse(scan.scan_complete)
        self.assertFalse(scan.authoritative_for_absence)


if __name__ == "__main__":
    unittest.main()
