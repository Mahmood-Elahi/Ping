-- MANUAL, ONE-TIME REMEDIATION. Do not run as a schema migration.
--
-- This transaction repairs only the 81 independently diagnosed canonical rows
-- whose ownership-derived lifecycle is active and internally consistent but
-- whose canonical removed_at is stale. Stop if the population is not exactly
-- that shape. Keep the snapshot table until the observation window closes.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '5min';

-- Block finalizers, retention, and retirement from changing either side of the
-- lifecycle relationship while eligibility is revalidated and repaired.
lock table public.tracked_job_source_scopes in share row exclusive mode;
lock table public.tracked_jobs in share row exclusive mode;

do $preflight$
declare
    v_inconsistent bigint;
    v_exact_stale bigint;
    v_other_inconsistent bigint;
begin
    if current_user is distinct from 'postgres' then
        raise exception
            'CANONICAL_REMOVED_AT_REMEDIATION_REQUIRES_POSTGRES current_user=%',
            current_user;
    end if;
    if pg_catalog.to_regclass('public.tracked_jobs') is null
       or pg_catalog.to_regclass('public.tracked_job_source_scopes') is null then
        raise exception 'CANONICAL_REMOVED_AT_REMEDIATION_MISSING_LIFECYCLE_TABLES';
    end if;
    if pg_catalog.to_regclass(
        'public.canonical_removed_at_remediation_20261015_snapshot'
    ) is not null then
        raise exception 'CANONICAL_REMOVED_AT_REMEDIATION_SNAPSHOT_ALREADY_EXISTS';
    end if;

    with owner_aggregate as (
        select owner.source_key,
            bool_or(owner.lifecycle_status='active') as has_active,
            bool_or(owner.lifecycle_status='possibly_missing') as has_possible,
            min(owner.missing_successful_runs) as minimum_missing,
            bool_or(
                owner.lifecycle_status='active'
                and owner.missing_successful_runs<>0
            ) as has_invalid_active_owner_counter
        from public.tracked_job_source_scopes owner
        group by owner.source_key
    ), assessment as (
        select
            (
                aggregate.source_key is null
                or tracked.status is distinct from case
                    when aggregate.has_active or aggregate.has_possible
                        then 'active' else 'closed' end
                or tracked.lifecycle_status is distinct from case
                    when aggregate.has_active then 'active'
                    when aggregate.has_possible then 'possibly_missing'
                    else 'removed' end
                or tracked.missing_successful_runs is distinct from
                    aggregate.minimum_missing
                or ((aggregate.has_active or aggregate.has_possible)
                    and tracked.removed_at is not null)
            ) as is_inconsistent,
            (
                aggregate.source_key is not null
                and aggregate.has_active
                and not aggregate.has_invalid_active_owner_counter
                and aggregate.minimum_missing=0
                and tracked.status='active'
                and tracked.lifecycle_status='active'
                and tracked.missing_successful_runs=0
                and tracked.missing_successful_runs is not distinct from
                    aggregate.minimum_missing
                and tracked.removed_at is not null
            ) as is_exact_stale_active_removed_at
        from public.tracked_jobs tracked
        left join owner_aggregate aggregate using(source_key)
    )
    select
        count(*) filter (where is_inconsistent),
        count(*) filter (where is_exact_stale_active_removed_at),
        count(*) filter (
            where is_inconsistent and not is_exact_stale_active_removed_at
        )
      into v_inconsistent,v_exact_stale,v_other_inconsistent
    from assessment;

    if v_inconsistent<>81
       or v_exact_stale<>81
       or v_other_inconsistent<>0 then
        raise exception
            'CANONICAL_REMOVED_AT_REMEDIATION_ELIGIBILITY_MISMATCH inconsistent=%, exact_stale=%, other=%',
            v_inconsistent,v_exact_stale,v_other_inconsistent;
    end if;
end
$preflight$;

create table public.canonical_removed_at_remediation_20261015_snapshot (
    remediation_id text not null
        check (remediation_id='20261015_canonical_removed_at'),
    source_key text primary key,
    previous_removed_at timestamptz not null,
    status text not null,
    lifecycle_status text not null,
    missing_successful_runs integer not null,
    first_seen_at timestamptz not null,
    last_seen_at timestamptz not null,
    owner_count bigint not null,
    active_owner_count bigint not null,
    possibly_missing_owner_count bigint not null,
    invalid_active_owner_counter_count bigint not null,
    minimum_owner_missing integer not null,
    captured_at timestamptz not null default transaction_timestamp(),
    captured_by name not null default current_user
);

revoke all on table
    public.canonical_removed_at_remediation_20261015_snapshot
    from public,anon,authenticated,service_role;

