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
    def __init__(self, posts, *, shell=None, csrf=None):
        self.posts = list(posts)
        self.get_responses = [
            shell or Response(text=shell_html(), content_type="text/html; charset=utf-8"),
            csrf or Response({"csrfToken": "a" * 64}),
        ]
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
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

        with self.assertRaisesRegex(AdapterError, "postingLocations"):
            dayforce.fetch_jobs(
                Session([page([posting(postingLocations=None)])]),
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
