from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import dayforce, paycor, peoplesoft, ukg_pro, workable
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.source_health import finalize_source_scan


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "config" / "company_careers_adapter_expansion_v1_batch_a_validation.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
HISTORICAL_PRODUCTION = ROOT / "config" / "target_companies.canadian_expansion_v3_batch_c_validated.yaml"

UKG_BOARD = (
    "https://recruiting.ultipro.ca/COO5001COLT/JobBoard/"
    "05857940-9028-4d4a-8544-26bd990d20d6/"
)
PAYCOR_CLIENT = "8a7883d06937f5f7016978fec55861f7"
PAYCOR_JOB = "8a7887a89efb878a019f23fa4cac326f"
PEOPLESOFT_JOB = "315022"
PEOPLESOFT_URL = (
    "https://recruiting.calgary.ca/psc/hcm/EMPLOYEE/HRMS/c/"
    "HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL?FOCUS=Applicant&Page=HRS_APP_JBPST_FL"
    f"&JobOpeningId={PEOPLESOFT_JOB}&PostingSeq=1&SiteId=1"
)


class Response:
    def __init__(self, value: object = "", *, status: int = 200, url: str = "https://jobs.test"):
        self.status_code = status
        self.url = url
        self._value = value
        if isinstance(value, (dict, list)):
            self.text = json.dumps(value)
        else:
            self.text = str(value)
        self.content = self.text.encode("utf-8")
        self.headers = {
            "content-type": "application/json" if isinstance(value, (dict, list)) else "text/html"
        }

    def json(self):
        if isinstance(self._value, Exception):
            raise self._value
        if isinstance(self._value, (dict, list)):
            return self._value
        return json.loads(self.text)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request")
        return self.responses.pop(0)


