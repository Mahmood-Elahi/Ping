-- Ping student-job persistence for Supabase/PostgreSQL.
-- Apply from a trusted admin context; never expose the service-role key.

create extension if not exists pgcrypto with schema extensions;

create table if not exists public.job_source_health (
    source_key text primary key,
    company text not null,
    source_scope text not null default 'default',
    platform text not null,
    consecutive_transient_failures integer not null default 0
        check (consecutive_transient_failures >= 0),
    cooldown_until timestamptz,
    last_failure_classification text,
    last_successful_authoritative_inventory timestamptz,
    updated_at timestamptz not null default now()
);
alter table public.job_source_health
    add column if not exists source_scope text not null default 'default';

create index if not exists job_source_health_cooldown_idx
    on public.job_source_health (cooldown_until)
    where cooldown_until is not null;

alter table public.job_source_health enable row level security;
revoke all on table public.job_source_health from public, anon, authenticated;
grant all on table public.job_source_health to service_role;

create table if not exists public.tracked_jobs (
    id uuid primary key default gen_random_uuid(),
    source_key text not null unique,
    company text not null,
    source_scope text not null default 'default',
    title text not null,
    location text,
    department text,
    apply_url text,
    source_url text,
    source_platform text not null,
    primary_category text,
    matched_categories jsonb not null default '[]'::jsonb,
    relevance_score integer not null default 0 check (relevance_score between 0 and 100),
    relevance_band text,
    positive_signals jsonb not null default '[]'::jsonb,
    negative_signals jsonb not null default '[]'::jsonb,
    location_status text,
    source_updated_at timestamptz,
    source_posted_at text,
    source_closes_at text,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    first_seen_basis text not null default 'observed_at_scan',
    removed_at timestamptz,
    status text not null default 'active' check (status in ('active', 'closed')),
    lifecycle_status text not null default 'active'
        check (lifecycle_status in ('active', 'possibly_missing', 'removed')),
    missing_successful_runs integer not null default 0 check (missing_successful_runs >= 0),
    freshness_age_source text,
    age_days double precision check (age_days is null or age_days >= 0),
    first_seen_age_hours double precision check (
        first_seen_age_hours is null or first_seen_age_hours >= 0
    ),
    freshness_band text check (
        freshness_band is null or freshness_band in ('new', 'fresh', 'normal', 'aging', 'stale')
    ),
    deadline_days_remaining double precision,
    urgency_band text check (
        urgency_band is null or urgency_band in ('apply_now', 'high', 'normal', 'low', 'closed')
    ),
    urgency_score integer not null default 0 check (urgency_score between 0 and 100),
    urgency_reasons jsonb not null default '[]'::jsonb,
    date_parse_errors jsonb not null default '[]'::jsonb,
    is_ranked_output boolean not null default true,
    raw_data jsonb not null default '{}'::jsonb,
    check (jsonb_typeof(matched_categories) = 'array'),
    check (jsonb_typeof(positive_signals) = 'array'),
    check (jsonb_typeof(negative_signals) = 'array'),
    check (jsonb_typeof(urgency_reasons) = 'array'),
    check (jsonb_typeof(date_parse_errors) = 'array'),
    check (jsonb_typeof(raw_data) = 'object')
);
alter table public.tracked_jobs
    add column if not exists source_scope text not null default 'default';

create table if not exists public.tracked_job_source_scopes (
    ownership_key text primary key,
    source_key text not null references public.tracked_jobs(source_key) on delete cascade,
    canonical_company text not null,
    source_scope text not null default 'default',
    source_scope_key text not null,
    provider_source_key text not null,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    missing_successful_runs integer not null default 0 check (missing_successful_runs >= 0),
    lifecycle_status text not null default 'active'
        check (lifecycle_status in ('active', 'possibly_missing', 'removed')),
    removed_at timestamptz,
    unique (source_key, source_scope_key)
);

create table if not exists public.source_scope_retirements (
    source_scope_key text primary key,
    canonical_company text not null,
    source_scope text not null,
    reason text not null check (length(btrim(reason)) >= 8),
    approved_by text not null check (length(btrim(approved_by)) >= 2),
    retired_at timestamptz not null default now()
);

create index if not exists tracked_job_source_scopes_scope_idx
    on public.tracked_job_source_scopes (source_scope_key, lifecycle_status);
create index if not exists tracked_job_source_scopes_source_idx
    on public.tracked_job_source_scopes (source_key);
create index if not exists tracked_job_source_scopes_scope_source_idx
    on public.tracked_job_source_scopes (source_scope_key, source_key);

alter table public.tracked_job_source_scopes enable row level security;
revoke all on table public.tracked_job_source_scopes from public, anon, authenticated;
grant all on table public.tracked_job_source_scopes to service_role;
alter table public.source_scope_retirements enable row level security;
revoke all on table public.source_scope_retirements from public, anon, authenticated;
grant all on table public.source_scope_retirements to service_role;

-- Phase 6 bounded optimization state. This is checkpoint state, never source
-- truth, and no adapter is activated merely by creating the table.
create table if not exists public.source_incremental_checkpoints (
    source_scope_key text not null,
    provider_identity text not null,
    config_fingerprint text not null,
    strategy_version text not null,
    cursor text,
    timestamp_watermark timestamptz,
    last_successful_full_at timestamptz,
    last_successful_incremental_at timestamptz,
    last_forced_full_reason text,
    representation_token text,
    snapshot_checksum text,
    ambiguous_since_last_full boolean not null default false,
    updated_at timestamptz not null default now(),
    primary key (source_scope_key, provider_identity),
    check (length(source_scope_key) between 4 and 512),
    check (length(provider_identity) between 1 and 256),
    check (length(config_fingerprint) between 1 and 128),
    check (length(strategy_version) between 1 and 64),
    check (cursor is null or length(cursor) <= 2048),
    check (representation_token is null or length(representation_token) <= 2048),
    check (snapshot_checksum is null or snapshot_checksum ~ '^[0-9a-f]{64}$')
);
alter table public.source_incremental_checkpoints enable row level security;
revoke all on table public.source_incremental_checkpoints from public, anon, authenticated;
grant all on table public.source_incremental_checkpoints to service_role;

-- Idempotent Freshness/Urgency v1 migration for an existing tracked_jobs table.
alter table public.tracked_jobs add column if not exists source_posted_at text;
alter table public.tracked_jobs add column if not exists source_closes_at text;
alter table public.tracked_jobs add column if not exists first_seen_basis text not null default 'observed_at_scan';
alter table public.tracked_jobs add column if not exists removed_at timestamptz;
alter table public.tracked_jobs add column if not exists lifecycle_status text not null default 'active';
alter table public.tracked_jobs add column if not exists freshness_age_source text;
alter table public.tracked_jobs add column if not exists age_days double precision;
alter table public.tracked_jobs add column if not exists first_seen_age_hours double precision;
alter table public.tracked_jobs add column if not exists freshness_band text;
alter table public.tracked_jobs add column if not exists deadline_days_remaining double precision;
alter table public.tracked_jobs add column if not exists urgency_band text;
alter table public.tracked_jobs add column if not exists urgency_score integer not null default 0;
alter table public.tracked_jobs add column if not exists urgency_reasons jsonb not null default '[]'::jsonb;
alter table public.tracked_jobs add column if not exists date_parse_errors jsonb not null default '[]'::jsonb;
alter table public.tracked_jobs add column if not exists is_ranked_output boolean not null default true;

-- Preserve the meaning of legacy lifecycle evidence. Existing production rows
-- already used status + missing_successful_runs before lifecycle_status existed.
-- Historical removal time is unknowable, so removed_at deliberately remains null.
update public.tracked_jobs
set lifecycle_status = case
    when status = 'closed' then 'removed'
    when missing_successful_runs > 0 then 'possibly_missing'
    else 'active'
