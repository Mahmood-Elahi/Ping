from __future__ import annotations

import unittest

from src.scraper.company_careers.adapters import browser_rendered_html, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
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
    STRUCTURAL_ROW_SOURCES = {
        "Absolute Software": {
            "path": "/absolute/job/security-engineer-101",
            "external_id": "security-engineer-101",
        },
        "EPCOR": {
            "path": "/epcor/job/engineering-student-102",
            "external_id": "engineering-student-102",
        },
        "Cameco": {
            "path": "/cameco/job/process-engineer-103",
            "external_id": "process-engineer-103",
        },
        "Snap": {
            "path": "/job?id=R0041042",
            "external_id": "R0041042",
        },
    }

    @classmethod
    def setUpClass(cls):
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    def parse(self, name: str, body: str):
        configured = self.targets[name]
        target = (
            browser_rendered_html._static_target(configured)
            if configured.platform == "browser_rendered_html"
            else configured
        )
        return static_html.fetch_jobs(Session(body), target)

    @staticmethod
    def source_page(rows: str) -> str:
        return (
            "<html><head><title>Careers</title></head><body>"
            f"<table class='jv-job-list'>{rows}</table></body></html>"
        )

    @staticmethod
    def source_row(name: str, path: str, title: str = "Software Engineer") -> str:
        role = " role='row'" if name == "Snap" else ""
        return (
            f"<tr{role}><td class='jv-job-list-name'>"
            f"<a href='{path}'>{title}</a></td>"
            "<td class='jv-job-list-location'>Calgary, Alberta</td>"
            "<td>Engineering</td></tr>"
        )

    @staticmethod
    def structural_row(name: str) -> str:
        if name == "Snap":
            return (
                "<tr role='row'><td>Team</td><td>Location</td>"
                "<td>Employment type</td></tr>"
            )
        return (
            "<tr class='jv-job-list-header'>"
            "<td class='jv-job-list-name'>Job title</td>"
            "<td class='jv-job-list-location'>Location</td></tr>"
        )

    def test_tektelic_canonical_vacancy_identity_and_source_contract(self):
        def vacancy(path: str, title: str) -> str:
            return (
                "<html><body><div class='vacancies-list'>"
                "<div class='col-lg-12'>"
                f"<p class='vacancy-title'>{title}</p>"
                f"<a href='{path}'>View vacancy</a>"
                "<div class='loc'><span>Calgary</span></div>"
                "</div></div></body></html>"
            )

        expected_id = "director-of-sales-dach"
        for path in (
            "/career/vacancies/director-of-sales-dach/",
            "/career/vacancies/director-of-sales-dach",
        ):
            with self.subTest(path=path):
                job = self.parse(
                    "TEKTELIC Communications", vacancy(path, "Director of Sales DACH")
                )[0]
                self.assertEqual(job.raw_metadata["external_job_id"], expected_id)
                self.assertEqual(job.job_id, f"static_html:tektelic.com:{expected_id}")
                self.assertEqual(job.company, "TEKTELIC Communications")
                self.assertEqual(job.url.split("/career/", 1)[0], "https://tektelic.com")

        retained = self.parse(
            "TEKTELIC Communications",
            vacancy(
                "/career/vacancies/lead-generation-manager-position/",
                "Lead Generation Manager",
            ),
        )[0]
        self.assertEqual(
            retained.raw_metadata["external_job_id"],
            "lead-generation-manager-position",
        )

    def test_tektelic_unrelated_path_and_host_fail_closed(self):
        configured = self.targets["TEKTELIC Communications"]
        regex = static_html._safe_regex(str(configured.external_id_regex))
        self.assertIsNone(
            regex.search("https://tektelic.com/career/news/director-of-sales-dach")
        )
        unrelated_path = (
            "<html><body><div class='vacancies-list'><div class='col-lg-12'>"
            "<p class='vacancy-title'>Director of Sales DACH</p>"
            "<a href='/career/news/director-of-sales-dach'>Read more</a>"
            "</div></div></body></html>"
        )
        with self.assertRaisesRegex(AdapterError, "canonical job URL"):
            self.parse("TEKTELIC Communications", unrelated_path)

        unrelated_host = (
            "<html><body><div class='vacancies-list'><div class='col-lg-12'>"
            "<p class='vacancy-title'>Director of Sales DACH</p>"
            "<a href='https://example.test/career/vacancies/director-of-sales-dach'>"
            "View vacancy</a></div></div></body></html>"
        )
        with self.assertRaisesRegex(AdapterError, "allowed-host"):
            self.parse("TEKTELIC Communications", unrelated_host)

    def test_structural_rows_are_ignored_only_without_job_signals(self):
        for name, contract in self.STRUCTURAL_ROW_SOURCES.items():
            with self.subTest(name=name, case="structural plus valid job"):
                body = self.source_page(
                    self.structural_row(name)
                    + self.source_row(name, contract["path"])
                )
                jobs = self.parse(name, body)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].company, name)
                self.assertEqual(
                    jobs[0].raw_metadata["external_job_id"], contract["external_id"]
                )

            with self.subTest(name=name, case="structural row only"):
                self.assertEqual(
                    self.parse(name, self.source_page(self.structural_row(name))),
                    [],
                )

    def test_job_like_linkless_rows_still_fail_closed(self):
        for name, contract in self.STRUCTURAL_ROW_SOURCES.items():
            if name == "Snap":
                titled = (
                    "<tr role='row'><td><a href='/job?id='>"
                    "Software Engineer</a></td></tr>"
                )
            else:
                titled = (
                    "<tr><td class='jv-job-list-name'>"
                    "<a>Software Engineer</a></td></tr>"
                )
            with self.subTest(name=name, case="configured title without identity"):
                with self.assertRaises(AdapterError):
                    self.parse(name, self.source_page(titled))

            role = " role='row'" if name == "Snap" else ""
            identity_signal = (
                f"<tr{role} data-job-reference='{contract['path']}'>"
                "<td>Job metadata</td></tr>"
            )
            with self.subTest(name=name, case="configured ID without detail link"):
                with self.assertRaisesRegex(AdapterError, "canonical job URL"):
                    self.parse(name, self.source_page(identity_signal))

    def test_ambiguous_links_and_unrelated_hosts_fail_closed(self):
        for name, contract in self.STRUCTURAL_ROW_SOURCES.items():
            separator = "&amp;" if "?" in contract["path"] else "-"
            second_path = f"{contract['path']}{separator}other=R0041043"
            if name != "Snap":
                second_path = contract["path"].rsplit("-", 1)[0] + "-202"
            ambiguous = self.source_row(name, contract["path"]).replace(
                "</td>",
                f"<a href='{second_path}'>Other job</a></td>",
                1,
            )
            with self.subTest(name=name, case="ambiguous links"):
                with self.assertRaisesRegex(AdapterError, "canonical job URL"):
                    self.parse(name, self.source_page(ambiguous))

            unrelated_host = self.source_row(
                name, f"https://example.test{contract['path']}"
            )
            with self.subTest(name=name, case="unrelated host"):
                with self.assertRaisesRegex(AdapterError, "allowed-host"):
                    self.parse(name, self.source_page(unrelated_host))

    def test_jobvite_company_paths_cannot_cross_attribute(self):
        cross_source_rows = {
            "Absolute Software": self.source_row(
                "Absolute Software", self.STRUCTURAL_ROW_SOURCES["EPCOR"]["path"]
            ),
            "EPCOR": self.source_row(
                "EPCOR", self.STRUCTURAL_ROW_SOURCES["Cameco"]["path"]
            ),
            "Cameco": self.source_row(
                "Cameco", self.STRUCTURAL_ROW_SOURCES["EPCOR"]["path"]
            ),
        }
        for name, row in cross_source_rows.items():
            with self.subTest(name=name), self.assertRaises(AdapterError):
                self.parse(name, self.source_page(row))

        epcor = self.parse(
            "EPCOR",
            self.source_page(
                self.source_row("EPCOR", self.STRUCTURAL_ROW_SOURCES["EPCOR"]["path"])
            ),
        )[0]
        cameco = self.parse(
            "Cameco",
            self.source_page(
                self.source_row("Cameco", self.STRUCTURAL_ROW_SOURCES["Cameco"]["path"])
            ),
        )[0]
        absolute = self.parse(
            "Absolute Software",
            self.source_page(
                self.source_row(
                    "Absolute Software",
                    self.STRUCTURAL_ROW_SOURCES["Absolute Software"]["path"],
                )
            ),
        )[0]
        self.assertEqual(epcor.source, "static_html:epcor")
        self.assertEqual(cameco.source, "static_html:cameco")
        self.assertNotIn(absolute.source, {epcor.source, cameco.source})

    def test_snap_requisition_query_id_remains_stable(self):
        contract = self.STRUCTURAL_ROW_SOURCES["Snap"]
        job = self.parse(
            "Snap",
            self.source_page(self.source_row("Snap", contract["path"])),
        )[0]
        self.assertEqual(job.raw_metadata["external_job_id"], "R0041042")
        self.assertEqual(job.job_id, "static_html:careers.snap.com:R0041042")

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
        historical = CompanyTarget.from_dict(
            {
                "name": "Deliveroo",
                "platform": "browser_rendered_html",
                "careers_url": "https://careers.deliveroo.co.uk/",
                "listing_url": "https://careers.deliveroo.co.uk/join-the-team/",
                "endpoint_variant": "browser_rendered_html_v1",
                "browser_wait_selector": "a[href*='/role/']",
                "browser_static_variant": "server_rendered_links",
                "browser_identity_selector": "head title",
                "browser_identity_regex": "Deliveroo",
                "listing_selector": "a[href*=/role/]",
                "job_card_selector": "a[href*=/role/]",
                "title_selector": "h4",
                "detail_link_selector": ":self",
                "external_id_regex": r"/role/[^/?#]+-([0-9a-f]{12})/?",
                "allowed_apply_hosts": ["careers.deliveroo.co.uk"],
            }
        )
        jobs = static_html.fetch_jobs(
            Session(body), browser_rendered_html._static_target(historical)
        )
        self.assertEqual(
            [(job.title, job.raw_metadata["external_job_id"]) for job in jobs],
            [("Software Engineer", "a1b2c3d4e5f6"), ("Data Engineer", "123456abcdef")],
        )


if __name__ == "__main__":
    unittest.main()
