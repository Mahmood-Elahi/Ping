from __future__ import annotations

import base64
import inspect
import json
import unittest
from pathlib import Path

import yaml

from scripts import company_watchlist_common, investigate_remaining_companies, investigate_unknown_companies
from scripts import validate_ats_adapter_expansion_v2_batch_a as batch
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import cornerstone_csod, sap_successfactors
from src.scraper.company_careers.models import AdapterError, CompanyTarget


ROOT = Path(__file__).resolve().parents[1]


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


def _token(*, corp="example", subject=-100, routes="rec-job-search/external"):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'HS512'})}.{encode({'corp': corp, 'sub': subject, 'rurls': routes})}.signature"


def csod_target(**overrides):
    values = {
        "name": "Example Space",
        "platform": "cornerstone_csod",
        "careers_url": "https://www.example.test/careers",
        "search_url": "https://example.csod.com/ux/ats/careersite/4/home?c=example&lang=en-US",
        "endpoint_url": "https://us.api.csod.com/rec-job-search/external/jobs",
        "endpoint_variant": cornerstone_csod.ENDPOINT_VARIANT,
        "tenant": "example",
        "site_id": "4",
        "locale": "en-US",
        "identity_regex": r"Example Space",
        "allowed_apply_hosts": ["example.csod.com"],
        "page_size": 25,
        "max_pages": 1,
        "max_jobs": 20,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def official_page(*, href=None, identity="Example Space"):
    href = href or "https://example.csod.com/ux/ats/careersite/4/home?c=example&amp;lang=en-US"
    return Response(f'<html><title>{identity} Careers</title><a href="{href}">Jobs</a></html>')


def csod_shell(*, corp="example", culture="en-US", culture_id=1, token=None, cloud="https://us.api.csod.com/"):
    context = {
        "corp": corp,
        "cultureID": culture_id,
        "cultureName": culture,
        "endpoints": {"cloud": cloud, "api": "/"},
        "token": token or _token(corp=corp),
    }
    return Response(f"<html><script>if(!csod.context) csod.context={json.dumps(context)};</script></html>")


def csod_page(*, total=1, requisitions=None):
    if requisitions is None:
        requisitions = [{
            "requisitionId": 7790,
            "displayJobTitle": "Systems Engineer Intern",
            "postingEffectiveDate": "08/09/2026",
            "postingExpirationDate": "09/09/2026",
            "locations": [{"city": "Calgary", "state": "Alberta", "country": "CA"}],
            "externalDescription": "Build reliable spacecraft systems.",
        }]
    return Response({"status": "Success", "data": {"totalCount": total, "requisitions": requisitions}})


def columnized_target(**overrides):
    values = {
        "name": "Example Semiconductor",
        "platform": "sap_successfactors",
        "careers_url": "https://example.test/careers",
        "search_url": "https://jobs.example.test/search/",
        "endpoint_url": "https://jobs.example.test/services/recruiting/v1/jobs",
        "endpoint_variant": sap_successfactors.COLUMNIZED_VARIANT,
        "identity_regex": r"Example Semiconductor",
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
        '<html>Example Semiconductor<link href="BS3ColumnizedSearch.css">'
        '<script src="j2w.searchManager.min.js"></script>'
        '<script>var CSRFToken = "11111111-2222-3333-4444-555555555555";</script></html>'
    )


def columnized_page():
    return Response({
        "jobSearchResult": [{"response": {
            "id": "77934", "unifiedStandardTitle": "Equipment Engineering Tech 3",
            "unifiedUrlTitle": "Equipment-Engineering-Tech-3",
            "unifiedStandardStart": "8/9/26", "supportedLocales": ["en_US"],
            "jobLocationShort": ["Newbury Park, CA, USA"],
        }}],
        "totalJobs": 1,
    })


def columnized_detail():
    url = "https://jobs.example.test/job/Equipment-Engineering-Tech-3/77934-en_US"
    return Response(
        f'''<html><head><link rel="canonical" href="{url}/">
        <meta property="og:title" content="Equipment Engineering Tech 3"></head>
        <body>Example Semiconductor<ul><li>Req ID: 77934</li><li>Location: Newbury Park, CA, USA</li></ul>
        <script>j2w.Apply.init({{ jobID : 77934 }});</script></body></html>''',
        url=url,
    )


class CornerstoneCsodAdapterTests(unittest.TestCase):
    def test_anonymous_public_contract_normalizes_stable_ids_and_urls(self):
        session = Session(gets=[official_page(), csod_shell()], posts=[csod_page()])
        jobs = cornerstone_csod.fetch_jobs(session, csod_target())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "cornerstone_csod:example.csod.com:4:7790")
        self.assertEqual(jobs[0].location, "Calgary, Alberta, CA")
        self.assertEqual(jobs[0].updated_at, "2026-08-09")
        self.assertEqual(
            jobs[0].url,
            "https://example.csod.com/ux/ats/careersite/4/home/requisition/7790?c=example&lang=en-US",
        )
        post = session.calls[2]
        self.assertEqual(post[1], "https://us.api.csod.com/rec-job-search/external/jobs")
        self.assertEqual(post[2]["json"]["pageNumber"], 1)
        self.assertEqual(post[2]["json"]["pageSize"], 25)
        self.assertTrue(post[2]["headers"]["Authorization"].startswith("Bearer "))

    def test_attribution_anonymity_challenge_and_bounds_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "did not link"):
            cornerstone_csod.fetch_jobs(
                Session(gets=[official_page(href="https://other.csod.com/ux/ats/careersite/4/home?c=other")]),
                csod_target(),
            )
        with self.assertRaisesRegex(AdapterError, "anonymous"):
            cornerstone_csod.fetch_jobs(
                Session(gets=[official_page(), csod_shell(token=_token(subject=42))]), csod_target()
            )
        with self.assertRaisesRegex(AdapterError, "CAPTCHA"):
            cornerstone_csod.fetch_jobs(
                Session(gets=[Response("Example Space verify you are human g-recaptcha")]), csod_target()
            )
        with self.assertRaisesRegex(AdapterError, "page_size"):
            cornerstone_csod.fetch_jobs(Session(), csod_target(page_size=50))

    def test_page_size_and_total_are_strict(self):
        too_many = [dict(csod_page()._value["data"]["requisitions"][0], requisitionId=i) for i in range(26)]
        with self.assertRaisesRegex(AdapterError, "page exceeded"):
            cornerstone_csod.fetch_jobs(
                Session(gets=[official_page(), csod_shell()], posts=[csod_page(total=26, requisitions=too_many)]),
                csod_target(max_jobs=50),
            )


