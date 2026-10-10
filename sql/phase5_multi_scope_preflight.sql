-- READ ONLY Phase 5 multi-scope migration preflight.
-- Run with psql -X -v ON_ERROR_STOP=1 -f sql/phase5_multi_scope_preflight.sql.

begin transaction read only;

do $preflight$
declare
    missing_tables text[];
    new_artifacts integer;
begin
    select array_agg(name order by name) into missing_tables
    from unnest(array[
        'job_source_health', 'tracked_jobs', 'job_sync_sessions',
        'job_sync_chunks', 'job_sync_stage', 'scan_source_syncs'
    ]) as required(name)
    where to_regclass('public.' || name) is null;
    if missing_tables is not null then
        raise exception 'PHASE5_PREFLIGHT_FAILED missing prerequisite tables: %', missing_tables;
    end if;
    if not exists (
        select 1 from information_schema.columns
        where table_schema='public' and table_name='job_sync_sessions'
          and column_name='protected_source_keys'
    ) then
        raise exception 'PHASE5_PREFLIGHT_FAILED Phase 3 protected_source_keys is missing';
    end if;
    if to_regprocedure(
        'public.begin_student_job_source_sync(uuid,uuid,text,integer,integer,bigint,boolean,text[],integer,timestamp with time zone)'
    ) is null then
        raise exception 'PHASE5_PREFLIGHT_FAILED expected Phase 3 begin RPC is missing';
    end if;
    if to_regprocedure(
        'public.stage_student_job_source_chunk(uuid,integer,integer,bigint,text,jsonb)'
    ) is null or to_regprocedure(
        'public.finalize_student_job_source_sync(uuid)'
    ) is null then
        raise exception 'PHASE5_PREFLIGHT_FAILED staged-sync prerequisite RPCs are missing';
    end if;

    select count(*) into new_artifacts from (
        select 1 where exists (select 1 from information_schema.columns where table_schema='public' and table_name='job_source_health' and column_name='source_scope')
        union all select 1 where exists (select 1 from information_schema.columns where table_schema='public' and table_name='tracked_jobs' and column_name='source_scope')
        union all select 1 where exists (select 1 from information_schema.columns where table_schema='public' and table_name='job_sync_sessions' and column_name='source_scope_key')
        union all select 1 where exists (select 1 from information_schema.columns where table_schema='public' and table_name='job_sync_stage' and column_name='provider_source_key')
        union all select 1 where to_regclass('public.tracked_job_source_scopes') is not null
        union all select 1 where to_regclass('public.source_scope_retirements') is not null
        union all select 1 where to_regprocedure('public.begin_student_job_source_sync(uuid,uuid,text,text,text,integer,integer,bigint,boolean,text[],integer,timestamp with time zone)') is not null
    ) artifacts;
    if new_artifacts <> 0 then
        raise exception 'PHASE5_PREFLIGHT_FAILED incompatible partial/already-applied state (%/7 new artifacts); use post-migration verification if 7/7', new_artifacts;
    end if;
    if not exists (
        select 1 from pg_extension where extname='pgcrypto'
    ) then
        raise exception 'PHASE5_PREFLIGHT_FAILED pgcrypto is required for ownership keys';
    end if;
end
$preflight$;

select 'PHASE3_READY_FOR_PHASE5' as migration_state,
       (select count(*) from public.tracked_jobs) as tracked_jobs_before,
       (select count(*) from public.job_sync_sessions where status='staging') as staged_sessions_before,
       (select count(*) from public.job_source_health) as source_health_rows_before;

rollback;
