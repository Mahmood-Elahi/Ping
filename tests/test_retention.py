from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.prune_supabase_retention import EXECUTION_CONFIRMATION, parse_args, run
from src.integrations.supabase_job_store import SupabaseJobStore
from src.scraper.company_careers.freshness import load_freshness_config, stable_job_key
from src.scraper.company_careers.models import JobRecord
from src.scraper.company_careers.student_roles import rank_student_jobs
from src.utils.job_retention import RetentionPolicy, effective_age_days


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
POLICY = load_freshness_config(str(ROOT / "config" / "freshness.yaml")).retention


def values(*, age_days: int, band: str = "aging", **extra):
    row = {
        "source_posted_at": (AS_OF - timedelta(days=age_days)).isoformat(),
        "first_seen_at": (AS_OF - timedelta(days=1)).isoformat(),
        "last_seen_at": AS_OF.isoformat(),
        "freshness_band": band,
        "lifecycle_status": "active",
        "status": "active",
    }
    row.update(extra)
    return row


def job(job_id: str) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        title="Software Engineering Intern",
        company="Example",
        location="Canada",
        url=f"https://example.test/jobs/{job_id}",
        updated_at="",
        source="greenhouse:example",
    )


def test_source_dated_job_at_61_days_is_retention_ineligible() -> None:
    row = values(age_days=61)
    assert effective_age_days(row, as_of=AS_OF) == 61
    assert not POLICY.age_is_eligible(row, as_of=AS_OF)
    assert not POLICY.observation_is_persistable(row, as_of=AS_OF)


def test_exactly_60_days_is_age_eligible_with_inclusive_boundary() -> None:
    row = values(age_days=60)
    assert effective_age_days(row, as_of=AS_OF) == 60
    assert POLICY.age_is_eligible(row, as_of=AS_OF)
    assert POLICY.observation_is_persistable(row, as_of=AS_OF)


def test_stale_job_is_ineligible_even_when_source_still_returns_it() -> None:
    assert not POLICY.observation_is_persistable(
        values(age_days=2, band="stale"), as_of=AS_OF
    )


@pytest.mark.parametrize("band", ["new", "fresh", "normal", "aging"])
def test_current_freshness_bands_under_60_days_remain_eligible(band: str) -> None:
    assert POLICY.current_output_is_eligible(
        values(age_days=2, band=band), as_of=AS_OF
    )


def test_unknown_source_age_uses_first_seen_fallback() -> None:
    row = {
        "source_posted_at": None,
        "first_seen_at": (AS_OF - timedelta(days=15)).isoformat(),
    }
    assert effective_age_days(row, as_of=AS_OF) == 15
    assert POLICY.age_is_eligible(row, as_of=AS_OF)


def test_malformed_source_posted_at_fails_safe_even_with_old_first_seen() -> None:
    row = {
        "source_posted_at": "not-a-trustworthy-date",
        "first_seen_at": (AS_OF - timedelta(days=61)).isoformat(),
        "freshness_band": "aging",
    }
    assert effective_age_days(row, as_of=AS_OF) is None
    assert POLICY.observation_is_persistable(row, as_of=AS_OF)

    normalized_row = {
        "source_posted_at": None,
        "first_seen_at": (AS_OF - timedelta(days=90)).isoformat(),
        "date_parse_errors": ["source_posted_at:unparseable"],
        "freshness_band": "aging",
    }
    assert effective_age_days(normalized_row, as_of=AS_OF) is None
    assert POLICY.observation_is_persistable(normalized_row, as_of=AS_OF)


def test_future_source_posted_at_fails_safe_even_with_old_first_seen() -> None:
    row = {
        "source_posted_at": (AS_OF + timedelta(days=1)).isoformat(),
        "first_seen_at": (AS_OF - timedelta(days=90)).isoformat(),
        "freshness_band": "aging",
    }
    assert effective_age_days(row, as_of=AS_OF) is None
    assert POLICY.observation_is_persistable(row, as_of=AS_OF)


def test_date_only_source_posted_at_is_valid_utc_age_evidence() -> None:
    row = {
        "source_posted_at": "2026-08-11",
        "first_seen_at": (AS_OF - timedelta(days=1)).isoformat(),
    }
    assert effective_age_days(row, as_of=AS_OF) == pytest.approx(60.5)
    assert not POLICY.age_is_eligible(row, as_of=AS_OF)


def test_exact_boundary_respects_timezone_offset() -> None:
    row = {
        "source_posted_at": "2026-08-11T06:00:00-06:00",
        "first_seen_at": (AS_OF - timedelta(days=1)).isoformat(),
    }
    assert effective_age_days(row, as_of=AS_OF) == 60
    assert POLICY.age_is_eligible(row, as_of=AS_OF)


