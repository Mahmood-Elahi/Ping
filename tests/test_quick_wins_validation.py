from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import validate_company_registry as validator
from src.scraper.company_careers.adapters import eightfold
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import (
    ParentSourceMapping,
    annotate_child_brand_metadata,
    canonical_job_key,
    load_parent_source_mappings,
    matching_child_brands,
)
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.quick_wins_candidates.yaml"
VALIDATED = ROOT / "config" / "target_companies.quick_wins_validated.yaml"
MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
REPORT = ROOT / "output" / "quick_wins_candidate_validation.json"


def job(
    *,
    company: str = "Meta",
    source: str = "custom:meta",
    departments: list[str] | None = None,
    title: str = "Engineer",
    raw_metadata: dict | None = None,
) -> JobRecord:
    return JobRecord(
        job_id="123",
        title=title,
        company=company,
        location="Remote",
        url="https://example.com/job/123",
        updated_at="",
        source=source,
        departments=departments or [],
        raw_metadata=raw_metadata or {},
    )


class QuickWinsRegistryTests(unittest.TestCase):
    def test_01_historical_validated_registry_is_exact_candidate_byte_prefix(self):
        historical = VALIDATED.read_bytes()
        candidate = CANDIDATES.read_bytes()
        self.assertEqual(candidate[: len(historical)], historical)
        _, historical_entries = validator.load_registry(VALIDATED)
        loaded_targets = load_target_companies(str(VALIDATED))
        self.assertEqual(
            [target.name for target in loaded_targets],
            [entry["name"] for entry in historical_entries],
        )

    def test_02_candidate_order_preserves_historical_then_investigated_additions(self):
        _, historical = validator.load_registry(VALIDATED)
        _, candidates = validator.load_registry(CANDIDATES)
        additions = validator.detect_additions(historical, candidates)
        self.assertEqual(candidates[: len(historical)], historical)
        self.assertEqual([entry["name"] for entry in additions], ["STMicroelectronics"])
        self.assertEqual(
            additions[0],
            {
                "name": "STMicroelectronics",
                "platform": "eightfold",
                "careers_url": "https://stmicroelectronics.eightfold.ai/careers",
                "endpoint_url": "https://stmicroelectronics.eightfold.ai/api/pcsx/search",
                "endpoint_variant": "pcsx",
                "organization_id": "stmicroelectronics.com",
                "max_pages": 2,
            },
        )
        normalized_names = [validator._name_key(entry) for entry in candidates]
        normalized_sources = [
            source
            for entry in candidates
            if (source := validator.source_key(entry)) is not None
        ]
        self.assertEqual(len(normalized_names), len(set(normalized_names)))
        self.assertEqual(len(normalized_sources), len(set(normalized_sources)))

    def test_03_validated_registry_contains_only_safe_registry_candidates(self):
        report = json.loads(REPORT.read_text(encoding="utf-8"))
        _, candidates = validator.load_registry(CANDIDATES)
        _, validated = validator.load_registry(VALIDATED)
        additions = validator.detect_additions(validated, candidates)
        unsafe_report_names = {
            result["company"]
            for result in report["results"]
            if not result["safe_to_activate"]
        }
        unsafe_registry_additions = {
            entry["name"] for entry in additions if entry["name"] in unsafe_report_names
        }
        validated_names = {validator._name_key(entry) for entry in validated}
        self.assertEqual(report["summary"]["eightfold_safe_to_activate"], [])
        self.assertEqual(unsafe_registry_additions, {"STMicroelectronics"})
        self.assertNotIn("stmicroelectronics", validated_names)

    def test_04_candidate_builder_uses_controlled_synthetic_baseline(self):
        baseline = (
            "target_companies:\n"
            "  - name: Synthetic Baseline\n"
            "    platform: greenhouse\n"
            "    slug: synthetic\n"
        )
        addition = {
            "name": "Synthetic Candidate",
            "platform": "eightfold",
            "careers_url": "https://careers.example.com/careers",
            "endpoint_url": "https://careers.example.com/api/pcsx/search",
            "endpoint_variant": "pcsx",
            "organization_id": "example.com",
            "max_pages": 1,
        }
        with TemporaryDirectory() as temp_dir:
            current = Path(temp_dir) / "current.yaml"
            candidate = Path(temp_dir) / "candidate.yaml"
            current.write_text(baseline, encoding="utf-8", newline="\n")
            candidate.write_bytes(validator.build_candidate_registry_bytes(current, [addition]))
            _, entries = validator.load_registry(candidate)
        self.assertEqual([entry["name"] for entry in entries], ["Synthetic Baseline", "Synthetic Candidate"])

    def test_05_duplicate_normalized_company_is_rejected(self):
        duplicates = validator._duplicate_company_owners(
            [{"name": "Existing Company"}],
            [{"name": "  existing   company  ", "platform": "greenhouse", "slug": "other"}],
        )
        self.assertEqual(duplicates, {0: "Existing Company"})

    def test_06_duplicate_normalized_source_identity_is_rejected(self):
        current = {
            "name": "Existing",
            "platform": "eightfold",
            "endpoint_url": "https://careers.example.com/api/pcsx/search",
        }
        addition = {
            "name": "Other",
            "platform": "eightfold",
            "endpoint_url": "https://CAREERS.example.com/api/pcsx/search",
        }
        self.assertEqual(validator._duplicate_owners([current], [addition]), {0: "Existing"})


