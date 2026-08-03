-- RoleRadar student-job persistence for Supabase/PostgreSQL.
--
-- Apply this schema from a trusted backend/admin context. The Python backend uses
-- SUPABASE_SERVICE_ROLE_KEY. Never expose that key in frontend code, browser
-- bundles, public logs, or client-side environment variables.

create extension if not exists pgcrypto;

create table if not exists public.tracked_jobs (
    id uuid primary key default gen_random_uuid(),
    source_key text not null unique,
    company text not null,
    title text not null,
    location text,
    department text,
    apply_url text,
    source_url text,
    source_platform text not null,
    primary_category text,
    matched_categories jsonb not null default '[]'::jsonb,
    relevance_score integer not null default 0
        check (relevance_score between 0 and 100),
    relevance_band text,
    positive_signals jsonb not null default '[]'::jsonb,
    negative_signals jsonb not null default '[]'::jsonb,
    location_status text,
    source_updated_at timestamptz,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    status text not null default 'active'
        check (status in ('active', 'closed')),
    missing_successful_runs integer not null default 0
        check (missing_successful_runs >= 0),
    raw_data jsonb not null default '{}'::jsonb,
    check (jsonb_typeof(matched_categories) = 'array'),
    check (jsonb_typeof(positive_signals) = 'array'),
    check (jsonb_typeof(negative_signals) = 'array'),
    check (jsonb_typeof(raw_data) = 'object')
);

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
    check (jsonb_typeof(exclusion_counts) = 'object')
);

-- This table is manually controlled. Job refreshes and the synchronization RPC
-- below never insert, update, or delete application rows.
create table if not exists public.applications (
    job_id uuid primary key references public.tracked_jobs(id) on delete restrict,
    application_status text not null,
    applied_at timestamptz,
    resume_version text,
    follow_up_at timestamptz,
    notes text,
    updated_at timestamptz not null default now()
);

-- The tracked_jobs_source_key_key unique constraint supplies the source-key index.
create index if not exists tracked_jobs_status_idx
    on public.tracked_jobs (status);
create index if not exists tracked_jobs_company_idx
    on public.tracked_jobs (company);
create index if not exists tracked_jobs_relevance_score_idx
    on public.tracked_jobs (relevance_score desc);
create index if not exists tracked_jobs_first_seen_at_idx
    on public.tracked_jobs (first_seen_at desc);
create index if not exists tracked_jobs_primary_category_idx
    on public.tracked_jobs (primary_category);

alter table public.tracked_jobs enable row level security;
alter table public.scan_runs enable row level security;
alter table public.applications enable row level security;

revoke all on table public.tracked_jobs from anon, authenticated;
revoke all on table public.scan_runs from anon, authenticated;
revoke all on table public.applications from anon, authenticated;
grant all on table public.tracked_jobs to service_role;
grant all on table public.scan_runs to service_role;
grant all on table public.applications to service_role;