@pytest.mark.parametrize("first_seen_at", ["invalid", "2026-10-11T00:00:00Z", None])
def test_missing_source_with_untrustworthy_first_seen_fails_safe(first_seen_at) -> None:
    row = {
        "source_posted_at": None,
        "first_seen_at": first_seen_at,
        "age_days": 999,
    }
    assert effective_age_days(row, as_of=AS_OF) is None
    assert POLICY.age_is_eligible(row, as_of=AS_OF)


def test_removed_closed_is_prunable_only_after_authoritative_closure() -> None:
    active = values(age_days=2, band="fresh")
    assert POLICY.prune_reason(active, as_of=AS_OF) is None
    removed = values(
        age_days=2,
        band="fresh",
        lifecycle_status="removed",
        status="closed",
        removed_at=AS_OF.isoformat(),
    )
    assert POLICY.prune_reason(removed, as_of=AS_OF) == "removed_closed"


def test_application_protection_and_reason_precedence_are_non_overlapping() -> None:
    rows = {
        "removed": values(
            age_days=90,
            band="stale",
            lifecycle_status="removed",
            status="closed",
            removed_at=AS_OF.isoformat(),
        ),
        "stale": values(age_days=90, band="stale"),
        "old": values(age_days=90, band="aging"),
        "protected": values(age_days=90, band="stale"),
    }
    protected_ids = {"protected"}
    buckets = Counter(
        "protected_by_application"
        if row_id in protected_ids and POLICY.prune_reason(row, as_of=AS_OF)
        else POLICY.prune_reason(row, as_of=AS_OF)
        for row_id, row in rows.items()
    )
    assert buckets == {
        "removed_closed": 1,
        "stale": 1,
        "over_60_days": 1,
        "protected_by_application": 1,
    }
    assert sum(buckets.values()) == len(rows)


def test_ranked_output_excludes_stale_and_over_age_but_keeps_valid_bands() -> None:
    jobs = [job(name) for name in ("new", "fresh", "normal", "aging", "stale", "old")]
    annotations = {}
    for current in jobs:
        band = current.job_id if current.job_id != "old" else "aging"
        age = 61 if current.job_id == "old" else 2
        annotations[stable_job_key(current)] = values(age_days=age, band=band)
    ranked = rank_student_jobs(
        jobs,
        now=AS_OF,
        freshness_by_key=annotations,
        retention_policy=POLICY,
    )
    assert {row["job_id"] for row in ranked} == {"new", "fresh", "normal", "aging"}


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []
        self.cursor = None
        self.row_limit = None

    def select(self, _columns):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def gt(self, field, value):
        self.cursor = (field, value)
        return self

    def order(self, _field, desc=False):
        return self

    def limit(self, count):
        self.row_limit = count
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(row.get(field) == value for field, value in self.filters)
        ]
        if self.cursor:
            field, value = self.cursor
            rows = [row for row in rows if row[field] > value]
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        return _Response(rows)


class _DashboardClient:
    def __init__(self, jobs):
        self.jobs = jobs
        self.tables_requested = []

    def table(self, name):
        self.tables_requested.append(name)
        if name == "tracked_jobs":
            return _Query(self.jobs)
        if name == "scan_runs":
            return _Query([])
        raise AssertionError(name)


def test_dashboard_hides_application_linked_old_and_stale_rows() -> None:
    valid = {
        "source_key": "valid",
        "company": "Example",
        "title": "Intern",
        "status": "active",
        "lifecycle_status": "active",
        "is_ranked_output": True,
        **values(age_days=2, band="fresh"),
    }
    old_application_job = {
        **valid,
        "source_key": "old-application",
        **values(age_days=61, band="aging"),
    }
    stale_application_job = {
        **valid,
        "source_key": "stale-application",
        **values(age_days=2, band="stale"),
    }
    client = _DashboardClient([valid, old_application_job, stale_application_job])
    store = SupabaseJobStore(
        client,
        now_factory=lambda: AS_OF,
        retention_policy=POLICY,
    )
    dashboard = store.fetch_dashboard_data()
    assert [row["source_key"] for row in dashboard.active_jobs] == ["valid"]
    assert "applications" not in client.tables_requested


def test_retention_migration_protects_applications_and_cascades_ownership() -> None:
    migration = (
        ROOT / "sql" / "migrations" / "20261010_phase5_bounded_retention.sql"
    ).read_text(encoding="utf-8").casefold()
    schema = (ROOT / "sql" / "supabase_schema.sql").read_text(encoding="utf-8").casefold()
    for sql in (migration, schema):
        assert "from public.applications application" in sql
        assert "protected_by_application" in sql
        assert "delete from public.tracked_jobs" in sql
        assert "references public.tracked_jobs(source_key) on delete cascade" in schema
        assert "counts_complete" in sql
        assert "current_tracked_jobs" in sql


def test_operational_history_prunes_only_finalized_and_preserves_staging() -> None:
    migration = (
        ROOT / "sql" / "migrations" / "20261010_phase5_bounded_retention.sql"
    ).read_text(encoding="utf-8").casefold()
    assert "session.status = 'finalized'" in migration
    assert "active.status = 'staging'" in migration
    assert "delete from public.job_sync_sessions" in migration
    assert "delete from public.scan_source_syncs" in migration
    assert "p_sync_history_retention_days" in migration
    assert "where status = 'staging'" in migration
    assert "join sync_retention_candidates candidate using (sync_id)" in migration
    assert "session.status = 'finalized'" in migration


