import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from scripts import validate_company_registry as validator
from src.scraper.company_careers.adapters import oracle_recruiting
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
CURRENT_REGISTRY = ROOT / "config" / "target_companies.yaml"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.oracle_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.oracle_validated.yaml"


class FakeResponse:
    def __init__(self, payload=None, status_code=200, url="https://oracle.example.test"):
        self.payload = payload
        self.status_code = status_code
        self.url = url
        self.history = []

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected live network request")
        return self.responses.pop(0)


class FailingSessionFactory:
    def __call__(self):
        raise AssertionError("Network session must not be initialized")


def target(**overrides):
    values = {
        "name": "Example",
        "platform": "oracle_recruiting",
        "careers_url": (
            "https://example.fa.us2.oraclecloud.com/"
            "hcmUI/CandidateExperience/en/sites/CX_1/jobs"
        ),
        "endpoint_url": (
            "https://example.fa.us2.oraclecloud.com/"
            "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        ),
        "site_number": "CX_1",
        "locale": "en",
        "max_pages": 2,
    }
    values.update(overrides)
    return CompanyTarget.from_dict(values)


def registry_entry(**overrides):
    result = {
        "name": "Example",
        "platform": "oracle_recruiting",
        "careers_url": (
            "https://example.fa.us2.oraclecloud.com/"
            "hcmUI/CandidateExperience/en/sites/CX_1/jobs"
        ),
        "endpoint_url": (
            "https://example.fa.us2.oraclecloud.com/"
            "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        ),
        "site_number": "CX_1",
        "locale": "en",
        "max_pages": 1,
    }
    result.update(overrides)
    return result


def requisition(job_id="12345", **overrides):
    result = {
        "Id": job_id,
        "Title": "Controls Engineer",
        "PostedDate": "2026-08-03T13:14:15Z",
        "PrimaryLocation": "Calgary, AB, Canada",
        "Department": "Engineering",
        "JobFunction": "Product Development",
        "ShortDescriptionStr": "Build reliable controls.",
        "OrganizationId": 3000000123,
        "secondaryLocations": [
            {
                "Name": "Edmonton, AB, Canada",
                "CountryCode": "CA",
            }
        ],
    }
    result.update(overrides)
    return result


def payload(jobs=None, *, total=None, offset=0, site_number="CX_1", limit=24):
    jobs = list(jobs or [])
    if total is None:
        total = len(jobs)
    return {
        "items": [
            {
                "TotalJobsCount": total,
                "Offset": offset,
                "Limit": limit,
                "SiteNumber": site_number,
                "requisitionList": jobs,
            }
        ],
        "count": 1,
        "hasMore": False,
        "limit": 200,
        "offset": 0,
    }


class OracleRecruitingAdapterTests(unittest.TestCase):
    def test_response_normalization_ids_apply_url_locations_date_and_metadata(self):
        session = FakeSession([FakeResponse(payload([requisition()]))])

        jobs = oracle_recruiting.fetch_jobs(
            session,
            target(
                careers_url=(
                    "https://example.fa.us2.oraclecloud.com/"
                    "hcmUI/CandidateExperience/en/sites/Honeywell/jobs"
                )
            ),
        )

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "12345")
        self.assertEqual(job.title, "Controls Engineer")
        self.assertEqual(
            job.url,
            "https://example.fa.us2.oraclecloud.com/"
            "hcmUI/CandidateExperience/en/sites/Honeywell/job/12345",
        )
        self.assertEqual(
            job.location,
            "Calgary, AB, Canada | Edmonton, AB, Canada",
        )
        self.assertEqual(job.offices, ["Calgary, AB, Canada", "Edmonton, AB, Canada"])
        self.assertEqual(job.updated_at, "2026-08-03")
        self.assertEqual(job.departments, ["Engineering", "Product Development"])
        self.assertEqual(job.description, "Build reliable controls.")
        self.assertEqual(job.raw_metadata["requisition"]["OrganizationId"], 3000000123)
        self.assertEqual(
            job.source,
            "oracle_recruiting:example.fa.us2.oraclecloud.com:cx_1",
        )

    def test_supported_organization_identifier_is_sent_as_finder_parameter(self):
        session = FakeSession([FakeResponse(payload([], total=0))])

        oracle_recruiting.fetch_jobs(
            session,
            target(max_pages=1, organization_id="3000000123"),
        )

        self.assertIn(
            "organizationId=3000000123",
            session.calls[0][1]["params"]["finder"],
        )

    @patch.object(oracle_recruiting.time, "sleep", return_value=None)
    def test_deterministic_pagination_and_maximum_page_enforcement(self, _sleep):
        first = [requisition(str(index)) for index in range(24)]
        second = [requisition(str(index)) for index in range(24, 48)]
        session = FakeSession(
            [
                FakeResponse(payload(first, total=100, offset=0)),
                FakeResponse(payload(second, total=100, offset=24)),
            ]
        )

        jobs = oracle_recruiting.fetch_jobs(session, target(max_pages=2))

        self.assertEqual(len(jobs), 48)
        self.assertEqual(len(session.calls), 2)
        finders = [call[1]["params"]["finder"] for call in session.calls]
        self.assertIn("limit=24,offset=0,sortBy=POSTING_DATES_DESC", finders[0])
        self.assertIn("limit=24,offset=24,sortBy=POSTING_DATES_DESC", finders[1])

    def test_max_pages_above_hard_limit_is_rejected_without_network(self):
        session = FakeSession([])
        with self.assertRaisesRegex(AdapterError, "max_pages cannot exceed 5"):
            oracle_recruiting.fetch_jobs(session, target(max_pages=6))
        self.assertEqual(session.calls, [])

    def test_valid_zero_job_response(self):
        jobs = oracle_recruiting.fetch_jobs(
            FakeSession([FakeResponse(payload([], total=0))]),
            target(max_pages=1),
        )
        self.assertEqual(jobs, [])

    def test_malformed_http_200_is_rejected(self):
        with self.assertRaisesRegex(AdapterError, "search envelope"):
            oracle_recruiting.fetch_jobs(
                FakeSession([FakeResponse({})]),
                target(max_pages=1),
            )

    def test_invalid_endpoint_is_rejected_without_network(self):
        session = FakeSession([])
        with self.assertRaisesRegex(AdapterError, "supported public Oracle endpoint"):
            oracle_recruiting.fetch_jobs(
                session,
                target(endpoint_url="https://example.test/jobs"),
            )
        self.assertEqual(session.calls, [])

    @patch.object(oracle_recruiting.time, "sleep", return_value=None)
    def test_retryable_failure_is_bounded_and_recovers(self, sleep):
        session = FakeSession(
            [
                FakeResponse({}, status_code=503),
                FakeResponse(payload([], total=0)),
            ]
        )

        jobs = oracle_recruiting.fetch_jobs(session, target(max_pages=1))

        self.assertEqual(jobs, [])
        self.assertEqual(len(session.calls), 2)
        sleep.assert_called_once_with(oracle_recruiting.RETRY_DELAY_SECONDS)

    def test_duplicate_source_is_rejected_without_network(self):
        result = validator.validate_company(
            registry_entry(name="Duplicate"),
            duplicate_of="Existing Oracle Source",
            session_factory=FailingSessionFactory(),
        )
        self.assertEqual(result["validation_status"], validator.DUPLICATE_SOURCE)
        self.assertFalse(result["safe_to_activate"])

    @patch.object(oracle_recruiting.time, "sleep", return_value=None)
    def test_validator_reports_pagination_and_valid_zero_jobs(self, _sleep):
        session = FakeSession([FakeResponse(payload([], total=0))])
        result = validator.validate_company(
            registry_entry(),
            session_factory=lambda: session,
            sleep=lambda _: None,
        )
        self.assertEqual(result["validation_status"], validator.VALIDATED_ZERO_JOBS)
        self.assertTrue(result["parse_success"])
        self.assertTrue(result["safe_to_activate"])
        self.assertEqual(result["pagination_result"]["offsets"], [0])
        self.assertEqual(result["pagination_result"]["stop_reason"], "validated_zero_jobs")

    def test_registry_loader_compatibility_and_existing_entries_unchanged(self):
        _, current = validator.load_registry(CURRENT_REGISTRY)
        _, candidate = validator.load_registry(CANDIDATE_REGISTRY)
        _, validated = validator.load_registry(VALIDATED_REGISTRY)
        targets = load_target_companies(str(CANDIDATE_REGISTRY))

        self.assertEqual(len(current), 48)
        self.assertEqual(candidate[:48], current)
        self.assertEqual(validated[:48], current)
        self.assertEqual(len(candidate), 53)
        self.assertEqual(len(validated), 53)
        self.assertEqual(validated, candidate)
        self.assertEqual([item.name for item in targets[-5:]], [
            "Texas Instruments",
            "JPMorgan Chase",
            "Emerson",
            "Honeywell",
            "Ford",
        ])
        self.assertTrue(all(item.platform == "oracle_recruiting" for item in targets[-5:]))
        self.assertNotIn("Schneider Electric", [item.name for item in targets])

    @patch.object(oracle_recruiting.time, "sleep", return_value=None)
    def test_oracle_validation_initializes_no_persistence(self, _sleep):
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
                "    platform: oracle_recruiting\n"
                "    careers_url: https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs\n"
                "    endpoint_url: https://example.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions\n"
                "    site_number: CX_1\n"
                "    locale: en\n"
                "    max_pages: 1\n",
                encoding="utf-8",
            )
            with (
                patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite initialized")) as sqlite_init,
                patch.object(SupabaseJobStore, "from_env", side_effect=AssertionError("Supabase initialized")) as supabase_init,
                patch.object(GoogleSheetsPublisher, "from_env", side_effect=AssertionError("Sheets initialized")) as sheets_init,
            ):
                validator.validate_registry(
                    current,
                    candidate,
                    only_additions=True,
                    output=root / "report.json",
                    validated_config=root / "validated.yaml",
                    session_factory=lambda: FakeSession([FakeResponse(payload([], total=0))]),
                    sleep=lambda _: None,
                )
            sqlite_init.assert_not_called()
            supabase_init.assert_not_called()
            sheets_init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
