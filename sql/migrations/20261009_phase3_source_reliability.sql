-- Phase 3 forward migration: source-local cooldowns and unreadable inventory
-- protection. This migration intentionally follows the immutable 20260924
-- staged-sync migration and is safe to apply to databases that already ran it.

create table if not exists public.job_source_health (
    source_key text primary key,
    company text not null,
    platform text not null,
    consecutive_transient_failures integer not null default 0
        check (consecutive_transient_failures >= 0),
    cooldown_until timestamptz,
    last_failure_classification text,
    last_successful_authoritative_inventory timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists job_source_health_cooldown_idx
    on public.job_source_health (cooldown_until)
    where cooldown_until is not null;

alter table public.job_source_health enable row level security;
revoke all on table public.job_source_health from public, anon, authenticated;
grant all on table public.job_source_health to service_role;

alter table public.job_sync_sessions
    add column if not exists protected_source_keys text[] not null default '{}';

-- PostgreSQL identifies functions by name and argument types. Drop the old
-- overload so callers cannot accidentally bypass protected inventory IDs.
drop function if exists public.begin_student_job_source_sync(
    uuid, uuid, text, integer, integer, bigint, boolean, integer, timestamptz
);

create or replace function public.begin_student_job_source_sync(
    p_sync_id uuid,
    p_scan_id uuid,
    p_company text,
    p_expected_chunks integer,
    p_expected_rows integer,
    p_expected_bytes bigint,
    p_reconcile_absence boolean,
    p_protected_source_keys text[],
    p_close_after_missing_runs integer,
    p_seen_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_session public.job_sync_sessions%rowtype;
    v_prior_result jsonb;
    v_created boolean := false;
begin
    if nullif(btrim(p_company), '') is null then
        raise exception 'p_company must not be empty';
    end if;
    if p_expected_chunks < 0 or p_expected_rows < 0 or p_expected_bytes < 0 then
        raise exception 'expected chunk, row, and byte counts must be nonnegative';
    end if;
    if p_close_after_missing_runs < 1 then
        raise exception 'p_close_after_missing_runs must be at least 1';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_scan_id::text || ':' || p_company, 0));
    select result into v_prior_result
    from public.scan_source_syncs
    where scan_id = p_scan_id and company = p_company;

    insert into public.job_sync_sessions (
        sync_id, scan_id, company, expected_chunks, expected_rows, expected_bytes,
        reconcile_absence, close_after_missing_runs, seen_at, status, result,
        finalized_at, protected_source_keys
    ) values (
        p_sync_id, p_scan_id, p_company, p_expected_chunks, p_expected_rows,
        p_expected_bytes, p_reconcile_absence, p_close_after_missing_runs,
        p_seen_at, case when v_prior_result is null then 'staging' else 'finalized' end,
        v_prior_result, case when v_prior_result is null then null else now() end,
        coalesce(p_protected_source_keys, '{}')
    ) on conflict (sync_id) do nothing
    returning true into v_created;

    select * into v_session
    from public.job_sync_sessions
    where sync_id = p_sync_id
    for update;

    if v_session.scan_id is distinct from p_scan_id
       or v_session.company is distinct from p_company
       or v_session.expected_chunks is distinct from p_expected_chunks
       or v_session.expected_rows is distinct from p_expected_rows
       or v_session.expected_bytes is distinct from p_expected_bytes
       or v_session.reconcile_absence is distinct from p_reconcile_absence
       or v_session.protected_source_keys is distinct from coalesce(p_protected_source_keys, '{}')
       or v_session.close_after_missing_runs is distinct from p_close_after_missing_runs
       or v_session.seen_at is distinct from p_seen_at then
        raise exception 'sync ID was already initialized with different metadata';
    end if;

    return jsonb_build_object(
        'status', v_session.status,
        'result', v_session.result,
        'replayed', not coalesce(v_created, false)
    );
end;
$$;

