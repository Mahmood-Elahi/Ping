import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from scripts import validate_company_registry as validator
from src.scraper.company_careers.models import JobRecord


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, url="https://example.test", history=None):
        self.status_code = status_code
        self.payload = payload
        self.url = url
        self.history = history or []

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def __bool__(self):
        return self.status_code < 400


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _response(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP call")
        response = self.responses.pop(0)
        response.url = url
        return response

    def get(self, url, **kwargs):
        return self._response("get", url, kwargs)

    def post(self, url, **kwargs):
        return self._response("post", url, kwargs)


class FailingSessionFactory:
    def __call__(self):
        raise AssertionError("Network session must not be initialized")


def greenhouse_payload(job=True):
    jobs = []
    if job:
        jobs.append(
            {
                "id": 101,
                "title": "Software Engineer",
                "location": {"name": "Remote"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
                "updated_at": "2026-08-03",
                "departments": [],
                "offices": [],
            }
        )
    return {"jobs": jobs}


def lever_payload(job=True):
    if not job:
        return []
    return [
        {
            "id": "lever-1",
            "text": "Robotics Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/lever-1",
            "createdAt": 123,
            "categories": {"location": "Calgary", "team": "Engineering"},
        }
    ]


def ashby_payload(job=True):
    return {
        "jobs": (
            [
                {
                    "id": "ashby-1",
                    "title": "Platform Engineer",
                    "location": "Remote",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
                    "publishedDate": "2026-08-03",
                }
            ]
            if job
            else []
        )
    }


def workday_payload(job=True):
    return {
        "jobPostings": (
            [
                {
                    "title": "Controls Engineer",
                    "externalPath": "/job/Controls-Engineer/R001",
                    "locationsText": "Remote",
                    "bulletFields": ["R001"],
                    "postedOn": "Posted Today",
                }
            ]
            if job
            else []
        )
    }


def target(platform="greenhouse", name="Acme", **overrides):
    result = {"name": name, "platform": platform}
    if platform in {"greenhouse", "lever", "ashby"}:
        result["slug"] = "acme"
    elif platform == "workday":
        result.update(tenant="acme", wd_subdomain="wd1", site_id="Careers")
    result.update(overrides)
    return result


def successful_job(name="Acme", source="greenhouse:acme"):
    return JobRecord(
        job_id="job-1",
        title="Engineer",
        company=name,
        location="Remote",
        url="https://example.test/jobs/1",
        updated_at="2026-08-03",
        source=source,
    )


def workday_candidate_entries():
    return [
        {
            "name": "Enbridge",
            "platform": "workday",
            "tenant": "enbridge",
            "wd_subdomain": "wd3",
            "site_id": "enbridge_careers",
            "careers_url": "https://www.enbridge.com/work-with-enbridge/careers",
            "source_url": "https://www.enbridge.com/work-with-enbridge/careers",
            "priority": "TBD",
        },
        {
            "name": "Medtronic",
            "platform": "workday",
            "tenant": "medtronic",
            "wd_subdomain": "wd1",
            "site_id": "MedtronicCareers",
            "careers_url": "https://www.medtronic.com/en-us/our-company/careers.html",
            "source_url": "https://www.medtronic.com/en-us/our-company/careers.html",
            "priority": "TBD",
        },
        {
            "name": "Shell",
            "platform": "workday",
            "tenant": "shell",
            "wd_subdomain": "wd3",
            "site_id": "shellcareers",
            "careers_url": "https://www.shell.com/careers.html",
            "source_url": "https://www.shell.com/careers.html",
            "priority": "TBD",
        },
    ]


class RegistryValidationTests(unittest.TestCase):
    def _workday_registry_fixture(self, root):
        current = root / "current.yaml"
        candidate = root / "candidate.yaml"
        current_text = (
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
        current.write_text(current_text, encoding="utf-8", newline="\n")
        candidate.write_bytes(
            validator.build_validated_registry_bytes(current, workday_candidate_entries())
        )
        return current, candidate, current_text.encode("utf-8")

    def test_workday_candidate_registry_adds_only_requested_companies(self):
        with TemporaryDirectory() as temp_dir:
            current_path, candidate_path, _ = self._workday_registry_fixture(Path(temp_dir))
            _, current = validator.load_registry(current_path)
            _, candidate = validator.load_registry(candidate_path)
            detected = validator.detect_additions(current, candidate)

        self.assertEqual([entry["name"] for entry in detected], ["Enbridge", "Medtronic", "Shell"])
        additions = {entry["name"]: entry for entry in detected}
        self.assertEqual(
            {
                name: (entry["tenant"], entry["wd_subdomain"], entry["site_id"])
                for name, entry in additions.items()
            },
            {
                "Enbridge": ("enbridge", "wd3", "enbridge_careers"),
                "Medtronic": ("medtronic", "wd1", "MedtronicCareers"),
                "Shell": ("shell", "wd3", "shellcareers"),
            },
        )
        allowed_fields = {
            "name",
            "platform",
            "tenant",
            "wd_subdomain",
            "site_id",
            "careers_url",
            "source_url",
            "priority",
        }
        self.assertTrue(all(set(entry) <= allowed_fields for entry in additions.values()))

    def test_workday_candidate_registry_preserves_baseline_bytes_and_order(self):
        with TemporaryDirectory() as temp_dir:
            current, candidate, baseline_bytes = self._workday_registry_fixture(Path(temp_dir))
            current_bytes = current.read_bytes()
            candidate_bytes = candidate.read_bytes()
            _, entries = validator.load_registry(candidate)

        self.assertEqual(current_bytes, baseline_bytes)
        self.assertEqual(candidate_bytes[: len(baseline_bytes)], baseline_bytes)
        self.assertEqual(
            [entry["name"] for entry in entries],
            ["Existing Greenhouse", "Existing Workday", "Enbridge", "Medtronic", "Shell"],
        )

    def test_live_registry_integrity_after_platform_promotions(self):
        registry = Path(__file__).resolve().parents[1] / "config" / "target_companies.yaml"
        _, entries = validator.load_registry(registry)
        names = [" ".join(str(entry["name"]).casefold().split()) for entry in entries]

        self.assertTrue(entries)
        self.assertEqual(len(names), len(set(names)))
        expected_platforms = {
            "Enbridge": "workday",
            "Medtronic": "workday",
            "Shell": "workday",
            "Texas Instruments": "oracle_recruiting",
            "JPMorgan Chase": "oracle_recruiting",
            "Emerson": "oracle_recruiting",
            "Honeywell": "oracle_recruiting",
            "Ford": "oracle_recruiting",
            "Lam Research": "sap_successfactors",
            "KUKA": "sap_successfactors",
            "Festo": "sap_successfactors",
            "Hyundai": "sap_successfactors",
            "Volkswagen Group": "sap_successfactors",
            "Volvo Cars": "sap_successfactors",
            "Ferrari": "sap_successfactors",
            "ZF Friedrichshafen": "sap_successfactors",
            "Halliburton": "sap_successfactors",
            "John Deere": "sap_successfactors",
            "Pembina Pipeline": "sap_successfactors",
            "Microsoft": "eightfold",
            "Qualcomm": "eightfold",
            "Micron": "eightfold",
            "Morgan Stanley": "eightfold",
            "Eaton": "eightfold",
        }
        for company, platform in expected_platforms.items():
            matches = [
                entry
                for entry in entries
                if " ".join(str(entry["name"]).casefold().split()) == company.casefold()
            ]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["platform"], platform)

    def test_candidate_additions_are_detected(self):
        current = [target(name="Existing")]
        candidate = [dict(current[0]), target(name="New", slug="new")]
        additions = validator.detect_additions(current, candidate)
        self.assertEqual([entry["name"] for entry in additions], ["New"])

    def test_existing_27_entries_are_not_revalidated_as_additions(self):
        current = [target(name=f"Existing {index}", slug=f"existing-{index}") for index in range(27)]
        candidate = current + [target(name="New", slug="new")]
        self.assertEqual([entry["name"] for entry in validator.detect_additions(current, candidate)], ["New"])

    def test_greenhouse_validation_uses_production_parser(self):
        session = FakeSession([FakeResponse(payload=greenhouse_payload())])
        result = validator.validate_company(target(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertEqual(result["jobs_returned"], 1)
        self.assertEqual(result["sample_job_title"], "Software Engineer")
        self.assertTrue(result["parse_success"])

    def test_lever_validation_uses_production_parser(self):
        session = FakeSession([FakeResponse(payload=lever_payload())])
        result = validator.validate_company(
            target("lever"), session_factory=lambda: session, sleep=lambda _: None
        )
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertEqual(result["sample_job_title"], "Robotics Engineer")

    def test_ashby_validation_uses_production_parser(self):
        session = FakeSession([FakeResponse(payload=ashby_payload())])
        result = validator.validate_company(
            target("ashby"), session_factory=lambda: session, sleep=lambda _: None
        )
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertIn("ashby-1", result["sample_apply_url"])

    def test_workday_validation_uses_one_production_parser_page(self):
        session = FakeSession([FakeResponse(payload=workday_payload())])
        result = validator.validate_company(
            target("workday"), session_factory=lambda: session, sleep=lambda _: None
        )
        self.assertEqual(result["validation_status"], validator.VALIDATED_WITH_JOBS)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["json"]["limit"], 20)
        self.assertEqual(result["sample_job_id"], "R001")
        self.assertEqual(result["stable_job_id_result"], "valid")
        self.assertEqual(result["apply_url_result"], "valid")
        self.assertEqual(
            result["sample_apply_url"],
            "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Controls-Engineer/R001",
        )

    def test_workday_valid_zero_job_response_is_accepted(self):
        session = FakeSession([FakeResponse(payload=workday_payload(False))])
        result = validator.validate_company(
            target("workday"), session_factory=lambda: session, sleep=lambda _: None
        )

        self.assertEqual(result["validation_status"], validator.VALIDATED_ZERO_JOBS)
        self.assertEqual(result["stable_job_id_result"], "not_applicable_zero_jobs")
        self.assertEqual(result["apply_url_result"], "not_applicable_zero_jobs")
        self.assertTrue(result["safe_to_activate"])

    def test_invalid_workday_tenant_site_combination_is_rejected(self):
        session = FakeSession(
            [FakeResponse(status_code=404, payload={}) for _ in range(4)]
        )
        result = validator.validate_company(
            target("workday", tenant="wrong", site_id="WrongSite"),
            session_factory=lambda: session,
            sleep=lambda _: None,
        )

        self.assertEqual(result["validation_status"], validator.INVALID_CONFIGURATION)
        self.assertEqual(result["http_status"], 404)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(session.calls[-1][0], "get")
        self.assertFalse(result["safe_to_activate"])

    def test_duplicate_workday_tenant_is_rejected_without_network(self):
        result = validator.validate_company(
            target("workday", name="Duplicate", tenant="acme", site_id="OtherSite"),
            duplicate_of="Existing Workday",
            session_factory=FailingSessionFactory(),
        )

        self.assertEqual(result["validation_status"], validator.DUPLICATE_SOURCE)
        self.assertEqual(result["duplicate_source_status"], "duplicates Existing Workday")
        self.assertFalse(result["safe_to_activate"])

    def test_valid_zero_job_response_is_accepted(self):
        session = FakeSession([FakeResponse(payload=greenhouse_payload(False))])
        result = validator.validate_company(target(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.VALIDATED_ZERO_JOBS)
        self.assertTrue(result["safe_to_activate"])

    def test_invalid_ats_identifier_is_rejected(self):
        session = FakeSession([FakeResponse(status_code=404, payload={"error": "not found"})])
        result = validator.validate_company(target(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.INVALID_CONFIGURATION)
        self.assertFalse(result["safe_to_activate"])
        self.assertEqual(result["http_status"], 404)

    def test_blocked_response_is_not_activated(self):
        session = FakeSession([FakeResponse(status_code=403, payload={})])
        result = validator.validate_company(target(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.BLOCKED)
        self.assertFalse(result["safe_to_activate"])

    def test_temporary_failure_is_retried_and_not_activated(self):
        session = FakeSession(
            [FakeResponse(status_code=503, payload={}), FakeResponse(status_code=503, payload={})]
        )
        result = validator.validate_company(target(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.TEMPORARY_FAILURE)
        self.assertEqual(result["attempts"], 2)
        self.assertFalse(result["safe_to_activate"])

    def test_duplicate_source_is_rejected_without_network(self):
        result = validator.validate_company(
            target(name="Copy"),
            duplicate_of="Existing",
            session_factory=FailingSessionFactory(),
        )
        self.assertEqual(result["validation_status"], validator.DUPLICATE_SOURCE)
        self.assertEqual(result["duplicate_source_status"], "duplicates Existing")

    def test_malformed_success_response_is_not_zero_jobs(self):
        session = FakeSession([FakeResponse(payload={})])
        result = validator.validate_company(target(), session_factory=lambda: session, sleep=lambda _: None)
        self.assertEqual(result["validation_status"], validator.UNSUPPORTED_RESPONSE)
        self.assertFalse(result["parse_success"])

    def test_registry_validation_initializes_no_supabase(self):
        self._assert_persistence_not_initialized("supabase")

    def test_registry_validation_initializes_no_google_sheets(self):
        self._assert_persistence_not_initialized("sheets")

    def test_registry_validation_initializes_no_sqlite(self):
        self._assert_persistence_not_initialized("sqlite")

    def _assert_persistence_not_initialized(self, integration):
        session = FakeSession([FakeResponse(payload=greenhouse_payload(False))])
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current.yaml"
            candidate = root / "candidate.yaml"
            current.write_text("target_companies: []\n", encoding="utf-8")
            candidate.write_text(
                yaml.safe_dump({"target_companies": [target()]}, sort_keys=False), encoding="utf-8"
            )
            if integration == "sqlite":
                mocked = patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite initialized"))
            elif integration == "supabase":
                from src.integrations.supabase_job_store import SupabaseJobStore

                mocked = patch.object(
                    SupabaseJobStore, "from_env", side_effect=AssertionError("Supabase initialized")
                )
            else:
                from src.integrations.google_sheets_publisher import GoogleSheetsPublisher

                mocked = patch.object(
                    GoogleSheetsPublisher,
                    "from_env",
                    side_effect=AssertionError("Google Sheets initialized"),
                )
            with mocked as initializer:
                validator.validate_registry(
                    current,
                    candidate,
                    only_additions=True,
                    output=root / "report.json",
                    validated_config=root / "validated.yaml",
                    session_factory=lambda: session,
                    sleep=lambda _: None,
                )
            initializer.assert_not_called()

    def test_validated_yaml_preserves_existing_bytes_and_order(self):
        current_text = (
            "target_companies:\n"
            "  - name: First\n"
            "    platform: greenhouse\n"
            "    slug: first\n"
            "  - name: Second\n"
            "    platform: lever\n"
            "    slug: second\n"
        )
        content = None
        with TemporaryDirectory() as temp_dir:
            current = Path(temp_dir) / "current.yaml"
            current.write_text(current_text, encoding="utf-8", newline="\n")
            content = validator.build_validated_registry_bytes(current, [target(name="Third")])
        self.assertTrue(content.startswith(current_text.encode("utf-8")))
        names = [entry["name"] for entry in yaml.safe_load(content)["target_companies"]]
        self.assertEqual(names, ["First", "Second", "Third"])

    def test_repeated_validation_output_is_deterministic(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current.yaml"
            candidate = root / "candidate.yaml"
            output = root / "report.json"
            validated = root / "validated.yaml"
            current.write_text("target_companies: []\n", encoding="utf-8")
            candidate.write_text(
                yaml.safe_dump({"target_companies": [target()]}, sort_keys=False), encoding="utf-8"
            )

            def run_once():
                session = FakeSession([FakeResponse(payload=greenhouse_payload())])
                validator.validate_registry(
                    current,
                    candidate,
                    only_additions=True,
                    output=output,
                    validated_config=validated,
                    session_factory=lambda: session,
                    sleep=lambda _: None,
                )
                return output.read_bytes(), validated.read_bytes()

            first = run_once()
            second = run_once()
            self.assertEqual(first, second)
            self.assertEqual(json.loads(first[0])["summary"]["validated_config_entries"], 1)

    def test_batch_size_above_five_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "registry.yaml"
            path.write_text("target_companies: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "between 1 and 5"):
                validator.validate_registry(path, path, batch_size=6, dry_run=True)


if __name__ == "__main__":
    unittest.main()
