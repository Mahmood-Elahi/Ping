from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest

from src.integrations.supabase_job_store import SupabaseJobStore

from src.scraper.company_careers.freshness import (
    FreshnessConfig,
    FreshnessThresholds,
    RemovalPolicy,
    UrgencyThresholds,
    empty_freshness_state,
    reconcile_lifecycle,
)
from src.scraper.company_careers.incremental import (
    CrawlMode,
    IncrementalCapability,
    IncrementalCheckpoint,
    IncrementalPolicy,
    IncrementalResult,
    ResultAuthority,
    ValidatedSnapshot,
    can_skip_hydration,
    choose_crawl_mode,
    configuration_fingerprint,
    material_content_fingerprint,
    reuse_validated_snapshot,
    validate_checkpoint_progress,
)
from src.scraper.company_careers.models import AdapterResult, SourceScanMetadata
from src.utils.job_retention import RetentionPolicy
from tests.test_supabase_job_store import FakeSupabaseClient


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
SCOPE = "example::default"
FP = configuration_fingerprint({"endpoint": "https://example.test/jobs"})
POLICY = IncrementalPolicy(
    capability=IncrementalCapability.TIMESTAMP_WATERMARK,
    strategy_version="watermark-v1",
    forced_full_interval=timedelta(days=7),
)


def checkpoint(**changes):
    base = IncrementalCheckpoint(
        source_scope_key=SCOPE,
        provider_identity="provider:board",
        config_fingerprint=FP,
        strategy_version="watermark-v1",
        timestamp_watermark=NOW - timedelta(hours=1),
        last_successful_full_at=NOW - timedelta(days=1),
        last_successful_incremental_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
    )
    return replace(base, **changes)


def decision(cp=None, **changes):
    return choose_crawl_mode(
        source_scope_key_value=SCOPE,
        provider_identity="provider:board",
        config_fingerprint=FP,
        policy=POLICY,
        checkpoint=cp,
        now=NOW,
        **changes,
    )


def test_01_initial_run_forces_full_crawl():
    assert decision().reason == "checkpoint_missing"


def test_02_valid_checkpoint_allows_incremental():
    assert decision(checkpoint()).mode is CrawlMode.INCREMENTAL_POSITIVE_ONLY


def test_03_config_fingerprint_change_forces_full():
    assert decision(checkpoint(config_fingerprint="old")).reason == "config_fingerprint_changed"


def test_04_corrupt_checkpoint_is_rejected_at_domain_boundary():
    corrupt = checkpoint().to_persistence_dict()
    corrupt["updated_at"] = "not-a-timestamp"
    assert decision(corrupt).reason == "checkpoint_corrupt"


def test_05_cursor_rollback_forces_fallback():
    policy = IncrementalCapability.CURSOR
    assert validate_checkpoint_progress(
        checkpoint(cursor="10"), checkpoint(cursor="9"), capability=policy
    ) == "cursor_rollback"


def test_06_timestamp_rollback_forces_fallback():
    assert validate_checkpoint_progress(
        checkpoint(), checkpoint(timestamp_watermark=NOW - timedelta(days=2)),
        capability=IncrementalCapability.TIMESTAMP_WATERMARK,
    ) == "timestamp_rollback"


def test_07_cursor_repetition_is_not_terminal_success():
    assert validate_checkpoint_progress(
        checkpoint(cursor="a"), checkpoint(cursor="a"),
        capability=IncrementalCapability.CURSOR,
    ) == "cursor_repeated"


def test_08_incremental_result_accepts_positive_observations():
    result = IncrementalResult(
        CrawlMode.INCREMENTAL_POSITIVE_ONLY, ResultAuthority.POSITIVE_ONLY,
        items_observed=2,
    )
    assert result.items_observed == 2


def test_09_incremental_result_cannot_reconcile_absence():
    with pytest.raises(ValueError, match="cannot authorize absence"):
        IncrementalResult(
            CrawlMode.INCREMENTAL_POSITIVE_ONLY,
            ResultAuthority.AUTHORITATIVE_INVENTORY,
        )


def test_10_positive_only_reconciliation_does_not_increment_missing():
    seeded = _reconcile([_job("1")], empty_freshness_state(), [("Example", "default")])
    observed = _reconcile([], seeded.state, [])
    assert observed.state["jobs"][next(iter(seeded.state["jobs"]))]["missing_scan_count"] == 0


def test_11_positive_only_reconciliation_does_not_close():
    seeded = _reconcile([_job("1")], empty_freshness_state(), [("Example", "default")])
    state = seeded.state
    for _ in range(3):
        state = _reconcile([], state, []).state
    assert next(iter(state["jobs"].values()))["lifecycle_status"] == "active"


