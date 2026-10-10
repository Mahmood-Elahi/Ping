-- Phase 5 Batch 3: logical source scopes and scope-owned lifecycle authority.
-- Forward-only; follows 20261009_phase3_source_reliability.sql.
-- The explicit transaction prevents callers from observing a schema where the
-- legacy RPC has been removed but its scope-aware replacement is not installed.

begin;

alter table public.job_source_health
    add column if not exists source_scope text not null default 'default';
alter table public.tracked_jobs
    add column if not exists source_scope text not null default 'default';
alter table public.job_sync_sessions
    add column if not exists source_scope text not null default 'default';
alter table public.job_sync_sessions
    add column if not exists source_scope_key text;
alter table public.job_sync_stage
    add column if not exists source_scope text not null default 'default';
alter table public.job_sync_stage
    add column if not exists source_scope_key text;
alter table public.job_sync_stage
    add column if not exists provider_source_key text;
alter table public.scan_source_syncs
    add column if not exists source_scope text not null default 'default';

update public.job_sync_sessions
set source_scope_key = lower(regexp_replace(btrim(company), '\s+', ' ', 'g')) || '::default'
where source_scope_key is null;
update public.job_sync_stage
set source_scope_key = lower(regexp_replace(btrim(company), '\s+', ' ', 'g')) || '::default',
    provider_source_key = source_key
where source_scope_key is null or provider_source_key is null;
alter table public.job_sync_sessions alter column source_scope_key set not null;
alter table public.job_sync_stage alter column source_scope_key set not null;
alter table public.job_sync_stage alter column provider_source_key set not null;

do $migration$
begin
    if exists (
        select 1 from pg_constraint
        where conrelid = 'public.scan_source_syncs'::regclass
          and conname = 'scan_source_syncs_pkey'
          and pg_get_constraintdef(oid) = 'PRIMARY KEY (scan_id, company)'
    ) then
        alter table public.scan_source_syncs drop constraint scan_source_syncs_pkey;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.scan_source_syncs'::regclass
          and conname = 'scan_source_syncs_pkey'
    ) then
        alter table public.scan_source_syncs
            add constraint scan_source_syncs_pkey
            primary key (scan_id, company, source_scope);
    end if;
end
$migration$;

create table if not exists public.tracked_job_source_scopes (
    ownership_key text primary key,
    source_key text not null references public.tracked_jobs(source_key) on delete cascade,
    canonical_company text not null,
    source_scope text not null default 'default',
    source_scope_key text not null,
    provider_source_key text not null,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    missing_successful_runs integer not null default 0
        check (missing_successful_runs >= 0),
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

insert into public.tracked_job_source_scopes (
    ownership_key, source_key, canonical_company, source_scope, source_scope_key,
    provider_source_key, first_seen_at, last_seen_at, missing_successful_runs,
    lifecycle_status, removed_at
)
select
    encode(digest(source_key || E'\x1f' ||
        lower(regexp_replace(btrim(company), '\s+', ' ', 'g')) || '::default', 'sha256'), 'hex'),
    source_key, company, 'default',
    lower(regexp_replace(btrim(company), '\s+', ' ', 'g')) || '::default',
    source_key, first_seen_at, last_seen_at, missing_successful_runs,
    lifecycle_status, removed_at
from public.tracked_jobs
on conflict (source_key, source_scope_key) do nothing;

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

drop function if exists public.begin_student_job_source_sync(
    uuid, uuid, text, integer, integer, bigint, boolean, text[], integer, timestamptz
);

create or replace function public.begin_student_job_source_sync(
    p_sync_id uuid,
    p_scan_id uuid,
    p_company text,
    p_source_scope text,
    p_source_scope_key text,
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
    if nullif(btrim(p_company), '') is null
       or nullif(btrim(p_source_scope), '') is null
       or nullif(btrim(p_source_scope_key), '') is null then
        raise exception 'company, source scope, and source scope key must not be empty';
    end if;
    if p_source_scope !~ '^[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?(?::[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?)?$' then
        raise exception 'source scope must be a normalized machine identifier';
    end if;
    if p_source_scope_key is distinct from
       lower(regexp_replace(btrim(p_company), '\s+', ' ', 'g')) || '::' || p_source_scope then
        raise exception 'source scope key does not match canonical company and source scope';
    end if;
    if p_expected_chunks < 0 or p_expected_rows < 0 or p_expected_bytes < 0 then
        raise exception 'expected chunk, row, and byte counts must be nonnegative';
    end if;
    if p_close_after_missing_runs < 1 then
        raise exception 'p_close_after_missing_runs must be at least 1';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        p_scan_id::text || ':' || p_source_scope_key, 0
    ));
    select result into v_prior_result
    from public.scan_source_syncs
    where scan_id = p_scan_id and company = p_company
      and source_scope = p_source_scope;

    insert into public.job_sync_sessions (
        sync_id, scan_id, company, source_scope, source_scope_key,
        expected_chunks, expected_rows, expected_bytes, reconcile_absence,
        close_after_missing_runs, seen_at, status, result, finalized_at,
        protected_source_keys
    ) values (
        p_sync_id, p_scan_id, p_company, p_source_scope, p_source_scope_key,
        p_expected_chunks, p_expected_rows, p_expected_bytes, p_reconcile_absence,
        p_close_after_missing_runs, p_seen_at,
        case when v_prior_result is null then 'staging' else 'finalized' end,
        v_prior_result, case when v_prior_result is null then null else now() end,
        coalesce(p_protected_source_keys, '{}')
    ) on conflict (sync_id) do nothing
    returning true into v_created;

    select * into v_session from public.job_sync_sessions
    where sync_id = p_sync_id for update;
    if v_session.scan_id is distinct from p_scan_id
       or v_session.company is distinct from p_company
       or v_session.source_scope is distinct from p_source_scope
       or v_session.source_scope_key is distinct from p_source_scope_key
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
        'status', v_session.status, 'result', v_session.result,
        'replayed', not coalesce(v_created, false)
    );