class QuickWinsEightfoldContractTests(unittest.TestCase):
    def test_07_registry_validation_uses_the_existing_eightfold_parser(self):
        self.assertIs(validator.ADAPTERS["eightfold"], eightfold.fetch_jobs)

    def test_08_unsupported_eightfold_variants_remain_rejected(self):
        target = CompanyTarget(
            name="Unsupported",
            platform="eightfold",
            careers_url="https://careers.example.com/careers",
            endpoint_url="https://careers.example.com/api/pcsx/search",
            endpoint_variant="smartapply_embedded",
            organization_id="example.com",
            max_pages=1,
        )
        with self.assertRaisesRegex(AdapterError, "endpoint_variant"):
            eightfold.validate_target(target)

    def test_09_stable_job_ids_are_required(self):
        invalid = job()
        invalid.job_id = ""
        self.assertIn("stable job_id/source", validator._valid_job_error([invalid]) or "")

    def test_10_official_apply_urls_are_required(self):
        invalid = job()
        invalid.url = ""
        self.assertIn("valid apply URL", validator._valid_job_error([invalid]) or "")


class ParentSourceMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mappings = load_parent_source_mappings(MAPPINGS)

    def test_11_parent_mapping_uses_normalized_exact_matching(self):
        matched = matching_child_brands(job(departments=[" instagram "]), self.mappings)
        self.assertEqual(matched, ["Instagram"])

    def test_12_parent_mapping_does_not_use_weak_substring_matching(self):
        candidate = job(
            departments=["Instagram Reels"],
            title="Instagram Infrastructure Engineer",
        )
        self.assertEqual(matching_child_brands(candidate, self.mappings), [])
        with self.assertRaisesRegex(ValueError, "structured exact-match"):
            ParentSourceMapping.from_dict(
                {
                    "child_company": "Unsafe",
                    "parent_company": "Parent",
                    "canonical_source": "custom:parent",
                    "official_child_careers_url": "https://example.com/careers",
                    "official_parent_search_url": "https://example.com/jobs",
                    "match": {"field": "title", "exact_values": ["Unsafe"]},
                    "status": "safe_with_metadata_mapping",
                }
            )

    def test_13_child_metadata_does_not_change_the_parent_source_key(self):
        original = job(departments=["Instagram"])
        annotated = annotate_child_brand_metadata(original, self.mappings)
        self.assertEqual(canonical_job_key(original), canonical_job_key(annotated))

    def test_14_canonical_parent_company_and_source_are_preserved(self):
        original = job(departments=["Instagram"])
        annotated = annotate_child_brand_metadata(original, self.mappings)
        self.assertEqual((annotated.company, annotated.source), ("Meta", "custom:meta"))

    def test_15_child_brand_metadata_is_deterministic_and_supports_overlap(self):
        original = job(departments=["WhatsApp", "Instagram"])
        first = annotate_child_brand_metadata(original, self.mappings)
        second = annotate_child_brand_metadata(original, self.mappings)
        self.assertEqual(first.raw_metadata["child_brands"], ["Instagram", "WhatsApp"])
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_16_unsafe_duplicate_risk_mappings_are_not_activated(self):
        unsafe = (
            "company_parent_source_mappings:\n"
            "  - child_company: Unsafe Child\n"
            "    parent_company: Parent\n"
            "    canonical_source: custom:parent\n"
            "    official_child_careers_url: https://example.com/child\n"
            "    official_parent_search_url: https://example.com/jobs\n"
            "    match:\n"
            "      field: departments\n"
            "      exact_values: [Unsafe Child]\n"
            "    status: unsafe_duplicate_risk\n"
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unsafe.yaml"
            path.write_text(unsafe, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Only validated safe"):
                load_parent_source_mappings(path)


class QuickWinsIsolationTests(unittest.TestCase):
    def _exercise_mapping(self):
        mappings = load_parent_source_mappings(MAPPINGS)
        return annotate_child_brand_metadata(job(departments=["Instagram"]), mappings)

    def test_17_mapping_validation_initializes_no_supabase(self):
        from src.integrations.supabase_job_store import SupabaseJobStore

        with patch.object(SupabaseJobStore, "from_env", side_effect=AssertionError("Supabase initialized")) as initialize:
            self._exercise_mapping()
        initialize.assert_not_called()

    def test_18_mapping_validation_initializes_no_google_sheets(self):
        from src.integrations.google_sheets_publisher import GoogleSheetsPublisher

        with patch.object(GoogleSheetsPublisher, "from_env", side_effect=AssertionError("Sheets initialized")) as initialize:
            self._exercise_mapping()
        initialize.assert_not_called()

    def test_19_mapping_validation_initializes_no_sqlite(self):
        with patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite initialized")) as initialize:
            self._exercise_mapping()
        initialize.assert_not_called()

    def test_20_historical_config_matches_report_and_current_production_prefix(self):
        historical = VALIDATED.read_bytes()
        production = PRODUCTION.read_bytes()
        report = json.loads(REPORT.read_text(encoding="utf-8"))
        digest = hashlib.sha256(historical).hexdigest().upper()
        self.assertEqual(digest, report["production_registry_sha256"])
        self.assertEqual(production[: len(historical)], historical)

        _, historical_entries = validator.load_registry(VALIDATED)
        _, production_entries = validator.load_registry(PRODUCTION)
        self.assertGreaterEqual(len(production_entries), len(historical_entries))
        self.assertEqual(
            production_entries[: len(historical_entries)],
            historical_entries,
        )


if __name__ == "__main__":
    unittest.main()
