from __future__ import annotations

import hashlib
import inspect
import json
import unittest
from pathlib import Path

import yaml

from scripts import company_watchlist_common, investigate_remaining_companies, investigate_unknown_companies
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import adp, hibob, sap_successfactors
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "config" / "company_careers_adapter_expansion_v1_batch_c_validation.yaml"
VALIDATED = ROOT / "config" / "target_companies.company_careers_adapter_expansion_v1_batch_c_validated.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PRODUCTION_SHA256 = "41757919E29E39F9628745D7D9E866422754325C61A75BC6CD9E16C176A744A3"


class Response:
    def __init__(self, value, *, status=200, url="https://jobs.test", content_type=None):
        self.status_code = status
        self.url = url
        self._value = value
        self.text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        self.content = self.text.encode("utf-8")
        self.headers = {
            "content-type": content_type
            or ("application/json" if isinstance(value, (dict, list)) else "text/html")
        }

    def json(self):
        if isinstance(self._value, (dict, list)):
            return self._value
        return json.loads(self.text)


class Session:
    def __init__(self, *, gets=None, posts=None):
        self.gets = list(gets or [])
        self.posts = list(posts or [])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if not self.gets:
            raise AssertionError(f"Unexpected GET {url}")
        return self.gets.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if not self.posts:
            raise AssertionError(f"Unexpected POST {url}")
        return self.posts.pop(0)