end;
$$;

-- Staging remains chunk-idempotent. New scope columns are copied from the
-- validated payload and must match the owning session.
create or replace function public.stage_student_job_source_chunk(
    p_sync_id uuid, p_chunk_number integer, p_chunk_rows integer,
    p_chunk_bytes bigint, p_payload_hash text, p_jobs jsonb
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
    select * into v_session from public.job_sync_sessions
    where sync_id = p_sync_id for update;
    if not found then raise exception 'unknown sync ID'; end if;
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
    select * into v_chunk from public.job_sync_chunks
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
        select 1 from jsonb_to_recordset(p_jobs)
          as item(company text, source_scope text, source_scope_key text)
        where item.company is distinct from v_session.company
           or item.source_scope is distinct from v_session.source_scope
           or item.source_scope_key is distinct from v_session.source_scope_key
    ) then raise exception 'incoming jobs must all belong to the sync source scope'; end if;
    if exists (
        select 1 from jsonb_to_recordset(p_jobs) as item(source_key text)
        join public.job_sync_stage staged
          on staged.sync_id = p_sync_id and staged.source_key = item.source_key
    ) then raise exception 'a source key was staged in more than one chunk'; end if;

    insert into public.job_sync_stage (
        sync_id, company, source_scope, source_scope_key, provider_source_key,
        chunk_number, source_key, title, location, department, apply_url,
        source_url, source_platform, primary_category, matched_categories,
        relevance_score, relevance_band, positive_signals, negative_signals,
        location_status, source_updated_at, source_posted_at, source_closes_at,
        first_seen_at, last_seen_at, first_seen_basis, removed_at, status,
        lifecycle_status, missing_successful_runs, freshness_age_source, age_days,
        first_seen_age_hours, freshness_band, deadline_days_remaining,
        urgency_band, urgency_score, urgency_reasons, date_parse_errors,
        is_ranked_output, raw_data
    )
    select p_sync_id, item.company, item.source_scope, item.source_scope_key,
        item.provider_source_key, p_chunk_number, item.source_key, item.title,
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
        source_key text, provider_source_key text, company text,
        source_scope text, source_scope_key text, title text, location text,
        department text, apply_url text, source_url text, source_platform text,
        primary_category text, matched_categories jsonb, relevance_score integer,
        relevance_band text, positive_signals jsonb, negative_signals jsonb,
        location_status text, source_updated_at timestamptz, source_posted_at text,
        source_closes_at text, first_seen_at timestamptz, last_seen_at timestamptz,
        first_seen_basis text, removed_at timestamptz, status text,
        lifecycle_status text, missing_successful_runs integer,
        freshness_age_source text, age_days double precision,
        first_seen_age_hours double precision, freshness_band text,
        deadline_days_remaining double precision, urgency_band text,
        urgency_score integer, urgency_reasons jsonb, date_parse_errors jsonb,
        is_ranked_output boolean, raw_data jsonb
    );
    insert into public.job_sync_chunks
        (sync_id, chunk_number, row_count, payload_bytes, payload_hash)
    values (p_sync_id, p_chunk_number, p_chunk_rows, p_chunk_bytes, p_payload_hash);
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
    v_chunks integer; v_rows bigint; v_bytes bigint; v_stage_rows bigint;
    v_inserted integer := 0; v_updated integer := 0; v_unchanged integer := 0;
    v_missing_incremented integer := 0; v_closed integer := 0; v_result jsonb;
