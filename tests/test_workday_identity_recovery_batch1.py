from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from scripts.validate_source_integrity import validate_source_integrity
from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.workday_identity_batch1_candidates.yaml"
ROLLBACK = ROOT / "config" / "target_companies.workday_identity_batch1_rollback.yaml"
REVIEW = ROOT / "config" / "workday_identity_recovery_batch1.yaml"

BATCH = [
    "Adobe",
    "CrowdStrike",
    "PayPal",
    "Salesforce",
    "Boston Dynamics",
    "Cadence Design Systems",
    "Blue Origin",
    "Medtronic",
    "Samsung Electronics",
    "Visa",
    "Rockwell Automation",
    "NXP Semiconductors",
    "Intel",
    "KLA",
    "Autodesk",
    "Marvell Technology",
    "RBC",
    "TD Bank",
    "CIBC",
    "Analog Devices",
]
READY = {
    "Adobe",
    "CrowdStrike",
    "Salesforce",
    "Boston Dynamics",
    "Cadence Design Systems",
    "Blue Origin",
    "Medtronic",
    "Visa",
    "NXP Semiconductors",
    "Intel",
    "KLA",
    "Marvell Technology",
    "RBC",
    "TD Bank",
    "CIBC",
    "Analog Devices",
}
BLOCKED = {"PayPal", "Samsung Electronics", "Rockwell Automation", "Autodesk"}
ACTIVATED = {
    "CrowdStrike",
    "Salesforce",
    "Boston Dynamics",
    "Cadence Design Systems",
    "Blue Origin",
    "Medtronic",
    "Visa",
    "NXP Semiconductors",
    "Intel",
    "KLA",
    "Marvell Technology",
    "TD Bank",
    "Analog Devices",
}
REQUIRED_EVIDENCE = {
    "evidence_source",
    "official_source_url",
    "official_binding_target_url",
    "official_binding_type",
    "expected_employer_name",
    "expected_source_scope",
    "expected_workday_tenant",
    "expected_workday_site",
    "expected_workday_hostname",
    "expected_apply_hostname",
}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def rows(path: Path) -> list[dict]:
    return load(path)["target_companies"]


def by_name(path: Path) -> dict[str, dict]:
    return {row["name"]: row for row in rows(path)}


def test_review_covers_exactly_the_requested_batch_in_order() -> None:
    companies = load(REVIEW)["companies"]
    assert [row["company"] for row in companies] == BATCH
    assert {row["company"] for row in companies} == set(BATCH)


def test_candidate_and_blocked_sets_are_explicit_and_disjoint() -> None:
    candidate_names = {row["name"] for row in rows(CANDIDATES)}
    assert candidate_names == READY
    assert candidate_names.isdisjoint(BLOCKED)
    report = load(REVIEW)["report"]
    assert set(report["ready_for_canary"]) == READY
    assert set(report["blocked"]) == BLOCKED
    assert report["status_counts"] == {
        "READY_FOR_CANARY": 16,
        "INSUFFICIENT_EVIDENCE": 1,
        "FAIL_IDENTITY": 3,
        "RECOVERED": 0,
    }


def test_production_registry_contains_only_the_approved_batch_activation() -> None:
    production = by_name(PRODUCTION)
    candidates = by_name(CANDIDATES)
    rollback = by_name(ROLLBACK)
    assert all(production[name] == candidates[name] for name in ACTIVATED)
    assert all(
        production[name] == rollback[name]
        for name in set(BATCH) - ACTIVATED
    )
    assert load(REVIEW)["goal"]["production_registry_modified"] is False
    assert load(REVIEW)["goal"]["canary_performed"] is False
    assert load(REVIEW)["goal"]["recovered_sources"] == []


