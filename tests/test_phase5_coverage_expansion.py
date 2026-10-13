from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from src.scraper.company_careers.phase5_coverage import (
    build_phase5_coverage_report,
    load_phase5_candidates,
    load_phase5_canary_plans,
)
from src.scraper.company_careers.registry import DEFAULT_REGISTRY_PATH, load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.phase5_batch1_candidates.yaml"
CANARIES = ROOT / "config" / "target_companies.phase5_batch1_canaries.yaml"
PRODUCTION_SHA256 = "8fecb534be41b22325531fbea93a7825673f1f4533bd5c159ac1cca1f299e7f2"
STATUSES = {
    "READY_FOR_CANARY",
    "NEEDS_IDENTITY_EVIDENCE",
    "NEEDS_SCOPE_EVIDENCE",
    "DUPLICATE_SCOPE",
    "UNSUPPORTED_PROTOCOL",
    "REJECTED",
}


def _production_entries() -> list[dict[str, object]]:
    return yaml.safe_load(PRODUCTION.read_text(encoding="utf-8"))["target_companies"]


def test_phase5_candidate_registry_is_not_loaded_by_normal_runtime() -> None:
    assert DEFAULT_REGISTRY_PATH == "config/target_companies.yaml"
    assert Path(DEFAULT_REGISTRY_PATH).name != CANDIDATES.name
    assert "target_companies" not in yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))
    assert len(load_target_companies()) == 533


def test_phase5_has_twenty_independently_ranked_candidates() -> None:
    candidates = load_phase5_candidates(CANDIDATES)
    assert len(candidates) == 20
    assert [item["rank"] for item in candidates] == list(range(1, 21))
    assert len({item["candidate_id"] for item in candidates}) == 20
    assert {item["status"] for item in candidates} <= STATUSES


def test_candidate_company_names_are_canonical_or_explicit_children() -> None:
    production_names = {entry["name"] for entry in _production_entries()}
    for candidate in load_phase5_candidates(CANDIDATES):
        assert str(candidate["company"]).strip()
        assert str(candidate["parent_company"]).strip() in production_names
        if candidate["company"] not in production_names:
            assert candidate["company"] != candidate["parent_company"]


def test_proposed_providers_exist_or_are_explicitly_unsupported() -> None:
    for candidate in load_phase5_candidates(CANDIDATES):
        provider = candidate["proposed_platform"]
        if provider == "unsupported":
            assert candidate.get("unsupported_provider")
            assert candidate["status"] in {"UNSUPPORTED_PROTOCOL", "NEEDS_SCOPE_EVIDENCE"}
        else:
            assert provider in ADAPTERS


def test_final_candidate_disposition_is_fail_closed() -> None:
    ready = [
        item for item in load_phase5_candidates(CANDIDATES)
        if item["status"] == "READY_FOR_CANARY"
    ]
    assert ready == []
    candidate = next(
        item for item in load_phase5_candidates(CANDIDATES)
        if item["candidate_id"] == "lightspeed_fr_greenhouse"
    )
    assert candidate["pre_canary_status"] == "READY_FOR_CANARY"
    assert candidate["status"] == "REJECTED"
    assert candidate["canary_outcome"] == "FAIL_IDENTITY"
    assert candidate["activation_allowed"] is False
    assert candidate["canary_rerun_allowed"] is False
    assert "HTTP 404" in candidate["canary_evidence"]


def test_no_candidate_silently_overwrites_production() -> None:
    digest = hashlib.sha256(PRODUCTION.read_bytes()).hexdigest()
    assert digest == PRODUCTION_SHA256
    production_names = {entry["name"] for entry in _production_entries()}
    canary_names = {target.name for target in load_target_companies(str(CANARIES))}
    assert canary_names == {"Lightspeed Commerce"}
    assert canary_names <= production_names


def test_canary_registry_is_opt_in_and_adapter_valid() -> None:
    assert Path(DEFAULT_REGISTRY_PATH).name != CANARIES.name
    targets = load_target_companies(str(CANARIES))
    assert [(target.name, target.platform, target.slug) for target in targets] == [
        ("Lightspeed Commerce", "greenhouse", "lightspeedhqfr")
    ]
    assert targets[0].source_scope == "france"
    assert all(target.platform in ADAPTERS for target in targets)
    assert all(target.source_activation == "active" for target in targets)


