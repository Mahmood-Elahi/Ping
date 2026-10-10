from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import yaml

from src.integrations.supabase_job_store import SupabaseJobStore, generate_source_key
from src.scraper.company_careers import scanner as scanner_module
from src.scraper.company_careers.freshness import (
    empty_freshness_state,
    load_freshness_config,
    reconcile_lifecycle,
)
from src.scraper.company_careers.models import AdapterError, CompanyTarget, CoverageStatus
from src.scraper.company_careers.phase5_coverage import build_registry_scope_metrics
from src.scraper.company_careers.phase5_rollout import (
    CANARY_DECISIONS,
    CanaryOutcome,
    classify_canary_evidence,
    evaluate_activation_gate,
)
from src.scraper.company_careers.registry import (
    AmbiguousCompanySourceError,
    CompanySourceRegistry,
    load_company_source_registry,
    load_target_companies,
)
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.source_cooldown import source_health_key
from src.scraper.company_careers.source_retirement import retire_source_scope
from src.utils.source_scope import source_scope_key


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PROPOSED = ROOT / "config" / "target_companies.phase5_batch4_lightspeed_proposed.yaml"
MIGRATION = ROOT / "sql" / "migrations" / "20261010_phase5_multi_scope_lifecycle.sql"
PREFLIGHT = ROOT / "sql" / "phase5_multi_scope_preflight.sql"
VERIFY = ROOT / "sql" / "phase5_multi_scope_verify.sql"
AS_OF = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
FRESHNESS = load_freshness_config(str(ROOT / "config" / "freshness.yaml"))


def _job(job_id: str, scope: str, *, exact: str | None = None) -> dict:
    return {
        "job_id": job_id,
        "title": "Software Engineer",
        "company": "Lightspeed Commerce",
        "location": "Paris" if scope == "france" else "Global",
        "url": f"https://jobs.example.test/{scope}/{job_id}",
        "source": f"fixture:{scope}",
        "source_scope": scope,
        "updated_at": "2026-10-10T00:00:00Z",
        "cross_source_equivalence": (
            {"kind": "shared_requisition_id", "value": exact} if exact else None
        ),
    }


def _reconcile(current, state, scopes, when=AS_OF):
    return reconcile_lifecycle(
        current,
        state,
        complete_companies=(),
        complete_source_scopes=scopes,
        as_of=when,
        config=FRESHNESS,
    )


def test_proposed_registry_loads_two_scopes_without_duplicate_company_failure() -> None:
    registry = load_company_source_registry(str(PROPOSED))
    sources = registry.get_sources(" lightspeed  commerce ")
    assert [item.source_scope for item in sources] == ["default", "france"]
    assert registry.get_source("Lightspeed Commerce", "default").platform == "static_html"
    france = registry.get_source("Lightspeed Commerce", "france")
    assert france.platform == "greenhouse"
    assert france.slug == "lightspeedhqfr"
    assert france.source_scope_key == "lightspeed commerce::france"
    assert all(item.source_activation == "active" for item in sources)


def test_ambiguous_company_only_lookup_never_selects_first_source() -> None:
    registry = load_company_source_registry(str(PROPOSED))
    with pytest.raises(AmbiguousCompanySourceError, match="multiple source scopes"):
        registry.get_company("Lightspeed Commerce")
    one = CompanySourceRegistry([CompanyTarget(name="Only", platform="greenhouse", slug="only")])
    assert one.get_company("Only").slug == "only"


def test_proposed_registry_preserves_exact_production_default_source() -> None:
    production = CompanySourceRegistry(load_target_companies(str(PRODUCTION)))
    proposed = load_company_source_registry(str(PROPOSED))
    current = production.get_company("Lightspeed Commerce")
    future = proposed.get_source("Lightspeed Commerce", "default")
    assert current.source_scope == future.source_scope == "default"
    for field in (
        "platform", "careers_url", "listing_url", "endpoint_variant",
        "external_id_regex", "max_pages", "max_jobs", "max_response_bytes",
    ):
        assert getattr(current, field) == getattr(future, field)


