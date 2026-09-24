import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from src.scraper.company_careers.adapters import (
    amazon,
    ashby,
    atlassian,
    google,
    greenhouse,
    ibm_search,
    lever,
    meta,
    shopify,
    uber,
    workday,
)
from src.scraper.company_careers import scanner as scanner_module
from src.scraper.company_careers.coverage_report import coverage_markdown, failure_items
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
    source_scan_metadata,
)
from src.scraper.company_careers.relevance import rank_jobs, score_job
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.student_roles import (
    build_student_job_filter,
    build_student_location_filter,
    classify_student_location,
    configure_student_targets,
    rank_student_jobs,
    score_student_job,
    student_job_exclusion_reason,
    write_student_ranked_jobs_artifacts,
)
from src.utils.job_database import JobDatabase
from src.utils.job_search_pipeline import JobSearchPipeline


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None, headers=None, url=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.headers = dict(headers or {})
        self.content = self.text.encode("utf-8")
        self.url = url

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        if not self.get_responses:
            raise AssertionError("Unexpected GET")
        return self.get_responses.pop(0)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        if not self.post_responses:
            raise AssertionError("Unexpected POST")
        return self.post_responses.pop(0)


class CompanyCareersTests(unittest.TestCase):
    def _student_job(
        self,
        title,
        url="https://example.com/job/1",
        location="Remote",
        updated_at="2026-08-03",
    ):
        return JobRecord(
            job_id=title,
            title=title,
            company="Example",
            location=location,
            url=url,
            updated_at=updated_at,
            source="test",
        )

    def test_student_role_filter_retains_intern_coop_and_student_titles(self):
        retained = [
            "Software Engineering Intern",
            "Embedded Systems Co-op",
            "Machine Learning Student Researcher",
        ]
        for title in retained:
            with self.subTest(title=title):
                self.assertIsNone(student_job_exclusion_reason(self._student_job(title)))

    def test_student_role_filter_excludes_senior_staff_and_principal_titles(self):
        excluded = [
            "Senior Software Engineer",
            "Staff Machine Learning Engineer",
            "Principal Engineer Intern Programs",
        ]
        for title in excluded:
            with self.subTest(title=title):
                self.assertEqual(
                    student_job_exclusion_reason(self._student_job(title)),
                    "seniority",
                )

    def test_student_role_filter_excludes_every_requested_seniority(self):
        for seniority in (
            "Senior",
            "Staff",
            "Principal",
            "Manager",
            "Director",
            "Lead",
            "Architect",
        ):
            title = f"{seniority} Software Engineering Intern"
            with self.subTest(title=title):
                self.assertEqual(
                    student_job_exclusion_reason(self._student_job(title)),
                    "seniority",
                )

    def test_student_location_eligibility_boundaries(self):
        cases = [
            (
                "Software Engineering Intern",
                "Toronto, Canada",
                "eligible_canada",
                None,
            ),
            (
                "Embedded Systems Co-op",
                "Calgary, Alberta",
                "eligible_canada",
                None,
            ),
            (
                "Machine Learning Intern",
                "Remote Canada",
                "eligible_remote_canada",
                None,
            ),
            (
                "Software Engineering Intern",
                "Remote - USA",
                "eligible_remote_us",
                None,
            ),
            (
                "Software Engineering Intern",
                "United States",
                "eligible_us_onsite",
                None,
            ),
            (
                "Software Engineering Intern",
                "Seattle, WA",
                "eligible_us_onsite",
                None,
            ),
            (
                "Chip Simulation Software Intern",
                "San Jose",
                "eligible_us_onsite",
                None,
            ),
            (
                "Software Engineering Intern",
                "Remote",
                "eligible_remote_unspecified",
                None,
            ),
            (
                "Software Engineering Intern",
                "",
                "manual_review_missing",
                None,
            ),
        ]
        location_filter = build_student_location_filter(
            locations=["Canada", "Remote"]
        )
        for title, location, status, exclusion in cases:
            job = self._student_job(title, location=location)
            with self.subTest(title=title, location=location):
                self.assertEqual(
                    classify_student_location(
                        job,
                        locations=["Canada", "Remote"],
                    ),
                    status,
                )
                self.assertEqual(location_filter(job), exclusion)

    def test_student_location_allows_worldwide_or_canada_with_us_mention(self):
        locations = ["Canada", "Remote"]
        worldwide = self._student_job(
            "Software Engineering Intern",
            location="Remote - Worldwide, including United States",
        )
        canada_and_us = self._student_job(
            "Software Engineering Intern",
            location="Canada or United States",
        )

        self.assertEqual(
            classify_student_location(worldwide, locations=locations),
            "eligible_worldwide",
        )
        self.assertEqual(
            classify_student_location(canada_and_us, locations=locations),
            "eligible_canada",
        )

    def test_student_location_does_not_parse_description_restrictions(self):
        job = self._student_job("Security Engineering Intern", location="Remote")
        job.description = "The company also has employees in the United States."

        self.assertEqual(
            classify_student_location(job, locations=["Canada", "Remote"]),
            "eligible_remote_unspecified",
        )

    def test_student_location_does_not_infer_eligibility_from_ambiguous_text(self):
        global_business = self._student_job(
            "Global Customer Services Intern",
            location="In-Office",
        )
        australian_victoria = self._student_job(
            "Software Engineering Intern",
            location="Melbourne, Victoria, AUS",
        )

        self.assertEqual(
            classify_student_location(
                global_business,
                locations=["Canada", "Remote"],
            ),
            "excluded_other_location",
        )
        self.assertEqual(
            classify_student_location(
                australian_victoria,
                locations=["Canada", "Remote"],
            ),
            "excluded_other_location",
        )

    def test_student_role_filter_excludes_restricted_programs_only_by_title(self):
        restricted = self._student_job("Detection Engineer - SkillBridge Intern")
        ordinary = self._student_job("Security Engineering Intern")

        self.assertEqual(student_job_exclusion_reason(restricted), "eligibility")
        self.assertIsNone(student_job_exclusion_reason(ordinary))

    def test_student_relevance_prefers_canadian_embedded_internship(self):
        canadian_intern = self._student_job(
            "Embedded Firmware Software Engineering Intern",
            location="Calgary, Alberta",
        )
        us_staff = self._student_job(
            "Staff Software Engineer",
            location="Remote - USA",
            url="https://example.com/job/2",
        )

        ranked = rank_student_jobs(
            [us_staff, canadian_intern],
            locations=["Canada", "Remote"],
        )
        intern_score = score_student_job(
            canadian_intern,
            locations=["Canada", "Remote"],
        )

        self.assertEqual(ranked[0]["title"], canadian_intern.title)
        self.assertGreater(ranked[0]["relevance_score"], ranked[1]["relevance_score"])
        self.assertIn("embedded_firmware", intern_score["matched_categories"])
        self.assertIn("eligible_canada", intern_score["positive_signals"])
        self.assertEqual(ranked[1]["location_status"], "eligible_remote_us")
        self.assertIn("staff", ranked[1]["negative_signals"])

    def test_student_relevance_rewards_recent_parseable_source_dates(self):
        recent = self._student_job(
            "Software Engineering Intern",
            location="Toronto, Canada",
            updated_at="2026-07-28T12:00:00+00:00",
        )
        old = self._student_job(
            "Software Engineering Intern",
            location="Toronto, Canada",
            updated_at="2025-01-01T12:00:00+00:00",
        )
        now = datetime(2026, 8, 3, tzinfo=timezone.utc)

        recent_score = score_student_job(
            recent,
            locations=["Canada", "Remote"],
            now=now,
        )
        old_score = score_student_job(
            old,
            locations=["Canada", "Remote"],
            now=now,
        )

        self.assertGreater(recent_score["score"], old_score["score"])
        self.assertIn(
            "recent_source_date_within_14_days",
            recent_score["positive_signals"],
        )

    def test_new_grad_titles_remain_excluded_even_with_legacy_opt_in(self):
        for title in ("New Grad Software Engineer", "Entry-Level Software Engineer"):
            job = self._student_job(title)
            with self.subTest(title=title):
                expected = "new_grad" if title.startswith("New Grad") else "job_type"
                self.assertEqual(student_job_exclusion_reason(job), expected)
                self.assertEqual(
                    student_job_exclusion_reason(job, include_new_grad=True),
                    expected,
                )

    def test_student_filter_reports_location_and_title_exclusions(self):
        jobs = [
            self._student_job("Software Engineering Intern", url="https://example.com/1"),
            self._student_job("Senior Software Engineer", url="https://example.com/2"),
            self._student_job("Software Engineer", url="https://example.com/3"),
            JobRecord(
                job_id="4",
                title="Embedded Systems Co-op",
                company="Example",
                location="London",
                url="https://example.com/4",
                updated_at="2026-08-03",
                source="test",
            ),
        ]
        scanner = CompanyCareersScanner(targets=[])

        retained, exclusions = scanner._filter_jobs(
            jobs,
            keywords=None,
            locations=["Canada", "Remote"],
            job_filter=build_student_job_filter(),
        )

        self.assertEqual([job.title for job in retained], ["Software Engineering Intern"])
        self.assertEqual(exclusions, {"seniority": 1, "job_type": 1, "location": 1})

    def test_student_search_overrides_only_role_query_collectors(self):
        targets = [
            CompanyTarget(name="Google", platform="google", search_terms=["Staff"]),
            CompanyTarget(name="HashiCorp", platform="ibm_search", search_terms=["hashicorp"]),
        ]

        configured = configure_student_targets(targets)

        self.assertEqual(configured[0].search_terms[0], "intern")
        self.assertEqual(configured[1].search_terms, ["hashicorp"])
        self.assertEqual(targets[0].search_terms, ["Staff"])

    def test_database_deduplicates_student_job_by_source_url(self):
        with TemporaryDirectory() as tmpdir:
            database = JobDatabase(os.path.join(tmpdir, "jobs.db"))
            try:
                job = {
                    "source_url": "https://example.com/student-job",
                    "job_title": "Software Engineering Intern",
                    "company_name": "Example",
                }
                with redirect_stdout(StringIO()):
                    self.assertTrue(database.add_job(job))
                    self.assertTrue(database.add_job(job))
                self.assertEqual(len(database.get_jobs()), 1)
            finally:
                database.close()

    def test_existing_company_collectors_remain_callable(self):
        self.assertTrue(scanner_module.ADAPTERS)
        self.assertTrue(all(callable(adapter) for adapter in scanner_module.ADAPTERS.values()))

    def test_greenhouse_normalization(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Staff Software Engineer",
                                "location": {"name": "Remote US"},
                                "absolute_url": "https://example.com/job/123",
                                "updated_at": "2026-04-01",
                                "departments": [{"name": "Engineering"}],
                                "offices": [{"name": "Remote"}],
                                "content": "Build platform systems.",
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")

        jobs = greenhouse.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "123")
        self.assertEqual(jobs[0].company, "Example")
        self.assertEqual(jobs[0].source, "greenhouse:example")
        self.assertEqual(jobs[0].departments, ["Engineering"])

    def test_greenhouse_upgrades_legacy_http_apply_url(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Engineer",
                                "absolute_url": "http://company.example/jobs/123?gh_jid=123",
                                "location": {"name": "Remote"},
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")

        jobs = greenhouse.fetch_jobs(session, target)

        self.assertEqual(jobs[0].url, "https://company.example/jobs/123?gh_jid=123")

    def test_lever_normalization(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    [
                        {
                            "id": "abc",
                            "text": "Principal Engineer",
                            "hostedUrl": "https://jobs.lever.co/example/abc",
                            "createdAt": 1770000000,
                            "categories": {"team": "Core", "location": "London"},
                            "descriptionPlain": "Own distributed services.",
                        }
                    ]
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="lever", slug="example")

        jobs = lever.fetch_jobs(session, target)

        self.assertEqual(jobs[0].title, "Principal Engineer")
        self.assertEqual(jobs[0].location, "London")
        self.assertEqual(jobs[0].offices, ["London"])

    def test_ashby_normalization(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": "ashby-1",
                                "title": "Staff Infrastructure Engineer",
                                "location": "Bengaluru",
                                "publishedDate": "2026-04-02",
                                "jobUrl": "https://jobs.ashbyhq.com/example/ashby-1",
                                "department": "Infrastructure",
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="ashby", slug="Example")

        jobs = ashby.fetch_jobs(session, target)

        self.assertEqual(jobs[0].job_id, "ashby-1")
        self.assertEqual(jobs[0].source, "ashby:Example")
        self.assertEqual(jobs[0].departments, ["Infrastructure"])

    def test_workday_normalization(self):
        session = FakeSession(
            post_responses=[
                FakeResponse(
                    {
                        "jobPostings": [
                            {
                                "title": "Senior Principal Engineer",
                                "locationsText": "Dublin",
                                "postedOn": "Posted 3 Days Ago",
                                "externalPath": "/job/Dublin/Senior-Principal_JR1",
                                "bulletFields": ["JR1"],
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            max_pages=1,
        )

        jobs = workday.fetch_jobs(session, target)

        self.assertEqual(jobs[0].job_id, "JR1")
        self.assertEqual(jobs[0].source, "workday:example")
        self.assertIn("/en-US/External/job/Dublin", jobs[0].url)

    def test_workday_uses_non_id_bullet_fields_when_location_text_is_omitted(self):
        session = FakeSession(
            post_responses=[
                FakeResponse(
                    {
                        "jobPostings": [
                            {
                                "title": "Software Engineer",
                                "postedOn": "Posted Today",
                                "externalPath": "/job/Canada-Toronto-Ontario/Software-Engineer_JR2",
                                "bulletFields": ["Toronto", "Ontario", "JR2"],
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            max_pages=1,
        )

        jobs = workday.fetch_jobs(session, target)

        self.assertEqual(jobs[0].job_id, "JR2")
        self.assertEqual(jobs[0].location, "Toronto, Ontario")

    def test_workday_uses_official_path_location_when_listing_fields_omit_it(self):
        session = FakeSession(
            post_responses=[
                FakeResponse(
                    {
                        "jobPostings": [
                            {
                                "title": "Data Analyst",
                                "externalPath": "/job/Toronto-ON/Data-Analyst_JR3",
                                "bulletFields": ["JR3"],
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            max_pages=1,
        )

        jobs = workday.fetch_jobs(session, target)

        self.assertEqual(jobs[0].location, "Toronto ON")

    def test_workday_falls_back_to_site_id_variants(self):
        session = FakeSession(
            post_responses=[
                FakeResponse({}, status_code=404),
                FakeResponse({}, status_code=404),
                FakeResponse({}, status_code=404),
                FakeResponse(
                    {
                        "jobPostings": [
                            {
                                "title": "Principal Engineer",
                                "locationsText": "Remote",
                                "externalPath": "/job/Remote/Principal-Engineer_JR2",
                            }
                        ]
                    }
                ),
            ]
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            site_ids=["ExternalCareers"],
            max_pages=1,
        )

        jobs = workday.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertIn("/en-US/ExternalCareers/job/Remote", jobs[0].url)
        self.assertEqual(len(session.post_calls), 4)

    def test_workday_discovers_and_validates_replacement_site_metadata(self):
        host = "example.wd1.myworkdayjobs.com"
        replacement = "External_Careers"
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {},
                    text=f'<a href="https://{host}/en-US/{replacement}/jobs">Jobs</a>',
                    headers={"content-type": "text/html"},
                    url="https://careers.example.com/jobs",
                ),
                FakeResponse(
                    {},
                    text=(
                        'window.workday = { "tenant": "example", '
                        f'"siteId": "{replacement}" }};'
                    ),
                    headers={"content-type": "text/html"},
                    url=f"https://{host}/en-US/{replacement}",
                ),
            ],
            post_responses=[
                FakeResponse({}, status_code=422),
                FakeResponse({}, status_code=422),
                FakeResponse({}, status_code=422),
                FakeResponse(
                    {
                        "total": 1,
                        "jobPostings": [
                            {
                                "title": "Controls Engineer",
                                "locationsText": "Calgary, AB",
                                "externalPath": "/job/Calgary/Controls-Engineer_R10001",
                                "bulletFields": ["R10001"],
                            }
                        ],
                    }
                ),
            ],
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            careers_url="https://careers.example.com/jobs",
            max_pages=1,
        )

        jobs = workday.fetch_jobs(session, target)

        self.assertEqual([job.job_id for job in jobs], ["R10001"])
        self.assertIn(f"/en-US/{replacement}/job/Calgary", jobs[0].url)
        self.assertEqual(len(session.get_calls), 2)
        self.assertEqual(len(session.post_calls), 4)
        metadata = getattr(session, "job_radar_scan_metadata")
        self.assertEqual(metadata["site_discovery"], "authoritative_workday_metadata")
        self.assertEqual(metadata["resolved_site_id"], replacement)

    def test_workday_rejects_unverified_or_cross_tenant_discovery(self):
        host = "example.wd1.myworkdayjobs.com"
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {},
                    text=f'<a href="https://{host}/OtherSite/jobs">Jobs</a>',
                    headers={"content-type": "text/html"},
                    url="https://careers.example.com/jobs",
                ),
                FakeResponse(
                    {},
                    text='window.workday = { tenant: "other", siteId: "OtherSite" };',
                    headers={"content-type": "text/html"},
                    url=f"https://{host}/OtherSite",
                ),
            ],
            post_responses=[FakeResponse({}, status_code=422) for _ in range(3)],
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            careers_url="https://careers.example.com/jobs",
            max_pages=1,
        )

        with self.assertRaisesRegex(AdapterError, "HTTP 422") as caught:
            workday.fetch_jobs(session, target)

        self.assertEqual(len(session.post_calls), 3)
        self.assertIn("configured_sites=[External]", str(caught.exception))
        self.assertIn("candidate_sites=[OtherSite]", str(caught.exception))
        self.assertIn("tenant/site metadata mismatch", str(caught.exception))
        metadata = getattr(session, "job_radar_scan_metadata")
        diagnostic = metadata["diagnostics"][-1]
        self.assertEqual(diagnostic["kind"], "workday_site_discovery")
        self.assertEqual(diagnostic["candidate_site_ids"], ["OtherSite"])

    def test_workday_discovery_reports_rejected_candidate_hostname(self):
        candidate_host = "other.wd3.myworkdayjobs.com"
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {},
                    text=f'<a href="https://{candidate_host}/OtherSite/jobs">Jobs</a>',
                    headers={"content-type": "text/html"},
                    url="https://careers.example.com/jobs",
                ),
            ],
            post_responses=[FakeResponse({}, status_code=422) for _ in range(3)],
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            careers_url="https://careers.example.com/jobs",
            max_pages=1,
        )

        with self.assertRaisesRegex(
            AdapterError, "candidate hostname tenant does not match configured tenant"
        ):
            workday.fetch_jobs(session, target)

        diagnostic = getattr(session, "job_radar_scan_metadata")["diagnostics"][-1]
        self.assertEqual(diagnostic["candidate_hostnames"], [candidate_host])
        self.assertEqual(diagnostic["candidate_site_ids"], ["OtherSite"])

    def test_workday_accepts_verified_same_tenant_alternate_wd_hostname(self):
        candidate_host = "example.wd3.myworkdayjobs.com"
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {},
                    text=f'<a href="https://{candidate_host}/OtherSite/jobs">Jobs</a>',
                    headers={"content-type": "text/html"},
                    url="https://careers.example.com/jobs",
                ),
                FakeResponse(
                    {},
                    text='window.workday={"tenant":"example","siteId":"OtherSite"};',
                    headers={"content-type": "text/html"},
                    url=f"https://{candidate_host}/OtherSite",
                ),
            ],
            post_responses=[
                FakeResponse({}, status_code=422),
                FakeResponse({}, status_code=422),
                FakeResponse({}, status_code=422),
                FakeResponse({
                    "total": 1,
                    "jobPostings": [{
                        "title": "Systems Engineer",
                        "locationsText": "Austin, TX",
                        "externalPath": "/job/Austin/Systems-Engineer_R20001",
                        "bulletFields": ["R20001"],
                    }],
                }),
            ],
        )
        target = CompanyTarget(
            name="Example",
            platform="workday",
            tenant="example",
            wd_subdomain="wd1",
            site_id="External",
            careers_url="https://careers.example.com/jobs",
            max_pages=1,
        )

        jobs = workday.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].url.startswith(f"https://{candidate_host}/"))
        metadata = getattr(session, "job_radar_scan_metadata")
        self.assertEqual(metadata["resolved_hostname"], candidate_host)

    def test_amazon_normalization_and_dedupe(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": "amz-1",
                                "title": "Principal SDE",
                                "normalized_location": "Seattle",
                                "posted_date": "2026-04-03",
                                "job_path": "/en/jobs/amz-1/principal-sde",
                                "business_category": "AWS",
                            },
                            {
                                "id": "amz-1",
                                "title": "Principal SDE",
                                "normalized_location": "Seattle",
                                "posted_date": "2026-04-03",
                                "job_path": "/en/jobs/amz-1/principal-sde",
                            },
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(
            name="Amazon",
            platform="amazon",
            search_terms=["Principal SDE"],
            max_pages=1,
        )

        jobs = amazon.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "custom:amazon")
        self.assertEqual(jobs[0].departments, ["AWS"])

    def test_shopify_embedded_data_normalization(self):
        encoded_values = [
            {"_1": 2},
            "loaderData",
            {"_3": 4},
            "route",
            {"_5": 6},
            "jobPosting",
            {
                "_7": 8,
                "_9": 10,
                "_11": 12,
                "_13": 14,
                "_15": 16,
                "_17": 18,
                "_19": 20,
                "_21": 22,
                "_23": 24,
                "_25": 26,
            },
            "id",
            "posting-1",
            "title",
            "Staff Software Engineer",
            "jobId",
            "job-1",
            "departmentName",
            "Engineering",
            "locationName",
            "Remote - Canada",
            "workplaceType",
            "Remote",
            "isListed",
            True,
            "publishedDate",
            "2026-04-01",
            "externalLink",
            "https://www.shopify.com/careers?ashby_jid=posting-1",
            "updatedAt",
            "2026-04-02T00:00:00Z",
        ]
        stream = json.dumps(json.dumps(encoded_values))
        html = f'<script>window.__reactRouterContext.streamController.enqueue({stream});</script>'
        session = FakeSession(get_responses=[FakeResponse({}, text=html)])
        target = CompanyTarget(name="Shopify", platform="shopify")

        jobs = shopify.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Staff Software Engineer")
        self.assertEqual(jobs[0].company, "Shopify")
        self.assertEqual(jobs[0].location, "Remote - Canada")
        self.assertEqual(jobs[0].source, "custom:shopify")
        self.assertEqual(jobs[0].departments, ["Engineering"])

    def test_google_af_init_data_normalization(self):
        google_data = [
            [
                [
                    "google-1",
                    "Staff Software Engineer",
                    "https://www.google.com/about/careers/applications/jobs/results/123",
                    [None, "<ul><li>Build services.</li></ul>"],
                    [None, "<ul><li>8 years experience.</li></ul>"],
                    "company-path",
                    None,
                    "Google",
                    "en-US",
                    [["Bengaluru, Karnataka, India", ["Address"], "Bengaluru", None, "KA", "IN"]],
                    [None, "<p>Build large scale systems.</p>"],
                    [2],
                    [1762358522, 0],
                    [1762358522, 0],
                    [1762358522, 0],
                ]
            ],
            None,
            1,
            20,
        ]
        html = "AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps(google_data) + "});"

        jobs = google._extract_jobs(html, "Google")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "google-1")
        self.assertEqual(jobs[0].title, "Staff Software Engineer")
        self.assertEqual(jobs[0].location, "Bengaluru, Karnataka, India")
        self.assertEqual(jobs[0].source, "custom:google")
        self.assertEqual(jobs[0].departments, ["Google"])
        self.assertIn("Build large scale systems.", jobs[0].description)

    def test_atlassian_normalization(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    [
                        {
                            "id": 24880,
                            "title": "Principal Software Engineer",
                            "locations": ["Remote - Remote"],
                            "category": "Engineering",
                            "overview": "<p>Build products.</p>",
                            "responsibilities": "<ul><li>Lead design.</li></ul>",
                            "qualifications": "<p>Distributed systems.</p>",
                            "applyUrl": "https://careers-americas.icims.com/jobs/24880/job?mode=apply",
                            "portalJobPost": {"updatedDate": "2026-04-13 03:26 AM"},
                        }
                    ]
                )
            ]
        )
        target = CompanyTarget(name="Atlassian", platform="atlassian")

        jobs = atlassian.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "24880")
        self.assertEqual(jobs[0].source, "custom:atlassian")
        self.assertEqual(jobs[0].departments, ["Engineering"])
        self.assertIn("Build products.", jobs[0].description)

    def test_atlassian_identical_duplicate_is_retained_as_mutable_incomplete(self):
        item = {
            "id": 24880,
            "title": "Principal Software Engineer",
            "locations": ["Remote - Remote"],
            "category": "Engineering",
            "applyUrl": "https://careers-americas.icims.com/jobs/24880/job?mode=apply",
            "portalJobPost": {"updatedDate": "2026-04-13 03:26 AM"},
        }
        session = FakeSession(get_responses=[FakeResponse([item, dict(item)])])

        jobs = atlassian.fetch_jobs(
            session, CompanyTarget(name="Atlassian", platform="atlassian")
        )
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["overlapping_jobs_skipped"], 1)
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "collection_changed_during_scan",
        )

    def test_uber_normalization(self):
        listing = """<!doctype html><html><body>
        <div data-slot="card" data-id="156408">
          <h2><a class="js-view-job" href="/en/jobs/156408/">Staff Software Engineer</a></h2>
          <div data-slot="card-description">
            <div class="rounded-full"><div>Sunnyvale, California</div></div>
            <div class="rounded-full"><div>Engineering</div></div>
          </div>
        </div></body></html>"""
        session = FakeSession(
            get_responses=[FakeResponse({}, text=listing, headers={"content-type": "text/html"})]
        )
        target = CompanyTarget(name="Uber", platform="uber", max_pages=1)

        jobs = uber.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "156408")
        self.assertEqual(jobs[0].location, "Sunnyvale, California")
        self.assertEqual(jobs[0].url, "https://jobs.uber.com/en/jobs/156408/")
        self.assertEqual(jobs[0].source, "custom:uber")
        self.assertEqual(jobs[0].departments, ["Engineering"])

    def test_ibm_search_normalization_for_hashicorp(self):
        session = FakeSession(
            post_responses=[
                FakeResponse(
                    {
                        "hits": {
                            "hits": [
                                {
                                    "_source": {
                                        "url": "https://careers.ibm.com/careers/JobDetail?jobId=108437",
                                        "title": "Engineering Manager - HashiCorp Terraform Actions",
                                        "description": "Build HashiCorp products.",
                                        "field_keyword_08": "Software Engineering",
                                        "field_keyword_19": "Multiple Cities",
                                    }
                                }
                            ]
                        }
                    }
                )
            ]
        )
        target = CompanyTarget(
            name="HashiCorp",
            platform="ibm_search",
            search_terms=["hashicorp"],
            max_pages=1,
        )

        jobs = ibm_search.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "108437")
        self.assertEqual(jobs[0].company, "HashiCorp")
        self.assertEqual(jobs[0].source, "custom:ibm_search")
        self.assertEqual(jobs[0].departments, ["Software Engineering"])

    def test_ibm_search_surfaces_authoritative_total(self):
        session = FakeSession(
            post_responses=[
                FakeResponse(
                    {
                        "hits": {
                            "total": {"value": 1, "relation": "eq"},
                            "hits": [
                                {
                                    "_source": {
                                        "url": "https://careers.ibm.com/careers/JobDetail?jobId=108437",
                                        "title": "Engineering Manager - HashiCorp",
                                        "description": "Build HashiCorp products.",
                                        "field_keyword_08": "Software Engineering",
                                        "field_keyword_19": "Multiple Cities",
                                    }
                                }
                            ],
                        }
                    }
                )
            ]
        )
        jobs = ibm_search.fetch_jobs(
            session,
            CompanyTarget(
                name="HashiCorp",
                platform="ibm_search",
                search_terms=["hashicorp"],
                max_pages=1,
            ),
        )
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 1)
        self.assertTrue(metadata["terminal_page_observed"])

    def test_meta_graphql_normalization(self):
        html = (
            '<script>requireLazy(["LSD"],function(){})</script>'
            '["LSD",[],{"token":"lsd-token"}]'
            '{"client_revision":123456789,"hsi":"987654321"}'
        )
        session = FakeSession(
            get_responses=[FakeResponse({}, text=html)],
            post_responses=[
                FakeResponse(
                    {
                        "data": {
                            "job_search_with_featured_jobs": {
                                "all_jobs": [
                                    {
                                        "id": "meta-1",
                                        "title": "Staff Software Engineer",
                                        "locations": ["Remote, US"],
                                        "teams": ["Software Engineering"],
                                        "sub_teams": ["Engineering"],
                                    }
                                ],
                                "featured_jobs": [],
                            }
                        }
                    }
                )
            ],
        )
        target = CompanyTarget(name="Meta", platform="meta", search_terms=["Staff Software Engineer"], max_pages=1)

        jobs = meta.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "meta-1")
        self.assertEqual(jobs[0].title, "Staff Software Engineer")
        self.assertEqual(jobs[0].company, "Meta")
        self.assertEqual(jobs[0].location, "Remote, US")
        self.assertEqual(jobs[0].url, "https://www.metacareers.com/jobs/meta-1/")
        self.assertEqual(jobs[0].source, "custom:meta")
        self.assertEqual(jobs[0].departments, ["Software Engineering"])
        self.assertEqual(jobs[0].offices, ["Remote, US"])
        self.assertIn("Teams: Software Engineering", jobs[0].description)
        post_data = session.post_calls[0][1]["data"]
        self.assertEqual(post_data["doc_id"], "29615178951461218")
        self.assertEqual(post_data["fb_api_req_friendly_name"], "CareersJobSearchResultsDataQuery")

    def test_scanner_reports_failed_technical(self):
        session = FakeSession(get_responses=[FakeResponse({}, status_code=404)])
        target = CompanyTarget(name="Broken", platform="greenhouse", slug="broken")
        scanner = CompanyCareersScanner(targets=[target], session=session)

        report = scanner.scan(keywords="Engineer", locations=["Remote"])

        self.assertEqual(report.results[0].status, CoverageStatus.FAILED_TECHNICAL)
        self.assertIn("HTTP 404", report.results[0].error)
        self.assertEqual(report.results[0].error_type, "stale_board_identifier")
        self.assertIn("current official career board", report.results[0].remediation)

    def test_scanner_distinguishes_migration_unresolved_and_protocol_failures(self):
        cases = (
            (
                "Cornerstone CSOD official careers page did not link the configured tenant and site",
                "probable_ats_migration",
            ),
            (
                "Workday HTTP 422 (candidate_sites=[none]; discovery_status=no_valid_candidate)",
                "source_unresolved",
            ),
            ("Example adapter HTTP 400", "adapter_protocol_failure"),
            ("Official careers page blocked response HTTP 403", "official_page_blocked"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                actual, remediation = CompanyCareersScanner._adapter_failure_details(error)
                self.assertEqual(actual, expected)
                self.assertTrue(remediation)

    def test_scanner_does_not_infer_completeness_from_a_short_unproven_result(self):
        def unproven_adapter(session, target, timeout):
            return [self._student_job("Software Engineering Intern")]

        old_adapter = scanner_module.ADAPTERS.get("unproven")
        scanner_module.ADAPTERS["unproven"] = unproven_adapter
        try:
            target = CompanyTarget(
                name="Unproven Co", platform="unproven", max_jobs=20
            )
            report = CompanyCareersScanner(
                targets=[target], session=FakeSession()
            ).scan()
        finally:
            if old_adapter is None:
                scanner_module.ADAPTERS.pop("unproven", None)
            else:
                scanner_module.ADAPTERS["unproven"] = old_adapter

        result = report.results[0]
        self.assertFalse(result.scan_complete)
        self.assertEqual(
            result.scan_completeness_reason, "adapter_completeness_not_proven"
        )
        self.assertEqual(result.scan_health, "incomplete")

    def test_adapter_natural_terminal_evidence_marks_scan_complete(self):
        def terminal_adapter(session, target, timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=True,
                    scan_completeness_reason="natural_terminal_page",
                    total_available=None,
                    pages_requested=2,
                    pages_completed=2,
                    stop_reason="natural_terminal_page",
                    safety_limit_reached=False,
                    terminal_evidence_type="terminal_page",
                    terminal_evidence_value="short_page",
                    has_more=False,
                    terminal_page_observed=True,
                ),
            )
            return [self._student_job("Software Engineering Intern")]

        old_adapter = scanner_module.ADAPTERS.get("terminal")
        scanner_module.ADAPTERS["terminal"] = terminal_adapter
        try:
            report = CompanyCareersScanner(
                targets=[CompanyTarget(name="Terminal Co", platform="terminal")],
                session=FakeSession(),
            ).scan()
        finally:
            if old_adapter is None:
                scanner_module.ADAPTERS.pop("terminal", None)
            else:
                scanner_module.ADAPTERS["terminal"] = old_adapter

        result = report.results[0]
        self.assertTrue(result.scan_complete)
        self.assertEqual(result.scan_health, "complete")
        self.assertEqual(result.scan_completeness_reason, "natural_terminal_page")
        self.assertEqual(result.pages_requested, 2)
        self.assertEqual(result.pages_completed, 2)
        self.assertEqual(result.terminal_evidence_type, "terminal_page")
        self.assertFalse(result.has_more)
        self.assertTrue(result.terminal_page_observed)
        self.assertEqual(
            report.coverage()[0]["terminal_evidence_value"], "short_page"
        )

    def test_scanner_authoritative_total_wins_at_exact_safety_cap(self):
        def capped_adapter(session, target, timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=False,
                    scan_completeness_reason="safety_limit_reached",
                    total_available=1,
                    authoritative_total_present=True,
                    pages_requested=1,
                    pages_completed=1,
                    stop_reason="maximum_results_reached",
                    safety_limit_reached=True,
                ),
            )
            return [self._student_job("Software Engineering Intern")]

        old_adapter = scanner_module.ADAPTERS.get("exact_cap")
        scanner_module.ADAPTERS["exact_cap"] = capped_adapter
        try:
            report = CompanyCareersScanner(
                targets=[CompanyTarget(name="Exact Cap", platform="exact_cap")],
                session=FakeSession(),
            ).scan()
        finally:
            if old_adapter is None:
                scanner_module.ADAPTERS.pop("exact_cap", None)
            else:
                scanner_module.ADAPTERS["exact_cap"] = old_adapter

        result = report.results[0]
        self.assertTrue(result.scan_complete)
        self.assertFalse(result.safety_limit_reached)
        self.assertEqual(result.stop_reason, "reported_total_reached")
        self.assertTrue(result.authoritative_total_present)

    def test_post_filter_limit_overrides_adapter_completion(self):
        def complete_adapter(session, target, timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=True,
                    scan_completeness_reason="complete_collection_response",
                    total_available=2,
                    pages_requested=1,
                    pages_completed=1,
                    stop_reason="complete_collection_response",
                ),
            )
            return [
                self._student_job("Software Engineering Intern"),
                self._student_job(
                    "Hardware Engineering Intern",
                    url="https://example.com/job/2",
                ),
            ]

        old_adapter = scanner_module.ADAPTERS.get("complete_for_limit")
        scanner_module.ADAPTERS["complete_for_limit"] = complete_adapter
        try:
            report = CompanyCareersScanner(
                targets=[
                    CompanyTarget(name="Limited Co", platform="complete_for_limit")
                ],
                session=FakeSession(),
            ).scan(max_jobs_per_company=1)
        finally:
            if old_adapter is None:
                scanner_module.ADAPTERS.pop("complete_for_limit", None)
            else:
                scanner_module.ADAPTERS["complete_for_limit"] = old_adapter

        result = report.results[0]
        self.assertEqual(len(result.jobs), 1)
        self.assertFalse(result.scan_complete)
        self.assertTrue(result.safety_limit_reached)
        self.assertEqual(
            result.scan_completeness_reason,
            "post_filter_safety_limit_reached",
        )

    def test_all_eligible_jobs_reach_ranking_when_best_job_is_after_twenty(self):
        ordinary = [
            JobRecord(
                job_id=f"ordinary-{index}",
                title="Engineering Intern",
                company="Example",
                location="Calgary, Canada",
                url=f"https://example.com/jobs/{index}",
                updated_at="2026-09-01",
                source="test:example",
                description="Support engineering projects.",
            )
            for index in range(20)
        ]
        strongest = JobRecord(
            job_id="strongest-after-twenty",
            title="Machine Learning Software Engineering Intern",
            company="Example",
            location="Calgary, Canada",
            url="https://example.com/jobs/strongest",
            updated_at="2026-09-01",
            source="test:example",
            description="Build Python machine learning infrastructure and distributed systems.",
        )

        ranked = rank_student_jobs(ordinary + [strongest])

        self.assertEqual(len(ranked), 21)
        self.assertEqual(ranked[0]["job_id"], "strongest-after-twenty")

    def test_scheduled_dashboard_path_has_no_per_company_twenty_job_limit(self):
        workflow = Path(".github/workflows/hourly-job-radar.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("python scripts/update_student_dashboard.py", workflow)
        self.assertNotIn("update_student_dashboard.py --max-jobs 20", workflow)
        self.assertIn("timeout-minutes: 45", workflow)

    def test_scanner_reports_not_implemented_with_remediation(self):
        target = CompanyTarget(name="UnknownCo", platform="unknown_platform")
        scanner = CompanyCareersScanner(targets=[target], session=FakeSession())

        report = scanner.scan(keywords="Engineer", locations=["Remote"])

        self.assertEqual(report.results[0].status, CoverageStatus.NOT_IMPLEMENTED)
        self.assertEqual(report.results[0].error_type, "not_implemented")
        self.assertIn("Register an adapter", report.results[0].remediation)

    def test_scanner_contains_unexpected_adapter_exceptions(self):
        def boom_adapter(session, target, timeout):
            raise ValueError("bad parser state")

        old_adapter = scanner_module.ADAPTERS.get("boom")
        scanner_module.ADAPTERS["boom"] = boom_adapter
        try:
            target = CompanyTarget(name="BoomCo", platform="boom")
            scanner = CompanyCareersScanner(targets=[target], session=FakeSession())

            report = scanner.scan(keywords="Engineer", locations=["Remote"])
        finally:
            if old_adapter is None:
                scanner_module.ADAPTERS.pop("boom", None)
            else:
                scanner_module.ADAPTERS["boom"] = old_adapter

        self.assertEqual(report.results[0].status, CoverageStatus.FAILED_TECHNICAL)
        self.assertEqual(report.results[0].error_type, "unexpected_error")
        self.assertIn("ValueError: bad parser state", report.results[0].error)

    def test_bounded_concurrency_isolates_failures_sessions_and_output_order(self):
        lock = threading.Lock()
        two_workers_started = threading.Event()
        release_workers = threading.Event()
        active = 0
        maximum_active = 0
        sessions = []

        class IsolatedSession:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        def session_factory():
            session = IsolatedSession()
            sessions.append(session)
            return session

        def bounded_adapter(session, target, timeout):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    two_workers_started.set()
                    release_workers.set()
            self.assertTrue(release_workers.wait(timeout=1))
            try:
                if target.name == "Second":
                    raise ValueError("malformed adapter response")
                publish_source_scan_metadata(
                    session,
                    SourceScanMetadata(
                        scan_complete=True,
                        scan_completeness_reason="natural_terminal_page",
                        stop_reason="natural_terminal_page",
                    ),
                )
                return [
                    JobRecord(
                        job_id=target.name,
                        title=f"{target.name} Engineer",
                        company=target.name,
                        location="Remote",
                        url=f"https://example.com/{target.name}",
                        updated_at="2026-09-01",
                        source="bounded:test",
                    )
                ]
            finally:
                with lock:
                    active -= 1

        previous = scanner_module.ADAPTERS.get("bounded_test")
        scanner_module.ADAPTERS["bounded_test"] = bounded_adapter
        try:
            report = CompanyCareersScanner(
                targets=[
                    CompanyTarget(name="First", platform="bounded_test"),
                    CompanyTarget(name="Second", platform="bounded_test"),
                    CompanyTarget(name="Third", platform="bounded_test"),
                ],
                session_factory=session_factory,
                max_workers=2,
            ).scan()
        finally:
            if previous is None:
                scanner_module.ADAPTERS.pop("bounded_test", None)
            else:
                scanner_module.ADAPTERS["bounded_test"] = previous

        self.assertTrue(two_workers_started.is_set())
        self.assertEqual(maximum_active, 2)
        self.assertEqual([item.target.name for item in report.results], ["First", "Second", "Third"])
        self.assertEqual(report.results[1].status, CoverageStatus.FAILED_TECHNICAL)
        self.assertEqual(report.results[1].error_type, "unexpected_error")
        self.assertEqual(report.results[0].scan_health, "complete")
        self.assertEqual(report.results[2].scan_health, "complete")
        self.assertEqual(len({id(session) for session in sessions}), 3)
        self.assertTrue(all(session.closed for session in sessions))

    def test_browser_startup_failure_does_not_abort_http_sources(self):
        def healthy_adapter(session, target, timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=True,
                    scan_completeness_reason="natural_terminal_page",
                    stop_reason="natural_terminal_page",
                ),
            )
            return [
                JobRecord(
                    job_id="healthy",
                    title="Healthy Engineer",
                    company=target.name,
                    location="Remote",
                    url="https://example.com/healthy",
                    updated_at="2026-09-01",
                    source="healthy:test",
                )
            ]

        previous = scanner_module.ADAPTERS.get("healthy_test")
        scanner_module.ADAPTERS["healthy_test"] = healthy_adapter
        try:
            with patch.object(
                scanner_module.browser_rendered_html.BrowserBatch,
                "__enter__",
                side_effect=AdapterError("Browser collector could not start Chromium"),
            ):
                report = CompanyCareersScanner(
                    targets=[
                        CompanyTarget(name="HTTP", platform="healthy_test"),
                        CompanyTarget(name="Browser", platform="browser_rendered_html"),
                    ],
                    max_workers=2,
                ).scan()
        finally:
            if previous is None:
                scanner_module.ADAPTERS.pop("healthy_test", None)
            else:
                scanner_module.ADAPTERS["healthy_test"] = previous

        self.assertEqual(report.results[0].scan_health, "complete")
        self.assertEqual(report.results[1].status, CoverageStatus.FAILED_TECHNICAL)
        self.assertEqual(
            report.results[1].scan_completeness_reason,
            "browser_batch_startup_failure",
        )

    def test_browser_terminal_metadata_is_propagated_without_unbounded_work(self):
        class FakeBrowserBatch:
            def __init__(self, timeout=30):
                self.timeout = timeout
                self.last_run = {}

            def __enter__(self):
                return self

            def close(self):
                return None

            def fetch_jobs(self, target, timeout):
                stop = (
                    "no_next_page"
                    if target.name == "Terminal"
                    else "no_pagination_configured"
                )
                self.last_run = {
                    "static_parser": SourceScanMetadata(
                        scan_complete=True,
                        scan_completeness_reason="validated_html_terminal",
                        total_available=25,
                        pages_requested=1,
                        pages_completed=1,
                        stop_reason=stop,
                    )
                }
                return [
                    JobRecord(
                        job_id=f"browser:{target.name}",
                        title="Engineer",
                        company=target.name,
                        location="Remote",
                        url="https://example.test/job/1",
                        updated_at="",
                        source="browser:test",
                    )
                ]

        with patch.object(
            scanner_module.browser_rendered_html,
            "BrowserBatch",
            FakeBrowserBatch,
        ):
            report = CompanyCareersScanner(
                targets=[
                    CompanyTarget(name="Terminal", platform="browser_rendered_html"),
                    CompanyTarget(name="Needs Scroll", platform="browser_rendered_html"),
                ],
                session=FakeSession(),
            ).scan()

        by_name = {row.target.name: row for row in report.results}
        self.assertTrue(by_name["Terminal"].scan_complete)
        self.assertFalse(by_name["Needs Scroll"].scan_complete)
        self.assertEqual(
            by_name["Needs Scroll"].scan_completeness_reason,
            "browser_exhaustion_not_proven",
        )
        self.assertEqual(by_name["Needs Scroll"].fetched_count, 1)

    def test_scanner_reports_fetched_no_matching_roles(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Product Manager",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/123",
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")
        scanner = CompanyCareersScanner(targets=[target], session=session)

        report = scanner.scan(keywords="Principal Engineer", locations=["Remote"])

        self.assertEqual(report.results[0].status, CoverageStatus.FETCHED_NO_MATCHING_ROLES)
        self.assertEqual(report.results[0].fetched_count, 1)
        self.assertEqual(report.results[0].matched_count, 0)

    def test_scanner_reports_manual_review_required(self):
        target = CompanyTarget(name="ManualCo", platform="manual_review")
        scanner = CompanyCareersScanner(targets=[target], session=FakeSession())

        report = scanner.scan(keywords="Engineer", locations=["Remote"])

        self.assertEqual(report.results[0].status, CoverageStatus.MANUAL_REVIEW_REQUIRED)
        self.assertEqual(report.results[0].fetched_count, 0)
        self.assertIn("Manual review required", report.results[0].error)
        self.assertEqual(report.results[0].error_type, "manual_review_required")

    def test_keyword_filter_uses_title_not_description(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Associate Principal, SOX Analytics",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/123",
                                "content": "Partner with engineering teams.",
                            },
                            {
                                "id": 456,
                                "title": "Principal Software Engineer, Performance",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/456",
                            },
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")
        scanner = CompanyCareersScanner(targets=[target], session=session)

        report = scanner.scan(keywords="Principal Engineer", locations=["Remote"])

        self.assertEqual(report.results[0].status, CoverageStatus.FETCHED)
        self.assertEqual(report.results[0].matched_count, 1)
        self.assertEqual(report.results[0].jobs[0].title, "Principal Software Engineer, Performance")

    def test_relevance_scoring_includes_ic_engineering_and_excludes_solutions(self):
        strong_job = greenhouse.JobRecord(
            job_id="1",
            title="Software Engineer, ML (Technical Leadership)",
            company="Example",
            location="Remote",
            url="https://example.com/job/1",
            updated_at="2026-04-01",
            source="greenhouse:example",
            departments=["Engineering"],
            description="Lead machine learning platform design.",
        )
        weak_job = greenhouse.JobRecord(
            job_id="2",
            title="Principal Solutions Engineer",
            company="Example",
            location="Remote",
            url="https://example.com/job/2",
            updated_at="2026-04-01",
            source="greenhouse:example",
            departments=["Sales"],
        )

        strong_score = score_job(strong_job, keywords="Principal Engineer, Staff Software Engineer")
        weak_score = score_job(weak_job, keywords="Principal Engineer, Staff Software Engineer")

        self.assertTrue(strong_score["is_relevant"])
        self.assertEqual(weak_score["band"], "low")
        self.assertFalse(weak_score["is_relevant"])
        self.assertIn("solutions engineering", weak_score["penalties"])

    def test_scanner_uses_relevance_for_candidate_matches(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Software Engineer, ML (Technical Leadership)",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/123",
                                "content": "Lead ML platform systems.",
                            },
                            {
                                "id": 456,
                                "title": "Principal Solutions Engineer",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/456",
                            },
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")
        scanner = CompanyCareersScanner(targets=[target], session=session)

        report = scanner.scan(
            keywords="Principal Engineer, Staff Software Engineer",
            locations=["Remote"],
        )

        self.assertEqual(report.results[0].status, CoverageStatus.FETCHED)
        self.assertEqual(report.results[0].fetched_count, 2)
        self.assertEqual(report.results[0].matched_count, 1)
        self.assertEqual(
            report.results[0].jobs[0].title,
            "Software Engineer, ML (Technical Leadership)",
        )

    def test_low_relevance_jobs_are_not_reported_as_failures(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Principal Solutions Engineer",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/123",
                            }
                        ]
                    }
                )
            ]
        )
        target = CompanyTarget(name="Example", platform="greenhouse", slug="example")
        scanner = CompanyCareersScanner(targets=[target], session=session)

        report = scanner.scan(keywords="Principal Engineer", locations=["Remote"])

        self.assertEqual(report.results[0].status, CoverageStatus.FETCHED_NO_MATCHING_ROLES)
        self.assertEqual(report.results[0].fetched_count, 1)
        self.assertEqual(report.results[0].matched_count, 0)
        self.assertEqual(failure_items(report), [])

    def test_pipeline_database_mapping_compatibility(self):
        pipeline = JobSearchPipeline(
            keywords="Engineer",
            locations=["Remote"],
            scrapers=["company_careers"],
            use_database=False,
            async_mode=True,
        )

        mapped = pipeline._company_careers_job_to_database_record(
            {
                "job_id": "1",
                "title": "Staff Engineer",
                "company": "Example",
                "location": "Remote",
                "url": "https://example.com/job/1",
                "updated_at": "2026-04-04",
                "source": "greenhouse:example",
                "departments": ["Engineering"],
                "offices": ["Remote"],
                "description": "Build systems.",
            }
        )

        self.assertEqual(mapped["job_title"], "Staff Engineer")
        self.assertEqual(mapped["company_name"], "Example")
        self.assertEqual(mapped["source_url"], "https://example.com/job/1")
        self.assertEqual(mapped["job_location"], "Remote")

    def test_coverage_markdown_summarizes_statuses(self):
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "jobs": [
                            {
                                "id": 123,
                                "title": "Principal Engineer",
                                "location": {"name": "Remote"},
                                "absolute_url": "https://example.com/job/123",
                            }
                        ]
                    }
                )
            ]
        )
        targets = [
            CompanyTarget(name="FetchedCo", platform="greenhouse", slug="fetched"),
            CompanyTarget(name="ManualCo", platform="manual_review"),
        ]
        report = CompanyCareersScanner(targets=targets, session=session).scan(
            keywords="Principal Engineer",
            locations=["Remote"],
        )

        markdown = coverage_markdown(
            report,
            jobs_path="jobs.json",
            coverage_json_path="coverage.json",
            failures_json_path="failures.json",
        )

        self.assertIn("Matched jobs: 1", markdown)
        self.assertIn("fetched: 1", markdown)
        self.assertIn("manual_review_required: 1", markdown)
        self.assertIn("## Failures And Manual Follow-Up", markdown)
        self.assertIn("manual_review_required", markdown)
        self.assertIn("| FetchedCo | fetched | 1 | 1 | greenhouse | fetched |", markdown)
        self.assertIn("jobs.json", markdown)
        self.assertIn("failures.json", markdown)
        failures = failure_items(report)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["company"], "ManualCo")

    def test_pipeline_writes_company_careers_coverage_artifacts(self):
        with TemporaryDirectory() as tmpdir:
            pipeline = JobSearchPipeline(
                keywords="Engineer",
                locations=["Remote"],
                scrapers=["company_careers"],
                output_dir=tmpdir,
                use_database=False,
                async_mode=True,
            )
            session = FakeSession(
                get_responses=[
                    FakeResponse(
                        {
                            "jobs": [
                                {
                                    "id": 123,
                                    "title": "Principal Engineer",
                                    "location": {"name": "Remote"},
                                    "absolute_url": "https://example.com/job/123",
                                }
                            ]
                        }
                    )
                ]
            )
            report = CompanyCareersScanner(
                targets=[CompanyTarget(name="Example", platform="greenhouse", slug="example")],
                session=session,
            ).scan(keywords="Principal Engineer", locations=["Remote"])
            paths = pipeline._company_careers_output_paths()

            pipeline._write_company_careers_report(report, paths)

            for key in (
                "coverage_json",
                "coverage_md",
                "coverage_csv",
                "failures_json",
                "ranked_json",
                "ranked_csv",
            ):
                with self.subTest(key=key):
                    self.assertTrue(paths[key].startswith(tmpdir))
                    with open(paths[key], encoding="utf-8") as file:
                        content = file.read()
                    if key == "failures_json":
                        self.assertIn("failure_count", content)
                    elif key.startswith("ranked"):
                        self.assertIn("relevance_score", content)
                    else:
                        self.assertIn("Example", content)
            with open(paths["ranked_json"], encoding="utf-8") as file:
                ranked = json.load(file)
            self.assertEqual(ranked[0]["company"], "Example")
            self.assertIn("relevance_score", ranked[0])
            self.assertTrue(paths["ranked_csv"].startswith(tmpdir))

    def test_pipeline_can_write_student_ranked_artifacts(self):
        with TemporaryDirectory() as tmpdir:
            pipeline = JobSearchPipeline(
                keywords="Intern OR Co-op OR Student",
                locations=["Canada", "Remote"],
                scrapers=["company_careers"],
                output_dir=tmpdir,
                use_database=False,
                async_mode=True,
                company_careers_ranked_writer=write_student_ranked_jobs_artifacts,
            )
            session = FakeSession(
                get_responses=[
                    FakeResponse(
                        {
                            "jobs": [
                                {
                                    "id": 123,
                                    "title": "Embedded Systems Co-op",
                                    "location": {"name": "Calgary, Alberta"},
                                    "absolute_url": "https://example.com/job/123",
                                }
                            ]
                        }
                    )
                ]
            )
            report = CompanyCareersScanner(
                targets=[
                    CompanyTarget(
                        name="Example",
                        platform="greenhouse",
                        slug="example",
                    )
                ],
                session=session,
            ).scan(
                locations=["Canada", "Remote"],
                job_filter=build_student_job_filter(),
                location_filter=build_student_location_filter(
                    locations=["Canada", "Remote"]
                ),
            )
            paths = pipeline._company_careers_output_paths()

            pipeline._write_company_careers_report(report, paths)

            with open(paths["ranked_json"], encoding="utf-8") as file:
                ranked = json.load(file)
            self.assertEqual(ranked[0]["location_status"], "eligible_canada")
            self.assertIn("embedded_firmware", ranked[0]["matched_categories"])
            self.assertIn("positive_signals", ranked[0])
            self.assertIn("negative_signals", ranked[0])

    def test_rank_jobs_orders_candidate_review_by_relevance(self):
        jobs = [
            greenhouse.JobRecord(
                job_id="1",
                title="Senior Engineer",
                company="Example",
                location="Remote",
                url="https://example.com/job/1",
                updated_at="2026-04-01",
                source="greenhouse:example",
            ),
            greenhouse.JobRecord(
                job_id="2",
                title="Principal Software Engineer, Infrastructure",
                company="Example",
                location="Remote",
                url="https://example.com/job/2",
                updated_at="2026-04-01",
                source="greenhouse:example",
            ),
        ]

        ranked = rank_jobs(jobs, keywords="Principal Engineer", locations=["Remote"])

        self.assertEqual(ranked[0]["job_id"], "2")
        self.assertGreater(ranked[0]["relevance_score"], ranked[1]["relevance_score"])


if __name__ == "__main__":
    unittest.main()
