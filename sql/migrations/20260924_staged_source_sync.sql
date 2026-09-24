-- Durable, bounded source synchronization for Supabase/PostgREST.
-- Apply before deploying code that calls the staged synchronization RPCs.

create table if not exists public.job_sync_sessions (
    sync_id uuid primary key,
    scan_id uuid not null,
    company text not null,
    expected_chunks integer not null check (expected_chunks >= 0),
    expected_rows integer not null check (expected_rows >= 0),
    expected_bytes bigint not null check (expected_bytes >= 0),
    reconcile_absence boolean not null,
    close_after_missing_runs integer not null check (close_after_missing_runs >= 1),
    seen_at timestamptz not null,
    status text not null default 'staging' check (status in ('staging', 'finalized')),
    result jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    finalized_at timestamptz,
    unique (sync_id, company),
    check (result is null or jsonb_typeof(result) = 'object')
);

create table if not exists public.job_sync_chunks (
    sync_id uuid not null references public.job_sync_sessions(sync_id) on delete cascade,
    chunk_number integer not null check (chunk_number >= 0),
    row_count integer not null check (row_count >= 0),
    payload_bytes bigint not null check (payload_bytes >= 0),
    payload_hash text not null,
    staged_at timestamptz not null default now(),
    primary key (sync_id, chunk_number)
);

create table if not exists public.job_sync_stage (
    sync_id uuid not null,
    company text not null,
    chunk_number integer not null,
    source_key text not null,
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
    raw_data jsonb not null,
    primary key (sync_id, source_key),
    foreign key (sync_id, company)
        references public.job_sync_sessions(sync_id, company) on delete cascade,
    check (jsonb_typeof(matched_categories) = 'array'),
    check (jsonb_typeof(positive_signals) = 'array'),
    check (jsonb_typeof(negative_signals) = 'array'),
    check (jsonb_typeof(urgency_reasons) = 'array'),
    check (jsonb_typeof(date_parse_errors) = 'array'),
    check (jsonb_typeof(raw_data) = 'object')
);

-- Used only by bounded stale-session cleanup. The stage primary key supports
-- every finalize join as (sync_id, source_key); tracked_jobs already has a
-- unique source_key index and tracked_jobs_company_idx for reconciliation.
create index if not exists job_sync_sessions_cleanup_idx
    on public.job_sync_sessions (updated_at);

alter table public.job_sync_sessions enable row level security;
alter table public.job_sync_chunks enable row level security;
alter table public.job_sync_stage enable row level security;
revoke all on table public.job_sync_sessions from anon, authenticated;
revoke all on table public.job_sync_chunks from anon, authenticated;
revoke all on table public.job_sync_stage from anon, authenticated;
grant all on table public.job_sync_sessions to service_role;
grant all on table public.job_sync_chunks to service_role;
grant all on table public.job_sync_stage to service_role;