def test_scheduler_and_cooldown_are_independent_per_scope(monkeypatch) -> None:
    default = CompanyTarget(name="Lightspeed Commerce", platform="batch4_stub")
    france = CompanyTarget(
        name="Lightspeed Commerce", platform="batch4_stub", source_scope="france"
    )
    called: list[str] = []

    def failing_adapter(_session, target, _timeout):
        called.append(target.source_scope)
        raise AdapterError("deterministic sibling failure")

    monkeypatch.setitem(scanner_module.ADAPTERS, "batch4_stub", failing_adapter)
    cooldowns = {
        source_health_key(default): {
            "cooldown_until": (AS_OF + timedelta(hours=1)).isoformat()
        }
    }
    report = CompanyCareersScanner(
        targets=[default, france],
        session=requests.Session(),
        cooldown_states=cooldowns,
        as_of=AS_OF,
    ).scan()
    assert [item.target.source_scope for item in report.results] == ["default", "france"]
    assert report.results[0].status == CoverageStatus.COOLDOWN_SKIPPED
    assert report.results[1].status == CoverageStatus.FAILED_TECHNICAL
    assert called == ["france"]
    assert source_health_key(default) != source_health_key(france)


def test_default_scope_preserves_legacy_source_health_fingerprint() -> None:
    target = CompanyTarget(
        name="Legacy Health", platform="greenhouse", slug="legacy-board"
    )
    # Frozen from the Phase 3 algorithm, including its stable false-valued
    # contract fields. A default-scope migration must not strand this row.
    assert source_health_key(target) == "greenhouse:legacy health:e291edeb870be72a"


def test_current_and_proposed_coverage_metrics_remain_company_aware() -> None:
    current = build_registry_scope_metrics(PRODUCTION)
    proposed = build_registry_scope_metrics(PROPOSED)
    assert current == {
        "canonical_companies": 533,
        "configured_sources": 533,
        "source_scopes": 533,
        "companies_with_multiple_recruiting_scopes": 0,
        "multi_scope_companies": [],
    }
    assert proposed["canonical_companies"] == 1
    assert proposed["configured_sources"] == 2
    assert proposed["source_scopes"] == 2
    assert proposed["companies_with_multiple_recruiting_scopes"] == 1
    assert proposed["multi_scope_companies"] == ["lightspeed commerce"]


@pytest.mark.parametrize("outcome", list(CanaryOutcome))
def test_canary_decision_table_is_fail_closed(outcome: CanaryOutcome) -> None:
    decision = evaluate_activation_gate(
        outcome,
        migration_deployed_and_verified=True,
        production_config_diff_reviewed=True,
        rollback_prepared=True,
    )
    assert decision.activate is (outcome is CanaryOutcome.PASS_COMPLETE)
    if outcome is not CanaryOutcome.PASS_COMPLETE:
        assert decision.decision == "DO_NOT_ACTIVATE"
    assert set(CANARY_DECISIONS) == set(CanaryOutcome)


def test_activation_gate_rejects_partial_or_missing_migration_readiness() -> None:
    partial = evaluate_activation_gate(
        "PASS_PARTIAL",
        migration_deployed_and_verified=True,
        production_config_diff_reviewed=True,
        rollback_prepared=True,
    )
    missing_migration = evaluate_activation_gate(
        "PASS_COMPLETE",
        migration_deployed_and_verified=False,
        production_config_diff_reviewed=True,
        rollback_prepared=True,
    )
    assert partial.activate is False
    assert "canary_PASS_COMPLETE" in partial.unmet_gates
    assert missing_migration.unmet_gates == ("migration_deployed_and_verified",)


def test_complete_canary_evidence_requires_every_authoritative_gate() -> None:
    complete = {
        "employer_identity_valid": True,
        "board_identity_valid": True,
        "scope_correct": True,
        "inventory_complete": True,
        "terminal_evidence_valid": True,
        "central_finalizer_authoritative": True,
        "jobs_observed": 4,
    }
    assert classify_canary_evidence(complete) is CanaryOutcome.PASS_COMPLETE
    assert classify_canary_evidence({**complete, "inventory_complete": False}) is CanaryOutcome.PASS_PARTIAL
    assert classify_canary_evidence({**complete, "identity_contradiction": True}) is CanaryOutcome.FAIL_IDENTITY
    assert classify_canary_evidence({**complete, "budget_exhausted": True}) is CanaryOutcome.FAIL_CONTRACT
    assert classify_canary_evidence({**complete, "transient_failure": True}) is CanaryOutcome.FAIL_TRANSIENT


