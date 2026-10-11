from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from src.integrations.supabase_job_store import source_key_for_external_id
from src.scraper.company_careers.phase5_coverage import (
    PHASE5_STATUSES,
    build_phase5_batched_coverage_report,
    load_phase5_canary_plans,
    load_phase5_candidates,
)
from src.scraper.company_careers.registry import DEFAULT_REGISTRY_PATH, load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
BATCH1 = ROOT / "config" / "target_companies.phase5_batch1_candidates.yaml"
BATCH1_CANARIES = ROOT / "config" / "target_companies.phase5_batch1_canaries.yaml"
BATCH2 = ROOT / "config" / "target_companies.phase5_batch2_candidates.yaml"
BATCH2_CANARIES = ROOT / "config" / "target_companies.phase5_batch2_canaries.yaml"
PRODUCTION_SHA256 = "4ce96583c98f6520d77bf3c0c0c4642a969594ad4b341e41fb0c39b3f55166ef"
OVERLAPS = {"DISJOINT", "PARTIAL_OVERLAP", "SUBSET", "UNKNOWN"}


def _data(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_batch1_registry_preserves_final_rejection_and_remains_valid() -> None:
    candidates = load_phase5_candidates(BATCH1)
    assert len(candidates) == 20
    assert [item["rank"] for item in candidates] == list(range(1, 21))
    assert [
        item["candidate_id"]
        for item in candidates
        if item["status"] == "READY_FOR_CANARY"
    ] == []
    lightspeed = next(
        item for item in candidates
        if item["candidate_id"] == "lightspeed_fr_greenhouse"
    )
    assert lightspeed["status"] == "REJECTED"
    assert lightspeed["canary_outcome"] == "FAIL_IDENTITY"


def test_batch2_has_approximately_twenty_ranked_progressions() -> None:
    candidates = load_phase5_candidates(BATCH2)
    assert len(candidates) == 21
    assert [item["rank"] for item in candidates] == list(range(1, 22))
    assert len({item["candidate_id"] for item in candidates}) == len(candidates)
    assert {item["status"] for item in candidates} <= set(PHASE5_STATUSES)
    assert all(item["previous_status"] for item in candidates)
    assert all(item["canonical_company"] for item in candidates)
    assert all(item["source_scope"] for item in candidates)
    assert all(item["what_changed_since_batch1"] for item in candidates)
    assert all(item["next_action"] for item in candidates)


def test_final_lightspeed_disposition_has_identity_failure_evidence() -> None:
    ready = [
        item
        for item in load_phase5_candidates(BATCH2)
        if item["status"] == "READY_FOR_CANARY"
    ]
    assert ready == []
    item = next(
        candidate for candidate in load_phase5_candidates(BATCH2)
        if candidate["candidate_id"] == "lightspeed_fr_greenhouse"
    )
    assert item["status"] == "REJECTED"
    assert item["canary_outcome"] == "FAIL_IDENTITY"
    assert item["activation_allowed"] is False
    assert item["canary_rerun_allowed"] is False
    assert item["proposed_platform"] in ADAPTERS


def test_only_rejected_historical_candidate_remains_in_canary_registry() -> None:
    candidates = load_phase5_candidates(BATCH2)
    canary_ids = {
        item["candidate_id"] for item in load_phase5_canary_plans(BATCH2_CANARIES)
    }
    blocked = {
        item["candidate_id"]
        for item in candidates
        if item["status"] != "READY_FOR_CANARY"
    }
    assert canary_ids == {"lightspeed_fr_greenhouse"}
    assert canary_ids & blocked == {"lightspeed_fr_greenhouse"}
    plans = load_phase5_canary_plans(BATCH2_CANARIES)
    assert all(plan["activation_allowed"] is False for plan in plans)
    assert all(plan["canary_rerun_allowed"] is False for plan in plans)
    assert all(
        item["proposed_platform"] in ADAPTERS
        for item in candidates
        if item["status"] == "PROVIDER_IMPLEMENTED"
    )


def test_production_registry_is_unchanged_and_batch2_is_opt_in() -> None:
    assert hashlib.sha256(PRODUCTION.read_bytes()).hexdigest() == PRODUCTION_SHA256
    assert DEFAULT_REGISTRY_PATH == "config/target_companies.yaml"
    assert BATCH2_CANARIES.name != Path(DEFAULT_REGISTRY_PATH).name
    production_names = {item.name for item in load_target_companies(str(PRODUCTION))}
    canary_names = {
        item.name for item in load_target_companies(str(BATCH2_CANARIES))
    }
    assert canary_names == {"Lightspeed Commerce"}
    target = load_target_companies(str(BATCH2_CANARIES))[0]
    assert target.source_scope == "france"
    assert target.source_scope_key == "lightspeed commerce::france"


def test_canary_budget_is_exact_and_never_activates_production() -> None:
    plans = load_phase5_canary_plans(BATCH2_CANARIES)
    assert len(plans) == 1
    plan = plans[0]
    assert plan["carried_forward_from_batch1"] is True
    assert plan["activation_allowed"] is False
    assert plan["architecture_activation_blocked"] is False
    assert plan["status"] == "REJECTED"
    assert plan["canary_outcome"] == "FAIL_IDENTITY"
    assert plan["canary_rerun_allowed"] is False
    assert plan["canonical_company"] == "Lightspeed Commerce"
    assert plan["source_scope"] == "france"
    assert plan["hard_request_budget"] == sum(
        int(plan[field])
        for field in (
            "identity_bootstrap_requests",
            "listing_pages",
            "detail_hydration_requests",
            "partition_requests",
            "retry_allowance",
        )
    ) == 21


def test_overlap_relationships_are_explicit_and_no_duplicate_can_activate() -> None:
    candidates = load_phase5_candidates(BATCH2)
    assert {item["overlap_classification"] for item in candidates} <= OVERLAPS
    assert all(item["overlap_classification"] for item in candidates)
    assert all(
        item["status"] != "READY_FOR_CANARY"
        for item in candidates
        if item["status"] == "DUPLICATE_SCOPE"
    )
    by_id = {item["candidate_id"]: item for item in candidates}
    assert by_id["ultium_spring_hill_adp"]["overlap_classification"] == "PARTIAL_OVERLAP"
    assert by_id["stantec_global_sitemap"]["overlap_classification"] == "SUBSET"


def test_multi_scope_model_is_implemented_and_no_longer_an_activation_blocker() -> None:
    data = _data(BATCH2)
    model = data["multi_scope_model"]
    assert data["architecture_conclusion"] == "MULTI_SCOPE_ARCHITECTURE_IMPLEMENTED"
    assert model["required"] is True
    assert model["implemented"] is True
    assert model["activation_blocker"] is False
    assert data["phase6_started"] is False
    assert "source scope" in " ".join(model["minimum_future_model"]).casefold()

    # Provider namespaces deliberately produce distinct source rows. Exact
    # cross-source equivalence and scope-aware reconciliation preserve them.
    assert source_key_for_external_id("workday:one", "R-1") != source_key_for_external_id(
        "talnet:one", "R-1"
    )


def test_provider_decisions_are_strict_and_none_was_implemented() -> None:
    decisions = _data(BATCH2)["provider_decisions"]
    assert {
        "salesforce_frecruit",
        "brassring",
        "tal_net",
        "stantec_sitemap_model",
        "coupang_cfs_protocol",
        "tupu360",
    } <= set(decisions)
    assert {item["decision"] for item in decisions.values()} == {"DEFER"}
    assert all(item["reason"] for item in decisions.values())
    assert all(
        item["status"] != "PROVIDER_IMPLEMENTED"
        for item in load_phase5_candidates(BATCH2)
    )


def test_batched_coverage_report_preserves_baseline_and_tracks_progression() -> None:
    report = build_phase5_batched_coverage_report(
        PRODUCTION, BATCH1, BATCH1_CANARIES, BATCH2, BATCH2_CANARIES
    )
    assert report["production"]["canonical_companies"] == 533
    assert report["production"]["configured_sources"] == 533
    assert report["production"]["source_scopes"] == 533
    assert report["production"]["active_sources"] == 533
    assert report["production"]["companies_with_multiple_recruiting_scopes"] == 0
    assert report["batch1"]["candidate_count"] == 20
    assert report["batch1"]["candidate_source_scopes"] == 20
    assert report["batch1"]["canary_count"] == 1
    assert report["batch2"]["candidate_count"] == 21
    assert report["batch2"]["candidate_source_scopes"] == 21
    assert report["batch2"]["canary_count"] == 1
    assert report["batch2"]["promoted_to_ready"] == []
    assert report["batch2"]["provider_decision_counts"] == {"DEFER": 6}
    assert report["batch2"]["providers_evaluated"] == 6
    assert report["batch2"]["providers_deferred"] == 6
    assert report["batch2"]["provider_implementations"] == 0
    assert report["batch2"]["still_blocked"] == 19
    assert report["batch2"]["rejected"] == 2
    assert report["batch2"]["multi_scope_architecture_blockers"] == 0
    assert report["batch2"]["status_counts"] == {
        "READY_FOR_CANARY": 0,
        "NEEDS_IDENTITY_EVIDENCE": 8,
        "NEEDS_SCOPE_EVIDENCE": 3,
        "DUPLICATE_SCOPE": 0,
        "UNSUPPORTED_PROTOCOL": 0,
        "DEFER": 8,
        "REJECTED": 2,
    }
    assert report["batch2"]["architecture_conclusion"] == (
        "MULTI_SCOPE_ARCHITECTURE_IMPLEMENTED"
    )
    assert report["batch2"]["phase6_started"] is False
