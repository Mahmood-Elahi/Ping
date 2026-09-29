from __future__ import annotations

import json
import re
import unittest

from lxml import html

from src.scraper.company_careers.adapters import (
    browser_rendered_html,
    embedded_data,
    oracle_recruiting,
    public_json,
    static_html,
    uber,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(
        self,
        body,
        *,
        url="https://jobs.example.test/",
        content_type="text/html",
        status_code=200,
    ):
        self.payload = body if isinstance(body, (dict, list)) else None
        self.content = (
            json.dumps(body).encode() if self.payload is not None else str(body).encode()
        )
        self.text = self.content.decode()
        self.url = url
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.history = []

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
            raise AssertionError("unexpected GET")
        response = self.responses.pop(0)
        if response.url == "https://jobs.example.test/":
            response.url = url
        return response


def uber_row(index: int) -> dict:
    job_id = str(300000 + index)
    return {
        "Id": job_id,
        "Reference": job_id,
        "Title": f"Engineer {index}",
        "Urls": [{"IsDefault": True, "Url": f"/en/jobs/{job_id}/"}],
        "Locations": [{"Address": "Calgary, Canada"}],
        "Teams": ["Engineering"],
        "DisplayDate": "2026-09-29",
    }


def uber_page(rows: list[dict], *, page: int, total: int) -> dict:
    return {
        "jobs": rows,
        "totalPages": (total + uber.PAGE_SIZE - 1) // uber.PAGE_SIZE,
        "totalJobs": total,
        "page": page,
        "pageSize": uber.PAGE_SIZE,
    }


class TwentySourceRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    def test_effective_registry_uses_only_proven_migrations(self):
        expected = {
            "ClickHouse": "ashby",
            "Zoom": "public_json",
            "CN Rail": "sap_successfactors",
            "Dell Technologies": "oracle_recruiting",
            "Goldman Sachs": "browser_rendered_html",
            "Lightspeed Commerce": "static_html",
            "MediaTek": "browser_rendered_html",
            "Stantec": "browser_rendered_html",
            "Renesas Electronics": "smartrecruiters",
            "Yaskawa Electric": "static_html",
            "Uber": "uber",
            "IMC Trading": "javascript_embedded_data",
            "Mercedes-Benz": "beesite",
            "Porsche": "beesite",
        }
        self.assertEqual(
            {name: self.targets[name].platform for name in expected}, expected
        )
        self.assertEqual(self.targets["Magic Leap"].platform, "greenhouse")
        self.assertEqual(self.targets["ClickHouse"].slug, "clickhouse")
        self.assertEqual(self.targets["Zoom"].site_id, "a5bcebfa-981b-46cd-b46f-ad91a50505b8")
        self.assertEqual(self.targets["CN Rail"].endpoint_variant, "rmk_columnized")
        self.assertEqual(self.targets["Dell Technologies"].site_number, "CX_1001")
        self.assertEqual(
            self.targets["Dell Technologies"].allowed_source_hosts,
            ["enterpriseplatform.dell.com"],
        )
        self.assertEqual(self.targets["Renesas Electronics"].slug, "RenesasElectronics")

    def test_browser_identity_selectors_bind_one_title_and_stable_id(self):
        fixtures = {
            "Goldman Sachs": (
                "<a href='/roles/137769'><span class='gs-uitk-mb-1'>Engineer</span></a>",
                "137769",
            ),
            "MediaTek": (
                "<a href='/en/jobs/MUS1234/engineer'><h1 class='h3'>Engineer</h1></a>",
                "MUS1234",
            ),
            "Stantec": (
                "<main><a href='/calgary/engineer/0DAF2C5A18E24979AC60FFC7FC5D63F2/job/'>"
                "<h2>Engineer</h2><p>Team</p><p>Calgary</p></a></main>",
                "0DAF2C5A18E24979AC60FFC7FC5D63F2",
            ),
        }
        for name, (markup, external_id) in fixtures.items():
            with self.subTest(name=name):
                target = browser_rendered_html._static_target(self.targets[name])
                root = html.fromstring(f"<html><body>{markup}</body></html>")
                containers = static_html.select(root, str(target.listing_selector))
                cards = static_html.select(root, str(target.job_card_selector))
                records = static_html._listing_records(
                    containers, cards, target, str(target.listing_url)
                )
                self.assertEqual(
                    (len(containers), len(cards), len(records)),
                    (1, 1, 1),
                )
                jobs = [
                    static_html._normalize(record, target, str(target.listing_url))
                    for record in records
                ]
                self.assertEqual(len(jobs), 1)
                self.assertTrue(jobs[0].job_id.endswith(f":{external_id}"))
                self.assertEqual(jobs[0].title, "Engineer")

    def test_lightspeed_uuid_and_yaskawa_official_http_upgrade(self):
        lightspeed = self.targets["Lightspeed Commerce"]
        markup = (
            "<html><body><ul class='filter__results__list'><li class='job'>"
            "<a href='/careers/job/engineer/678c4b79-5c18-4066-953b-2ee40e0d2741/"
            "?office=Toronto'><span class='job-title'>Engineer</span>"
            "<span class='job-office'>Toronto</span><span class='job-dept'>Engineering</span>"
            "</a></li></ul></body></html>"
        )
        jobs = static_html.fetch_jobs(
            Session(Response(markup, url=lightspeed.listing_url)), lightspeed
        )
        self.assertEqual(len(jobs), 1)
        self.assertIn("678c4b79-5c18-4066-953b-2ee40e0d2741", jobs[0].job_id)

        yaskawa = self.targets["Yaskawa Electric"]
        upgraded = static_html._official_url(
            "http://recruiting.ultipro.com/YAS1000YAAMI/JobBoard/board/"
            "OpportunityDetail?opportunityId=f2b1b1af-27a1-4ac6-9bc3-87a66747a634",
            yaskawa,
        )
        self.assertTrue(upgraded.startswith("https://recruiting.ultipro.com/"))
        with self.assertRaises(AdapterError):
            static_html._official_url("http://evil.test/job/1", yaskawa)

    def test_imc_flight_collection_ignores_unrelated_wrappers_and_filters_us(self):
        target = self.targets["IMC Trading"]
        unrelated = "self.__next_f.push(" + json.dumps([1, '1:{"other":[]}']) + ")"
        metadata = [
            {"name": "one", "value": None},
            {"name": "two", "value": None},
            {"name": "three", "value": None},
            {"name": "Career Site Locale", "value": "us"},
        ]
        us_job = {
            "id": 4823923101,
            "title": "Quantitative Trader Intern",
            "updated_at": "2026-09-29",
            "metadata": metadata,
            "offices": [{"name": "Chicago"}],
            "departments": [{"name": "Trading"}],
        }
        eu_job = {**us_job, "id": 4823923102, "metadata": [*metadata[:3], {"name": "Career Site Locale", "value": "eu"}]}
        stream = '2:["$",null,null,{"jobs":' + json.dumps([us_job, eu_job]) + "}]"
        matching = "self.__next_f.push(" + json.dumps([1, stream]) + ")"
        page = "<html><body>" + "".join(
            f"<script>{wrapper}</script>" for wrapper in [unrelated] * 25 + [matching]
        ) + "</body></html>"
        session = Session(Response(page, url=target.listing_url))

        jobs = embedded_data.fetch_jobs(session, target)
        metadata_result = source_scan_metadata(session)

        self.assertEqual([job.job_id.rsplit(":", 1)[-1] for job in jobs], ["4823923101"])
        self.assertTrue(metadata_result["scan_complete"])
        self.assertEqual(metadata_result["filtered_collection_total"], 1)

    def test_uber_api_requires_exact_pagination_and_reaches_total(self):
        first = [uber_row(index) for index in range(10)]
        second = [uber_row(10)]
        session = Session(
            Response(uber_page(first, page=1, total=11), content_type="application/json"),
            Response(uber_page(second, page=2, total=11), content_type="application/json"),
        )
        jobs = uber.fetch_jobs(session, self.targets["Uber"])
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 11)
        self.assertEqual(len({job.job_id for job in jobs}), 11)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 11)
        self.assertTrue(all(call[0] == uber.SEARCH_API_URL for call in session.calls))

        malformed = uber_page(first, page=1, total=11)
        malformed["page"] = 2
        with self.assertRaisesRegex(AdapterError, "pagination contract"):
            uber.fetch_jobs(
                Session(Response(malformed, content_type="application/json")),
                self.targets["Uber"],
            )

    def test_zoom_requires_official_embed_and_exact_advertiser(self):
        target = self.targets["Zoom"]
        identity = Response(
            f"<html><title>Zoom Careers</title>{target.site_id}</html>",
            url=target.careers_url,
        )
        row = {
            "data": {
                "jobID": "293de5d3-21cd-40ce-ad6d-f42505afca61",
                "referenceNumber": "R1001",
                "jobName": "Engineer",
                "activeStatus": True,
                "siteID": target.site_id,
                "advertiserName": "Zoom",
                "siteName": "Zoom Careers",
                "jobURL": "https://careers.zoom.com/job-details/engineer-jobs-1001",
                "city": "Calgary",
                "country": "Canada",
            }
        }
        session = Session(
            identity,
            Response([row], url=target.endpoint_url, content_type="application/json"),
        )
        jobs = public_json.fetch_jobs(session, target)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].url.startswith("https://www.careers.zoom.us/"))
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

        wrong = json.loads(json.dumps(row))
        wrong["data"]["advertiserName"] = "Other"
        with self.assertRaisesRegex(AdapterError, "advertiser attribution"):
            public_json.fetch_jobs(
                Session(
                    identity,
                    Response([wrong], url=target.endpoint_url, content_type="application/json"),
                ),
                target,
            )

    def test_oracle_vanity_host_and_beesite_attribution_are_narrow(self):
        oracle_recruiting.validate_target(self.targets["Dell Technologies"])
        invalid = CompanyTarget.from_dict(
            {
                **self.targets["Dell Technologies"].__dict__,
                "endpoint_url": "https://evil.test/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            }
        )
        with self.assertRaisesRegex(AdapterError, "supported public Oracle endpoint"):
            oracle_recruiting.validate_target(invalid)

        same_host_without_explicit_allowance = CompanyTarget.from_dict(
            {
                **self.targets["Dell Technologies"].__dict__,
                "careers_url": (
                    "https://unreviewed.example/"
                    "hcmUI/CandidateExperience/en/sites/careers/jobs"
                ),
                "endpoint_url": (
                    "https://unreviewed.example/"
                    "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                ),
                "allowed_source_hosts": [],
            }
        )
        with self.assertRaisesRegex(AdapterError, "supported public Oracle endpoint"):
            oracle_recruiting.validate_target(same_host_without_explicit_allowance)

        mercedes = re.compile(self.targets["Mercedes-Benz"].job_attribution_regex)
        for value in ("Mercedes-Benz AG", "Mercedes-AMG GmbH", "MHP USA", "Athlon Germany"):
            if value.startswith("MHP"):
                continue
            self.assertRegex(value, mercedes)
        self.assertNotRegex("Unrelated Motors", mercedes)
        porsche = re.compile(self.targets["Porsche"].job_attribution_regex)
        for value in ("Porsche AG", "MHP USA", "V4Smart GmbH"):
            self.assertRegex(value, porsche)
        self.assertNotRegex("Unrelated Motors", porsche)


if __name__ == "__main__":
    unittest.main()
