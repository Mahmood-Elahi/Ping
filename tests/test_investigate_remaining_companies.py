"""Fixture-only tests for second-pass remaining-company research."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.fingerprint_company_platforms import PARENT_SOURCE_RULES
from scripts.investigate_remaining_companies import (
    BoundedResearchClient,
    DetailedEvidence,
    EXISTING_ADAPTER_FAMILIES,
    FINGERPRINT_FIELDS,
    build_clusters,
    build_record,
    career_score,
    endpoint_variant,
    merge_batch,
    normalize_company_name,
    parent_candidates,
    refresh_record_metadata,
    reconcile_companies,
    technical_score,
)


def source(company: str = "Acme", rank: str = "10", url: str = "https://careers.acme.example/jobs") -> dict[str, str]:
    return {
        "Rank": rank,
        "Company": company,
        "Careers URL": url,
        "Priority": "TBD",
        "Notes": "",
        "Official Careers URL": url,
        "Official Job Search URL": url,
    }


def fetched(url: str = "https://careers.acme.example/jobs", body: str = "<html><title>Jobs</title></html>", status: int = 200) -> DetailedEvidence:
    return DetailedEvidence(
        requested_url=url,
        final_url=url,
        status_code=status,
        redirect_chain=[{"url": url, "status": status}],
        content_type="text/html",
        text=body,
        title="Jobs",
        note=f"HTTP {status}",
        headers={},
    )


class FakeClient:
    def __init__(self, evidence: DetailedEvidence | None = None) -> None:
        self.evidence = evidence or fetched()
        self.calls: list[str] = []

    def fetch(self, url: str) -> DetailedEvidence:
        self.calls.append(url)
        item = self.evidence
        if item.requested_url == "https://careers.acme.example/jobs" and url != item.requested_url:
            return DetailedEvidence(**{**item.__dict__, "requested_url": url, "final_url": url})
        return item


class ReconciliationTests(unittest.TestCase):
    def test_reconciliation_original_against_registry(self):
        rows, report = reconcile_companies([source("Acme"), source("Beta")], [{"name": "Acme", "platform": "greenhouse"}])
        self.assertEqual((report.original_count, report.covered_count, report.uncovered_count), (2, 1, 1))
        self.assertIsNotNone(rows[0]["_production"])

    def test_company_name_normalization(self):
        self.assertEqual(normalize_company_name("  NVIDIA, Inc.  "), "nvidia inc")
        self.assertEqual(normalize_company_name("A&B"), "a and b")

    def test_parent_brand_normalization_has_no_substring_match(self):
        _, report = reconcile_companies([source("Meta Materials")], [{"name": "Meta", "platform": "meta"}])
        self.assertEqual(report.covered_count, 0)

    def test_covered_versus_uncovered(self):
        rows, _ = reconcile_companies([source("Acme"), source("Beta")], [{"name": "Acme", "platform": "lever"}])
        self.assertEqual([row["_production"] is None for row in rows], [False, True])

    def test_production_only_companies(self):
        _, report = reconcile_companies([source("Acme")], [{"name": "Acme"}, {"name": "Gamma"}])
        self.assertEqual(report.production_only, ["Gamma"])

    def test_ambiguous_mapping_reporting(self):
        _, report = reconcile_companies(
            [source("Acme Inc")],
            [{"name": "Acme LLC"}, {"name": "Acme Corp"}],
        )
        self.assertEqual(report.covered_count, 0)
        self.assertEqual(report.ambiguous[0]["company"], "Acme Inc")


class InvestigationTests(unittest.TestCase):
    def test_existing_fingerprint_preservation(self):
        previous = {"Detected Platform Family": "icims", "Confidence": "high"}
        row = build_record(source(), fetched(), previous, "2026-08-04T00:00:00+00:00")
        self.assertEqual(row["previous_platform_family"], "icims")
        self.assertEqual(row["previous_confidence"], "high")

    def test_resume_behavior(self):
        completed = {field: "" for field in FINGERPRINT_FIELDS}
        completed.update({"company": "Acme", "current_classification": "unknown"})
        client = FakeClient()
        rows = merge_batch([source()], [completed], {}, client, resume=True)
        self.assertEqual(rows[0]["current_classification"], "unknown")
        self.assertEqual(client.calls, [])

    def test_batch_size_maximum(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 25"):
            merge_batch([source()], [], {}, FakeClient(), batch_size=26)

    def test_deterministic_output_ordering(self):
        sources = [source("Beta", "2"), source("Acme", "1")]
        rows = merge_batch(sources, [], {}, FakeClient(), timestamp="fixed")
        self.assertEqual([row["company"] for row in rows], ["Beta", "Acme"])

    def test_redirect_recording(self):
        evidence = fetched("https://jobs.acme.example")
        evidence.redirect_chain = [
            {"url": "https://acme.example/careers", "status": 302, "location": "https://jobs.acme.example"},
            {"url": "https://jobs.acme.example", "status": 200},
        ]
        row = build_record(source(url="https://jobs.acme.example"), evidence, {}, "fixed")
        self.assertIn('"status": 302', row["redirect_chain"])

    def test_platform_evidence_recording(self):
        url = "https://jobs.lever.co/acme"
        row = build_record(source(url=url), fetched(url), {}, "fixed")
        self.assertEqual(row["platform_family"], "lever")
        self.assertIn("host matched", row["evidence"])

    def test_existing_adapter_family_detection(self):
        self.assertTrue({"greenhouse", "workday", "phenom"}.issubset(EXISTING_ADAPTER_FAMILIES))

    def test_parent_company_source_classification(self):
        url = "https://www.uber.com/us/en/careers/list/?team=UberEats"
        row = build_record(source("Uber Eats", url=url), fetched(url), {}, "fixed")
        self.assertEqual(row["current_classification"], "parent_company_source")
        self.assertEqual(row["parent_company"], "Uber")

    def test_x_redirect_is_parent_source_candidate(self):
        url = "https://careers.x.com/en"
        evidence = fetched("https://x.ai/careers")
        evidence.requested_url = url
        row = build_record(source("X", url=url), evidence, {}, "fixed")
        self.assertEqual(row["current_classification"], "parent_company_source")
        self.assertEqual(row["parent_company"], "xAI")

    def test_browser_required_classification(self):
        body = '<html><div id="root"></div><noscript>Enable JavaScript</noscript></html>'
        row = build_record(source(), fetched(body=body), {}, "fixed")
        self.assertEqual(row["current_classification"], "browser_required")

    def test_stale_fingerprint_classification(self):
        url = "https://jobs.lever.co/acme"
        row = build_record(source(url=url), fetched(url), {"Detected Platform Family": "workday"}, "fixed")
        self.assertEqual(row["stale_fingerprint"], "true")

    def test_invalid_prior_source_is_stale_even_when_family_matches(self):
        row = {
            "company": "Acme", "platform_family": "greenhouse",
            "current_classification": "invalid_careers_url", "stale_fingerprint": "false",
        }
        refreshed = refresh_record_metadata(
            [row], {"acme": {"Detected Platform Family": "greenhouse", "Confidence": "high"}},
        )
        self.assertEqual(refreshed[0]["stale_fingerprint"], "true")

    def test_invalid_careers_url_classification(self):
        row = build_record(source(url=""), DetailedEvidence("", note="Missing careers URL", redirect_chain=[]), {}, "fixed")
        self.assertEqual(row["current_classification"], "invalid_careers_url")

    def test_inactive_company_classification(self):
        row = build_record(source(), fetched(body="<html>The company has ceased operations.</html>"), {}, "fixed")
        self.assertEqual(row["current_classification"], "inactive_company")

    def test_custom_rest_detection(self):
        body = '<script>fetch("/api/v2/jobs/openings")</script>'
        row = build_record(source(), fetched(body=body), {}, "fixed")
        self.assertEqual(row["platform_family"], "custom_rest_api")

    def test_custom_graphql_detection(self):
        body = '<script>const endpoint="/graphql"; ApolloClient; query Jobs { jobs { id } }</script>'
        row = build_record(source(), fetched(body=body), {}, "fixed")
        self.assertEqual(row["platform_family"], "custom_graphql")

    def test_embedded_data_detection(self):
        body = '<script id="__NEXT_DATA__">{"jobs":[{"jobId":"J42","applyUrl":"https://acme.example/apply"}]}</script>'
        row = build_record(source(), fetched(body=body), {}, "fixed")
        self.assertEqual(row["platform_family"], "javascript_embedded_data")

    def test_static_listing_detection(self):
        body = '<a href="/jobs/engineer-123">Engineer</a><a href="/jobs/designer-456">Designer</a>'
        row = build_record(source(), fetched(body=body), {}, "fixed")
        self.assertEqual(row["platform_family"], "static_html")

    def test_anti_bot_classification(self):
        row = build_record(source(), fetched(body="<title>Attention Required! | Cloudflare</title>", status=403), {}, "fixed")
        self.assertEqual(row["current_classification"], "blocked")
        self.assertTrue(row["anti_bot_or_access_block_evidence"])

    def test_authentication_required_classification(self):
        row = build_record(source(), fetched(body="Sign in to view jobs", status=401), {}, "fixed")
        self.assertEqual(row["current_classification"], "authentication_required")
        self.assertEqual(row["authentication_required"], "true")

    def test_stable_job_id_evidence(self):
        url = "https://jobs.lever.co/acme"
        row = build_record(source(url=url), fetched(url), {}, "fixed")
        self.assertEqual(row["stable_job_id_evidence"], "true")

    def test_apply_url_evidence(self):
        url = "https://jobs.ashbyhq.com/acme"
        row = build_record(source(url=url), fetched(url), {}, "fixed")
        self.assertEqual(row["official_apply_url_evidence"], "true")

    def test_cluster_construction(self):
        first = build_record(source("Acme", url="https://jobs.lever.co/acme"), fetched("https://jobs.lever.co/acme"), {}, "fixed")
        second = build_record(source("Beta", url="https://jobs.lever.co/beta"), fetched("https://jobs.lever.co/beta"), {}, "fixed")
        clusters = build_clusters([first, second])
        self.assertEqual(clusters[0]["company_count"], "2")
        self.assertEqual(clusters[0]["estimated_companies_unlocked"], "2")

    def test_priority_score_determinism(self):
        url = "https://jobs.lever.co/acme"
        one = build_record(source(url=url), fetched(url), {}, "fixed")
        two = build_record(source(url=url), fetched(url), {}, "fixed")
        self.assertEqual((one["technical_priority"], one["career_relevance_priority"], one["combined_priority"]),
                         (two["technical_priority"], two["career_relevance_priority"], two["combined_priority"]))


class SafetyTests(unittest.TestCase):
    def test_no_production_registry_modification(self):
        registry = Path("config/target_companies.yaml")
        before = registry.read_bytes()
        reconcile_companies([source("Acme")], [{"name": "Acme"}])
        self.assertEqual(registry.read_bytes(), before)

    def test_no_supabase_initialization(self):
        text = Path("scripts/investigate_remaining_companies.py").read_text(encoding="utf-8")
        self.assertNotIn("supabase_job_store", text.casefold())

    def test_no_google_sheets_initialization(self):
        text = Path("scripts/investigate_remaining_companies.py").read_text(encoding="utf-8")
        self.assertNotIn("google_sheets_publisher", text.casefold())

    def test_no_sqlite_initialization(self):
        text = Path("scripts/investigate_remaining_companies.py").read_text(encoding="utf-8")
        self.assertNotIn("sqlite", text.casefold())

    def test_parent_candidate_safety_distinction(self):
        safe_url = "https://www.uber.com/us/en/careers/list/?team=UberEats"
        safe = build_record(source("Uber Eats", url=safe_url), fetched(safe_url), {}, "fixed")
        uncertain_url = "https://careers.google.com/jobs/results/?q=Google%20DeepMind"
        uncertain = build_record(source("Google DeepMind", url=uncertain_url), fetched(uncertain_url), {}, "fixed")
        candidates = parent_candidates([safe, uncertain])
        safety = {row["company"]: row["mapping_safety"] for row in candidates}
        self.assertEqual(safety["Uber Eats"], "safe")
        self.assertEqual(safety["Google DeepMind"], "manual_review")

    def test_client_enforces_bounded_parameters(self):
        with self.assertRaises(ValueError):
            BoundedResearchClient(request_delay=0, timeout=61, max_redirects=5)
        with self.assertRaises(ValueError):
            BoundedResearchClient(request_delay=0, timeout=5, max_redirects=11)

    def test_endpoint_variant_is_specific(self):
        url = "https://jobs.lever.co/acme"
        record = build_record(source(url=url), fetched(url), {}, "fixed")
        self.assertEqual(record["platform_variant"], "postings_api_v0")

    def test_parent_rules_are_explicit(self):
        self.assertIn("googledeepmind", PARENT_SOURCE_RULES)
        self.assertNotIn("deep", PARENT_SOURCE_RULES)

    def test_career_priority_does_not_depend_on_job_counts(self):
        self.assertGreater(career_score(source(rank="1")), career_score(source(rank="246")))


if __name__ == "__main__":
    unittest.main()