def test_rollback_rows_are_exact_preactivation_definitions() -> None:
    production = by_name(PRODUCTION)
    rollback = by_name(ROLLBACK)
    assert list(rollback) == BATCH
    assert all(production[name] != rollback[name] for name in ACTIVATED)
    assert all(
        production[name] == rollback[name]
        for name in set(BATCH) - ACTIVATED
    )


def test_every_candidate_has_complete_exact_identity_metadata() -> None:
    for row in rows(CANDIDATES):
        evidence = row["workday_identity"]
        assert set(evidence) == REQUIRED_EVIDENCE, row["name"]
        target = CompanyTarget.from_dict(row)
        hostname = f"{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
        status, diagnostic = workday._configured_identity_evidence(
            target, str(target.site_id), hostname
        )
        assert status == "validated", (row["name"], diagnostic)
        assert diagnostic["expected_employer_name"] == row["name"]
        assert diagnostic["expected_source_scope"] == target.source_scope
        assert diagnostic["expected_apply_hostname"] in row["allowed_apply_hosts"]


def test_every_candidate_fails_closed_on_identity_mismatch() -> None:
    for row in rows(CANDIDATES):
        target = CompanyTarget.from_dict(row)
        hostname = f"{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
        wrong_site = f"{target.site_id}_wrong"
        status, diagnostic = workday._configured_identity_evidence(
            target, wrong_site, hostname
        )
        assert status == "failed", row["name"]
        assert diagnostic["exact_binding"] is False

        changed = deepcopy(row)
        changed["workday_identity"]["expected_source_scope"] = "wrong-scope"
        changed_target = CompanyTarget.from_dict(changed)
        status, diagnostic = workday._configured_identity_evidence(
            changed_target, str(changed_target.site_id), hostname
        )
        assert status == "failed", row["name"]
        assert diagnostic["exact_binding"] is False


def test_scope_corrections_are_explicit_and_do_not_claim_other_scopes() -> None:
    candidates = by_name(CANDIDATES)
    assert candidates["Adobe"]["source_scope"] == "experienced"
    assert candidates["Adobe"]["site_id"] == "external_experienced"
    assert candidates["RBC"]["source_scope"] == "global"
    assert candidates["CIBC"]["site_id"] == "search"
    assert by_name(ROLLBACK)["CIBC"]["site_id"] == "campus"
    samsung = next(
        row for row in load(REVIEW)["companies"]
        if row["company"] == "Samsung Electronics"
    )
    assert samsung["final_status"] == "FAIL_IDENTITY"
    assert "SRI-B" in samsung["evidence"]["scope"]


def test_blocked_sources_have_no_candidate_and_name_the_missing_authority() -> None:
    review = {row["company"]: row for row in load(REVIEW)["companies"]}
    for name in BLOCKED:
        assert review[name]["proposed_candidate"] is None
        assert review[name]["evidence_gaps"]
        assert review[name]["final_status"] in {
            "FAIL_IDENTITY",
            "INSUFFICIENT_EVIDENCE",
        }


def test_candidate_registry_passes_strict_offline_source_integrity() -> None:
    result = validate_source_integrity(
        registry_path=CANDIDATES,
        retained_pattern="jobs/no-workday-identity-batch1-retained-coverage-*.json",
    )
    assert result["network_requests"] == 0
    assert result["summary"]["sources"] == len(READY)
    assert result["summary"]["ARCHITECTURAL"] == 0


def test_recovery_does_not_relax_workday_completion_or_absence_rules() -> None:
    source = (ROOT / "src" / "scraper" / "company_careers" / "adapters" / "workday.py").read_text(encoding="utf-8")
    assert "workday_provider_cap_reached" in source
    assert "workday_unsplittable_partition" in source
    assert "collection_restart_count=1" in source
    assert "collection_retry_exhausted" in source
    contracts = (ROOT / "docs" / "SOURCE_CONTRACTS.md").read_text(encoding="utf-8")
    assert "two consecutive qualifying absences" in contracts
    assert "PASS_COMPLETE" in contracts