def test_12_sibling_scope_isolated():
    first = _reconcile([_job("1", "professional"), _job("1", "early")], empty_freshness_state(), [])
    second = _reconcile([], first.state, [("Example", "professional")])
    owners = next(iter(second.state["jobs"].values()))["scope_ownership"]
    assert owners["example::early"]["lifecycle_status"] == "active"


def test_13_periodic_full_crawl_occurs():
    cp = checkpoint(last_successful_full_at=NOW - timedelta(days=7))
    assert decision(cp).reason == "forced_full_interval_elapsed"


def test_14_full_authoritative_can_reconcile_absence():
    seeded = _reconcile([_job("1")], empty_freshness_state(), [("Example", "default")])
    missing = _reconcile([], seeded.state, [("Example", "default")])
    assert next(iter(missing.state["jobs"].values()))["missing_scan_count"] == 1


def test_15_checkpoint_candidate_allowed_only_for_accepted_result():
    result = IncrementalResult(
        CrawlMode.INCREMENTAL_POSITIVE_ONLY,
        ResultAuthority.POSITIVE_ONLY,
        checkpoint_candidate=checkpoint(),
    )
    assert result.checkpoint_candidate is not None


def test_16_failed_or_incomplete_result_cannot_advance_checkpoint():
    with pytest.raises(ValueError, match="incomplete"):
        IncrementalResult(
            CrawlMode.INCREMENTAL_POSITIVE_ONLY,
            ResultAuthority.INCOMPLETE,
            checkpoint_candidate=checkpoint(),
        )


def test_17_partial_adapter_result_has_no_absence_authority():
    result = AdapterResult([], SourceScanMetadata(), crawl_mode=CrawlMode.INCREMENTAL_POSITIVE_ONLY,
                           result_authority=ResultAuthority.POSITIVE_ONLY)
    assert result.result_authority is ResultAuthority.POSITIVE_ONLY


def test_18_retention_is_not_provider_absence():
    policy = RetentionPolicy(max_job_age_days=60)
    old = {"source_posted_at": "2026-01-01T00:00:00Z", "lifecycle_status": "active"}
    assert policy.prune_reason(old, as_of=NOW) == "over_60_days"
    assert old["lifecycle_status"] == "active"


def test_19_protected_identity_prevents_false_absence():
    seeded = _reconcile([_job("1")], empty_freshness_state(), [("Example", "default")])
    key = next(iter(seeded.state["jobs"]))
    protected = _reconcile([], seeded.state, [("Example", "default")], [key])
    assert protected.state["jobs"][key]["missing_scan_count"] == 0


def test_20_trusted_unchanged_version_skips_hydration():
    assert can_skip_hydration(stored_version="v1", inventory_version="v1")


def test_21_changed_or_missing_version_requires_hydration():
    assert not can_skip_hydration(stored_version="v1", inventory_version="v2")
    assert not can_skip_hydration(stored_version="v1", inventory_version=None)


def test_22_observation_timestamp_does_not_change_material_fingerprint():
    a = _job("1") | {"last_seen_at": "2026-10-09T00:00:00Z"}
    b = _job("1") | {"last_seen_at": "2026-10-10T00:00:00Z"}
    assert material_content_fingerprint(a) == material_content_fingerprint(b)


def test_23_valid_reviewed_304_reuses_snapshot():
    payload = b'[{"id":"1"}]'
    cached = ValidatedSnapshot(SCOPE, FP, "provider:board", '"etag"', payload)
    assert reuse_validated_snapshot(
        response_status=304, source_scope_key_value=SCOPE, config_fingerprint=FP,
        provider_identity="provider:board", response_validator='"etag"',
        cached=cached, contract_reviewed=True,
    ) == payload


def test_24_304_without_valid_snapshot_fails_closed():
    with pytest.raises(ValueError, match="invalid_304_state"):
        reuse_validated_snapshot(
            response_status=304, source_scope_key_value=SCOPE,
            config_fingerprint=FP, provider_identity="provider:board",
            response_validator='"etag"', cached=None, contract_reviewed=True,
        )


def test_25_cache_corruption_cannot_become_empty_inventory():
    cached = ValidatedSnapshot(SCOPE, FP, "provider:board", '"etag"', b"[]")
    object.__setattr__(cached, "payload", b"corrupt")
    with pytest.raises(ValueError, match="cache_corruption"):
        reuse_validated_snapshot(
            response_status=304, source_scope_key_value=SCOPE,
            config_fingerprint=FP, provider_identity="provider:board",
            response_validator='"etag"', cached=cached, contract_reviewed=True,
        )


