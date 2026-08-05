from __future__ import annotations

import csv
import hashlib
import inspect
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from scripts import validate_company_registry as validator
from scripts import validate_existing_adapter_v4_batch as batch
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import lever, workday
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_PRODUCTION_SHA256 = "966175F9AA6D0595903CB1AF527CF92A068CA21AFCC71551CBC535BBFA121C35"
PARENT_MAPPINGS_SHA256 = "9FCFA2C5A6B78BB170422E7FD752569AB26BB38ED415F32782E28DB303A323B5"
EXPECTED = ["Intel", "NXP Semiconductors", "KLA", "Capital One", "Unity", "Loft Orbital"]
EXPECTED_SOURCE_IDENTITIES = [
    "workday:intel:External",
    "workday:nxp:careers",
    "workday:kla:Search",
    "workday:capitalone:Capital_One",
    "workday:unitytech:Unity",
    "lever:loftorbital",
]


def historical_prefix_bytes(path: Path) -> bytes:
    marker = f"  - name: {EXPECTED[0]}".encode("utf-8")
    content = path.read_bytes()
    for line in content.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == marker:
            return content[:content.index(line)]
    raise AssertionError(f"{path} does not contain the first reviewed candidate {EXPECTED[0]}")


def registry_entries_from_bytes(content: bytes) -> list[dict]:
    data = yaml.safe_load(content.decode("utf-8")) or {}
    return list(data.get("target_companies") or [])


def historical_baseline_entries() -> list[dict]:
    return registry_entries_from_bytes(historical_prefix_bytes(batch.CANDIDATE_REGISTRY))


def configured_source_identity(entry: dict) -> str:
    if entry["platform"] == "workday":
        return f"workday:{entry['tenant']}:{entry['site_id']}"
    return f"{entry['platform']}:{entry['slug']}"


def normalized_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.url = "https://example.test/api"
        self.history = []
        self.headers = {"content-type": "application/json"}

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
        raise AssertionError("A network session must not be initialized")


def posting(index, *, stable_id=None, title=None):
    identity = stable_id or f"REQ-{index}"
    return {
        "title": title or f"Engineer {index}",
        "externalPath": f"/job/Engineer/{identity}",
        "locationsText": "Remote",
        "bulletFields": [identity],
        "postedOn": "Posted Today",
    }


def lever_posting(index, *, job_id=None, hosted_url=None):
    identity = job_id if job_id is not None else f"lever-{index}"
    return {
        "id": identity,
        "text": f"Engineer {index}",
        "hostedUrl": hosted_url if hosted_url is not None else f"https://jobs.lever.co/acme/{identity}",
        "createdAt": 1,
        "categories": {"location": "Remote", "team": "Engineering"},
    }


def lever_target(**overrides):
    entry = {
        "name": "Acme",
        "platform": "lever",
        "slug": "acme",
        "allowed_apply_hosts": ["jobs.lever.co"],
        "max_pages": 1,
        "max_jobs": 20,
    }
    entry.update(overrides)
    return entry


def workday_target(**overrides):
    entry = {
        "name": "Acme",
        "platform": "workday",
        "tenant": "acme",
        "wd_subdomain": "wd1",
        "site_id": "Careers",
        "allowed_apply_hosts": ["acme.wd1.myworkdayjobs.com"],
        "max_pages": 2,
        "max_jobs": 20,
    }
    entry.update(overrides)
    return entry


