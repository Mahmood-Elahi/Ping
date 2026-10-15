-- MANUAL ROLLBACK for sql/20261015_canonical_removed_at_remediation.sql.
--
-- This intentionally restores the exact 81 stale timestamps captured by the
-- remediation snapshot. Run it only to reverse that remediation, never after
-- any target row or ownership state has legitimately changed.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '5min';

lock table public.tracked_job_source_scopes in share row exclusive mode;
lock table public.tracked_jobs in share row exclusive mode;

do $rollback_preflight$
declare
    v_snapshot_rows bigint;
    v_changed_rows bigint;
begin
    if current_user is distinct from 'postgres' then
        raise exception
            'CANONICAL_REMOVED_AT_ROLLBACK_REQUIRES_POSTGRES current_user=%',
            current_user;
    end if;
    if pg_catalog.to_regclass(
        'public.canonical_removed_at_remediation_20261015_snapshot'
    ) is null then
        raise exception 'CANONICAL_REMOVED_AT_ROLLBACK_SNAPSHOT_MISSING';
    end if;

    select count(*) into v_snapshot_rows
    from public.canonical_removed_at_remediation_20261015_snapshot;
    if v_snapshot_rows<>81 then
        raise exception
            'CANONICAL_REMOVED_AT_ROLLBACK_SNAPSHOT_COUNT_MISMATCH count=%',
            v_snapshot_rows;
    end if;

    with owner_aggregate as (
        select owner.source_key,
            count(*) as owner_count,
            count(*) filter (where owner.lifecycle_status='active')
                as active_owner_count,
            count(*) filter (where owner.lifecycle_status='possibly_missing')
                as possibly_missing_owner_count,
            count(*) filter (
                where owner.lifecycle_status='active'
                  and owner.missing_successful_runs<>0
            ) as invalid_active_owner_counter_count,
            min(owner.missing_successful_runs) as minimum_missing
        from public.tracked_job_source_scopes owner
        group by owner.source_key
    )
    select count(*)
      into v_changed_rows
    from public.canonical_removed_at_remediation_20261015_snapshot snapshot
    left join public.tracked_jobs tracked using(source_key)
    left join owner_aggregate aggregate using(source_key)
    where tracked.source_key is null
       or tracked.removed_at is not null
       or tracked.status is distinct from snapshot.status
       or tracked.lifecycle_status is distinct from snapshot.lifecycle_status
       or tracked.missing_successful_runs is distinct from
          snapshot.missing_successful_runs
       or tracked.first_seen_at is distinct from snapshot.first_seen_at
       or tracked.last_seen_at is distinct from snapshot.last_seen_at
       or aggregate.owner_count is distinct from snapshot.owner_count
       or aggregate.active_owner_count is distinct from snapshot.active_owner_count
       or aggregate.possibly_missing_owner_count is distinct from
          snapshot.possibly_missing_owner_count
       or aggregate.invalid_active_owner_counter_count is distinct from
          snapshot.invalid_active_owner_counter_count
       or aggregate.minimum_missing is distinct from snapshot.minimum_owner_missing;

    if v_changed_rows<>0 then
        raise exception
            'CANONICAL_REMOVED_AT_ROLLBACK_TARGETS_CHANGED count=%',
            v_changed_rows;
    end if;
end
$rollback_preflight$;

select
    'pre_rollback' as phase,
    count(*) as snapshot_rows,
    count(*) filter (where tracked.removed_at is null) as currently_cleared_rows
from public.canonical_removed_at_remediation_20261015_snapshot snapshot
join public.tracked_jobs tracked using(source_key);

do $restore$
declare
    v_updated bigint;
begin
    update public.tracked_jobs tracked
       set removed_at=snapshot.previous_removed_at
    from public.canonical_removed_at_remediation_20261015_snapshot snapshot
    where tracked.source_key=snapshot.source_key
      and tracked.removed_at is null
      and tracked.status is not distinct from snapshot.status
      and tracked.lifecycle_status is not distinct from snapshot.lifecycle_status
      and tracked.missing_successful_runs is not distinct from
          snapshot.missing_successful_runs
      and tracked.first_seen_at is not distinct from snapshot.first_seen_at
      and tracked.last_seen_at is not distinct from snapshot.last_seen_at;

    get diagnostics v_updated = row_count;
    if v_updated<>81 then
        raise exception
            'CANONICAL_REMOVED_AT_ROLLBACK_UPDATE_COUNT_MISMATCH updated=%',
            v_updated;
    end if;
end
$restore$;

do $rollback_postflight$
declare
    v_restore_mismatch bigint;
begin
    select count(*)
      into v_restore_mismatch
    from public.canonical_removed_at_remediation_20261015_snapshot snapshot
    left join public.tracked_jobs tracked using(source_key)
    where tracked.source_key is null
       or tracked.removed_at is distinct from snapshot.previous_removed_at
       or tracked.status is distinct from snapshot.status
       or tracked.lifecycle_status is distinct from snapshot.lifecycle_status
       or tracked.missing_successful_runs is distinct from
          snapshot.missing_successful_runs
       or tracked.first_seen_at is distinct from snapshot.first_seen_at
       or tracked.last_seen_at is distinct from snapshot.last_seen_at;

    if v_restore_mismatch<>0 then
        raise exception
            'CANONICAL_REMOVED_AT_ROLLBACK_POSTFLIGHT_FAILED count=%',
            v_restore_mismatch;
    end if;
end
$rollback_postflight$;

select
    'post_rollback' as phase,
    count(*) as restored_rows,
    count(*) filter (
        where tracked.removed_at is not distinct from snapshot.previous_removed_at
    ) as exact_timestamp_matches
from public.canonical_removed_at_remediation_20261015_snapshot snapshot
join public.tracked_jobs tracked using(source_key);

commit;