def ukg_target(**overrides):
    values = {
        "name": "CoolIT Systems",
        "platform": "ukg_pro",
        "careers_url": UKG_BOARD,
        "identity_regex": r"^Coolit Systems Inc\.?$",
        "max_pages": 1,
        "max_jobs": 50,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def ukg_board(*, third_party=False, name="Coolit Systems Inc"):
    config = {
        "Id": "05857940-9028-4d4a-8544-26bd990d20d6",
        "Name": name,
        "HasThirdPartyPostings": third_party,
        "OpportunityDetailUrl": "",
    }
    return Response(f"<html><script>jobBoard: {json.dumps(config)}, isAuthenticated: false</script></html>")


def ukg_payload(job_id="11eea7bb-83d6-4013-94e0-9bb0448173fd", *, total=1):
    return {
        "opportunities": [
            {
                "Id": job_id,
                "Title": "Manager, CDU Sustaining Engineering",
                "RequisitionNumber": "MANAG001709",
                "FullTime": True,
                "JobCategoryName": "Engineering",
                "Locations": [
                    {
                        "LocalizedName": "Quarry Park HQ",
                        "LocalizedLocationId": "Calgary, Alberta",
                        "DisplayName": False,
                        "DisplayLocationId": True,
                        "DisplayAddress": False,
                        "DisplayDescription": False,
                        "Address": {
                            "City": "Calgary",
                            "State": {"Code": "AB", "Name": "Alberta"},
                            "Country": {"Code": "CAN", "Name": "Canada"},
                        },
                    }
                ],
                "PostedDate": "2026-07-27T17:14:14.606Z",
                "BriefDescription": "Lead the sustaining engineering team.",
                "JobLocationType": 1,
            }
        ],
        "totalCount": total,
        "locations": [],
    }


def paycor_target(**overrides):
    values = {
        "name": "Ballard Power Systems",
        "platform": "paycor",
        "careers_url": "https://www.ballard.com/careers/",
        "endpoint_url": (
            "https://recruitingbypaycor.com/career/CareerHome.action?clientId="
            + PAYCOR_CLIENT
        ),
        "identity_regex": r"\bBallard\b",
        "max_pages": 1,
        "max_jobs": 50,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def paycor_page(*, identity="Ballard", job_id=PAYCOR_JOB):
    return Response(
        f"""
        <html><body><div>{identity} careers. Click a position title below to apply online.</div>
        <div class="gnewtonCareerGroupRowClass">
          <div class="gnewtonCareerGroupJobTitleClass">
            <a href="https://recruitingbypaycor.com/career/JobIntroduction.action?clientId={PAYCOR_CLIENT}&amp;id={job_id}&amp;source=&amp;lang=en">
              Manager, Enterprise Solutions
            </a>
          </div>
          <div class="gnewtonCareerGroupJobDescriptionClass">Burnaby, British Columbia</div>
        </div></body></html>
        """,
        url=f"https://recruitingbypaycor.com/career/CareerHome.action?clientId={PAYCOR_CLIENT}",
    )


def paycor_detail(*, identity="Ballard Power Systems", job_id=PAYCOR_JOB):
    return Response(
        f"""
        <html><body><div>{identity}</div>
          <td id="gnewtonJobPosition"><b>Position:</b> Manager, Enterprise Solutions</td>
          <td id="gnewtonJobLocationInfo">Burnaby, British Columbia</td>
          <td id="gnewtonJobDescriptionText">Lead enterprise technology modernization.</td>
          <form id="gravityApplyJob" method="post"
            action="https://recruitingbypaycor.com/career/SubmitResume.action?parentUrl=&amp;clientId={PAYCOR_CLIENT}&amp;id={job_id}"></form>
        </body></html>
        """,
        url=(
            "https://recruitingbypaycor.com/career/JobIntroduction.action?clientId="
            f"{PAYCOR_CLIENT}&id={job_id}"
        ),
    )


def peoplesoft_target(**overrides):
    values = {
        "name": "City of Calgary",
        "platform": "peoplesoft",
        "careers_url": "https://www.calgary.ca/careers.html",
        "listing_url": "https://www.calgary.ca/careers.html",
        "identity_regex": r"\bCity of Calgary\b",
        "allowed_apply_hosts": ["recruiting.calgary.ca"],
        "site_id": "1",
        "max_pages": 1,
        "max_jobs": 20,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def peoplesoft_listing(*, url=PEOPLESOFT_URL):
    return Response(
        f'<html><body><h1>City of Calgary Careers</h1><a class="job-title" href="{url.replace("&", "&amp;")}">Team Lead, Credit and Collection</a></body></html>',
        url="https://www.calgary.ca/careers.html",
    )


def peoplesoft_detail(
    *,
    job_id=PEOPLESOFT_JOB,
    title="Team Lead, Credit and Collection",
    include_apply=True,
    url=PEOPLESOFT_URL,
):
    apply = '<a id="APPLYJOB" href="javascript:void(0)">Apply for Job</a>' if include_apply else ""
    return Response(
        f"""
        <html><body><div id="PT_BRAND_COMPINFO">City of Calgary</div>{apply}
          <span id="HRS_SCH_WRK2_HRS_JOB_OPENING_ID">{job_id}</span>
          <span id="HRS_SCH_WRK2_POSTING_TITLE">{title}</span>
          <span id="HRS_SCH_WRK_HRS_DESCRLONG">Calgary, Alberta, Canada</span>
          <span id="HRS_SCH_PSTDSC_DESCRLONG$1">Lead the credit and collections team.</span>
        </body></html>
        """,
        url=url,
    )


def peoplesoft_stale_detail():
    return Response(
        "<html><body><h1>City of Calgary Careers</h1><p>Search Jobs</p></body></html>",
        url=(
            "https://recruiting.calgary.ca/psc/hcm/EMPLOYEE/HRMS/c/"
            "HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL?Page=HRS_APP_SCHJOB_FL&Action=U"
        ),
    )


class UKGProAdapterTests(unittest.TestCase):
    def test_stable_id_identity_location_detail_url_and_metadata(self):
        session = Session([ukg_board(), Response(ukg_payload())])
        jobs = ukg_pro.fetch_jobs(session, ukg_target())

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "11eea7bb-83d6-4013-94e0-9bb0448173fd")
        self.assertEqual(job.company, "CoolIT Systems")
        self.assertEqual(job.location, "Calgary, Alberta")
        self.assertEqual(job.updated_at, "2026-07-27")
        self.assertEqual(job.departments, ["Engineering"])
        self.assertEqual(job.raw_metadata["board_name"], "Coolit Systems Inc")
        self.assertEqual(
            job.url,
            UKG_BOARD.rstrip("/")
            + "/OpportunityDetail?opportunityId=11eea7bb-83d6-4013-94e0-9bb0448173fd",
        )
        self.assertEqual(session.calls[1][0], "post")
        self.assertEqual(session.calls[1][2]["json"]["opportunitySearch"]["Top"], 50)
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["authoritative_total_present"])
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")
        self.assertEqual(metadata["terminal_evidence_value"], 1)

    def test_mixed_or_third_party_board_fails_before_search(self):
        for response in (ukg_board(third_party=True), ukg_board(name="Pason & IWS Career Opportunities")):
            with self.subTest(response=response.text):
                session = Session([response])
                with self.assertRaisesRegex(AdapterError, "mixed|third-party"):
                    ukg_pro.fetch_jobs(session, ukg_target())
                self.assertEqual(len(session.calls), 1)

    @patch.object(ukg_pro.time, "sleep", return_value=None)
    def test_pagination_is_bounded_and_total_must_be_stable(self, _sleep):
        first = ukg_payload(total=51)
        first["opportunities"] = [
            dict(first["opportunities"][0], Id=f"00000000-0000-4000-8000-{index:012d}")
            for index in range(50)
        ]
        second = ukg_payload(job_id="00000000-0000-4000-8000-000000000051", total=52)
        with self.assertRaisesRegex(AdapterError, "changed during pagination"):
            ukg_pro.fetch_jobs(Session([ukg_board(), Response(first), Response(second)]), ukg_target(max_pages=2, max_jobs=100))

    @patch.object(ukg_pro.time, "sleep", return_value=None)
    def test_pason_shape_adapts_both_caps_without_detail_enrichment(self, _sleep):
        template = ukg_payload()["opportunities"][0]
        first_items = [
            dict(
                template,
                Id=f"00000000-0000-4000-8000-{index:012d}",
                BriefDescription=(
                    "Pason Systems role" if index <= 14 else "IWS role"
                ),
            )
            for index in range(1, 21)
        ]
        second_items = [
            dict(
                template,
                Id=f"00000000-0000-4000-8000-{index:012d}",
                BriefDescription="Pason Systems role",
            )
            for index in range(21, 27)
        ]
        target = ukg_target(
            name="Pason Systems",
            identity_regex=r"Pason",
            job_attribution_regex=r"\bPason(?: Systems)?\b",
            job_attribution_exclude_regex=r"\b(?:IWS)\b",
            max_pages=1,
            max_jobs=20,
        )
        session = Session(
            [
                ukg_board(name="Pason Systems"),
                Response({"opportunities": first_items, "totalCount": 26}),
                Response({"opportunities": second_items, "totalCount": 26}),
            ]
        )

        jobs = ukg_pro.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 20)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "reported_total_reached")
        self.assertEqual(metadata["total_available"], 26)
        self.assertEqual(metadata["pages_requested"], 2)
        self.assertEqual(metadata["effective_listing_request_ceiling"], 2)
        self.assertEqual(metadata["effective_listing_job_ceiling"], 26)
        self.assertEqual(metadata["listing_results_observed_at_decision"], 26)
        self.assertFalse(metadata["authoritative_total_present"])
        self.assertEqual(metadata["terminal_evidence_type"], "api_pagination_contract")
        self.assertEqual(
            metadata["terminal_evidence_value"],
            {"reported_total": 26, "raw_results_observed": 26},
        )
        finalized = finalize_source_scan(metadata, jobs_collected=len(jobs))
        self.assertEqual(finalized["scan_state"], "complete")
        self.assertEqual([call[0] for call in session.calls], ["get", "post", "post"])
        self.assertEqual(
            session.calls[1][2]["json"]["opportunitySearch"]["Top"], 20
        )
        self.assertEqual(
            session.calls[2][2]["json"]["opportunitySearch"]["Top"], 12
        )

    def test_invalid_board_host_and_excessive_limits_fail_without_network(self):
        with self.assertRaisesRegex(AdapterError, "supported public JobBoard"):
            ukg_pro.fetch_jobs(Session([]), ukg_target(careers_url="https://evil.test/board"))
        with self.assertRaisesRegex(AdapterError, "max_pages cannot exceed"):
            ukg_pro.fetch_jobs(Session([]), ukg_target(max_pages=3))


