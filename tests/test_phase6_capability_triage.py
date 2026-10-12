from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from src.scraper.company_careers.adapters import greenhouse, smartrecruiters
from src.scraper.company_careers.incremental import (
    CrawlMode,
    IncrementalCapability,
    IncrementalCheckpoint,
    choose_crawl_mode,
)
from src.scraper.company_careers.phase6_triage import (
    ALIASES,
    CAPABILITIES,
    OptimizationPattern,
    Phase6Classification,
    canonical_family,
    capability_for,
    incremental_policy_for,
    request_cost_summary,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
EXPECTED_REGISTRY_SHA256 = (
    "020e0b6f4df9dfe65f391551f516beeaafa18c45fe561ae6a01744a234352408"
)
EXPECTED_PRODUCTION_COUNTS = {
    "adp": 8,
    "amazon": 1,
    "apple": 1,
    "ashby": 72,
    "atlassian": 1,
    "avature": 7,
    "bamboohr": 7,
    "beesite": 2,
    "browser_rendered_html": 4,
    "clearcompany": 1,
    "cornerstone_csod": 1,
    "dayforce": 5,
    "deel": 1,
    "eightfold": 12,
    "exalead": 1,
    "first_party_json": 1,
    "google": 1,
    "greenhouse": 102,
    "hibob": 3,
    "ibm_search": 2,
    "icims": 12,
    "javascript_embedded_data": 10,
    "jobvite": 2,
    "lever": 25,
    "mcloud_cws": 1,
    "meta": 1,
    "oracle_recruiting": 16,
    "paycor": 2,
    "peoplesoft": 1,
    "phenom": 16,
    "public_json": 7,
    "sap_successfactors": 44,
    "shopify": 1,
    "smartrecruiters": 16,
    "startdate": 1,
    "static_html": 26,
    "talentbrew": 4,
    "teamtailor": 1,
    "uber": 1,
    "ukg_pro": 5,
    "workday": 108,
}


@pytest.fixture(scope="module")
def production_targets():
    return load_target_companies(apply_reliability_repairs=True)


def test_every_production_adapter_family_has_exactly_one_classification(
    production_targets,
):
    counts = Counter(target.platform for target in production_targets)

    assert len(production_targets) == 533
    assert len(counts) == 41
    assert counts == EXPECTED_PRODUCTION_COUNTS
    assert set(counts) <= set(CAPABILITIES)
    assert all(
        isinstance(CAPABILITIES[family].classification, Phase6Classification)
        for family in counts
    )


def test_every_scanner_dispatch_family_is_triaged_and_dormant_workable_is_excluded():
    dispatch_families = {canonical_family(name) for name in ADAPTERS}

    assert dispatch_families == set(CAPABILITIES)
    assert "source_xml" in dispatch_families
    assert "workable" not in dispatch_families


def test_smartrecruiters_and_greenhouse_remain_full_crawl_only():
    for family, adapter_policy in (
        ("smartrecruiters", smartrecruiters.INCREMENTAL_POLICY),
        ("greenhouse", greenhouse.INCREMENTAL_POLICY),
    ):
        capability = capability_for(family)
        assert capability.classification is Phase6Classification.FULL_CRAWL_ONLY
        assert capability.patterns == (OptimizationPattern.NONE,)
        assert adapter_policy.capability is IncrementalCapability.NONE


def test_no_production_source_has_incremental_enabled(production_targets):
    assert all(
        incremental_policy_for(target.platform).capability
        is IncrementalCapability.NONE
        for target in production_targets
    )


def test_high_value_candidates_must_name_a_non_none_safe_pattern():
    high_value = [
        capability
        for capability in CAPABILITIES.values()
        if capability.classification
        is Phase6Classification.HIGH_VALUE_PHASE6_CANDIDATE
    ]

    assert all(capability.patterns != (OptimizationPattern.NONE,) for capability in high_value)
    # Stage B3 found no adapter that clears both the correctness and materiality gates.
    assert high_value == []


@pytest.mark.parametrize(
    "family",
    sorted(
        family
        for family, capability in CAPABILITIES.items()
        if capability.classification is Phase6Classification.FULL_CRAWL_ONLY
    ),
)
def test_full_crawl_only_family_cannot_initialize_incremental_checkpoint(family):
    policy = incremental_policy_for(family)
    checkpoint = IncrementalCheckpoint(
        source_scope_key="example::default",
        provider_identity=f"{family}:example",
        config_fingerprint="a" * 64,
        strategy_version=policy.strategy_version,
        timestamp_watermark=NOW,
        last_successful_full_at=NOW,
        updated_at=NOW,
    )

    decision = choose_crawl_mode(
        source_scope_key_value="example::default",
        provider_identity=f"{family}:example",
        config_fingerprint="a" * 64,
        policy=policy,
        checkpoint=checkpoint,
        now=NOW,
    )

    assert decision.mode is CrawlMode.FULL_AUTHORITATIVE
    assert decision.reason == "capability_none"
    assert not decision.checkpoint_valid


@pytest.mark.parametrize(
    ("alias", "family"),
    [
        ("custom:amazon", "amazon"),
        ("custom:google", "google"),
        ("embedded_data", "javascript_embedded_data"),
        ("cornerstone", "cornerstone_csod"),
        ("csod", "cornerstone_csod"),
        ("successfactors", "sap_successfactors"),
        ("browser_html", "browser_rendered_html"),
    ],
)
def test_adapter_aliases_map_consistently(alias, family):
    assert ALIASES[alias] == family
    assert canonical_family(alias) == family
    assert capability_for(alias) is CAPABILITIES[family]


def test_unknown_alias_fails_closed():
    with pytest.raises(KeyError, match="Unknown Phase 6 adapter family"):
        canonical_family("not-a-real-adapter")


def test_production_registry_file_and_registry_shape_remain_unchanged():
    registry = ROOT / "config" / "target_companies.yaml"
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == EXPECTED_REGISTRY_SHA256


def test_request_cost_calculations_are_bounded_and_deterministic(production_targets):
    by_family = {
        family: request_cost_summary(
            target for target in production_targets if target.platform == family
        )
        for family in EXPECTED_PRODUCTION_COUNTS
    }

    workday = by_family["workday"]
    assert workday.source_count == 108
    assert workday.listing_page_ceiling_total == 2_700
    assert workday.listing_page_ceiling_median == 25.0
    assert workday.listing_page_ceiling_max == 25
    assert workday.job_ceiling_total == 54_000
    assert workday.job_ceiling_max == 500
    assert workday.endpoint_variants == (("<default>", 91), ("wday_cxs_v1", 17))

    static = by_family["static_html"]
    assert static.listing_page_ceiling_total == 29
    assert static.listing_page_ceiling_max == 2
    assert static.endpoint_variants == (
        ("server_rendered_cards", 23),
        ("server_rendered_links", 3),
    )

    assert request_cost_summary(()).source_count == 0
    assert all(
        summary.listing_page_ceiling_total
        <= summary.source_count * summary.listing_page_ceiling_max
        for summary in by_family.values()
    )


def test_priority_scores_are_bounded_and_do_not_claim_measurements():
    for capability in CAPABILITIES.values():
        score = capability.score
        assert 0 <= score.priority <= 20
        assert "ms" not in capability.expected_savings.casefold()
        assert "%" not in capability.expected_savings


def test_phase6_runbook_records_triage_decision_and_next_action():
    runbook = (ROOT / "docs" / "PHASE6_INCREMENTAL_OPTIMIZATION.md").read_text(
        encoding="utf-8"
    )

    assert "## Stage B3: cross-adapter capability triage" in runbook
    assert "41 production adapter families" in runbook
    assert "533 production sources" in runbook
    assert "NEXT_DEEP_REVIEW = NONE" in runbook
    assert "PHASE6_CLOSE_EARLY" in runbook
    assert "jobseek/apps/crawler/src/core/monitors/eightfold.py" in runbook