end
where lifecycle_status is distinct from case
    when status = 'closed' then 'removed'
    when missing_successful_runs > 0 then 'possibly_missing'
    else 'active'
end;

-- ADD COLUMN does not inherit the checks from CREATE TABLE when migrating an
-- existing table. Add named constraints idempotently, then validate them.
do $migration$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_lifecycle_status_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_lifecycle_status_check
            check (lifecycle_status in ('active', 'possibly_missing', 'removed')) not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_age_days_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_age_days_check
            check (age_days is null or age_days >= 0) not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_first_seen_age_hours_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_first_seen_age_hours_check
            check (first_seen_age_hours is null or first_seen_age_hours >= 0) not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_freshness_band_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_freshness_band_check
            check (
                freshness_band is null
                or freshness_band in ('new', 'fresh', 'normal', 'aging', 'stale')
            ) not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_urgency_band_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_urgency_band_check
            check (
                urgency_band is null
                or urgency_band in ('apply_now', 'high', 'normal', 'low', 'closed')
            ) not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_urgency_score_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_urgency_score_check
            check (urgency_score between 0 and 100) not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_urgency_reasons_array_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_urgency_reasons_array_check
            check (jsonb_typeof(urgency_reasons) = 'array') not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.tracked_jobs'::regclass
          and conname = 'tracked_jobs_date_parse_errors_array_check'
    ) then
        alter table public.tracked_jobs add constraint tracked_jobs_date_parse_errors_array_check
            check (jsonb_typeof(date_parse_errors) = 'array') not valid;
    end if;
end
$migration$;

alter table public.tracked_jobs validate constraint tracked_jobs_lifecycle_status_check;
alter table public.tracked_jobs validate constraint tracked_jobs_age_days_check;
alter table public.tracked_jobs validate constraint tracked_jobs_first_seen_age_hours_check;
alter table public.tracked_jobs validate constraint tracked_jobs_freshness_band_check;
alter table public.tracked_jobs validate constraint tracked_jobs_urgency_band_check;
alter table public.tracked_jobs validate constraint tracked_jobs_urgency_score_check;
alter table public.tracked_jobs validate constraint tracked_jobs_urgency_reasons_array_check;
alter table public.tracked_jobs validate constraint tracked_jobs_date_parse_errors_array_check;

create table if not exists public.scan_runs (
    id uuid primary key default gen_random_uuid(),
    started_at timestamptz not null,
    completed_at timestamptz not null,
    success boolean not null,
    companies_checked integer not null default 0 check (companies_checked >= 0),
    jobs_fetched integer not null default 0 check (jobs_fetched >= 0),
    jobs_retained integer not null default 0 check (jobs_retained >= 0),
    jobs_inserted integer not null default 0 check (jobs_inserted >= 0),
    jobs_updated integer not null default 0 check (jobs_updated >= 0),
    jobs_unchanged integer not null default 0 check (jobs_unchanged >= 0),
    exclusion_counts jsonb not null default '{}'::jsonb,
    coverage_report_path text,
    ranked_artifact_path text,
    error_message text,
    failure_stage text,
    diagnostics jsonb not null default '{}'::jsonb,
    check (jsonb_typeof(exclusion_counts) = 'object'),
    check (jsonb_typeof(diagnostics) = 'object')
);

alter table public.scan_runs add column if not exists failure_stage text;
alter table public.scan_runs add column if not exists diagnostics jsonb not null default '{}'::jsonb;

do $migration$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.scan_runs'::regclass
          and conname = 'scan_runs_diagnostics_object_check'
    ) then
        alter table public.scan_runs add constraint scan_runs_diagnostics_object_check
            check (jsonb_typeof(diagnostics) = 'object') not valid;
    end if;
end
$migration$;
alter table public.scan_runs validate constraint scan_runs_diagnostics_object_check;

create table if not exists public.scan_source_syncs (
    scan_id uuid not null,
    company text not null,
    result jsonb not null,
    completed_at timestamptz not null default now(),
    primary key (scan_id, company),
    check (jsonb_typeof(result) = 'object')
);

create table if not exists public.applications (
    job_id uuid primary key references public.tracked_jobs(id) on delete restrict,
    application_status text not null,
    applied_at timestamptz,
    resume_version text,
    follow_up_at timestamptz,
    notes text,
    updated_at timestamptz not null default now()
);

create index if not exists tracked_jobs_status_idx on public.tracked_jobs (status);
create index if not exists tracked_jobs_company_idx on public.tracked_jobs (company);
create index if not exists tracked_jobs_relevance_score_idx
    on public.tracked_jobs (relevance_score desc);
create index if not exists tracked_jobs_first_seen_at_idx
    on public.tracked_jobs (first_seen_at desc);
create index if not exists tracked_jobs_primary_category_idx
    on public.tracked_jobs (primary_category);
create index if not exists tracked_jobs_urgency_idx
    on public.tracked_jobs (urgency_score desc, first_seen_at desc);

alter table public.tracked_jobs enable row level security;
alter table public.scan_runs enable row level security;
alter table public.scan_source_syncs enable row level security;
alter table public.applications enable row level security;

revoke all on table public.tracked_jobs from anon, authenticated;
revoke all on table public.scan_runs from anon, authenticated;
revoke all on table public.scan_source_syncs from anon, authenticated;
revoke all on table public.applications from anon, authenticated;
grant all on table public.tracked_jobs to service_role;
grant all on table public.scan_runs to service_role;
grant all on table public.scan_source_syncs to service_role;
grant all on table public.applications to service_role;

