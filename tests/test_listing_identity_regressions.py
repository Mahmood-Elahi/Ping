from __future__ import annotations

import unittest

from src.scraper.company_careers.adapters import browser_rendered_html, static_html
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(self, body: str, url: str):
        self.content = body.encode()
        self.status_code = 200
        self.url = url
        self.headers = {"content-type": "text/html"}
        self.history = []


class Session:
    def __init__(self, body: str):
        self.body = body

    def get(self, url, **_kwargs):
        return Response(self.body, url)


class ListingIdentityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = {target.name: target for target in load_target_companies()}

    def parse(self, name: str, body: str):
        configured = self.targets[name]
        target = (
            browser_rendered_html._static_target(configured)
            if configured.platform == "browser_rendered_html"
            else configured
        )
        return static_html.fetch_jobs(Session(body), target)

    def test_goldman_sachs_equivalent_nested_titles(self):
        body = (
            "<html><body><a href='/roles/12345'>"
            "<div><span class='gs-text'>Quantitative Engineer</span></div>"
            "<div><span class='gs-text'> Quantitative   Engineer </span></div>"
            "</a></body></html>"
        )
        jobs = self.parse("Goldman Sachs", body)
        self.assertEqual([(job.title, job.raw_metadata["external_job_id"]) for job in jobs], [("Quantitative Engineer", "12345")])

    def test_sanctuary_broad_region_splits_into_link_bound_records(self):
        body = (
            "<html><body><div role='region'>"
            "<article><p>Robotics Engineer</p><a href='/sanctuary-ai/1001/'>Apply</a></article>"
            "<article><p>Controls Engineer</p><a href='/sanctuary-ai/1002/'>Apply</a></article>"
            "</div></body></html>"
        )
        jobs = self.parse("Sanctuary AI", body)
        self.assertEqual(
            [(job.title, job.raw_metadata["external_job_id"]) for job in jobs],
            [("Robotics Engineer", "1001"), ("Controls Engineer", "1002")],
        )

    def test_imperial_oil_equivalent_duplicate_links_in_row(self):
        body = (
            "<html><body><div id='searchresults'><table>"
            "<tr class='data-row'><td>"
            "<a class='jobTitle-link' href='/Imperial/job/Calgary-Engineer/12345/'>Engineer</a>"
            "<a class='jobTitle-link' href='/Imperial/job/Calgary-Engineer/12345/'> Engineer </a>"
            "</td><td class='colLocation'><span class='jobLocation'>Calgary</span></td>"
            "<td class='colDate'><span class='jobDate'>2026-09-01</span></td></tr>"
            "</table></div></body></html>"
        )
        jobs = self.parse("Imperial Oil", body)
        self.assertEqual(
            [(job.title, job.location, job.raw_metadata["external_job_id"]) for job in jobs],
            [("Engineer", "Calgary", "12345")],
        )

    def test_deliveroo_link_binds_to_associated_heading(self):
        body = (
            "<html><body><section>"
            "<article><h4>Software Engineer</h4>"
            "<a href='/role/software-engineer-a1b2c3d4e5f6'>View role</a></article>"
            "<article><h4>Data Engineer</h4>"
            "<a href='/role/data-engineer-123456abcdef'>View role</a></article>"
            "</section></body></html>"
        )
        jobs = self.parse("Deliveroo", body)
        self.assertEqual(
            [(job.title, job.raw_metadata["external_job_id"]) for job in jobs],
            [("Software Engineer", "a1b2c3d4e5f6"), ("Data Engineer", "123456abcdef")],
        )


if __name__ == "__main__":
    unittest.main()
