from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

import yaml

from tests.company_careers_reliability_assertions import with_reliability_repairs

from scripts import company_watchlist_common
from scripts import validate_ats_adapter_expansion_v2_batch_b as batch
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import deel, talentbrew, teamtailor
from src.scraper.company_careers.models import AdapterError, CompanyTarget


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, value, *, status=200, url="https://jobs.test", content_type="text/html"):
        self.status_code = status
        self.url = url
        self.text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected GET {url}")
        return self.responses.pop(0)


def teamtailor_target(**overrides):
    values = {
        "name": "Example Semiconductor",
        "platform": "teamtailor",
        "listing_url": "https://careers.example.test/jobs",
        "endpoint_variant": teamtailor.ENDPOINT_VARIANT,
        "identity_regex": r"Example Semiconductor",
        "allowed_apply_hosts": ["careers.example.test"],
        "max_pages": 1,
        "max_jobs": 20,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def talentbrew_target(**overrides):
    values = {
        "name": "Example Networks",
        "platform": "talentbrew",
        "listing_url": "https://jobs.example.test/search-jobs/",
        "endpoint_variant": talentbrew.ENDPOINT_VARIANT,
        "identity_regex": r"Example Networks",
        "allowed_apply_hosts": ["jobs.example.test"],
        "max_pages": 1,
        "max_jobs": 20,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def deel_target(**overrides):
    values = {
        "name": "Example Carbon",
        "platform": "deel",
        "tenant": "example-carbon",
        "listing_url": "https://jobs.deel.test/example-carbon",
        "endpoint_variant": deel.ENDPOINT_VARIANT,
        "identity_regex": r"Example Carbon Technologies",
        "allowed_apply_hosts": ["jobs.deel.test"],
        "max_pages": 1,
        "max_jobs": 20,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


class GenericAdapterTests(unittest.TestCase):
    def test_teamtailor_normalizes_standard_cards(self):
        body = """<html><title>Example Semiconductor jobs</title><body>
        <ul id="jobs_list_container"><li><a href="/jobs/8190875-firmware-engineer">
        Firmware Engineer Intern</a><span class="text-base"><span>Engineering</span>
        <span class="mx-[2px]">·</span><span>Trondheim</span></span></li></ul></body></html>"""
        jobs = teamtailor.fetch_jobs(
            Session([Response(body, url="https://careers.example.test/jobs")]),
            teamtailor_target(),
        )
        self.assertEqual(jobs[0].job_id, "teamtailor:careers.example.test:8190875")
        self.assertEqual(jobs[0].title, "Firmware Engineer Intern")
        self.assertEqual(jobs[0].location, "Trondheim")
        self.assertEqual(jobs[0].url, "https://careers.example.test/jobs/8190875-firmware-engineer")

    def test_talentbrew_deduplicates_responsive_cards_by_stable_id(self):
        card = """<a data-job-id="98062537056" href="/job/calgary/engineer/1/98062537056">
        <h2>Systems Engineer</h2><span class="job-location">Calgary, Alberta</span>
        <span class="job-category">Engineering</span></a>"""
        body = f"<html><title>Example Networks careers</title><section id='search-results'>{card}{card}</section></html>"
        jobs = talentbrew.fetch_jobs(
            Session([Response(body, url="https://jobs.example.test/search-jobs/")]),
            talentbrew_target(),
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "talentbrew:jobs.example.test:98062537056")
        self.assertEqual(jobs[0].location, "Calgary, Alberta")

    def test_deel_itemlist_and_jobposting_are_exact_and_bounded(self):
        job_id = "21faddcf-7ad7-43f3-83b6-8a5615416aa2"
        url = f"https://jobs.deel.test/example-carbon/job-details/{job_id}/overview"
        listing = f"""<html><script type="application/ld+json">{{"@type":"Organization",
        "name":"Example Carbon Technologies","url":"https://jobs.deel.test/example-carbon"}}</script>
        <script type="application/ld+json">{{"@type":"ItemList","itemListElement":[{{"@type":"ListItem",
        "position":1,"url":"{url}"}}]}}</script></html>"""
        detail = f"""<html><script type="application/ld+json">{{"@type":"JobPosting",
        "title":"Process Engineering Student","datePosted":"2026-08-10T12:00:00Z",
        "hiringOrganization":{{"name":"Example Carbon Technologies"}},
        "jobLocation":[{{"address":{{"addressLocality":"Calgary","addressRegion":"Alberta",
        "addressCountry":"Canada"}}}}],"identifier":{{"value":"{job_id}"}},
        "directApply":true,"url":"{url}"}}</script></html>"""
        jobs = deel.fetch_jobs(
            Session([
                Response(listing, url="https://jobs.deel.test/example-carbon"),
                Response(detail, url=url),
            ]),
            deel_target(),
        )
        self.assertEqual(jobs[0].job_id, f"deel:example-carbon:{job_id}")
        self.assertEqual(jobs[0].location, "Calgary, Alberta, Canada")
        self.assertEqual(jobs[0].updated_at, "2026-08-10")
        self.assertEqual(jobs[0].url, url)

    def test_deel_exact_empty_itemlist_is_valid_zero_job_contract(self):
        body = """<html><script type="application/ld+json">{"@type":"Organization",
        "name":"Example Carbon Technologies","url":"https://jobs.deel.test/example-carbon"}</script>
        <script type="application/ld+json">{"@type":"ItemList","itemListElement":[]}</script></html>"""
        session = Session([Response(body, url="https://jobs.deel.test/example-carbon")])
        self.assertEqual(deel.fetch_jobs(session, deel_target()), [])
        self.assertTrue(session.deel_pagination["zero_job_contract"])

    def test_identity_challenge_session_and_bounds_fail_closed(self):
        wrong_identity = "<html><title>Other Company</title><ul id='jobs_list_container'></ul></html>"
        with self.assertRaises(AdapterError):
            teamtailor.fetch_jobs(Session([Response(wrong_identity)]), teamtailor_target())
        challenge = "<html><title>Access denied</title>Cloudflare challenge CAPTCHA</html>"
        with self.assertRaisesRegex(AdapterError, "blocked"):
            talentbrew.fetch_jobs(Session([Response(challenge)]), talentbrew_target())
        with self.assertRaisesRegex(AdapterError, "page limit"):
            teamtailor.validate_target(teamtailor_target(max_pages=3))
        with self.assertRaisesRegex(AdapterError, "exact configured tenant"):
            deel.validate_target(deel_target(listing_url="https://jobs.deel.test/other"))


class BatchArtifactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production = yaml.safe_load(batch.PRODUCTION.read_text(encoding="utf-8"))["target_companies"]
        cls.candidates = yaml.safe_load(batch.CANDIDATE_REGISTRY.read_text(encoding="utf-8"))["target_companies"]
        cls.validated = yaml.safe_load(batch.VALIDATED_REGISTRY.read_text(encoding="utf-8"))["target_companies"]
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))

    def test_activation_preserves_validated_registry_exactly(self):
        self.assertEqual(len(self.production), 401)
        self.assertEqual(len(self.candidates), 339)
        self.assertEqual(self.production[:339], with_reliability_repairs(self.validated))
        self.assertEqual(self.candidates, self.validated)
        production = {row["name"]: row for row in self.production}
        candidate = {row["name"]: row for row in self.candidates}
        self.assertEqual(production["Nordic Semiconductor"]["platform"], "teamtailor")
        self.assertEqual(production["Palo Alto Networks"]["platform"], "talentbrew")
        self.assertEqual(production["Rakuten"]["platform"], "talentbrew")
        self.assertEqual(production["Carbon Upcycling"]["platform"], "deel")
        self.assertEqual(candidate["Nordic Semiconductor"]["platform"], "teamtailor")
        self.assertEqual(candidate["Palo Alto Networks"]["platform"], "talentbrew")
        self.assertEqual(candidate["Rakuten"]["platform"], "talentbrew")
        self.assertEqual(candidate["Carbon Upcycling"]["platform"], "deel")
        production_names = [row["name"] for row in self.production]
        self.assertEqual(production_names[338], "Carbon Upcycling")
        self.assertEqual(production_names.count("Carbon Upcycling"), 1)
        self.assertNotIn("Best Buy", candidate)
        self.assertFalse(self.report["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_fixture_outcomes_jobs_hits_and_projection_are_exact(self):
        self.assertEqual([row["company"] for row in self.report["results"]], batch.FIXTURES)
        self.assertEqual(
            self.report["migrated_fixtures"],
            ["Nordic Semiconductor", "Palo Alto Networks", "Rakuten"],
        )
        self.assertEqual(self.report["recovered_fixtures"], ["Carbon Upcycling"])
        self.assertEqual(self.report["remaining_blockers"][0]["company"], "Best Buy")
        self.assertEqual(
            self.report["remaining_blockers"][0]["validation_status"],
            "session_token_dependency",
        )
        self.assertEqual(self.report["projection"], {
            "production_sources": 339,
            "parent_mappings": 4,
            "original_master_coverage": "208/246",
            "master_delta": 0,
        })
        self.assertEqual(self.report["jobs"]["normalized_total"], 48)
        self.assertEqual(self.report["jobs"]["student_intern_hits_total"], 0)

    def test_adapters_are_generic_registered_and_registry_valid(self):
        self.assertIs(scanner.ADAPTERS["teamtailor"], teamtailor.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["talentbrew"], talentbrew.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["deel"], deel.fetch_jobs)
        self.assertNotIn("servicenow", scanner.ADAPTERS)
        expected = {
            "Nordic Semiconductor": "teamtailor:careers.nordicsemi.com",
            "Palo Alto Networks": "talentbrew:jobs.paloaltonetworks.com",
            "Rakuten": "talentbrew:japan-job-en.rakuten.careers",
            "Carbon Upcycling": "deel:carbon-upcycling-technologies",
        }
        for entry in batch.CANDIDATE_ENTRIES:
            self.assertIsNone(registry_validator.configuration_error(entry))
            self.assertEqual(registry_validator.source_key(entry), expected[entry["name"]])
        detection = company_watchlist_common.detect_ats(
            "https://jobs.deel.com/carbon-upcycling-technologies"
        )
        self.assertEqual((detection.platform, detection.tenant), ("deel", "carbon-upcycling-technologies"))

    def test_adapter_sources_have_no_fixture_specific_branches(self):
        source = "\n".join(inspect.getsource(module).casefold() for module in (teamtailor, talentbrew, deel))
        for company in batch.FIXTURES:
            self.assertNotIn(company.casefold(), source)
        for entry in batch.CANDIDATE_ENTRIES:
            if entry["platform"] in {"teamtailor", "talentbrew"}:
                self.assertNotIn("job_card_selector", entry)
                self.assertNotIn("title_selector", entry)
                self.assertNotIn("location_selector", entry)


if __name__ == "__main__":
    unittest.main()