create or replace function public.begin_student_job_source_sync(
    p_sync_id uuid,
    p_scan_id uuid,
    p_company text,
    p_expected_chunks integer,
    p_expected_rows integer,
    p_expected_bytes bigint,
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
        finalized_at
    ) values (
        p_sync_id, p_scan_id, p_company, p_expected_chunks, p_expected_rows,
        p_expected_bytes, p_reconcile_absence, p_close_after_missing_runs,
        p_seen_at, case when v_prior_result is null then 'staging' else 'finalized' end,
        v_prior_result, case when v_prior_result is null then null else now() end
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

create or replace function public.stage_student_job_source_chunk(
    p_sync_id uuid,
    p_chunk_number integer,
    p_chunk_rows integer,
    p_chunk_bytes bigint,
    p_payload_hash text,
    p_jobs jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_session public.job_sync_sessions%rowtype;
    v_chunk public.job_sync_chunks%rowtype;
begin
    select * into v_session
    from public.job_sync_sessions
    where sync_id = p_sync_id
    for update;
    if not found then
        raise exception 'unknown sync ID';
    end if;
    if v_session.status = 'finalized' then
        return jsonb_build_object('status', 'finalized', 'replayed', true);
    end if;
    if p_chunk_number < 0 or p_chunk_number >= v_session.expected_chunks then
        raise exception 'chunk number is outside the declared range';
    end if;
    if p_chunk_rows < 1 or p_chunk_rows > 200 then
        raise exception 'chunk row count exceeds the 200-row limit';
    end if;
    if p_chunk_bytes < 1 or p_chunk_bytes > 524288 then
        raise exception 'chunk payload exceeds the 512 KiB limit';
    end if;
    if jsonb_typeof(p_jobs) <> 'array' or jsonb_array_length(p_jobs) <> p_chunk_rows then
        raise exception 'chunk row count does not match p_jobs';
    end if;

    select * into v_chunk
    from public.job_sync_chunks
    where sync_id = p_sync_id and chunk_number = p_chunk_number;
    if found then
        if v_chunk.row_count is distinct from p_chunk_rows
           or v_chunk.payload_bytes is distinct from p_chunk_bytes
           or v_chunk.payload_hash is distinct from p_payload_hash then
            raise exception 'chunk retry metadata does not match the staged chunk';
        end if;
        update public.job_sync_sessions set updated_at = now() where sync_id = p_sync_id;
        return jsonb_build_object('status', 'staged', 'replayed', true);
    end if;

    if exists (
        select 1
        from jsonb_to_recordset(p_jobs) as item(company text)
        where item.company is distinct from v_session.company
    ) then
        raise exception 'incoming jobs must all belong to the sync company';
    end if;
    if exists (
        select 1
        from jsonb_to_recordset(p_jobs) as item(source_key text)
        join public.job_sync_stage staged
          on staged.sync_id = p_sync_id and staged.source_key = item.source_key
    ) then
        raise exception 'a source key was staged in more than one chunk';
    end if;

    insert into public.job_sync_stage (
        sync_id, company, chunk_number, source_key, title, location, department,
        apply_url, source_url, source_platform, primary_category,
        matched_categories, relevance_score, relevance_band, positive_signals,
        negative_signals, location_status, source_updated_at, source_posted_at,
        source_closes_at, first_seen_at, last_seen_at, first_seen_basis,
        removed_at, status, lifecycle_status, missing_successful_runs,
        freshness_age_source, age_days, first_seen_age_hours, freshness_band,
        deadline_days_remaining, urgency_band, urgency_score, urgency_reasons,
        date_parse_errors, is_ranked_output, raw_data
    )
    select
        p_sync_id, item.company, p_chunk_number, item.source_key, item.title,
        item.location, item.department, item.apply_url, item.source_url,
        item.source_platform, item.primary_category,
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
    from jsonb_to_recordset(p_jobs) as item (
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

    insert into public.job_sync_chunks (
        sync_id, chunk_number, row_count, payload_bytes, payload_hash
    ) values (
        p_sync_id, p_chunk_number, p_chunk_rows, p_chunk_bytes, p_payload_hash
    );
    update public.job_sync_sessions set updated_at = now() where sync_id = p_sync_id;
    return jsonb_build_object('status', 'staged', 'replayed', false);
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

create or replace function public.abort_student_job_source_sync(p_sync_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_status text;
begin
    select status into v_status
    from public.job_sync_sessions where sync_id = p_sync_id for update;
    if not found or v_status = 'finalized' then
        return false;
    end if;
    delete from public.job_sync_sessions where sync_id = p_sync_id;
    return true;
end;
$$;

create or replace function public.cleanup_stale_job_sync_sessions(
    p_stale_before timestamptz default now() - interval '24 hours',
    p_limit integer default 1000
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_deleted integer;
begin
    if p_limit < 1 then
        raise exception 'p_limit must be at least 1';
    end if;
    with stale as (
        select sync_id
        from public.job_sync_sessions
        where updated_at < p_stale_before
        order by updated_at
        for update skip locked
        limit p_limit
    ), deleted as (
        delete from public.job_sync_sessions sessions
        using stale
        where sessions.sync_id = stale.sync_id
        returning sessions.sync_id
    )
    select count(*) into v_deleted from deleted;
    return v_deleted;
end;
$$;

revoke all on function public.begin_student_job_source_sync(
    uuid, uuid, text, integer, integer, bigint, boolean, integer, timestamptz
) from public, anon, authenticated;
revoke all on function public.stage_student_job_source_chunk(
    uuid, integer, integer, bigint, text, jsonb
) from public, anon, authenticated;
revoke all on function public.finalize_student_job_source_sync(uuid)
    from public, anon, authenticated;
revoke all on function public.abort_student_job_source_sync(uuid)
    from public, anon, authenticated;
revoke all on function public.cleanup_stale_job_sync_sessions(timestamptz, integer)
    from public, anon, authenticated;
grant execute on function public.begin_student_job_source_sync(
    uuid, uuid, text, integer, integer, bigint, boolean, integer, timestamptz
) to service_role;
grant execute on function public.stage_student_job_source_chunk(
    uuid, integer, integer, bigint, text, jsonb
) to service_role;
grant execute on function public.finalize_student_job_source_sync(uuid) to service_role;
grant execute on function public.abort_student_job_source_sync(uuid) to service_role;
grant execute on function public.cleanup_stale_job_sync_sessions(timestamptz, integer)
    to service_role;

comment on table public.job_sync_stage is
    'Durable bounded staging rows for atomic source finalization; never a lifecycle authority.';
comment on function public.finalize_student_job_source_sync(uuid) is
    'Atomically applies one fully staged company source and reconciles absence exactly once.';