def test_26_checkpoint_state_is_bounded():
    with pytest.raises(ValueError, match="cursor exceeds"):
        checkpoint(cursor="x" * 2049)


def test_27_phase3_adapter_completeness_is_still_separate():
    result = AdapterResult([], SourceScanMetadata(scan_complete=False))
    assert not result.scan["scan_complete"]
    assert result.result_authority is ResultAuthority.INCOMPLETE


def test_28_ambiguous_incremental_forces_full():
    assert decision(checkpoint(ambiguous_since_last_full=True)).reason == "previous_incremental_ambiguous"


def test_29_provider_regression_forces_full():
    assert decision(checkpoint(), provider_state_regressed=True).reason == "provider_state_regressed"


def test_30_metrics_are_deterministic_and_non_speculative():
    result = IncrementalResult(
        CrawlMode.INCREMENTAL_POSITIVE_ONLY, ResultAuthority.POSITIVE_ONLY,
        checkpoint_candidate=checkpoint(), requests_saved=4,
        detail_hydrations_skipped=3, items_observed=2,
    )
    metrics = result.metrics(now=NOW)
    assert metrics["requests_saved"] == 4
    assert metrics["absence_authorized"] is False
    assert metrics["checkpoint_advanced"] is False


def test_positive_only_store_path_passes_false_absence_permission():
    client = FakeSupabaseClient()
    store = SupabaseJobStore(client, now_factory=lambda: NOW)
    store.sync_student_jobs(
        [_job("1")], successful_companies=(), started_at=NOW,
        companies_checked=1, jobs_fetched=1,
        source_authorities={SCOPE: ResultAuthority.POSITIVE_ONLY},
    )
    begin = next(params for name, params in client.rpc_calls if name == "begin_student_job_source_sync")
    assert begin["p_reconcile_absence"] is False


def test_store_hands_checkpoint_to_atomic_finalizer_session():
    client = FakeSupabaseClient()
    store = SupabaseJobStore(client, now_factory=lambda: NOW)
    store.sync_student_jobs(
        [_job("1")], successful_companies=(), started_at=NOW,
        companies_checked=1, jobs_fetched=1,
        source_authorities={SCOPE: ResultAuthority.POSITIVE_ONLY},
        checkpoint_advances={SCOPE: checkpoint()},
    )
    begin = next(params for name, params in client.rpc_calls if name == "begin_student_job_source_sync")
    assert begin["p_checkpoint_candidate"]["source_scope_key"] == SCOPE


def test_incomplete_store_result_cannot_advance_checkpoint():
    store = SupabaseJobStore(FakeSupabaseClient(), now_factory=lambda: NOW)
    with pytest.raises(ValueError, match="incomplete"):
        store.sync_student_jobs(
            [_job("1")], successful_companies=(), started_at=NOW,
            companies_checked=1, jobs_fetched=1,
            source_authorities={SCOPE: ResultAuthority.INCOMPLETE},
            checkpoint_advances={SCOPE: checkpoint()},
        )


def test_phase6_migration_is_atomic_bounded_and_service_role_only():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "sql/migrations/20261010_phase6_incremental_checkpoints.sql").read_text()
    bootstrap = (root / "sql/supabase_schema.sql").read_text()
    for sql in (migration, bootstrap):
        lowered = sql.casefold()
        assert "source_incremental_checkpoints" in lowered
        assert "checkpoint_candidate" in lowered
        assert "after update of status on public.job_sync_sessions" in lowered
        assert "grant all on table public.source_incremental_checkpoints to service_role" in lowered
        assert "grant all on table public.source_incremental_checkpoints to anon" not in lowered
        assert "length(cursor) <= 2048" in lowered


def _job(job_id, scope="default"):
    return {
        "job_id": job_id,
        "source": "provider",
        "company": "Example",
        "source_scope": scope,
        "title": "Engineer",
        "location": "Calgary",
        "url": f"https://example.test/{job_id}",
        "updated_at": "2026-10-10T00:00:00Z",
    }


def _reconcile(jobs, state, scopes, protected=()):
    return reconcile_lifecycle(
        jobs,
        state,
        as_of=NOW,
        config=FreshnessConfig(
            FreshnessThresholds(24, 7, 30, 45),
            UrgencyThresholds(2, 7, 48, 30, {"apply_now": 100, "high": 75, "normal": 50, "low": 25, "closed": 0}),
            RemovalPolicy(consecutive_complete_scans=2),
            RetentionPolicy(),
        ),
        complete_source_scopes=scopes,
        complete_companies=(),
        protected_source_keys=protected,
    )
