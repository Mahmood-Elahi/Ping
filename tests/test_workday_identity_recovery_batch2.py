from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from scripts.validate_source_integrity import validate_source_integrity
from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = (
    ROOT / "config" / "target_companies.workday_identity_batch2_candidates.yaml"
)
MIGRATIONS = (
    ROOT
    / "config"
    / "target_companies.workday_identity_batch2_migration_candidates.yaml"
)
ROLLBACK = (
    ROOT / "config" / "target_companies.workday_identity_batch2_rollback.yaml"
)
ACTIVATION_ROLLBACK = (
    ROOT
    / "config"
    / "target_companies.workday_identity_batch2_activation_rollback.yaml"
)
REVIEW = ROOT / "config" / "workday_identity_recovery_batch2.yaml"

BATCH = [
    "Broadcom",
    "Microchip Technology",
    "Lattice Semiconductor",
    "Capital One",
    "Magna International",
    "Valeo",
    "Unity",
    "Sony",
    "PTC",
    "TC Energy",
    "Enbridge",
    "Shell",
    "Suncor Energy",
    "Cenovus Energy",
    "ConocoPhillips",
    "CAE Inc.",
    "Silicon Labs",
    "Ciena",
    "Logitech",
    "Symbotic",
]
READY = {
    "Microchip Technology",
    "Lattice Semiconductor",
    "Capital One",
    "Magna International",
    "Valeo",
    "Unity",
    "PTC",
    "TC Energy",
    "Suncor Energy",
    "Cenovus Energy",
    "ConocoPhillips",
    "CAE Inc.",
    "Silicon Labs",
    "Ciena",
    "Logitech",
    "Symbotic",
}
ACTIVATED = READY - {"Valeo"}
MIGRATION = {"Sony", "Shell"}
BLOCKED = {"Broadcom", "Enbridge"}
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


def test_classifications_are_explicit_disjoint_and_complete() -> None:
    report = load(REVIEW)["report"]
    assert set(report["ready_for_canary"]) == READY
    assert set(report["needs_scope_migration"]) == MIGRATION
    assert set(report["blocked_pending_evidence"]) == BLOCKED
    assert report["invalid_source"] == []
    assert report["status_counts"] == {
        "READY_FOR_CANARY": 16,
        "NEEDS_SCOPE_MIGRATION": 2,
        "BLOCKED_PENDING_EVIDENCE": 2,
        "INVALID_SOURCE": 0,
    }


def test_candidate_registries_match_the_classifications() -> None:
    assert {row["name"] for row in rows(CANDIDATES)} == READY
    assert {row["name"] for row in rows(MIGRATIONS)} == MIGRATION
    assert READY.isdisjoint(MIGRATION | BLOCKED)


def test_rollback_rows_preserve_the_exact_reviewed_baseline_definitions() -> None:
    from scripts.validate_workday_recovery_activation import registry_before_recovery
    production = {r["name"]: r for r in registry_before_recovery()["target_companies"]}
    rollback = by_name(ROLLBACK)
    activation_rollback = by_name(ACTIVATION_ROLLBACK)
    assert list(rollback) == BATCH
    assert activation_rollback == {name: rollback[name] for name in activation_rollback}
    assert set(activation_rollback) == ACTIVATED
    excluded = {"Valeo"} | MIGRATION | BLOCKED
    assert all(rollback[name] == production[name] for name in excluded)


def test_straightforward_candidates_preserve_default_scope_tenant_and_site() -> None:
    production = by_name(PRODUCTION)
    for name, row in by_name(CANDIDATES).items():
        assert row["source_scope"] == "default"
        assert row["tenant"] == production[name]["tenant"]
        assert row["wd_subdomain"] == production[name]["wd_subdomain"]
        assert row["site_id"] == production[name]["site_id"]


def test_migration_candidates_are_isolated_and_exact() -> None:
    production = by_name(PRODUCTION)
    migrations = by_name(MIGRATIONS)

    assert migrations["Sony"]["source_scope"] == "europe"
    assert migrations["Sony"]["site_id"] == "Sony_Europe_Careers"
    assert production["Sony"].get("source_scope", "default") == "default"
    assert production["Sony"]["site_id"] == "SonyGlobalCareers"

    assert migrations["Shell"]["source_scope"] == "default"
    assert migrations["Shell"]["site_id"] == "ShellCareers"
    assert production["Shell"]["site_id"] == "shellcareers"


def test_every_candidate_has_complete_exact_identity_metadata() -> None:
    for path in (CANDIDATES, MIGRATIONS):
        for row in rows(path):
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
    for path in (CANDIDATES, MIGRATIONS):
        for row in rows(path):
            target = CompanyTarget.from_dict(row)
            hostname = f"{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
            status, diagnostic = workday._configured_identity_evidence(
                target, f"{target.site_id}_wrong", hostname
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


def test_blocked_source_has_no_candidate_and_names_missing_authority() -> None:
    review = {row["company"]: row for row in load(REVIEW)["companies"]}
    assert review["Broadcom"]["classification"] == "BLOCKED_PENDING_EVIDENCE"
    assert review["Broadcom"]["proposed_candidate"] is None
    assert len(review["Broadcom"]["evidence_gaps"]) >= 2


def test_review_does_not_claim_unavailable_lifecycle_identifiers() -> None:
    review = load(REVIEW)
    audit = review["lifecycle_identity_audit"]
    assert audit["retained_active_or_historical_job_ids_in_checkout"] == 0
    assert audit["supabase_or_other_production_store_queried"] is False
    for company in review["companies"]:
        identifiers = company["active_and_historical_job_identifiers"]
        assert identifiers["repository_status"] == "NOT_AVAILABLE_IN_CHECKOUT"
        assert identifiers["production_store_queried"] is False


def test_candidate_registries_pass_strict_offline_source_integrity() -> None:
    for path, expected in ((CANDIDATES, 16), (MIGRATIONS, 2)):
        result = validate_source_integrity(
            registry_path=path,
            retained_pattern="jobs/no-workday-identity-batch2-retained-coverage-*.json",
        )
        assert result["network_requests"] == 0
        assert result["summary"]["sources"] == expected
        assert result["summary"]["ARCHITECTURAL"] == 0


def test_recovery_does_not_relax_workday_or_lifecycle_contracts() -> None:
    source = (
        ROOT / "src" / "scraper" / "company_careers" / "adapters" / "workday.py"
    ).read_text(encoding="utf-8")
    assert "workday_provider_cap_reached" in source
    assert "workday_unsplittable_partition" in source
    assert "collection_restart_count=1" in source
    assert "collection_retry_exhausted" in source

    contracts = (ROOT / "docs" / "SOURCE_CONTRACTS.md").read_text(encoding="utf-8")
    assert "two consecutive qualifying absences" in contracts
    assert "PASS_COMPLETE" in contracts


def test_no_batch1_company_appears_in_batch2_candidate_files() -> None:
    batch1 = {
        row["name"]
        for row in rows(
            ROOT
            / "config"
            / "target_companies.workday_identity_batch1_activation_proposal.yaml"
        )
    }
    batch2 = {row["name"] for row in rows(CANDIDATES)} | {
        row["name"] for row in rows(MIGRATIONS)
    }
    assert batch1.isdisjoint(batch2)
