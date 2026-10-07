from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import dayforce
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
    source_scan_metadata,
)
from src.scraper.company_careers.student_roles import (
    student_job_exclusion_reason,
    student_location_exclusion_reason,
)


class Response:
    def __init__(self, payload=None, *, text=None, status_code=200, content_type=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.content = self.text.encode("utf-8")
        self.headers = {
            "content-type": content_type
            or ("application/json" if payload is not None else "text/html; charset=utf-8")
        }

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class Session:
    def __init__(self, posts, *, shell=None, csrf=None, details=None):
        self.posts = list(posts)
        self.details = list(details or [])
        self.get_responses = [
            shell or Response(text=shell_html(), content_type="text/html; charset=utf-8"),
            csrf or Response({"csrfToken": "a" * 64}),
        ]
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if "/jobs/" in url:
            return self.details.pop(0)
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.posts.pop(0)


def target(**overrides):
    values = dict(
        name="Example Canada",
        platform="dayforce",
        tenant="example",
        site_id="CANDIDATEPORTAL",
        locale="en-CA",
        search_url="https://jobs.dayforcehcm.com/en-CA/example/CANDIDATEPORTAL",
        endpoint_url="https://jobs.dayforcehcm.com/api/geo/example/jobposting/search",
        endpoint_variant="nextjs_jobposting_search",
        allowed_apply_hosts=["jobs.dayforcehcm.com"],
        page_size=25,
        max_pages=2,
        max_jobs=50,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def shell_html(*, tenant="example", board="CANDIDATEPORTAL", locale="en-CA", board_id=1):
    data = {
        "query": {"clientNamespace": tenant, "careerSiteXRefCode": board},
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["site-info", {"clientNamespace": tenant}],
                            "state": {
                                "data": {
                                    "clientNamespace": tenant.casefold(),
                                    "jobBoardCode": board.casefold(),
                                    "cultureCode": locale,
                                    "jobBoardId": board_id,
                                }
                            },
                        }
                    ]
                }
            }
        },
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script></html>"
    )


def posting(posting_id=101, requisition_id=501, **overrides):
    row = {
        "clientNamespace": "example",
        "jobBoardId": 1,
        "jobPostingId": posting_id,
        "jobReqId": requisition_id,
        "jobTitle": "Software Engineering Intern",
        "jobDescription": "Build &amp; test reliable systems.",
        "hasVirtualLocation": False,
        "postingStartTimestampUTC": "2026-08-28T04:00:00+00:00",
        "postingExpiryTimestampUTC": "2026-09-30T03:59:00+00:00",
        "postingLocations": [
            {
                "formattedAddress": "Calgary, AB, Canada",
                "cityName": "Calgary",
                "stateCode": "AB",
                "isoCountryCode": "CA",
            }
        ],
    }
    row.update(overrides)
    return row


def page(rows, *, total=None, offset=0):
    return Response(
        {
            "jobPostings": rows,
            "maxCount": len(rows) if total is None else total,
            "offset": offset,
            "count": len(rows),
        }
    )


def detail_html(row, *, query=None):
    data = {
        "query": query or {},
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"state": {"data": row}}]
                }
            }
        }
    }
    return Response(
        text=(
            '<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data)
            + "</script></html>"
        ),
        content_type="text/html; charset=utf-8",
    )


