import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from scripts import validate_company_registry as validator
from src.scraper.company_careers.adapters import eightfold
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_code=200,
        *,
        text="",
        content_type="application/json",
        url="https://careers.example.com/api/pcsx/search",
    ):
        self.payload = payload
        self.status_code = status_code
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = {"content-type": content_type} if content_type else {}
        self.url = url
        self.history = []

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        if self.payload is None:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.url = url
        return response


class FailingSessionFactory:
    def __call__(self):
        raise AssertionError("network session initialized")


def target(**overrides):
    values = {
        "name": "Example",
        "platform": "eightfold",
        "careers_url": "https://careers.example.com/careers",
        "endpoint_url": "https://careers.example.com/api/pcsx/search",
        "endpoint_variant": "pcsx",
        "organization_id": "example.com",
        "max_pages": 2,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def registry_entry(**overrides):
    values = {
        "name": "Example",
        "platform": "eightfold",
        "careers_url": "https://careers.example.com/careers",
        "endpoint_url": "https://careers.example.com/api/pcsx/search",
        "endpoint_variant": "pcsx",
        "organization_id": "example.com",
        "max_pages": 2,
        "fingerprint_confidence": "high",
    }
    values.update(overrides)
    return values


def position(index=1, **overrides):
    values = {
        "id": 900000 + index,
        "displayJobId": f"REQ-{index}",
        "atsJobId": f"REQ-{index}",
        "name": f"Software Engineer {index}",
        "locations": ["Calgary, Alberta, Canada", "Edmonton, Alberta, Canada"],
        "standardizedLocations": ["Calgary, AB, CA", "Edmonton, AB, CA"],
        "postedTs": 1785801600,
        "department": "Engineering",
        "workLocationOption": "hybrid",
        "locationFlexibility": None,
        "positionUrl": f"/careers/job/{900000 + index}",
    }
    values.update(overrides)
    return values


def payload(positions, count=None, **overrides):
    positions = list(positions)
    data = {
        "positions": positions,
        "count": len(positions) if count is None else count,
        "filterDef": [],
        "sortBy": "relevance",
        "appliedFilters": {},
    }
    data.update(overrides.pop("data", {}))
    result = {"status": 200, "error": {"message": "", "body": ""}, "data": data}
    result.update(overrides)
    return result


class EightfoldAdapterTests(unittest.TestCase):
    def fetch(self, responses, **target_overrides):
        session = FakeSession(responses)
        with patch.object(eightfold.time, "sleep", return_value=None):
            jobs = eightfold.fetch_jobs(session, target(**target_overrides))
        return jobs, session

    def test_normalizes_explicit_id_apply_url_locations_date_department_and_description(self):
        item = position(
            atsJobId="ATS-42",
            displayJobId="DISPLAY-42",
            locations=["Calgary, AB", "calgary, ab", "Edmonton, AB"],
            jobDescription="<p>Build <b>safe</b> systems.</p><ul><li>Test them</li></ul>",
        )
        jobs, _ = self.fetch([FakeResponse(payload([item]))])
        job = jobs[0]
        self.assertEqual(job.job_id, "eightfold:careers.example.com:ATS-42")
        self.assertEqual(job.source, "eightfold:careers.example.com")
        self.assertEqual(job.url, "https://careers.example.com/careers/job/900001")
        self.assertEqual(job.offices, ["Calgary, AB", "Edmonton, AB"])
        self.assertEqual(job.location, "Calgary, AB | Edmonton, AB | Hybrid")
        self.assertEqual(job.updated_at, "2026-08-04")
        self.assertEqual(job.departments, ["Engineering"])
        self.assertEqual(job.description, "Build safe systems.\nTest them")
        self.assertEqual(job.raw_metadata["external_job_id"], "ATS-42")
        self.assertEqual(job.raw_metadata["position_id"], "900001")

    def test_url_identifier_fallback_and_absolute_official_apply_url(self):
        item = position(
            atsJobId="",
            displayJobId="",
            positionUrl="https://careers.example.com/careers/job/900001",
        )
        jobs, _ = self.fetch([FakeResponse(payload([item]))])
        self.assertEqual(jobs[0].job_id, "eightfold:careers.example.com:900001")
        self.assertEqual(jobs[0].url, item["positionUrl"])

    def test_primary_and_multiple_location_fallback_remote_and_hybrid_preservation(self):
        remote = position(
            locations=[],
            standardizedLocations=["Canada", "canada", "United States"],
            workLocationOption="remote_global",
            locationFlexibility="hybrid",
        )
        jobs, _ = self.fetch([FakeResponse(payload([remote]))])
        self.assertEqual(jobs[0].offices, ["Canada", "United States"])
        self.assertEqual(jobs[0].location, "Canada | United States | Remote Global / Hybrid")

    def test_missing_optional_metadata_does_not_discard_job(self):
        item = position(
            locations=[],
            standardizedLocations=[],
            postedTs=None,
            department=None,
            workLocationOption=None,
            locationFlexibility=None,
        )
        jobs, _ = self.fetch([FakeResponse(payload([item]))])
        self.assertEqual(jobs[0].location, "")
        self.assertEqual(jobs[0].updated_at, "")
        self.assertEqual(jobs[0].departments, [])
        self.assertIsNone(jobs[0].description)

    def test_malformed_individual_position_is_skipped_with_diagnostic(self):
        session = FakeSession([FakeResponse(payload([position(1), position(2, name="")], count=2))])
        with patch.object(eightfold.time, "sleep", return_value=None):
            jobs = eightfold.fetch_jobs(session, target())
        self.assertEqual([job.title for job in jobs], ["Software Engineer 1"])
        self.assertEqual(len(session.eightfold_pagination["diagnostics"]), 1)

    def test_malformed_entire_success_response_is_rejected(self):
        invalid_payloads = [
            [],
            {"status": 200},
            {"status": 200, "data": {"positions": {}, "count": 0}},
            {"status": 200, "data": {"positions": [], "count": -1}},
            {"status": 500, "data": {"positions": [], "count": 0}},
        ]
        for value in invalid_payloads:
            with self.subTest(value=value):
                with self.assertRaises(AdapterError):
                    self.fetch([FakeResponse(value)])

    def test_valid_zero_job_response(self):
        jobs, session = self.fetch([FakeResponse(payload([], count=0))])
        self.assertEqual(jobs, [])
        self.assertEqual(session.eightfold_pagination["stop_reason"], "validated_zero_jobs")

    def test_offset_pagination_is_deterministic(self):
        first = [position(index) for index in range(1, 11)]
        second = [position(index) for index in range(11, 16)]
        jobs, session = self.fetch(
            [FakeResponse(payload(first, count=15)), FakeResponse(payload(second, count=15))]
        )
        self.assertEqual(len(jobs), 15)
        self.assertEqual([call[1]["params"]["start"] for call in session.calls], ["0", "10"])
        self.assertEqual(session.eightfold_pagination["offsets"], [0, 10])
        self.assertEqual(session.eightfold_pagination["stop_reason"], "total_jobs_reached")

    def test_repeated_page_detection(self):
        page = [position(index) for index in range(1, 11)]
        jobs, session = self.fetch(
            [FakeResponse(payload(page, count=30)), FakeResponse(payload(page, count=30))]
        )
        self.assertEqual(len(jobs), 10)
        self.assertEqual(session.eightfold_pagination["stop_reason"], "repeated_page")

    def test_no_new_stable_id_stop(self):
        first = [position(index) for index in range(1, 11)]
        second = list(reversed(first))
        jobs, session = self.fetch(
            [FakeResponse(payload(first, count=30)), FakeResponse(payload(second, count=30))]
        )
        self.assertEqual(len(jobs), 10)
        self.assertEqual(session.eightfold_pagination["stop_reason"], "no_new_stable_ids")

    def test_maximum_pages_and_results_are_enforced(self):
        pages = [
            FakeResponse(payload([position(page * 10 + index) for index in range(1, 11)], count=100))
            for page in range(5)
        ]
        jobs, session = self.fetch(pages, max_pages=5)
        self.assertEqual(len(jobs), eightfold.MAX_RESULTS)
        self.assertEqual(len(session.calls), eightfold.MAX_PAGES)
        self.assertEqual(session.eightfold_pagination["stop_reason"], "maximum_results_reached")

    def test_maximum_page_configuration_is_rejected_without_network(self):
        session = FakeSession([])
        with self.assertRaisesRegex(AdapterError, "max_pages cannot exceed"):
            eightfold.fetch_jobs(session, target(max_pages=eightfold.MAX_PAGES + 1))
        self.assertEqual(session.calls, [])

    def test_retryable_network_and_rate_limit_failures_are_bounded(self):
        for first in (requests.Timeout("timed out"), FakeResponse(None, 429)):
            with self.subTest(first=first):
                jobs, session = self.fetch([first, FakeResponse(payload([], count=0))])
                self.assertEqual(jobs, [])
                self.assertEqual(len(session.calls), 2)

    def test_non_retryable_client_failure_is_not_retried(self):
        session = FakeSession([FakeResponse(None, 400)])
        with self.assertRaisesRegex(AdapterError, "HTTP 400"):
            eightfold.fetch_jobs(session, target())
        self.assertEqual(len(session.calls), 1)

    def test_authentication_browser_session_and_blocked_responses_are_rejected(self):
        cases = [
            (FakeResponse(None, 401), "authentication required"),
            (FakeResponse(None, text="<html>Enable JavaScript required</html>", content_type="text/html"), "browser required"),
            (FakeResponse(None, text="<html>Session initialization required</html>", content_type="text/html"), "session required"),
            (FakeResponse(None, text="<html>Consent required</html>", content_type="text/html"), "blocked or consent"),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AdapterError, message):
                    self.fetch([response])

    def test_invalid_organization_endpoint_and_unsupported_legacy_variant(self):
        cases = [
            ({"organization_id": "not a domain"}, "organization_id"),
            ({"endpoint_url": "https://other.example.com/api/pcsx/search"}, "same-host"),
            ({"endpoint_url": "https://careers.example.com/guessed/api"}, "endpoint_url"),
            ({"endpoint_variant": "smartapply_embedded"}, "endpoint_variant"),
        ]
        for overrides, message in cases:
            session = FakeSession([])
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(AdapterError, message):
                    eightfold.fetch_jobs(session, target(**overrides))
                self.assertEqual(session.calls, [])


class EightfoldValidationTests(unittest.TestCase):
    def test_duplicate_company_and_source_are_rejected_without_network(self):
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
        self.assertEqual(company["validation_status"], validator.DUPLICATE_COMPANY)
        self.assertEqual(source["validation_status"], validator.DUPLICATE_SOURCE)

    def test_john_deere_remains_successfactors_in_production_registry(self):
        _, current = validator.load_registry("config/target_companies.yaml")
        john_deere = next(entry for entry in current if entry["name"] == "John Deere")
        self.assertEqual(john_deere["platform"], "sap_successfactors")
        self.assertEqual(validator.source_key(john_deere), "sap_successfactors:jobs.deere.com")

    @patch.object(eightfold.time, "sleep", return_value=None)
    def test_validator_reports_samples_pagination_and_confidence(self, _sleep):
        session = FakeSession([FakeResponse(payload([position(1)], count=1))])
        result = validator.validate_company(
            registry_entry(max_pages=1),
            session_factory=lambda: session,
            sleep=lambda _: None,
        )
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertTrue(result["safe_to_activate"])
        self.assertEqual(result["fingerprint_confidence"], "high")
        self.assertEqual(result["endpoint_variant"], "pcsx")
        self.assertEqual(result["request_method"], "GET")
        self.assertEqual(result["pages_requested"], 1)
        self.assertEqual(result["total_available_jobs"], 1)
        self.assertEqual(result["sample_job_id"], "eightfold:careers.example.com:REQ-1")
        self.assertEqual(result["sample_location"], "Calgary, Alberta, Canada | Edmonton, Alberta, Canada | Hybrid")
        self.assertEqual(result["sample_posting_date"], "2026-08-04")

    def test_validator_classifies_auth_browser_session_blocked_and_malformed(self):
        cases = [
            (FakeResponse(None, 401), validator.AUTHENTICATION_REQUIRED),
            (FakeResponse(None, text="<html>Enable JavaScript required</html>", content_type="text/html"), validator.BROWSER_REQUIRED),
            (FakeResponse(None, text="<html>Session initialization required</html>", content_type="text/html"), validator.SESSION_REQUIRED),
            (FakeResponse(None, text="<html>Consent required</html>", content_type="text/html"), validator.BLOCKED),
            (FakeResponse({"status": 200, "data": {"positions": "bad", "count": 0}}), validator.MALFORMED_RESPONSE),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                with patch.object(eightfold.time, "sleep", return_value=None):
                    result = validator.validate_company(
                        registry_entry(max_pages=1),
                        session_factory=lambda response=response: FakeSession([response]),
                        sleep=lambda _: None,
                    )
                self.assertEqual(result["validation_status"], expected)
                self.assertFalse(result["safe_to_activate"])

    def test_registry_loader_candidate_membership_order_uniqueness_and_byte_prefix(self):
        expected = ["Microsoft", "Qualcomm", "Micron", "Morgan Stanley", "Eaton"]
        _, real_candidates = validator.load_registry(
            "config/target_companies.eightfold_candidates.yaml"
        )
        eightfold_candidates = [
            entry for entry in real_candidates if entry["platform"] == "eightfold"
        ]
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

        self.assertEqual([entry["name"] for entry in eightfold_candidates], expected)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "baseline.yaml"
            candidate_path = root / "candidate.yaml"
            baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
            candidate_path.write_bytes(
                validator.build_validated_registry_bytes(
                    baseline_path,
                    eightfold_candidates,
                )
            )
            baseline_bytes = baseline_path.read_bytes()
            candidate_bytes = candidate_path.read_bytes()
            _, baseline = validator.load_registry(baseline_path)
            _, candidates = validator.load_registry(candidate_path)
            additions = validator.detect_additions(baseline, candidates)
            targets = load_target_companies(str(candidate_path))

        normalized_names = [validator._name_key(entry) for entry in candidates]
        source_keys = [validator.source_key(entry) for entry in candidates]

        self.assertEqual(candidate_bytes[: len(baseline_bytes)], baseline_bytes)
        self.assertEqual(candidates[: len(baseline)], baseline)
        self.assertEqual(
            [entry["name"] for entry in baseline],
            ["Synthetic Greenhouse Baseline", "Synthetic Workday Baseline"],
        )
        self.assertEqual(len(additions), 5)
        self.assertEqual([entry["name"] for entry in additions], expected)
        self.assertTrue(all(entry["platform"] == "eightfold" for entry in additions))
        self.assertEqual(len(normalized_names), len(set(normalized_names)))
        self.assertTrue(all(source_keys))
        self.assertEqual(len(source_keys), len(set(source_keys)))
        self.assertEqual(
            [item.name for item in targets[: len(baseline)]],
            ["Synthetic Greenhouse Baseline", "Synthetic Workday Baseline"],
        )
        self.assertEqual([item.name for item in targets[-len(expected) :]], expected)
        self.assertTrue(
            all(item.platform == "eightfold" for item in targets[-len(expected) :])
        )

    def test_validated_registry_builder_adds_only_safe_entries_deterministically(self):
        baseline = (
            "target_companies:\n"
            "  - name: Existing\n"
            "    platform: greenhouse\n"
            "    slug: existing\n"
        )
        safe = [registry_entry(name="Safe")]
        unsafe = registry_entry(name="Unsafe", organization_id="unsafe.example")
        with TemporaryDirectory() as temp_dir:
            current = Path(temp_dir) / "current.yaml"
            current.write_text(baseline, encoding="utf-8", newline="\n")
            first = validator.build_validated_registry_bytes(current, safe)
            second = validator.build_validated_registry_bytes(current, safe)
            output = Path(temp_dir) / "validated.yaml"
            output.write_bytes(first)
            _, entries = validator.load_registry(output)
        self.assertEqual(first, second)
        self.assertEqual([entry["name"] for entry in entries], ["Existing", "Safe"])
        self.assertNotIn(unsafe["name"], [entry["name"] for entry in entries])

    @patch.object(eightfold.time, "sleep", return_value=None)
    def test_validation_initializes_no_supabase_sheets_or_sqlite(self, _adapter_sleep):
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
                "    platform: eightfold\n"
                "    careers_url: https://careers.example.com/careers\n"
                "    endpoint_url: https://careers.example.com/api/pcsx/search\n"
                "    endpoint_variant: pcsx\n"
                "    organization_id: example.com\n"
                "    max_pages: 1\n",
                encoding="utf-8",
            )
            fake = FakeSession([FakeResponse(payload([], count=0))])
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
        self.assertEqual(report["summary"]["status_counts"][validator.VALIDATED_ZERO_JOBS], 1)


if __name__ == "__main__":
    unittest.main()