class PaycorAdapterTests(unittest.TestCase):
    def test_stable_id_location_identity_and_official_apply_url(self):
        jobs = paycor.fetch_jobs(Session([paycor_page(), paycor_detail()]), paycor_target())

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, PAYCOR_JOB)
        self.assertEqual(job.company, "Ballard Power Systems")
        self.assertEqual(job.location, "Burnaby, British Columbia")
        self.assertEqual(
            job.url,
            "https://recruitingbypaycor.com/career/SubmitResume.action?"
            f"clientId={PAYCOR_CLIENT}&id={PAYCOR_JOB}",
        )
        self.assertEqual(job.raw_metadata["client_id"], PAYCOR_CLIENT)
        self.assertEqual(job.description, "Lead enterprise technology modernization.")

    def test_result_cap_remains_intentionally_incomplete(self):
        second_id = "8a7887a89efb878a019f23fa4cac3270"
        first_page = paycor_page()
        second_card = f"""
        <div class="gnewtonCareerGroupRowClass">
          <div class="gnewtonCareerGroupJobTitleClass">
            <a href="https://recruitingbypaycor.com/career/JobIntroduction.action?clientId={PAYCOR_CLIENT}&amp;id={second_id}">
              Senior Systems Engineer
            </a>
          </div>
          <div class="gnewtonCareerGroupJobDescriptionClass">Calgary, Alberta</div>
        </div>
        """
        listing = Response(
            first_page.text.replace("</body>", second_card + "</body>"),
            url=first_page.url,
        )
        session = Session([listing, paycor_detail()])

        jobs = paycor.fetch_jobs(session, paycor_target(max_jobs=1))

        self.assertEqual(len(jobs), 1)
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["scan_completeness_reason"], "safety_limit_reached")
        self.assertEqual(metadata["stop_reason"], "maximum_results_reached")
        self.assertEqual(metadata["total_available"], 2)

    def test_cross_client_unstable_identity_and_captcha_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "cross-client"):
            paycor.fetch_jobs(Session([paycor_page(job_id="not-stable")]), paycor_target())
        with self.assertRaisesRegex(AdapterError, "mixed or ambiguous"):
            paycor.fetch_jobs(Session([paycor_page(identity="Another Company")]), paycor_target())
        with self.assertRaisesRegex(AdapterError, "CAPTCHA"):
            paycor.fetch_jobs(Session([Response("Ballard turnstile captcha")]), paycor_target())
        with self.assertRaisesRegex(AdapterError, "description contract"):
            paycor.fetch_jobs(
                Session([paycor_page(), paycor_detail().__class__("Ballard Power Systems")]),
                paycor_target(),
            )

    def test_only_one_bounded_page_is_supported(self):
        with self.assertRaisesRegex(AdapterError, "exactly one"):
            paycor.fetch_jobs(Session([]), paycor_target(max_pages=2))


