from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = ROOT / "config" / "pagination_completeness_recovery_batch1.yaml"
REGISTRY_PATH = ROOT / "config" / "target_companies.yaml"
SNAPSHOT_PATH = ROOT / "jobs" / "company_careers_coverage_20261001_142550.json"

EXPECTED_COMPANIES = [
    "Atlassian",
    "Shopify",
    "Meta",
    "JPMorgan Chase",
    "Emerson",
    "Honeywell",
    "Microsoft",
    "Qualcomm",
    "Micron",
    "Eaton",
    "Samsung Electronics",
    "GE Vernova",
    "Rivian",
    "General Motors",
    "Caterpillar",
    "Intuitive Surgical",
    "Coupang",
    "Zalando",
    "Capital One",
    "Goldman Sachs",
]
ALLOWED_STATUSES = {
    "RECOVERED_BY_SHARED_FIX",
    "READY_FOR_CANARY",
    "NEEDS_SOURCE_SPECIFIC_REPAIR",
    "FAIL_IDENTITY",
    "FAIL_CONTRACT",
    "FAIL_TRANSIENT",
    "UNSUPPORTED_PROVIDER",
    "NO_SAFE_REPAIR",
    "INSUFFICIENT_EVIDENCE",
}


def _review() -> dict:
    return yaml.safe_load(REVIEW_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_batch_has_each_requested_company_exactly_once_in_order() -> None:
    rows = _review()["companies"]

    assert [row["company"] for row in rows] == EXPECTED_COMPANIES
    assert len({row["company"] for row in rows}) == 20
    assert all(row["source_scope"] == "default" for row in rows)


def test_each_company_has_one_allowed_final_status_and_counts_reconcile() -> None:
    review = _review()
    statuses = [row["status"] for row in review["companies"]]
    counts = {
        status: statuses.count(status) for status in review["status_counts"]
    }

    assert set(statuses).issubset(ALLOWED_STATUSES)
    assert counts == review["status_counts"]
    assert counts == {
        "READY_FOR_CANARY": 3,
        "RECOVERED_BY_SHARED_FIX": 0,
        "NEEDS_SOURCE_SPECIFIC_REPAIR": 10,
        "FAIL_IDENTITY": 6,
        "FAIL_CONTRACT": 1,
    }


def test_outcome_groups_are_disjoint_and_cover_the_batch() -> None:
    groups = _review()["outcome_groups"]
    flattened = [company for members in groups.values() for company in members]

    assert len(flattened) == len(set(flattened)) == 20
    assert set(flattened) == set(EXPECTED_COMPANIES)
    assert len(groups["A_READY_FOR_MANUAL_CANARY"]) == 3
    assert len(groups["B_NEEDS_MORE_DETERMINISTIC_REPAIR"]) == 10
    assert len(groups["C_BLOCKED_NO_SAFE_REPAIR"]) == 1
    assert len(groups["D_RECLASSIFIED_NOT_PAGINATION"]) == 6


def test_review_is_anchored_to_unchanged_registry_and_runtime_evidence() -> None:
    evidence = _review()["evidence_boundary"]

    assert evidence["production_registry_changed"] is False
    assert evidence["live_validation_performed"] is False
    assert evidence["production_registry"]["sha256"] == _sha256(REGISTRY_PATH)
    assert evidence["newest_full_runtime_snapshot"]["sha256"] == _sha256(
        SNAPSHOT_PATH
    )


def test_recorded_platforms_match_the_production_registry() -> None:
    review_rows = {row["company"]: row for row in _review()["companies"]}
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))[
        "target_companies"
    ]
    production = {row["name"]: row for row in registry}

    assert all(
        review_rows[name]["platform"] == production[name]["platform"]
        for name in EXPECTED_COMPANIES
    )


def test_ready_canaries_are_exactly_the_reviewed_bounded_commands() -> None:
    review = _review()
    ready = {
        row["company"]
        for row in review["companies"]
        if row["status"] in {"READY_FOR_CANARY", "RECOVERED_BY_SHARED_FIX"}
    }
    commanded = {
        company
        for canary in review["manual_canaries"]
        for company in canary["companies"]
    }

    assert ready == commanded == {"Atlassian", "Shopify", "Intuitive Surgical"}
    assert [row["request_budget_per_source"] for row in review["manual_canaries"]] == [
        1,
        8,
    ]
    assert all("--run" in row["command"] for row in review["manual_canaries"])


def test_mutation_and_identity_failures_are_not_promoted_by_budget() -> None:
    rows = {row["company"]: row for row in _review()["companies"]}
    mutation_sources = {
        "JPMorgan Chase",
        "Emerson",
        "Honeywell",
        "Microsoft",
        "Qualcomm",
        "Micron",
        "Eaton",
        "Rivian",
    }
    identity_sources = {
        "Samsung Electronics",
        "GE Vernova",
        "General Motors",
        "Caterpillar",
        "Zalando",
        "Capital One",
    }

    assert all(rows[name]["status"] == "NEEDS_SOURCE_SPECIFIC_REPAIR" for name in mutation_sources)
    assert all(rows[name]["status"] == "FAIL_IDENTITY" for name in identity_sources)
    assert all(
        rows[name]["request_budget"]["config_only_fix"] is False
        for name in mutation_sources | identity_sources
    )


def test_no_shared_semantic_relaxation_was_implemented() -> None:
    shared = _review()["shared_repairs"]

    assert shared["implemented"] == []
    assert {row["family"] for row in shared["rejected"]} == {
        "oracle_recruiting",
        "eightfold_pcsx",
        "workday_wday_cxs_v1",
        "static_html",
    }


def test_pass_complete_gate_set_includes_authority_and_accounting() -> None:
    gates = set(_review()["pass_complete_gates"])

    assert {
        "canonical_employer_identity_validated",
        "terminal_evidence_valid",
        "collection_mutation_absent",
        "every_inventory_item_accounted",
        "unreadable_identifiers_protected",
        "central_finalizer_authoritative",
    }.issubset(gates)