-- Synchronize one canonical source in one transaction. Source-scoping keeps the
-- request beneath measured production statement/gateway limits while preserving
-- atomic observation plus absence reconciliation for that source. The ledger
-- makes response-loss retries idempotent, including missing-evidence increments.
create or replace function public.sync_student_job_source(
    p_scan_id uuid,
    p_company text,
    p_jobs jsonb,
    p_reconcile_absence boolean,
    p_close_after_missing_runs integer,
    p_seen_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_inserted integer := 0;
    v_updated integer := 0;
    v_unchanged integer := 0;
    v_missing_incremented integer := 0;
    v_closed integer := 0;
    v_result jsonb;
begin
    if p_close_after_missing_runs < 1 then
        raise exception 'p_close_after_missing_runs must be at least 1';
    end if;
    if nullif(btrim(p_company), '') is null then
        raise exception 'p_company must not be empty';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_scan_id::text || ':' || p_company, 0));
    select result into v_result
    from public.scan_source_syncs
    where scan_id = p_scan_id and company = p_company;
    if found then
        return v_result || jsonb_build_object('replayed', true);
    end if;

    create temporary table sync_student_jobs_incoming (
        source_key text primary key,
        company text not null,
        title text not null,
        location text,
        department text,
        apply_url text,
        source_url text,
        source_platform text not null,
        primary_category text,
        matched_categories jsonb not null,
        relevance_score integer not null,
        relevance_band text,
        positive_signals jsonb not null,
        negative_signals jsonb not null,
        location_status text,
        source_updated_at timestamptz,
        source_posted_at text,
        source_closes_at text,
        first_seen_at timestamptz not null,
        last_seen_at timestamptz not null,
        first_seen_basis text not null,
        removed_at timestamptz,
        status text not null,
        lifecycle_status text not null,
        missing_successful_runs integer not null,
        freshness_age_source text,
        age_days double precision,
        first_seen_age_hours double precision,
        freshness_band text,
        deadline_days_remaining double precision,
        urgency_band text,
        urgency_score integer not null,
        urgency_reasons jsonb not null,
        date_parse_errors jsonb not null,
        is_ranked_output boolean not null,
        raw_data jsonb not null
    ) on commit drop;

    insert into sync_student_jobs_incoming
    select
        item.source_key, item.company, item.title, item.location, item.department,
        item.apply_url, item.source_url, item.source_platform, item.primary_category,
        coalesce(item.matched_categories, '[]'::jsonb), item.relevance_score,
        item.relevance_band, coalesce(item.positive_signals, '[]'::jsonb),
        coalesce(item.negative_signals, '[]'::jsonb), item.location_status,
        item.source_updated_at, item.source_posted_at, item.source_closes_at,
        item.first_seen_at, item.last_seen_at,
        coalesce(item.first_seen_basis, 'observed_at_scan'), item.removed_at,
        item.status, item.lifecycle_status, item.missing_successful_runs,
        item.freshness_age_source, item.age_days, item.first_seen_age_hours,
        item.freshness_band, item.deadline_days_remaining, item.urgency_band,
        item.urgency_score, coalesce(item.urgency_reasons, '[]'::jsonb),
        coalesce(item.date_parse_errors, '[]'::jsonb), item.is_ranked_output,
        coalesce(item.raw_data, '{}'::jsonb)
    from jsonb_to_recordset(coalesce(p_jobs, '[]'::jsonb)) as item (
        source_key text, company text, title text, location text, department text,
        apply_url text, source_url text, source_platform text, primary_category text,
        matched_categories jsonb, relevance_score integer, relevance_band text,
        positive_signals jsonb, negative_signals jsonb, location_status text,
        source_updated_at timestamptz, source_posted_at text, source_closes_at text,
        first_seen_at timestamptz, last_seen_at timestamptz, first_seen_basis text,
        removed_at timestamptz, status text, lifecycle_status text,
        missing_successful_runs integer, freshness_age_source text,
        age_days double precision, first_seen_age_hours double precision,
        freshness_band text, deadline_days_remaining double precision,
        urgency_band text, urgency_score integer, urgency_reasons jsonb,
        date_parse_errors jsonb, is_ranked_output boolean, raw_data jsonb
    );

    if exists (
        select 1 from sync_student_jobs_incoming where company is distinct from p_company
    ) then
        raise exception 'incoming jobs must all belong to p_company';
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
    from sync_student_jobs_incoming incoming
    left join public.tracked_jobs existing using (source_key);

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
    select * from sync_student_jobs_incoming
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
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then 'closed' else tracked.status end,
            lifecycle_status = case
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then 'removed' else 'possibly_missing' end,
            removed_at = case
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then coalesce(tracked.removed_at, p_seen_at) else tracked.removed_at end,
            urgency_band = case
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then 'closed' else tracked.urgency_band end,
            urgency_score = case
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then 0 else tracked.urgency_score end,
            urgency_reasons = case
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then '["confirmed_removed"]'::jsonb else tracked.urgency_reasons end
        where p_reconcile_absence
          and tracked.company = p_company
          and tracked.status <> 'closed'
          and not exists (
              select 1 from sync_student_jobs_incoming incoming
              where incoming.source_key = tracked.source_key
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
    values (p_scan_id, p_company, v_result);

    return v_result;
end;
$$;

revoke all on function public.sync_student_job_source(
    uuid, text, jsonb, boolean, integer, timestamptz
) from public, anon, authenticated;
grant execute on function public.sync_student_job_source(
    uuid, text, jsonb, boolean, integer, timestamptz
) to service_role;

drop function if exists public.sync_student_job_scan(
    jsonb, text[], integer, timestamptz, jsonb
);

comment on table public.applications is
    'Manually controlled application tracking; never modified by job refreshes.';
comment on table public.job_source_health is
    'Source/config-local transient failure streaks and bounded cooldown recovery state.';
comment on function public.sync_student_job_source(uuid, text, jsonb, boolean, integer, timestamptz) is
    'Atomically synchronizes one canonical source and idempotently reconciles absence for complete scans.';

-- The direct JSON-array RPC above is retained only so this historical base
-- schema can upgrade older installations in place. It is removed immediately;
-- apply sql/migrations/20260924_staged_source_sync.sql next to install the
-- bounded durable-staging RPCs used by the application, then apply the later
-- chronological migrations through
-- 20261015_optimize_source_sync_finalizer.sql. The final migrations keep the
-- SECURITY DEFINER finalizer's fixed search path able to resolve pgcrypto and
-- bound canonical lifecycle aggregation to the current sync/scope's keys.
drop function if exists public.sync_student_job_source(
    uuid, text, jsonb, boolean, integer, timestamptz
);

-- Bootstrap parity for an existing database where the chronological staged
-- sync migrations have already installed the finalizer. On a clean bootstrap
-- the function is installed and repaired by the forward migrations above.
do $bootstrap$
begin
    if to_regprocedure(
        'public.finalize_student_job_source_sync(uuid)'
    ) is not null then
        execute $finalizer$
create or replace function public.finalize_student_job_source_sync(p_sync_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
    v_session public.job_sync_sessions%rowtype;
    v_chunks integer; v_rows bigint; v_bytes bigint; v_stage_rows bigint;
    v_inserted integer := 0; v_updated integer := 0; v_unchanged integer := 0;
    v_missing_incremented integer := 0; v_closed integer := 0; v_result jsonb;
    v_canonical_updated integer := 0;
begin
    select * into v_session from public.job_sync_sessions where sync_id = p_sync_id;
    if not found then raise exception 'unknown sync ID'; end if;

    -- Use the existing retirement mutex identity so finalization and explicit
    -- retirement cannot mutate the same logical source scope concurrently.
    -- Unrelated scopes remain independent.
    perform pg_advisory_xact_lock(hashtextextended(
        'retire:' || v_session.source_scope_key, 0
    ));
    select * into v_session from public.job_sync_sessions
    where sync_id = p_sync_id for update;
    if v_session.status = 'finalized' then
        return v_session.result || jsonb_build_object('replayed', true);
    end if;
    select result into v_result from public.scan_source_syncs
    where scan_id = v_session.scan_id and company = v_session.company
      and source_scope = v_session.source_scope;
    if found then
        update public.job_sync_sessions set status='finalized', result=v_result,
            finalized_at=now(), updated_at=now() where sync_id=p_sync_id;
        delete from public.job_sync_chunks where sync_id=p_sync_id;
        delete from public.job_sync_stage where sync_id=p_sync_id;
        return v_result || jsonb_build_object('replayed', true);
    end if;
    if exists (
        select 1 from public.source_scope_retirements retirement
        where retirement.source_scope_key=v_session.source_scope_key
    ) then
        raise exception 'cannot finalize a retired source scope';
    end if;
    if exists (
        select 1 from public.job_sync_sessions newer
        where newer.source_scope_key=v_session.source_scope_key
          and newer.status='finalized'
          and newer.sync_id<>p_sync_id
          and row(newer.seen_at,newer.created_at) >
              row(v_session.seen_at,v_session.created_at)
    ) then
        raise exception 'cannot finalize a source scope after a newer observation';
    end if;
    select count(*), coalesce(sum(row_count),0), coalesce(sum(payload_bytes),0)
      into v_chunks,v_rows,v_bytes from public.job_sync_chunks where sync_id=p_sync_id;
    select count(*) into v_stage_rows from public.job_sync_stage where sync_id=p_sync_id;
    if v_chunks<>v_session.expected_chunks or v_rows<>v_session.expected_rows
       or v_bytes<>v_session.expected_bytes or v_stage_rows<>v_session.expected_rows then
        raise exception 'cannot finalize an incomplete staged synchronization';
    end if;

    select count(*) filter(where existing.source_key is null),
           count(*) filter(where existing.source_key is not null), 0
      into v_inserted,v_updated,v_unchanged
    from public.job_sync_stage incoming
    left join public.tracked_jobs existing using(source_key)
    where incoming.sync_id=p_sync_id;

    -- Compare wide/material values once. Only rows with a real material change
    -- assign those columns; the following heartbeat update sees their refreshed
    -- last_seen_at and therefore touches only materially stable observations.
    update public.tracked_jobs tracked set
        company=incoming.company, source_scope=incoming.source_scope,
        title=incoming.title, location=incoming.location, department=incoming.department,
        apply_url=incoming.apply_url, source_url=incoming.source_url,
        source_platform=incoming.source_platform, primary_category=incoming.primary_category,
        matched_categories=incoming.matched_categories,
        relevance_score=incoming.relevance_score, relevance_band=incoming.relevance_band,
        positive_signals=incoming.positive_signals, negative_signals=incoming.negative_signals,
        location_status=incoming.location_status, source_updated_at=incoming.source_updated_at,
        source_posted_at=coalesce(incoming.source_posted_at,tracked.source_posted_at),
        source_closes_at=coalesce(incoming.source_closes_at,tracked.source_closes_at),
        last_seen_at=incoming.last_seen_at, is_ranked_output=incoming.is_ranked_output,
        raw_data=incoming.raw_data
    from public.job_sync_stage incoming
    where incoming.sync_id=p_sync_id
      and tracked.source_key=incoming.source_key
      and row(
            tracked.company, tracked.source_scope, tracked.title,
            tracked.location, tracked.department, tracked.apply_url,
            tracked.source_url, tracked.source_platform,
            tracked.primary_category, tracked.matched_categories,
            tracked.relevance_score, tracked.relevance_band,
            tracked.positive_signals, tracked.negative_signals,
            tracked.location_status, tracked.source_updated_at,
            tracked.source_posted_at, tracked.source_closes_at,
            tracked.is_ranked_output, tracked.raw_data
          ) is distinct from row(
            incoming.company, incoming.source_scope, incoming.title,
            incoming.location, incoming.department, incoming.apply_url,
            incoming.source_url, incoming.source_platform,
            incoming.primary_category, incoming.matched_categories,
            incoming.relevance_score, incoming.relevance_band,
            incoming.positive_signals, incoming.negative_signals,
            incoming.location_status, incoming.source_updated_at,
            coalesce(incoming.source_posted_at,tracked.source_posted_at),
            coalesce(incoming.source_closes_at,tracked.source_closes_at),
            incoming.is_ranked_output, incoming.raw_data
          );

    -- last_seen_at participates in tracked_jobs_retention_removed_idx, so this
    -- is not generally a HOT update. It still avoids assigning wide JSONB and
    -- other unchanged material values. Although the comparison may detoast
    -- JSONB, the measured production gain must come from the controlled rollout,
    -- not from an inferred HOT benefit.
    update public.tracked_jobs tracked set
        last_seen_at=incoming.last_seen_at
    from public.job_sync_stage incoming
    where incoming.sync_id=p_sync_id
      and tracked.source_key=incoming.source_key
      and tracked.last_seen_at is distinct from incoming.last_seen_at;

    insert into public.tracked_jobs (
        source_key, company, source_scope, title, location, department, apply_url,
        source_url, source_platform, primary_category, matched_categories,
        relevance_score, relevance_band, positive_signals, negative_signals,
        location_status, source_updated_at, source_posted_at, source_closes_at,
        first_seen_at, last_seen_at, first_seen_basis, removed_at, status,
        lifecycle_status, missing_successful_runs, freshness_age_source, age_days,
        first_seen_age_hours, freshness_band, deadline_days_remaining,
        urgency_band, urgency_score, urgency_reasons, date_parse_errors,
        is_ranked_output, raw_data
    ) select source_key,company,source_scope,title,location,department,apply_url,
        source_url,source_platform,primary_category,matched_categories,
        relevance_score,relevance_band,positive_signals,negative_signals,
        location_status,source_updated_at,source_posted_at,source_closes_at,
        first_seen_at,last_seen_at,first_seen_basis,removed_at,status,
        lifecycle_status,missing_successful_runs,freshness_age_source,age_days,
        first_seen_age_hours,freshness_band,deadline_days_remaining,urgency_band,
        urgency_score,urgency_reasons,date_parse_errors,is_ranked_output,raw_data
    from public.job_sync_stage incoming
    where incoming.sync_id=p_sync_id
      and not exists (
          select 1 from public.tracked_jobs existing
          where existing.source_key=incoming.source_key
      )
    on conflict(source_key) do update set
        company=excluded.company, source_scope=excluded.source_scope,
        title=excluded.title, location=excluded.location, department=excluded.department,
        apply_url=excluded.apply_url, source_url=excluded.source_url,
        source_platform=excluded.source_platform, primary_category=excluded.primary_category,
        matched_categories=excluded.matched_categories,
        relevance_score=excluded.relevance_score, relevance_band=excluded.relevance_band,
        positive_signals=excluded.positive_signals, negative_signals=excluded.negative_signals,
        location_status=excluded.location_status, source_updated_at=excluded.source_updated_at,
        source_posted_at=coalesce(excluded.source_posted_at,tracked_jobs.source_posted_at),
        source_closes_at=coalesce(excluded.source_closes_at,tracked_jobs.source_closes_at),
        last_seen_at=excluded.last_seen_at, is_ranked_output=excluded.is_ranked_output,
        raw_data=excluded.raw_data;

    update public.tracked_job_source_scopes owner set
        provider_source_key=incoming.provider_source_key,
        last_seen_at=incoming.last_seen_at,
        missing_successful_runs=0,
        lifecycle_status=incoming.lifecycle_status,
        removed_at=incoming.removed_at
    from public.job_sync_stage incoming
    where incoming.sync_id=p_sync_id
      and owner.source_key=incoming.source_key
      and owner.source_scope_key=incoming.source_scope_key
      and row(
            owner.provider_source_key,
            owner.missing_successful_runs,
            owner.lifecycle_status,
            owner.removed_at
          ) is distinct from row(
            incoming.provider_source_key,
            0,
            incoming.lifecycle_status,
            incoming.removed_at
          );

    update public.tracked_job_source_scopes owner set
        last_seen_at=incoming.last_seen_at
    from public.job_sync_stage incoming
    where incoming.sync_id=p_sync_id
      and owner.source_key=incoming.source_key
      and owner.source_scope_key=incoming.source_scope_key
      and owner.last_seen_at is distinct from incoming.last_seen_at;

    insert into public.tracked_job_source_scopes (
        ownership_key,source_key,canonical_company,source_scope,source_scope_key,
        provider_source_key,first_seen_at,last_seen_at,missing_successful_runs,
        lifecycle_status,removed_at
    ) select encode(digest(source_key || E'\x1f' || source_scope_key,'sha256'),'hex'),
        source_key,company,source_scope,source_scope_key,provider_source_key,
        first_seen_at,last_seen_at,missing_successful_runs,lifecycle_status,removed_at
    from public.job_sync_stage incoming
    where incoming.sync_id=p_sync_id
      and not exists (
          select 1
          from public.tracked_job_source_scopes existing
          where existing.source_key=incoming.source_key
            and existing.source_scope_key=incoming.source_scope_key
      )
    on conflict(source_key,source_scope_key) do update set
        provider_source_key=excluded.provider_source_key,
        last_seen_at=excluded.last_seen_at, missing_successful_runs=0,
        lifecycle_status=excluded.lifecycle_status, removed_at=excluded.removed_at;

    -- Reconcile only canonical keys observed by this sync. This bounded guard
    -- repairs pre-existing drift for touched jobs without scanning the entire
    -- canonical table, and the DISTINCT predicate prevents no-op writes.
    with affected as (
        select incoming.source_key
        from public.job_sync_stage incoming
        where incoming.sync_id=p_sync_id
    ), aggregate as (
        select owner.source_key,
            bool_or(owner.lifecycle_status='active') as has_active,
            bool_or(owner.lifecycle_status='possibly_missing') as has_possible,
            min(owner.missing_successful_runs) as minimum_missing
        from affected
        join public.tracked_job_source_scopes owner using(source_key)
        group by owner.source_key
    ), canonical_updates as (
        update public.tracked_jobs tracked set
            status=case when aggregate.has_active or aggregate.has_possible
                        then 'active' else 'closed' end,
            lifecycle_status=case when aggregate.has_active then 'active'
                        when aggregate.has_possible then 'possibly_missing' else 'removed' end,
            missing_successful_runs=aggregate.minimum_missing,
            removed_at=case when aggregate.has_active or aggregate.has_possible then null
                            else coalesce(tracked.removed_at,v_session.seen_at) end
        from aggregate
        where tracked.source_key=aggregate.source_key
          and row(
                tracked.status,
                tracked.lifecycle_status,
                tracked.missing_successful_runs,
                tracked.removed_at
              ) is distinct from row(
                case when aggregate.has_active or aggregate.has_possible
                     then 'active' else 'closed' end,
                case when aggregate.has_active then 'active'
                     when aggregate.has_possible then 'possibly_missing' else 'removed' end,
                aggregate.minimum_missing,
                case when aggregate.has_active or aggregate.has_possible then null
                     else coalesce(tracked.removed_at,v_session.seen_at) end
              )
        returning tracked.source_key
    )
    select count(*) into v_canonical_updated from canonical_updates;

    with missing as (
        update public.tracked_job_source_scopes owner set
            missing_successful_runs=owner.missing_successful_runs+1,
            lifecycle_status=case when owner.missing_successful_runs+1 >= v_session.close_after_missing_runs
                then 'removed' else 'possibly_missing' end,
            removed_at=case when owner.missing_successful_runs+1 >= v_session.close_after_missing_runs
                then coalesce(owner.removed_at,v_session.seen_at) else owner.removed_at end
        where v_session.reconcile_absence
          and owner.source_scope_key=v_session.source_scope_key
          and owner.lifecycle_status<>'removed'
          and not(owner.provider_source_key=any(v_session.protected_source_keys))
          and not exists(select 1 from public.job_sync_stage incoming
              where incoming.sync_id=p_sync_id and incoming.source_key=owner.source_key)
        returning owner.ownership_key,owner.source_key,
                  owner.missing_successful_runs,owner.lifecycle_status,owner.removed_at
    ), affected as (
        select distinct missing.source_key from missing
    ), aggregate as (
        select owner.source_key,
            bool_or((case when missing.ownership_key is not null
                then missing.lifecycle_status else owner.lifecycle_status end)='active')
                as has_active,
            bool_or((case when missing.ownership_key is not null
                then missing.lifecycle_status else owner.lifecycle_status end)='possibly_missing')
                as has_possible,
            min(case when missing.ownership_key is not null
                then missing.missing_successful_runs
                else owner.missing_successful_runs end) as minimum_missing
        from affected
        join public.tracked_job_source_scopes owner using(source_key)
        left join missing using(ownership_key)
        group by owner.source_key
    ), canonical_updates as (
        update public.tracked_jobs tracked set
            status=case when aggregate.has_active then 'active'
                        when aggregate.has_possible then 'active' else 'closed' end,
            lifecycle_status=case when aggregate.has_active then 'active'
                        when aggregate.has_possible then 'possibly_missing' else 'removed' end,
            missing_successful_runs=aggregate.minimum_missing,
            removed_at=case when aggregate.has_active or aggregate.has_possible then null
                            else coalesce(tracked.removed_at,v_session.seen_at) end
        from aggregate
        where tracked.source_key=aggregate.source_key
          and row(
                tracked.status,
                tracked.lifecycle_status,
                tracked.missing_successful_runs,
                tracked.removed_at
              ) is distinct from row(
                case when aggregate.has_active then 'active'
                     when aggregate.has_possible then 'active' else 'closed' end,
                case when aggregate.has_active then 'active'
                     when aggregate.has_possible then 'possibly_missing' else 'removed' end,
                aggregate.minimum_missing,
                case when aggregate.has_active or aggregate.has_possible then null
                     else coalesce(tracked.removed_at,v_session.seen_at) end
              )
        returning tracked.source_key
    )
    select (select count(*) from missing),
           v_canonical_updated+(select count(*) from canonical_updates)
      into v_missing_incremented,v_canonical_updated;

    select count(*) into v_closed from public.tracked_jobs tracked
    where tracked.status='closed' and tracked.source_key in (
        select source_key from public.tracked_job_source_scopes
        where source_scope_key=v_session.source_scope_key
    );
    v_result:=jsonb_build_object('inserted',v_inserted,'updated',v_updated,
        'unchanged',v_unchanged,'missing_incremented',v_missing_incremented,
        'closed',v_closed,'replayed',false);
    insert into public.scan_source_syncs(scan_id,company,source_scope,result)
    values(v_session.scan_id,v_session.company,v_session.source_scope,v_result);
    update public.job_sync_sessions set status='finalized',result=v_result,
        finalized_at=now(),updated_at=now() where sync_id=p_sync_id;
    delete from public.job_sync_chunks where sync_id=p_sync_id;
    delete from public.job_sync_stage where sync_id=p_sync_id;
    return v_result;
end;
$$;
$finalizer$;
        execute 'revoke all on function public.finalize_student_job_source_sync(uuid) '
            'from public, anon, authenticated';
        execute 'grant execute on function public.finalize_student_job_source_sync(uuid) '
            'to service_role';
    end if;
end
$bootstrap$;

-- Administrative-only source retirement is deliberately separate from scan
-- reconciliation. Installations upgraded from Phase 3 receive the identical
-- contract from 20261010_phase5_multi_scope_lifecycle.sql.
create or replace function public.retire_student_job_source_scope(
    p_company text, p_source_scope text, p_source_scope_key text,
    p_reason text, p_approved_by text, p_retired_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_owned integer := 0; v_retired integer := 0; v_closed integer := 0;
begin
    if nullif(btrim(p_company), '') is null
       or nullif(btrim(p_source_scope), '') is null
       or nullif(btrim(p_source_scope_key), '') is null
       or length(btrim(coalesce(p_reason, ''))) < 8
       or length(btrim(coalesce(p_approved_by, ''))) < 2
       or p_retired_at is null then
        raise exception 'source retirement requires company, scope, exact key, reason, approver, and timestamp';
    end if;
    if p_source_scope !~ '^[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?(?::[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?)?$'
       or p_source_scope_key is distinct from
          lower(regexp_replace(btrim(p_company), '\s+', ' ', 'g')) || '::' || p_source_scope then
        raise exception 'invalid source scope identity';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('retire:' || p_source_scope_key, 0));
    insert into public.source_scope_retirements (
        source_scope_key, canonical_company, source_scope, reason, approved_by, retired_at
    ) values (
        p_source_scope_key, p_company, p_source_scope, btrim(p_reason),
        btrim(p_approved_by), p_retired_at
    ) on conflict (source_scope_key) do nothing;
    select count(*) into v_owned from public.tracked_job_source_scopes
        where source_scope_key = p_source_scope_key;
    with retired as (
        update public.tracked_job_source_scopes set lifecycle_status='removed',
            removed_at=coalesce(removed_at,p_retired_at)
        where source_scope_key=p_source_scope_key and lifecycle_status<>'removed'
        returning source_key
    ) select count(*) into v_retired from retired;
    update public.tracked_jobs tracked set
        status=case when a.has_active or a.has_possible then 'active' else 'closed' end,
        lifecycle_status=case when a.has_active then 'active'
            when a.has_possible then 'possibly_missing' else 'removed' end,
        missing_successful_runs=a.minimum_missing,
        removed_at=case when a.has_active or a.has_possible then null
            else coalesce(tracked.removed_at,p_retired_at) end
    from (
        select source_key, bool_or(lifecycle_status='active') has_active,
            bool_or(lifecycle_status='possibly_missing') has_possible,
            min(missing_successful_runs) minimum_missing
        from public.tracked_job_source_scopes group by source_key
    ) a
    where tracked.source_key=a.source_key and tracked.source_key in (
        select source_key from public.tracked_job_source_scopes
        where source_scope_key=p_source_scope_key
    );
    select count(*) into v_closed from public.tracked_jobs where status='closed'
      and source_key in (select source_key from public.tracked_job_source_scopes
                         where source_scope_key=p_source_scope_key);
    return jsonb_build_object('source_scope_key',p_source_scope_key,'owned',v_owned,
        'owners_retired',v_retired,'canonical_jobs_closed',v_closed,
        'replayed',v_retired=0);
end;
$$;
revoke all on function public.retire_student_job_source_scope(
    text,text,text,text,text,timestamptz
) from public,anon,authenticated;
grant execute on function public.retire_student_job_source_scope(
    text,text,text,text,text,timestamptz
) to service_role;

-- Bounded local-retention maintenance. Retention never supplies source-absence
-- evidence, application-linked jobs are protected, and only finalized history
-- is eligible for the seven-day operational cleanup.
create or replace function public.job_retention_source_timestamp(p_value text)
returns timestamptz
language plpgsql
immutable
set search_path = public, pg_temp
as $$
begin
    if p_value is null or btrim(p_value) = '' then
        return null;
    end if;
    if btrim(p_value) ~ '^\d{4}-\d{2}-\d{2}$' then
        return (btrim(p_value)::date)::timestamp at time zone 'UTC';
    end if;
    if btrim(p_value) ~ '^\d{4}-\d{2}-\d{2}[Tt ][0-2]\d:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:[Zz]|[+-][0-2]\d(?::?[0-5]\d)?)$' then
        return btrim(p_value)::timestamptz;
    end if;
    return null;
exception when others then
    return null;
end;
$$;

revoke all on function public.job_retention_source_timestamp(text)
    from public, anon, authenticated;
grant execute on function public.job_retention_source_timestamp(text)
    to service_role;

-- Common execute-mode branches use indexes. Exact dry-run inventory counts
-- remain intentionally comprehensive and are reserved for the cleanup tool.
create index if not exists tracked_jobs_retention_removed_idx
    on public.tracked_jobs (
        coalesce(removed_at, last_seen_at, first_seen_at), source_key
    )
    where lifecycle_status = 'removed' and status = 'closed';
create index if not exists tracked_jobs_retention_stale_idx
    on public.tracked_jobs (source_key)
    where freshness_band = 'stale';
create index if not exists tracked_jobs_retention_posted_idx
    on public.tracked_jobs (
        public.job_retention_source_timestamp(source_posted_at), source_key
    )
    where nullif(btrim(source_posted_at), '') is not null;
create index if not exists job_sync_sessions_retention_idx
    on public.job_sync_sessions (finalized_at, sync_id)
    where status = 'finalized';
create index if not exists scan_source_syncs_retention_idx
    on public.scan_source_syncs (completed_at, scan_id, company, source_scope);
create index if not exists job_sync_sessions_active_scan_idx
    on public.job_sync_sessions (scan_id)
    where status = 'staging';

drop function if exists public.maintain_student_job_retention(
    boolean,timestamptz,integer,boolean,integer,integer,integer
);

create function public.maintain_student_job_retention(
    p_execute boolean,
    p_as_of timestamptz,
    p_max_job_age_days integer,
    p_drop_stale boolean,
    p_removed_job_retention_days integer,
    p_sync_history_retention_days integer,
    p_batch_size integer
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_removed_closed bigint := 0;
    v_stale bigint := 0;
    v_over_age bigint := 0;
    v_protected bigint := 0;
    v_total_candidates bigint := 0;
    v_total_deletable bigint := 0;
    v_scope_rows bigint := 0;
    v_old_sessions bigint := 0;
    v_old_source_syncs bigint := 0;
    v_current_jobs bigint;
    v_current_scope_rows bigint;
    v_current_sessions bigint;
    v_current_source_syncs bigint;
    v_jobs_deleted bigint := 0;
    v_scope_rows_deleted bigint := 0;
    v_sessions_deleted bigint := 0;
    v_source_syncs_deleted bigint := 0;
    v_sync_chunks_deleted bigint := 0;
    v_sync_stage_deleted bigint := 0;
begin
    if p_execute is null or p_as_of is null or p_drop_stale is null then
        raise exception 'execution mode, reference time, and stale policy are required';
    end if;
    if p_max_job_age_days < 1
       or p_removed_job_retention_days < 0
       or p_sync_history_retention_days < 1
       or p_batch_size < 1
       or p_batch_size > 5000 then
        raise exception 'invalid retention policy or batch size';
    end if;

    create temporary table job_retention_candidates (
        id uuid primary key,
        source_key text not null,
        bucket text not null
    ) on commit drop;

    create temporary table sync_retention_candidates (
        sync_id uuid primary key
    ) on commit drop;

    if p_execute then
        -- Execute mode materializes at most one deletable batch. The explicit
        -- anti-join and the applications FK's ON DELETE RESTRICT protect rows
        -- even if an application is inserted concurrently.
        insert into job_retention_candidates (id, source_key, bucket)
        select job.id, job.source_key, 'removed_closed'
        from public.tracked_jobs job
        where job.lifecycle_status = 'removed'
          and job.status = 'closed'
          and coalesce(job.removed_at, job.last_seen_at, job.first_seen_at)
              <= p_as_of - make_interval(days => p_removed_job_retention_days)
          and not exists (
              select 1 from public.applications application
              where application.job_id = job.id
          )
        order by coalesce(job.removed_at, job.last_seen_at, job.first_seen_at),
                 job.source_key
        limit p_batch_size;

        if p_drop_stale then
            insert into job_retention_candidates (id, source_key, bucket)
            select job.id, job.source_key, 'stale'
            from public.tracked_jobs job
            where job.freshness_band = 'stale'
              and not exists (
                  select 1 from job_retention_candidates candidate
                  where candidate.id = job.id
              )
              and not exists (
                  select 1 from public.applications application
                  where application.job_id = job.id
              )
            order by job.source_key
            limit greatest(
                p_batch_size - (select count(*) from job_retention_candidates), 0
            );
        end if;

        insert into job_retention_candidates (id, source_key, bucket)
        select classified.id, classified.source_key, 'over_60_days'
        from (
            select job.id, job.source_key
            from public.tracked_jobs job
            where nullif(btrim(job.source_posted_at), '') is not null
              and public.job_retention_source_timestamp(job.source_posted_at)
                  < p_as_of - make_interval(days => p_max_job_age_days)
            union all
            select job.id, job.source_key
            from public.tracked_jobs job
            where nullif(btrim(job.source_posted_at), '') is null
              and not (job.date_parse_errors ?| array[
                  'source_posted_at:unparseable',
                  'source_posted_at:future_date'
              ])
              and job.first_seen_at
                  < p_as_of - make_interval(days => p_max_job_age_days)
        ) classified
        where not exists (
              select 1 from job_retention_candidates candidate
              where candidate.id = classified.id
          )
          and not exists (
              select 1 from public.applications application
              where application.job_id = classified.id
          )
        order by classified.source_key
        limit greatest(
            p_batch_size - (select count(*) from job_retention_candidates), 0
        );

        -- A canonical job can own multiple scope rows. Trim the job batch so
        -- the ownership cascade also stays within the per-table row cap.
        with ownership_counts as (
            select candidate.id, candidate.bucket, candidate.source_key,
                   count(owner.ownership_key) as ownership_rows
            from job_retention_candidates candidate
            left join public.tracked_job_source_scopes owner
              on owner.source_key = candidate.source_key
            group by candidate.id, candidate.bucket, candidate.source_key
        ), bounded as (
            select id
            from (
                select id, ownership_rows,
                    sum(ownership_rows) over (
                        order by bucket, source_key
                        rows between unbounded preceding and current row
                    ) as running_ownership_rows
                from ownership_counts
                where ownership_rows <= p_batch_size
            ) ranked
            where running_ownership_rows <= p_batch_size
        )
        delete from job_retention_candidates candidate
        where not exists (select 1 from bounded where bounded.id = candidate.id);
    else
        -- Dry-run mode gives exact, non-overlapping counts without mutating
        -- persistent rows. Protected rows are separate from deletable totals.
        insert into job_retention_candidates (id, source_key, bucket)
        select classified.id, classified.source_key,
            case when exists (
                select 1 from public.applications application
                where application.job_id = classified.id
            ) then 'protected_by_application' else classified.reason end
        from (
            select job.id, job.source_key,
                case
                    when job.lifecycle_status = 'removed'
                         and job.status = 'closed'
                         and coalesce(job.removed_at, job.last_seen_at, job.first_seen_at)
                             <= p_as_of - make_interval(days => p_removed_job_retention_days)
                        then 'removed_closed'
                    when p_drop_stale and job.freshness_band = 'stale'
                        then 'stale'
                    when (
                        case
                            when nullif(btrim(job.source_posted_at), '') is not null
                                then case
                                    when public.job_retention_source_timestamp(
                                        job.source_posted_at
                                    ) <= p_as_of
                                    then extract(epoch from (
                                        p_as_of - public.job_retention_source_timestamp(
                                            job.source_posted_at
                                        )
                                    )) / 86400.0
                                end
                            when job.date_parse_errors ?| array[
                                'source_posted_at:unparseable',
                                'source_posted_at:future_date'
                            ] then null
                            when job.first_seen_at <= p_as_of
                                then extract(epoch from (
                                    p_as_of - job.first_seen_at
                                )) / 86400.0
                        end
                    ) > p_max_job_age_days
                        then 'over_60_days'
                    else null
                end as reason
            from public.tracked_jobs job
        ) classified
        where classified.reason is not null;
    end if;

    select
        count(*) filter (where bucket = 'removed_closed'),
        count(*) filter (where bucket = 'stale'),
        count(*) filter (where bucket = 'over_60_days'),
        count(*) filter (where bucket = 'protected_by_application'),
        count(*),
        count(*) filter (where bucket <> 'protected_by_application')
    into v_removed_closed, v_stale, v_over_age, v_protected,
         v_total_candidates, v_total_deletable
    from job_retention_candidates;

    select count(*) into v_scope_rows
    from public.tracked_job_source_scopes owner
    join job_retention_candidates candidate using (source_key)
    where candidate.bucket <> 'protected_by_application';

    if not p_execute then
        select count(*) into v_old_sessions
        from public.job_sync_sessions session
        where session.status = 'finalized'
          and coalesce(session.finalized_at, session.updated_at, session.created_at)
              < p_as_of - make_interval(days => p_sync_history_retention_days);

        select count(*) into v_old_source_syncs
        from public.scan_source_syncs source_sync
        where source_sync.completed_at
                  < p_as_of - make_interval(days => p_sync_history_retention_days)
          and not exists (
              select 1 from public.job_sync_sessions active
              where active.scan_id = source_sync.scan_id
                and active.status = 'staging'
          );

        select count(*) into v_current_jobs from public.tracked_jobs;
        select count(*) into v_current_scope_rows from public.tracked_job_source_scopes;
        select count(*) into v_current_sessions from public.job_sync_sessions;
        select count(*) into v_current_source_syncs from public.scan_source_syncs;
    else
        delete from public.tracked_jobs job
        using job_retention_candidates candidate
        where job.id = candidate.id;
        get diagnostics v_jobs_deleted = row_count;
        v_scope_rows_deleted := v_scope_rows;

        insert into sync_retention_candidates (sync_id)
        select session.sync_id
        from public.job_sync_sessions session
        where session.status = 'finalized'
          and coalesce(session.finalized_at, session.updated_at, session.created_at)
              < p_as_of - make_interval(days => p_sync_history_retention_days)
        order by coalesce(session.finalized_at, session.updated_at, session.created_at),
                 session.sync_id
        limit p_batch_size;

        -- Finalized sessions normally have no staged children. If interrupted
        -- historical state does, drain each child table in bounded pieces and
        -- delete the parent only after its children are gone. Staging sessions
        -- are never selected.
        with batch as (
            select staged.ctid
            from public.job_sync_stage staged
            join sync_retention_candidates candidate using (sync_id)
            order by staged.sync_id, staged.source_key
            limit p_batch_size
        )
        delete from public.job_sync_stage staged
        using batch
        where staged.ctid = batch.ctid;
        get diagnostics v_sync_stage_deleted = row_count;

        with batch as (
            select chunk.ctid
            from public.job_sync_chunks chunk
            join sync_retention_candidates candidate using (sync_id)
            order by chunk.sync_id, chunk.chunk_number
            limit p_batch_size
        )
        delete from public.job_sync_chunks chunk
        using batch
        where chunk.ctid = batch.ctid;
        get diagnostics v_sync_chunks_deleted = row_count;

        delete from public.job_sync_sessions session
        using sync_retention_candidates candidate
        where session.sync_id = candidate.sync_id
          and not exists (
              select 1 from public.job_sync_stage staged
              where staged.sync_id = session.sync_id
          )
          and not exists (
              select 1 from public.job_sync_chunks chunk
              where chunk.sync_id = session.sync_id
          );
        get diagnostics v_sessions_deleted = row_count;
        v_old_sessions := (
            select count(*) from sync_retention_candidates
        );

        with batch as (
            select source_sync.scan_id, source_sync.company, source_sync.source_scope
            from public.scan_source_syncs source_sync
            where source_sync.completed_at
                      < p_as_of - make_interval(days => p_sync_history_retention_days)
              and not exists (
                  select 1 from public.job_sync_sessions active
                  where active.scan_id = source_sync.scan_id
                    and active.status = 'staging'
              )
            order by source_sync.completed_at, source_sync.scan_id,
                     source_sync.company, source_sync.source_scope
            limit p_batch_size
        )
        delete from public.scan_source_syncs source_sync
        using batch
        where source_sync.scan_id = batch.scan_id
          and source_sync.company = batch.company
          and source_sync.source_scope = batch.source_scope;
        get diagnostics v_source_syncs_deleted = row_count;
        v_old_source_syncs := v_source_syncs_deleted;
    end if;

    return jsonb_build_object(
        'dry_run', not p_execute,
        'counts_complete', not p_execute,
        'batch_size', p_batch_size,
        'before', jsonb_build_object(
            'removed_closed', v_removed_closed,
            'stale', v_stale,
            'over_60_days', v_over_age,
            'protected_by_application', v_protected,
            'total_unique_job_candidates', v_total_candidates,
            'total_jobs_deletable', v_total_deletable,
            'tracked_job_source_scope_rows_affected', v_scope_rows,
            'old_sync_sessions', v_old_sessions,
            'old_scan_source_syncs', v_old_source_syncs,
            'current_tracked_jobs', v_current_jobs,
            'current_tracked_job_source_scope_rows', v_current_scope_rows,
            'current_job_sync_sessions', v_current_sessions,
            'current_scan_source_syncs', v_current_source_syncs
        ),
        'deleted', jsonb_build_object(
            'tracked_jobs', v_jobs_deleted,
            'tracked_job_source_scope_rows', v_scope_rows_deleted,
            'job_sync_sessions', v_sessions_deleted,
            'job_sync_chunks', v_sync_chunks_deleted,
            'job_sync_stage', v_sync_stage_deleted,
            'scan_source_syncs', v_source_syncs_deleted
        ),
        'after', case when p_execute then jsonb_build_object(
            'selected_job_candidates_remaining',
                greatest(v_total_deletable - v_jobs_deleted, 0),
            'selected_old_sync_sessions_remaining',
                greatest(v_old_sessions - v_sessions_deleted, 0),
            'selected_old_scan_source_syncs_remaining',
                greatest(v_old_source_syncs - v_source_syncs_deleted, 0)
        ) else jsonb_build_object(
            'removed_closed', v_removed_closed,
            'stale', v_stale,
            'over_60_days', v_over_age,
            'protected_by_application', v_protected,
            'total_unique_job_candidates', v_total_candidates,
            'total_jobs_deletable', v_total_deletable,
            'tracked_job_source_scope_rows_affected', v_scope_rows,
            'old_sync_sessions', v_old_sessions,
            'old_scan_source_syncs', v_old_source_syncs,
            'current_tracked_jobs', v_current_jobs,
            'current_tracked_job_source_scope_rows', v_current_scope_rows,
            'current_job_sync_sessions', v_current_sessions,
            'current_scan_source_syncs', v_current_source_syncs
        ) end
    );
end;
$$;

revoke all on function public.maintain_student_job_retention(
    boolean,timestamptz,integer,boolean,integer,integer,integer
) from public, anon, authenticated;
grant execute on function public.maintain_student_job_retention(
    boolean,timestamptz,integer,boolean,integer,integer,integer
) to service_role;

comment on function public.maintain_student_job_retention(
    boolean,timestamptz,integer,boolean,integer,integer,integer
) is
    'Dry-runs or executes bounded local retention without creating source-absence evidence; application-linked jobs and active sync sessions are protected.';

-- Phase 6 bootstrap parity. Chronological upgrades receive the same objects in
-- 20261010_phase6_incremental_checkpoints.sql.
alter table public.job_sync_sessions
    add column if not exists checkpoint_candidate jsonb;

create or replace function public.begin_student_job_source_sync(
    p_sync_id uuid, p_scan_id uuid, p_company text, p_source_scope text,
    p_source_scope_key text, p_expected_chunks integer,
    p_expected_rows integer, p_expected_bytes bigint,
    p_reconcile_absence boolean, p_protected_source_keys text[],
    p_close_after_missing_runs integer, p_seen_at timestamptz,
    p_checkpoint_candidate jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_result jsonb;
    v_existing jsonb;
begin
    if p_checkpoint_candidate is not null then
        if jsonb_typeof(p_checkpoint_candidate) <> 'object'
           or pg_column_size(p_checkpoint_candidate) > 16384 then
            raise exception 'checkpoint candidate must be a bounded object';
        end if;
        if p_checkpoint_candidate->>'source_scope_key' is distinct from p_source_scope_key then
            raise exception 'checkpoint candidate source scope mismatch';
        end if;
        if nullif(btrim(p_checkpoint_candidate->>'provider_identity'), '') is null
           or nullif(btrim(p_checkpoint_candidate->>'config_fingerprint'), '') is null
           or nullif(btrim(p_checkpoint_candidate->>'strategy_version'), '') is null then
            raise exception 'checkpoint candidate identity is incomplete';
        end if;
    end if;
    v_result := public.begin_student_job_source_sync(
        p_sync_id, p_scan_id, p_company, p_source_scope, p_source_scope_key,
        p_expected_chunks, p_expected_rows, p_expected_bytes,
        p_reconcile_absence, p_protected_source_keys,
        p_close_after_missing_runs, p_seen_at
    );
    select checkpoint_candidate into v_existing
    from public.job_sync_sessions where sync_id = p_sync_id for update;
    if v_existing is not null and v_existing is distinct from p_checkpoint_candidate then
        raise exception 'sync ID was already initialized with a different checkpoint candidate';
    end if;
    update public.job_sync_sessions
       set checkpoint_candidate = p_checkpoint_candidate, updated_at = now()
     where sync_id = p_sync_id and status = 'staging';
    return v_result;
end;
$$;

revoke all on function public.begin_student_job_source_sync(
    uuid,uuid,text,text,text,integer,integer,bigint,boolean,text[],integer,timestamptz,jsonb
) from public, anon, authenticated;
grant execute on function public.begin_student_job_source_sync(
    uuid,uuid,text,text,text,integer,integer,bigint,boolean,text[],integer,timestamptz,jsonb
) to service_role;

create or replace function public.commit_student_job_incremental_checkpoint()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_candidate jsonb;
begin
    if new.checkpoint_candidate is null then return new; end if;
    v_candidate := new.checkpoint_candidate;
    if v_candidate->>'source_scope_key' is distinct from new.source_scope_key then
        raise exception 'checkpoint candidate source scope mismatch at finalization';
    end if;
    insert into public.source_incremental_checkpoints (
        source_scope_key,provider_identity,config_fingerprint,strategy_version,
        cursor,timestamp_watermark,last_successful_full_at,
        last_successful_incremental_at,last_forced_full_reason,
        representation_token,snapshot_checksum,ambiguous_since_last_full,updated_at
    ) values (
        new.source_scope_key,v_candidate->>'provider_identity',
        v_candidate->>'config_fingerprint',v_candidate->>'strategy_version',
        v_candidate->>'cursor',(v_candidate->>'timestamp_watermark')::timestamptz,
        (v_candidate->>'last_successful_full_at')::timestamptz,
        (v_candidate->>'last_successful_incremental_at')::timestamptz,
        v_candidate->>'last_forced_full_reason',v_candidate->>'representation_token',
        v_candidate->>'snapshot_checksum',
        coalesce((v_candidate->>'ambiguous_since_last_full')::boolean,false),
        coalesce((v_candidate->>'updated_at')::timestamptz,now())
    ) on conflict(source_scope_key,provider_identity) do update set
        config_fingerprint=excluded.config_fingerprint,
        strategy_version=excluded.strategy_version,cursor=excluded.cursor,
        timestamp_watermark=excluded.timestamp_watermark,
        last_successful_full_at=excluded.last_successful_full_at,
        last_successful_incremental_at=excluded.last_successful_incremental_at,
        last_forced_full_reason=excluded.last_forced_full_reason,
        representation_token=excluded.representation_token,
        snapshot_checksum=excluded.snapshot_checksum,
        ambiguous_since_last_full=excluded.ambiguous_since_last_full,
        updated_at=excluded.updated_at;
    return new;
end;
$$;

revoke all on function public.commit_student_job_incremental_checkpoint()
    from public, anon, authenticated;
grant execute on function public.commit_student_job_incremental_checkpoint()
    to service_role;
drop trigger if exists commit_student_job_incremental_checkpoint on public.job_sync_sessions;
create trigger commit_student_job_incremental_checkpoint
after update of status on public.job_sync_sessions for each row
when (new.status = 'finalized' and old.status is distinct from new.status)
execute function public.commit_student_job_incremental_checkpoint();
