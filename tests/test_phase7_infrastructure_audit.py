from __future__ import annotations

import json
from pathlib import Path

import yaml

from src.scraper.company_careers import scanner
from src.scraper.company_careers.phase7_audit import (
    BACKLOG_CATEGORIES,
    INFRASTRUCTURE_CATEGORIES,
    ROOT,
    build_summary,
    validate_audit,
)


AUDIT_PATH = ROOT / "config" / "phase7_infrastructure_audit.yaml"
REGISTRY_PATH = ROOT / "config" / "target_companies.yaml"
SNAPSHOT_PATH = ROOT / "jobs" / "company_careers_coverage_20261001_142550.json"


def _audit():
    return validate_audit(AUDIT_PATH)


def _classification_map(audit):
    return {
        company: category
        for category, companies in audit["runtime_problem_classifications"].items()
        for company in companies
    }


def test_every_runtime_incomplete_or_failed_source_has_exactly_one_classification():
    audit = _audit()
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected = {
        row["company"]
        for row in snapshot["coverage"]
        if row.get("scan_health") in {"incomplete", "failed"}
    }
    classified = _classification_map(audit)

    assert set(classified) == expected
    assert len(classified) == 61


def test_adapter_and_protocol_failures_are_not_mislabeled_as_infrastructure():
    classified = _classification_map(_audit())
    assert classified["Google"] == "B"
    assert classified["Chemelex"] == "E"
    assert classified["Bombardier"] == "D"
    assert classified["Cameco"] == "D"
    assert all(
        category not in INFRASTRUCTURE_CATEGORIES
        for name, category in classified.items()
        if name not in {"GitHub", "Parkland"}
    )
    assert set(classified.values()).issubset(BACKLOG_CATEGORIES | {"I"})


def test_every_production_browser_source_has_an_explicit_verdict():
    audit = _audit()
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["target_companies"]
    configured = {row["name"] for row in registry if row["platform"] == "browser_rendered_html"}
    reviewed = {row["company"] for row in audit["browser_sources"]}

    assert configured == reviewed == {"Goldman Sachs", "MediaTek", "Nutrien", "Stantec"}
    assert all(row["source_scope"] == "default" for row in audit["browser_sources"])
    assert all(row["implementation"] == "browser_rendered_html_v1" for row in audit["browser_sources"])


def test_proxy_candidates_require_source_specific_block_evidence():
    audit = _audit()
    candidates = audit["infrastructure_candidates"]

    assert {item["company"] for item in candidates} == {"GitHub", "Parkland"}
    assert all(item["category"] == "I" for item in candidates)
    assert all(item["evidence"]["snapshot_error"] for item in candidates)
    assert all(item["alternate_egress_plausibility"] == "unknown" for item in candidates)
    assert all(item["decision"] == "REMAIN_INCOMPLETE" for item in candidates)


def test_external_inventory_cannot_authorize_absence_without_first_party_proof():
    policy = _audit()["external_inventory_policy"]

    assert policy["requires_first_party"] is True
    assert policy["requires_identity_verifiable"] is True
    assert policy["requires_complete_and_bounded"] is True
    assert policy["third_party_authoritative_for_absence"] is False
    assert policy["eligible_current_candidates"] == []


def test_production_registry_platforms_and_rows_match_the_audited_snapshot():
    audit = _audit()
    summary = build_summary(AUDIT_PATH)

    assert summary["production"] == {"canonical_companies": 533, "configured_sources": 533}
    assert audit["infrastructure_enabled"]["production_platform_changes"] is False


def test_no_new_phase7_infrastructure_is_enabled():
    enabled = _audit()["infrastructure_enabled"]

    assert enabled
    assert not any(enabled.values())
    assert "phase7_audit" not in scanner.ADAPTERS


def test_phase7_audit_does_not_change_lifecycle_semantics():
    audit = _audit()

    assert audit["infrastructure_enabled"]["lifecycle_changes"] is False
    assert audit["decision"] == "PHASE7_CLOSE_EARLY"
    assert audit["primary_recommendation"] == "CLOSE_PHASE7_EARLY"


def test_runtime_and_known_remaining_counts_are_separate_and_reproducible():
    summary = build_summary(AUDIT_PATH)

    assert summary["newest_full_runtime_snapshot"]["complete"] == 472
    assert summary["newest_full_runtime_snapshot"]["incomplete"] == 47
    assert summary["newest_full_runtime_snapshot"]["failed"] == 14
    assert summary["runtime_problem_category_counts"] == {
        "B": 3,
        "C": 4,
        "D": 44,
        "E": 2,
        "F": 3,
        "I": 2,
        "L": 3,
    }
    assert summary["known_remaining_category_counts"] == {
        "B": 3,
        "C": 4,
        "D": 39,
        "E": 2,
        "F": 2,
        "I": 2,
        "L": 3,
    }
    assert summary["known_remaining_problem_sources"] == 55


def test_rejected_candidates_are_not_counted_as_retired_production_sources():
    counts = _audit()["candidate_and_retirement_counts"]

    assert counts["production_retired_or_rejected"] == 0
    assert counts["phase5_rejected_candidate_rows"] == 5
    assert counts["phase5_unique_rejected_proposals"] == 4
