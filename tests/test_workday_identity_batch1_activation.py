from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import validate_workday_batch1_activation_package as package
from scripts.validate_source_integrity import validate_source_integrity
from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = (
    ROOT / "config" / "target_companies.workday_identity_batch1_candidates.yaml"
)
PROPOSAL = (
    ROOT / "config" / "target_companies.workday_identity_batch1_activation_proposal.yaml"
)
ROLLBACK = (
    ROOT / "config" / "target_companies.workday_identity_batch1_activation_rollback.yaml"
)
REVIEW = ROOT / "config" / "workday_identity_batch1_activation_review.yaml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def rows(path: Path) -> list[dict]:
    return load(path)["target_companies"]


def by_name(path: Path) -> dict[str, dict]:
    return {row["name"]: row for row in rows(path)}


def test_activation_proposal_is_exact_tested_thirteen_company_subset() -> None:
    proposal = by_name(PROPOSAL)
    candidates = by_name(CANDIDATES)

    assert tuple(proposal) == package.APPROVED_COMPANIES
    assert proposal == {name: candidates[name] for name in package.APPROVED_COMPANIES}
    assert set(proposal).isdisjoint(package.MIGRATION_REVIEW_COMPANIES)


def test_approved_rows_preserve_default_scope_and_exact_identity() -> None:
    for row in rows(PROPOSAL):
        assert row["source_scope"] == "default"
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

    assert tuple(rollback) == package.APPROVED_COMPANIES
    assert proposal == {name: production[name] for name in package.APPROVED_COMPANIES}

    review = load(REVIEW)
    assert review["package"]["activated_production_registry_sha256"] == (
        "5efb8fb28f9405c2b3528d98a33e9f1c33475465f001a1d73fe681f995c2e168"
    )
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


def test_exact_rollback_can_be_reconstructed_without_unrelated_changes() -> None:
    production_rows = rows(PRODUCTION)
    production = by_name(PRODUCTION)
    rollback = by_name(ROLLBACK)
    restored = {
        row["name"]: rollback.get(row["name"], row)
        for row in production_rows
    }

    assert list(restored) == list(production)
    for name, row in restored.items():
        assert row == rollback.get(name, production[name])


def test_simulated_registry_has_no_duplicate_company_scope_identity() -> None:
    merged = package.build_activation_registry(activation_approved=True)
    keys = [package.logical_scope_key(row) for row in merged["target_companies"]]
    assert len(keys) == len(set(keys))


def test_reported_change_fields_match_exact_baseline_diff() -> None:
    rollback = by_name(ROLLBACK)
    proposal = by_name(PROPOSAL)
    report_rows = {
        row["company"]: row for row in load(REVIEW)["unchanged_scope_companies"]
    }

    assert set(report_rows) == set(package.APPROVED_COMPANIES)
    for name in package.APPROVED_COMPANIES:
        expected = {
            key
            for key in set(rollback[name]) | set(proposal[name])
            if rollback[name].get(key, object()) != proposal[name].get(key, object())
        }
        assert set(report_rows[name]["proposed_change_fields"]) == expected
        assert report_rows[name]["status"] == "READY_FOR_ACTIVATION"


def test_adobe_and_rbc_are_scope_migrations_not_activation_rows() -> None:
    production = by_name(PRODUCTION)
    candidates = by_name(CANDIDATES)
    audit = load(REVIEW)["migration_audits"]

    assert production["Adobe"].get("source_scope", "default") == "default"
    assert candidates["Adobe"]["source_scope"] == "experienced"
    assert audit["Adobe"]["classification"] == "NEEDS_SCOPE_MIGRATION"
    assert audit["Adobe"]["historical_default_owner_records"] == 332

    assert production["RBC"].get("source_scope", "default") == "default"
    assert candidates["RBC"]["source_scope"] == "global"
    assert audit["RBC"]["classification"] == "NEEDS_SCOPE_MIGRATION"
    assert audit["RBC"]["historical_default_owner_records"] == 734


def test_cibc_site_replacement_remains_blocked_pending_exact_evidence() -> None:
    production = by_name(PRODUCTION)
    candidates = by_name(CANDIDATES)
    audit = load(REVIEW)["migration_audits"]["CIBC"]

    assert production["CIBC"]["site_id"] == "campus"
    assert candidates["CIBC"]["site_id"] == "search"
    assert production["CIBC"].get("source_scope", "default") == "default"
    assert candidates["CIBC"]["source_scope"] == "default"
    assert audit["classification"] == "BLOCKED_PENDING_EVIDENCE"
    assert audit["historical_default_owner_records"] == 44
    assert len(audit["required_evidence"]) >= 4


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


def test_package_and_source_integrity_validators_are_offline() -> None:
    package_result = package.validate_package()
    integrity = validate_source_integrity(
        registry_path=PROPOSAL,
        retained_pattern="jobs/no-workday-batch1-activation-retained-coverage-*.json",
    )

    assert package_result["status"] == "PASS"
    assert package_result["network_requests"] == 0
    assert package_result["production_writes"] == 0
    assert integrity["network_requests"] == 0
    assert integrity["summary"]["sources"] == len(package.APPROVED_COMPANIES)
    assert integrity["summary"]["ARCHITECTURAL"] == 0


def test_report_uses_only_operator_supplied_live_evidence() -> None:
    review = load(REVIEW)
    evidence = review["operator_supplied_live_evidence"]
    summary = review["activation_summary"]

    assert evidence["fresh_validation_performed_by_this_task"] is False
    assert evidence["common_result"]["canary_outcome"] == "PASS_COMPLETE"
    assert evidence["aggregate_jobs"] == 13632
    assert len(evidence["companies"]) == 16
    assert summary == {
        "ready_for_controlled_activation": 13,
        "needs_scope_migration": 2,
        "blocked_pending_evidence": 1,
        "independently_activatable": True,
        "independence_condition": summary["independence_condition"],
    }