def wfn_target(**overrides):
    values = {
        "name": "Example Space",
        "platform": "adp",
        "careers_url": (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
            "recruitment.html?cid=11111111-2222-3333-4444-555555555555"
            "&ccId=123456_1&lang=en_US"
        ),
        "endpoint_url": (
            "https://workforcenow.adp.com/mascsr/default/careercenter/public/"
            "events/staffing/v1/job-requisitions"
        ),
        "endpoint_variant": adp.WORKFORCE_NOW,
        "identity_regex": r"Example Space",
        "allowed_apply_hosts": ["workforcenow.adp.com"],
        "page_size": 10,
        "max_pages": 1,
        "max_jobs": 1,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def wfn_item(*, item_id="123456789_1", external_id="42001", title="Systems Intern"):
    return {
        "itemID": item_id,
        "requisitionTitle": title,
        "postDate": "2026-08-09T12:00:00Z",
        "customFieldGroup": {
            "stringFields": [
                {"stringValue": external_id, "nameCode": {"codeValue": "ExternalJobID"}}
            ]
        },
        "requisitionLocations": [
            {"nameCode": {"shortName": " Calgary, AB, Canada"}}
        ],
    }


def myjobs_target(**overrides):
    values = {
        "name": "Example Automation",
        "platform": "adp",
        "slug": "example",
        "careers_url": "https://myjobs.adp.com/example/cx",
        "endpoint_url": adp.MYJOBS_API,
        "endpoint_variant": adp.MYJOBS,
        "organization_id": "ORG123456",
        "identity_regex": r"^Example Automation$",
        "allowed_apply_hosts": ["myjobs.adp.com"],
        "page_size": 20,
        "max_pages": 1,
        "max_jobs": 1,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def myjobs_config():
    return {
        "domain": "example",
        "clientName": "Example Automation",
        "orgoid": "ORG123456",
        "active": True,
        "myJobsToken": "x" * 40,
        "settings": {"careerSiteType": "External"},
    }


def myjobs_item(**overrides):
    value = {
        "reqId": "5000000000001",
        "publishedJobTitle": "Robotics Intern",
        "postingDate": "2026-08-08T12:00:00Z",
        "careerSiteDomains": ["example"],
        "canApply": True,
        "clientRequisitionID": "R-12",
        "requisitionLocations": [{"nameCode": {"longName": "Calgary, AB, Canada"}}],
        "jobDescription": "<p>Build safe robots.</p>",
    }
    value.update(overrides)
    return value


def hibob_target(**overrides):
    values = {
        "name": "Example Payments",
        "platform": "hibob",
        "slug": "example",
        "careers_url": "https://example.careers.hibob.com/",
        "endpoint_url": "https://example.careers.hibob.com/api/job-ad",
        "endpoint_variant": hibob.ENDPOINT_VARIANT,
        "identity_regex": r"^Example Payments Inc$",
        "allowed_apply_hosts": ["example.careers.hibob.com"],
        "max_pages": 1,
        "max_jobs": 20,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def hibob_item(**overrides):
    value = {
        "id": "56ade5eb-5943-4c50-9c85-680c7aa83a3d",
        "title": "Software Engineering Intern",
        "site": "Calgary",
        "country": "Canada",
        "department": "Engineering",
        "publishedAt": "2026-08-04T18:21:54.038742329Z",
        "description": "<p>Build reliable payments.</p>",
    }
    value.update(overrides)
    return value


def columnized_target(**overrides):
    values = {
        "name": "Example Rail",
        "platform": "sap_successfactors",
        "careers_url": "https://example.test/careers",
        "search_url": "https://jobs.example.test/search/",
        "endpoint_url": "https://jobs.example.test/services/recruiting/v1/jobs",
        "endpoint_variant": sap_successfactors.COLUMNIZED_VARIANT,
        "identity_regex": r"Example Rail",
        "locale": "en_US",
        "allowed_apply_hosts": ["jobs.example.test"],
        "page_size": 10,
        "max_pages": 1,
        "max_jobs": 1,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def columnized_identity():
    return Response(
        '<html><title>Example Rail Jobs</title><link href="BS3ColumnizedSearch.css">'
        '<script src="j2w.searchManager.min.js"></script>'
        '<script>var CSRFToken = "11111111-2222-3333-4444-555555555555";</script></html>',
        url="https://jobs.example.test/search/",
    )


def columnized_detail(job_id="106210", title="Train Conductor"):
    url = f"https://jobs.example.test/job/Train-Conductor/{job_id}-en_US"
    return Response(
        f'''<html><head><title>{title} Job Details | Example Rail</title>
        <link rel="canonical" href="{url}/"><meta property="og:title" content="{title}"></head>
        <body>Example Rail<ul><li>Req ID: {job_id}</li><li>Department: Operations</li>
        <li>Location: Calgary, Alberta</li></ul><span itemprop="description">Move freight.</span>
        <script>j2w.Apply.init({{ jobID : {job_id}, sourceId : '' }});</script></body></html>''',
        url=url,
    )


def columnized_page(**response_overrides):
    response = {
        "id": "106210",
        "unifiedStandardTitle": "Train Conductor",
        "unifiedUrlTitle": "Train-Conductor",
        "unifiedStandardStart": "8/9/26",
        "supportedLocales": ["en_US"],
    }
    response.update(response_overrides)
    return Response({"jobSearchResult": [{"response": response}], "totalJobs": 1})


class AdpAdapterTests(unittest.TestCase):
    def test_scanner_isolates_anonymous_adp_sessions_between_targets(self):
        sessions = []

        class ClosingSession(Session):
            def __init__(self):
                item = wfn_item()
                super().__init__(gets=[
                    Response({"contentLinks": [{"contentBody": {"contentTitle": "Example Space"}}]}),
                    Response({
                        "jobRequisitions": [item],
                        "meta": {"startSequence": 0, "totalNumber": 1},
                    }),
                    Response({**item, "requisitionDescription": "Example role."}),
                ])
                self.closed = False

            def close(self):
                self.closed = True

        def session_factory():
            session = ClosingSession()
            sessions.append(session)
            return session

        second = wfn_target(
            name="Example Space Two",
            careers_url=(
                "https://workforcenow.cloud.adp.com/mascsr/default/mdf/recruitment/"
                "recruitment.html?cid=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                "&ccId=654321_2&lang=en_US"
            ),
            endpoint_url=(
                "https://workforcenow.cloud.adp.com/mascsr/default/careercenter/public/"
                "events/staffing/v1/job-requisitions"
            ),
            allowed_apply_hosts=["workforcenow.cloud.adp.com"],
        )
        report = scanner.CompanyCareersScanner(
            targets=[wfn_target(), second],
            session_factory=session_factory,
            parent_source_mappings=[],
        ).scan()

        self.assertEqual(
            [result.status for result in report.results],
            ["fetched", "fetched"],
        )
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertTrue(all(session.closed for session in sessions))

    def test_workforce_now_correlates_identity_listing_detail_and_apply_url(self):
        item = wfn_item()
        detail = {**item, "requisitionDescription": "<p>Example Space role.</p>"}
        session = Session(
            gets=[
                Response({"contentLinks": [{"contentBody": {"contentTitle": "Example Space"}}]}),
                Response({"jobRequisitions": [item], "meta": {"startSequence": 0, "totalNumber": 1}}),
                Response(detail),
            ]
        )
        jobs = adp.fetch_jobs(session, wfn_target())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Systems Intern")
        self.assertEqual(jobs[0].location, "Calgary, AB, Canada")
        self.assertIn("jobId=42001", jobs[0].url)
        self.assertEqual(jobs[0].raw_metadata["apply_url"], jobs[0].url)
        self.assertIn("123456789_1", jobs[0].raw_metadata["api_detail_url"])
        self.assertEqual(session.calls[1][2]["params"]["$top"], 10)

    def test_workforce_now_fails_closed_on_identity_or_detail_drift(self):
        with self.assertRaisesRegex(AdapterError, "identity"):
            adp.fetch_jobs(
                Session(gets=[Response({"contentLinks": [{"contentBody": {"contentTitle": "Other"}}]})]),
                wfn_target(),
            )
        item = wfn_item()
        changed = {**item, "requisitionTitle": "Changed", "requisitionDescription": "x"}
        with self.assertRaisesRegex(AdapterError, "detail title"):
            adp.fetch_jobs(
                Session(gets=[
                    Response({"contentLinks": [{"contentBody": {"contentTitle": "Example Space"}}]}),
                    Response({"jobRequisitions": [item], "meta": {"startSequence": 0, "totalNumber": 1}}),
                    Response(changed),
                ]),
                wfn_target(),
            )

    def test_myjobs_requires_exact_site_attribution_and_apply_action(self):
        session = Session(gets=[
            Response(myjobs_config()),
            Response({"count": 1, "jobRequisitions": [myjobs_item()]}),
        ])
        jobs = adp.fetch_jobs(session, myjobs_target())
        self.assertEqual(jobs[0].job_id, "adp:org123456:5000000000001")
        self.assertEqual(jobs[0].location, "Calgary, AB, Canada")
        self.assertIn("reqId=5000000000001", jobs[0].url)
        headers = session.calls[1][2]["headers"]
        self.assertEqual(headers["orgoid"], "ORG123456")
        self.assertNotIn("rolecode", {key.casefold() for key in headers})
        with self.assertRaisesRegex(AdapterError, "crossed or ambiguously"):
            adp.fetch_jobs(
                Session(gets=[
                    Response(myjobs_config()),
                    Response({"count": 1, "jobRequisitions": [myjobs_item(careerSiteDomains=["other"])]}),
                ]),
                myjobs_target(),
            )

    def test_adp_rejects_unreviewed_hosts_and_unsafe_bounds(self):
        with self.assertRaisesRegex(AdapterError, "recruitment URL"):
            adp.fetch_jobs(Session(), wfn_target(careers_url="https://evil.test/jobs"))
        with self.assertRaisesRegex(AdapterError, "page_size"):
            adp.fetch_jobs(Session(), wfn_target(page_size=1))


class HiBobAdapterTests(unittest.TestCase):
    def test_normalizes_uuid_location_detail_and_official_apply_url(self):
        session = Session(gets=[
            Response({
                "companyName": "Example Payments Inc",
                "isPublished": True,
                "isBobCareerSite": True,
            }),
            Response({"filterGroups": [], "jobAdDetails": [hibob_item()]}),
        ])
        jobs = hibob.fetch_jobs(session, hibob_target())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Calgary, Canada")
        self.assertTrue(jobs[0].url.endswith("/apply"))
        self.assertIn("56ade5eb-5943-4c50-9c85-680c7aa83a3d", jobs[0].raw_metadata["detail_url"])

    def test_identity_and_malformed_rows_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "identity"):
            hibob.fetch_jobs(Session(gets=[Response({
                "companyName": "Other", "isPublished": True, "isBobCareerSite": True,
            })]), hibob_target())
        with self.assertRaisesRegex(AdapterError, "stable UUID"):
            hibob.fetch_jobs(Session(gets=[
                Response({"companyName": "Example Payments Inc", "isPublished": True, "isBobCareerSite": True}),
                Response({"jobAdDetails": [hibob_item(id="not-a-uuid")]}),
            ]), hibob_target())


class ColumnizedRmkAdapterTests(unittest.TestCase):
    def test_public_json_page_is_bounded_and_each_detail_is_corroborated(self):
        session = Session(
            gets=[columnized_identity(), columnized_detail()],
            posts=[columnized_page()],
        )
        jobs = sap_successfactors.fetch_jobs(session, columnized_target())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "sap_successfactors:jobs.example.test:106210")
        self.assertEqual(jobs[0].location, "Calgary, Alberta")
        self.assertEqual(jobs[0].updated_at, "2026-08-09")
        self.assertEqual(jobs[0].raw_metadata["apply_url"], jobs[0].url)
        post = next(call for call in session.calls if call[0] == "post")
        self.assertEqual(post[2]["json"]["pageNumber"], 0)
        self.assertEqual(post[2]["json"]["sortBy"], "recent")

    def test_csrf_title_id_apply_and_response_bounds_fail_closed(self):
        bad_identity = Response("<html>Example Rail BS3ColumnizedSearch j2w.searchManager</html>")
        with self.assertRaisesRegex(AdapterError, "CSRF"):
            sap_successfactors.fetch_jobs(Session(gets=[bad_identity]), columnized_target())
        with self.assertRaisesRegex(AdapterError, "detail title"):
            sap_successfactors.fetch_jobs(
                Session(gets=[columnized_identity(), columnized_detail(title="Other")], posts=[columnized_page()]),
                columnized_target(),
            )
        oversized = [{"response": {}} for _ in range(11)]
        with self.assertRaisesRegex(AdapterError, "page exceeded"):
            sap_successfactors.fetch_jobs(
                Session(gets=[columnized_identity()], posts=[Response({"jobSearchResult": oversized, "totalJobs": 11})]),
                columnized_target(max_jobs=20),
            )


class BatchCValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = yaml.safe_load(VALIDATION.read_text(encoding="utf-8"))
        cls.targets = load_target_companies(str(VALIDATED))

    def test_projection_and_repeatable_target_results(self):
        self.assertEqual(self.data["projection"]["production_sources"], 274)
        self.assertEqual(self.data["projection"]["original_master_coverage"], 196)
        self.assertEqual(self.data["projection"]["jobs_normalized"], 84)
        self.assertEqual(self.data["projection"]["current_student_intern_hits"], 0)
        observed = {row["name"]: row["jobs_normalized_each_run"] for row in self.data["recovery_targets"]}
        self.assertEqual(observed, {
            "Astrobotic": 14, "Intuitive Machines": 20, "FANUC": 20,
            "Helcim": 10, "CPKC": 20,
        })

    def test_validated_targets_are_appended_once_to_exact_269_entry_prefix(self):
        self.assertIs(scanner.ADAPTERS["adp"], adp.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["hibob"], hibob.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["sap_successfactors"], sap_successfactors.fetch_jobs)

        production_bytes = PRODUCTION.read_bytes()
        marker = b"  - name: Astrobotic"
        baseline_bytes = production_bytes[:production_bytes.index(marker)]
        self.assertEqual(hashlib.sha256(baseline_bytes).hexdigest().upper(), PRODUCTION_SHA256)

        production = yaml.safe_load(production_bytes)["target_companies"]
        validated = yaml.safe_load(VALIDATED.read_text(encoding="utf-8"))["target_companies"]
        promoted = ["Astrobotic", "Intuitive Machines", "FANUC", "Helcim", "CPKC"]
        self.assertEqual(len(production), 325)
        self.assertEqual(production[269:274], validated)
        self.assertEqual([row["name"] for row in production[269:274]], promoted)
        names = [row["name"] for row in production]
        for name in promoted:
            self.assertEqual(names.count(name), 1)
        self.assertEqual(len(load_parent_source_mappings()), 4)

        fanuc = production[271]
        self.assertEqual(fanuc["name"], "FANUC")
        self.assertEqual(fanuc["identity_regex"], "^FANUC America$")
        self.assertEqual(fanuc["organization_id"], "G36B3N8NPGHCGC17")

        master = reconcile_master(
            load_master_workbook(ROOT / "config" / "company_watchlist.xlsx"),
            load_target_companies(str(PRODUCTION)),
            load_parent_source_mappings(),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 196)
        self.assertEqual(master.total, 246)

        # The validation artifact remains immutable milestone evidence.
        self.assertFalse(self.data["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.data["publication"].values()))

    def test_registry_validator_knows_all_new_contracts(self):
        expected = {
            "Astrobotic": "adp:workforcenow.cloud.adp.com:dde86c74-1cf6-4830-b396-33b9bd085ff9",
            "Intuitive Machines": "adp:workforcenow.adp.com:b0e24f83-6e4d-492d-9d6a-bc0fea197d6a",
            "FANUC": "adp:g36b3n8npghcgc17:fanuc",
            "Helcim": "hibob:helcim",
            "CPKC": "sap_successfactors:careers.cpr.ca",
        }
        rows = yaml.safe_load(VALIDATED.read_text(encoding="utf-8"))["target_companies"]
        for row in rows:
            with self.subTest(name=row["name"]):
                self.assertIsNone(registry_validator.configuration_error(row))
                self.assertEqual(registry_validator.source_key(row), expected[row["name"]])

    def test_generic_adapters_have_no_company_specific_branches(self):
        source = (inspect.getsource(adp) + inspect.getsource(hibob) + inspect.getsource(sap_successfactors)).casefold()
        for company in ("astrobotic", "intuitive machines", "fanuc", "helcim", "cpkc"):
            self.assertNotIn(company, source)

    def test_research_tools_classify_new_families_as_reviewed_existing_adapters(self):
        self.assertIn("adp", investigate_remaining_companies.EXISTING_ADAPTER_FAMILIES)
        self.assertIn("hibob", investigate_remaining_companies.EXISTING_ADAPTER_FAMILIES)
        self.assertIn("adp", investigate_unknown_companies.EXISTING_ADAPTERS)
        self.assertNotIn("adp", investigate_unknown_companies.KNOWN_FUTURE_FAMILIES)
        self.assertIn("adp", company_watchlist_common.DETECTED_REQUIRES_REVIEW)
        self.assertIn("hibob", company_watchlist_common.DETECTED_REQUIRES_REVIEW)


if __name__ == "__main__":
    unittest.main()
