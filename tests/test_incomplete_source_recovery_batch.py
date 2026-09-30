from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.scraper.company_careers.adapters import embedded_data, icims, smartrecruiters, static_html
from src.scraper.company_careers.listing_completion import finish_authoritative_total
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    SourceScanMetadata,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "target_companies.yaml"
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"

SMARTRECRUITERS = {
    "Avery Dennison": (473, 5, 500),
    "Canva": (233, 3, 300),
    "Western Digital": (331, 4, 400),
}
ICIMS_COMPANIES = {
    "Hexagon Autonomy & Positioning / NovAtel",
    "Kinaxis",
    "MaxLinear",
    "SECURE Energy Services",
}


class Response:
    def __init__(
        self,
        body: str,
        *,
        url: str,
        status: int = 200,
        content_type: str = "text/html",
    ) -> None:
        self.text = body
        self.content = body.encode()
        self.url = url
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.history = []


class Session:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def legacy_target(**overrides) -> CompanyTarget:
    values = dict(
        name="Legacy Example",
        platform="icims",
        tenant="example",
        board_id="example-public",
        search_url="https://careers-example.icims.com/jobs/search",
        endpoint_variant=icims.LEGACY_HTML,
        allowed_apply_hosts=["careers-example.icims.com"],
        max_pages=1,
        max_jobs=20,
        page_size=20,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def legacy_page(*job_ids: str, next_link: bool = False, no_results: bool = False) -> str:
    cards = "".join(
        (
            "<li class='iCIMS_JobCardItem'><div class='header left'>Calgary</div>"
            f"<div class='title'><a href='/jobs/{job_id}/engineer/job'>"
            f"<h3>Engineer {job_id}</h3></a></div></li>"
        )
        for job_id in job_ids
    )
    next_html = "<a class='next' href='?pr=1'>Next</a>" if next_link else ""
    empty_html = (
        "<div class='iCIMS_JobSearchNoResults'>No jobs found</div>"
        if no_results
        else ""
    )
    return (
        "<html><head><title>Careers</title></head><body>"
        f"<div class='iCIMS_JobsTable'><ul class='iCIMS_JobsList'>{cards}</ul>"
        f"{empty_html}{next_html}</div></body></html>"
    )


class IncompleteSourceRecoveryBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["target_companies"]
        repairs = yaml.safe_load(REPAIRS.read_text(encoding="utf-8"))[
            "company_careers_reliability_repairs"
        ]
        cls.raw_entries = {item["name"]: item for item in raw}
        cls.repair_entries = {item["name"]: item for item in repairs}
        cls.targets = {
            item.name: item
            for item in load_target_companies(apply_reliability_repairs=True)
        }

    def test_smartrecruiters_bounds_follow_existing_overlay_pattern(self):
        for company, (saved_total, max_pages, max_jobs) in SMARTRECRUITERS.items():
            with self.subTest(company=company):
                target = self.targets[company]
                self.assertEqual(self.raw_entries[company], self.repair_entries[company])
                self.assertEqual((target.max_pages, target.max_jobs), (max_pages, max_jobs))
                self.assertGreaterEqual(target.max_jobs, saved_total)
                self.assertGreaterEqual(
                    target.max_pages * smartrecruiters.MAX_PAGE_SIZE,
                    target.max_jobs,
                )
                smartrecruiters.validate_target(target)

                metadata = SourceScanMetadata()
                self.assertTrue(
                    finish_authoritative_total(metadata, saved_total, saved_total)
                )
                self.assertEqual(
                    metadata["scan_completeness_reason"], "reported_total_reached"
                )

    def test_smartrecruiters_ceiling_and_large_boards_stay_incomplete(self):
        metadata = SourceScanMetadata(
            scan_completeness_reason="safety_limit_reached",
            stop_reason="maximum_results_reached",
            safety_limit_reached=True,
        )
        self.assertFalse(finish_authoritative_total(metadata, 501, 500))
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        for company, expected in {"Bosch": (1, 20), "SGS": (1, 20)}.items():
            target = self.targets[company]
            self.assertEqual((target.max_pages, target.max_jobs), expected)

    @patch.object(icims.time, "sleep", return_value=None)
    def test_icims_contiguous_indexed_terminal_empty_page_is_authoritative(self, _sleep):
        session = Session(
            Response(
                legacy_page("101", next_link=True),
                url="https://careers-example.icims.com/jobs/search?pr=0",
            ),
            Response(
                legacy_page(),
                url="https://careers-example.icims.com/jobs/search?pr=1",
            ),
        )
        jobs = icims.fetch_jobs(session, legacy_target())
        metadata = source_scan_metadata(session)
        self.assertEqual([job.raw_metadata["external_job_id"] for job in jobs], ["101"])
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"], "validated_terminal_empty_page"
        )
        self.assertEqual(metadata["terminal_evidence_type"], "terminal_page")

    def test_icims_first_page_empty_requires_explicit_zero_contract(self):
        valid = Session(
            Response(
                legacy_page(no_results=True),
                url="https://careers-example.icims.com/jobs/search?pr=0",
            )
        )
        self.assertEqual(icims.fetch_jobs(valid, legacy_target()), [])
        self.assertTrue(source_scan_metadata(valid)["scan_complete"])

        unproven = Session(
            Response(
                legacy_page(),
                url="https://careers-example.icims.com/jobs/search?pr=0",
            )
        )
        with self.assertRaisesRegex(AdapterError, "did not prove an empty"):
            icims.fetch_jobs(unproven, legacy_target())

    @patch.object(icims.time, "sleep", return_value=None)
    def test_icims_terminal_empty_page_safety_guards(self, _sleep):
        cases = {
            "continuation": Response(
                legacy_page(next_link=True),
                url="https://careers-example.icims.com/jobs/search?pr=1",
            ),
            "pagination_gap": Response(
                legacy_page(),
                url="https://careers-example.icims.com/jobs/search?pr=2",
            ),
            "malformed_shell": Response(
                "<html><head><title>Careers</title></head><body></body></html>",
                url="https://careers-example.icims.com/jobs/search?pr=1",
            ),
        }
        for name, terminal_response in cases.items():
            with self.subTest(name=name):
                session = Session(
                    Response(
                        legacy_page("101", next_link=True),
                        url="https://careers-example.icims.com/jobs/search?pr=0",
                    ),
                    terminal_response,
                )
                icims.fetch_jobs(session, legacy_target())
                metadata = source_scan_metadata(session)
                self.assertFalse(metadata["scan_complete"])
                self.assertEqual(
                    metadata["stop_reason"], "empty_page_without_terminal_contract"
                )

        blocked = Session(
            Response(
                legacy_page("101", next_link=True),
                url="https://careers-example.icims.com/jobs/search?pr=0",
            ),
            Response(
                "<html><body>Access denied</body></html>",
                url="https://careers-example.icims.com/jobs/search?pr=1",
            ),
        )
        with self.assertRaisesRegex(AdapterError, "blocked"):
            icims.fetch_jobs(blocked, legacy_target())

    @patch.object(icims.time, "sleep", return_value=None)
    def test_icims_repeated_page_is_never_terminal(self, _sleep):
        first = legacy_page("101", next_link=True)
        session = Session(
            Response(first, url="https://careers-example.icims.com/jobs/search?pr=0"),
            Response(first, url="https://careers-example.icims.com/jobs/search?pr=1"),
        )
        with self.assertRaisesRegex(AdapterError, "repeated a complete page"):
            icims.fetch_jobs(session, legacy_target())
        self.assertFalse(source_scan_metadata(session)["scan_complete"])

    def test_target_icims_boards_share_the_legacy_numbered_page_contract(self):
        for company in ICIMS_COMPANIES:
            with self.subTest(company=company):
                target = self.targets[company]
                self.assertEqual(target.endpoint_variant, icims.LEGACY_HTML)
                self.assertTrue(target.search_url.endswith("/jobs/search") or "/jobs/search?" in target.search_url)
                icims.validate_target(target)

    def test_sanctuary_uses_the_reviewed_kula_structured_collection(self):
        target = self.targets["Sanctuary AI"]
        self.assertEqual(target.platform, "javascript_embedded_data")
        row = {
            "id": 33158,
            "account_id": 1484,
            "title": "Robotics Engineer",
            "launch_at": "2026-09-29T00:00:00Z",
            "ats_job": {
                "offices": [{"name": "Vancouver"}],
                "ats_department": {"name": "Robotics"},
            },
        }
        stream = '1:["$",{"jobs":' + json.dumps([row]) + "}]"
        wrapper = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        session = Session(
            Response(
                f"<html><script>{wrapper}</script></html>",
                url=str(target.listing_url),
            )
        )
        jobs = embedded_data.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual([item.raw_metadata["external_job_id"] for item in jobs], ["33158"])
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "complete_structured_collection",
        )

    def test_sanctuary_malformed_structured_row_is_not_hidden(self):
        target = self.targets["Sanctuary AI"]
        row = {
            "id": None,
            "account_id": 1484,
            "title": "Engineer",
            "launch_at": "2026-09-29T00:00:00Z",
            "ats_job": {
                "offices": [{"name": "Vancouver"}],
                "ats_department": {"name": "Robotics"},
            },
        }
        stream = '1:["$",{"jobs":' + json.dumps([row]) + "}]"
        wrapper = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        with self.assertRaisesRegex(AdapterError, "stable identifier"):
            embedded_data.fetch_jobs(
                Session(Response(f"<script>{wrapper}</script>", url=str(target.listing_url))),
                target,
            )

    def test_sanctuary_wrong_kula_account_fails_closed(self):
        target = self.targets["Sanctuary AI"]
        row = {"id": 33158, "account_id": 9999, "title": "Engineer"}
        stream = '1:["$",{"jobs":' + json.dumps([row]) + "}]"
        wrapper = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        with self.assertRaisesRegex(AdapterError, "attribution"):
            embedded_data.fetch_jobs(
                Session(Response(f"<script>{wrapper}</script>", url=str(target.listing_url))),
                target,
            )


if __name__ == "__main__":
    unittest.main()
