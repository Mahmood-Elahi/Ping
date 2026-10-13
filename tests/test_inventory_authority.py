from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from src.scraper.company_careers import scanner as scanner_module
from src.scraper.company_careers.freshness import (
    FreshnessRuntime,
    empty_freshness_state,
    load_freshness_config,
    stable_job_key,
)
from src.scraper.company_careers.incremental import ResultAuthority
from src.scraper.company_careers.models import (
    AdapterError,
    AdapterResult,
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    InventoryAuthority,
    InventoryItem,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.source_cooldown import (
    classify_failure,
    reconcile_source_health,
)
from src.utils.job_retention import RetentionPolicy
from scripts.validate_source_integrity_canaries import (
    CanaryBudgetReached,
    run_canary,
)


AS_OF = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
FRESHNESS = load_freshness_config()
ROOT = Path(__file__).resolve().parents[1]


def job(job_id: str, *, scope: str = "default") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        title=f"Role {job_id}",
        company="Example",
        location="Calgary",
        url=f"https://jobs.example.test/{scope}/{job_id}",
        updated_at="2026-10-12T00:00:00Z",
        source=f"fixture:{scope}",
        source_scope=scope,
    )


def metadata(kind: str, count: int) -> SourceScanMetadata:
    common = dict(
        jobs_normalized=count,
        pages_requested=1,
        pages_completed=1,
        source_identity_status="validated",
    )
    if kind == "complete":
        return SourceScanMetadata(
            **common,
            scan_complete=True,
            scan_completeness_reason="complete_collection_response",
            stop_reason="complete_collection_response",
            total_available=count,
            complete_collection_response=True,
            terminal_page_observed=True,
            terminal_evidence_type="complete_collection_response",
            has_more=False,
        )
    if kind == "cap":
        return SourceScanMetadata(
            **common,
            stop_reason="provider_cap_reached",
            scan_completeness_reason="provider_cap_reached",
            safety_limit_reached=True,
            has_more=True,
        )
    if kind == "mutation":
        return SourceScanMetadata(
            **common,
            stop_reason="collection_changed_during_scan",
            scan_completeness_reason="collection_changed_during_scan",
            collection_changed_during_scan=True,
        )
    if kind == "identity":
        return SourceScanMetadata(
            **{**common, "source_identity_status": "failed"},
            stop_reason="identity_failure",
            scan_completeness_reason="identity_failure",
            identity_required=True,
        )
    if kind == "unproven":
        return SourceScanMetadata(
            **{**common, "source_identity_status": "unproven"},
            stop_reason="maximum_pages_reached",
            scan_completeness_reason="safety_limit_reached",
            safety_limit_reached=True,
            has_more=True,
        )
    if kind == "repeated":
        return SourceScanMetadata(
            **common,
            stop_reason="repeated_page",
            scan_completeness_reason="repeated_page",
            repeated_page=True,
        )
    return SourceScanMetadata(
        **common,
        stop_reason="maximum_pages_reached",
        scan_completeness_reason="maximum_pages_reached",
        safety_limit_reached=True,
        has_more=True,
    )


def scan_result(monkeypatch, authority: InventoryAuthority, kind: str, jobs: list[JobRecord]):
    platform = f"fixture_{authority.value}_{kind}"
    target = CompanyTarget(
        name="Example",
        platform=platform,
        inventory_authority=authority,
    )

    def adapter(_session, _target, _timeout):
        return AdapterResult(
            jobs=jobs,
            scan=metadata(kind, len(jobs)),
            inventory_items=tuple(InventoryItem(item.job_id, item.source) for item in jobs),
        )

    monkeypatch.setitem(scanner_module.ADAPTERS, platform, adapter)
    scanner = CompanyCareersScanner(
        targets=[target],
        session=requests.Session(),
        parent_source_mappings=[],
        as_of=AS_OF,
    )
    return scanner._scan_target(
        target,
        keywords=None,
        locations=None,
        max_jobs_per_company=None,
        job_filter=None,
        location_filter=None,
        browser_batch=None,
        browser_batch_error=None,
    )


def process(report: CompanyCareersReport, state=None, *, when=AS_OF):
    runtime = FreshnessRuntime(
        config=FRESHNESS,
        previous_state=state or empty_freshness_state(),
        as_of=when,
    )
    return runtime.process_report(report)


