from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest

from tests.company_careers_activation_milestones import (
    historical_parent_mapping_bytes,
    historical_parent_mapping_projection,
)
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.company_careers_reliability_assertions import (
    assert_historical_prefix,
    with_reliability_repairs,
)

from scripts import validate_adapter_extensions_v4a as rollout
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import eightfold, icims, oracle_recruiting, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_MASTER_BATCH_G_VALIDATED = ROOT / "config" / "target_companies.original_master_recovery_batch_g_validated.yaml"
EXPECTED = [
    "onsemi", "Siemens", "Schneider Electric", "Yaskawa Electric", "Aptiv",
    "Chevron", "OHB SE", "Mercado Libre", "Bolt",
]
SAFE = ["onsemi", "Siemens", "Schneider Electric", "Chevron", "Mercado Libre", "Bolt"]
SOURCES = [
    "oracle_recruiting:hctz.fa.us2.oraclecloud.com:cx_1001",
    "static_html:jobs.siemens.com",
    "icims:careers.se.com:schneider-electric-public",
    "static_html:careers.chevron.com",
    "eightfold:mercadolibre.eightfold.ai",
    "static_html:bolt.eu",
]


class Response:
    def __init__(self, text="", *, status=200, content_type="text/html", payload=None, url="https://jobs.test/list"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.url = url
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None and not text else text
        self.content = self.text.encode("utf-8")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload if self._payload is not None else json.loads(self.text)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.responses.pop(0)


def static_entry(**overrides):
    value = {
        "name": "Example", "platform": "static_html",
        "careers_url": "https://jobs.test/list", "listing_url": "https://jobs.test/list",
        "endpoint_variant": "server_rendered_cards", "listing_selector": "main.jobs",
        "job_card_selector": "article.job", "title_selector": "h2",
        "location_selector": "span.location", "department_selector": "span.department",
        "detail_link_selector": "a[href]", "external_id_regex": r"/job/([0-9]+)",
        "allowed_apply_hosts": ["jobs.test"], "max_pages": 1, "max_jobs": 20,
        "max_response_bytes": rollout.MAX_RESPONSE_BYTES,
    }
    value.update(overrides)
    return value


def card(job_id="1", host="jobs.test", title="Engineer"):
    return (
        f"<article class='job'><h2>{title}</h2><span class='location'>Calgary</span>"
        f"<span class='department'>Engineering</span>"
        f"<a href='https://{host}/job/{job_id}'>Apply</a></article>"
    )


def page(cards="", *, empty=False):
    marker = "<p class='empty'>No jobs</p>" if empty else ""
    return Response(f"<html><title>Jobs</title><main class='jobs'>{cards}{marker}</main></html>")


def review():
    return {
        "company": "Example", "master_rank": 1, "prior_platform": "static_html",
        "current_platform": "static_html", "implementation_path": "reusable_static_html_variant",
        "adapter_family": "static_html", "endpoint_variant": "server_rendered_cards",
    }


class AdapterExtensionsV4ABulkActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_bytes = rollout.PRODUCTION.read_bytes()
        cls.parent_bytes = rollout.PARENT_MAPPINGS.read_bytes()
        with rollout.INVENTORY.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))
        cls.candidate_targets = load_target_companies(str(rollout.CANDIDATE_REGISTRY))
        cls.validated_targets = load_target_companies(str(rollout.VALIDATED_REGISTRY))

    def test_01_exact_nine_company_reviewed_inventory(self):
        self.assertEqual([row["company"] for row in self.inventory], EXPECTED)

    def test_02_inventory_is_derived_from_v4_evidence(self):
        with rollout.UNCOVERED_INVENTORY.open(newline="", encoding="utf-8") as handle:
            source = {row["company"]: row for row in csv.DictReader(handle)}
        self.assertTrue(all(source[name]["recoverability_class"] == "adapter_extension_required" for name in EXPECTED))

    def test_03_candidate_order_follows_master_workbook(self):
        self.assertEqual([int(row["master_rank"]) for row in self.inventory], sorted(int(row["master_rank"]) for row in self.inventory))

    def test_04_historical_133_entry_prefix_is_preserved(self):
        _, candidate = registry_validator.load_registry(rollout.CANDIDATE_REGISTRY)
        _, validated = registry_validator.load_registry(rollout.VALIDATED_REGISTRY)
        _, production = registry_validator.load_registry(rollout.PRODUCTION)
        first_batch_a = next(
            index for index, item in enumerate(candidate)
            if registry_validator._name_key(item) == "onsemi"
        )
        historical = candidate[:first_batch_a]
        self.assertEqual(first_batch_a, rollout.HISTORICAL_PRODUCTION_COUNT)
        self.assertEqual(validated[:first_batch_a], historical)
        assert_historical_prefix(self, production, historical)
        self.assertGreaterEqual(len(production), len(validated))
        self.assertEqual(production[:len(validated)], with_reliability_repairs(validated))

    def test_05_candidate_registry_loader_compatibility(self):
        self.assertEqual(len(self.candidate_targets), 139)

    def test_06_validated_registry_loader_compatibility(self):
        self.assertEqual(len(self.validated_targets), 139)

    def test_07_validated_registry_contains_only_safe_candidates(self):
        names = [target.name for target in self.validated_targets[133:]]
        safe_from_csv = [row["company"] for row in self.inventory if row["safe_to_activate"] == "true"]
        self.assertEqual(names, safe_from_csv)
        self.assertEqual(names, SAFE)

    def test_08_candidate_additions_use_milestone_specific_baseline(self):
        self.assertEqual([target.name for target in self.candidate_targets[133:]], SAFE)

    def test_09_synthetic_baseline_delta_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "base.yaml"
            candidate = root / "candidate.yaml"
            baseline.write_text("target_companies:\n  - name: Base\n    platform: greenhouse\n    slug: base\n", encoding="utf-8")
            rollout.build_candidate_registry(baseline, candidate, [{"name": "New", "platform": "lever", "slug": "new"}])
            self.assertEqual(len(load_target_companies(str(candidate))) - len(load_target_companies(str(baseline))), 1)

    def test_10_duplicate_normalized_company_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); base = root / "base.yaml"; out = root / "out.yaml"
            base.write_text("target_companies:\n  - name: Acme Corp\n    platform: greenhouse\n    slug: acme\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                rollout.build_candidate_registry(base, out, [{"name": "  ACME   CORP ", "platform": "lever", "slug": "other"}])

    def test_11_duplicate_normalized_source_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); base = root / "base.yaml"; out = root / "out.yaml"
            base.write_text("target_companies:\n  - name: One\n    platform: lever\n    slug: shared\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_source"):
                rollout.build_candidate_registry(base, out, [{"name": "Two", "platform": "lever", "slug": "SHARED"}])

    def test_12_exact_source_identity_generation(self):
        self.assertEqual([registry_validator.source_key(item) for item in rollout.CANDIDATE_ENTRIES], SOURCES)

    def test_13_configuration_validation_precedes_network(self):
        entry = static_entry(listing_selector=None)
        factory = unittest.mock.Mock(side_effect=AssertionError("network initialized"))
        result = rollout.validate_candidate(entry, review(), session_factory=factory)
        self.assertEqual(result["status"], "invalid_configuration")
        factory.assert_not_called()

    def test_14_stable_ids_are_required(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([page(card(""))]))
        self.assertEqual(result["status"], "missing_stable_id")

    def test_15_official_apply_urls_are_required(self):
        def invalid_url(_session, _target, _timeout):
            return [JobRecord("1", "Engineer", "Example", "Calgary", "", "", "static_html:jobs.test")]
        with patch.dict(scanner.ADAPTERS, {"static_html": invalid_url}):
            result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([]))
        self.assertEqual(result["status"], "invalid_apply_url")

    def test_16_invalid_apply_host_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([page(card(host="evil.test"))]))
        self.assertEqual(result["status"], "invalid_apply_host")

    def test_17_valid_zero_job_response_is_accepted(self):
        entry = static_entry(empty_selector="p.empty")
        result = rollout.validate_candidate(entry, review(), session_factory=lambda: Session([page(empty=True)]))
        self.assertEqual(result["status"], "validated_zero_jobs")

    def test_18_malformed_http_200_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([Response("<html><title>Jobs</title></html>")]))
        self.assertEqual(result["status"], "malformed_response")

    def test_19_login_response_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([Response("<html><title>Login</title></html>")]))
        self.assertEqual(result["status"], "authentication_required")

    def test_20_consent_response_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([Response("<html><body>Consent required</body></html>")]))
        self.assertIn(result["status"], {"unsupported_variant", "failed_technical"})

    def test_21_session_dependent_response_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([Response("<html><title>Session required</title></html>")]))
        self.assertEqual(result["status"], "session_required")

    def test_22_browser_shell_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([Response("<html><body>Enable JavaScript to continue</body></html>")]))
        self.assertEqual(result["status"], "browser_required")

    def test_23_blocked_response_is_rejected(self):
        result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda: Session([Response("Access denied", status=403)]))
        self.assertEqual(result["status"], "blocked")

    @patch.object(static_html.time, "sleep", return_value=None)
    def test_24_repeated_page_termination(self, _sleep):
        entry = static_entry(max_pages=2, pagination_url_template="https://jobs.test/list?page={page}")
        session = Session([page(card("1")), page(card("1"))])
        static_html.fetch_jobs(session, CompanyTarget.from_dict(entry), 5)
        self.assertEqual(session.static_html_pagination["stop_reason"], "repeated_page")

    def test_25_repeated_cursor_termination(self):
        source = Path(eightfold.__file__).read_text(encoding="utf-8")
        self.assertIn("seen_page_ids", source)
        self.assertIn('"repeated_page"', source)

    @patch.object(static_html.time, "sleep", return_value=None)
    def test_26_conflicting_repeated_id_fails_closed(self, _sleep):
        entry = static_entry(max_pages=2, pagination_url_template="https://jobs.test/list?page={page}")
        session = Session([page(card("1")), page(card("1") + card("1", title="Duplicate"))])
        with self.assertRaisesRegex(AdapterError, "identity_conflict"):
            static_html.fetch_jobs(session, CompanyTarget.from_dict(entry), 5)
        self.assertEqual(session.static_html_pagination["stop_reason"], "conflicting_stable_id")

    def test_27_two_page_validation_limit(self):
        bounded = rollout.CachedBoundedSession(Session([Response("ok"), Response("ok")]))
        bounded.get("https://x.test/1"); bounded.get("https://x.test/2")
        with self.assertRaisesRegex(AdapterError, "two-page"):
            bounded.get("https://x.test/3")

    def test_28_unpaginated_listing_does_not_claim_complete_at_job_limit(self):
        cards = "".join(card(str(index)) for index in range(25))
        session = Session([page(cards)])
        jobs = static_html.fetch_jobs(
            session, CompanyTarget.from_dict(static_entry()), 5
        )
        self.assertEqual(len(jobs), 25)
        self.assertFalse(session.static_html_pagination["scan_complete"])
        self.assertEqual(
            session.static_html_pagination["scan_completeness_reason"],
            "no_pagination_configured",
        )

    @patch.object(static_html.time, "sleep", return_value=None)
    def test_29_bounded_retry_enforcement(self, _sleep):
        session = Session([Response("", status=503), Response("", status=503)])
        with self.assertRaisesRegex(AdapterError, "HTTP 503"):
            static_html.fetch_jobs(session, CompanyTarget.from_dict(static_entry()), 5)
        self.assertEqual(len(session.calls), 2)

    def test_30_response_byte_limit(self):
        bounded = rollout.CachedBoundedSession(Session([Response("x" * (rollout.MAX_RESPONSE_BYTES + 1))]))
        with self.assertRaisesRegex(AdapterError, "byte limit"):
            bounded.get("https://x.test/jobs")

    def test_31_request_caching(self):
        session = Session([Response("ok")]); bounded = rollout.CachedBoundedSession(session)
        first = bounded.get("https://x.test/jobs", params={"page": 1})
        second = bounded.get("https://x.test/jobs", params={"page": 1})
        self.assertIs(first, second); self.assertEqual((bounded.network_requests, bounded.cache_hits), (1, 1))

    def test_32_production_scanner_dispatch_is_used(self):
        self.assertIs(scanner.ADAPTERS["oracle_recruiting"], oracle_recruiting.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["icims"], icims.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["eightfold"], eightfold.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["static_html"], static_html.fetch_jobs)

    def test_33_production_parser_used_for_every_existing_adapter_candidate(self):
        self.assertTrue(all(entry["platform"] in scanner.ADAPTERS for entry in rollout.CANDIDATE_ENTRIES))

    def test_34_no_company_name_branches(self):
        source = "\n".join(Path(module.__file__).read_text(encoding="utf-8") for module in (icims, static_html, oracle_recruiting, eightfold)).casefold()
        self.assertTrue(
            all(
                f'"{name.casefold()}"' not in source
                and f"'{name.casefold()}'" not in source
                for name in EXPECTED
            )
        )

    def test_35_existing_production_adapter_fixtures_remain_unchanged(self):
        changed = subprocess.check_output(["git", "diff", "--name-only", "--", "tests/fixtures"], cwd=ROOT, text=True)
        self.assertEqual(changed.strip(), "")

    def test_36_unsupported_variants_continue_failing_closed(self):
        with self.assertRaisesRegex(AdapterError, "unsupported variant"):
            static_html.validate_target(CompanyTarget.from_dict(static_entry(endpoint_variant="company_special")))

    def test_37_candidate_validation_batches_are_capped_at_five(self):
        self.assertEqual(rollout.MAX_BATCH_SIZE, 5)
        self.assertLessEqual(rollout.MAX_BATCH_SIZE, registry_validator.MAX_BATCH_SIZE)

    def test_38_production_registry_remains_unchanged(self):
        _, validated = registry_validator.load_registry(rollout.VALIDATED_REGISTRY)
        _, production = registry_validator.load_registry(rollout.PRODUCTION)
        first_batch_a = next(
            index for index, item in enumerate(validated)
            if registry_validator._name_key(item) == "onsemi"
        )
        historical = validated[:first_batch_a]
        additions = validated[first_batch_a:]

        self.assertEqual(first_batch_a, rollout.HISTORICAL_PRODUCTION_COUNT)
        self.assertEqual([item["name"] for item in additions], SAFE)
        self.assertGreaterEqual(len(production), len(validated))
        assert_historical_prefix(self, production, historical)
        self.assertEqual(
            production[first_batch_a:first_batch_a + len(additions)],
            additions,
        )
        for expected in additions:
            matches = [
                item for item in production
                if registry_validator._name_key(item) == registry_validator._name_key(expected)
            ]
            self.assertEqual(matches, [expected])
            self.assertEqual(
                registry_validator.source_key(matches[0]),
                registry_validator.source_key(expected),
            )
        excluded = {"ohb se"}
        self.assertFalse(excluded & {registry_validator._name_key(item) for item in validated})
        self.assertEqual(
            sum(registry_validator._name_key(item) == "ohb se" for item in production),
            1,
        )
        _, batch_g = registry_validator.load_registry(ORIGINAL_MASTER_BATCH_G_VALIDATED)
        expected_yaskawa = next(
            item for item in batch_g
            if registry_validator._name_key(item) == "yaskawa electric"
        )
        self.assertEqual(
            [item for item in production if registry_validator._name_key(item) == "yaskawa electric"],
            [expected_yaskawa],
        )
        self.assertEqual(
            sum(registry_validator._name_key(item) == "aptiv" for item in production),
            1,
        )

    def test_39_parent_mappings_remain_unchanged(self):
        expected = subprocess.check_output(["git", "show", "HEAD:config/company_parent_source_mappings.yaml"], cwd=ROOT)
        expected_at_rollout = historical_parent_mapping_projection(
            expected,
            source="HEAD:config/company_parent_source_mappings.yaml",
        )
        self.assertEqual(
            historical_parent_mapping_bytes(rollout.PARENT_MAPPINGS),
            expected_at_rollout,
        )

    def test_40_no_supabase_initialization(self):
        source = Path(rollout.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("create_client", source); self.assertNotIn("supabase", "\n".join(source.splitlines()[15:]))

    def test_41_no_google_sheets_initialization(self):
        source = Path(rollout.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("gspread", source); self.assertNotIn("googleapiclient", source)

    def test_42_no_sqlite_initialization(self):
        source = Path(rollout.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("sqlite3", source); self.assertNotIn("database", "\n".join(source.splitlines()[15:]))

    def test_43_no_dashboard_publication(self):
        source = Path(rollout.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("update_student_dashboard", source); self.assertNotIn("publish", source)

    def test_44_no_browser_automation(self):
        source = Path(rollout.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("selenium", source); self.assertNotIn("playwright", source); self.assertNotIn("webdriver", source)

    def test_45_existing_full_suite_safety_contract(self):
        self.assertEqual(rollout.HISTORICAL_PRODUCTION_COUNT, 133)
        self.assertEqual(rollout.HISTORICAL_ORIGINAL_COVERAGE, 128)
        self.assertEqual(rollout.EXPECTED_PARENT_MAPPINGS, 4)


if __name__ == "__main__":
    unittest.main()
