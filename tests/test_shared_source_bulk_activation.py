from __future__ import annotations

import ast
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from tests.company_careers_reliability_assertions import assert_historical_prefix

from scripts import validate_company_registry as validator
from scripts import validate_shared_api_batch as rollout
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import CompanyTarget, JobRecord
from src.scraper.company_careers.parent_sources import (
    ParentSourceMapping,
    annotate_child_brand_metadata,
    canonical_job_key,
    load_parent_source_mappings,
    original_company_coverage,
)
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]


def document(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def inventory_rows():
    with rollout.CANDIDATE_INVENTORY.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def target_entries(path: Path):
    return document(path)["target_companies"]


def mapping_entries(path: Path):
    return document(path)["company_parent_source_mappings"]


def historical_target_prefix():
    entries = target_entries(rollout.CANDIDATE_REGISTRY)
    reviewed = {validator._name_key(item) for item in rollout.CANONICAL_ENTRIES}
    first_reviewed = next(
        index for index, item in enumerate(entries)
        if validator._name_key(item) in reviewed
    )
    return entries[:first_reviewed]


def historical_mapping_prefix():
    mappings = mapping_entries(rollout.CANDIDATE_REGISTRY)
    first_deepmind = next(
        index for index, item in enumerate(mappings)
        if validator._name_key({"name": item["child_company"]}) == "google deepmind"
    )
    return mappings[:first_deepmind]


def additions(path: Path):
    historical = {validator._name_key(item) for item in historical_target_prefix()}
    return [item for item in target_entries(path) if validator._name_key(item) not in historical]


class SharedSourceBulkActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads(rollout.REPORT.read_text(encoding="utf-8"))
        cls.inventory = inventory_rows()

    def test_01_exact_candidate_set_is_derived_from_research(self):
        self.assertEqual([row["company"] for row in self.inventory], rollout.EXPECTED_COMPANIES)
        self.assertEqual(
            [item["name"] for item in rollout.CANONICAL_ENTRIES],
            [
                "Netflix", "Wise", "Jump Trading", "Optiver", "BlackRock",
                "Schneider Electric", "Continental", "Snap", "bp",
                "Canadian Natural Resources", "Astrobotic", "Relativity Space",
            ],
        )
        self.assertEqual(
            [item["child_company"] for item in rollout.PARENT_CANDIDATES],
            [
                "Google DeepMind", "Clearpath Robotics", "OTTO Motors",
                "Imperial Oil", "Walmart Global Tech", "Uber Eats",
                "Costco Technology", "X", "Threads", "WeChat",
            ],
        )

    def test_02_historical_baselines_excluded_reviewed_companies(self):
        production = {validator._name_key(item) for item in historical_target_prefix()}
        mapped = {
            validator._name_key({"name": item["child_company"]})
            for item in historical_mapping_prefix()
        }
        reviewed = {validator._name_key({"name": name}) for name in rollout.EXPECTED_COMPANIES}
        self.assertFalse(reviewed & production)
        self.assertFalse(reviewed & mapped)

    def test_03_candidate_order_follows_master_workbook(self):
        ranks = [int(row["master_rank"]) for row in self.inventory]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(ranks, [rollout._master_ranks()[name] for name in rollout.EXPECTED_COMPANIES])

    def test_04_milestone_artifacts_preserve_historical_prefixes(self):
        targets = historical_target_prefix()
        mappings = historical_mapping_prefix()
        for path in (rollout.CANDIDATE_REGISTRY, rollout.VALIDATED_REGISTRY):
            self.assertEqual(target_entries(path)[:len(targets)], targets)
            self.assertEqual(mapping_entries(path)[:len(mappings)], mappings)

    def test_05_candidate_and_production_registries_are_loader_compatible(self):
        historical_count = len(historical_target_prefix())
        self.assertEqual(
            len(load_target_companies(str(rollout.CANDIDATE_REGISTRY))),
            historical_count + len(rollout.CANONICAL_ENTRIES),
        )
        production = target_entries(rollout.PRODUCTION)
        self.assertEqual(
            len(load_target_companies(str(rollout.PRODUCTION))),
            len(production),
        )
        self.assertGreaterEqual(
            len(production),
            historical_count + len(rollout.SAFE_CANONICAL_NAMES),
        )

    def test_06_validated_registry_contains_only_safe_canonical_sources(self):
        self.assertEqual([item["name"] for item in additions(rollout.VALIDATED_REGISTRY)], rollout.SAFE_CANONICAL_NAMES)
        self.assertEqual(
            len(load_target_companies(str(rollout.VALIDATED_REGISTRY))),
            len(historical_target_prefix()) + len(rollout.SAFE_CANONICAL_NAMES),
        )

    def test_07_candidate_registry_contains_all_canonical_candidates(self):
        self.assertEqual(
            [item["name"] for item in additions(rollout.CANDIDATE_REGISTRY)],
            [item["name"] for item in rollout.CANONICAL_ENTRIES],
        )

    def test_08_existing_parent_mappings_are_unchanged_and_first(self):
        historical = historical_mapping_prefix()
        self.assertEqual(
            [item["child_company"] for item in historical],
            ["Instagram", "Universal Robots", "WhatsApp"],
        )
        for path in (
            rollout.PRODUCTION_MAPPINGS,
            rollout.CANDIDATE_REGISTRY,
            rollout.VALIDATED_REGISTRY,
        ):
            self.assertEqual(mapping_entries(path)[:len(historical)], historical)

    def test_09_validated_mapping_is_only_exact_deepmind_rule(self):
        mappings = mapping_entries(rollout.VALIDATED_REGISTRY)
        self.assertEqual(len(mappings), len(historical_mapping_prefix()) + 1)
        self.assertEqual(
            sum(item["child_company"] == "Google DeepMind" for item in mappings), 1
        )
        deepmind = mappings[-1]
        self.assertEqual(
            (deepmind["child_company"], deepmind["parent_company"], deepmind["canonical_source"],
             deepmind["match_field"], deepmind["accepted_values"]),
            ("Google DeepMind", "Google", "custom:google", "departments", ["DeepMind"]),
        )
        self.assertTrue(deepmind["enabled"])
        ParentSourceMapping.from_dict(deepmind)

    def test_10_candidate_mapping_order_and_uniqueness(self):
        mappings = mapping_entries(rollout.CANDIDATE_REGISTRY)[len(historical_mapping_prefix()):]
        self.assertEqual([item["child_company"] for item in mappings], [item["child_company"] for item in rollout.PARENT_CANDIDATES])
        normalized = [validator._name_key({"name": item["child_company"]}) for item in mappings]
        self.assertEqual(len(normalized), len(set(normalized)))

    def test_11_mapped_child_cannot_coexist_as_independent_source(self):
        target_names = {validator._name_key(item) for item in target_entries(rollout.CANDIDATE_REGISTRY)}
        children = {validator._name_key({"name": item["child_company"]}) for item in rollout.PARENT_CANDIDATES}
        self.assertFalse(target_names & children)
        production_names = {
            validator._name_key(item) for item in target_entries(rollout.PRODUCTION)
        }
        self.assertNotIn("google deepmind", production_names)

    def test_12_duplicate_company_and_source_identities_are_absent(self):
        for path in (rollout.CANDIDATE_REGISTRY, rollout.PRODUCTION):
            entries = target_entries(path)
            names = [validator._name_key(item) for item in entries]
            sources = [validator.source_key(item) for item in entries]
            sources = [source for source in sources if source]
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(len(sources), len(set(sources)))

    def test_13_synthetic_milestone_delta_is_one(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "base.yaml"
            baseline.write_text("target_companies:\n  - name: Base\n    platform: greenhouse\n    slug: base\n", encoding="utf-8")
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_bytes(validator.build_candidate_registry_bytes(
                baseline, [{"name": "Delta", "platform": "lever", "slug": "delta"}],
            ))
            self.assertEqual(len(load_target_companies(str(candidate))) - len(load_target_companies(str(baseline))), 1)

    def test_14_reclassified_sources_use_production_adapters(self):
        by_name = {item["name"]: item for item in rollout.CANONICAL_ENTRIES}
        expected = {
            "Wise": "static_html", "Jump Trading": "greenhouse", "Optiver": "static_html",
            "BlackRock": "static_html", "Snap": "static_html", "bp": "workday",
            "Canadian Natural Resources": "oracle_recruiting", "Relativity Space": "greenhouse",
        }
        self.assertEqual({name: by_name[name]["platform"] for name in expected}, expected)
        self.assertTrue(all(platform in scanner.ADAPTERS for platform in expected.values()))

    def test_15_reviewed_static_configurations_validate_without_network(self):
        for item in rollout.CANONICAL_ENTRIES:
            if item["platform"] == "static_html":
                static_html.validate_target(load_target_companies_from_item(item))

    def test_16_no_company_name_branches_were_added_to_adapters(self):
        adapter_root = ROOT / "src" / "scraper" / "company_careers" / "adapters"
        constants = set()
        for path in adapter_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            constants.update(
                node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            )
        for name in rollout.EXPECTED_COMPANIES:
            self.assertNotIn(name, constants)

    def test_17_unproven_adapter_families_have_no_empty_test_or_adapter_files(self):
        for filename in ("public_rest.py", "public_graphql.py", "oracle_taleo.py"):
            self.assertFalse((ROOT / "src" / "scraper" / "company_careers" / "adapters" / filename).exists())
        for filename in ("test_public_rest_adapter.py", "test_public_graphql_adapter.py", "test_adp_adapter.py", "test_oracle_taleo_adapter.py"):
            self.assertFalse((ROOT / "tests" / filename).exists())

    def test_18_production_dispatch_and_parent_annotator_are_used(self):
        source = Path(rollout.__file__).read_text(encoding="utf-8")
        self.assertIn("adapters=scanner.ADAPTERS", source)
        self.assertIn("scanner.CompanyCareersScanner", source)
        self.assertIn("parent_source_mappings=annotations", source)

    def test_19_validation_batches_and_limits_are_bounded(self):
        self.assertEqual((rollout.MAX_BATCH_SIZE, rollout.MAX_PAGES, rollout.MAX_JOBS), (5, 2, 20))
        self.assertTrue(all(len(batch) <= 5 for batch in self.report["network_sub_batches"]))
        self.assertTrue(all(item["max_pages"] <= 2 and item["max_jobs"] <= 20 for item in rollout.CANONICAL_ENTRIES))
        self.assertTrue(all(row["normalized_jobs_inspected"] <= 20 for row in self.report["canonical_candidates"]))
        self.assertTrue(all(row["parent_jobs_inspected"] <= 20 for row in self.report["parent_mapping_candidates"]))

    def test_20_validator_imports_no_persistence_or_dashboard_modules(self):
        tree = ast.parse(Path(rollout.__file__).read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        for forbidden in ("supabase", "gspread", "google_sheets", "sqlite3", "update_student_dashboard"):
            self.assertFalse(any(forbidden in name for name in imports), imports)

    def test_21_live_report_has_expected_safe_and_excluded_results(self):
        statuses = {row["company"]: row["status"] for row in self.report["canonical_candidates"]}
        for name in rollout.SAFE_CANONICAL_NAMES:
            self.assertEqual(statuses[name], validator.VALIDATED_WITH_JOBS)
        self.assertEqual(statuses["Netflix"], "unsupported_variant")
        self.assertEqual(statuses["Schneider Electric"], "blocked")
        self.assertEqual(statuses["Continental"], "unsupported_variant")
        self.assertEqual(statuses["Astrobotic"], "browser_required")

    def test_22_parent_results_reject_weak_attribution(self):
        rows = {row["child_company"]: row for row in self.report["parent_mapping_candidates"]}
        self.assertEqual(rows["Google DeepMind"]["status"], "validated_parent_mapping_with_current_jobs")
        for name in ("Clearpath Robotics", "OTTO Motors", "Uber Eats", "Threads"):
            self.assertEqual(rows[name]["status"], "weak_parent_attribution")
            self.assertFalse(rows[name]["safe_to_activate"])
        self.assertGreater(rows["Google DeepMind"]["current_exact_matches"], 0)

    def test_23_report_totals_and_projected_coverage_are_exact(self):
        totals = self.report["totals"]
        self.assertEqual(
            (totals["production_canonical_count"], totals["candidate_canonical_count"], totals["validated_canonical_count"]),
            (119, 131, 127),
        )
        self.assertEqual(
            (totals["production_mapped_count"], totals["candidate_mapped_count"], totals["validated_mapped_count"]),
            (3, 13, 4),
        )
        self.assertEqual(totals["projected_original_company_coverage"], 122)

    def test_24_validated_registry_coverage_is_derived_not_assumed(self):
        mappings = load_parent_source_mappings_from_registry(rollout.VALIDATED_REGISTRY)
        coverage = original_company_coverage(
            load_target_companies(str(rollout.VALIDATED_REGISTRY)), mappings,
            inventory_path=rollout.REMAINING,
        )
        self.assertEqual(coverage["total_original_companies_covered"], 122)

    def test_25_inventory_records_classification_changes_and_exclusions(self):
        rows = {row["company"]: row for row in self.inventory}
        self.assertEqual((rows["Wise"]["prior_classification"], rows["Wise"]["platform"]), ("custom_rest_api", "static_html"))
        self.assertEqual((rows["Snap"]["prior_classification"], rows["Snap"]["platform"]), ("custom_graphql", "static_html"))
        self.assertEqual((rows["bp"]["prior_classification"], rows["bp"]["platform"]), ("avature", "workday"))
        self.assertTrue(rows["Continental"]["exclusion_reason"])
        self.assertEqual(rows["Google DeepMind"]["exact_metadata_field"], "departments")

    def test_26_production_contains_exact_validated_activation(self):
        historical = historical_target_prefix()
        production = target_entries(rollout.PRODUCTION)
        validated = additions(rollout.VALIDATED_REGISTRY)
        assert_historical_prefix(self, production, historical)
        self.assertEqual(
            production[len(historical):len(historical) + len(validated)],
            validated,
        )
        self.assertEqual(
            [item["name"] for item in production[len(historical):len(historical) + len(validated)]],
            rollout.SAFE_CANONICAL_NAMES,
        )
        for expected in validated:
            matches = [
                item for item in production
                if validator._name_key(item) == validator._name_key(expected)
            ]
            self.assertEqual(matches, [expected])
            self.assertEqual(validator.source_key(matches[0]), validator.source_key(expected))
        rejected = {"Netflix"}
        self.assertFalse(rejected & {item["name"] for item in production})
        self.assertEqual(sum(item["name"] == "Continental" for item in production), 1)
        self.assertEqual(sum(item["name"] == "Astrobotic" for item in production), 1)

        batch_a = target_entries(
            ROOT / "config" / "target_companies.adapter_extensions_v4a_validated.yaml"
        )
        first_batch_a = next(
            index for index, item in enumerate(batch_a)
            if validator._name_key(item) == "onsemi"
        )
        batch_a_additions = batch_a[first_batch_a:]
        schneider = next(
            item for item in batch_a_additions
            if validator._name_key(item) == "schneider electric"
        )
        self.assertNotIn("Schneider Electric", [item["name"] for item in validated])
        self.assertEqual(
            validator.source_key(schneider),
            "icims:careers.se.com:schneider-electric-public",
        )
        self.assertEqual(
            [item for item in production if validator._name_key(item) == "schneider electric"],
            [schneider],
        )

    def test_27_production_mapping_is_exact_and_unique(self):
        historical = historical_mapping_prefix()
        production = mapping_entries(rollout.PRODUCTION_MAPPINGS)
        deepmind = mapping_entries(rollout.VALIDATED_REGISTRY)[-1]
        wandb = production[-1]
        self.assertEqual(wandb["child_company"], "Weights & Biases")
        self.assertEqual(wandb["parent_company"], "CoreWeave")
        self.assertEqual(wandb["canonical_source"], "greenhouse:coreweave")
        self.assertEqual(production, historical + [deepmind, wandb])
        children = [
            validator._name_key({"name": item["child_company"]})
            for item in production
        ]
        identities = [
            (
                item["canonical_source"].casefold(),
                item["match_field"],
                tuple(sorted(" ".join(value.casefold().split()) for value in item["accepted_values"])),
            )
            for item in production
        ]
        self.assertEqual(len(children), len(set(children)))
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(all(item["enabled"] for item in production))

    def test_28_deepmind_requires_exact_structured_metadata(self):
        mappings = load_parent_source_mappings(rollout.PRODUCTION_MAPPINGS)
        original = JobRecord(
            job_id="google-1",
            title="Software Engineer",
            company="Google",
            location="Canada",
            url="https://www.google.com/about/careers/applications/jobs/results/1",
            updated_at="",
            source="custom:google",
            description="General role",
            departments=[" DeepMind "],
        )
        annotated = annotate_child_brand_metadata(original, mappings)
        self.assertEqual(annotated.child_brands, ["Google DeepMind"])
        self.assertEqual(canonical_job_key(annotated), canonical_job_key(original))
        self.assertEqual(
            (annotated.company, annotated.job_id, annotated.url, annotated.source),
            (original.company, original.job_id, original.url, original.source),
        )
        false_positives = (
            {"title": "Google DeepMind Software Engineer"},
            {"description": "Build systems for Google DeepMind"},
            {"url": "https://careers.example/jobs/google-deepmind"},
        )
        for values in false_positives:
            candidate = JobRecord(
                job_id="google-false",
                title=values.get("title", "Software Engineer"),
                company="Google",
                location="Canada",
                url=values.get("url", "https://careers.example/jobs/1"),
                updated_at="",
                source="custom:google",
                description=values.get("description", "General role"),
                departments=["Engineering"],
            )
            self.assertNotIn(
                "Google DeepMind",
                annotate_child_brand_metadata(candidate, mappings).child_brands,
            )

    def test_29_google_is_scanned_once_and_keeps_one_canonical_row(self):
        mappings = load_parent_source_mappings(rollout.PRODUCTION_MAPPINGS)
        google_job = JobRecord(
            job_id="google-1",
            title="Software Engineer",
            company="Google",
            location="Canada",
            url="https://www.google.com/about/careers/applications/jobs/results/1",
            updated_at="",
            source="custom:google",
            departments=["DeepMind"],
        )
        adapter = Mock(return_value=[google_job])
        with patch.dict(scanner.ADAPTERS, {"google_test": adapter}, clear=False):
            report = scanner.CompanyCareersScanner(
                targets=[CompanyTarget(name="Google", platform="google_test")],
                parent_source_mappings=mappings,
            ).scan()
        adapter.assert_called_once()
        self.assertEqual(len(report.jobs), 1)
        self.assertEqual(report.jobs[0].company, "Google")
        self.assertEqual(report.jobs[0].child_brands, ["Google DeepMind"])
        self.assertEqual(report.to_dict()["canonical_sources_checked"], 1)
        deepmind = next(
            item for item in report.mapped_coverage
            if item["company"] == "Google DeepMind"
        )
        self.assertEqual(deepmind["mapped_jobs"], 1)


def load_target_companies_from_item(item):
    return CompanyTarget.from_dict(dict(item))


def load_parent_source_mappings_from_registry(path: Path):
    with tempfile.TemporaryDirectory() as directory:
        mapping_path = Path(directory) / "mappings.yaml"
        mapping_path.write_text(
            yaml.safe_dump({"company_parent_source_mappings": document(path)["company_parent_source_mappings"]}, sort_keys=False),
            encoding="utf-8",
        )
        return load_parent_source_mappings(mapping_path)


if __name__ == "__main__":
    unittest.main()
