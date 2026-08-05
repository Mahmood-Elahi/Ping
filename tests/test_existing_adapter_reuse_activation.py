from __future__ import annotations

import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import validate_adapter_reuse_candidates as reuse
from scripts import validate_company_registry as validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.models import AdapterError, JobRecord
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.adapter_reuse_candidates.yaml"
VALIDATED = ROOT / "config" / "target_companies.adapter_reuse_validated.yaml"


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.url = "https://example.test/api"
        self.history = []

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network call")
        response = self.responses.pop(0)
        response.url = url
        return response

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)


class FailingSessionFactory:
    def __call__(self):
        raise AssertionError("Network session must not be initialized")


def target(**overrides):
    entry = {"name": "Acme", "platform": "greenhouse", "slug": "acme"}
    entry.update(overrides)
    return entry


def job(index=1, *, url="https://jobs.example.test/apply/1", job_id=None):
    return JobRecord(
        job_id=job_id if job_id is not None else f"job-{index}",
        title=f"Engineer {index}",
        company="Acme",
        location="Remote",
        url=url,
        updated_at="2026-08-04",
        source="greenhouse:acme",
    )


def adapter_returning(jobs, payload=None):
    def fetch(session, _target, timeout):
        session.get("https://example.test/api", timeout=timeout)
        return list(jobs)

    return fetch, FakeSession([FakeResponse(payload if payload is not None else {"jobs": []})])


class AdapterReuseRegistryTests(unittest.TestCase):
    def test_01_exact_investigation_set(self):
        self.assertEqual(
            reuse.EXPECTED_COMPANIES,
            [
                "Samsung Electronics", "Visa", "Plaid", "Kraken",
                "Hudson River Trading", "Rockwell Automation", "Phoenix Contact",
                "GE Vernova", "Valeo", "PTC", "Agility Robotics", "TC Energy",
                "ConocoPhillips", "Ovintiv", "Keyera", "Sierra Space", "ispace",
            ],
        )

    def test_02_research_input_reconciles_to_exact_set(self):
        self.assertEqual(set(reuse._research_candidates()), set(reuse.EXPECTED_COMPANIES))
        reuse.reconcile_candidates()

    def test_03_candidate_registry_preserves_production_bytes_as_prefix(self):
        self.assertTrue(CANDIDATES.read_bytes().startswith(PRODUCTION.read_bytes()))

    def test_04_production_order_is_unchanged(self):
        _, production = validator.load_registry(PRODUCTION)
        _, candidates = validator.load_registry(CANDIDATES)
        self.assertEqual(candidates[: len(production)], production)

    def test_05_candidate_order_follows_workbook(self):
        _, production = validator.load_registry(PRODUCTION)
        _, candidates = validator.load_registry(CANDIDATES)
        additions = validator.detect_additions(production, candidates)
        self.assertEqual([entry["name"] for entry in additions], reuse.EXPECTED_COMPANIES)
        self.assertEqual(
            [entry["original_workbook_rank"] for entry in additions],
            sorted(entry["original_workbook_rank"] for entry in additions),
        )

    def test_06_candidate_registry_is_loader_compatible(self):
        self.assertEqual(len(load_target_companies(str(CANDIDATES))), 92)

    def test_07_validated_registry_contains_only_safe_candidates(self):
        _, production = validator.load_registry(PRODUCTION)
        _, validated = validator.load_registry(VALIDATED)
        additions = validator.detect_additions(production, validated)
        self.assertTrue({entry["name"] for entry in additions} <= set(reuse.EXPECTED_COMPANIES))

    def test_08_candidate_delta_uses_a_synthetic_baseline(self):
        baseline = b"target_companies:\n  - name: Synthetic\n    platform: greenhouse\n    slug: synthetic\n"
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current.yaml"
            candidate = root / "candidate.yaml"
            current.write_bytes(baseline)
            candidate.write_bytes(
                validator.build_candidate_registry_bytes(current, [target(name="Added", slug="added")])
            )
            _, entries = validator.load_registry(candidate)
            self.assertTrue(candidate.read_bytes().startswith(baseline))
        self.assertEqual([entry["name"] for entry in entries], ["Synthetic", "Added"])

    def test_09_duplicate_normalized_company_is_rejected(self):
        duplicates = validator._duplicate_company_owners(
            [{"name": "Existing Company"}],
            [target(name="  existing   company  ")],
        )
        self.assertEqual(duplicates, {0: "Existing Company"})

    def test_10_duplicate_normalized_source_is_rejected(self):
        duplicates = validator._duplicate_owners(
            [target(name="Existing")],
            [target(name="Other", slug=" ACME ")],
        )
        self.assertEqual(duplicates, {0: "Existing"})

    def test_11_candidate_source_identities_are_unique(self):
        identities = [validator.source_key(entry) for entry in reuse.CANDIDATE_ENTRIES]
        self.assertNotIn(None, identities)
        self.assertEqual(len(identities), len(set(identities)))

    def test_12_production_config_is_byte_identical_to_head(self):
        comparison = subprocess.run(
            ["git", "diff", "--exit-code", "--", "config/target_companies.yaml"],
            cwd=ROOT,
            capture_output=True,
        )
        self.assertEqual(comparison.returncode, 0, comparison.stdout.decode(errors="replace"))


