"""Tests for safe company-watchlist enrichment and importing."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml

from scripts.company_watchlist_common import ENRICHED_FIELDS, RESEARCH_FIELDS, detect_ats, write_csv
from scripts.enrich_company_watchlist import enrich_rows
from scripts.import_company_watchlist import (
    build_generated_registry,
    dump_registry,
    import_watchlist,
    validate_registry_data,
)
from scripts.research_company_watchlist import (
    ControlledResearchClient,
    generate_reports,
    merge_batch,
)
from src.scraper.company_careers.registry import load_target_companies
from src.integrations.supabase_job_store import generate_source_key


def enriched_row(
    company: str,
    url: str,
    platform: str,
    *,
    slug: str = "",
    tenant: str = "",
    wd_subdomain: str = "",
    site_id: str = "",
    status: str = "validated",
    priority: str = "TBD",
    notes: str = "",
) -> dict[str, str]:
    return {
        "Rank": "1",
        "Company": company,
        "Careers URL": url,
        "Priority": priority,
        "Notes": notes,
        "ATS Platform": platform,
        "ATS Slug": slug,
        "Workday Tenant": tenant,
        "Workday Subdomain": wd_subdomain,
        "Workday Site ID": site_id,
        "Import Status": status,
        "Validation Notes": "fixture",
        "Source URL": url,
    }


def research_row(
    company: str,
    careers_url: str,
    job_search_url: str,
    platform: str,
    status: str,
    *,
    required_adapter: str,
    reusable: str = "false",
    reviewed: str = "true",
) -> dict[str, str]:
    return {
        "Company": company,
        "Official Careers URL": careers_url,
        "Official Job Search URL": job_search_url,
        "Detected Platform": platform,
        "Required Adapter": required_adapter,
        "Existing Adapter Reusable": reusable,
        "Source URL": careers_url,
        "Research Status": status,
        "Research Notes": "review fixture",
        "Reviewed": reviewed,
        "ATS Slug": "",
        "Workday Tenant": "",
        "Workday Subdomain": "",
        "Workday Site ID": "",
    }


class ATSDetectionTests(unittest.TestCase):
    def test_greenhouse_url_parsing(self):
        for url in (
            "https://boards.greenhouse.io/example/jobs/1",
            "https://job-boards.greenhouse.io/example",
        ):
            with self.subTest(url=url):
                detected = detect_ats(url)
                self.assertEqual(detected.platform, "greenhouse")
                self.assertEqual(detected.slug, "example")

    def test_lever_url_parsing(self):
        detected = detect_ats("https://jobs.lever.co/example/abc")
        self.assertEqual(detected.platform, "lever")
        self.assertEqual(detected.slug, "example")

    def test_ashby_url_parsing(self):
        detected = detect_ats("https://jobs.ashbyhq.com/Example/abc")
        self.assertEqual(detected.platform, "ashby")
        self.assertEqual(detected.slug, "Example")

    def test_workday_url_parsing(self):
        detected = detect_ats(
            "https://acme.wd5.myworkdayjobs.com/External_Careers/job/Calgary/123"
        )
        self.assertEqual(detected.platform, "workday")
        self.assertEqual(detected.tenant, "acme")
        self.assertEqual(detected.wd_subdomain, "wd5")
        self.assertEqual(detected.site_id, "External_Careers")

    def test_workday_url_parsing_skips_locale_prefix(self):
        detected = detect_ats(
            "https://cadence.wd1.myworkdayjobs.com/en-US/External_Careers/job/123"
        )
        self.assertEqual(detected.platform, "workday")
        self.assertEqual(detected.tenant, "cadence")
        self.assertEqual(detected.wd_subdomain, "wd1")
        self.assertEqual(detected.site_id, "External_Careers")


class EnrichmentTests(unittest.TestCase):
    def test_duplicate_company_handling(self):
        source = [
            {"Rank": "1", "Company": "Acme, Inc.", "Careers URL": "", "Priority": "TBD", "Notes": "one"},
            {"Rank": "2", "Company": "ACME INC", "Careers URL": "", "Priority": "High", "Notes": "two"},
        ]
        enriched, manual = enrich_rows(source, [], validate_urls=False)
        self.assertEqual([row["Import Status"] for row in enriched], ["duplicate_company"] * 2)
        self.assertEqual(len(manual), 2)

    def test_unsupported_url_reporting(self):
        source = [
            {
                "Rank": "1",
                "Company": "Acme",
                "Careers URL": "https://careers.acme.example/jobs",
                "Priority": "TBD",
                "Notes": "",
            }
        ]
        enriched, manual = enrich_rows(source, [], validate_urls=False)
        self.assertEqual(enriched[0]["Import Status"], "manual_review")
        self.assertIn("Unsupported", enriched[0]["Validation Notes"])
        self.assertEqual(manual, enriched)

    def test_missing_careers_url_reporting(self):
        source = [
            {"Rank": "1", "Company": "Acme", "Careers URL": "", "Priority": "TBD", "Notes": "research"}
        ]
        enriched, manual = enrich_rows(source, [], validate_urls=False)
        self.assertEqual(enriched[0]["Import Status"], "manual_review")
        self.assertIn("Missing careers URL", enriched[0]["Validation Notes"])
        self.assertEqual(len(manual), 1)

    def test_reviewed_research_row_is_merged(self):
        source = [
            {"Rank": "1", "Company": "Apple", "Careers URL": "", "Priority": "TBD", "Notes": ""}
        ]
        research = [
            research_row(
                "Apple",
                "https://www.apple.com/careers/us/",
                "https://jobs.apple.com/en-us/search",
                "custom_apple",
                "validated_custom_adapter_required",
                required_adapter="custom Apple adapter",
            )
        ]
        enriched, _ = enrich_rows(source, [], research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["ATS Platform"], "custom_apple")
        self.assertEqual(enriched[0]["Import Status"], "validated_custom_adapter_required")

    def test_unreviewed_research_row_is_ignored(self):
        source = [
            {"Rank": "1", "Company": "Apple", "Careers URL": "", "Priority": "TBD", "Notes": ""}
        ]
        research = [
            research_row(
                "Apple",
                "https://www.apple.com/careers/us/",
                "https://jobs.apple.com/en-us/search",
                "custom_apple",
                "validated_custom_adapter_required",
                required_adapter="custom Apple adapter",
                reviewed="false",
            )
        ]
        enriched, _ = enrich_rows(source, [], research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["Import Status"], "manual_review")
        self.assertEqual(enriched[0]["Careers URL"], "")

    def test_official_careers_url_is_preserved(self):
        source = [
            {"Rank": "1", "Company": "Apple", "Careers URL": "", "Priority": "TBD", "Notes": ""}
        ]
        official_url = "https://www.apple.com/careers/us/"
        research = [
            research_row(
                "Apple",
                official_url,
                "https://jobs.apple.com/en-us/search",
                "custom_apple",
                "validated_custom_adapter_required",
                required_adapter="custom Apple adapter",
            )
        ]
        enriched, _ = enrich_rows(source, [], research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["Careers URL"], official_url)

    def test_apple_remains_custom_adapter_required(self):
        self._assert_custom_adapter_required(
            "Apple",
            "https://www.apple.com/careers/us/",
            "https://jobs.apple.com/en-us/search",
            "custom_apple",
        )

    def test_microsoft_remains_custom_adapter_required(self):
        self._assert_custom_adapter_required(
            "Microsoft",
            "https://careers.microsoft.com/",
            "https://apply.careers.microsoft.com/careers",
            "custom_microsoft",
        )

    def test_tesla_remains_custom_adapter_required(self):
        self._assert_custom_adapter_required(
            "Tesla",
            "https://www.tesla.com/careers",
            "https://www.tesla.com/careers/search",
            "custom_tesla",
        )

    def test_google_deepmind_reuses_google_but_stays_review_gated(self):
        source = [
            {
                "Rank": "1",
                "Company": "Google DeepMind",
                "Careers URL": "",
                "Priority": "TBD",
                "Notes": "",
            }
        ]
        research = [
            research_row(
                "Google DeepMind",
                "https://deepmind.google/careers/",
                "https://www.google.com/about/careers/applications/jobs/results/?q=Google%20DeepMind",
                "google",
                "manual_review",
                required_adapter="existing Google adapter with safe organization/team filter",
                reusable="true",
            )
        ]
        enriched, _ = enrich_rows(source, [], research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["ATS Platform"], "google")
        self.assertEqual(enriched[0]["Import Status"], "manual_review")
        self.assertIn("organization/team filter", enriched[0]["Validation Notes"])

    def test_invalid_reviewed_research_is_marked_invalid(self):
        source = [
            {"Rank": "1", "Company": "Apple", "Careers URL": "", "Priority": "TBD", "Notes": ""}
        ]
        research = [
            research_row(
                "Apple",
                "https://simplify.jobs/apple",
                "https://simplify.jobs/apple/jobs",
                "custom_apple",
                "validated_custom_adapter_required",
                required_adapter="custom Apple adapter",
            )
        ]
        enriched, _ = enrich_rows(source, [], research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["Import Status"], "invalid")
        self.assertIn("prohibited", enriched[0]["Validation Notes"])

    def test_existing_configuration_wins_over_reviewed_research(self):
        source = [
            {"Rank": "1", "Company": "Google", "Careers URL": "", "Priority": "TBD", "Notes": ""}
        ]
        existing = [{"name": "Google", "platform": "google", "max_pages": 3}]
        research = [
            research_row(
                "Google",
                "https://www.google.com/about/careers/",
                "https://www.google.com/about/careers/applications/jobs/results/",
                "custom_google_replacement",
                "validated_custom_adapter_required",
                required_adapter="replacement",
            )
        ]
        enriched, _ = enrich_rows(source, existing, research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["Import Status"], "existing_config")
        self.assertEqual(enriched[0]["ATS Platform"], "google")
        self.assertEqual(existing, [{"name": "Google", "platform": "google", "max_pages": 3}])

    def _assert_custom_adapter_required(
        self,
        company: str,
        careers_url: str,
        job_search_url: str,
        platform: str,
    ) -> None:
        source = [
            {"Rank": "1", "Company": company, "Careers URL": "", "Priority": "TBD", "Notes": ""}
        ]
        research = [
            research_row(
                company,
                careers_url,
                job_search_url,
                platform,
                "validated_custom_adapter_required",
                required_adapter=f"{platform} adapter",
            )
        ]
        enriched, _ = enrich_rows(source, [], research_rows=research, validate_urls=False)
        self.assertEqual(enriched[0]["ATS Platform"], platform)
        self.assertEqual(enriched[0]["Import Status"], "validated_custom_adapter_required")


class ImportTests(unittest.TestCase):
    def test_custom_adapter_required_rows_are_not_imported(self):
        rows = [
            enriched_row(
                "Apple",
                "https://www.apple.com/careers/us/",
                "custom_apple",
                status="validated_custom_adapter_required",
            )
        ]
        generated, report = build_generated_registry(rows, [])
        self.assertEqual(generated["target_companies"], [])
        self.assertEqual(report["decisions"][0]["decision"], "manual_review")

    def test_google_and_deepmind_jobs_share_stable_source_key(self):
        google_job = {
            "job_id": "google-123",
            "source": "custom:google",
            "company": "Google",
            "url": "https://www.google.com/about/careers/applications/jobs/results/123",
        }
        deepmind_alias = {**google_job, "company": "Google DeepMind"}
        self.assertEqual(generate_source_key(google_job), generate_source_key(deepmind_alias))

    def test_duplicate_url_handling(self):
        rows = [
            enriched_row("First", "https://boards.greenhouse.io/shared", "greenhouse", slug="shared"),
            enriched_row("Second", "https://boards.greenhouse.io/shared/", "greenhouse", slug="shared"),
        ]
        generated, report = build_generated_registry(rows, [])
        self.assertEqual([entry["name"] for entry in generated["target_companies"]], ["First"])
        self.assertEqual(report["decisions"][1]["decision"], "duplicate_url")

    def test_existing_configuration_preservation(self):
        existing = [{"name": "Acme", "platform": "greenhouse", "slug": "working", "max_pages": 2}]
        rows = [enriched_row("ACME", "https://jobs.lever.co/weaker", "lever", slug="weaker")]
        generated, report = build_generated_registry(rows, existing)
        self.assertEqual(generated["target_companies"], existing)
        self.assertEqual(report["decisions"][0]["decision"], "existing_preserved")

    def test_reviewed_ats_fields_allow_official_landing_page(self):
        rows = [
            enriched_row(
                "Acme",
                "https://www.acme.com/careers",
                "greenhouse",
                slug="acme",
                status="validated_supported",
            )
        ]
        generated, report = build_generated_registry(rows, [])
        self.assertEqual(generated["target_companies"][0]["slug"], "acme")
        self.assertEqual(report["decisions"][0]["decision"], "added")

    def test_existing_custom_adapter_preservation(self):
        existing = [
            {
                "name": "Custom Co",
                "platform": "custom:shopify",
                "max_pages": 7,
                "search_terms": ["Staff Engineer"],
            }
        ]
        snapshot = copy.deepcopy(existing)
        rows = [enriched_row("Custom Co", "https://boards.greenhouse.io/customco", "greenhouse", slug="customco")]
        generated, _ = build_generated_registry(rows, existing)
        self.assertEqual(generated["target_companies"], snapshot)

    def test_priority_and_notes_preservation(self):
        rows = [
            enriched_row(
                "Acme",
                "https://jobs.ashbyhq.com/acme",
                "ashby",
                slug="acme",
                priority="High",
                notes="Alberta office",
            )
        ]
        generated, _ = build_generated_registry(rows, [])
        target = generated["target_companies"][0]
        self.assertEqual(target["priority"], "High")
        self.assertEqual(target["notes"], "Alberta office")

    def test_dry_run_does_not_modify_registry_files(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = root / "target_companies.yaml"
            registry.write_text("target_companies:\n- name: Existing\n  platform: google\n", encoding="utf-8")
            original = registry.read_bytes()
            input_csv = root / "enriched.csv"
            output = root / "generated.yaml"
            report = root / "report.json"
            write_csv(
                input_csv,
                [enriched_row("Acme", "https://jobs.lever.co/acme", "lever", slug="acme")],
                ENRICHED_FIELDS,
            )
            import_watchlist(input_csv, output, registry, validate=True, dry_run=True, report_path=report)
            self.assertEqual(registry.read_bytes(), original)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_repeated_import_is_idempotent(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = root / "base.yaml"
            base.write_text("target_companies:\n- name: Existing\n  platform: google\n", encoding="utf-8")
            input_csv = root / "enriched.csv"
            write_csv(
                input_csv,
                [enriched_row("Acme", "https://jobs.lever.co/acme", "lever", slug="acme")],
                ENRICHED_FIELDS,
            )
            first = root / "first.yaml"
            second = root / "second.yaml"
            import_watchlist(input_csv, first, base, validate=True, report_path=root / "first.json")
            import_watchlist(input_csv, second, first, validate=True, report_path=root / "second.json")
            self.assertEqual(first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8"))

    def test_generated_yaml_is_registry_loader_compatible(self):
        rows = [enriched_row("Acme", "https://jobs.lever.co/acme", "lever", slug="acme")]
        generated, _ = build_generated_registry(rows, [{"name": "Existing", "platform": "google"}])
        validate_registry_data(generated)
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "generated.yaml"
            path.write_text(dump_registry(generated), encoding="utf-8")
            targets = load_target_companies(str(path))
        self.assertEqual([target.name for target in targets], ["Existing", "Acme"])
        self.assertEqual(targets[1].platform, "lever")
        self.assertEqual(targets[1].slug, "acme")

    def test_import_report_is_json_serializable(self):
        _, report = build_generated_registry([], [{"name": "Existing", "platform": "google"}])
        self.assertEqual(json.loads(json.dumps(report))["summary"]["generated_entries"], 1)


class ResearchBatchTests(unittest.TestCase):
    def test_batch_size_is_capped_at_twenty_five(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 25"):
            merge_batch([], [], [], Mock(), start_row=1, batch_size=26)

    def test_reviewed_rows_are_preserved_without_network_calls(self):
        reviewed = research_row(
            "Acme",
            "https://www.acme.com/careers",
            "https://www.acme.com/careers/jobs",
            "custom",
            "validated_custom_adapter_required",
            required_adapter="custom_company_site",
        )
        client = Mock()
        merged, decisions = merge_batch(
            [{"Company": "Acme"}], [reviewed], [], client, start_row=1, batch_size=1
        )
        self.assertEqual(merged, [reviewed])
        self.assertEqual(decisions[0]["decision"], "preserved_reviewed")
        client.search.assert_not_called()

    def test_controlled_client_retries_are_bounded_without_live_network(self):
        response = Mock(status_code=503)
        response.close = Mock()
        session = Mock()
        session.request.return_value = response
        sleeps: list[float] = []
        client = ControlledResearchClient(
            "key", delay=0.25, retries=2, session=session, sleep_fn=sleeps.append
        )
        with self.assertRaisesRegex(Exception, "after 3 attempts"):
            client._request("get", "https://example.com")
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual(sleeps, [0.25, 0.25, 0.25, 0.5, 0.25])

    def test_reports_cover_full_watchlist_and_group_custom_adapters(self):
        custom = research_row(
            "Acme",
            "https://www.acme.com/careers",
            "https://www.acme.com/careers/jobs",
            "custom",
            "validated_custom_adapter_required",
            required_adapter="custom_company_site",
        )
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch("scripts.research_company_watchlist.PLATFORM_SUMMARY", root / "platform.csv"),
                patch("scripts.research_company_watchlist.RESEARCH_SUMMARY", root / "summary.json"),
                patch("scripts.research_company_watchlist.MANUAL_REVIEW_REPORT", root / "manual.csv"),
                patch("scripts.research_company_watchlist.CUSTOM_ADAPTER_REPORT", root / "custom.csv"),
            ):
                summary = generate_reports(
                    [{"Company": "Acme"}, {"Company": "Pending"}], [custom], []
                )
            self.assertEqual(summary["total_workbook_companies"], 2)
            self.assertEqual(summary["researched_companies"], 1)
            self.assertEqual(summary["unsupported_adapter_families"], ["custom_company_site"])
            self.assertTrue((root / "platform.csv").exists())
            custom_rows = (root / "custom.csv").read_text(encoding="utf-8-sig")
            self.assertIn("Acme", custom_rows)
            self.assertIn("Companies Unlocked", custom_rows)

    def test_research_csv_schema_contains_required_identifier_columns(self):
        for field in ("ATS Slug", "Workday Tenant", "Workday Subdomain", "Workday Site ID"):
            self.assertIn(field, RESEARCH_FIELDS)


if __name__ == "__main__":
    unittest.main()
