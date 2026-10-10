-- Phase 5 bounded retention for current jobs and operational sync history.
-- Apply only after the Phase 5 multi-scope lifecycle migration. This migration
-- installs maintenance capability; it never invokes cleanup.

begin;

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

commit;
