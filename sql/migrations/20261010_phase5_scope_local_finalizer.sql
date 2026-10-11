-- Phase 5 forward repair: keep canonical lifecycle derivation bounded to the
-- jobs affected by one finalized source scope. Shared jobs still aggregate
-- every owner for each affected canonical source key.

begin;

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

    with affected as (
        select source_key
        from public.job_sync_stage
        where sync_id=p_sync_id
        union
        select source_key
        from public.tracked_job_source_scopes
        where source_scope_key=v_session.source_scope_key
    ), aggregate as (
        select owner.source_key,
            bool_or(owner.lifecycle_status='active') as has_active,
            bool_or(owner.lifecycle_status='possibly_missing') as has_possible,
            min(owner.missing_successful_runs) as minimum_missing
        from affected
        join public.tracked_job_source_scopes owner using(source_key)
        group by owner.source_key
    )
    update public.tracked_jobs tracked set
        status=case when aggregate.has_active then 'active'
                    when aggregate.has_possible then 'active' else 'closed' end,
        lifecycle_status=case when aggregate.has_active then 'active'
                    when aggregate.has_possible then 'possibly_missing' else 'removed' end,
        missing_successful_runs=aggregate.minimum_missing,
        removed_at=case when aggregate.has_active or aggregate.has_possible then null
                        else coalesce(tracked.removed_at,v_session.seen_at) end
    from aggregate
    where tracked.source_key=aggregate.source_key;

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

revoke all on function public.finalize_student_job_source_sync(uuid)
    from public,anon,authenticated;
grant execute on function public.finalize_student_job_source_sync(uuid)
    to service_role;

comment on function public.finalize_student_job_source_sync(uuid) is
    'Atomically applies one logical source scope, reconciles only its ownership rows, then derives canonical lifecycle across all owners for affected keys only.';

commit;