create or replace function public.finalize_student_job_source_sync(p_sync_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_session public.job_sync_sessions%rowtype;
    v_chunks integer;
    v_rows bigint;
    v_bytes bigint;
    v_stage_rows bigint;
    v_inserted integer := 0;
    v_updated integer := 0;
    v_unchanged integer := 0;
    v_missing_incremented integer := 0;
    v_closed integer := 0;
    v_result jsonb;
begin
    select * into v_session
    from public.job_sync_sessions
    where sync_id = p_sync_id;
    if not found then
        raise exception 'unknown sync ID';
    end if;
    perform pg_advisory_xact_lock(
        hashtextextended(v_session.scan_id::text || ':' || v_session.company, 0)
    );
    select * into v_session
    from public.job_sync_sessions
    where sync_id = p_sync_id
    for update;
    if v_session.status = 'finalized' then
        return v_session.result || jsonb_build_object('replayed', true);
    end if;
    select result into v_result
    from public.scan_source_syncs
    where scan_id = v_session.scan_id and company = v_session.company;
    if found then
        update public.job_sync_sessions
        set status = 'finalized', result = v_result, finalized_at = now(), updated_at = now()
        where sync_id = p_sync_id;
        delete from public.job_sync_chunks where sync_id = p_sync_id;
        delete from public.job_sync_stage where sync_id = p_sync_id;
        return v_result || jsonb_build_object('replayed', true);
    end if;

    select count(*), coalesce(sum(row_count), 0), coalesce(sum(payload_bytes), 0)
    into v_chunks, v_rows, v_bytes
    from public.job_sync_chunks where sync_id = p_sync_id;
    select count(*) into v_stage_rows
    from public.job_sync_stage where sync_id = p_sync_id;
    if v_chunks <> v_session.expected_chunks
       or v_rows <> v_session.expected_rows
       or v_bytes <> v_session.expected_bytes
       or v_stage_rows <> v_session.expected_rows then
        raise exception 'cannot finalize an incomplete staged synchronization';
    end if;

    select
        count(*) filter (where existing.source_key is null),
        count(*) filter (
            where existing.source_key is not null and (
                existing.company is distinct from incoming.company
                or existing.title is distinct from incoming.title
                or existing.relevance_score is distinct from incoming.relevance_score
                or existing.lifecycle_status is distinct from incoming.lifecycle_status
                or existing.freshness_band is distinct from incoming.freshness_band
                or existing.urgency_band is distinct from incoming.urgency_band
                or existing.is_ranked_output is distinct from incoming.is_ranked_output
                or existing.raw_data is distinct from incoming.raw_data
            )
        ),
        count(*) filter (
            where existing.source_key is not null and not (
                existing.company is distinct from incoming.company
                or existing.title is distinct from incoming.title
                or existing.relevance_score is distinct from incoming.relevance_score
                or existing.lifecycle_status is distinct from incoming.lifecycle_status
                or existing.freshness_band is distinct from incoming.freshness_band
                or existing.urgency_band is distinct from incoming.urgency_band
                or existing.is_ranked_output is distinct from incoming.is_ranked_output
                or existing.raw_data is distinct from incoming.raw_data
            )
        )
    into v_inserted, v_updated, v_unchanged
    from public.job_sync_stage incoming
    left join public.tracked_jobs existing using (source_key)
    where incoming.sync_id = p_sync_id;

    insert into public.tracked_jobs (
        source_key, company, title, location, department, apply_url, source_url,
        source_platform, primary_category, matched_categories, relevance_score,
        relevance_band, positive_signals, negative_signals, location_status,
        source_updated_at, source_posted_at, source_closes_at, first_seen_at,
        last_seen_at, first_seen_basis, removed_at, status, lifecycle_status,
        missing_successful_runs, freshness_age_source, age_days,
        first_seen_age_hours, freshness_band, deadline_days_remaining,
        urgency_band, urgency_score, urgency_reasons, date_parse_errors,
        is_ranked_output, raw_data
    )
    select
        source_key, company, title, location, department, apply_url, source_url,
        source_platform, primary_category, matched_categories, relevance_score,
        relevance_band, positive_signals, negative_signals, location_status,
        source_updated_at, source_posted_at, source_closes_at, first_seen_at,
        last_seen_at, first_seen_basis, removed_at, status, lifecycle_status,
        missing_successful_runs, freshness_age_source, age_days,
        first_seen_age_hours, freshness_band, deadline_days_remaining,
        urgency_band, urgency_score, urgency_reasons, date_parse_errors,
        is_ranked_output, raw_data
    from public.job_sync_stage where sync_id = p_sync_id
    on conflict (source_key) do update set
        company = excluded.company,
        title = excluded.title,
        location = excluded.location,
        department = excluded.department,
        apply_url = excluded.apply_url,
        source_url = excluded.source_url,
        source_platform = excluded.source_platform,
        primary_category = excluded.primary_category,
        matched_categories = excluded.matched_categories,
        relevance_score = excluded.relevance_score,
        relevance_band = excluded.relevance_band,
        positive_signals = excluded.positive_signals,
        negative_signals = excluded.negative_signals,
        location_status = excluded.location_status,
        source_updated_at = excluded.source_updated_at,
        source_posted_at = coalesce(excluded.source_posted_at, tracked_jobs.source_posted_at),
        source_closes_at = coalesce(excluded.source_closes_at, tracked_jobs.source_closes_at),
        last_seen_at = excluded.last_seen_at,
        first_seen_basis = tracked_jobs.first_seen_basis,
        removed_at = excluded.removed_at,
        status = excluded.status,
        lifecycle_status = excluded.lifecycle_status,
        missing_successful_runs = excluded.missing_successful_runs,
        freshness_age_source = excluded.freshness_age_source,
        age_days = excluded.age_days,
        first_seen_age_hours = excluded.first_seen_age_hours,
        freshness_band = excluded.freshness_band,
        deadline_days_remaining = excluded.deadline_days_remaining,
        urgency_band = excluded.urgency_band,
        urgency_score = excluded.urgency_score,
        urgency_reasons = excluded.urgency_reasons,
        date_parse_errors = excluded.date_parse_errors,
        is_ranked_output = excluded.is_ranked_output,
        raw_data = excluded.raw_data;

    with missing_updates as (
        update public.tracked_jobs tracked
        set
            missing_successful_runs = tracked.missing_successful_runs + 1,
            status = case
                when tracked.missing_successful_runs + 1 >= v_session.close_after_missing_runs
                    then 'closed' else tracked.status end,
            lifecycle_status = case
                when tracked.missing_successful_runs + 1 >= v_session.close_after_missing_runs
                    then 'removed' else 'possibly_missing' end,
            removed_at = case
                when tracked.missing_successful_runs + 1 >= v_session.close_after_missing_runs
                    then coalesce(tracked.removed_at, v_session.seen_at)
                else tracked.removed_at end,
            urgency_band = case
                when tracked.missing_successful_runs + 1 >= v_session.close_after_missing_runs
                    then 'closed' else tracked.urgency_band end,
            urgency_score = case
                when tracked.missing_successful_runs + 1 >= v_session.close_after_missing_runs
                    then 0 else tracked.urgency_score end,
            urgency_reasons = case
                when tracked.missing_successful_runs + 1 >= v_session.close_after_missing_runs
                    then '["confirmed_removed"]'::jsonb else tracked.urgency_reasons end
        where v_session.reconcile_absence
          and tracked.company = v_session.company
          and tracked.status <> 'closed'
          and not (tracked.source_key = any(v_session.protected_source_keys))
          and not exists (
              select 1 from public.job_sync_stage incoming
              where incoming.sync_id = p_sync_id
                and incoming.source_key = tracked.source_key
          )
        returning status
    )
    select count(*), count(*) filter (where status = 'closed')
    into v_missing_incremented, v_closed
    from missing_updates;

    v_result := jsonb_build_object(
        'inserted', v_inserted, 'updated', v_updated, 'unchanged', v_unchanged,
        'missing_incremented', v_missing_incremented, 'closed', v_closed,
        'replayed', false
    );
    insert into public.scan_source_syncs (scan_id, company, result)
    values (v_session.scan_id, v_session.company, v_result);
    update public.job_sync_sessions
    set status = 'finalized', result = v_result, finalized_at = now(), updated_at = now()
    where sync_id = p_sync_id;
    delete from public.job_sync_chunks where sync_id = p_sync_id;
    delete from public.job_sync_stage where sync_id = p_sync_id;
    return v_result;
end;
$$;

revoke all on function public.begin_student_job_source_sync(
    uuid, uuid, text, integer, integer, bigint, boolean, text[], integer, timestamptz
) from public, anon, authenticated;
grant execute on function public.begin_student_job_source_sync(
    uuid, uuid, text, integer, integer, bigint, boolean, text[], integer, timestamptz
) to service_role;

revoke all on function public.finalize_student_job_source_sync(uuid)
    from public, anon, authenticated;
grant execute on function public.finalize_student_job_source_sync(uuid)
    to service_role;

comment on table public.job_source_health is
    'Source/config-local transient failure streaks and bounded cooldown recovery state.';
comment on column public.job_sync_sessions.protected_source_keys is
    'Authoritative listed identities excluded from absence reconciliation when hydration is unreadable.';
comment on function public.finalize_student_job_source_sync(uuid) is
    'Atomically applies one fully staged company source and reconciles absence exactly once while protecting unreadable listed identities.';