insert into public.canonical_removed_at_remediation_20261015_snapshot (
    remediation_id,source_key,previous_removed_at,status,lifecycle_status,
    missing_successful_runs,first_seen_at,last_seen_at,owner_count,
    active_owner_count,possibly_missing_owner_count,
    invalid_active_owner_counter_count,minimum_owner_missing
)
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
select
    '20261015_canonical_removed_at',tracked.source_key,tracked.removed_at,
    tracked.status,tracked.lifecycle_status,tracked.missing_successful_runs,
    tracked.first_seen_at,tracked.last_seen_at,aggregate.owner_count,
    aggregate.active_owner_count,aggregate.possibly_missing_owner_count,
    aggregate.invalid_active_owner_counter_count,aggregate.minimum_missing
from public.tracked_jobs tracked
join owner_aggregate aggregate using(source_key)
where aggregate.active_owner_count>0
  and aggregate.invalid_active_owner_counter_count=0
  and aggregate.minimum_missing=0
  and tracked.status='active'
  and tracked.lifecycle_status='active'
  and tracked.missing_successful_runs=0
  and tracked.missing_successful_runs is not distinct from
      aggregate.minimum_missing
  and tracked.removed_at is not null
order by tracked.source_key;

do $snapshot_guard$
declare
    v_snapshot_rows bigint;
begin
    select count(*) into v_snapshot_rows
    from public.canonical_removed_at_remediation_20261015_snapshot;
    if v_snapshot_rows<>81 then
        raise exception
            'CANONICAL_REMOVED_AT_REMEDIATION_SNAPSHOT_COUNT_MISMATCH count=%',
            v_snapshot_rows;
    end if;
end
$snapshot_guard$;

select
    'pre_remediation' as phase,
    count(*) as snapshot_rows,
    count(*) filter (where previous_removed_at is not null)
        as nonnull_removed_at_rows
from public.canonical_removed_at_remediation_20261015_snapshot;

do $repair$
declare
    v_updated bigint;
begin
    update public.tracked_jobs tracked
       set removed_at=null
    from public.canonical_removed_at_remediation_20261015_snapshot snapshot
    where tracked.source_key=snapshot.source_key
      and tracked.removed_at is not distinct from snapshot.previous_removed_at
      and tracked.status is not distinct from snapshot.status
      and tracked.lifecycle_status is not distinct from snapshot.lifecycle_status
      and tracked.missing_successful_runs is not distinct from
          snapshot.missing_successful_runs
      and tracked.first_seen_at is not distinct from snapshot.first_seen_at
      and tracked.last_seen_at is not distinct from snapshot.last_seen_at;

    get diagnostics v_updated = row_count;
    if v_updated<>81 then
        raise exception
            'CANONICAL_REMOVED_AT_REMEDIATION_UPDATE_COUNT_MISMATCH updated=%',
            v_updated;
    end if;
end
$repair$;

do $postflight$
declare
    v_remaining_drift bigint;
    v_snapshot_mismatch bigint;
begin
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
            bool_or(owner.lifecycle_status='active') as has_active,
            bool_or(owner.lifecycle_status='possibly_missing') as has_possible,
            min(owner.missing_successful_runs) as minimum_missing
        from public.tracked_job_source_scopes owner
        group by owner.source_key
    )
    select count(*)
      into v_remaining_drift
    from public.tracked_jobs tracked
    left join owner_aggregate aggregate using(source_key)
    where aggregate.source_key is null
       or tracked.status is distinct from case
            when aggregate.has_active or aggregate.has_possible
                then 'active' else 'closed' end
       or tracked.lifecycle_status is distinct from case
            when aggregate.has_active then 'active'
            when aggregate.has_possible then 'possibly_missing'
            else 'removed' end
       or tracked.missing_successful_runs is distinct from aggregate.minimum_missing
       or ((aggregate.has_active or aggregate.has_possible)
           and tracked.removed_at is not null);

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
      into v_snapshot_mismatch
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

    if v_remaining_drift<>0 or v_snapshot_mismatch<>0 then
        raise exception
            'CANONICAL_REMOVED_AT_REMEDIATION_POSTFLIGHT_FAILED remaining_drift=%, snapshot_mismatch=%',
            v_remaining_drift,v_snapshot_mismatch;
    end if;
end
$postflight$;

select
    'post_remediation' as phase,
    count(*) as snapshot_rows,
    count(*) filter (where tracked.removed_at is null) as cleared_rows,
    count(*) filter (where tracked.removed_at is not null) as uncleared_rows
from public.canonical_removed_at_remediation_20261015_snapshot snapshot
join public.tracked_jobs tracked using(source_key);

commit;
