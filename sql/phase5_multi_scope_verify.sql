-- READ ONLY post-migration verification. Every `ok` value must be true.
begin transaction read only;

select 'tracked_jobs_default_scope' check_name,
       count(*) filter (where source_scope='default') = count(*) as ok,
       count(*) as checked_rows
from public.tracked_jobs
union all
select 'source_health_default_scope',
       count(*) filter (where source_scope='default') = count(*), count(*)
from public.job_source_health
union all
select 'ownership_complete',
       (select count(*) from public.tracked_job_source_scopes) =
       (select count(*) from public.tracked_jobs),
       (select count(*) from public.tracked_jobs)
union all
select 'ownership_not_orphaned',
       count(*) = 0, count(*)
from public.tracked_job_source_scopes owner
left join public.tracked_jobs tracked using(source_key)
where tracked.source_key is null
union all
select 'ownership_not_duplicated',
       count(*) = 0, count(*)
from (
    select source_key,source_scope_key from public.tracked_job_source_scopes
    group by source_key,source_scope_key having count(*) > 1
) duplicates
union all
select 'ownership_lifecycle_parity',
       count(*) = 0, count(*)
from public.tracked_jobs tracked
join public.tracked_job_source_scopes owner using(source_key)
where owner.source_scope_key = lower(regexp_replace(btrim(tracked.company),'\s+',' ','g')) || '::default'
  and (owner.missing_successful_runs is distinct from tracked.missing_successful_runs
       or owner.lifecycle_status is distinct from tracked.lifecycle_status
       or owner.removed_at is distinct from tracked.removed_at)
union all
select 'staged_sessions_have_scope_identity', count(*)=0, count(*)
from public.job_sync_sessions
where source_scope is null or source_scope_key is null
union all
select 'staged_rows_have_provider_identity', count(*)=0, count(*)
from public.job_sync_stage
where source_scope is null or source_scope_key is null or provider_source_key is null;

select to_regprocedure(
    'public.begin_student_job_source_sync(uuid,uuid,text,text,text,integer,integer,bigint,boolean,text[],integer,timestamp with time zone)'
) is not null as scope_begin_rpc_installed,
to_regprocedure('public.stage_student_job_source_chunk(uuid,integer,integer,bigint,text,jsonb)') is not null as stage_rpc_installed,
to_regprocedure('public.finalize_student_job_source_sync(uuid)') is not null as finalize_rpc_installed,
to_regprocedure('public.retire_student_job_source_scope(text,text,text,text,text,timestamp with time zone)') is not null as retirement_rpc_installed;

select c.relname,
       c.relrowsecurity as rls_enabled,
       has_table_privilege('service_role', c.oid, 'SELECT,INSERT,UPDATE,DELETE') as service_role_dml
from pg_class c
join pg_namespace n on n.oid=c.relnamespace
where n.nspname='public'
  and c.relname in ('tracked_job_source_scopes','source_scope_retirements')
order by c.relname;

rollback;
