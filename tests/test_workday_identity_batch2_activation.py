from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from scripts import validate_workday_batch2_activation_package as package
from scripts import validate_workday_recovery_activation as recovery
from scripts.validate_source_integrity import validate_source_integrity
from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = (
    ROOT / "config" / "target_companies.workday_identity_batch2_candidates.yaml"
)
PROPOSAL = (
    ROOT
    / "config"
    / "target_companies.workday_identity_batch2_activation_proposal.yaml"
)
ROLLBACK = (
    ROOT
    / "config"
    / "target_companies.workday_identity_batch2_activation_rollback.yaml"
)
ORIGINAL_ROLLBACK = (
    ROOT / "config" / "target_companies.workday_identity_batch2_rollback.yaml"
)
REVIEW = ROOT / "config" / "workday_identity_batch2_activation_review.yaml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def rows(path: Path) -> list[dict]:
    return load(path)["target_companies"]


def by_name(path: Path) -> dict[str, dict]:
    return {row["name"]: row for row in rows(path)}


def structural_digest(data: dict) -> str:
    payload = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_activation_proposal_is_exact_tested_fifteen_company_subset() -> None:
    proposal = by_name(PROPOSAL)
    candidates = by_name(CANDIDATES)

    assert tuple(proposal) == package.APPROVED_COMPANIES
    assert proposal == {name: candidates[name] for name in package.APPROVED_COMPANIES}
    assert set(proposal).isdisjoint(package.EXCLUDED_COMPANIES)


def test_approved_rows_preserve_default_scope_provider_and_exact_identity() -> None:
    proposal = by_name(PROPOSAL)
    rollback = by_name(ROLLBACK)

    for name, row in proposal.items():
        assert row["source_scope"] == "default"
        for field in ("name", "tenant", "wd_subdomain", "site_id"):
            assert row[field] == rollback[name][field]

        evidence = row["workday_identity"]
        assert evidence["expected_source_scope"] == "default"
        assert evidence["expected_employer_name"] == row["name"]
        assert evidence["expected_workday_tenant"] == row["tenant"]
        assert evidence["expected_workday_site"] == row["site_id"]
        assert evidence["expected_apply_hostname"] in row["allowed_apply_hosts"]

        target = CompanyTarget.from_dict(row)
        hostname = f"{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
        status, diagnostic = workday._configured_identity_evidence(
            target, str(target.site_id), hostname
        )
        assert status == "validated", (row["name"], diagnostic)


def test_production_rows_equal_the_exact_activated_proposal() -> None:
    production = by_name(PRODUCTION)
    proposal = by_name(PROPOSAL)
    rollback = by_name(ROLLBACK)
    original_rollback = by_name(ORIGINAL_ROLLBACK)

    assert tuple(rollback) == package.APPROVED_COMPANIES
    assert proposal == {name: production[name] for name in package.APPROVED_COMPANIES}
    assert rollback == {
        name: original_rollback[name] for name in package.APPROVED_COMPANIES
    }

    review = load(REVIEW)
    digest = recovery.baseline_record()["sha256"]["config/target_companies.yaml"]
    assert digest == review["package"]["activated_production_registry_sha256"]
    assert review["package"]["production_registry_modified"] is True
    assert review["package"]["activation_approved"] is True
    assert review["package"]["activation_executed"] is True


def test_simulated_activation_changes_no_unrelated_production_row() -> None:
    production = by_name(PRODUCTION)
    proposal = by_name(PROPOSAL)
    merged_rows = package.build_activation_registry(activation_approved=True)[
        "target_companies"
    ]
    merged = {row["name"]: row for row in merged_rows}

    assert list(merged) == list(production)
    for name, row in production.items():
        assert merged[name] == proposal.get(name, row)


def test_exact_rollback_reconstructs_the_pre_activation_registry() -> None:
    production_data = recovery.registry_before_recovery()
    production = {r["name"]: r for r in production_data["target_companies"]}
    rollback = by_name(ROLLBACK)
    restored = deepcopy(production_data)
    restored["target_companies"] = [
        deepcopy(rollback.get(row["name"], row))
        for row in production_data["target_companies"]
    ]
    restored_by_name = {row["name"]: row for row in restored["target_companies"]}

    assert list(restored_by_name) == list(production)
    for name, row in restored_by_name.items():
        assert row == rollback.get(name, production[name])

    review = load(REVIEW)
    assert structural_digest(restored) == review["package"][
        "baseline_production_registry_structural_sha256"
    ]


def test_activated_registry_has_no_identity_or_provider_collisions() -> None:
    result = package.validate_package()
    production_rows = rows(PRODUCTION)
    logical_keys = [package.logical_scope_key(row) for row in production_rows]
    provider_keys = [
        key
        for row in production_rows
        if (key := package.workday_provider_key(row)) is not None
    ]

    assert result["status"] == "PASS"
    assert result["registry_state"] == "activated"
    assert len(logical_keys) == len(set(logical_keys))
    assert len(provider_keys) == len(set(provider_keys))