class AdapterReuseContractTests(unittest.TestCase):
    def test_13_production_scanner_dispatch_is_used(self):
        for platform in {entry["platform"] for entry in reuse.CANDIDATE_ENTRIES}:
            self.assertIs(validator.ADAPTERS[platform], scanner.ADAPTERS[platform])

    def test_14_no_new_adapter_is_registered(self):
        self.assertEqual(
            {entry["platform"] for entry in reuse.CANDIDATE_ENTRIES},
            {"workday", "greenhouse", "ashby", "lever"},
        )

    def test_15_stable_ids_are_required(self):
        fetch, session = adapter_returning([job(job_id="")], {"jobs": [{}]})
        result = validator.validate_company(
            target(), session_factory=lambda: session, adapters={"greenhouse": fetch}
        )
        self.assertFalse(result["safe_to_activate"])
        self.assertIn("stable job_id", result["error"])

    def test_16_duplicate_stable_ids_are_rejected(self):
        fetch, session = adapter_returning([job(1), job(2, job_id="job-1")], {"jobs": [{}, {}]})
        result = validator.validate_company(
            target(), session_factory=lambda: session, adapters={"greenhouse": fetch}
        )
        self.assertFalse(result["safe_to_activate"])
        self.assertIn("duplicates stable", result["error"])

    def test_17_official_apply_urls_are_required(self):
        fetch, session = adapter_returning([job(url="")], {"jobs": [{}]})
        result = validator.validate_company(
            target(), session_factory=lambda: session, adapters={"greenhouse": fetch}
        )
        self.assertIn("valid apply URL", result["error"])

    def test_18_invalid_apply_hosts_are_rejected(self):
        fetch, session = adapter_returning([job(url="https://evil.test/apply")], {"jobs": [{}]})
        result = validator.validate_company(
            target(allowed_apply_hosts=["jobs.example.test"]),
            session_factory=lambda: session,
            adapters={"greenhouse": fetch},
        )
        self.assertEqual(result["apply_url_result"], "invalid")
        self.assertFalse(result["safe_to_activate"])

    def test_19_allowlisted_apply_hosts_are_accepted(self):
        fetch, session = adapter_returning([job()], {"jobs": [{}]})
        result = validator.validate_company(
            target(allowed_apply_hosts=["jobs.example.test"]),
            session_factory=lambda: session,
            adapters={"greenhouse": fetch},
        )
        self.assertEqual(result["apply_url_result"], "valid")
        self.assertTrue(result["safe_to_activate"])

    def test_20_valid_zero_jobs_requires_conclusive_schema(self):
        good = FakeSession([FakeResponse({"jobs": [], "meta": {"total": 0}})])
        result = validator.validate_company(target(), session_factory=lambda: good)
        self.assertEqual(result["validation_status"], validator.VALIDATED_ZERO_JOBS)
        bad = FakeSession([FakeResponse({})])
        result = validator.validate_company(target(), session_factory=lambda: bad)
        self.assertFalse(result["safe_to_activate"])

    def test_21_malformed_http_200_is_rejected(self):
        session = FakeSession([FakeResponse(ValueError("not json"))])
        result = validator.validate_company(target(), session_factory=lambda: session)
        self.assertFalse(result["safe_to_activate"])
        self.assertEqual(result["http_status"], 200)

    def test_22_authentication_pages_are_rejected(self):
        def auth_adapter(session, _target, timeout):
            session.get("https://example.test/api", timeout=timeout)
            raise AdapterError("authentication required")

        session = FakeSession([FakeResponse({"jobs": []})])
        result = validator.validate_company(
            target(), session_factory=lambda: session, adapters={"greenhouse": auth_adapter}
        )
        self.assertEqual(result["validation_status"], validator.AUTHENTICATION_REQUIRED)

    def test_23_browser_and_session_sources_are_rejected_without_network(self):
        browser = validator.validate_company(
            target(browser_required=True), session_factory=FailingSessionFactory()
        )
        session = validator.validate_company(
            target(session_required=True), session_factory=FailingSessionFactory()
        )
        self.assertEqual(browser["validation_status"], validator.BROWSER_REQUIRED)
        self.assertEqual(session["validation_status"], validator.SESSION_REQUIRED)

    def test_24_blocked_responses_are_rejected(self):
        session = FakeSession([FakeResponse({}, status_code=403)])
        result = validator.validate_company(target(), session_factory=lambda: session)
        self.assertEqual(result["validation_status"], validator.BLOCKED)
        self.assertFalse(result["safe_to_activate"])

    def test_25_pagination_and_normalized_job_limits_are_enforced(self):
        jobs = [job(index, url=f"https://jobs.example.test/apply/{index}") for index in range(25)]
        fetch, session = adapter_returning(jobs, {"jobs": [{} for _ in jobs]})
        result = validator.validate_company(
            target(max_pages=99, allowed_apply_hosts=["jobs.example.test"]),
            session_factory=lambda: session,
            adapters={"greenhouse": fetch},
        )
        self.assertEqual(result["pages_requested"], 1)
        self.assertEqual(result["jobs_returned"], 25)
        self.assertEqual(result["jobs_normalized"], reuse.MAX_JOBS)
        self.assertTrue(all(entry["max_pages"] <= reuse.MAX_PAGES for entry in reuse.CANDIDATE_ENTRIES))

    def test_26_adapter_exceptions_are_contained(self):
        def explode(_session, _target, _timeout):
            raise RuntimeError("boom")

        result = validator.validate_company(
            target(), session_factory=lambda: FakeSession([]), adapters={"greenhouse": explode}
        )
        self.assertFalse(result["safe_to_activate"])
        self.assertIn("RuntimeError", result["error"])


class AdapterReuseIsolationTests(unittest.TestCase):
    def _exercise_mocked_validation(self):
        session = FakeSession([FakeResponse({"jobs": [], "meta": {"total": 0}})])
        return validator.validate_company(target(), session_factory=lambda: session)

    def test_27_no_supabase_initialization(self):
        from src.integrations.supabase_job_store import SupabaseJobStore

        with patch.object(SupabaseJobStore, "from_env", side_effect=AssertionError) as initialize:
            self._exercise_mocked_validation()
        initialize.assert_not_called()

    def test_28_no_google_sheets_initialization(self):
        from src.integrations.google_sheets_publisher import GoogleSheetsPublisher

        with patch.object(GoogleSheetsPublisher, "from_env", side_effect=AssertionError) as initialize:
            self._exercise_mocked_validation()
        initialize.assert_not_called()

    def test_29_no_sqlite_initialization(self):
        with patch.object(sqlite3, "connect", side_effect=AssertionError) as initialize:
            self._exercise_mocked_validation()
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