def test_authoritative_complete_persists_positives_and_reconciles_absence(monkeypatch) -> None:
    initial = scan_result(
        monkeypatch, InventoryAuthority.AUTHORITATIVE, "complete", [job("A"), job("B")]
    )
    seeded = process(CompanyCareersReport([initial]))
    current = scan_result(
        monkeypatch, InventoryAuthority.AUTHORITATIVE, "complete", [job("B"), job("C")]
    )
    result = process(
        CompanyCareersReport([current]), seeded.state, when=AS_OF + timedelta(days=1)
    )

    assert current.authoritative_for_absence is True
    assert stable_job_key(job("C")) in result.state["jobs"]
    assert result.state["jobs"][stable_job_key(job("A"))]["missing_scan_count"] == 1


def test_authoritative_incomplete_keeps_positives_without_reconciling_absence(monkeypatch) -> None:
    seeded = process(
        CompanyCareersReport(
            [scan_result(monkeypatch, InventoryAuthority.AUTHORITATIVE, "complete", [job("A")])]
        )
    )
    current = scan_result(
        monkeypatch, InventoryAuthority.AUTHORITATIVE, "partial", [job("B")]
    )
    result = process(CompanyCareersReport([current]), seeded.state)

    assert stable_job_key(job("B")) in result.state["jobs"]
    assert result.state["jobs"][stable_job_key(job("A"))]["missing_scan_count"] == 0
    assert current.authoritative_for_absence is False


def test_discovery_only_partial_persists_positives_without_completion_or_absence(monkeypatch) -> None:
    seeded = process(
        CompanyCareersReport(
            [scan_result(monkeypatch, InventoryAuthority.AUTHORITATIVE, "complete", [job("A")])]
        )
    )
    current = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "partial", [job("B")]
    )
    result = process(CompanyCareersReport([current]), seeded.state)

    assert current.result_authority is ResultAuthority.POSITIVE_ONLY
    assert current.positive_inventory_usable is True
    assert current.scan_complete is False
    assert current.inventory_complete is False
    assert current.source_health_code == "healthy_discovery_only"
    assert current.authoritative_for_absence is False
    assert stable_job_key(job("B")) in result.state["jobs"]
    assert result.state["jobs"][stable_job_key(job("A"))]["missing_scan_count"] == 0


def test_discovery_only_terminal_evidence_still_cannot_authorize_absence(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "complete", [job("A")]
    )

    assert result.terminal_evidence_type == "complete_collection_response"
    assert result.scan_complete is False
    assert result.inventory_complete is False
    assert result.scan_completeness_reason == "discovery_only_no_absence_authority"
    assert result.authoritative_for_absence is False
    assert CompanyCareersReport([result]).authoritative_source_scopes() == ()


def test_discovery_only_provider_cap_and_mutation_keep_safe_positives(monkeypatch) -> None:
    for kind in ("cap", "mutation"):
        result = scan_result(
            monkeypatch, InventoryAuthority.DISCOVERY_ONLY, kind, [job(kind)]
        )
        assert result.jobs
        assert result.positive_inventory_usable is True
        assert result.source_health_code == "healthy_discovery_only"
        assert result.authoritative_for_absence is False


def test_discovery_only_repeated_page_keeps_validated_first_page_positives(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "repeated", [job("FIRST")]
    )

    assert result.jobs
    assert result.result_authority is ResultAuthority.POSITIVE_ONLY
    assert result.positive_inventory_usable is True
    assert result.source_health_code == "healthy_discovery_only"
    assert result.stop_reason == "repeated_page"
    assert result.scan_complete is False
    assert result.authoritative_for_absence is False


def test_discovery_only_unproven_identity_rejects_otherwise_valid_positives(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "unproven", [job("UNSAFE")]
    )

    assert result.status == "failed_technical"
    assert result.scan_state == "failed"
    assert result.source_identity_status == "unproven"
    assert result.source_health_code == "identity_failure"
    assert result.result_authority is ResultAuthority.INCOMPLETE
    assert result.positive_inventory_usable is False
    assert result.jobs == []
    assert result.inventory_items == ()


def test_discovery_only_does_not_hide_parser_or_identity_failure(monkeypatch) -> None:
    platform = "fixture_identity_failure"
    target = CompanyTarget(
        name="Example",
        platform=platform,
        inventory_authority=InventoryAuthority.DISCOVERY_ONLY,
    )

    def adapter(_session, _target, _timeout):
        raise AdapterError("identity mismatch in malformed response")

    monkeypatch.setitem(scanner_module.ADAPTERS, platform, adapter)
    scanner = CompanyCareersScanner(
        targets=[target], session=requests.Session(), parent_source_mappings=[]
    )
    result = scanner._scan_target(
        target,
        keywords=None,
        locations=None,
        max_jobs_per_company=None,
        job_filter=None,
        location_filter=None,
        browser_batch=None,
        browser_batch_error=None,
    )

    assert result.scan_state == "failed"
    assert result.source_health_code == "identity_failure"
    assert result.positive_inventory_usable is False
    assert result.authoritative_for_absence is False


