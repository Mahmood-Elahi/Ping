from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import yaml

from tests.company_careers_reliability_assertions import (
    assert_historical_prefix,
    assert_historical_target_prefix,
)
from tests.text_bytes import canonical_text_bytes

from scripts import validate_company_registry as registry_validator
from scripts import validate_tsmc_source_recovery as recovery
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import avature, sap_successfactors
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]


def avature_html(job_id: str = "101", host: str = "careers.tsmc.com") -> str:
    return f'''<html><body><section data-total-job-results="1">
      <article class="article article--result">
        <h3><a href="https://{host}/en_US/careers/JobDetail/Engineer/{job_id}">Engineer</a></h3>
        <span class="list-item-location">Taiwan</span>
        <span class="list-item-posted">Posted: Aug 04, 2026</span>
      </article></section></body></html>'''


def avature_page(*ids: str) -> str:
    articles = "".join(
        f'''<article class="article article--result"><h3><a href="https://careers.tsmc.com/en_US/careers/JobDetail/Role/{value}">Role {value}</a></h3><span class="list-item-location">Taiwan</span></article>'''
        for value in ids
    )
    return f"<html><body>{articles}</body></html>"


def sap_table_html(job_id: str = "1001", requisition: str = "REQ-1") -> str:
    return f'''<!doctype html><html><body>
      <span class="paginationLabel">Results 1 – 1 of 1</span>
      <table id="searchresults"><thead><tr>
        <th id="hdrTitle">Title</th><th id="hdrRequisition">Requisition ID</th>
        <th id="hdrLocation">Location</th><th id="hdrDate">Posting Date</th>
      </tr></thead><tbody><tr class="data-row">
        <td headers="hdrTitle"><a class="jobTitle-link" href="/job/Test/{job_id}/">Engineer</a></td>
        <td headers="hdrRequisition">{requisition}</td><td headers="hdrLocation">Phoenix, AZ</td>
        <td headers="hdrDate">Aug 4, 2026</td>
      </tr></tbody></table></body></html>'''


def response(url: str, text: str, status: int = 200, **kwargs):
    return recovery.FixtureResponse(url, status, text, kwargs.get("headers"), kwargs.get("history"))


def current_target(**overrides) -> CompanyTarget:
    values = dict(recovery.current_tsmc_entry())
    values.update(overrides)
    return CompanyTarget.from_dict(values)


