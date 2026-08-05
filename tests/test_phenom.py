import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode

import requests

from scripts import validate_company_registry as validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import phenom
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


EXPECTED_COMPANIES = [
    "Mastercard",
    "ABB",
    "Toyota",
    "Honda",
    "NASA Jet Propulsion Laboratory",
    "eBay",
]


class FakeResponse:
    def __init__(
        self,
        text="",
        status_code=200,
        *,
        content_type="text/html; charset=UTF-8",
        url="https://careers.example.com/us/en/search-results",
        headers=None,
    ):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.headers.update(headers or {})
        self.url = url
        self.history = []


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        params = kwargs.get("params") or {}
        response.url = url + (("?" + urlencode(params)) if params else "")
        return response


class FailingSessionFactory:
    def __call__(self):
        raise AssertionError("network session initialized")


def target(**overrides):
    values = {
        "name": "Example",
        "platform": "phenom",
        "careers_url": "https://careers.example.com/us/en/",
        "search_url": "https://careers.example.com/us/en/search-results",
        "endpoint_variant": "embedded_refine_search",
        "ref_num": "EXAMPLE",
        "locale": "en_us",
        "allowed_apply_hosts": [
            "careers.example.com",
            "example.wd1.myworkdayjobs.com",
            "career41.sapsf.com",
        ],
        "max_pages": 2,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def registry_entry(**overrides):
    values = {
        "name": "Example",
        "platform": "phenom",
        "careers_url": "https://careers.example.com/us/en/",
        "search_url": "https://careers.example.com/us/en/search-results",
        "endpoint_variant": "embedded_refine_search",
        "ref_num": "EXAMPLE",
        "locale": "en_us",
        "allowed_apply_hosts": ["example.wd1.myworkdayjobs.com"],
        "max_pages": 2,
        "fingerprint_confidence": "high",
        "fingerprint_evidence": "official page embedded Phenom contract",
    }
    values.update(overrides)
    return values


def job(index=1, **overrides):
    values = {
        "reqId": f"REQ-{index}",
        "jobId": f"REQ-{index}",
        "jobSeqNo": f"EXAMPLEREQ{index}EXTERNALENUS",
        "title": f"Software Engineer {index}",
        "location": "Calgary, Alberta, Canada",
        "multi_location": [
            "Calgary, Alberta, Canada",
            "calgary, alberta, canada",
            "Edmonton, Alberta, Canada",
        ],
        "remote": "Hybrid",
        "descriptionTeaser": "<p>Build <b>safe</b> systems.</p><ul><li>Test them</li></ul>",
        "postedDate": "2026-08-04T00:00:00.000+0000",
        "category": "Engineering",
        "department": "Platform",
        "businessSegment": "Automation",
        "businessUnit": "Cloud",
        "team": "Runtime",
        "jobFamily": "Software",
        "applyUrl": (
            "https://example.wd1.myworkdayjobs.com/Jobs/job/Calgary/"
            f"Software-Engineer_REQ-{index}/apply"
        ),
    }
    values.update(overrides)
    return values


def page(jobs, total=None, *, status=200, hits=None, data_overrides=None):
    jobs = list(jobs)
    data = {
        "jobs": jobs,
        "aggregations": [],
        "SEARCH_CONFIG": {"contextualSearch": False},
        "suggestions": [],
        "ui_skills_selection": [],
        "locationData": {},
        "keywords": "",
        "subsearch": "",
    }
    data.update(data_overrides or {})
    return {
        "status": status,
        "hits": len(jobs) if hits is None else hits,
        "totalHits": len(jobs) if total is None else total,
        "data": data,
        "eid": {"eid": "test"},
    }


def html_response(
    search=None,
    *,
    ref_num="EXAMPLE",
    locale="en_us",
    page_name="search-results",
    base_domain="https://careers.example.com",
    ddo_overrides=None,
):
    config = {
        "widgetApiEndpoint": "https://careers.example.com/widgets",
        "country": "us",
        "deviceType": "desktop",
        "locale": locale,
        "refNum": ref_num,
        "baseUrl": "https://careers.example.com/us/en/",
        "baseDomain": base_domain,
        "pageName": page_name,
        "siteType": "external",
        "pageId": "page11",
    }
    ddo = {"siteConfig": {"status": "success"}, "flashParams": {}}
    if search is not None:
        ddo["eagerLoadRefineSearch"] = search
    ddo.update(ddo_overrides or {})
    text = (
        "<html><head><script>/*&lt;!--*/ var phApp = phApp || "
        + json.dumps(config)
        + "; phApp.ddo = "
        + json.dumps(ddo)
        + "; phApp.sessionParams = {\"from\":\"0\"}; /*--&gt;*/</script></head></html>"
    )
    return FakeResponse(text)


class PhenomAdapterTests(unittest.TestCase):
    def fetch(self, responses, **target_overrides):
        session = FakeSession(responses)
        with patch.object(phenom.time, "sleep", return_value=None):
            jobs = phenom.fetch_jobs(session, target(**target_overrides))
        return jobs, session

    def test_normalizes_ids_apply_locations_remote_description_date_and_metadata(self):
        jobs, _ = self.fetch([html_response(page([job()]))])
        record = jobs[0]
        self.assertEqual(record.job_id, "phenom:careers.example.com:EXAMPLE:REQ-1")
        self.assertEqual(record.source, "phenom:careers.example.com:EXAMPLE")
        self.assertEqual(
            record.url,
            "https://example.wd1.myworkdayjobs.com/Jobs/job/Calgary/Software-Engineer_REQ-1/apply",
        )
        self.assertEqual(record.offices, ["Calgary, Alberta, Canada", "Edmonton, Alberta, Canada"])
        self.assertEqual(
            record.location,
            "Calgary, Alberta, Canada | Edmonton, Alberta, Canada | Hybrid",
        )
        self.assertEqual(record.description, "Build safe systems.\nTest them")
        self.assertEqual(record.updated_at, "2026-08-04")
        self.assertEqual(
            record.departments,
            ["Platform", "Engineering", "Automation", "Cloud", "Runtime", "Software"],
        )
        self.assertEqual(record.raw_metadata["external_job_id"], "REQ-1")
        self.assertEqual(record.raw_metadata["ref_num"], "EXAMPLE")

    def test_url_id_fallback_uses_observed_successfactors_query_identifier(self):
        item = job(
            reqId="",
            jobId="",
            jobSeqNo="",
            applyUrl=(
                "https://career41.sapsf.com/career?company=nahondaPRD&"
                "career_job_req_id=9039"
            ),
        )
        jobs, _ = self.fetch([html_response(page([item]))])
        self.assertEqual(jobs[0].job_id, "phenom:careers.example.com:EXAMPLE:9039")

    def test_relative_apply_url_is_resolved_and_unrelated_domain_is_rejected(self):
        relative = job(applyUrl="/us/en/hvhapply?jobSeqNo=EXAMPLE1")
        jobs, _ = self.fetch([html_response(page([relative]))])
        self.assertEqual(
            jobs[0].url,
            "https://careers.example.com/us/en/hvhapply?jobSeqNo=EXAMPLE1",
        )
        invalid = job(applyUrl="https://jobs.example-aggregator.test/REQ-1")
        with self.assertRaisesRegex(AdapterError, "none were valid"):
            self.fetch([html_response(page([invalid]))])

    def test_honda_observed_apply_template_avoids_detail_requests(self):
        item = job(applyUrl="", jobSeqNo="AHMAHMUS11219EXTERNALENUS")
        jobs, session = self.fetch(
            [html_response(page([item]))],
            apply_url_template=(
                "https://careers.example.com/us/en/hvhapply?jobSeqNo={job_seq_no}"
            ),
        )
        self.assertEqual(
            jobs[0].url,
            "https://careers.example.com/us/en/hvhapply?jobSeqNo=AHMAHMUS11219EXTERNALENUS",
        )
        self.assertEqual(len(session.calls), 1)

    def test_location_maps_remote_hybrid_and_case_insensitive_deduplication(self):
        item = job(
            location="Remote, Canada",
            multi_location_array=[
                {"location": "remote, canada"},
                {"location": "Vancouver, British Columbia, Canada"},
            ],
            multi_location=[],
            remote="Remote or Hybrid",
            workstyle="Hybrid",
        )
        jobs, _ = self.fetch([html_response(page([item]))])
        self.assertEqual(
            jobs[0].location,
            "Remote, Canada | Vancouver, British Columbia, Canada | Remote or Hybrid",
        )

    def test_missing_optional_metadata_keeps_valid_job(self):
        item = job(
            location="",
            multi_location=[],
            remote=None,
            descriptionTeaser=None,
            postedDate=None,
            category=None,
            department=None,
            businessSegment=None,
            businessUnit=None,
            team=None,
            jobFamily=None,
        )
        jobs, _ = self.fetch([html_response(page([item]))])
        self.assertEqual(jobs[0].location, "")
        self.assertEqual(jobs[0].updated_at, "")
        self.assertEqual(jobs[0].departments, [])
        self.assertIsNone(jobs[0].description)

    def test_malformed_individual_job_is_skipped_with_diagnostic(self):
        jobs, session = self.fetch(
            [html_response(page([job(1), job(2, title="")], total=2))]
        )
        self.assertEqual([record.title for record in jobs], ["Software Engineer 1"])
        self.assertEqual(len(session.phenom_pagination["diagnostics"]), 1)

    def test_malformed_entire_http_200_is_rejected(self):
        cases = [
            FakeResponse("<html>generic landing page</html>"),
            html_response(None),
            html_response(page([], total=0, status=500)),
            html_response(page([], total=-1)),
            html_response(page([], total=0, data_overrides={"jobs": {}})),
            html_response(page([job()], total=1, hits=0)),
        ]
        for response in cases:
            with self.subTest(text=response.text[:80]):
                with self.assertRaises(AdapterError):
                    self.fetch([response])

    def test_valid_zero_job_response(self):
        jobs, session = self.fetch([html_response(page([], total=0))])
        self.assertEqual(jobs, [])
        self.assertEqual(session.phenom_pagination["stop_reason"], "validated_zero_jobs")

    def test_offset_pagination_is_deterministic(self):
        first = [job(index) for index in range(1, 11)]
        second = [job(index) for index in range(11, 16)]
        jobs, session = self.fetch(
            [html_response(page(first, total=15)), html_response(page(second, total=15))]
        )
        self.assertEqual(len(jobs), 15)
        self.assertEqual([call[1]["params"] for call in session.calls], [{}, {"from": "10", "s": "1"}])
        self.assertEqual(session.phenom_pagination["offsets"], [0, 10])
        self.assertEqual(session.phenom_pagination["stop_reason"], "total_jobs_reached")

    def test_repeated_page_and_no_new_id_stops(self):
        first = [job(index) for index in range(1, 11)]
        for second, expected in (
            (first, "repeated_page"),
            (list(reversed(first)), "no_new_stable_ids"),
        ):
            with self.subTest(expected=expected):
                jobs, session = self.fetch(
                    [html_response(page(first, total=30)), html_response(page(second, total=30))]
                )
                self.assertEqual(len(jobs), 10)
                self.assertEqual(session.phenom_pagination["stop_reason"], expected)

    def test_repeated_stable_ids_are_diagnosed_but_new_jobs_continue(self):
        first = [job(index) for index in range(1, 11)]
        second = [job(10)] + [job(index) for index in range(11, 20)]
        jobs, session = self.fetch(
            [html_response(page(first, total=20)), html_response(page(second, total=20))]
        )
        self.assertEqual(len(jobs), 19)
        self.assertTrue(session.phenom_pagination["diagnostics"])

    def test_maximum_pages_results_and_configuration_are_enforced(self):
        responses = [
            html_response(page([job(p * 10 + i) for i in range(1, 11)], total=100))
            for p in range(5)
        ]
        jobs, session = self.fetch(responses, max_pages=5)
        self.assertEqual(len(jobs), phenom.MAX_RESULTS)
        self.assertEqual(len(session.calls), phenom.MAX_PAGES)
        self.assertEqual(session.phenom_pagination["stop_reason"], "maximum_results_reached")
        with self.assertRaisesRegex(AdapterError, "max_pages cannot exceed"):
            phenom.fetch_jobs(FakeSession([]), target(max_pages=phenom.MAX_PAGES + 1))

    def test_network_and_rate_limit_retries_are_bounded_and_respect_retry_after(self):
        for first in (
            requests.Timeout("timed out"),
            FakeResponse("", 429, headers={"Retry-After": "0"}),
        ):
            with self.subTest(first=first):
                jobs, session = self.fetch([first, html_response(page([], total=0))])
                self.assertEqual(jobs, [])
                self.assertEqual(len(session.calls), 2)

    def test_nonretry_client_authentication_and_invalid_endpoint_failures(self):
        cases = [
            (400, "HTTP 400"),
            (401, "authentication required"),
            (403, "blocked"),
            (404, "HTTP 404"),
        ]
        for status, message in cases:
            with self.subTest(status=status):
                session = FakeSession([FakeResponse("", status)])
                with self.assertRaisesRegex(AdapterError, message):
                    phenom.fetch_jobs(session, target())
                self.assertEqual(len(session.calls), 1)

    def test_browser_session_block_consent_and_login_pages_are_rejected(self):
        cases = [
            ("<html>Enable JavaScript required</html>", "browser required"),
            ("<html>Session initialization required</html>", "session required"),
            ("<html>Request blocked by bot challenge</html>", "blocked page"),
            ("<html>Cookie consent required</html>", "consent page"),
            ("<html>Authentication required</html>", "authentication required"),
        ]
        for body, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AdapterError, message):
                    self.fetch([FakeResponse(body)])

    def test_invalid_identifiers_urls_hosts_template_and_variant_reject_before_network(self):
        cases = [
            ({"ref_num": "?bad"}, "ref_num"),
            ({"locale": "english"}, "locale"),
            ({"search_url": "https://other.example.com/us/en/search-results"}, "same-host"),
            ({"search_url": "https://careers.example.com/guessed-api"}, "search-results"),
            ({"endpoint_variant": "widgets_post"}, "endpoint_variant"),
            ({"allowed_apply_hosts": []}, "allowed_apply_hosts"),
            ({"apply_url_template": "https://careers.example.com/apply"}, "job_seq_no"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                session = FakeSession([])
                with self.assertRaisesRegex(AdapterError, message):
                    phenom.fetch_jobs(session, target(**overrides))
                self.assertEqual(session.calls, [])

    def test_wrong_tenant_locale_page_and_base_domain_are_rejected(self):
        cases = [
            ({"ref_num": "OTHER"}, "refNum"),
            ({"locale": "fr_ca"}, "locale"),
            ({"page_name": "home"}, "search-results"),
            ({"base_domain": "https://other.example.com"}, "baseDomain"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(AdapterError, message):
                    self.fetch([html_response(page([], total=0), **kwargs)])

    def test_observed_candidate_field_variants_share_one_contract(self):
        examples = [
            job(businessSegment="Electrification"),
            job(department="DEP Parts Centers", workLocation="Remote or Hybrid"),
            job(category="Financial and Business Operations"),
        ]
        for item in examples:
            with self.subTest(title=item["title"]):
                jobs, _ = self.fetch([html_response(page([item]))])
                self.assertEqual(len(jobs), 1)


class PhenomValidationAndRegistryTests(unittest.TestCase):
    def test_scanner_and_validator_register_production_adapter(self):
        self.assertIs(scanner.ADAPTERS["phenom"], phenom.fetch_jobs)
        self.assertIs(validator.ADAPTERS["phenom"], phenom.fetch_jobs)

    def test_validator_reports_samples_pagination_evidence_and_identifiers(self):
        session = FakeSession([html_response(page([job()], total=1))])
        with patch.object(phenom.time, "sleep", return_value=None):
            result = validator.validate_company(
                registry_entry(max_pages=1),
                session_factory=lambda: session,
                sleep=lambda _: None,
            )
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertTrue(result["safe_to_activate"])
        self.assertEqual(result["fingerprint_evidence"], "official page embedded Phenom contract")
        self.assertEqual(result["request_method"], "GET")
        self.assertEqual(result["configured_identifiers"]["ref_num"], "EXAMPLE")
        self.assertEqual(result["pages_requested"], 1)
        self.assertEqual(result["total_available_jobs"], 1)
        self.assertEqual(result["sample_job_id"], "phenom:careers.example.com:EXAMPLE:REQ-1")
        self.assertEqual(result["sample_posting_date"], "2026-08-04")

    def test_stale_fingerprint_duplicate_company_and_source_skip_network(self):
        stale = validator.validate_company(
            registry_entry(stale_fingerprint=True, stale_fingerprint_reason="moved to another ATS"),
            session_factory=FailingSessionFactory(),
        )
        company = validator.validate_company(
            registry_entry(),
            duplicate_company_of="Existing Example",
            session_factory=FailingSessionFactory(),
        )
        source = validator.validate_company(
            registry_entry(),
            duplicate_of="Existing source",
            session_factory=FailingSessionFactory(),
        )
        self.assertEqual(stale["validation_status"], validator.STALE_FINGERPRINT)
        self.assertEqual(company["validation_status"], validator.DUPLICATE_COMPANY)
        self.assertEqual(source["validation_status"], validator.DUPLICATE_SOURCE)

    def test_validator_classifies_browser_auth_session_block_malformed_and_invalid(self):
        cases = [
            (FakeResponse("", 401), validator.AUTHENTICATION_REQUIRED),
            (FakeResponse("<html>Enable JavaScript required</html>"), validator.BROWSER_REQUIRED),
            (FakeResponse("<html>Session initialization required</html>"), validator.SESSION_REQUIRED),
            (FakeResponse("<html>Cookie consent required</html>"), validator.BLOCKED),
            (FakeResponse("<html>generic landing page</html>"), validator.MALFORMED_RESPONSE),
            (FakeResponse("", 404), validator.INVALID_CONFIGURATION),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                result = validator.validate_company(
                    registry_entry(max_pages=1),
                    session_factory=lambda response=response: FakeSession([response]),
                    sleep=lambda _: None,
                )
                self.assertEqual(result["validation_status"], expected)
                self.assertFalse(result["safe_to_activate"])

    def test_candidate_registry_membership_order_loader_uniqueness_and_byte_prefix(self):
        _, real_candidates = validator.load_registry(
            "config/target_companies.phenom_candidates.yaml"
        )
        phenom_candidates = [entry for entry in real_candidates if entry["platform"] == "phenom"]
        baseline_text = (
            "target_companies:\n"
            "  - name: Synthetic Greenhouse Baseline\n"
            "    platform: greenhouse\n"
            "    slug: synthetic-greenhouse-baseline\n"
            "  - name: Synthetic Workday Baseline\n"
            "    platform: workday\n"
            "    tenant: synthetic-workday-baseline\n"
            "    wd_subdomain: wd1\n"
            "    site_id: Careers\n"
        )
        self.assertEqual([entry["name"] for entry in phenom_candidates], EXPECTED_COMPANIES)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = root / "baseline.yaml"
            candidate = root / "candidate.yaml"
            baseline.write_text(baseline_text, encoding="utf-8", newline="\n")
            candidate.write_bytes(
                validator.build_candidate_registry_bytes(baseline, phenom_candidates)
            )
            baseline_bytes = baseline.read_bytes()
            candidate_bytes = candidate.read_bytes()
            _, baseline_entries = validator.load_registry(baseline)
            _, entries = validator.load_registry(candidate)
            targets = load_target_companies(str(candidate))

        additions = validator.detect_additions(baseline_entries, entries)
        names = [validator._name_key(entry) for entry in entries]
        sources = [validator.source_key(entry) for entry in entries]
        self.assertEqual(candidate_bytes[: len(baseline_bytes)], baseline_bytes)
        self.assertEqual(entries[: len(baseline_entries)], baseline_entries)
        self.assertEqual([entry["name"] for entry in additions], EXPECTED_COMPANIES)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(sources))
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual([item.name for item in targets[-6:]], EXPECTED_COMPANIES)

    def test_promoted_phenom_companies_appear_exactly_once_in_production(self):
        _, production_entries = validator.load_registry("config/target_companies.yaml")

        for expected_name in EXPECTED_COMPANIES:
            with self.subTest(company=expected_name):
                matches = [
                    entry
                    for entry in production_entries
                    if validator._name_key(entry) == expected_name.casefold()
                ]
                self.assertEqual(len(matches), 1)
                self.assertEqual(str(matches[0].get("platform")).casefold(), "phenom")

    def test_validated_registry_contains_only_safe_companies_when_present(self):
        _, validated_entries = validator.load_registry(
            "config/target_companies.phenom_validated.yaml"
        )
        validated_phenom = []
        for expected_name in EXPECTED_COMPANIES:
            matches = [
                entry
                for entry in validated_entries
                if validator._name_key(entry) == expected_name.casefold()
                and str(entry.get("platform")).casefold() == "phenom"
            ]
            self.assertEqual(len(matches), 1, expected_name)
            validated_phenom.append(matches[0])

        baseline_text = (
            "target_companies:\n"
            "  - name: Synthetic Greenhouse Baseline\n"
            "    platform: greenhouse\n"
            "    slug: synthetic-greenhouse-baseline\n"
            "  - name: Synthetic Workday Baseline\n"
            "    platform: workday\n"
            "    tenant: synthetic-workday-baseline\n"
            "    wd_subdomain: wd1\n"
            "    site_id: Careers\n"
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = root / "baseline.yaml"
            generated = root / "validated.yaml"
            baseline.write_text(baseline_text, encoding="utf-8", newline="\n")
            baseline_bytes = baseline.read_bytes()
            generated.write_bytes(
                validator.build_validated_registry_bytes(baseline, validated_phenom)
            )
            generated_bytes = generated.read_bytes()
            _, baseline_entries = validator.load_registry(baseline)
            _, generated_entries = validator.load_registry(generated)
            loaded_targets = load_target_companies(str(generated))

        additions = validator.detect_additions(baseline_entries, generated_entries)
        normalized_names = [validator._name_key(entry) for entry in generated_entries]
        normalized_sources = [validator.source_key(entry) for entry in generated_entries]
        self.assertEqual(generated_bytes[: len(baseline_bytes)], baseline_bytes)
        self.assertEqual(generated_entries[: len(baseline_entries)], baseline_entries)
        self.assertEqual(len(additions), 6)
        self.assertEqual([entry["name"] for entry in additions], EXPECTED_COMPANIES)
        self.assertTrue(all(entry["platform"] == "phenom" for entry in additions))
        self.assertEqual(len(normalized_names), len(set(normalized_names)))
        self.assertTrue(all(normalized_sources))
        self.assertEqual(len(normalized_sources), len(set(normalized_sources)))
        self.assertEqual([target.name for target in loaded_targets[-6:]], EXPECTED_COMPANIES)

    def test_candidate_delta_tests_do_not_depend_on_historical_production_count(self):
        baseline = "target_companies:\n  - name: One\n    platform: greenhouse\n    slug: one\n  - name: Two\n    platform: ashby\n    slug: two\n"
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "baseline.yaml"
            path.write_text(baseline, encoding="utf-8")
            baseline_bytes = path.read_bytes()
            _, real = validator.load_registry("config/target_companies.phenom_candidates.yaml")
            additions = [entry for entry in real if entry["platform"] == "phenom"]
            built = validator.build_candidate_registry_bytes(path, additions)
        self.assertTrue(built.startswith(baseline_bytes))
        self.assertEqual(built.count(b"platform: phenom"), 6)

    def test_validation_initializes_no_supabase_sheets_or_sqlite(self):
        from src.integrations.google_sheets_publisher import GoogleSheetsPublisher
        from src.integrations.supabase_job_store import SupabaseJobStore

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current.yaml"
            candidate = root / "candidate.yaml"
            current.write_text("target_companies: []\n", encoding="utf-8")
            candidate.write_text(
                "target_companies:\n"
                "  - name: Example\n"
                "    platform: phenom\n"
                "    careers_url: https://careers.example.com/us/en/\n"
                "    search_url: https://careers.example.com/us/en/search-results\n"
                "    endpoint_variant: embedded_refine_search\n"
                "    ref_num: EXAMPLE\n"
                "    locale: en_us\n"
                "    allowed_apply_hosts: [example.wd1.myworkdayjobs.com]\n"
                "    max_pages: 1\n",
                encoding="utf-8",
            )
            fake = FakeSession([html_response(page([], total=0))])
            with (
                patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite initialized")) as sqlite_init,
                patch.object(SupabaseJobStore, "from_env", side_effect=AssertionError("Supabase initialized")) as supabase_init,
                patch.object(GoogleSheetsPublisher, "from_env", side_effect=AssertionError("Sheets initialized")) as sheets_init,
            ):
                report = validator.validate_registry(
                    current,
                    candidate,
                    only_additions=True,
                    output=root / "report.json",
                    validated_config=root / "validated.yaml",
                    session_factory=lambda: fake,
                    sleep=lambda _: None,
                )
            sqlite_init.assert_not_called()
            supabase_init.assert_not_called()
            sheets_init.assert_not_called()
        self.assertEqual(
            report["summary"]["status_counts"][validator.VALIDATED_ZERO_JOBS], 1
        )


if __name__ == "__main__":
    unittest.main()