def test_discovery_only_rejects_returned_jobs_when_identity_metadata_fails(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "identity", [job("UNSAFE")]
    )

    assert result.status == "failed_technical"
    assert result.scan_state == "failed"
    assert result.source_health_code == "identity_failure"
    assert result.jobs == []
    assert result.normalized_jobs == []
    assert result.inventory_items == ()


def test_discovery_only_valid_empty_terminal_is_healthy_but_never_complete(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "complete", []
    )

    assert result.status == "fetched_no_matching_roles"
    assert result.source_health_code == "healthy_discovery_only"
    assert result.scan_complete is False
    assert result.inventory_complete is False
    assert result.positive_inventory_usable is False
    assert classify_failure(result) is None


def test_discovery_only_scope_cannot_close_another_scope(monkeypatch) -> None:
    auth_job = job("AUTH", scope="authoritative")
    discovery_job = job("DISC", scope="discovery")
    auth = scan_result(
        monkeypatch, InventoryAuthority.AUTHORITATIVE, "complete", [auth_job]
    )
    discovery = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "complete", [discovery_job]
    )
    auth.target.source_scope = "authoritative"
    discovery.target.source_scope = "discovery"
    for item in auth.normalized_jobs:
        item.source_scope = "authoritative"
    for item in discovery.normalized_jobs:
        item.source_scope = "discovery"

    seeded = process(CompanyCareersReport([auth, discovery]))
    followup = process(
        CompanyCareersReport([discovery]), seeded.state, when=AS_OF + timedelta(days=1)
    )

    assert CompanyCareersReport([auth, discovery]).authoritative_source_scopes() == (
        ("Example", "authoritative"),
    )
    assert followup.state["jobs"][stable_job_key(auth_job)]["missing_scan_count"] == 0


def test_expected_discovery_result_is_not_a_failure_or_cooldown_trigger(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "partial", [job("A")]
    )
    previous = {
        "source": {
            "consecutive_transient_failures": 3,
            "cooldown_until": (AS_OF + timedelta(hours=6)).isoformat(),
            "last_successful_authoritative_inventory": "2026-10-01T00:00:00+00:00",
        }
    }
    from src.scraper.company_careers.source_cooldown import source_health_key

    previous = {source_health_key(result.target): next(iter(previous.values()))}
    row = reconcile_source_health([result], previous, as_of=AS_OF)[0]

    assert classify_failure(result) is None
    assert row["consecutive_transient_failures"] == 0
    assert row["cooldown_until"] is None
    assert row["last_successful_authoritative_inventory"] == "2026-10-01T00:00:00+00:00"


def test_discovery_canary_serializes_effective_authority_and_preserves_raw_stop(monkeypatch) -> None:
    target = CompanyTarget(
        name="Example",
        platform="fixture_discovery_canary",
        inventory_authority=InventoryAuthority.DISCOVERY_ONLY,
    )

    def interrupted(session, _target, _timeout):
        publish_source_scan_metadata(
            session,
            SourceScanMetadata(
                jobs_normalized=3,
                total_available=100,
                authoritative_total_present=True,
                has_more=True,
                source_identity_status="validated",
            ),
        )
        raise CanaryBudgetReached("fixture budget")

    monkeypatch.setitem(scanner_module.ADAPTERS, target.platform, interrupted)
    row = run_canary(target, 1)

    assert row["canary_outcome"] == "PASS_PARTIAL"
    assert row["inventory_authority"] == "discovery_only"
    assert row["positive_inventory_usable"] is True
    assert row["result_authority"] == "positive_only"
    assert row["authoritative_for_absence"] is False
    assert row["can_reconcile_absence"] is False
    assert row["central_finalizer_authoritative"] is False
    assert row["source_health_code"] == "healthy_discovery_only"
    assert row["stop_reason"] == "canary_budget_reached"
    assert row["scan_complete"] is False
    assert row["inventory_complete"] is False


def test_coverage_serialization_includes_both_authority_views(monkeypatch) -> None:
    result = scan_result(
        monkeypatch, InventoryAuthority.DISCOVERY_ONLY, "partial", [job("A")]
    )

    row = result.to_coverage_dict()

    assert row["inventory_authority"] == "discovery_only"
    assert row["positive_inventory_usable"] is True
    assert row["result_authority"] == "positive_only"
    assert row["authoritative_for_absence"] is False
    assert row["can_reconcile_absence"] is False


def test_retention_remains_independent_of_inventory_authority() -> None:
    policy = RetentionPolicy(max_job_age_days=60, drop_stale=True)
    old = {
        "source_posted_at": (AS_OF - timedelta(days=61)).isoformat(),
        "first_seen_at": AS_OF.isoformat(),
        "freshness_band": "aging",
        "inventory_authority": "discovery_only",
    }

    assert policy.observation_is_persistable(old, as_of=AS_OF) is False