class TSMCSourceRecoveryTests(unittest.TestCase):
    def test_01_current_production_contains_exactly_one_tsmc(self):
        targets = load_target_companies(str(recovery.PRODUCTION))
        self.assertEqual(sum(target.name == "TSMC" for target in targets), 1)

    def test_02_historical_tsmc_source_identity_is_recorded(self):
        row = recovery._csv_row({
            "current_production_configuration": recovery.current_tsmc_entry(),
            "historical_validation_evidence": recovery.historical_evidence(),
            "current_source_discovery": {"current_official_careers_url": recovery.OFFICIAL_CAREERS_URL, "official_search_url": recovery.OLD_AVATURE_URL, "detected_platform": "avature", "platform_variant": avature.PORTAL_HTML},
            "endpoint_evidence": {"url": recovery.OLD_AVATURE_URL, "source_identity": "avature:careers.tsmc.com:6"},
            "browser_session_authentication_checks": {}, "pagination_checks": {},
            "redirect_chain": [], "warnings": [], "diagnostics": [], "final_status": recovery.TEMPORARY_FAILURE,
            "safe_to_repair": False, "recommended_action": "temporary_failure", "evidence_timestamp": recovery.EVIDENCE_TIMESTAMP,
        })
        self.assertEqual(row["current_production_source_identity"], "avature:careers.tsmc.com:6")
        self.assertEqual(recovery.historical_evidence()["status"], recovery.VALIDATED_WITH_JOBS)

    def test_03_http_403_is_blocked_evidence(self):
        self.assertEqual(recovery.classify_http_response(response("https://x", "denied", 403)), recovery.BLOCKED)

    def test_04_one_403_does_not_remove_source(self):
        _, validated = recovery.build_artifact_bytes(False)
        self.assertEqual(validated, recovery.PRODUCTION.read_bytes())
        self.assertEqual(recovery.current_tsmc_entry()["platform"], "avature")

    def test_05_current_official_url_recovery(self):
        self.assertEqual(recovery.OFFICIAL_CAREERS_URL, recovery.current_tsmc_entry()["careers_url"])
        self.assertTrue(recovery.RECOVERED_SEARCH_URL.startswith("https://ro.careers.tsmc.com/"))

    def test_06_redirect_chain_recording(self):
        first = response("https://one", "", 301)
        first.headers["location"] = "https://two"
        final = response("https://two", "ok", history=[first])
        self.assertEqual(recovery.redirect_chain(final), [
            {"status": 301, "url": "https://one", "location": "https://two"},
            {"status": 200, "url": "https://two"},
        ])

    def test_07_existing_adapter_detection(self):
        self.assertEqual(recovery.detect_platform("SAP as service provider", recovery.RECOVERED_SEARCH_URL), "sap_successfactors")
        self.assertEqual(recovery.detect_platform("Avature SearchJobs", recovery.OLD_AVATURE_URL), "avature")

    def test_08_existing_adapter_compatibility_uses_production_parser(self):
        self.assertIs(scanner.ADAPTERS["sap_successfactors"], sap_successfactors.fetch_jobs)
        session = recovery.BoundedSession(recovery.FixtureTransport([response(recovery.RECOVERED_SEARCH_URL, sap_table_html())]))
        result = recovery.validate_adapter_candidate(session, recovery.recovered_entry())
        self.assertEqual(result["status"], recovery.VALIDATED_WITH_JOBS)

    def test_09_avature_public_endpoint_normalization(self):
        value = recovery.normalize_avature_search_url(recovery.OLD_AVATURE_URL + "/?jobId=1#x")
        self.assertEqual(value, recovery.OLD_AVATURE_URL)

    def test_10_avature_replacement_endpoint_validation(self):
        session = recovery.BoundedSession(recovery.FixtureTransport([response(recovery.OLD_AVATURE_URL, avature_html())]))
        result = recovery.validate_adapter_candidate(session, recovery.current_tsmc_entry())
        self.assertEqual(result["status"], recovery.VALIDATED_WITH_JOBS)
        self.assertTrue(result["checks"]["stable_ids_valid"])

    def test_11_session_dependent_avature_variant_rejected(self):
        body = "<html><title>Session required</title><body>Session required</body></html>"
        result = recovery.validate_adapter_candidate(
            recovery.BoundedSession(recovery.FixtureTransport([response(recovery.OLD_AVATURE_URL, body)])),
            recovery.current_tsmc_entry(),
        )
        self.assertEqual(result["status"], recovery.SESSION_REQUIRED)

    def test_12_browser_required_avature_variant_rejected(self):
        body = "<html><body>Enable JavaScript to continue</body></html>"
        result = recovery.validate_adapter_candidate(
            recovery.BoundedSession(recovery.FixtureTransport([response(recovery.OLD_AVATURE_URL, body)])),
            recovery.current_tsmc_entry(),
        )
        self.assertEqual(result["status"], recovery.BROWSER_REQUIRED)

    def test_13_authentication_required_variant_rejected(self):
        body = "<html><title>Login</title></html>"
        result = recovery.validate_adapter_candidate(
            recovery.BoundedSession(recovery.FixtureTransport([response(recovery.OLD_AVATURE_URL, body)])),
            recovery.current_tsmc_entry(),
        )
        self.assertEqual(result["status"], recovery.AUTHENTICATION_REQUIRED)

    def test_14_malformed_http_200_rejected(self):
        malformed = response("https://x", "", headers={"content-type": "text/html"})
        self.assertEqual(recovery.classify_http_response(malformed), recovery.MALFORMED_RESPONSE)

    def test_15_stable_job_ids_required(self):
        jobs = [SimpleNamespace(job_id="", url="https://careers.tsmc.com/job/1")]
        self.assertFalse(recovery._job_checks(jobs, "careers.tsmc.com")["stable_ids_valid"])

    def test_16_official_apply_urls_required(self):
        jobs = [SimpleNamespace(job_id="one", url="http://careers.tsmc.com/job/1")]
        self.assertFalse(recovery._job_checks(jobs, "careers.tsmc.com")["apply_urls_valid"])

    def test_17_invalid_apply_host_rejected(self):
        session = recovery.BoundedSession(recovery.FixtureTransport([response(recovery.OLD_AVATURE_URL, avature_html(host="evil.test"))]))
        result = recovery.validate_adapter_candidate(session, recovery.current_tsmc_entry())
        self.assertFalse(result["safe_to_repair"])

    @patch.object(avature.time, "sleep", return_value=None)
    def test_18_repeated_page_termination(self, _sleep):
        pages = [response(recovery.OLD_AVATURE_URL, avature_page("1", "2")), response(recovery.OLD_AVATURE_URL, avature_page("1", "2"))]
        transport = recovery.FixtureTransport(pages)
        target = current_target(page_size=2)
        jobs = avature.fetch_jobs(transport, target)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(transport.avature_pagination["stop_reason"], "repeated_page")

    @patch.object(avature.time, "sleep", return_value=None)
    def test_19_no_new_id_termination(self, _sleep):
        pages = [response(recovery.OLD_AVATURE_URL, avature_page("1", "2")), response(recovery.OLD_AVATURE_URL, avature_page("1", "1"))]
        transport = recovery.FixtureTransport(pages)
        avature.fetch_jobs(transport, current_target(page_size=2))
        self.assertEqual(transport.avature_pagination["stop_reason"], "no_new_stable_ids")

    def test_20_two_page_validation_limit(self):
        self.assertEqual(recovery.MAX_PAGES, 2)
        self.assertLessEqual(int(recovery.current_tsmc_entry()["max_pages"]), recovery.MAX_PAGES)

    def test_21_twenty_job_validation_limit(self):
        jobs = [SimpleNamespace(job_id=str(i), url=f"https://careers.tsmc.com/job/{i}") for i in range(25)]
        self.assertEqual(recovery._job_checks(jobs, "careers.tsmc.com")["normalized_jobs_inspected"], 20)

    def test_22_bounded_retry_enforcement(self):
        transport = recovery.FixtureTransport([requests.ConnectionError("one"), requests.ConnectionError("two")])
        session = recovery.BoundedSession(transport)
        with self.assertRaises(requests.ConnectionError):
            session.get("https://x")
        self.assertEqual(transport.calls, 2)

    def test_23_response_byte_limit(self):
        session = recovery.BoundedSession(recovery.FixtureTransport([response("https://x", "12345")]), max_response_bytes=4)
        with self.assertRaisesRegex(Exception, "byte limit"):
            session.get("https://x")

    def test_24_request_cache_behavior(self):
        transport = recovery.FixtureTransport([response("https://x", "ok")])
        session = recovery.BoundedSession(transport)
        self.assertIs(session.get("https://x"), session.get("https://x"))
        self.assertEqual(transport.calls, 1)
        self.assertTrue(session.request_attempts[-1]["cached"])

    def test_25_no_company_name_branch_in_adapters(self):
        source = Path(avature.__file__).read_text(encoding="utf-8") + Path(sap_successfactors.__file__).read_text(encoding="utf-8")
        self.assertNotIn('target.name == "TSMC"', source)
        self.assertNotIn("target.name == 'TSMC'", source)

    def test_26_existing_avature_sources_remain_compatible(self):
        source_before = subprocess.run(
            ["git", "show", "HEAD:src/scraper/company_careers/adapters/avature.py"],
            cwd=ROOT, check=True, capture_output=True,
        ).stdout
        self.assertEqual(
            canonical_text_bytes(Path(avature.__file__).read_bytes()),
            canonical_text_bytes(source_before),
        )

    def test_27_reclassification_preserves_source_identity_rules(self):
        self.assertEqual(registry_validator.source_key(recovery.recovered_entry()), "sap_successfactors:ro.careers.tsmc.com")
        self.assertNotEqual(registry_validator.source_key(recovery.recovered_entry()), registry_validator.source_key(recovery.current_tsmc_entry()))

    def test_28_candidate_registry_loader_compatibility(self):
        _, candidate = registry_validator.load_registry(recovery.CANDIDATE)
        _, production = registry_validator.load_registry(recovery.PRODUCTION)
        self.assertEqual(len(load_target_companies(str(recovery.CANDIDATE))), 133)
        self.assertGreaterEqual(len(production), len(candidate))
        assert_historical_prefix(self, production, candidate)

    def test_29_validated_registry_loader_compatibility(self):
        self.assertEqual(len(load_target_companies(str(recovery.VALIDATED))), 133)

    def test_30_no_duplicate_normalized_company(self):
        for path in (recovery.CANDIDATE, recovery.VALIDATED):
            _, entries = registry_validator.load_registry(path)
            keys = [registry_validator._name_key(entry) for entry in entries]
            self.assertEqual(len(keys), len(set(keys)))

    def test_31_no_duplicate_normalized_source(self):
        for path in (recovery.CANDIDATE, recovery.VALIDATED):
            _, entries = registry_validator.load_registry(path)
            keys = [registry_validator.source_key(entry) for entry in entries]
            keys = [key for key in keys if key]
            self.assertEqual(len(keys), len(set(keys)))

    def test_32_tsmc_registry_position_preserved_on_replacement(self):
        _, original = registry_validator.load_registry(recovery.PRODUCTION)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replacement.yaml"
            path.write_bytes(recovery.replace_tsmc_bytes(recovery.PRODUCTION, recovery.recovered_entry()))
            _, replaced = registry_validator.load_registry(path)
        original_index = next(i for i, item in enumerate(original) if item["name"] == "TSMC")
        replaced_index = next(i for i, item in enumerate(replaced) if item["name"] == "TSMC")
        self.assertEqual(original_index, replaced_index)

    def test_33_non_tsmc_production_bytes_preserved(self):
        replacement = recovery.replace_tsmc_bytes(recovery.PRODUCTION, recovery.recovered_entry())
        original = recovery.PRODUCTION.read_bytes()
        offsets = recovery._entry_offsets(original)
        _, entries = registry_validator.load_registry(recovery.PRODUCTION)
        index = next(i for i, item in enumerate(entries) if item["name"] == "TSMC")
        start, end = offsets[index], offsets[index + 1]
        replacement_offsets = recovery._entry_offsets(replacement)
        replacement_end = replacement_offsets[index + 1]
        self.assertEqual(original[:start], replacement[:start])
        self.assertEqual(original[end:], replacement[replacement_end:])

    def test_34_unsafe_outcome_produces_no_production_ready_replacement(self):
        candidate, validated = recovery.build_artifact_bytes(False)
        self.assertIn(b"operation: no_promotable_registry_change", candidate)
        self.assertEqual(validated, recovery.PRODUCTION.read_bytes())

    def test_35_production_registry_remains_unchanged(self):
        _, historical = registry_validator.load_registry(recovery.VALIDATED)
        _, production = registry_validator.load_registry(recovery.PRODUCTION)
        historical_tsmc = next(item for item in historical if item["name"] == "TSMC")
        production_tsmc = [item for item in production if item["name"] == "TSMC"]

        self.assertGreaterEqual(len(production), len(historical))
        assert_historical_prefix(self, production, historical)
        self.assertEqual(production_tsmc, [historical_tsmc])
        self.assertEqual(
            registry_validator.source_key(production_tsmc[0]),
            "avature:careers.tsmc.com:6",
        )

    def test_36_parent_mappings_remain_unchanged(self):
        data = yaml.safe_load(recovery.PARENT_MAPPINGS.read_text(encoding="utf-8"))
        enabled = [row for row in data["company_parent_source_mappings"] if row["enabled"]]
        self.assertEqual(len(enabled), 5)

    def test_37_no_supabase_initialization(self):
        self._assert_no_import("supabase")

    def test_38_no_google_sheets_initialization(self):
        self._assert_no_import("gspread")
        self._assert_no_import("google_sheets")

    def test_39_no_sqlite_initialization(self):
        self._assert_no_import("sqlite3")

    def test_40_no_dashboard_publication(self):
        source = Path(recovery.__file__).read_text(encoding="utf-8")
        self.assertNotIn("update_student_dashboard", source)
        self.assertNotIn("CompanyCareersScanner(", source)

    def test_41_no_browser_automation(self):
        imports = self._imports()
        self.assertFalse(any(name in module for module in imports for name in ("playwright", "selenium", "chromium")))

    def test_42_existing_full_suite_safety_contract(self):
        self.assertIs(scanner.ADAPTERS["avature"], avature.fetch_jobs)
        historical = load_target_companies(str(recovery.VALIDATED))
        production = load_target_companies()
        assert_historical_target_prefix(self, production, historical)
        self.assertGreaterEqual(len(production), 139)
        historical_tsmc = next(target for target in historical if target.name == "TSMC")
        self.assertEqual([target for target in production if target.name == "TSMC"], [historical_tsmc])
        self.assertEqual(historical_tsmc.platform, "avature")
        self.assertEqual(historical_tsmc.tenant, "careers.tsmc.com")
        self.assertEqual(historical_tsmc.board_id, "6")
        self.assertEqual(historical_tsmc.search_url, recovery.OLD_AVATURE_URL)
        self.assertEqual(historical_tsmc.endpoint_variant, avature.PORTAL_HTML)

    @staticmethod
    def _imports():
        tree = ast.parse(Path(recovery.__file__).read_text(encoding="utf-8"))
        result = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                result.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                result.append(node.module or "")
        return result

    def _assert_no_import(self, forbidden: str):
        self.assertFalse(any(forbidden in module for module in self._imports()), self._imports())


if __name__ == "__main__":
    unittest.main()