-- Perform one successful scan refresh in a single database transaction. If this
-- function raises, job upserts, missing counters, closures, and scan_runs all roll
-- back together.
create or replace function public.sync_student_job_scan(
    p_jobs jsonb,
    p_successful_companies text[],
    p_close_after_missing_runs integer,
    p_seen_at timestamptz,
    p_scan jsonb
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
begin
    if p_close_after_missing_runs < 1 then
        raise exception 'p_close_after_missing_runs must be at least 1';
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
        first_seen_at timestamptz not null,
        last_seen_at timestamptz not null,
        status text not null,
        missing_successful_runs integer not null,
        raw_data jsonb not null
    ) on commit drop;

    insert into sync_student_jobs_incoming (
        source_key,
        company,
        title,
        location,
        department,
        apply_url,
        source_url,
        source_platform,
        primary_category,
        matched_categories,
        relevance_score,
        relevance_band,
        positive_signals,
        negative_signals,
        location_status,
        source_updated_at,
        first_seen_at,
        last_seen_at,
        status,
        missing_successful_runs,
        raw_data
    )
    select
        item.source_key,
        item.company,
        item.title,
        item.location,
        item.department,
        item.apply_url,
        item.source_url,
        item.source_platform,
        item.primary_category,
        coalesce(item.matched_categories, '[]'::jsonb),
        item.relevance_score,
        item.relevance_band,
        coalesce(item.positive_signals, '[]'::jsonb),
        coalesce(item.negative_signals, '[]'::jsonb),
        item.location_status,
        item.source_updated_at,
        item.first_seen_at,
        item.last_seen_at,
        item.status,
        item.missing_successful_runs,
        coalesce(item.raw_data, '{}'::jsonb)
    from jsonb_to_recordset(coalesce(p_jobs, '[]'::jsonb)) as item (
        source_key text,
        company text,
        title text,
        location text,
        department text,
        apply_url text,
        source_url text,
        source_platform text,
        primary_category text,
        matched_categories jsonb,
        relevance_score integer,
        relevance_band text,
        positive_signals jsonb,
        negative_signals jsonb,
        location_status text,
        source_updated_at timestamptz,
        first_seen_at timestamptz,
        last_seen_at timestamptz,
        status text,
        missing_successful_runs integer,
        raw_data jsonb
    );

    select
        count(*) filter (where existing.source_key is null),
        count(*) filter (
            where existing.source_key is not null
              and (
                  existing.company is distinct from incoming.company
                  or existing.title is distinct from incoming.title
                  or existing.location is distinct from incoming.location
                  or existing.department is distinct from incoming.department
                  or existing.apply_url is distinct from incoming.apply_url
                  or existing.source_url is distinct from incoming.source_url
                  or existing.source_platform is distinct from incoming.source_platform
                  or existing.primary_category is distinct from incoming.primary_category
                  or existing.matched_categories is distinct from incoming.matched_categories
                  or existing.relevance_score is distinct from incoming.relevance_score
                  or existing.relevance_band is distinct from incoming.relevance_band
                  or existing.positive_signals is distinct from incoming.positive_signals
                  or existing.negative_signals is distinct from incoming.negative_signals
                  or existing.location_status is distinct from incoming.location_status
                  or existing.source_updated_at is distinct from incoming.source_updated_at
                  or existing.status is distinct from 'active'
                  or existing.missing_successful_runs is distinct from 0
                  or existing.raw_data is distinct from incoming.raw_data
              )
        ),
        count(*) filter (
            where existing.source_key is not null
              and not (
                  existing.company is distinct from incoming.company
                  or existing.title is distinct from incoming.title
                  or existing.location is distinct from incoming.location
                  or existing.department is distinct from incoming.department
                  or existing.apply_url is distinct from incoming.apply_url
                  or existing.source_url is distinct from incoming.source_url
                  or existing.source_platform is distinct from incoming.source_platform
                  or existing.primary_category is distinct from incoming.primary_category
                  or existing.matched_categories is distinct from incoming.matched_categories
                  or existing.relevance_score is distinct from incoming.relevance_score
                  or existing.relevance_band is distinct from incoming.relevance_band
                  or existing.positive_signals is distinct from incoming.positive_signals
                  or existing.negative_signals is distinct from incoming.negative_signals
                  or existing.location_status is distinct from incoming.location_status
                  or existing.source_updated_at is distinct from incoming.source_updated_at
                  or existing.status is distinct from 'active'
                  or existing.missing_successful_runs is distinct from 0
                  or existing.raw_data is distinct from incoming.raw_data
              )
        )
    into v_inserted, v_updated, v_unchanged
    from sync_student_jobs_incoming incoming
    left join public.tracked_jobs existing using (source_key);

    insert into public.tracked_jobs (
        source_key,
        company,
        title,
        location,
        department,
        apply_url,
        source_url,
        source_platform,
        primary_category,
        matched_categories,
        relevance_score,
        relevance_band,
        positive_signals,
        negative_signals,
        location_status,
        source_updated_at,
        first_seen_at,
        last_seen_at,
        status,
        missing_successful_runs,
        raw_data
    )
    select
        source_key,
        company,
        title,
        location,
        department,
        apply_url,
        source_url,
        source_platform,
        primary_category,
        matched_categories,
        relevance_score,
        relevance_band,
        positive_signals,
        negative_signals,
        location_status,
        source_updated_at,
        first_seen_at,
        last_seen_at,
        status,
        missing_successful_runs,
        raw_data
    from sync_student_jobs_incoming
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
        last_seen_at = excluded.last_seen_at,
        status = 'active',
        missing_successful_runs = 0,
        raw_data = excluded.raw_data;

    with missing_updates as (
        update public.tracked_jobs tracked
        set
            missing_successful_runs = tracked.missing_successful_runs + 1,
            status = case
                when tracked.missing_successful_runs + 1 >= p_close_after_missing_runs
                    then 'closed'
                else tracked.status
            end
        where tracked.company = any(coalesce(p_successful_companies, array[]::text[]))
          and tracked.status <> 'closed'
          and not exists (
              select 1
              from sync_student_jobs_incoming incoming
              where incoming.source_key = tracked.source_key
          )
        returning status
    )
    select
        count(*),
        count(*) filter (where status = 'closed')
    into v_missing_incremented, v_closed
    from missing_updates;

    insert into public.scan_runs (
        started_at,
        completed_at,
        success,
        companies_checked,
        jobs_fetched,
        jobs_retained,
        jobs_inserted,
        jobs_updated,
        jobs_unchanged,
        exclusion_counts,
        coverage_report_path,
        ranked_artifact_path,
        error_message
    ) values (
        (p_scan ->> 'started_at')::timestamptz,
        (p_scan ->> 'completed_at')::timestamptz,
        true,
        coalesce((p_scan ->> 'companies_checked')::integer, 0),
        coalesce((p_scan ->> 'jobs_fetched')::integer, 0),
        coalesce((p_scan ->> 'jobs_retained')::integer, 0),
        v_inserted,
        v_updated,
        v_unchanged,
        coalesce(p_scan -> 'exclusion_counts', '{}'::jsonb),
        p_scan ->> 'coverage_report_path',
        p_scan ->> 'ranked_artifact_path',
        null
    );

    return jsonb_build_object(
        'inserted', v_inserted,
        'updated', v_updated,
        'unchanged', v_unchanged,
        'missing_incremented', v_missing_incremented,
        'closed', v_closed
    );
end;
$$;

revoke all on function public.sync_student_job_scan(
    jsonb,
    text[],
    integer,
    timestamptz,
    jsonb
) from public, anon, authenticated;
grant execute on function public.sync_student_job_scan(
    jsonb,
    text[],
    integer,
    timestamptz,
    jsonb
) to service_role;

comment on table public.applications is
    'Manually controlled application tracking; never modified by job refreshes.';
comment on function public.sync_student_job_scan(jsonb, text[], integer, timestamptz, jsonb) is
    'Backend-only atomic refresh. Invoke with SUPABASE_SERVICE_ROLE_KEY; never expose that key to frontend code.';
