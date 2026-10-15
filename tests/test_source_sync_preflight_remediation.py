from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPARE_PATH = ROOT / "scripts" / "compare_source_sync_finalizer.py"
BASELINE_PATH = (
    ROOT / "sql" / "migrations" / "20261010_phase5_scope_local_finalizer.sql"
)
MIGRATION_PATH = (
    ROOT / "sql" / "migrations" / "20261015_optimize_source_sync_finalizer.sql"
)
VERIFY_PATH = ROOT / "sql" / "20261015_source_sync_optimization_verify.sql"
REMEDIATION_PATH = ROOT / "sql" / "20261015_canonical_removed_at_remediation.sql"
ROLLBACK_PATH = (
    ROOT / "sql" / "20261015_canonical_removed_at_remediation_rollback.sql"
)


def _load_compare_module():
    spec = importlib.util.spec_from_file_location("compare_source_sync_finalizer", COMPARE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_function_acl_accepts_real_owner_acl_and_rejects_api_roles() -> None:
    for path in (MIGRATION_PATH, VERIFY_PATH):
        sql = path.read_text(encoding="utf-8").casefold()
        assert "postgres:execute:false" in sql
        assert "service_role:execute:false" in sql
        assert "postgres:execute:true" not in sql
        assert "has_function_privilege" in sql
        assert "anon_can_execute" in sql
        assert "authenticated_can_execute" in sql


def test_lexical_comparator_classifies_only_formatting_and_comments_as_harmless() -> None:
    compare = _load_compare_module()
    baseline = compare.extract_finalizer_body(BASELINE_PATH.read_text(encoding="utf-8"))
    reformatted = "\n-- deployment-only comment\n" + baseline.replace(
        "declare", "DECLARE", 1
    ).replace("begin", "\n\nBEGIN", 1)

    result = compare.compare_bodies(baseline, reformatted)

    assert result["result"] == "TOKEN_EQUIVALENT"
    assert result["baseline_normalized_body_md5"] != result[
        "deployed_normalized_body_md5"
    ]
    assert result["baseline_token_sha256"] == result["deployed_token_sha256"]


def test_lexical_comparator_fails_closed_on_sql_or_literal_changes() -> None:
    compare = _load_compare_module()
    baseline = compare.extract_finalizer_body(BASELINE_PATH.read_text(encoding="utf-8"))
    changed_sql = baseline.replace("v_chunks<>v_session.expected_chunks", "false", 1)
    changed_literal = baseline.replace("'unknown sync ID'", "'different error'", 1)

    for changed in (changed_sql, changed_literal):
        result = compare.compare_bodies(baseline, changed)
        assert result["result"] == "TOKEN_DIFFERENCE_STOP"
        assert "first_mismatch_token" in result


def _tracked_jobs_assignments(sql: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", match).strip().casefold()
        for match in re.findall(
            r"update\s+public\.tracked_jobs\s+tracked\s+set\s+(.*?)\s+from\s+",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
    ]


def test_removed_at_remediation_is_exact_guarded_and_column_minimal() -> None:
    sql = REMEDIATION_PATH.read_text(encoding="utf-8")
    folded = sql.casefold()

    assert "v_inconsistent<>81" in folded
    assert "v_exact_stale<>81" in folded
    assert "v_other_inconsistent<>0" in folded
    assert "aggregate.active_owner_count>0" in folded
    assert "aggregate.invalid_active_owner_counter_count=0" in folded
    assert "aggregate.minimum_missing=0" in folded
    assert "tracked.status='active'" in folded
    assert "tracked.lifecycle_status='active'" in folded
    assert "tracked.missing_successful_runs=0" in folded
    assert "tracked.missing_successful_runs is not distinct from" in folded
    assert "tracked.removed_at is not null" in folded
    assert "snapshot_count_mismatch" in folded
    assert "postflight_failed" in folded
    assert "lock table public.tracked_job_source_scopes" in folded
    assert "lock table public.tracked_jobs" in folded
    assert "commit;" in folded
    assert "update public.tracked_job_source_scopes" not in folded
    assert _tracked_jobs_assignments(sql) == ["removed_at=null"]


def test_removed_at_rollback_requires_unchanged_snapshot_and_restores_only_timestamp() -> None:
    sql = ROLLBACK_PATH.read_text(encoding="utf-8")
    folded = sql.casefold()

    assert "snapshot_count_mismatch" in folded
    assert "rollback_targets_changed" in folded
    assert "snapshot.previous_removed_at" in folded
    assert "tracked.first_seen_at is distinct from snapshot.first_seen_at" in folded
    assert "tracked.last_seen_at is distinct from snapshot.last_seen_at" in folded
    assert "update public.tracked_job_source_scopes" not in folded
    assert "drop table" not in folded
    assert _tracked_jobs_assignments(sql) == [
        "removed_at=snapshot.previous_removed_at"
    ]


def test_verifier_reports_stale_removed_at_separately_from_other_drift() -> None:
    sql = VERIFY_PATH.read_text(encoding="utf-8").casefold()

    assert "canonical_stale_active_removed_at_count" in sql
    assert "canonical_other_lifecycle_inconsistency_count" in sql
    assert "is_exact_stale_active_removed_at" in sql
    assert "stop_canonical_lifecycle_drift" in sql