begin
    select * into v_session from public.job_sync_sessions where sync_id = p_sync_id;
    if not found then raise exception 'unknown sync ID'; end if;
    perform pg_advisory_xact_lock(hashtextextended(
        v_session.scan_id::text || ':' || v_session.source_scope_key, 0
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
    from public.job_sync_stage where sync_id=p_sync_id
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

    insert into public.tracked_job_source_scopes (
        ownership_key,source_key,canonical_company,source_scope,source_scope_key,
        provider_source_key,first_seen_at,last_seen_at,missing_successful_runs,
        lifecycle_status,removed_at
    ) select encode(digest(source_key || E'\x1f' || source_scope_key,'sha256'),'hex'),
        source_key,company,source_scope,source_scope_key,provider_source_key,
        first_seen_at,last_seen_at,missing_successful_runs,lifecycle_status,removed_at
    from public.job_sync_stage where sync_id=p_sync_id
    on conflict(source_key,source_scope_key) do update set
        provider_source_key=excluded.provider_source_key,
        last_seen_at=excluded.last_seen_at, missing_successful_runs=0,
        lifecycle_status=excluded.lifecycle_status, removed_at=excluded.removed_at;

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
        returning owner.source_key
    ) select count(*) into v_missing_incremented from missing;

    update public.tracked_jobs tracked set
        status=case when aggregate.has_active then 'active'
                    when aggregate.has_possible then 'active' else 'closed' end,
        lifecycle_status=case when aggregate.has_active then 'active'
                    when aggregate.has_possible then 'possibly_missing' else 'removed' end,
        missing_successful_runs=aggregate.minimum_missing,
        removed_at=case when aggregate.has_active or aggregate.has_possible then null
                        else coalesce(tracked.removed_at,v_session.seen_at) end
    from (
        select source_key,
            bool_or(lifecycle_status='active') as has_active,
            bool_or(lifecycle_status='possibly_missing') as has_possible,
            min(missing_successful_runs) as minimum_missing
        from public.tracked_job_source_scopes group by source_key
    ) aggregate
    where tracked.source_key=aggregate.source_key
      and (tracked.source_key in (select source_key from public.job_sync_stage where sync_id=p_sync_id)
           or tracked.source_key in (select source_key from public.tracked_job_source_scopes
                                     where source_scope_key=v_session.source_scope_key));

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

create or replace function public.retire_student_job_source_scope(
    p_company text,
    p_source_scope text,
    p_source_scope_key text,
    p_reason text,
    p_approved_by text,
    p_retired_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_owned integer := 0;
    v_retired integer := 0;
    v_closed integer := 0;
begin
    if nullif(btrim(p_company), '') is null
       or nullif(btrim(p_source_scope), '') is null
       or nullif(btrim(p_source_scope_key), '') is null
       or length(btrim(coalesce(p_reason, ''))) < 8
       or length(btrim(coalesce(p_approved_by, ''))) < 2
       or p_retired_at is null then
        raise exception 'source retirement requires company, scope, exact key, reason, approver, and timestamp';
    end if;
    if p_source_scope !~ '^[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?(?::[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?)?$' then
        raise exception 'source scope must be a normalized machine identifier';
    end if;
    if p_source_scope_key is distinct from
       lower(regexp_replace(btrim(p_company), '\s+', ' ', 'g')) || '::' || p_source_scope then
        raise exception 'source scope key does not match canonical company and source scope';
    end if;

    perform pg_advisory_xact_lock(hashtextextended('retire:' || p_source_scope_key, 0));
    insert into public.source_scope_retirements (
        source_scope_key, canonical_company, source_scope, reason,
        approved_by, retired_at
    ) values (
        p_source_scope_key, p_company, p_source_scope, btrim(p_reason),
        btrim(p_approved_by), p_retired_at
    ) on conflict (source_scope_key) do nothing;

    select count(*) into v_owned
    from public.tracked_job_source_scopes
    where source_scope_key = p_source_scope_key;

    with retired as (
        update public.tracked_job_source_scopes owner set
            lifecycle_status = 'removed',
            removed_at = coalesce(owner.removed_at, p_retired_at)
        where owner.source_scope_key = p_source_scope_key
          and owner.lifecycle_status <> 'removed'
        returning owner.source_key
    ) select count(*) into v_retired from retired;

    update public.tracked_jobs tracked set
        status = case when aggregate.has_active or aggregate.has_possible
            then 'active' else 'closed' end,
        lifecycle_status = case when aggregate.has_active then 'active'
            when aggregate.has_possible then 'possibly_missing' else 'removed' end,
        missing_successful_runs = aggregate.minimum_missing,
        removed_at = case when aggregate.has_active or aggregate.has_possible
            then null else coalesce(tracked.removed_at, p_retired_at) end
    from (
        select source_key,
            bool_or(lifecycle_status = 'active') as has_active,
            bool_or(lifecycle_status = 'possibly_missing') as has_possible,
            min(missing_successful_runs) as minimum_missing
        from public.tracked_job_source_scopes
        group by source_key
    ) aggregate
    where tracked.source_key = aggregate.source_key
      and tracked.source_key in (
          select source_key from public.tracked_job_source_scopes
          where source_scope_key = p_source_scope_key
      );

    select count(*) into v_closed
    from public.tracked_jobs tracked
    where tracked.status = 'closed'
      and tracked.source_key in (
          select source_key from public.tracked_job_source_scopes
          where source_scope_key = p_source_scope_key
      );
    return jsonb_build_object(
        'source_scope_key', p_source_scope_key,
        'owned', v_owned,
        'owners_retired', v_retired,
        'canonical_jobs_closed', v_closed,
        'replayed', v_retired = 0
    );
end;
$$;

revoke all on function public.begin_student_job_source_sync(
    uuid,uuid,text,text,text,integer,integer,bigint,boolean,text[],integer,timestamptz
) from public,anon,authenticated;
grant execute on function public.begin_student_job_source_sync(
    uuid,uuid,text,text,text,integer,integer,bigint,boolean,text[],integer,timestamptz
) to service_role;
revoke all on function public.stage_student_job_source_chunk(
    uuid,integer,integer,bigint,text,jsonb
) from public,anon,authenticated;
grant execute on function public.stage_student_job_source_chunk(
    uuid,integer,integer,bigint,text,jsonb
) to service_role;
revoke all on function public.finalize_student_job_source_sync(uuid)
    from public,anon,authenticated;
grant execute on function public.finalize_student_job_source_sync(uuid)
    to service_role;
revoke all on function public.retire_student_job_source_scope(
    text,text,text,text,text,timestamptz
) from public,anon,authenticated;
grant execute on function public.retire_student_job_source_scope(
    text,text,text,text,text,timestamptz
) to service_role;

comment on table public.tracked_job_source_scopes is
    'Per-logical-source-scope ownership and absence state for canonical tracked jobs.';
comment on table public.source_scope_retirements is
    'Explicit administrative evidence for intentionally retired logical source scopes.';
comment on column public.job_sync_sessions.source_scope_key is
    'Stable canonical-company plus logical-scope identity; independent of source configuration fingerprint.';
comment on function public.finalize_student_job_source_sync(uuid) is
    'Atomically applies one logical source scope, reconciles only its ownership rows, then derives canonical job activity across all owners.';
comment on function public.retire_student_job_source_scope(text,text,text,text,text,timestamptz) is
    'Explicitly retires one logical source scope; never inferred from scan failure, cooldown, or registry absence.';

commit;
