from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = ROOT / "config" / "pagination_completeness_recovery_batch1.yaml"
REGISTRY_PATH = ROOT / "config" / "target_companies.yaml"
REPAIRS_PATH = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"
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
    "RECOVERED",
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
        "RECOVERED": 2,
        "RECOVERED_BY_SHARED_FIX": 0,
        "NEEDS_SOURCE_SPECIFIC_REPAIR": 11,
        "FAIL_IDENTITY": 6,
        "FAIL_CONTRACT": 1,
    }


def test_outcome_groups_are_disjoint_and_cover_the_batch() -> None:
    groups = _review()["outcome_groups"]
    flattened = [company for members in groups.values() for company in members]

    assert len(flattened) == len(set(flattened)) == 20
    assert set(flattened) == set(EXPECTED_COMPANIES)
    assert groups["A_LIVE_VALIDATED_RECOVERED"] == [
        "Shopify",
        "Intuitive Surgical",
    ]
    assert len(groups["B_NEEDS_MORE_DETERMINISTIC_REPAIR"]) == 11
    assert len(groups["C_BLOCKED_NO_SAFE_REPAIR"]) == 1
    assert len(groups["D_RECLASSIFIED_NOT_PAGINATION"]) == 6


def test_review_is_anchored_to_activated_registry_and_runtime_evidence() -> None:
    evidence = _review()["evidence_boundary"]

    assert evidence["production_registry_changed"] is True
    assert evidence["live_validation_performed"] is True
    assert evidence["live_validation_performed_by"] == "operator"
    assert evidence["production_registry_after_activation"]["sha256"] == (
        "4ce96583c98f6520d77bf3c0c0c4642a969594ad4b341e41fb0c39b3f55166ef"
    )
    assert evidence["production_registry_current_after_later_activations"][
        "sha256"
    ] == _sha256(REGISTRY_PATH)
    assert evidence["production_reliability_overlay_after_activation"][
        "sha256"
    ] == _sha256(REPAIRS_PATH)
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


def test_live_closeout_and_historical_commands_are_recorded_exactly() -> None:
    review = _review()
    rows = {row["company"]: row for row in review["companies"]}
    commanded = {
        company
        for canary in review["historical_manual_canaries"]
        for company in canary["companies"]
    }

    assert commanded == {"Atlassian", "Shopify", "Intuitive Surgical"}
    assert [
        row["request_budget_per_source"]
        for row in review["historical_manual_canaries"]
    ] == [
        1,
        8,
    ]
    assert all(
        "--run" in row["command"] for row in review["historical_manual_canaries"]
    )
    assert rows["Shopify"]["live_validation"]["validation_status"] == "PASS_COMPLETE"
    assert rows["Intuitive Surgical"]["live_validation"]["validation_status"] == "PASS_COMPLETE"
    assert rows["Atlassian"]["live_validation"]["validation_status"] == "LIVE_CANARY_FAILED"
    assert rows["Atlassian"]["live_validation"]["exact_error"] == (
        "AdapterError: Atlassian Careers job URL did not match the observed "
        "iCIMS host and stable-ID path contract"
    )
    assert rows["Atlassian"]["recovery_state"] == "NOT_RECOVERED"


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
