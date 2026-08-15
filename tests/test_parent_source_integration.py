import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import validate_company_registry as registry_validation
from scripts import validate_parent_source_integration as validation
from src.integrations.google_sheets_publisher import GoogleSheetsPublisher
from src.integrations.supabase_job_store import (
    SupabaseJobStore,
    generate_source_key,
    tracked_job_payload,
)
from src.scraper.company_careers import scanner as scanner_module
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
)
from src.scraper.company_careers.parent_sources import (
    ParentSourceMapping,
    annotate_child_brand_metadata,
    canonical_job_key,
    load_parent_source_mappings,
    original_company_coverage,
)
from src.scraper.company_careers.registry import load_target_companies
from tests.test_google_sheets_publisher import FakeSpreadsheet
from tests.test_supabase_job_store import FakeSupabaseClient, MutableClock
from tests.test_update_student_dashboard import FakeDashboardClient


ROOT = Path(__file__).resolve().parents[1]
MAPPINGS_PATH = ROOT / "config" / "company_parent_source_mappings.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.parent_mappings_candidates.yaml"
VALIDATED = ROOT / "config" / "target_companies.parent_mappings_validated.yaml"


def job(
    *,
    job_id="meta-1",
    company="Meta",
    source="custom:meta",
    departments=None,
    title="Software Engineer Intern",
    description="Student role",
    url="https://www.metacareers.com/jobs/meta-1/",
    raw_metadata=None,
):
    return JobRecord(
        job_id=job_id,
        title=title,
        company=company,
        location="Remote",
        url=url,
        updated_at="2026-08-01",
        source=source,
        departments=departments or [],
        description=description,
        raw_metadata=raw_metadata or {},
    )


def mapping_dict(**overrides):
    values = {
        "child_company": "Child",
        "parent_company": "Parent",
        "enabled": True,
        "match_field": "departments",
        "match_type": "exact_normalized",
        "accepted_values": ["Child"],
        "canonical_source": "custom:parent",
        "display_behavior": "child_brand_metadata",
        "confidence": "high",
        "evidence": {"validation_artifact": "review.json"},
    }
    values.update(overrides)
    return values


class ParentSourceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.mappings = load_parent_source_mappings(MAPPINGS_PATH)

    def test_01_exactly_four_enabled_mappings(self):
        self.assertEqual(len(self.mappings), 4)
        self.assertTrue(all(item.enabled for item in self.mappings))
        self.assertEqual(
            [item.child_company for item in self.mappings],
            ["Google DeepMind", "Instagram", "Universal Robots", "WhatsApp"],
        )

    def test_02_loader_remains_compatible_with_reviewed_legacy_schema(self):
        item = ParentSourceMapping.from_dict(
            {
                "child_company": "Child",
                "parent_company": "Parent",
                "canonical_source": "custom:parent",
                "official_child_careers_url": "https://example.com/child",
                "official_parent_search_url": "https://example.com/jobs",
                "match": {"field": "departments", "exact_values": ["Child"]},
                "status": "safe_with_metadata_mapping",
            }
        )
        self.assertTrue(item.enabled)
        self.assertEqual(item.match_type, "exact_normalized")

    def test_03_unknown_mapping_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown parent-source mapping fields"):
            ParentSourceMapping.from_dict(mapping_dict(executable="no"))

    def test_04_duplicate_child_mapping_is_rejected(self):
        document = {"company_parent_source_mappings": [mapping_dict(), mapping_dict()]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mappings.yaml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate normalized child"):
                load_parent_source_mappings(path)

    def test_05_duplicate_exact_values_are_normalized_safely(self):
        item = ParentSourceMapping.from_dict(
            mapping_dict(accepted_values=[" Child ", "child", "CHILD"])
        )
        self.assertEqual(item.exact_values, ("Child",))


class ParentSourceAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.mappings = load_parent_source_mappings(MAPPINGS_PATH)

    def test_06_instagram_uses_exact_structured_meta_metadata(self):
        self.assertEqual(
            annotate_child_brand_metadata(
                job(departments=[" instagram "]), self.mappings
            ).child_brands,
            ["Instagram"],
        )

    def test_07_whatsapp_uses_exact_structured_meta_metadata(self):
        self.assertEqual(
            annotate_child_brand_metadata(
                job(departments=["WhatsApp"]), self.mappings
            ).child_brands,
            ["WhatsApp"],
        )

    def test_08_title_only_instagram_is_rejected(self):
        mapped = annotate_child_brand_metadata(
            job(title="Instagram Software Intern", departments=["Infrastructure"]),
            self.mappings,
        )
        self.assertEqual(mapped.child_brands, [])

    def test_09_description_only_whatsapp_is_rejected(self):
        mapped = annotate_child_brand_metadata(
            job(description="Build WhatsApp services", departments=["Infrastructure"]),
            self.mappings,
        )
        self.assertEqual(mapped.child_brands, [])

    def test_10_one_meta_job_supports_both_brands_without_duplication(self):
        original = job(departments=["WhatsApp", "Instagram"])
        mapped = annotate_child_brand_metadata(original, self.mappings)
        self.assertEqual(mapped.child_brands, ["Instagram", "WhatsApp"])
        self.assertEqual(canonical_job_key(mapped), canonical_job_key(original))

    def test_11_child_brand_order_and_casefold_deduplication_are_deterministic(self):
        candidate = job(
            raw_metadata={"child_brands": ["whatsapp", "Instagram", "WHATSAPP"]}
        )
        self.assertEqual(candidate.child_brands, ["Instagram", "whatsapp"])

    def test_12_universal_robots_uses_exact_subsidiary_metadata(self):
        candidate = job(
            job_id="sap_successfactors:jobs.teradyne.com:10",
            company="Teradyne",
            source="sap_successfactors:jobs.teradyne.com",
            url="https://jobs.teradyne.com/job/Odense/Role/10/",
            raw_metadata={"fields": {"Subsidiary": ["Universal-Robots"]}},
        )
        self.assertEqual(
            annotate_child_brand_metadata(candidate, self.mappings).child_brands,
            ["Universal Robots"],
        )

    def test_13_robotics_title_alone_does_not_match_universal_robots(self):
        candidate = job(
            company="Teradyne",
            source="sap_successfactors:jobs.teradyne.com",
            title="Robotics Engineering Intern",
            raw_metadata={"fields": {"Subsidiary": ["Teradyne"]}},
        )
        self.assertEqual(
            annotate_child_brand_metadata(candidate, self.mappings).child_brands, []
        )

    def test_14_canonical_company_id_url_and_source_are_unchanged(self):
        for original in (
            job(departments=["Instagram"]),
            job(
                job_id="sap_successfactors:jobs.teradyne.com:10",
                company="Teradyne",
                source="sap_successfactors:jobs.teradyne.com",
                url="https://jobs.teradyne.com/job/Odense/Role/10/",
                raw_metadata={"fields": {"Subsidiary": ["Universal-Robots"]}},
            ),
        ):
            mapped = annotate_child_brand_metadata(original, self.mappings)
            self.assertEqual(
                (mapped.company, mapped.job_id, mapped.url, mapped.source),
                (original.company, original.job_id, original.url, original.source),
            )
            self.assertEqual(generate_source_key(mapped), generate_source_key(original))

    def test_15_child_brands_survive_job_serialization(self):
        serialized = annotate_child_brand_metadata(
            job(departments=["Instagram"]), self.mappings
        ).to_dict()
        self.assertEqual(serialized["child_brands"], ["Instagram"])
        self.assertEqual(serialized["raw_metadata"]["child_brands"], ["Instagram"])

    def test_15b_greenhouse_acquisition_company_uses_exact_metadata(self):
        mapping = ParentSourceMapping.from_dict(
            mapping_dict(
                child_company="Weights & Biases",
                parent_company="CoreWeave",
                canonical_source="greenhouse:coreweave",
                match_field="raw_metadata.greenhouse_metadata.Acquisition Company",
                accepted_values=["Weights & Biases"],
            )
        )
        candidate = job(
            company="CoreWeave",
            source="greenhouse:coreweave",
            raw_metadata={
                "greenhouse_metadata": {
                    "Acquisition Company": "Weights & Biases",
                }
            },
        )
        self.assertEqual(
            annotate_child_brand_metadata(candidate, [mapping]).child_brands,
            ["Weights & Biases"],
        )
        candidate.raw_metadata["greenhouse_metadata"]["Acquisition Company"] = "CoreWeave"
        self.assertEqual(
            annotate_child_brand_metadata(candidate, [mapping]).child_brands,
            [],
        )


class ParentSourceScannerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.mappings = load_parent_source_mappings(MAPPINGS_PATH)
        self.meta_target = CompanyTarget(name="Meta", platform="meta")

    def test_16_annotation_occurs_before_job_filter(self):
        observed = []
        adapter = Mock(return_value=[job(departments=["Instagram"])])
        with patch.dict(scanner_module.ADAPTERS, {"meta": adapter}, clear=False):
            report = scanner_module.CompanyCareersScanner(
                targets=[self.meta_target], parent_source_mappings=self.mappings
            ).scan(job_filter=lambda item: observed.append(item.child_brands) or None)
        self.assertEqual(observed, [["Instagram"]])
        self.assertEqual(report.jobs[0].child_brands, ["Instagram"])

    def test_17_parent_scan_occurs_once_and_mapped_brands_never_invoke_scanner(self):
        adapter = Mock(return_value=[job(departments=["Instagram", "WhatsApp"])])
        with patch.dict(scanner_module.ADAPTERS, {"meta": adapter}, clear=False):
            report = scanner_module.CompanyCareersScanner(
                targets=[self.meta_target], parent_source_mappings=self.mappings
            ).scan()
        adapter.assert_called_once()
        self.assertEqual(len(report.jobs), 1)
        self.assertEqual(len(report.mapped_coverage), len(self.mappings))

    def test_18_zero_mapped_jobs_is_not_a_technical_failure(self):
        with patch.dict(
            scanner_module.ADAPTERS,
            {"meta": Mock(return_value=[job(departments=["Infrastructure"])])},
            clear=False,
        ):
            report = scanner_module.CompanyCareersScanner(
                targets=[self.meta_target], parent_source_mappings=self.mappings
            ).scan()
        statuses = {item["company"]: item["status"] for item in report.mapped_coverage}
        self.assertEqual(statuses["Instagram"], CoverageStatus.MAPPED_NO_CURRENT_JOBS)
        self.assertEqual(statuses["WhatsApp"], CoverageStatus.MAPPED_NO_CURRENT_JOBS)

    def test_19_parent_technical_failure_is_inherited_without_child_jobs(self):
        def fail(*_args):
            raise AdapterError("parent unavailable")

        with patch.dict(scanner_module.ADAPTERS, {"meta": fail}, clear=False):
            report = scanner_module.CompanyCareersScanner(
                targets=[self.meta_target], parent_source_mappings=self.mappings
            ).scan()
        rows = {item["company"]: item for item in report.mapped_coverage}
        self.assertEqual(rows["Instagram"]["status"], CoverageStatus.PARENT_FAILED_TECHNICAL)
        self.assertEqual(rows["WhatsApp"]["mapped_jobs"], 0)
        self.assertEqual(report.jobs, [])

    def test_20_coverage_credits_children_without_claiming_independent_scans(self):
        with patch.dict(
            scanner_module.ADAPTERS,
            {"meta": Mock(return_value=[job(departments=["Instagram"])])},
            clear=False,
        ):
            report = scanner_module.CompanyCareersScanner(
                targets=[self.meta_target], parent_source_mappings=self.mappings
            ).scan()
        instagram = next(item for item in report.coverage() if item["company"] == "Instagram")
        self.assertEqual(instagram["coverage_relationship"], "parent_company_source")
        self.assertEqual(instagram["canonical_parent"], "Meta")
        self.assertEqual(report.to_dict()["canonical_sources_checked"], 1)

    def test_21_candidate_original_coverage_becomes_87_without_live_count_constant(self):
        targets = load_target_companies(str(CANDIDATES))
        historical_mappings = [
            item for item in self.mappings if item.child_company != "Google DeepMind"
        ]
        coverage = original_company_coverage(targets, historical_mappings)
        self.assertEqual(coverage["total_original_companies_covered"], 87)
        self.assertEqual(coverage["mapped_child_brands_covered"], 3)


class ParentSourcePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.mappings = load_parent_source_mappings(MAPPINGS_PATH)
        self.clock = MutableClock()
        self.client = FakeSupabaseClient()
        self.store = SupabaseJobStore(self.client, now_factory=self.clock)
        self.started = datetime(2026, 8, 4, tzinfo=timezone.utc)

    def _ranked(self, brands):
        return {
            **job(departments=["Instagram"]).to_dict(),
            "child_brands": brands,
            "raw_metadata": {"child_brands": brands},
            "relevance_score": 80,
            "matched_categories": ["software_engineering"],
        }

    def _sync(self, value, successful=("Meta",)):
        return self.store.sync_student_jobs(
            [value],
            successful_companies=successful,
            started_at=self.started,
            companies_checked=1,
            jobs_fetched=1,
        )

    def test_22_child_brands_survive_supabase_payload_mapping(self):
        payload = tracked_job_payload(
            self._ranked(["Instagram"]), seen_at=self.started
        )
        self.assertEqual(payload["raw_data"]["child_brands"], ["Instagram"])

    def test_23_child_only_change_is_update_not_insert_and_preserves_first_seen(self):
        first = self._ranked([])
        second = self._ranked(["Instagram"])
        inserted = self._sync(first)
        source_key = generate_source_key(first)
        first_seen = self.client.tracked_jobs[source_key]["first_seen_at"]
        self.clock.advance()
        updated = self._sync(second)
        self.assertEqual((inserted.inserted, updated.updated, updated.inserted), (1, 1, 0))
        self.assertEqual(self.client.tracked_jobs[source_key]["first_seen_at"], first_seen)

    def test_24_rediscovery_keeps_one_row_and_same_source_key(self):
        value = self._ranked(["Instagram", "WhatsApp"])
        self._sync(value)
        self._sync(copy.deepcopy(value))
        self.assertEqual(len(self.client.tracked_jobs), 1)

    def test_25_applications_are_never_changed(self):
        self.client.applications["manual"] = {"status": "applied", "notes": "keep"}
        before = copy.deepcopy(self.client.applications)
        self._sync(self._ranked(["Instagram"]))
        self.assertEqual(self.client.applications, before)

    def test_26_parent_failure_does_not_advance_missing_count(self):
        value = self._ranked(["Instagram"])
        self._sync(value)
        source_key = generate_source_key(value)
        self.store.sync_student_jobs(
            [],
            successful_companies=[],
            started_at=self.started,
            companies_checked=1,
            jobs_fetched=0,
        )
        self.assertEqual(
            self.client.tracked_jobs[source_key]["missing_successful_runs"], 0
        )

    def test_26b_child_brands_are_restored_from_raw_data_for_dashboard_rows(self):
        client = FakeDashboardClient()
        client.queries["tracked_jobs"].data[0]["raw_data"] = {
            "raw_metadata": {"child_brands": ["WhatsApp", "Instagram"]}
        }
        dashboard = SupabaseJobStore(client).fetch_dashboard_data()
        self.assertEqual(
            dashboard.active_jobs[0]["child_brands"], ["Instagram", "WhatsApp"]
        )


class ParentSourceSheetsAndRegistryTests(unittest.TestCase):
    def test_27_sheets_expose_child_brands_on_one_job_row(self):
        sheet = FakeSpreadsheet(("New Jobs", "Applications"))
        publisher = GoogleSheetsPublisher(
            sheet, now_factory=lambda: datetime(2026, 8, 4, tzinfo=timezone.utc)
        )
        publisher.publish(
            active_jobs=[
                {
                    "source_key": "one",
                    "company": "Meta",
                    "title": "Intern",
                    "status": "active",
                    "first_seen_at": "2026-08-04T00:00:00Z",
                    "child_brands": ["Instagram", "WhatsApp"],
                }
            ],
            scan_run={"success": True, "companies_checked": 1},
            companies=[],
        )
        active = sheet.sheets["Active Jobs"].values
        self.assertEqual(len(active), 2)
        self.assertEqual(active[1][-1], "Instagram; WhatsApp")

    def test_28_companies_tab_distinguishes_mapped_brands(self):
        sheet = FakeSpreadsheet(("Applications",))
        publisher = GoogleSheetsPublisher(sheet)
        publisher.publish(
            active_jobs=[],
            scan_run={},
            companies=[
                {
                    "company": "Instagram",
                    "platform": "custom:meta",
                    "status": "mapped_no_current_jobs",
                    "coverage_relationship": "parent_company_source",
                    "canonical_parent": "Meta",
                    "mapped_jobs": 0,
                }
            ],
        )
        self.assertEqual(
            sheet.sheets["Companies"].values[1][4:],
            ["parent_company_source", "Meta", 0],
        )

    def test_29_applications_tab_remains_untouched(self):
        sheet = FakeSpreadsheet(("Applications",))
        applications = sheet.sheets["Applications"]
        GoogleSheetsPublisher(sheet).publish(active_jobs=[], scan_run={}, companies=[])
        self.assertEqual(applications.clear_calls, 0)
        self.assertEqual(applications.update_calls, [])

    def test_30_historical_registries_append_only_reviewed_teradyne_source(self):
        teradyne_marker = b"  - name: Teradyne"
        candidate_bytes = CANDIDATES.read_bytes()
        validated_bytes = VALIDATED.read_bytes()

        self.assertEqual(candidate_bytes.count(teradyne_marker), 1)
        self.assertEqual(validated_bytes.count(teradyne_marker), 1)
        candidate_prefix = candidate_bytes[: candidate_bytes.index(teradyne_marker)]
        validated_prefix = validated_bytes[: validated_bytes.index(teradyne_marker)]
        self.assertEqual(candidate_prefix, validated_prefix)

        with tempfile.TemporaryDirectory() as temp_dir:
            historical_path = Path(temp_dir) / "historical_parent_source_baseline.yaml"
            historical_path.write_bytes(candidate_prefix)
            historical_targets = load_target_companies(str(historical_path))

        self.assertEqual(len(historical_targets), 92)
        historical_names = [target.name for target in historical_targets]
        for path, data in (
            (CANDIDATES, candidate_bytes),
            (VALIDATED, validated_bytes),
        ):
            with self.subTest(registry=path.name):
                _, entries = registry_validation.load_registry(path)
                targets = load_target_companies(str(path))
                self.assertTrue(data.startswith(candidate_prefix))
                self.assertEqual(
                    [target.name for target in targets],
                    [entry["name"] for entry in entries],
                )
                self.assertEqual(
                    [target.name for target in targets[: len(historical_targets)]],
                    historical_names,
                )

                appended = entries[len(historical_targets) :]
                self.assertEqual(len(appended), 1)
                teradyne = appended[0]
                self.assertEqual(teradyne["name"], "Teradyne")
                self.assertEqual(teradyne["platform"], "sap_successfactors")
                self.assertEqual(
                    teradyne["search_url"], "https://jobs.teradyne.com/search/"
                )
                self.assertEqual(teradyne["allowed_apply_hosts"], ["jobs.teradyne.com"])
                self.assertEqual(
                    registry_validation.source_key(teradyne),
                    "sap_successfactors:jobs.teradyne.com",
                )

                normalized_names = [
                    registry_validation._name_key(entry) for entry in entries
                ]
                normalized_sources = [
                    source
                    for entry in entries
                    if (source := registry_validation.source_key(entry)) is not None
                ]
                self.assertNotIn("Universal Robots", [entry["name"] for entry in entries])
                self.assertTrue(all(normalized_names))
                self.assertEqual(len(normalized_names), len(set(normalized_names)))
                self.assertEqual(len(normalized_sources), len(set(normalized_sources)))

        self.assertTrue(PRODUCTION.read_bytes().startswith(validated_bytes))

    def test_31_universal_robots_is_never_an_independent_registry_target(self):
        for path in (PRODUCTION, CANDIDATES, VALIDATED):
            names = {target.name for target in load_target_companies(str(path))}
            self.assertNotIn("Universal Robots", names)

    def test_32_promoted_parent_source_preserves_production_integrity(self):
        _, production = registry_validation.load_registry(PRODUCTION)
        loaded_targets = load_target_companies(str(PRODUCTION))
        teradyne_entries = [
            entry
            for entry in production
            if registry_validation._name_key(entry) == "teradyne"
        ]

        self.assertEqual(len(teradyne_entries), 1)
        teradyne = teradyne_entries[0]
        self.assertEqual(teradyne["platform"], "sap_successfactors")
        self.assertEqual(teradyne["search_url"], "https://jobs.teradyne.com/search/")
        self.assertEqual(teradyne["allowed_apply_hosts"], ["jobs.teradyne.com"])
        self.assertEqual(
            registry_validation.source_key(teradyne),
            "sap_successfactors:jobs.teradyne.com",
        )

        production_names = [entry["name"] for entry in production]
        self.assertNotIn("Universal Robots", production_names)

        mappings = load_parent_source_mappings(MAPPINGS_PATH)
        self.assertEqual(
            {
                mapping.child_company: (
                    mapping.parent_company,
                    mapping.canonical_source,
                )
                for mapping in mappings
            },
            {
                "Google DeepMind": ("Google", "custom:google"),
                "Instagram": ("Meta", "custom:meta"),
                "WhatsApp": ("Meta", "custom:meta"),
                "Universal Robots": (
                    "Teradyne",
                    "sap_successfactors:jobs.teradyne.com",
                ),
            },
        )
        self.assertTrue(all(mapping.enabled for mapping in mappings))

        normalized_names = [
            registry_validation._name_key(entry) for entry in production
        ]
        normalized_sources = [
            source
            for entry in production
            if (source := registry_validation.source_key(entry)) is not None
        ]
        self.assertTrue(all(normalized_names))
        self.assertEqual(len(normalized_names), len(set(normalized_names)))
        self.assertEqual(len(normalized_sources), len(set(normalized_sources)))
        self.assertEqual(
            [target.name for target in loaded_targets],
            production_names,
        )


class ParentSourceValidationIsolationTests(unittest.TestCase):
    def test_33_validation_is_bounded_read_only_and_initializes_no_persistence(self):
        historical_mappings = [
            item
            for item in load_parent_source_mappings(MAPPINGS_PATH)
            if item.child_company != "Google DeepMind"
        ]
        meta_jobs = [job(job_id=f"meta-{index}", departments=["Instagram"]) for index in range(25)]
        teradyne_jobs = [
            job(
                job_id=f"sap_successfactors:jobs.teradyne.com:{index}",
                company="Teradyne",
                source="sap_successfactors:jobs.teradyne.com",
                url=f"https://jobs.teradyne.com/job/Test/Role/{index}/",
                raw_metadata={"fields": {"Subsidiary": ["Universal-Robots"]}},
            )
            for index in range(25)
        ]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(validation, "OUTPUT_PATH", Path(temp_dir) / "report.json"),
            patch.object(
                validation,
                "load_parent_source_mappings",
                return_value=historical_mappings,
            ),
            patch.object(validation.meta, "fetch_jobs", return_value=meta_jobs),
            patch.object(
                validation.sap_successfactors, "fetch_jobs", return_value=teradyne_jobs
            ),
            patch.object(
                SupabaseJobStore,
                "from_env",
                side_effect=AssertionError("Supabase initialized"),
            ) as supabase,
            patch(
                "src.integrations.google_sheets_publisher.GoogleSheetsPublisher.from_env",
                side_effect=AssertionError("Sheets initialized"),
            ) as sheets,
            patch.object(
                sqlite3, "connect", side_effect=AssertionError("SQLite initialized")
            ) as sqlite,
        ):
            report = validation.run_validation(timeout=1)
        self.assertTrue(all(row["parent_jobs_inspected"] <= 20 for row in report["results"]))
        self.assertTrue(
            all(max(row["batch_sizes"] or [0]) <= 5 for row in report["results"])
        )
        self.assertEqual(
            report["persistence_initialized"],
            {"supabase": False, "google_sheets": False, "sqlite": False},
        )
        supabase.assert_not_called()
        sheets.assert_not_called()
        sqlite.assert_not_called()


if __name__ == "__main__":
    unittest.main()