class ExistingAdapterV4ArtifactTests(unittest.TestCase):
    def test_01_exact_six_company_inventory(self):
        with batch.INVENTORY.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["company"] for row in rows], EXPECTED)

    def test_02_exact_candidates_derive_from_v4_manifest(self):
        batch.reconcile_candidates()
        self.assertEqual(batch.EXPECTED_COMPANIES, EXPECTED)

    def test_03_candidate_additions_derive_from_historical_artifact_prefix(self):
        _, candidates = validator.load_registry(batch.CANDIDATE_REGISTRY)
        self.assertEqual(
            [entry["name"] for entry in validator.detect_additions(historical_baseline_entries(), candidates)],
            EXPECTED,
        )

    def test_04_parent_mapped_companies_are_excluded(self):
        mapped = {
            " ".join(item.child_company.casefold().split())
            for item in load_parent_source_mappings(batch.PARENT_MAPPINGS)
        }
        self.assertFalse(mapped & {validator._name_key(entry) for entry in batch.CANDIDATE_ENTRIES})

    def test_05_candidate_order_follows_master_workbook(self):
        ranks = [entry["original_workbook_rank"] for entry in batch.CANDIDATE_ENTRIES]
        self.assertEqual(ranks, sorted(ranks))

    def test_06_historical_127_entry_byte_prefix_is_preserved(self):
        candidate_prefix = historical_prefix_bytes(batch.CANDIDATE_REGISTRY)
        validated_prefix = historical_prefix_bytes(batch.VALIDATED_REGISTRY)
        self.assertEqual(candidate_prefix, validated_prefix)
        self.assertEqual(len(registry_entries_from_bytes(candidate_prefix)), batch.HISTORICAL_PRODUCTION_COUNT)
        self.assertEqual(hashlib.sha256(candidate_prefix).hexdigest().upper(), HISTORICAL_PRODUCTION_SHA256)
        self.assertTrue(batch.PRODUCTION.read_bytes().startswith(candidate_prefix))

    def test_07_candidate_registry_contains_exactly_six_historical_additions(self):
        _, candidates = validator.load_registry(batch.CANDIDATE_REGISTRY)
        additions = validator.detect_additions(historical_baseline_entries(), candidates)
        self.assertEqual([entry["name"] for entry in additions], EXPECTED)
        self.assertEqual(len(additions), 6)

    def test_08_candidate_registry_is_loader_compatible(self):
        _, entries = validator.load_registry(batch.CANDIDATE_REGISTRY)
        self.assertEqual(len(load_target_companies(str(batch.CANDIDATE_REGISTRY))), len(entries))

    def test_09_validated_registry_contains_only_safe_candidates(self):
        report = __import__("json").loads(batch.REPORT.read_text(encoding="utf-8"))
        safe = [row["company"] for row in report["results"] if row["safe_to_activate"]]
        _, validated = validator.load_registry(batch.VALIDATED_REGISTRY)
        additions = validator.detect_additions(historical_baseline_entries(), validated)
        self.assertEqual([entry["name"] for entry in additions], safe)
        self.assertEqual(len(additions), 6)

    def test_10_validated_additions_preserve_candidate_order(self):
        _, validated = validator.load_registry(batch.VALIDATED_REGISTRY)
        names = [
            entry["name"]
            for entry in validator.detect_additions(historical_baseline_entries(), validated)
        ]
        self.assertEqual(names, [name for name in EXPECTED if name in names])

    def test_11_historical_delta_uses_controlled_synthetic_baseline(self):
        baseline = b"target_companies:\n  - name: Synthetic\n    platform: lever\n    slug: synthetic\n"
        with TemporaryDirectory() as temp_dir:
            current = Path(temp_dir) / "current.yaml"
            candidate = Path(temp_dir) / "candidate.yaml"
            current.write_bytes(baseline)
            candidate.write_bytes(validator.build_candidate_registry_bytes(current, [lever_target(name="Added")]))
            _, entries = validator.load_registry(candidate)
        self.assertEqual([entry["name"] for entry in entries], ["Synthetic", "Added"])

    def test_12_duplicate_normalized_company_is_rejected(self):
        duplicates = validator._duplicate_company_owners([{"name": "Intel"}], [{"name": " INTEL "}])
        self.assertEqual(duplicates, {0: "Intel"})

    def test_13_duplicate_normalized_source_is_rejected(self):
        duplicates = validator._duplicate_owners([lever_target(name="One")], [lever_target(name="Two", slug=" ACME ")])
        self.assertEqual(duplicates, {0: "One"})

    def test_14_workday_tenant_site_identities_match_endpoints(self):
        for entry in batch.CANDIDATE_ENTRIES[:5]:
            with self.subTest(company=entry["name"]):
                endpoint = validator.endpoint_for(entry)
                self.assertIn(f"/wday/cxs/{entry['tenant']}/{entry['site_id']}/jobs", endpoint)
                self.assertEqual(validator.source_key(entry), f"workday:{entry['tenant']}")

    def test_15_lever_board_identity_matches_endpoint(self):
        entry = batch.CANDIDATE_ENTRIES[-1]
        self.assertEqual(entry["slug"], "loftorbital")
        self.assertEqual(validator.source_key(entry), "lever:loftorbital")
        self.assertEqual(validator.endpoint_for(entry), "https://api.lever.co/v0/postings/loftorbital?mode=json")