def test_historically_excluded_companies_have_exact_baseline_rows() -> None:
    production = {r["name"]: r for r in recovery.registry_before_recovery()["target_companies"]}
    original_rollback = by_name(ORIGINAL_ROLLBACK)
    excluded = load(REVIEW)["excluded_companies"]

    assert set(excluded) == set(package.EXCLUDED_COMPANIES)
    for name in package.EXCLUDED_COMPANIES:
        assert production[name] == original_rollback[name]
        assert excluded[name]["production_changed"] is False

    assert excluded["Valeo"]["canary_outcome"] == "FAIL_CONTRACT"
    assert excluded["Valeo"]["failure_reason"] == "collection_mutation"
    assert excluded["Broadcom"]["status"] == "BLOCKED_PENDING_EVIDENCE"
    assert excluded["Enbridge"]["status"] == "BLOCKED_PENDING_EVIDENCE"
    assert excluded["Sony"]["status"] == "NEEDS_SCOPE_MIGRATION"
    assert excluded["Shell"]["status"] == "NEEDS_SCOPE_MIGRATION"


def test_reported_change_fields_match_exact_baseline_diff() -> None:
    rollback = by_name(ROLLBACK)
    proposal = by_name(PROPOSAL)
    report_rows = {
        row["company"]: row for row in load(REVIEW)["activated_companies"]
    }

    assert set(report_rows) == set(package.APPROVED_COMPANIES)
    for name in package.APPROVED_COMPANIES:
        expected = {
            key
            for key in set(rollback[name]) | set(proposal[name])
            if rollback[name].get(key, object()) != proposal[name].get(key, object())
        }
        assert set(report_rows[name]["changed_fields"]) == expected
        assert report_rows[name]["canary_outcome"] == "PASS_COMPLETE"


def test_lifecycle_state_and_policy_are_explicitly_unchanged() -> None:
    lifecycle = load(REVIEW)["lifecycle_preservation"]

    assert lifecycle["possibly_missing_ownership_records"] == 22
    assert lifecycle["inspection_was_read_only"] is True
    assert lifecycle["inspection_repeated_by_this_task"] is False
    assert lifecycle["counters_reset_or_modified"] is False
    assert lifecycle["closure_policy_changed"] is False
    assert lifecycle["recovery_framework_added"] is False
    assert lifecycle["logical_scope_migrations"] == 0


def test_missing_or_conflicting_identity_evidence_fails_closed() -> None:
    row = rows(PROPOSAL)[0]
    missing = deepcopy(row)
    missing.pop("workday_identity")
    missing_target = CompanyTarget.from_dict(missing)
    hostname = (
        f"{missing_target.tenant}.{missing_target.wd_subdomain}.myworkdayjobs.com"
    )
    status, _ = workday._configured_identity_evidence(
        missing_target, str(missing_target.site_id), hostname
    )
    assert status == "unproven"

    conflicting = deepcopy(row)
    conflicting["workday_identity"]["expected_source_scope"] = "conflict"
    conflicting_target = CompanyTarget.from_dict(conflicting)
    status, diagnostic = workday._configured_identity_evidence(
        conflicting_target, str(conflicting_target.site_id), hostname
    )
    assert status == "failed"
    assert diagnostic["exact_binding"] is False


def test_unapproved_activation_is_rejected() -> None:
    with pytest.raises(package.ActivationApprovalRequired, match="explicit operator approval"):
        package.build_activation_registry(activation_approved=False)


def test_package_and_strict_source_integrity_validators_are_offline() -> None:
    package_result = package.validate_package()
    integrity = validate_source_integrity(
        registry_path=PROPOSAL,
        retained_pattern="jobs/no-workday-batch2-activation-retained-coverage-*.json",
    )

    assert package_result["status"] == "PASS"
    assert package_result["network_requests"] == 0
    assert package_result["production_writes"] == 0
    assert integrity["network_requests"] == 0
    assert integrity["summary"]["sources"] == len(package.APPROVED_COMPANIES)
    assert integrity["summary"]["ARCHITECTURAL"] == 0


def test_review_uses_only_operator_supplied_live_evidence() -> None:
    review = load(REVIEW)
    evidence = review["operator_supplied_live_evidence"]
    summary = review["activation_summary"]

    assert evidence["fresh_validation_performed_by_this_task"] is False
    assert evidence["canary_run_date"] == "2026-10-19"
    assert evidence["common_result"]["canary_outcome"] == "PASS_COMPLETE"
    assert tuple(evidence["companies"]) == package.APPROVED_COMPANIES
    assert summary["activated"] == 15
    assert summary["excluded"] == 5