def test_migration_installs_bounded_capability_without_invoking_cleanup() -> None:
    migration = (
        ROOT / "sql" / "migrations" / "20261010_phase5_bounded_retention.sql"
    ).read_text(encoding="utf-8").casefold()
    assert migration.lstrip().startswith("-- phase 5 bounded retention")
    assert "\nbegin;" in migration
    assert migration.rstrip().endswith("commit;")
    assert "limit p_batch_size" in migration
    assert "if p_execute then" in migration
    assert "select public.maintain_student_job_retention" not in migration
    assert "perform public.maintain_student_job_retention" not in migration
    assert "drop function if exists public.maintain_student_job_retention" in migration
    assert "security definer" in migration
    assert "to service_role" in migration
    assert "from public, anon, authenticated" in migration
    assert "language plpgsql\nimmutable" in migration
    assert "tracked_jobs_retention_posted_idx" in migration
    assert "tracked_jobs_retention_stale_idx" in migration
    assert "job_sync_sessions_active_scan_idx" in migration


class _MaintenanceStore:
    def __init__(self, reports):
        self.reports = iter(reports)
        self.calls = []
        self.validated = False

    def validate_setup(self):
        self.validated = True

    def maintain_retention(self, **kwargs):
        self.calls.append(kwargs)
        report = next(self.reports)
        if isinstance(report, Exception):
            raise report
        return report


def _report(*, jobs=0, sessions=0, source_syncs=0, deleted=None):
    counts = {
        "total_jobs_deletable": jobs,
        "old_sync_sessions": sessions,
        "old_scan_source_syncs": source_syncs,
    }
    return {
        "before": counts,
        "deleted": deleted or {
            "tracked_jobs": 0,
            "tracked_job_source_scope_rows": 0,
            "job_sync_sessions": 0,
            "job_sync_chunks": 0,
            "job_sync_stage": 0,
            "scan_source_syncs": 0,
        },
        "after": counts,
    }


def test_cleanup_dry_run_makes_zero_execute_calls(monkeypatch, capsys) -> None:
    store = _MaintenanceStore([_report(jobs=4)])
    monkeypatch.setattr(SupabaseJobStore, "from_env", lambda **_kwargs: store)
    args = parse_args(["--as-of", AS_OF.isoformat()])
    assert run(args) == 0
    assert store.validated
    assert [call["execute"] for call in store.calls] == [False]
    assert '"total_jobs_deletable": 4' in capsys.readouterr().out


def test_interrupted_cleanup_reports_checkpoint_and_is_safely_resumable(
    monkeypatch, capsys
) -> None:
    first = _MaintenanceStore([
        _report(jobs=2),
        _report(deleted={
            "tracked_jobs": 1,
            "tracked_job_source_scope_rows": 1,
            "job_sync_sessions": 0,
            "job_sync_chunks": 0,
            "job_sync_stage": 0,
            "scan_source_syncs": 0,
        }),
        RuntimeError("interrupted"),
    ])
    monkeypatch.setattr(SupabaseJobStore, "from_env", lambda **_kwargs: first)
    args = parse_args([
        "--as-of", AS_OF.isoformat(), "--execute", "--confirm", EXECUTION_CONFIRMATION
    ])
    with pytest.raises(RuntimeError, match="interrupted"):
        run(args)
    checkpoint = capsys.readouterr().err
    assert '"batches_completed": 1' in checkpoint
    assert '"tracked_jobs": 1' in checkpoint
    assert '"resumable": true' in checkpoint

    resumed = _MaintenanceStore([
        _report(jobs=1),
        _report(deleted={
            "tracked_jobs": 1,
            "tracked_job_source_scope_rows": 1,
            "job_sync_sessions": 0,
            "job_sync_chunks": 0,
            "job_sync_stage": 0,
            "scan_source_syncs": 0,
        }),
        _report(),
        _report(),
    ])
    monkeypatch.setattr(SupabaseJobStore, "from_env", lambda **_kwargs: resumed)
    assert run(args) == 0
    assert [call["execute"] for call in resumed.calls] == [False, True, True, False]
    assert '"complete": true' in capsys.readouterr().out


def test_maintenance_cli_is_dry_run_by_default_and_execution_is_explicit() -> None:
    assert not parse_args([]).execute
    with pytest.raises(SystemExit):
        parse_args(["--execute"])
    args = parse_args(["--execute", "--confirm", EXECUTION_CONFIRMATION])
    assert args.execute


def test_policy_defaults_match_production_contract() -> None:
    assert POLICY == RetentionPolicy(
        max_job_age_days=60,
        drop_stale=True,
        removed_job_retention_days=0,
        sync_history_retention_days=7,
    )