class ExistingAdapterV4ContractTests(unittest.TestCase):
    def test_16_stable_ids_are_required(self):
        result = validator.validate_company(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse([lever_posting(1, job_id="")])]),
        )
        self.assertEqual(batch._review_status(result), "missing_stable_id")

    def test_17_official_apply_urls_are_required(self):
        result = validator.validate_company(
            lever_target(allowed_apply_hosts=[]),
            session_factory=lambda: FakeSession([FakeResponse([lever_posting(1, hosted_url="")])]),
        )
        self.assertEqual(batch._review_status(result), "invalid_apply_url")

    def test_18_invalid_apply_host_is_rejected(self):
        result = validator.validate_company(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse([lever_posting(1, hosted_url="https://evil.test/1")])]),
        )
        self.assertEqual(batch._review_status(result), "invalid_apply_host")

    def test_19_valid_zero_job_schema_is_accepted(self):
        result = validator.validate_company(
            workday_target(),
            session_factory=lambda: FakeSession([FakeResponse({"jobPostings": [], "total": 0})]),
            max_pages_limit=2,
        )
        self.assertEqual(result["validation_status"], validator.VALIDATED_ZERO_JOBS)
        self.assertTrue(result["safe_to_activate"])

    def test_20_malformed_success_response_is_rejected(self):
        result = validator.validate_company(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse({"not": "a list"})]),
        )
        self.assertFalse(result["safe_to_activate"])
        self.assertEqual(result["http_status"], 200)

    def test_21_authentication_response_is_rejected(self):
        def authentication_adapter(session, _target, timeout):
            session.get("https://example.test/api", timeout=timeout)
            raise AdapterError("authentication required")

        result = validator.validate_company(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse([], 401)]),
            adapters={"lever": authentication_adapter},
        )
        self.assertEqual(result["validation_status"], validator.AUTHENTICATION_REQUIRED)

    def test_22_browser_and_session_sources_are_rejected(self):
        browser = validator.validate_company(lever_target(browser_required=True), session_factory=FailingSessionFactory())
        session = validator.validate_company(lever_target(session_required=True), session_factory=FailingSessionFactory())
        self.assertEqual(browser["validation_status"], validator.BROWSER_REQUIRED)
        self.assertEqual(session["validation_status"], validator.SESSION_REQUIRED)

    def test_23_blocked_response_is_rejected(self):
        result = validator.validate_company(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse([], 403)]),
        )
        self.assertEqual(result["validation_status"], validator.BLOCKED)

    def test_24_repeated_page_terminates_with_valid_empty_view(self):
        payload = {"jobPostings": [posting(1)]}
        underlying = FakeSession([FakeResponse(payload), FakeResponse(payload)])
        session = batch.CachedBoundedSession(underlying)
        first = session.post("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs", json={"offset": 0})
        second = session.post("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs", json={"offset": 20})
        self.assertEqual(len(first.json()["jobPostings"]), 1)
        self.assertEqual(second.json()["jobPostings"], [])
        self.assertTrue(session.repeated_page_detected)

    def test_25_no_new_stable_id_terminates(self):
        underlying = FakeSession([
            FakeResponse({"jobPostings": [posting(1)]}),
            FakeResponse({"jobPostings": [posting(1, title="Different rendering")]}),
        ])
        session = batch.CachedBoundedSession(underlying)
        url = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
        session.post(url, json={"offset": 0})
        second = session.post(url, json={"offset": 20})
        self.assertEqual(second.json()["jobPostings"], [])
        self.assertTrue(session.no_new_stable_id_detected)

    def test_26_two_page_validation_limit(self):
        responses = [
            FakeResponse({"jobPostings": [posting(index) for index in range(20)]}),
            FakeResponse({"jobPostings": [posting(index) for index in range(20, 40)]}),
        ]
        fake = FakeSession(responses)
        result = batch.validate_candidate(workday_target(), session_factory=lambda: fake, sleep=lambda _seconds: None)
        self.assertEqual(result["pages_requested"], 2)
        self.assertEqual(len(fake.calls), 2)

    def test_27_twenty_job_inspection_limit(self):
        result = batch.validate_candidate(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse([lever_posting(index) for index in range(25)])]),
        )
        self.assertEqual(result["jobs_observed"], 20)
        self.assertEqual(result["normalized_jobs_inspected"], 20)

    def test_28_bounded_retry_behavior(self):
        fake = FakeSession([FakeResponse([], 503), FakeResponse([])])
        result = batch.validate_candidate(
            lever_target(),
            session_factory=lambda: fake,
            attempts=2,
            retry_delay=0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(result["status"], validator.VALIDATED_ZERO_JOBS)

    def test_29_production_scanner_dispatch_is_used(self):
        for platform in {entry["platform"] for entry in batch.CANDIDATE_ENTRIES}:
            self.assertIs(validator.ADAPTERS[platform], scanner.ADAPTERS[platform])

    def test_30_production_workday_adapter_is_used(self):
        self.assertIs(scanner.ADAPTERS["workday"], workday.fetch_jobs)

    def test_31_production_lever_adapter_is_used(self):
        self.assertIs(scanner.ADAPTERS["lever"], lever.fetch_jobs)

    def test_32_no_company_name_adapter_branches(self):
        source = "\n".join((inspect.getsource(workday), inspect.getsource(lever), inspect.getsource(scanner)))
        for company in EXPECTED:
            self.assertNotIn(company, source)

    def test_33_validation_batch_size_is_capped_at_five(self):
        self.assertEqual(batch.MAX_BATCH_SIZE, 5)
        chunks = [batch.CANDIDATE_ENTRIES[index:index + batch.MAX_BATCH_SIZE] for index in range(0, 6, 5)]
        self.assertEqual([len(chunk) for chunk in chunks], [5, 1])

    def _zero_job_validation(self):
        return batch.validate_candidate(
            lever_target(),
            session_factory=lambda: FakeSession([FakeResponse([])]),
        )

    def test_34_no_supabase_initialization(self):
        from src.integrations.supabase_job_store import SupabaseJobStore
        with patch.object(SupabaseJobStore, "from_env", side_effect=AssertionError) as initialize:
            self._zero_job_validation()
        initialize.assert_not_called()

    def test_35_no_google_sheets_initialization(self):
        from src.integrations.google_sheets_publisher import GoogleSheetsPublisher
        with patch.object(GoogleSheetsPublisher, "from_env", side_effect=AssertionError) as initialize:
            self._zero_job_validation()
        initialize.assert_not_called()

    def test_36_no_sqlite_initialization(self):
        with patch.object(sqlite3, "connect", side_effect=AssertionError) as initialize:
            self._zero_job_validation()
        initialize.assert_not_called()

    def test_37_production_contains_exact_promotions_and_remains_append_only(self):
        _, production = validator.load_registry(batch.PRODUCTION)
        _, validated = validator.load_registry(batch.VALIDATED_REGISTRY)
        additions = validator.detect_additions(historical_baseline_entries(), validated)
        production_names = [validator._name_key(entry) for entry in production]
        production_sources = [validator.source_key(entry) for entry in production]
        production_sources = [source for source in production_sources if source]

        self.assertEqual(len(production_names), len(set(production_names)))
        self.assertEqual(len(production_sources), len(set(production_sources)))
        self.assertEqual([configured_source_identity(entry) for entry in additions], EXPECTED_SOURCE_IDENTITIES)
        for addition in additions:
            matches = [
                entry for entry in production
                if validator._name_key(entry) == validator._name_key(addition)
            ]
            self.assertEqual(matches, [addition])

        workday_sites = [
            (entry["tenant"].casefold(), entry["site_id"].casefold())
            for entry in production if entry["platform"] == "workday"
        ]
        lever_boards = [
            entry["slug"].casefold()
            for entry in production if entry["platform"] == "lever"
        ]
        self.assertEqual(len(workday_sites), len(set(workday_sites)))
        self.assertEqual(len(lever_boards), len(set(lever_boards)))
        self.assertTrue(
            normalized_newlines(batch.PRODUCTION.read_bytes()).startswith(
                normalized_newlines(batch.VALIDATED_REGISTRY.read_bytes())
            )
        )

    def test_38_parent_mapping_configuration_remains_unchanged(self):
        digest = hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper()
        self.assertEqual(digest, PARENT_MAPPINGS_SHA256)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)

    def test_39_request_cache_avoids_duplicate_network_call(self):
        fake = FakeSession([FakeResponse([])])
        session = batch.CachedBoundedSession(fake)
        session.get("https://api.lever.co/v0/postings/acme?mode=json")
        session.get("https://api.lever.co/v0/postings/acme?mode=json")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(session.cache_hits, 1)

    def test_40_workday_uses_path_corroborated_requisition_field(self):
        self.assertEqual(
            workday._stable_job_id(
                ["Spotlight Job", "JR0281513"],
                "/job/US-Oregon-Hillsboro/Engineer_JR0281513",
            ),
            "JR0281513",
        )
        self.assertEqual(
            workday._stable_job_id(["JR1"], "/job/Dublin/Engineer_JR1"),
            "JR1",
        )

    def test_41_workday_locale_behavior_is_verified_from_apply_path(self):
        result = batch.validate_candidate(
            workday_target(max_pages=1),
            session_factory=lambda: FakeSession([FakeResponse({"jobPostings": [posting(1)]})]),
        )
        self.assertEqual(result["locale_behavior_result"], "valid_en-US_detail_path")


if __name__ == "__main__":
    unittest.main()