def test_lightspeed_disjoint_and_unknown_overlap_remain_independent() -> None:
    global_job = _job("G-1", "default")
    france_job = _job("F-1", "france")
    similar_global = _job("SIMILAR", "default")
    similar_france = _job("SIMILAR", "france")
    assert generate_source_key(global_job) != generate_source_key(france_job)
    assert generate_source_key(similar_global) != generate_source_key(similar_france)
    seeded = _reconcile(
        [global_job, france_job], empty_freshness_state(),
        [("Lightspeed Commerce", "default"), ("Lightspeed Commerce", "france")],
    )
    one = _reconcile([global_job], seeded.state, [("Lightspeed Commerce", "france")])
    two = _reconcile([global_job], one.state, [("Lightspeed Commerce", "france")], AS_OF + timedelta(hours=1))
    assert two.state["jobs"][generate_source_key(france_job)]["lifecycle_status"] == "removed"
    assert two.state["jobs"][generate_source_key(global_job)]["lifecycle_status"] == "active"


def test_global_disappearance_never_closes_france_only_record() -> None:
    global_job = _job("G-2", "default")
    france_job = _job("F-2", "france")
    seeded = _reconcile(
        [global_job, france_job], empty_freshness_state(),
        [("Lightspeed Commerce", "default"), ("Lightspeed Commerce", "france")],
    )
    one = _reconcile([france_job], seeded.state, [("Lightspeed Commerce", "default")])
    two = _reconcile([france_job], one.state, [("Lightspeed Commerce", "default")], AS_OF + timedelta(hours=1))
    assert two.state["jobs"][generate_source_key(global_job)]["lifecycle_status"] == "removed"
    assert two.state["jobs"][generate_source_key(france_job)]["lifecycle_status"] == "active"


def test_exact_overlap_uses_independent_owners_and_aggregate_stays_active() -> None:
    global_job = _job("GLOBAL-ID", "default", exact="LS-EXACT")
    france_job = _job("FR-ID", "france", exact="LS-EXACT")
    assert generate_source_key(global_job) == generate_source_key(france_job)
    seeded = _reconcile(
        [global_job, france_job], empty_freshness_state(),
        [("Lightspeed Commerce", "default"), ("Lightspeed Commerce", "france")],
    )
    first = _reconcile([global_job], seeded.state, [("Lightspeed Commerce", "france")])
    shared = first.state["jobs"][generate_source_key(global_job)]
    assert shared["scope_ownership"][source_scope_key("Lightspeed Commerce", "france")]["lifecycle_status"] == "possibly_missing"
    second = _reconcile([global_job], first.state, [("Lightspeed Commerce", "france")], AS_OF + timedelta(hours=1))
    shared = second.state["jobs"][generate_source_key(global_job)]
    assert shared["scope_ownership"][source_scope_key("Lightspeed Commerce", "france")]["lifecycle_status"] == "removed"
    assert shared["scope_ownership"][source_scope_key("Lightspeed Commerce", "default")]["lifecycle_status"] == "active"
    assert shared["lifecycle_status"] == "active"


def test_explicit_retirement_closes_france_only_but_preserves_shared_global_owner() -> None:
    france_only = _job("FR-ONLY", "france")
    shared_global = _job("G-SHARED", "default", exact="LS-SHARED")
    shared_france = _job("F-SHARED", "france", exact="LS-SHARED")
    seeded = _reconcile(
        [france_only, shared_global, shared_france], empty_freshness_state(),
        [("Lightspeed Commerce", "default"), ("Lightspeed Commerce", "france")],
    )
    retired = retire_source_scope(
        seeded.state,
        company="Lightspeed Commerce",
        source_scope="france",
        reason="approved source-level rollback",
        approved_by="release-owner",
        retired_at=AS_OF,
    )
    assert retired.owners_retired == 2
    assert retired.canonical_jobs_closed == 1
    assert retired.state["jobs"][generate_source_key(france_only)]["lifecycle_status"] == "removed"
    assert retired.state["jobs"][generate_source_key(shared_global)]["lifecycle_status"] == "active"
    assert retired.state["jobs"][generate_source_key(shared_global)]["scope_ownership"][source_scope_key("Lightspeed Commerce", "default")]["lifecycle_status"] == "active"


def test_failure_cooldown_and_pre_activation_candidate_removal_are_not_retirement() -> None:
    france = _job("FR-ACTIVE", "france")
    seeded = _reconcile(
        [france], empty_freshness_state(), [("Lightspeed Commerce", "france")]
    )
    unchanged_after_failure = _reconcile([], seeded.state, [])
    unchanged_after_cooldown = _reconcile([], unchanged_after_failure.state, [])
    key = generate_source_key(france)
    assert unchanged_after_cooldown.state["jobs"][key]["lifecycle_status"] == "active"
    assert "source_retirements" not in unchanged_after_cooldown.state

    empty_retirement = retire_source_scope(
        empty_freshness_state(),
        company="Lightspeed Commerce",
        source_scope="france",
        reason="candidate removed before activation",
        approved_by="release-owner",
        retired_at=AS_OF,
    )
    assert empty_retirement.owners_retired == 0
    assert empty_retirement.canonical_jobs_closed == 0


