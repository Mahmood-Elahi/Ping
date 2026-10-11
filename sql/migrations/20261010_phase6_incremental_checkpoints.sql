-- Phase 6: bounded incremental checkpoints committed atomically with accepted
-- source observations. This migration creates architecture only; no source is
-- activated for incremental crawling.

begin;

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

comment on table public.source_incremental_checkpoints is
    'Bounded source/config/strategy checkpoints. Optimization state only; never lifecycle truth.';

alter table public.source_incremental_checkpoints enable row level security;
revoke all on table public.source_incremental_checkpoints from public, anon, authenticated;
grant all on table public.source_incremental_checkpoints to service_role;

alter table public.job_sync_sessions
    add column if not exists checkpoint_candidate jsonb;

-- Preserve the Phase 5 RPC and extend it with an optional bounded candidate.
-- The durable candidate is not accepted as a checkpoint until finalization.
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
    p_seen_at timestamptz,
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
    uuid, uuid, text, text, text, integer, integer, bigint, boolean,
    text[], integer, timestamptz, jsonb
) from public, anon, authenticated;
grant execute on function public.begin_student_job_source_sync(
    uuid, uuid, text, text, text, integer, integer, bigint, boolean,
    text[], integer, timestamptz, jsonb
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
    if new.checkpoint_candidate is null then
        return new;
    end if;
    v_candidate := new.checkpoint_candidate;
    if v_candidate->>'source_scope_key' is distinct from new.source_scope_key then
        raise exception 'checkpoint candidate source scope mismatch at finalization';
    end if;
    insert into public.source_incremental_checkpoints (
        source_scope_key, provider_identity, config_fingerprint,
        strategy_version, cursor, timestamp_watermark,
        last_successful_full_at, last_successful_incremental_at,
        last_forced_full_reason, representation_token, snapshot_checksum,
        ambiguous_since_last_full, updated_at
    ) values (
        new.source_scope_key,
        v_candidate->>'provider_identity',
        v_candidate->>'config_fingerprint',
        v_candidate->>'strategy_version',
        v_candidate->>'cursor',
        (v_candidate->>'timestamp_watermark')::timestamptz,
        (v_candidate->>'last_successful_full_at')::timestamptz,
        (v_candidate->>'last_successful_incremental_at')::timestamptz,
        v_candidate->>'last_forced_full_reason',
        v_candidate->>'representation_token',
        v_candidate->>'snapshot_checksum',
        coalesce((v_candidate->>'ambiguous_since_last_full')::boolean, false),
        coalesce((v_candidate->>'updated_at')::timestamptz, now())
    )
    on conflict (source_scope_key, provider_identity) do update set
        config_fingerprint = excluded.config_fingerprint,
        strategy_version = excluded.strategy_version,
        cursor = excluded.cursor,
        timestamp_watermark = excluded.timestamp_watermark,
        last_successful_full_at = excluded.last_successful_full_at,
        last_successful_incremental_at = excluded.last_successful_incremental_at,
        last_forced_full_reason = excluded.last_forced_full_reason,
        representation_token = excluded.representation_token,
        snapshot_checksum = excluded.snapshot_checksum,
        ambiguous_since_last_full = excluded.ambiguous_since_last_full,
        updated_at = excluded.updated_at;
    return new;
end;
$$;

revoke all on function public.commit_student_job_incremental_checkpoint()
    from public, anon, authenticated;
grant execute on function public.commit_student_job_incremental_checkpoint()
    to service_role;

drop trigger if exists commit_student_job_incremental_checkpoint
    on public.job_sync_sessions;
create trigger commit_student_job_incremental_checkpoint
after update of status on public.job_sync_sessions
for each row
when (new.status = 'finalized' and old.status is distinct from new.status)
execute function public.commit_student_job_incremental_checkpoint();

commit;