class SuccessFactorsColumnizedContractTests(unittest.TestCase):
    def test_existing_columnized_contract_remains_supported(self):
        jobs = sap_successfactors.fetch_jobs(
            Session(gets=[columnized_identity(), columnized_detail()], posts=[columnized_page()]),
            columnized_target(),
        )
        self.assertEqual(jobs[0].job_id, "sap_successfactors:jobs.example.test:77934")
        self.assertEqual(jobs[0].location, "Newbury Park, CA, USA")
        self.assertEqual(jobs[0].url, "https://jobs.example.test/job/Equipment-Engineering-Tech-3/77934-en_US")


class BatchArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[: cls.validated_bytes.index(b"  - name: CN Rail")]
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        cls.production = yaml.safe_load(cls.production_bytes)["target_companies"]
        cls.candidates = yaml.safe_load(cls.candidate_bytes)["target_companies"]
        cls.validated = yaml.safe_load(cls.validated_bytes)["target_companies"]
        cls.historical = yaml.safe_load(cls.historical_bytes)["target_companies"]

    def test_activation_preserves_exact_336_entry_production_prefix(self):
        self.assertEqual(len(self.production), 344)
        self.assertEqual(len(self.candidates), 338)
        self.assertEqual(len(self.validated), 338)
        self.assertEqual(len(self.historical), 336)
        self.assertEqual(self.candidate_bytes, self.validated_bytes)
        self.assertEqual([row["name"] for row in self.candidates[336:]], ["CN Rail", "OHB SE"])
        self.assertEqual(self.candidates, self.validated)
        self.assertEqual(
            [row["name"] for row in self.production[:336]],
            [row["name"] for row in self.validated[:336]],
        )
        self.assertEqual(self.production[336:338], self.validated[336:])
        production_names = [row["name"] for row in self.production]
        self.assertEqual(production_names[338], "Carbon Upcycling")
        for name in ("CN Rail", "OHB SE"):
            self.assertEqual(production_names.count(name), 1)
        self.assertEqual(self.report["projection"]["production_sources"], 338)
        self.assertEqual(self.report["projection"]["original_master_coverage"], 208)
        self.assertFalse(self.report["production_registry_activated"])
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_all_ten_fixtures_have_exact_outcomes(self):
        results = {row["company"]: row for row in self.report["results"]}
        self.assertEqual(list(results), batch.FIXTURES)
        self.assertEqual(self.report["recovered_companies"], ["CN Rail", "OHB SE"])
        blockers = {
            "WestJet": "session_or_waf_dependency",
            "SMART Technologies": "session_or_waf_dependency",
            "Ross Video": "session_or_waf_dependency",
            "Chemelex": "session_or_waf_dependency",
            "Blackline Safety": "captcha_dependency",
            "AltaLink": "captcha_dependency",
            "Hugging Face": "captcha_dependency",
            "Skyworks Solutions": "session_state_dependency",
        }
        self.assertEqual(
            {row["company"]: row["validation_status"] for row in self.report["remaining_blockers"]},
            blockers,
        )
        production_names = {row["name"] for row in self.production}
        self.assertTrue(set(blockers).isdisjoint(production_names))

    def test_adapters_and_registry_validator_are_wired_generically(self):
        self.assertIs(scanner.ADAPTERS["cornerstone_csod"], cornerstone_csod.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["sap_successfactors"], sap_successfactors.fetch_jobs)
        self.assertNotIn("dayforce", scanner.ADAPTERS)
        self.assertNotIn("workable", scanner.ADAPTERS)
        expected = {
            "CN Rail": "cornerstone_csod:cn360.csod.com:1",
            "OHB SE": "cornerstone_csod:career-ohb.csod.com:4",
        }
        for row in batch.CANDIDATE_ENTRIES:
            self.assertIsNone(registry_validator.configuration_error(row))
            self.assertEqual(registry_validator.source_key(row), expected[row["name"]])

    def test_adapter_and_research_classification_have_no_fixture_branches(self):
        source = inspect.getsource(cornerstone_csod).casefold()
        for company in batch.FIXTURES:
            self.assertNotIn(company.casefold(), source)
        self.assertIn("cornerstone_csod", investigate_remaining_companies.EXISTING_ADAPTER_FAMILIES)
        self.assertIn("cornerstone_csod", investigate_unknown_companies.EXISTING_ADAPTERS)
        self.assertIn("cornerstone_csod", company_watchlist_common.DETECTED_REQUIRES_REVIEW)
        detection = company_watchlist_common.detect_ats(
            "https://example.csod.com/ux/ats/careersite/4/home?c=example&lang=en-US"
        )
        self.assertEqual((detection.platform, detection.tenant, detection.site_id), ("cornerstone_csod", "example", "4"))


if __name__ == "__main__":
    unittest.main()