def test_supabase_retirement_boundary_requires_evidence_and_exact_scope_key() -> None:
    calls = []

    class Response:
        data = {"owners_retired": 1, "canonical_jobs_closed": 1}

    class Request:
        def execute(self):
            return Response()

    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return Request()

    store = SupabaseJobStore(Client())
    result = store.retire_source_scope(
        company="Lightspeed Commerce",
        source_scope="france",
        reason="approved source rollback",
        approved_by="release-owner",
        retired_at=AS_OF,
    )
    assert result["owners_retired"] == 1
    assert calls[0][0] == "retire_student_job_source_scope"
    assert calls[0][1]["p_source_scope_key"] == "lightspeed commerce::france"
    with pytest.raises(ValueError, match="requires company"):
        store.retire_source_scope(
            company="Lightspeed Commerce",
            source_scope="france",
            reason="short",
            approved_by="release-owner",
            retired_at=AS_OF,
        )


def test_migration_preflight_and_verification_are_read_only_and_complete() -> None:
    preflight = PREFLIGHT.read_text(encoding="utf-8").casefold()
    verification = VERIFY.read_text(encoding="utf-8").casefold()
    migration = MIGRATION.read_text(encoding="utf-8").casefold()
    assert "begin transaction read only" in preflight
    assert preflight.rstrip().endswith("rollback;")
    for contract in (
        "protected_source_keys", "expected phase 3 begin rpc", "pgcrypto",
        "incompatible partial/already-applied state", "tracked_job_source_scopes",
    ):
        assert contract in preflight
    for contract in (
        "ownership_complete", "ownership_not_orphaned", "ownership_not_duplicated",
        "ownership_lifecycle_parity", "scope_begin_rpc_installed", "rls_enabled",
        "service_role_dml",
    ):
        assert contract in verification
    assert verification.rstrip().endswith("rollback;")
    assert migration.lstrip().startswith("-- phase 5")
    assert "begin;" in migration[:500]
    assert migration.rstrip().endswith("commit;")


def test_legacy_backfill_preserves_lifecycle_and_staged_identity() -> None:
    migration = MIGRATION.read_text(encoding="utf-8").casefold()
    for contract in (
        "add column if not exists source_scope text not null default 'default'",
        "set source_scope_key = lower(regexp_replace(btrim(company)",
        "provider_source_key = source_key",
        "source_key, first_seen_at, last_seen_at, missing_successful_runs",
        "lifecycle_status, removed_at",
        "on conflict (source_key, source_scope_key) do nothing",
        "references public.tracked_jobs(source_key) on delete cascade",
    ):
        assert contract in migration
    assert migration.index("insert into public.tracked_job_source_scopes") < migration.index("drop function if exists public.begin_student_job_source_sync")


def test_retirement_sql_is_administrative_audited_and_service_role_only() -> None:
    migration = MIGRATION.read_text(encoding="utf-8").casefold()
    schema = (ROOT / "sql" / "supabase_schema.sql").read_text(encoding="utf-8").casefold()
    for content in (migration, schema):
        assert "create table if not exists public.source_scope_retirements" in content
        assert "create or replace function public.retire_student_job_source_scope" in content
        assert "p_reason text" in content
        assert "p_approved_by text" in content
        assert "grant execute on function public.retire_student_job_source_scope" in content
        assert "to service_role" in content
        assert "from public,anon,authenticated" in content


def test_batch4_backlog_classification_is_complete_and_disjoint() -> None:
    data = yaml.safe_load((ROOT / "config" / "phase5_batch4_backlog.yaml").read_text(encoding="utf-8"))
    categories = (
        "VALIDATION_ONLY", "IDENTITY_RESEARCH", "PROTOCOL_DEFERRED",
        "SCOPE_UNRESOLVED", "REJECTED",
    )
    values = [candidate for category in categories for candidate in data[category]]
    assert len(values) == len(set(values)) == 21
    assert data["VALIDATION_ONLY"] == ["lightspeed_fr_greenhouse"]
    assert data["phase6_started"] is False
    assert data["coding_batch_required_for_lightspeed_rollout"] is False
