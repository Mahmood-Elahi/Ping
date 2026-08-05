from __future__ import annotations

import csv
import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import validate_adapter_extensions_v4b as rollout
from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import lever, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


EXPECTED = [
    "IMC Trading", "Valve", "Niantic", "Roblox", "Zoox",
    "Unitree Robotics", "LINE", "Match Group",
]
SAFE = ["IMC Trading", "Valve", "Roblox", "Zoox", "Unitree Robotics", "Match Group"]
SOURCES = [
    "static_html:www.imc.com", "static_html:www.valvesoftware.com",
    "static_html:careers.roblox.com", "lever:zoox",
    "static_html:www.unitree.com", "lever:matchgroup",
]


def historical_prefix_bytes(path: Path) -> bytes:
    first_batch_b = registry_validator._name_key({"name": SAFE[0]})
    lines = path.read_bytes().splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.decode("utf-8").strip()
        if not stripped.startswith("- name:"):
            continue
        parsed = yaml.safe_load(stripped)
        if isinstance(parsed, list) and registry_validator._name_key(parsed[0]) == first_batch_b:
            return b"".join(lines[:index])
    raise AssertionError(f"Batch B boundary {SAFE[0]!r} not found in {path}")


class Response:
    def __init__(self, value="", *, status=200, content_type=None, url="https://jobs.test/list"):
        self.status_code = status
        self.url = url
        self._value = value
        if isinstance(value, (dict, list)):
            self.text = json.dumps(value)
            content_type = content_type or "application/json"
        else:
            self.text = str(value)
            content_type = content_type or "text/html"
        self.content = self.text.encode()
        self.headers = {"content-type": content_type}

    def json(self):
        if isinstance(self._value, Exception):
            raise self._value
        if isinstance(self._value, (dict, list)):
            return self._value
        return json.loads(self.text)


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


def lever_entry(**overrides):
    value = {
        "name": "Example", "platform": "lever", "slug": "example",
        "careers_url": "https://example.test/careers", "search_url": "https://jobs.lever.co/example",
        "endpoint_variant": "postings_api_v0", "allowed_apply_hosts": ["jobs.lever.co"],
        "max_pages": 1, "max_jobs": 20, "max_response_bytes": rollout.MAX_RESPONSE_BYTES,
    }
    value.update(overrides)
    return value


def card(job_id="1", host="jobs.test", title="Engineer"):
    return (
        f"<article class='job'><h2>{title}</h2><span class='location'>Calgary</span>"
        f"<span class='department'>Engineering</span>"
        f"<a href='https://{host}/job/{job_id}'>Apply</a></article>"
    )


def page(cards="", *, empty=False, title="Jobs"):
    marker = "<p class='empty'>No jobs</p>" if empty else ""
    return Response(f"<html><title>{title}</title><main class='jobs'>{cards}{marker}</main></html>")


def posting(job_id="abc", *, board="example", department="Engineering", host="jobs.lever.co"):
    return {
        "id": job_id, "text": "Engineer", "hostedUrl": f"https://{host}/{board}/{job_id}",
        "createdAt": 1, "categories": {"department": department, "team": "Core", "location": "Remote"},
    }


def review(platform="static_html"):
    return {
        "company": "Example", "master_rank": 1, "prior_platform": platform,
        "current_platform": platform, "implementation_path": "existing_adapter_variant_extension",
        "adapter_family": platform, "endpoint_variant": "test_variant",
    }