class DayforceTests(unittest.TestCase):
    def test_successful_retrieval_normalization_and_public_request_contract(self):
        session = Session([page([posting()])])

        jobs = dayforce.fetch_jobs(session, target())

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "dayforce:example:candidateportal:501")
        self.assertEqual(job.source, "dayforce:example:candidateportal")
        self.assertEqual(job.title, "Software Engineering Intern")
        self.assertEqual(job.location, "Calgary, AB, Canada")
        self.assertEqual(job.updated_at, "2026-08-28")
        self.assertEqual(job.description, "Build & test reliable systems.")
        self.assertEqual(
            job.url,
            "https://jobs.dayforcehcm.com/en-CA/example/CANDIDATEPORTAL/jobs/101",
        )
        self.assertEqual(job.raw_metadata["requisition_id"], 501)
        self.assertEqual(job.raw_metadata["job_posting_id"], 101)
        self.assertEqual([call[0] for call in session.calls], ["get", "get", "post"])
        self.assertEqual(session.dayforce_pagination["detail_requests"], 0)
        self.assertTrue(source_scan_metadata(session)["scan_complete"])
        self.assertEqual(
            source_scan_metadata(session)["scan_completeness_reason"],
            "reported_total_enumerated",
        )
        search = session.calls[-1]
        self.assertEqual(search[1], target().endpoint_url)
        self.assertEqual(search[2]["headers"]["X-CSRF-TOKEN"], "a" * 64)
        self.assertEqual(
            search[2]["json"],
            {
                "clientNamespace": "example",
                "jobBoardCode": "candidateportal",
                "cultureCode": "en-CA",
                "paginationStart": 0,
            },
        )

    def test_pagination_uses_bounded_offsets_and_terminates_at_total(self):
        first = [posting(index, 1000 + index) for index in range(1, 26)]
        second = [posting(index, 1000 + index) for index in range(26, 31)]
        session = Session([page(first, total=30), page(second, total=30, offset=25)])

        jobs = dayforce.fetch_jobs(session, target())

        self.assertEqual(len(jobs), 30)
        posts = [call for call in session.calls if call[0] == "post"]
        self.assertEqual([call[2]["json"]["paginationStart"] for call in posts], [0, 25])
        self.assertEqual(session.dayforce_pagination["stop_reason"], "total_jobs_reached")
        self.assertEqual(session.dayforce_pagination["total_available"], 30)

    def test_zero_result_board_is_valid(self):
        session = Session([page([], total=0)])

        self.assertEqual(dayforce.fetch_jobs(session, target()), [])
        self.assertEqual(session.dayforce_pagination["stop_reason"], "total_jobs_reached")

    def test_malformed_response_fails_closed(self):
        with self.assertRaisesRegex(AdapterError, "jobPostings"):
            dayforce.fetch_jobs(Session([Response({})]), target())

    def test_duplicate_posting_id_and_repeated_page_fail_closed(self):
        duplicate = [posting(101, 501), posting(101, 502)]
        with self.assertRaisesRegex(AdapterError, "repeated stable posting ID"):
            dayforce.fetch_jobs(Session([page(duplicate)]), target())

        repeated = [posting(index, 1000 + index) for index in range(1, 26)]
        with self.assertRaisesRegex(AdapterError, "repeated a result page"):
            dayforce.fetch_jobs(
                Session([page(repeated, total=50), page(repeated, total=50, offset=25)]),
                target(),
            )

    def test_duplicate_requisition_is_normalized_once(self):
        session = Session(
            [page([posting(101, 501), posting(102, 501, jobTitle="Stage en logiciel")])]
        )

        jobs = dayforce.fetch_jobs(session, target())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(session.dayforce_pagination["duplicate_requisitions_skipped"], 1)
        self.assertEqual(
            session.dayforce_pagination["duplicate_posting_ids_skipped"], [102]
        )
        self.assertEqual(
            session.dayforce_pagination["duplicate_requisition_ids"], [501]
        )

    def test_localized_postings_share_stable_source_and_requisition_identity(self):
        english_config = dayforce.validate_target(target())
        french_target = target(
            locale="fr-CA",
            search_url="https://jobs.dayforcehcm.com/fr-CA/example/CANDIDATEPORTAL",
        )
        french_config = dayforce.validate_target(french_target)
        english = dayforce._normalize(
            posting(101, 501), target(), english_config,
            {"jobBoardId": 1},
        )
        french = dayforce._normalize(
            posting(202, 501, jobTitle="Stagiaire en logiciel"),
            french_target,
            french_config,
            {"jobBoardId": 1},
        )

        self.assertEqual((english.source, english.job_id), (french.source, french.job_id))
        self.assertNotEqual(english.url, french.url)

    def test_student_filter_and_canadian_location_compatibility(self):
        job = dayforce.fetch_jobs(Session([page([posting()])]), target())[0]

        self.assertIsNone(student_job_exclusion_reason(job))
        self.assertIsNone(student_location_exclusion_reason(job))

    def test_virtual_location_is_explicit_and_keeps_offices(self):
        job = dayforce.fetch_jobs(
            Session([page([posting(hasVirtualLocation=True)])]), target()
        )[0]

        self.assertEqual(job.location, "Remote | Calgary, AB, Canada")
        self.assertEqual(job.offices, ["Remote", "Calgary, AB, Canada"])

    def test_virtual_only_posting_may_omit_location_array(self):
        job = dayforce.fetch_jobs(
            Session([page([posting(hasVirtualLocation=True, postingLocations=None)])]),
            target(),
        )[0]

        self.assertEqual(job.location, "Remote")
        self.assertEqual(job.offices, ["Remote"])

        with self.assertRaisesRegex(AdapterError, "no structured location"):
            dayforce.fetch_jobs(
                Session(
                    [page([posting(postingLocations=None)])],
                    details=[detail_html(posting(postingLocations=None))],
                ),
                target(),
            )

    def test_singleton_posting_location_uses_the_same_object_contract(self):
        singleton = {
            "formattedAddress": "Edmonton, AB, Canada",
            "cityName": "Edmonton",
            "stateCode": "AB",
            "isoCountryCode": "CA",
        }

        job = dayforce.fetch_jobs(
            Session([page([posting(postingLocations=singleton)])]),
            target(),
        )[0]

        self.assertEqual(job.location, "Edmonton, AB, Canada")
        self.assertEqual(job.offices, ["Edmonton, AB, Canada"])
        self.assertEqual(job.raw_metadata["posting_locations"], [singleton])

    def test_empty_location_list_remains_distinct_from_null(self):
        job = dayforce.fetch_jobs(
            Session([page([posting(postingLocations=[])])]),
            target(),
        )[0]

        self.assertEqual(job.location, "")
        self.assertEqual(job.offices, [])
        with self.assertRaisesRegex(AdapterError, "no structured location"):
            dayforce.fetch_jobs(
                Session(
                    [page([posting(postingLocations=None)])],
                    details=[detail_html(posting(postingLocations=None))],
                ),
                target(),
            )

    def test_null_posting_locations_uses_explicit_structured_fallback(self):
        job = dayforce.fetch_jobs(
            Session([page([posting(
                postingLocations=None,
                primaryLocation={
                    "address": {
                        "city": "Edmonton",
                        "stateProvince": {"codeValue": "AB"},
                        "country": {"longName": "Canada"},
                    }
                },
            )])]),
            target(),
        )[0]

        self.assertEqual(job.location, "Edmonton, AB, Canada")
        self.assertEqual(job.offices, ["Edmonton, AB, Canada"])

    def test_null_posting_locations_without_explicit_fallback_reports_shape(self):
        secret = "description text " + ("x" * 200)
        row = posting(
            postingLocations=None,
            jobDescription=secret,
            opaqueLocationId="LOC-14987",
        )
        with self.assertRaisesRegex(AdapterError, "postingLocations is null") as caught:
            dayforce._normalize(
                row,
                target(),
                dayforce.validate_target(target()),
                {"jobBoardId": 1},
            )
        message = str(caught.exception)
        self.assertIn("virtual=false", message)
        self.assertIn("opaqueLocationId", message)
        self.assertIn("postingLocations: entries=1; shape=null", message)
        self.assertNotIn(secret, message)

    def test_null_listing_location_uses_identity_checked_detail_location(self):
        listing = posting(postingLocations=None)
        detail = posting(
            postingLocations=[{
                "cityName": "Edmonton",
                "stateCode": "AB",
                "isoCountryCode": "CA",
            }]
        )
        session = Session([page([listing])], details=[detail_html(detail)])

        job = dayforce.fetch_jobs(session, target())[0]

        self.assertEqual(job.location, "Edmonton, AB, CA")
        self.assertEqual(session.dayforce_pagination["detail_requests"], 1)
        self.assertEqual(
            [call[1] for call in session.calls if "/jobs/" in call[1]],
            ["https://jobs.dayforcehcm.com/en-CA/example/CANDIDATEPORTAL/jobs/101"],
        )

    def test_null_listing_location_rejects_malformed_or_missing_detail_location(self):
        listing = posting(postingLocations=None)
        cases = (
            (posting(postingLocations=[{"cityName": 42}]), "location is invalid"),
        )
        for detail, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AdapterError, message):
                    dayforce.fetch_jobs(
                        Session([page([listing])], details=[detail_html(detail)]),
                        target(),
                    )

    def test_identity_valid_locationless_posting_isolated_from_valid_sibling(self):
        valid = posting(101, 501)
        locationless = posting(102, 502, postingLocations=None)
        session = Session(
            [page([valid, locationless], total=2)],
            details=[detail_html(posting(102, 502, postingLocations=None))],
        )

        jobs = dayforce.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "dayforce:example:candidateportal:501")
        self.assertEqual(jobs[0].location, "Calgary, AB, Canada")
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertEqual(
            metadata["rejected_record_ids"],
            [{"posting_id": 102, "requisition_id": 502}],
        )
        self.assertEqual(metadata["rejected_posting_ids"], [102])
        self.assertEqual(metadata["rejected_requisition_ids"], [502])
        self.assertEqual(
            metadata["rejected_record_reason"], "missing_structured_location"
        )
        self.assertEqual(metadata["malformed_records"], 1)
        self.assertEqual(metadata["stop_reason"], "total_jobs_reached")
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"], "malformed_listing_records"
        )

    def test_all_identity_valid_locationless_postings_fail_the_source(self):
        listings = [
            posting(101, 501, postingLocations=None),
            posting(102, 502, postingLocations=None),
        ]
        details = [
            detail_html(posting(101, 501, postingLocations=None)),
            detail_html(posting(102, 502, postingLocations=[])),
        ]
        session = Session([page(listings, total=2)], details=details)

        with self.assertRaisesRegex(AdapterError, "no structured location"):
            dayforce.fetch_jobs(session, target())

        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["rejected_record_count"], 2)
        self.assertEqual(metadata["malformed_records"], 2)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"], "malformed_listing_records"
        )

    def test_locationless_isolation_does_not_relax_identity_contracts(self):
        listing = posting(postingLocations=None)
        valid_detail = posting(
            postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}]
        )
        cases = (
            (
                posting(postingLocations=None, clientNamespace="other"),
                valid_detail,
                {},
                "tenant boundaries",
            ),
            (
                posting(postingLocations=None, jobBoardId=99),
                valid_detail,
                {},
                "job-board boundaries",
            ),
            (
                listing,
                posting(
                    posting_id=999,
                    postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
                ),
                {},
                "identity mismatch",
            ),
            (
                listing,
                posting(
                    requisition_id=999,
                    postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
                ),
                {},
                "identity mismatch",
            ),
            (
                listing,
                posting(
                    postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
                    jobTitle="Different title",
                ),
                {},
                "title identity mismatch",
            ),
        )
        for candidate, detail, query, message in cases:
            with self.subTest(message=message):
                session = Session(
                    [page([candidate])], details=[detail_html(detail, query=query)]
                )
                with self.assertRaisesRegex(AdapterError, message):
                    dayforce.fetch_jobs(session, target())
                self.assertEqual(
                    source_scan_metadata(session)["rejected_record_count"], 0
                )

        route_detail = posting(
            postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}]
        )
        route_detail.pop("jobPostingId")
        route_detail.pop("jobReqId")
        route_session = Session(
            [page([listing])],
            details=[detail_html(route_detail, query={"postingId": "999"})],
        )
        with self.assertRaisesRegex(AdapterError, "canonical route identity mismatch"):
            dayforce.fetch_jobs(route_session, target())
        self.assertEqual(
            source_scan_metadata(route_session)["rejected_record_count"], 0
        )

        with self.assertRaisesRegex(AdapterError, "search_url"):
            dayforce.validate_target(
                target(search_url="https://evil.example/en-CA/example/CANDIDATEPORTAL")
            )

    def test_null_listing_location_rejects_detail_identity_mismatch(self):
        listing = posting(postingLocations=None)
        mismatched = posting(
            posting_id=999,
            postingLocations=[{"formattedAddress": "Edmonton, AB, Canada"}],
        )

        with self.assertRaisesRegex(AdapterError, "identity mismatch"):
            dayforce.fetch_jobs(
                Session([page([listing])], details=[detail_html(mismatched)]),
                target(),
            )

    def test_detail_accepts_alternate_structured_identity_representations(self):
        listing = posting(postingLocations=None)
        detail = posting(
            postingLocations=[{"formattedAddress": "Edmonton, AB, Canada"}],
        )
        detail.pop("jobPostingId")
        detail.pop("jobReqId")
        detail["canonicalPostingUrl"] = (
            "https://jobs.dayforcehcm.com/en-CA/example/CANDIDATEPORTAL/jobs/101"
        )
        detail["identifiers"] = [
            {"type": "requisitionId", "value": 501},
        ]

        job = dayforce.fetch_jobs(
            Session([page([listing])], details=[detail_html(detail)]),
            target(),
        )[0]

        self.assertEqual(job.location, "Edmonton, AB, Canada")

    def test_detail_accepts_nested_name_code_identity(self):
        listing = posting(postingLocations=None)
        detail = posting(
            postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
        )
        detail.pop("jobPostingId")
        detail.pop("jobReqId")
        detail["postingIdentifier"] = {
            "identifierType": "postingId",
            "codeValue": "101",
        }
        detail["requisitionIdentifier"] = {
            "name": "requisitionId",
            "id": "501",
        }

        job = dayforce.fetch_jobs(
            Session([page([listing])], details=[detail_html(detail)]),
            target(),
        )[0]

        self.assertEqual(job.location, "Calgary, AB, Canada")

    def test_detail_accepts_canonical_route_identity_with_separate_payload(self):
        listing = posting(postingLocations=None)
        detail = posting(
            postingLocations=[{"formattedAddress": "Vancouver, BC, Canada"}],
        )
        detail.pop("jobPostingId")
        detail.pop("jobReqId")

        job = dayforce.fetch_jobs(
            Session(
                [page([listing])],
                details=[detail_html(detail, query={"postingId": "101"})],
            ),
            target(),
        )[0]

        self.assertEqual(job.location, "Vancouver, BC, Canada")

    def test_detail_reference_number_must_match_listing_reference(self):
        listing = posting(postingLocations=None, referenceNumber="REQ-501")
        detail = posting(
            postingLocations=[{"formattedAddress": "Toronto, ON, Canada"}],
            referenceNumber="REQ-501",
        )
        detail.pop("jobPostingId")
        detail.pop("jobReqId")
        job = dayforce.fetch_jobs(
            Session([page([listing])], details=[detail_html(detail)]),
            target(),
        )[0]
        self.assertEqual(job.location, "Toronto, ON, Canada")

        detail["referenceNumber"] = "REQ-999"
        with self.assertRaisesRegex(AdapterError, "identity mismatch"):
            dayforce.fetch_jobs(
                Session([page([listing])], details=[detail_html(detail)]),
                target(),
            )

    def test_detail_rejects_ambiguous_conflicting_and_absent_identity(self):
        listing = posting(postingLocations=None)
        ambiguous = posting(
            postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
            canonicalUrl=(
                "https://jobs.dayforcehcm.com/en-CA/example/CANDIDATEPORTAL/jobs/999"
            ),
        )
        absent = posting(
            postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
        )
        absent.pop("jobPostingId")
        absent.pop("jobReqId")

        for detail, message in (
            (ambiguous, "ambiguous structured identities"),
            (absent, "did not expose one structured posting identity"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(AdapterError, message):
                    dayforce.fetch_jobs(
                        Session([page([listing])], details=[detail_html(detail)]),
                        target(),
                    )

    def test_missing_location_detail_enrichment_is_bounded(self):
        count = dayforce.MAX_DETAIL_REQUESTS + 1
        listings = [
            posting(index, 500 + index, postingLocations=None)
            for index in range(1, count + 1)
        ]
        details = [
            detail_html(posting(
                index,
                500 + index,
                postingLocations=[{"formattedAddress": "Calgary, AB, Canada"}],
            ))
            for index in range(1, dayforce.MAX_DETAIL_REQUESTS + 1)
        ]
        session = Session([page(listings)], details=details)

        with self.assertRaisesRegex(AdapterError, "detail enrichment exceeded"):
            dayforce.fetch_jobs(session, target())

        self.assertEqual(session.dayforce_pagination["detail_requests"], 10)
        self.assertEqual(
            source_scan_metadata(session)["scan_completeness_reason"],
            "missing_location_detail_enrichment_limit_reached",
        )
        self.assertEqual(
            len([call for call in session.calls if "/jobs/" in call[1]]),
            dayforce.MAX_DETAIL_REQUESTS,
        )

    def test_malformed_location_objects_and_collection_shapes_fail_closed(self):
        cases = (
            ({"items": [{"formattedAddress": "Calgary, AB"}]}, "no stable display value"),
            ({"formattedAddress": {"value": "Calgary, AB"}}, "must be text"),
            ([{"cityName": 42}], "must be text"),
            (["Calgary, AB"], "must be an object"),
            ("Calgary, AB", "must be a list or location object"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(AdapterError, message):
                    dayforce.fetch_jobs(
                        Session([page([posting(postingLocations=value)])]),
                        target(),
                    )

    def test_tenant_locale_board_and_bounds_are_validated(self):
        with self.assertRaisesRegex(AdapterError, "search_url"):
            dayforce.validate_target(target(search_url="https://evil.example/jobs"))
        with self.assertRaisesRegex(AdapterError, "locale"):
            dayforce.validate_target(target(locale="english"))
        with self.assertRaisesRegex(AdapterError, "max_pages"):
            dayforce.validate_target(target(max_pages=dayforce.MAX_PAGES + 1))
        with self.assertRaisesRegex(AdapterError, "page capacity"):
            dayforce.validate_target(target(max_pages=1, max_jobs=50))
        with self.assertRaisesRegex(AdapterError, "max_response_bytes"):
            dayforce.validate_target(target(max_response_bytes=dayforce.MAX_RESPONSE_BYTES + 1))

    def test_scanner_isolates_a_dayforce_source_failure(self):
        targets = [target(name="Broken Dayforce"), CompanyTarget("Healthy", "greenhouse")]
        healthy_job = JobRecord(
            job_id="1",
            title="Software Engineering Intern",
            company="Healthy",
            location="Toronto, ON, Canada",
            url="https://example.test/jobs/1",
            updated_at="2026-08-28",
            source="greenhouse:healthy",
        )

        def broken(*_args):
            raise AdapterError("Dayforce malformed response")

        with patch.dict(
            scanner.ADAPTERS,
            {"dayforce": broken, "greenhouse": lambda *_args: [healthy_job]},
        ):
            report = scanner.CompanyCareersScanner(
                targets=targets, parent_source_mappings=[]
            ).scan()

        self.assertEqual(report.results[0].status, CoverageStatus.FAILED_TECHNICAL)
        self.assertEqual(report.results[1].status, CoverageStatus.FETCHED)
        self.assertEqual(report.results[1].jobs, [healthy_job])

    def test_registry_configuration_and_production_targets_are_unique(self):
        entry = {
            "name": "Example Canada",
            "platform": "dayforce",
            "tenant": "example",
            "site_id": "CANDIDATEPORTAL",
            "locale": "en-CA",
            "search_url": "https://jobs.dayforcehcm.com/en-CA/example/CANDIDATEPORTAL",
            "endpoint_url": "https://jobs.dayforcehcm.com/api/geo/example/jobposting/search",
            "endpoint_variant": "nextjs_jobposting_search",
            "allowed_apply_hosts": ["jobs.dayforcehcm.com"],
            "page_size": 25,
            "max_pages": 2,
            "max_jobs": 50,
        }
        self.assertIsNone(registry_validator.configuration_error(entry))
        self.assertEqual(
            registry_validator.source_key(entry),
            "dayforce:example:candidateportal",
        )

        registry = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config" / "target_companies.yaml")
            .read_text(encoding="utf-8")
        )["target_companies"]
        dayforce_entries = [row for row in registry if row.get("platform") == "dayforce"]
        self.assertEqual(
            {row["name"] for row in dayforce_entries},
            {
                "WestJet",
                "SMART Technologies",
                "Ross Video",
                "Payments Canada",
                "Chemelex",
            },
        )
        source_keys = [registry_validator.source_key(row) for row in registry]
        populated = [key for key in source_keys if key]
        self.assertEqual(len(populated), len(set(populated)))


if __name__ == "__main__":
    unittest.main()
