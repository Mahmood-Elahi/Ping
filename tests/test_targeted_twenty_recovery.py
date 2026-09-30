from __future__ import annotations

import json
import unittest

from src.scraper.company_careers.adapters import (
    embedded_data,
    icims,
    public_json,
    talentbrew,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(self, body, *, url: str, content_type: str = "text/html", status=200):
        self.status_code = status
        self.url = url
        self.headers = {"content-type": content_type}
        self.payload = body if isinstance(body, (dict, list)) else None
        self.content = (
            json.dumps(body).encode("utf-8")
            if self.payload is not None
            else str(body).encode("utf-8")
        )
        self.text = self.content.decode("utf-8")

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.responses.pop(0)


def targets():
    return {
        target.name: target
        for target in load_target_companies(apply_reliability_repairs=True)
    }


class RegistryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = targets()

    def test_proven_migrations_and_same_platform_repairs(self):
        expected = {
            "Sanctuary AI": "javascript_embedded_data",
            "General Motors": "static_html",
            "Imperial Oil": "static_html",
            "Snap": "javascript_embedded_data",
            "Unitree Robotics": "static_html",
            "Valve": "public_json",
            "iRobot": "talentbrew",
            "Hexagon Autonomy & Positioning / NovAtel": "icims",
        }
        self.assertEqual(
            {name: self.targets[name].platform for name in expected}, expected
        )
        self.assertIsNone(self.targets["General Motors"].next_page_selector)
        self.assertEqual(
            self.targets["Imperial Oil"].next_page_selector, "a.paginationItemLast"
        )
        self.assertEqual(self.targets["Sanctuary AI"].job_attribution_regex, "^1484$")

    def test_no_configured_company_activates_unitree_public_json(self):
        for apply_repairs in (False, True):
            with self.subTest(apply_reliability_repairs=apply_repairs):
                configured = load_target_companies(
                    apply_reliability_repairs=apply_repairs
                )
                self.assertFalse(
                    any(
                        target.endpoint_variant == public_json.UNITREE_JOBS
                        for target in configured
                    )
                )


class StructuredCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = targets()

    def test_sanctuary_kula_complete_collection_and_attribution(self):
        target = self.targets["Sanctuary AI"]
        rows = [{
            "id": 33153,
            "account_id": 1484,
            "title": "Senior Mechanical Engineer",
            "launch_at": "2026-03-11T16:34:33.000Z",
            "ats_job": {
                "ats_department": {"id": 1, "name": "Mechanical"},
                "offices": [{"name": "Vancouver"}],
            },
        }]
        stream = '1:["$",{"jobs":' + json.dumps(rows) + "}]"
        wrapper = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        session = Session(Response(f"<html><script>{wrapper}</script></html>", url=target.listing_url))
        jobs = embedded_data.fetch_jobs(session, target)
        self.assertEqual([job.raw_metadata["external_job_id"] for job in jobs], ["33153"])
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

        bad = [{**rows[0], "account_id": 9999}]
        bad_stream = '1:["$",{"jobs":' + json.dumps(bad) + "}]"
        bad_wrapper = "self.__next_f.push(" + json.dumps([1, bad_stream]) + ")"
        with self.assertRaisesRegex(AdapterError, "attribution"):
            embedded_data.fetch_jobs(
                Session(Response(f"<script>{bad_wrapper}</script>", url=target.listing_url)),
                target,
            )

    def test_snap_unfiltered_cache_is_atomic_and_fail_closed(self):
        target = self.targets["Snap"]
        row = {
            "_index": "jobs_2026-09-29",
            "_id": "R0046868",
            "_source": {
                "External_Posting": "1",
                "id": "R0046868",
                "jobPostingSite": "Snap Inc.",
                "title": "Workday Configuration Analyst",
                "offices": [{"name": "Los Angeles"}],
                "departments": "People",
            },
        }
        key = 'jobs--{"location":[],"role":[],"team":[],"type":[]}'
        payload = {key: {"data": {"body": [row], "aggs": {}}, "expiryTime": 1}}
        page = (
            "<html><script>window.ASYNC_DATA_CONTROLLER_CACHE = "
            + json.dumps(payload)
            + "; window.APP_STATE = {};</script></html>"
        )
        session = Session(Response(page, url=target.listing_url))
        jobs = embedded_data.fetch_jobs(session, target)
        self.assertEqual(jobs[0].url, "https://careers.snap.com/job?id=R0046868")
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

        malformed = json.loads(json.dumps(payload))
        malformed[key]["data"]["body"][0]["_id"] = "different"
        bad_page = "<script>window.ASYNC_DATA_CONTROLLER_CACHE = " + json.dumps(malformed) + ";</script>"
        with self.assertRaisesRegex(AdapterError, "identity"):
            embedded_data.fetch_jobs(
                Session(Response(bad_page, url=target.listing_url)), target
            )

        unterminated = (
            "<script>window.ASYNC_DATA_CONTROLLER_CACHE = "
            + json.dumps(payload)
            + " window.APP_STATE = {};</script>"
        )
        with self.assertRaisesRegex(AdapterError, "trailing data"):
            embedded_data.fetch_jobs(
                Session(Response(unterminated, url=target.listing_url)), target
            )

    def test_unitree_counted_api_and_wrong_count_fail_closed(self):
        target = CompanyTarget(
            name="Unitree Robotics future adapter fixture",
            platform="public_json",
            careers_url="https://www.unitree.com/position/",
            endpoint_url="https://api.unitree.com/website/job/list?perPage=50",
            endpoint_variant=public_json.UNITREE_JOBS,
            allowed_source_hosts=["api.unitree.com"],
            allowed_apply_hosts=["www.unitree.com"],
            max_pages=1,
            max_jobs=20,
            max_response_bytes=3_000_000,
        )
        identity = Response(
            '<html>Unitree Robotics<script>apiBase:"https://api.unitree.com/website"</script></html>',
            url=target.careers_url,
        )
        row = {"id": "2060189838933491712", "title": "Robotics Engineer", "postTime": "2026/06/01"}
        api = Response(
            {"code": 100, "data": {"count": 1, "items": [row]}},
            url=target.endpoint_url,
            content_type="application/json",
        )
        session = Session(identity, api)
        jobs = public_json.fetch_jobs(session, target)
        self.assertEqual(jobs[0].job_id, "public_json:api.unitree.com:2060189838933491712")
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

        bad = Response(
            {"code": 100, "data": {"count": 2, "items": [row]}},
            url=target.endpoint_url,
            content_type="application/json",
        )
        with self.assertRaisesRegex(AdapterError, "count"):
            public_json.fetch_jobs(Session(identity, bad), target)

    def test_valve_unfiltered_id_keyed_api_and_conflict_fail_closed(self):
        target = self.targets["Valve"]
        identity = Response(
            "<html><title>Valve Corporation</title>"
            "<script src='/en/public/javascript/jobs.js?v=1'></script></html>",
            url=target.careers_url,
        )
        script = Response(
            "fetch('https://www.valvesoftware.com/en/jobs/job-search?search=')",
            url="https://www.valvesoftware.com/en/public/javascript/jobs.js?v=1",
        )
        api = Response(
            {"2": {"reqid": 2, "name": "3D Character Artist", "description": "Role"}},
            url=target.endpoint_url,
            content_type="application/json",
        )
        session = Session(identity, script, api)
        jobs = public_json.fetch_jobs(session, target)
        self.assertEqual(jobs[0].url, "https://www.valvesoftware.com/en/jobs?job_id=2")
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

        conflicting = Response(
            {"2": {"reqid": 3, "name": "Artist"}},
            url=target.endpoint_url,
            content_type="application/json",
        )
        with self.assertRaisesRegex(AdapterError, "conflicts"):
            public_json.fetch_jobs(Session(identity, script, conflicting), target)


class HtmlTerminalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = targets()

    def test_talentbrew_server_metadata_completes_irobot(self):
        target = self.targets["iRobot"]
        card = (
            "<a href='/job/tokyo/account-manager/42682/100427657728' "
            "data-job-id='100427657728'><h2>Account Manager</h2>"
            "<span class='job-location'>Tokyo, Tokyo</span></a>"
        )
        page = (
            "<html><title>iRobot Careers</title><body>iRobot"
            "<section id='search-results' data-total-results='1' "
            "data-total-job-results='1' data-total-pages='1' "
            "data-current-page='1' data-records-per-page='10'>"
            f"<div id='search-results-list'>{card}</div></section></body></html>"
        )
        session = Session(Response(page, url=target.listing_url))
        jobs = talentbrew.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 1)

        contradictory = page.replace("data-total-results='1'", "data-total-results='2'")
        bad_session = Session(Response(contradictory, url=target.listing_url))
        talentbrew.fetch_jobs(bad_session, target)
        self.assertFalse(source_scan_metadata(bad_session)["scan_complete"])

    @staticmethod
    def _legacy_root(current: int, total: int, *, next_link: bool = False):
        from lxml import html

        links = "".join(
            "<a class='selected' href='?pr=%d'><span>Page</span>%d"
            "<span>of %d, Current Page</span></a>" % (page - 1, page, total)
            if page == current
            else "<a href='?pr=%d'><span>Page</span>%d<span>of %d</span></a>"
            % (page - 1, page, total)
            for page in range(1, total + 1)
        )
        continuation = "<a class='next' href='?pr=3'>Next</a>" if next_link else ""
        return html.fromstring(f"<nav>{links}{continuation}</nav>")

    def test_legacy_icims_page_before_final_has_more_without_next_control(self):
        self.assertTrue(icims._legacy_has_more(self._legacy_root(1, 3), 0))

    def test_legacy_icims_final_page_without_continuation_is_terminal(self):
        self.assertFalse(icims._legacy_has_more(self._legacy_root(3, 3), 2))

    def test_legacy_icims_final_page_with_next_control_fails_closed(self):
        with self.assertRaisesRegex(AdapterError, "continuation control"):
            icims._legacy_has_more(self._legacy_root(3, 3, next_link=True), 2)

    def test_legacy_icims_selected_page_must_match_requested_page(self):
        with self.assertRaisesRegex(AdapterError, "contradicted"):
            icims._legacy_has_more(self._legacy_root(2, 3), 0)

    def test_legacy_icims_multiple_selected_pages_fail_closed(self):
        from lxml import html

        multiple = html.fromstring(
            "<nav>"
            "<a class='selected' href='?pr=0'>Page 1 of 3, Current Page</a>"
            "<a class='selected' href='?pr=0'>Page 1 of 3, Current Page</a>"
            "</nav>"
        )
        with self.assertRaisesRegex(AdapterError, "multiple current pages"):
            icims._legacy_has_more(multiple, 0)

    def test_legacy_icims_malformed_selected_page_fails_closed(self):
        from lxml import html

        malformed = html.fromstring(
            "<nav><a class='selected' href='?pr=0'>Current page: one of three</a></nav>"
        )
        with self.assertRaisesRegex(AdapterError, "malformed"):
            icims._legacy_has_more(malformed, 0)

    def test_legacy_icims_hexagon_request_params(self):
        params = icims._page_params(
            self.targets["Hexagon Autonomy & Positioning / NovAtel"], 1
        )
        self.assertEqual(params["schemaId"], "")
        self.assertEqual(params["o"], "")


if __name__ == "__main__":
    unittest.main()
