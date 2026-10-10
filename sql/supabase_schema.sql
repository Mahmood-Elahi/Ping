-- RoleRadar student-job persistence for Supabase/PostgreSQL.
-- Apply from a trusted admin context; never expose the service-role key.

create extension if not exists pgcrypto;

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

alter table public.tracked_job_source_scopes enable row level security;
revoke all on table public.tracked_job_source_scopes from public, anon, authenticated;
grant all on table public.tracked_job_source_scopes to service_role;
alter table public.source_scope_retirements enable row level security;
revoke all on table public.source_scope_retirements from public, anon, authenticated;
grant all on table public.source_scope_retirements to service_role;

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
-- chronological migrations through 20261010_phase5_multi_scope_lifecycle.sql.
drop function if exists public.sync_student_job_source(
    uuid, text, jsonb, boolean, integer, timestamptz
);

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