class AdapterExtensionsV4BArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with rollout.INVENTORY.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))
        cls.report = json.loads(rollout.REPORT.read_text(encoding="utf-8"))
        cls.candidates = load_target_companies(str(rollout.CANDIDATE_REGISTRY))
        cls.validated = load_target_companies(str(rollout.VALIDATED_REGISTRY))
        cls.production = load_target_companies(str(rollout.PRODUCTION))

    def test_01_exact_eight_company_inventory_and_master_order(self):
        self.assertEqual([row["company"] for row in self.inventory], EXPECTED)
        self.assertEqual([int(row["master_rank"]) for row in self.inventory], sorted(int(row["master_rank"]) for row in self.inventory))

    def test_02_inventory_is_derived_from_v4_evidence(self):
        rollout.reconcile_candidates()
        self.assertEqual(rollout.EXPECTED_COMPANIES, EXPECTED)

    def test_03_exact_historical_139_entry_prefix(self):
        candidate_prefix = historical_prefix_bytes(rollout.CANDIDATE_REGISTRY)
        validated_prefix = historical_prefix_bytes(rollout.VALIDATED_REGISTRY)
        historical = (yaml.safe_load(candidate_prefix.decode("utf-8")) or {})["target_companies"]
        _, production = registry_validator.load_registry(rollout.PRODUCTION)
        _, milestone = registry_validator.load_registry(rollout.VALIDATED_REGISTRY)

        self.assertEqual(candidate_prefix, validated_prefix)
        self.assertEqual(len(historical), rollout.HISTORICAL_PRODUCTION_COUNT)
        self.assertEqual(production[:len(historical)], historical)

        promoted = milestone[len(historical):]
        self.assertEqual([entry["name"] for entry in promoted], SAFE)
        self.assertEqual([registry_validator.source_key(entry) for entry in promoted], SOURCES)
        production_sources = [registry_validator.source_key(entry) for entry in production]
        for expected in promoted:
            matches = [
                entry for entry in production
                if registry_validator._name_key(entry) == registry_validator._name_key(expected)
            ]
            self.assertEqual(matches, [expected])
            expected_source = registry_validator.source_key(expected)
            self.assertEqual(registry_validator.source_key(matches[0]), expected_source)
            self.assertEqual(production_sources.count(expected_source), 1)
        production_names = {registry_validator._name_key(entry) for entry in production}
        self.assertFalse({"niantic", "line"} & production_names)

    def test_04_registry_loader_compatibility(self):
        self.assertEqual((len(self.candidates), len(self.validated)), (145, 145))

    def test_05_candidate_and_validated_milestone_deltas(self):
        self.assertEqual([target.name for target in self.candidates[139:]], SAFE)
        self.assertEqual([target.name for target in self.validated[139:]], SAFE)

    def test_06_validated_registry_uses_only_safe_statuses(self):
        safe = [row["company"] for row in self.report["results"] if row["status"] in rollout.SAFE_STATUSES]
        self.assertEqual(safe, SAFE)

    def test_07_exclusions_are_explicit_and_not_activated(self):
        excluded = {row["company"]: row["validation_status"] for row in self.inventory if row["safe_to_activate"] == "false"}
        self.assertEqual(excluded, {"Niantic": "stale_source", "LINE": "browser_required"})

    def test_08_source_identities_are_unique(self):
        source_ids = [row["source_identity"] for row in self.inventory if row["source_identity"]]
        self.assertEqual(source_ids, SOURCES)
        self.assertEqual(len(source_ids), len(set(source_ids)))

    def test_09_duplicate_company_and_source_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base.yaml"
            out = Path(temp) / "out.yaml"
            base.write_text("target_companies:\n  - name: One\n    platform: lever\n    slug: shared\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                rollout.build_candidate_registry(base, out, [lever_entry(name=" one ", slug="other")])
            with self.assertRaisesRegex(ValueError, "duplicate_source"):
                rollout.build_candidate_registry(base, out, [lever_entry(name="Two", slug="SHARED")])

    def test_10_report_counts_and_projection(self):
        totals = self.report["totals"]
        self.assertEqual((totals["candidate_registry_count"], totals["validated_registry_count"]), (145, 145))
        self.assertEqual((totals["validated_with_jobs"], totals["excluded"]), (6, 2))
        self.assertEqual(totals["projected_original_company_coverage"], 140)

    def test_11_production_and_parent_mappings_unchanged(self):
        candidate_prefix = historical_prefix_bytes(rollout.CANDIDATE_REGISTRY)
        validated_prefix = historical_prefix_bytes(rollout.VALIDATED_REGISTRY)
        self.assertEqual(candidate_prefix, validated_prefix)
        self.assertEqual(self.report["production_registry_sha256"], hashlib.sha256(candidate_prefix).hexdigest().upper())
        self.assertEqual(self.report["parent_mappings_sha256"], hashlib.sha256(rollout.PARENT_MAPPINGS.read_bytes()).hexdigest().upper())

    def test_12_future_append_only_production_growth_does_not_change_milestone_delta(self):
        future = [vars(target) for target in self.production]
        future.append({"name": "Future Source", "platform": "lever", "slug": "future"})
        historical_prefix = future[:rollout.HISTORICAL_PRODUCTION_COUNT]
        _, milestone = registry_validator.load_registry(rollout.CANDIDATE_REGISTRY)
        self.assertEqual([entry["name"] for entry in milestone[len(historical_prefix):]], SAFE)


class AdapterExtensionsV4BContractTests(unittest.TestCase):
    def test_13_stable_ids_and_official_apply_urls(self):
        result = rollout.validate_candidate(
            static_entry(), review(), session_factory=lambda: Session([page(card())]),
        )
        self.assertEqual((result["status"], result["stable_id_result"], result["apply_url_result"]),
                         ("validated_with_jobs", "valid", "valid"))

    def test_14_invalid_apply_host_rejection(self):
        result = rollout.validate_candidate(
            static_entry(), review(), session_factory=lambda: Session([page(card(host="evil.test"))]),
        )
        self.assertEqual(result["status"], "invalid_apply_host")

    def test_15_malformed_http_200_rejection(self):
        result = rollout.validate_candidate(
            lever_entry(), review("lever"), session_factory=lambda: Session([Response({"not": "a list"})]),
        )
        self.assertEqual(result["status"], "malformed_response")

    def test_16_login_consent_session_browser_and_blocked_page_rejection(self):
        cases = [
            (page(title="Login"), "authentication_required"),
            (Response("<html>Cookie consent required</html>"), "unsupported_variant"),
            (page(title="Session Expired"), "session_required"),
            (Response("<html>Enable JavaScript to continue</html>"), "browser_required"),
            (Response("<html>Cloudflare challenge</html>"), "blocked"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                result = rollout.validate_candidate(static_entry(), review(), session_factory=lambda r=response: Session([r]))
                self.assertEqual(result["status"], expected)

    def test_17_valid_zero_job_handling(self):
        entry = static_entry(empty_selector="p.empty")
        result = rollout.validate_candidate(entry, review(), session_factory=lambda: Session([page(empty=True)]))
        self.assertEqual(result["status"], "validated_zero_jobs")
        self.assertTrue(result["safe_to_activate"])

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_18_pagination_and_repeated_page_termination(self):
        entry = static_entry(max_pages=2, pagination_url_template="https://jobs.test/list?page={page}")
        session = Session([page(card()), page(card())])
        result = rollout.validate_candidate(entry, review(), session_factory=lambda: session)
        self.assertEqual((result["pages_requested"], result["pagination_result"]), (2, "repeated_page"))

    @patch("src.scraper.company_careers.adapters.static_html.time.sleep", lambda _: None)
    def test_19_two_page_and_twenty_job_limits(self):
        first = "".join(card(str(index)) for index in range(1, 16))
        second = "".join(card(str(index)) for index in range(16, 31))
        entry = static_entry(max_pages=2, pagination_url_template="https://jobs.test/list?page={page}")
        result = rollout.validate_candidate(entry, review(), session_factory=lambda: Session([page(first), page(second)]))
        self.assertEqual((result["pages_requested"], result["normalized_jobs_inspected"]), (2, 20))

    def test_20_bounded_retries_caching_and_response_limits(self):
        session = Session([Response([], status=503), Response([])])
        with patch("src.scraper.company_careers.adapters.lever.time.sleep", lambda _: None):
            result = rollout.validate_candidate(lever_entry(), review("lever"), session_factory=lambda: session)
        self.assertEqual((len(session.calls), result["status"]), (2, "validated_zero_jobs"))
        cached = rollout.CachedBoundedSession(Session([Response([])]))
        cached.get("https://api.test/jobs")
        cached.get("https://api.test/jobs")
        self.assertEqual((cached.network_requests, cached.cache_hits), (1, 1))
        oversized = Response("x" * 30)
        with self.assertRaisesRegex(AdapterError, "byte limit"):
            limited = rollout.CachedBoundedSession(Session([oversized]))
            with patch.object(rollout, "MAX_RESPONSE_BYTES", 10):
                limited.get("https://api.test/large")

    def test_21_production_parser_and_dispatch_usage(self):
        self.assertIs(scanner.ADAPTERS["lever"], lever.fetch_jobs)
        self.assertIs(scanner.ADAPTERS["static_html"], static_html.fetch_jobs)

    def test_22_no_company_specific_adapter_branches(self):
        source = "\n".join((inspect.getsource(lever), inspect.getsource(static_html), inspect.getsource(scanner)))
        for company in EXPECTED:
            self.assertNotIn(company.casefold(), source.casefold())

    def test_23_unsupported_variants_fail_closed(self):
        with self.assertRaisesRegex(AdapterError, "unsupported variant"):
            lever.validate_target(CompanyTarget.from_dict(lever_entry(endpoint_variant="future_v9")))
        with self.assertRaisesRegex(AdapterError, "unsupported variant"):
            static_html.validate_target(CompanyTarget.from_dict(static_entry(endpoint_variant="future_shell")))

    def test_24_no_external_service_or_browser_initialization(self):
        source = inspect.getsource(rollout).casefold()
        for forbidden in ("supabase", "gspread", "sqlite3", "playwright", "selenium", "webdriver"):
            self.assertNotIn(f"import {forbidden}", source)
        self.assertEqual(rollout.MAX_BATCH_SIZE, 5)

    def test_25_department_filter_does_not_misattribute_portfolio_brands(self):
        entry = lever_entry(slug="matchgroup", department_allowlist=["Match Group"])
        response = Response([
            posting("corp", board="matchgroup", department="Match Group"),
            posting("brand", board="matchgroup", department="Tinder"),
        ])
        result = rollout.validate_candidate(entry, review("lever"), session_factory=lambda: Session([response]))
        self.assertEqual((result["status"], result["jobs_observed"]), ("validated_with_jobs", 1))


if __name__ == "__main__":
    unittest.main()