def test_missing_inventory_authority_is_backwards_compatible() -> None:
    target = CompanyTarget.from_dict({"name": "Legacy", "platform": "greenhouse"})

    assert target.inventory_authority is InventoryAuthority.AUTHORITATIVE


def test_invalid_inventory_authority_is_rejected() -> None:
    try:
        CompanyTarget.from_dict(
            {"name": "Invalid", "platform": "greenhouse", "inventory_authority": "maybe"}
        )
    except ValueError as exc:
        assert "maybe" in str(exc)
    else:
        raise AssertionError("invalid inventory authority was accepted")


def test_direct_legacy_result_still_uses_central_gate() -> None:
    result = CompanyScanResult(
        target=CompanyTarget(name="Legacy", platform="greenhouse"),
        scan_complete=True,
        status="fetched",
    )

    assert result.authoritative_for_absence is True


def test_candidate_overlay_and_36_source_classification_are_deterministic() -> None:
    production = yaml.safe_load(
        (ROOT / "config" / "target_companies.yaml").read_text(encoding="utf-8")
    )["target_companies"]
    candidates = yaml.safe_load(
        (ROOT / "config" / "target_companies.discovery_only_candidates.yaml").read_text(
            encoding="utf-8"
        )
    )["target_companies"]
    artifact = yaml.safe_load(
        (ROOT / "config" / "discovery_only_source_classification.yaml").read_text(
            encoding="utf-8"
        )
    )
    production_by_name = {row["name"]: row for row in production}
    discovery_only_production = {
        row["name"]: row
        for row in production
        if row.get("inventory_authority") == "discovery_only"
    }
    candidate_names = {row["name"] for row in candidates}
    source_rows = artifact["sources"]

    assert len(source_rows) == len({row["company"] for row in source_rows}) == 36
    assert artifact["artifact"]["classification_counts"] == {
        "AUTHORITATIVE": 0,
        "DISCOVERY_ONLY": 26,
        "BLOCKED_IDENTITY": 9,
        "BLOCKED_CONTRACT": 1,
    }
    assert candidate_names == {
        row["company"] for row in source_rows if row["recommended_mode"] == "DISCOVERY_ONLY"
    }
    assert all(row["inventory_authority"] == "discovery_only" for row in candidates)
    assert set(discovery_only_production) == candidate_names
    assert len(discovery_only_production) == 26
    for candidate in candidates:
        assert candidate == production_by_name[candidate["name"]]
    blocked = {row["company"]: row for row in source_rows}
    assert blocked["JPMorgan Chase"]["recommended_mode"] == "BLOCKED_IDENTITY"
    assert blocked["Coupang"]["recommended_mode"] == "BLOCKED_IDENTITY"
    assert "JPMorgan Chase" not in candidate_names
    assert "Coupang" not in candidate_names
    blocked_names = {
        row["company"]
        for row in source_rows
        if row["recommended_mode"] in {"BLOCKED_IDENTITY", "BLOCKED_CONTRACT"}
    }
    assert blocked_names.isdisjoint(discovery_only_production)
    assert production_by_name["Micron"]["platform"] == "eightfold"
    required = {
        "company",
        "current_platform",
        "current_source",
        "positive_identity_status",
        "positive_record_contract_status",
        "absence_authority_status",
        "recommended_mode",
        "reason",
        "current_blocker",
        "requires_live_canary",
        "candidate_registry_present",
    }
    assert all(set(row) == required for row in source_rows)


def test_micron_decision_uses_checked_in_operator_evidence_without_activation() -> None:
    artifact = yaml.safe_load(
        (ROOT / "config" / "discovery_only_source_classification.yaml").read_text(
            encoding="utf-8"
        )
    )["artifact"]
    micron = next(
        row
        for row in yaml.safe_load(
            (ROOT / "config" / "discovery_only_source_classification.yaml").read_text(
                encoding="utf-8"
            )
        )["sources"]
        if row["company"] == "Micron"
    )

    assert micron["recommended_mode"] == "DISCOVERY_ONLY"
    assert artifact["micron_prepared_alternate"]["status"] == "PREPARED_NOT_ACTIVATED"
    assert artifact["micron_prepared_alternate"]["tenant"] == "micron"
    assert artifact["micron_prepared_alternate"]["site_id"] == "External"
    assert (ROOT / "evidence" / "micron_identity" / "micron_id_crosswalk.json").exists()
    assert (ROOT / "evidence" / "micron_identity" / "micron_JR101216_probe.json").exists()
