from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

import requests
import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import company_watchlist_common, investigate_remaining_companies, investigate_unknown_companies
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import adp, hibob, sap_successfactors
from src.scraper.company_careers.models import AdapterError, AdapterResult, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "config" / "company_careers_adapter_expansion_v1_batch_c_validation.yaml"
VALIDATED = ROOT / "config" / "target_companies.company_careers_adapter_expansion_v1_batch_c_validated.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
BASELINE_REGISTRY = ROOT / "config" / "target_companies.calgary_canada_expansion_v2_batch_a_validated.yaml"


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
        self.history = []

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
        value = self.gets.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

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


def wfn_item(
    *, item_id="123456789_1", external_id="42001", title="Systems Intern", **overrides
):
    value = {
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
    value.update(overrides)
    return value


def wfn_session(items, *, details=None, identity="Example Space", total=None):
    detail_rows = list(details if details is not None else items)
    return Session(gets=[
        Response({"contentLinks": [{"contentBody": {"contentTitle": identity}}]}),
        Response({
            "jobRequisitions": items,
            "meta": {
                "startSequence": 0,
                "totalNumber": len(items) if total is None else total,
            },
        }),
        *[Response(detail) for detail in detail_rows],
    ])


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
    def test_workforce_now_classifies_exact_adp_maintenance_redirect_without_parsing_html(self):
        response = Response(
            "<html><title>We are sorry | ADP maintenance page</title></html>",
            url="https://sorry.adp.com/maintenance/6a923d2c556ac/",
        )
        with self.assertRaisesRegex(AdapterError, "transient upstream maintenance redirect"):
            adp.fetch_jobs(Session(gets=[response]), wfn_target())

        error_type, _ = scanner.CompanyCareersScanner._adapter_failure_details(
            "ADP Workforce Now career-center identity returned transient upstream maintenance redirect"
        )
        self.assertEqual(error_type, "transient_upstream_http")

    def test_workforce_now_does_not_treat_arbitrary_html_as_maintenance(self):
        response = Response(
            "<html><title>Welcome</title></html>",
            url="https://workforcenow.adp.com/mascsr/default/error.html",
        )
        with self.assertRaisesRegex(AdapterError, "was not valid JSON"):
            adp.fetch_jobs(Session(gets=[response]), wfn_target())

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
        self.assertIsInstance(jobs, AdapterResult)
        self.assertEqual(jobs.scan, session.adp_pagination)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Systems Intern")
        self.assertEqual(jobs[0].location, "Calgary, AB, Canada")
        self.assertIn("jobId=42001", jobs[0].url)
        self.assertEqual(jobs[0].raw_metadata["apply_url"], jobs[0].url)
        self.assertIn("123456789_1", jobs[0].raw_metadata["api_detail_url"])
        self.assertEqual(session.calls[1][2]["params"]["$top"], 10)
        self.assertEqual(
            jobs[0].job_id,
            "adp:workforcenow.adp.com:11111111-2222-3333-4444-555555555555:123456789_1",
        )
        self.assertEqual(
            jobs[0].source,
            "adp:workforcenow.adp.com:11111111-2222-3333-4444-555555555555",
        )

    def test_workforce_now_isolates_one_identity_valid_locationless_record(self):
        valid = wfn_item()
        rejected = wfn_item(
            item_id="123456789_2",
            external_id="42002",
            title="Controls Intern",
            requisitionLocations=[{}],
        )
        session = wfn_session([valid, rejected])

        jobs = adp.fetch_jobs(session, wfn_target(max_jobs=10))

        self.assertEqual([job.job_id.rsplit(":", 1)[-1] for job in jobs], ["123456789_1"])
        metadata = session.adp_pagination
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "malformed_listing_records")
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertEqual(metadata["rejected_record_ids"], ["123456789_2"])
        self.assertEqual(metadata["rejected_record_reason"], "missing_stable_location")

    def test_workforce_now_retains_multiple_valid_records_without_duplicates(self):
        first = wfn_item()
        rejected = wfn_item(
            item_id="123456789_2", external_id="42002",
            requisitionLocations=[{"aliasNames": ["Calgary"]}],
        )
        second = wfn_item(
            item_id="123456789_3", external_id="42003", title="Robotics Intern",
            requisitionLocations=[{"workLocation": {"displayName": "Vancouver, BC"}}],
        )
        session = wfn_session([first, rejected, second])

        jobs = adp.fetch_jobs(session, wfn_target(max_jobs=10))

        self.assertEqual(
            {job.raw_metadata["item_id"] for job in jobs},
            {"123456789_1", "123456789_3"},
        )
        self.assertEqual(len(jobs), 2)
        self.assertEqual(session.adp_pagination["rejected_record_count"], 1)

    def test_workforce_now_all_locationless_records_fail_the_source(self):
        rows = [
            wfn_item(
                item_id=f"123456789_{index}", external_id=f"4200{index}",
                requisitionLocations=[{}],
            )
            for index in (1, 2)
        ]
        session = wfn_session(rows)

        with self.assertRaisesRegex(AdapterError, "no valid jobs after rejecting"):
            adp.fetch_jobs(session, wfn_target(max_jobs=10))

        self.assertFalse(session.adp_pagination["scan_complete"])
        self.assertEqual(session.adp_pagination["rejected_record_count"], 2)

    def test_workforce_now_alias_names_alone_remain_insufficient(self):
        row = wfn_item(requisitionLocations=[{"aliasNames": ["Calgary, AB"]}])
        session = wfn_session([row])
        with self.assertRaisesRegex(AdapterError, "no valid jobs after rejecting"):
            adp.fetch_jobs(session, wfn_target())
        self.assertEqual(
            session.adp_pagination["rejected_record_reason"],
            "missing_stable_location",
        )

    def test_adp_valid_location_shapes_remain_accepted(self):
        cases = {
            "nameCode": (
                [{"nameCode": {"longName": "Calgary, AB, Canada"}}],
                ["Calgary, AB, Canada"],
            ),
            "address": (
                [{"address": {"cityName": "Calgary", "stateCode": "AB"}}],
                ["Calgary, AB"],
            ),
            "workLocation": (
                [{"workLocation": {"displayName": "Vancouver, BC"}}],
                ["Vancouver, BC"],
            ),
        }
        for label, (value, expected) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(adp._location_values(value), expected)

    def test_workforce_now_identity_disagreements_remain_source_fatal(self):
        listing = wfn_item(requisitionLocations=[{}])
        disagreements = {
            "detail itemID": wfn_item(item_id="123456789_2", requisitionLocations=[{}]),
            "detail title": wfn_item(title="Changed", requisitionLocations=[{}]),
            "external job ID": wfn_item(external_id="99999", requisitionLocations=[{}]),
        }
        for message, detail in disagreements.items():
            with self.subTest(message=message), self.assertRaisesRegex(AdapterError, message):
                adp.fetch_jobs(
                    wfn_session([listing], details=[detail]),
                    wfn_target(),
                )

    def test_workforce_now_location_disagreement_is_not_isolated(self):
        listing = wfn_item(requisitionLocations=[{}])
        detail = wfn_item(
            requisitionLocations=[{"nameCode": {"shortName": "Calgary, AB"}}]
        )
        session = wfn_session([listing], details=[detail])
        with self.assertRaisesRegex(AdapterError, "detail locations"):
            adp.fetch_jobs(session, wfn_target())
        self.assertEqual(session.adp_pagination["rejected_record_count"], 0)

    def test_workforce_now_bad_apply_host_and_malformed_id_remain_source_fatal(self):
        with self.assertRaisesRegex(AdapterError, "allowed_apply_hosts"):
            adp.fetch_jobs(Session(), wfn_target(allowed_apply_hosts=["evil.test"]))

        malformed = wfn_item(item_id="ambiguous/id", requisitionLocations=[{}])
        session = wfn_session([malformed])
        with self.assertRaisesRegex(AdapterError, "itemID is missing or invalid"):
            adp.fetch_jobs(session, wfn_target())
        self.assertEqual(session.adp_pagination["rejected_record_count"], 0)

        malformed_location = wfn_item(requisitionLocations=[{"nameCode": "Calgary"}])
        session = wfn_session([malformed_location])
        with self.assertRaisesRegex(AdapterError, "nameCode is malformed"):
            adp.fetch_jobs(session, wfn_target())
        self.assertEqual(session.adp_pagination["rejected_record_count"], 0)

    def test_workforce_now_rejection_overrides_reported_total_completion(self):
        valid = wfn_item()
        rejected = wfn_item(
            item_id="123456789_2", external_id="42002", requisitionLocations=[]
        )
        session = wfn_session([valid, rejected], total=2)
        jobs = adp.fetch_jobs(session, wfn_target(max_jobs=10))

        self.assertEqual(len(jobs), 1)
        self.assertEqual(session.adp_pagination["stop_reason"], "total_jobs_reached")
        self.assertFalse(session.adp_pagination["scan_complete"])
        self.assertEqual(
            session.adp_pagination["scan_completeness_reason"],
            "malformed_listing_records",
        )

        # Even a contradictory upstream total that equals the retained count
        # cannot let scanner-level authoritative-total recovery erase rejection.
        scanner_session = wfn_session([valid, rejected], total=1)
        result = scanner.CompanyCareersScanner(
            targets=[wfn_target(max_jobs=10)],
            session=scanner_session,
            parent_source_mappings=[],
        ).scan().results[0]
        self.assertFalse(result.scan_complete)
        self.assertEqual(result.scan_completeness_reason, "malformed_listing_records")

    def test_workforce_now_paramount_and_novarc_namespaces_remain_tenant_bound(self):
        tenants = (
            (
                "Paramount Resources", "b8dcb5ca-ab7a-4dad-b6cd-2c7a858453dd",
                "19000101_000001", "Paramount Resources",
            ),
            (
                "Novarc Technologies", "d355e8f6-9a6c-48a9-b7ba-45a41dc5daad",
                "9200648065638_2", "WNVPD0000191809",
            ),
        )
        for name, cid, cc_id, identity in tenants:
            with self.subTest(name=name):
                target = wfn_target(
                    name=name,
                    careers_url=(
                        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                        f"recruitment.html?cid={cid}&ccId={cc_id}&lang=en_CA"
                    ),
                    identity_regex=identity,
                )
                jobs = adp.fetch_jobs(
                    wfn_session([wfn_item()], identity=identity), target
                )
                self.assertEqual(
                    jobs[0].source,
                    f"adp:workforcenow.adp.com:{cid}",
                )

        novarc = wfn_target(
            name="Novarc Technologies",
            careers_url=(
                "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                "recruitment.html?cid=d355e8f6-9a6c-48a9-b7ba-45a41dc5daad"
                "&ccId=9200648065638_2&lang=en_CA"
            ),
            identity_regex="WNVPD0000191809",
        )
        with self.assertRaisesRegex(AdapterError, "tenant identity"):
            adp.fetch_jobs(
                wfn_session([], identity="Paramount Resources"), novarc
            )

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

    def test_workforce_now_accepts_exact_metadata_free_zero_inventory(self):
        session = Session(gets=[
            Response({"contentLinks": [{"contentBody": {"contentTitle": "Example Space"}}]}),
            Response({"jobRequisitions": []}),
        ])
        self.assertEqual(adp.fetch_jobs(session, wfn_target()), [])
        self.assertEqual(session.adp_pagination["total_available"], 0)
        self.assertEqual(session.adp_pagination["stop_reason"], "total_jobs_reached")

        for malformed in (
            {"jobRequisitions": [wfn_item()]},
            {"jobRequisitions": [], "unexpected": True},
        ):
            with self.subTest(payload=malformed), self.assertRaisesRegex(
                AdapterError, "listing response is malformed"
            ):
                adp.fetch_jobs(
                    Session(gets=[
                        Response({"contentLinks": [{"contentBody": {"contentTitle": "Example Space"}}]}),
                        Response(malformed),
                    ]),
                    wfn_target(),
                )

    def test_workforce_now_authoritative_total_adapts_detail_coupled_first_stage(self):
        for total, normal_pages in ((26, 2), (88, 3)):
            with self.subTest(total=total):
                rows = [
                    wfn_item(
                        item_id=f"123456789_{index}",
                        external_id=f"42{index:03d}",
                        title=f"Systems Intern {index}",
                    )
                    for index in range(total)
                ]
                responses = [
                    Response(
                        {
                            "contentLinks": [
                                {"contentBody": {"contentTitle": "Example Space"}}
                            ]
                        }
                    )
                ]
                for offset in range(0, total, 10):
                    page_rows = rows[offset : offset + 10]
                    responses.append(
                        Response(
                            {
                                "jobRequisitions": page_rows,
                                "meta": {
                                    "startSequence": offset,
                                    "totalNumber": total,
                                },
                            }
                        )
                    )
                    responses.extend(Response(row) for row in page_rows)
                session = Session(gets=responses)

                jobs = adp.fetch_jobs(
                    session,
                    wfn_target(max_pages=normal_pages, max_jobs=20),
                )

                self.assertEqual(len(jobs), total)
                self.assertTrue(session.adp_pagination["scan_complete"])
                self.assertFalse(session.adp_pagination["safety_limit_reached"])
                self.assertEqual(
                    session.adp_pagination["pages_completed"], (total + 9) // 10
                )
                self.assertTrue(
                    session.adp_pagination["adaptive_completion_eligible"]
                )

    def test_myjobs_requires_exact_site_attribution_and_apply_action(self):
        session = Session(gets=[
            Response(myjobs_config()),
            Response({"count": 1, "jobRequisitions": [myjobs_item()]}),
        ])
        jobs = adp.fetch_jobs(session, myjobs_target())
        self.assertIsInstance(jobs, AdapterResult)
        self.assertEqual(jobs.scan, session.adp_pagination)
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

    def test_myjobs_later_transport_failure_keeps_valid_rows_without_authority(self):
        rows = [
            myjobs_item(reqId=str(5000000000001 + index))
            for index in range(20)
        ]
        session = Session(
            gets=[
                Response(myjobs_config()),
                Response({"count": 40, "jobRequisitions": rows}),
                requests.Timeout("second page timed out"),
            ]
        )

        result = adp.fetch_jobs(
            session,
            myjobs_target(max_pages=2, max_jobs=40),
        )

        self.assertEqual(len(result.jobs), 20)
        self.assertIn("partial_progress", result.capabilities_used)
        self.assertFalse(result.scan["scan_complete"])
        self.assertEqual(result.scan["stop_reason"], "request_failure")

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

        production = yaml.safe_load(PRODUCTION.read_bytes())["target_companies"]
        validated = yaml.safe_load(VALIDATED.read_text(encoding="utf-8"))["target_companies"]
        baseline = yaml.safe_load(BASELINE_REGISTRY.read_bytes())["target_companies"]
        self.assertEqual(len(baseline), 269)
        assert_historical_prefix(self, production, baseline)
        promoted = ["Astrobotic", "Intuitive Machines", "FANUC", "Helcim", "CPKC"]
        self.assertGreaterEqual(len(production), len(validated))
        self.assertEqual(production[269:274], validated)
        self.assertEqual([row["name"] for row in production[269:274]], promoted)
        names = [row["name"] for row in production]
        for name in promoted:
            self.assertEqual(names.count(name), 1)
        self.assertEqual(len(load_parent_source_mappings()), 5)

        fanuc = production[271]
        self.assertEqual(fanuc["name"], "FANUC")
        self.assertEqual(fanuc["identity_regex"], "^FANUC America$")
        self.assertEqual(fanuc["organization_id"], "G36B3N8NPGHCGC17")

        master = reconcile_master(
            load_master_workbook(ROOT / "config" / "company_watchlist.xlsx"),
            load_target_companies(str(PRODUCTION)),
            load_parent_source_mappings(),
        )
        self.assertGreaterEqual(len(master.canonical_names) + len(master.mapped_names), 208)
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