class PeopleSoftAdapterTests(unittest.TestCase):
    def test_stable_id_details_identity_location_and_job_specific_url(self):
        jobs = peoplesoft.fetch_jobs(
            Session([peoplesoft_listing(), peoplesoft_detail()]),
            peoplesoft_target(),
        )

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, PEOPLESOFT_JOB)
        self.assertEqual(job.title, "Team Lead, Credit and Collection")
        self.assertEqual(job.company, "City of Calgary")
        self.assertEqual(job.location, "Calgary, Alberta, Canada")
        self.assertEqual(job.url, PEOPLESOFT_URL)
        self.assertEqual(job.description, "Lead the credit and collections team.")

    def test_official_stale_detail_redirect_is_skipped_without_attribution(self):
        second_url = PEOPLESOFT_URL.replace(PEOPLESOFT_JOB, "315023")
        listing = Response(
            """
            <html><body><h1>City of Calgary Careers</h1>
              <a href="{first}">Closed Role</a>
              <a href="{second}">  TEAM LEAD, CREDIT AND COLLECTION  </a>
            </body></html>
            """.format(first=PEOPLESOFT_URL.replace("&", "&amp;"), second=second_url.replace("&", "&amp;")),
            url="https://www.calgary.ca/careers.html",
        )
        jobs = peoplesoft.fetch_jobs(
            Session(
                [
                    listing,
                    peoplesoft_stale_detail(),
                    peoplesoft_detail(job_id="315023", url=second_url),
                ]
            ),
            peoplesoft_target(),
        )

        self.assertEqual([job.job_id for job in jobs], ["315023"])

    def test_title_and_redirect_identity_remain_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "title does not match"):
            peoplesoft.fetch_jobs(
                Session([peoplesoft_listing(), peoplesoft_detail(title="Different Role")]),
                peoplesoft_target(),
            )
        with self.assertRaisesRegex(AdapterError, "outside its official HTTPS host"):
            peoplesoft.fetch_jobs(
                Session(
                    [
                        peoplesoft_listing(),
                        peoplesoft_detail(url="https://evil.test/redirect"),
                    ]
                ),
                peoplesoft_target(),
            )

    def test_detail_mismatch_missing_apply_and_unofficial_hosts_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "does not match"):
            peoplesoft.fetch_jobs(
                Session([peoplesoft_listing(), peoplesoft_detail(job_id="999999")]),
                peoplesoft_target(),
            )
        with self.assertRaisesRegex(AdapterError, "apply flow"):
            peoplesoft.fetch_jobs(
                Session([peoplesoft_listing(), peoplesoft_detail(include_apply=False)]),
                peoplesoft_target(),
            )
        with self.assertRaisesRegex(AdapterError, "no stable public"):
            peoplesoft.fetch_jobs(
                Session([peoplesoft_listing(url=PEOPLESOFT_URL.replace("recruiting.calgary.ca", "evil.test"))]),
                peoplesoft_target(),
            )

    def test_captcha_and_page_limits_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "CAPTCHA"):
            peoplesoft.fetch_jobs(
                Session([Response("City of Calgary: verify you are human CAPTCHA challenge")]),
                peoplesoft_target(),
            )
        with self.assertRaisesRegex(AdapterError, "exactly one"):
            peoplesoft.fetch_jobs(Session([]), peoplesoft_target(max_pages=2))
        mixed_site = PEOPLESOFT_URL.replace("SiteId=1", "SiteId=2")
        with self.assertRaisesRegex(AdapterError, "mixed site"):
            peoplesoft.fetch_jobs(
                Session([peoplesoft_listing(url=mixed_site)]),
                peoplesoft_target(),
            )


