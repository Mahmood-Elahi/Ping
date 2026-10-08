from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import yaml

from scripts.project_completeness_migration import build_projection
from scripts.validate_targeted_terminal_evidence import representative_targets
from src.scraper.company_careers.adapters import (
    browser_rendered_html,
    greenhouse,
    static_html,
    ukg_pro,
)
from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_health import finalize_source_scan


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "target_companies.yaml"
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"
RECOVERED = {
    "Aurora Innovation": "public_json",
    "Deliveroo": "public_json",
    "Figure AI": "greenhouse",
    "MDA Space": "ukg_pro",
    "LINE": "javascript_embedded_data",
    "ByteDance": "first_party_json",
}
UNRESOLVED = {
    "Stantec",
}


class Response:
    def __init__(self, value: object, *, content_type: str = "application/json") -> None:
        self.status_code = 200
        self._value = value
        self.text = value if isinstance(value, str) else json.dumps(value)
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.url = "https://jobs.example.test"

    def json(self):
        return self._value


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(("get", url))
        return self.responses.pop(0)

    def post(self, url: str, **_kwargs):
        self.calls.append(("post", url))
        return self.responses.pop(0)


def configured_targets():
    return {
        target.name: target
        for target in load_target_companies(apply_reliability_repairs=True)
        if target.name in set(RECOVERED) | UNRESOLVED
    }


class SevenBrowserSourceRecoveryTests(unittest.TestCase):
    def test_only_sources_with_independent_collection_contracts_are_migrated(self):
        targets = configured_targets()

        self.assertEqual(
            {name: targets[name].platform for name in RECOVERED}, RECOVERED
        )
        self.assertEqual(
            {targets[name].platform for name in UNRESOLVED},
            {"browser_rendered_html"},
        )
        self.assertTrue(all(not targets[name].single_page_collection for name in UNRESOLVED))

    def test_canonical_registry_and_repair_overlay_describe_the_same_migrations(self):
        registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["target_companies"]
        repairs = yaml.safe_load(REPAIRS.read_text(encoding="utf-8"))[
            "company_careers_reliability_repairs"
        ]
        canonical = {item["name"]: item for item in registry}
        overlays = {item["name"]: item for item in repairs}

        for name, platform in RECOVERED.items():
            with self.subTest(company=name):
                self.assertEqual(canonical[name], overlays[name])
                self.assertEqual(canonical[name]["platform"], platform)

    def test_saved_diagnostics_classify_adapter_migrations_without_reopening_them(self):
        projection = build_projection()

        expected_migrations = {
                "Aurora Innovation: browser_rendered_html -> public_json",
                "Figure AI: browser_rendered_html -> greenhouse",
                "IMC Trading: static_html -> javascript_embedded_data",
                "LINE: browser_rendered_html -> javascript_embedded_data",
                "MDA Space: browser_rendered_html -> ukg_pro",
                "Renesas Electronics: static_html -> smartrecruiters",
                "Zoom: static_html -> public_json",
        }
        self.assertTrue(
            expected_migrations.issubset(
                set(projection["summary"]["active_contract_migrations"])
            )
        )
        self.assertGreaterEqual(
            projection["summary"]["migrated_to_stronger_active_contract"], 7
        )
        browser_representatives = {
            target.name
            for target in representative_targets()
            if target.platform == "browser_rendered_html"
        }
        self.assertEqual(browser_representatives, {"Stantec"})

    def test_figure_ai_uses_retained_exact_greenhouse_board_and_complete_response(self):
        target = configured_targets()["Figure AI"]
        session = Session(
            [
                Response(
                    {
                        "jobs": [
                            {
                                "id": 4007375006,
                                "title": "Robotics Engineer",
                                "location": {"name": "Sunnyvale, CA"},
                                "absolute_url": (
                                    "https://job-boards.greenhouse.io/figureai/"
                                    "jobs/4007375006"
                                ),
                                "updated_at": "2026-09-24T00:00:00Z",
                                "content": "Build humanoid robots.",
                                "departments": [{"name": "Engineering"}],
                                "offices": [{"name": "Sunnyvale"}],
                            }
                        ]
                    }
                )
            ]
        )

        jobs = greenhouse.fetch_jobs(session, target)
        metadata = finalize_source_scan(
            source_scan_metadata(session), jobs_collected=len(jobs)
        )

        self.assertEqual(target.slug, "figureai")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            session.calls,
            [("get", "https://boards-api.greenhouse.io/v1/boards/figureai/jobs")],
        )
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["terminal_evidence_type"], "complete_collection_response")
        self.assertEqual(metadata["scan_state"], "complete")

    def test_mda_ukg_total_count_is_explicit_authoritative_terminal_evidence(self):
        target = configured_targets()["MDA Space"]
        board_id = "664818ff-3594-4bec-9f30-3394e59e19f3"
        board = {
            "Id": board_id,
            "Name": "MDA Space",
            "HasThirdPartyPostings": False,
            "OpportunityDetailUrl": "",
        }
        opportunity_id = "2d92492e-cdf4-4978-b491-de3add58834e"
        session = Session(
            [
                Response(
                    f"<html><script>jobBoard: {json.dumps(board)}, "
                    "isAuthenticated: false</script></html>",
                    content_type="text/html",
                ),
                Response(
                    {
                        "opportunities": [
                            {
                                "Id": opportunity_id,
                                "Title": "Satellite Systems Engineer",
                                "RequisitionNumber": "MDA-1",
                                "FullTime": True,
                                "JobCategoryName": "Engineering",
                                "Locations": [],
                                "PostedDate": "2026-09-24T00:00:00Z",
                                "BriefDescription": "Build space systems.",
                                "JobLocationType": 1,
                            }
                        ],
                        "totalCount": 1,
                    }
                ),
            ]
        )

        jobs = ukg_pro.fetch_jobs(session, target)
        metadata = finalize_source_scan(
            source_scan_metadata(session), jobs_collected=len(jobs)
        )

        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["authoritative_total_present"])
        self.assertEqual(metadata["total_available"], 1)
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")
        self.assertEqual(metadata["terminal_evidence_value"], 1)
        self.assertEqual(metadata["has_more"], False)
        self.assertEqual(metadata["scan_state"], "complete")

    def test_one_render_without_pagination_is_not_terminal_evidence(self):
        target = configured_targets()["Stantec"]
        target = replace(target, pagination_url_template=None, max_pages=1)
        body = (
            "<!doctype html><html><head><title>Stantec careers</title></head><body>"
            "<main><a href='/en/job/0123456789ABCDEF0123456789ABCDEF/job'>"
            "<h2>Engineer</h2><p>Engineering</p><p>Calgary</p></a></main>"
            "</body></html>"
        )
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/html"},
            url=target.listing_url,
            history=[],
        )

        jobs = static_html.fetch_jobs(
            session, browser_rendered_html._static_target(target)
        )
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(target.browser_scroll_steps, 0)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "no_pagination_configured")
        self.assertIsNone(metadata["terminal_evidence_type"])


if __name__ == "__main__":
    unittest.main()