def test_canary_budgets_are_individual_and_bounded() -> None:
    plans = load_phase5_canary_plans(CANARIES)
    assert len(plans) == 1
    for plan in plans:
        calculated = sum(
            int(plan[field])
            for field in (
                "identity_bootstrap_requests",
                "listing_pages",
                "detail_hydration_requests",
                "partition_requests",
                "retry_allowance",
            )
        )
        assert calculated == plan["hard_request_budget"]
        assert 1 <= calculated <= 52
        assert plan["activation_allowed"] is False
        assert plan["status"] == "REJECTED"
        assert plan["canary_outcome"] == "FAIL_IDENTITY"
        assert plan["canary_rerun_allowed"] is False


def test_parent_and_subsidiary_candidates_remain_distinguishable() -> None:
    candidates = {item["candidate_id"]: item for item in load_phase5_candidates(CANDIDATES)}
    assert candidates["progress_rail_global_salesforce"]["company"] == "Progress Rail"
    assert candidates["progress_rail_global_salesforce"]["parent_company"] == "Caterpillar"
    assert candidates["gm_financial_oracle"]["company"] == "GM Financial"
    assert candidates["gm_financial_oracle"]["parent_company"] == "General Motors"
    assert candidates["chevron_stations_workday"]["company"] == "Chevron Stations"


def test_regional_and_global_scopes_remain_distinct() -> None:
    scopes = {
        item["candidate_id"]: item["scope"] for item in load_phase5_candidates(CANDIDATES)
    }
    assert scopes["catl_global_successfactors_feed"] == "global"
    assert scopes["catl_us_first_party"] == "united_states"
    assert scopes["mitsubishi_electric_other_regions"] == "non_us_regions"
    assert scopes["stantec_global_sitemap"] == "global"


def test_professional_and_early_career_scopes_remain_distinct() -> None:
    candidates = {item["candidate_id"]: item for item in load_phase5_candidates(CANDIDATES)}
    assert candidates["blackrock_early_careers_talnet"]["scope"] == "early_careers"
    assert candidates["chevron_university_workday"]["scope"] == "university"
    assert candidates["healthineers_china_campus"]["scope"] == "china_campus"


def test_rejected_historical_canary_is_never_activation_eligible() -> None:
    candidates = load_phase5_candidates(CANDIDATES)
    canary_ids = {item["candidate_id"] for item in load_phase5_canary_plans(CANARIES)}
    blocked_ids = {
        item["candidate_id"] for item in candidates
        if item["status"] in {"DUPLICATE_SCOPE", "REJECTED", "UNSUPPORTED_PROTOCOL"}
    }
    assert canary_ids & blocked_ids == {"lightspeed_fr_greenhouse"}
    plans = load_phase5_canary_plans(CANARIES)
    assert all(plan["activation_allowed"] is False for plan in plans)
    assert all(plan["canary_rerun_allowed"] is False for plan in plans)
    assert all(
        item["status"] != "READY_FOR_CANARY"
        for item in candidates
        if item["proposed_platform"] == "unsupported"
    )


def test_phase5_configuration_only_report_is_complete() -> None:
    report = build_phase5_coverage_report(PRODUCTION, CANDIDATES, CANARIES)
    assert report["canonical_companies"] == 533
    assert report["configured_sources"] == 533
    assert report["source_scopes"] == 533
    assert report["active_sources"] == 533
    assert sum(report["active_sources_by_provider"].values()) == 533
    assert report["companies_with_multiple_recruiting_scopes"] == 0
    assert report["phase5_discovered_multi_scope_company_count"] == 10
    assert report["phase5_candidate_count"] == 20
    assert report["candidate_source_scopes"] == 20
    assert report["phase5_canary_count"] == 1
    assert report["candidate_status_counts"] == {
        "READY_FOR_CANARY": 0,
        "NEEDS_IDENTITY_EVIDENCE": 7,
        "NEEDS_SCOPE_EVIDENCE": 2,
        "DUPLICATE_SCOPE": 0,
        "UNSUPPORTED_PROTOCOL": 8,
        "REJECTED": 3,
    }


def test_pinpoint_decision_is_evidence_based_and_non_runnable() -> None:
    data = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))
    decision = data["pinpoint_decision"]
    assert decision["decision"] == "NOT_WORTH_IT"
    assert decision["reference_board_count"] == 99
    assert decision["canonical_target_matches"] == 0
    assert "postings.json" in decision["protocol_evidence"]
    assert "pinpoint" not in ADAPTERS
