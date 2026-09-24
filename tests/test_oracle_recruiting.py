import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from scripts import validate_company_registry as validator
from src.scraper.company_careers.adapters import oracle_recruiting
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


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


def oracle_candidate_entries():
    return [
        registry_entry(
            name="Texas Instruments",
            careers_url="https://careers.ti.com/en/sites/CX/jobs",
            endpoint_url=(
                "https://edbz.fa.us2.oraclecloud.com/"
                "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            ),
            site_number="CX",
            max_pages=2,
        ),
        registry_entry(
            name="JPMorgan Chase",
            careers_url=(
                "https://jpmc.fa.oraclecloud.com/"
                "hcmUI/CandidateExperience/en/sites/CX_1001/jobs"
            ),
            endpoint_url=(
                "https://jpmc.fa.oraclecloud.com/"
                "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            ),
            site_number="CX_1001",
            max_pages=2,
        ),
        registry_entry(
            name="Emerson",
            careers_url=(
                "https://hdjq.fa.us2.oraclecloud.com/"
                "hcmUI/CandidateExperience/en/sites/CX_1/jobs"
            ),
            endpoint_url=(
                "https://hdjq.fa.us2.oraclecloud.com/"
                "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            ),
            max_pages=2,
        ),
        registry_entry(
            name="Honeywell",
            careers_url=(
                "https://ibqbjb.fa.ocs.oraclecloud.com/"
                "hcmUI/CandidateExperience/en/sites/Honeywell/jobs"
            ),
            endpoint_url=(
                "https://ibqbjb.fa.ocs.oraclecloud.com/"
                "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            ),
            max_pages=2,
        ),
        registry_entry(
            name="Ford",
            careers_url=(
                "https://efds.fa.em5.oraclecloud.com/"
                "hcmUI/CandidateExperience/en/sites/CX_1/jobs"
            ),
            endpoint_url=(
                "https://efds.fa.em5.oraclecloud.com/"
                "hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            ),
            max_pages=2,
        ),
    ]


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
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "reported_total_reached")
        self.assertEqual(metadata["total_available"], 1)
        self.assertEqual((metadata["pages_requested"], metadata["pages_completed"]), (1, 1))

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
                FakeResponse(payload(first, total=20_001, offset=0)),
                FakeResponse(payload(second, total=20_001, offset=24)),
            ]
        )

        jobs = oracle_recruiting.fetch_jobs(session, target(max_pages=2))

        self.assertEqual(len(jobs), 48)
        self.assertEqual(len(session.calls), 2)
        finders = [call[1]["params"]["finder"] for call in session.calls]
        self.assertIn("limit=24,offset=0,sortBy=POSTING_DATES_DESC", finders[0])
        self.assertIn("limit=24,offset=24,sortBy=POSTING_DATES_DESC", finders[1])
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "maximum_pages_reached")
        self.assertEqual(metadata["total_available"], 20_001)

    def test_max_pages_above_hard_limit_is_rejected_without_network(self):
        session = FakeSession([])
        with self.assertRaisesRegex(AdapterError, "max_pages cannot exceed 5"):
            oracle_recruiting.fetch_jobs(session, target(max_pages=6))
        self.assertEqual(session.calls, [])

    def test_valid_zero_job_response(self):
        session = FakeSession([FakeResponse(payload([], total=0))])
        jobs = oracle_recruiting.fetch_jobs(session, target(max_pages=1))
        self.assertEqual(jobs, [])
        self.assertTrue(source_scan_metadata(session)["scan_complete"])

    @patch.object(oracle_recruiting.time, "sleep", return_value=None)
    def test_changing_total_and_repeated_ids_fail_closed(self, _sleep):
        first = [requisition(str(index)) for index in range(24)]
        changed = FakeSession(
            [
                FakeResponse(payload(first, total=48, offset=0)),
                FakeResponse(
                    payload(
                        [requisition(str(index)) for index in range(24, 48)],
                        total=49,
                        offset=24,
                    )
                ),
            ]
        )
        with self.assertRaisesRegex(AdapterError, "changed during pagination"):
            oracle_recruiting.fetch_jobs(changed, target(max_pages=2))
        changed_metadata = source_scan_metadata(changed)
        self.assertFalse(changed_metadata["scan_complete"])
        self.assertEqual(changed_metadata["scan_completeness_reason"], "inconsistent_total")

        repeated = FakeSession(
            [
                FakeResponse(payload(first, total=25, offset=0)),
                FakeResponse(payload([requisition("0")], total=25, offset=24)),
            ]
        )
        with self.assertRaisesRegex(AdapterError, "repeated stable job ID"):
            oracle_recruiting.fetch_jobs(repeated, target(max_pages=2))
        repeated_metadata = source_scan_metadata(repeated)
        self.assertFalse(repeated_metadata["scan_complete"])
        self.assertEqual(repeated_metadata["stop_reason"], "repeated_job_id")

    def test_short_page_before_reported_total_fails_closed(self):
        session = FakeSession(
            [FakeResponse(payload([requisition("1")], total=2, offset=0))]
        )
        with self.assertRaisesRegex(AdapterError, "short page ended before"):
            oracle_recruiting.fetch_jobs(session, target(max_pages=1))
        self.assertFalse(source_scan_metadata(session)["scan_complete"])

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
        baseline_text = (
            "target_companies:\n"
            "  - name: Existing Greenhouse\n"
            "    platform: greenhouse\n"
            "    slug: existing\n"
            "  - name: Existing Workday\n"
            "    platform: workday\n"
            "    tenant: existing\n"
            "    wd_subdomain: wd1\n"
            "    site_id: Careers\n"
        )
        additions = oracle_candidate_entries()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline_path = root / "baseline.yaml"
            candidate_path = root / "candidate.yaml"
            baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
            candidate_path.write_bytes(
                validator.build_validated_registry_bytes(baseline_path, additions)
            )
            baseline_bytes = baseline_path.read_bytes()
            candidate_bytes = candidate_path.read_bytes()
            _, baseline = validator.load_registry(baseline_path)
            _, candidate = validator.load_registry(candidate_path)
            detected = validator.detect_additions(baseline, candidate)
            targets = load_target_companies(str(candidate_path))

        expected_names = [entry["name"] for entry in additions]
        self.assertEqual(candidate_bytes[: len(baseline_bytes)], baseline_bytes)
        self.assertEqual(candidate[: len(baseline)], baseline)
        self.assertEqual(len(candidate), len(baseline) + len(additions))
        self.assertEqual([entry["name"] for entry in detected], expected_names)
        self.assertEqual([item.name for item in targets[-len(additions) :]], expected_names)
        self.assertTrue(
            all(item.platform == "oracle_recruiting" for item in targets[-len(additions) :])
        )
        self.assertNotIn("Schneider Electric", expected_names)

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