class WorkableFailClosedContractTests(unittest.TestCase):
    def target(self):
        return CompanyTarget.from_dict(
            {
                "name": "AltaLink",
                "platform": "workable",
                "slug": "altalink",
                "careers_url": "https://apply.workable.com/altalink/",
                "identity_regex": r"^AltaLink$",
                "max_pages": 1,
                "max_jobs": 20,
            }
        )

    def test_public_widget_normalizes_stable_official_apply_contract(self):
        portal = Response('<script>window.careers={"wjb_acp_turnstile_captcha_enabled":false}</script>')
        widget = Response(
            {
                "name": "AltaLink",
                "jobs": [
                    {
                        "title": "Real Time System Analyst I",
                        "shortcode": "F6ADABDA9D",
                        "application_url": "https://apply.workable.com/j/F6ADABDA9D/apply",
                        "published_on": "2026-07-31",
                        "department": "Operations (Perm/Term)",
                        "locations": [
                            {"country": "Canada", "city": "Calgary", "region": "Alberta"}
                        ],
                    }
                ],
            }
        )
        jobs = workable.fetch_jobs(Session([portal, widget]), self.target())
        self.assertEqual(jobs[0].job_id, "F6ADABDA9D")
        self.assertEqual(jobs[0].company, "AltaLink")
        self.assertEqual(jobs[0].location, "Calgary, Alberta, Canada")
        self.assertEqual(jobs[0].url, "https://apply.workable.com/j/F6ADABDA9D/apply")

    def test_altalink_turnstile_flag_fails_before_widget_request(self):
        session = Session(
            [Response('<script>window.careers={"wjb_acp_turnstile_captcha_enabled":true}</script>')]
        )
        with self.assertRaisesRegex(AdapterError, "CAPTCHA"):
            workable.fetch_jobs(session, self.target())
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("workable", scanner.ADAPTERS)


class RecoveryTargetValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = yaml.safe_load(VALIDATION.read_text(encoding="utf-8"))
        cls.rows = {row["name"]: row for row in cls.data["recovery_targets"]}

    def test_exact_targets_and_repeatable_validation_outcome(self):
        self.assertEqual(
            list(self.rows),
            [
                "SMART Technologies",
                "CoolIT Systems",
                "AltaLink",
                "Ballard Power Systems",
                "City of Calgary",
            ],
        )
        ready = [name for name, row in self.rows.items() if row["production_ready"]]
        self.assertEqual(ready, ["CoolIT Systems", "Ballard Power Systems", "City of Calgary"])
        self.assertEqual(self.rows["SMART Technologies"]["validation_status"], "session_or_waf_dependency")
        self.assertEqual(self.rows["AltaLink"]["validation_status"], "captcha_dependency")

    def test_only_repeatably_validated_adapters_are_registered(self):
        self.assertIs(scanner.ADAPTERS["ukg_pro"], ukg_pro.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["paycor"], paycor.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["peoplesoft"], peoplesoft.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["dayforce"], dayforce.fetch_jobs)
        self.assertNotIn("workable", scanner.ADAPTERS)

    def test_registered_targets_use_the_shared_registry_validator(self):
        expected_sources = {
            "CoolIT Systems": (
                "ukg_pro:recruiting.ultipro.ca:coo5001colt:"
                "05857940-9028-4d4a-8544-26bd990d20d6"
            ),
            "Ballard Power Systems": f"paycor:{PAYCOR_CLIENT}",
            "City of Calgary": "peoplesoft:recruiting.calgary.ca:1",
        }
        for name, source in expected_sources.items():
            with self.subTest(name=name):
                self.assertIsNone(registry_validator.configuration_error(self.rows[name]))
                self.assertEqual(registry_validator.source_key(self.rows[name]), source)

    def test_only_ready_targets_are_appended_to_the_unchanged_baseline(self):
        # The validation artifact remains immutable milestone evidence.
        self.assertFalse(self.data["production_registry_activated"])
        self.assertTrue(all(not row["activated"] for row in self.rows.values()))

        production_data = yaml.safe_load(PRODUCTION.read_text(encoding="utf-8"))["target_companies"]
        historical_data = yaml.safe_load(HISTORICAL_PRODUCTION.read_text(encoding="utf-8"))["target_companies"]
        baseline_count = self.data["baseline"]["production_sources"]
        promoted = ["CoolIT Systems", "Ballard Power Systems", "City of Calgary"]

        self.assertGreaterEqual(len(production_data), baseline_count + len(promoted))
        assert_historical_prefix(self, production_data, historical_data)
        self.assertEqual(
            [row["name"] for row in production_data[baseline_count:baseline_count + len(promoted)]],
            promoted,
        )
        self.assertEqual(
            len(load_parent_source_mappings()),
            self.data["baseline"]["parent_mappings"] + 1,
        )
        self.assertEqual(
            (
                self.data["baseline"]["original_master_coverage"],
                self.data["baseline"]["original_master_total"],
            ),
            (186, 246),
        )
        production_names = [row["name"] for row in production_data]
        for name in promoted:
            self.assertEqual(production_names.count(name), 1)
        historical_names = production_names[:baseline_count + len(promoted)]
        self.assertNotIn("SMART Technologies", historical_names)
        self.assertEqual(production_names.count("SMART Technologies"), 1)
        self.assertNotIn("AltaLink", production_names)

        runtime_fields = {
            "ukg_pro": ("name", "platform", "careers_url", "identity_regex", "max_pages", "max_jobs"),
            "paycor": (
                "name", "platform", "careers_url", "endpoint_url", "identity_regex", "max_pages", "max_jobs",
            ),
            "peoplesoft": (
                "name", "platform", "careers_url", "listing_url", "identity_regex",
                "allowed_apply_hosts", "site_id", "max_pages", "max_jobs",
            ),
        }
        for production_row in production_data[baseline_count:baseline_count + len(promoted)]:
            validated_row = self.rows[production_row["name"]]
            fields = runtime_fields[production_row["platform"]]
            self.assertEqual(production_row, {field: validated_row[field] for field in fields})

    def test_generic_adapters_have_no_company_specific_branches(self):
        source = "\n".join(inspect.getsource(module).casefold() for module in (ukg_pro, paycor, peoplesoft, workable))
        for company in self.rows:
            self.assertNotIn(company.casefold(), source)

    def test_pason_attribution_remains_separate(self):
        source = inspect.getsource(ukg_pro).casefold()
        self.assertNotIn("pason", source)
        self.assertNotIn("iws", source)


if __name__ == "__main__":
    unittest.main()
