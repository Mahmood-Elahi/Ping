-- Bound source finalization to rows whose lifecycle can actually change.
--
-- The index is built concurrently because this migration targets an existing
-- production table. Run this file with autocommit enabled; migration runners
-- that wrap an entire file in one transaction cannot execute CONCURRENTLY.

do $preflight$
declare
    v_target_oid oid := pg_catalog.to_regprocedure(
        'public.finalize_student_job_source_sync(uuid)'
    );
    v_owner_name text;
    v_language_name text;
    v_prokind "char";
    v_security_definer boolean;
    v_function_config text[];
    v_returns_set boolean;
    v_return_type oid;
    v_normalized_body_md5 text;
    v_execute_acl text[];
    v_postgres_can_execute boolean;
    v_service_role_can_execute boolean;
    v_anon_can_execute boolean;
    v_authenticated_can_execute boolean;
    v_overloads text[];
    v_exact_overload_inventory boolean;
begin
    if pg_catalog.to_regclass('public.job_sync_stage') is null
       or pg_catalog.to_regclass('public.tracked_jobs') is null
       or pg_catalog.to_regclass('public.tracked_job_source_scopes') is null
       or pg_catalog.to_regclass('public.source_scope_retirements') is null then
        raise exception 'SOURCE_SYNC_OPTIMIZATION_PREFLIGHT_FAILED missing staged-sync objects';
    end if;

    select
        coalesce(
            array_agg(
                pg_catalog.format(
                    '%I.%I(%s) -> %s',
                    procedure_namespace.nspname,
                    procedure_state.proname,
                    pg_catalog.pg_get_function_identity_arguments(procedure_state.oid),
                    pg_catalog.pg_get_function_result(procedure_state.oid)
                )
                order by procedure_state.oid::regprocedure::text
            ),
            array[]::text[]
        ),
        count(*)=1 and bool_and(
            procedure_state.oid=v_target_oid
            and procedure_state.pronargs=1
            and procedure_state.proargtypes[0]='uuid'::regtype::oid
            and procedure_state.proallargtypes is null
            and procedure_state.proargmodes is null
            and procedure_state.provariadic=0
            and procedure_state.pronargdefaults=0
            and not procedure_state.proretset
            and procedure_state.prorettype='jsonb'::regtype
        )
      into v_overloads,v_exact_overload_inventory
    from pg_catalog.pg_proc procedure_state
    join pg_catalog.pg_namespace procedure_namespace
      on procedure_namespace.oid=procedure_state.pronamespace
    where procedure_namespace.nspname='public'
      and procedure_state.proname='finalize_student_job_source_sync';

    if not coalesce(v_exact_overload_inventory,false) then
        raise exception
            'SOURCE_SYNC_OPTIMIZATION_BASELINE_OVERLOAD_MISMATCH expected public.finalize_student_job_source_sync(uuid) -> jsonb only; found %',
            v_overloads;
    end if;

    select
        pg_catalog.pg_get_userbyid(procedure_state.proowner),
        language_state.lanname,
        procedure_state.prokind,
        procedure_state.prosecdef,
        procedure_state.proconfig,
        procedure_state.proretset,
        procedure_state.prorettype,
        pg_catalog.md5(pg_catalog.btrim(pg_catalog.regexp_replace(
            pg_catalog.btrim(procedure_state.prosrc),
            '[[:space:]]+', ' ', 'g'
        ))),
        pg_catalog.has_function_privilege(
            'postgres',procedure_state.oid,'EXECUTE'
        ),
        pg_catalog.has_function_privilege(
            'service_role',procedure_state.oid,'EXECUTE'
        ),
        pg_catalog.has_function_privilege(
            'anon',procedure_state.oid,'EXECUTE'
        ),
        pg_catalog.has_function_privilege(
            'authenticated',procedure_state.oid,'EXECUTE'
        )
      into v_owner_name,v_language_name,v_prokind,v_security_definer,
           v_function_config,v_returns_set,v_return_type,v_normalized_body_md5,
           v_postgres_can_execute,v_service_role_can_execute,
           v_anon_can_execute,v_authenticated_can_execute
    from pg_catalog.pg_proc procedure_state
    join pg_catalog.pg_language language_state
      on language_state.oid=procedure_state.prolang
    where procedure_state.oid=v_target_oid;

    select coalesce(
        array_agg(
            (case when expanded_acl.grantee=0 then 'PUBLIC'
                  else grantee_role.rolname end)
            || ':EXECUTE:' || expanded_acl.is_grantable::text
            order by case when expanded_acl.grantee=0 then 'PUBLIC'
                          else grantee_role.rolname end
        ) filter (where expanded_acl.privilege_type='EXECUTE'),
        array[]::text[]
    )
      into v_execute_acl
    from pg_catalog.pg_proc procedure_state
    cross join lateral pg_catalog.aclexplode(
        coalesce(
            procedure_state.proacl,
            pg_catalog.acldefault('f',procedure_state.proowner)
        )
    ) expanded_acl
    left join pg_catalog.pg_roles grantee_role
      on grantee_role.oid=expanded_acl.grantee
    where procedure_state.oid=v_target_oid;

    if v_owner_name is distinct from 'postgres'
       or v_language_name is distinct from 'plpgsql'
       or v_prokind is distinct from 'f'
       or v_security_definer is distinct from true
       or v_function_config is distinct from
            array['search_path=public, extensions, pg_temp']::text[]
       or v_returns_set is distinct from false
       or v_return_type is distinct from 'jsonb'::regtype
       -- PostgreSQL owners retain inherent grant authority even when their
       -- materialized ACL row reports is_grantable=false. Require the exact
       -- production ACL plus effective owner/service execution, rather than
       -- inventing a true grant-option bit for the owner.
       or v_execute_acl is distinct from array[
            'postgres:EXECUTE:false',
            'service_role:EXECUTE:false'
        ]::text[]
       or v_postgres_can_execute is distinct from true
       or v_service_role_can_execute is distinct from true
       or v_anon_can_execute is distinct from false
       or v_authenticated_can_execute is distinct from false then
        raise exception
            'SOURCE_SYNC_OPTIMIZATION_BASELINE_CONTRACT_MISMATCH owner=%, language=%, kind=%, security_definer=%, config=%, return_type=%, execute_acl=%, postgres_can_execute=%, service_role_can_execute=%, anon_can_execute=%, authenticated_can_execute=%',
            v_owner_name,v_language_name,v_prokind,v_security_definer,
            v_function_config,v_return_type::regtype,v_execute_acl,
            v_postgres_can_execute,v_service_role_can_execute,
            v_anon_can_execute,v_authenticated_can_execute;
    end if;

    -- Accept the exact preceding Phase 5 body or this migration's exact body.
    -- The latter is the safe rerun path after a verified successful install.
    if v_normalized_body_md5 not in (
        '9516024979ccc1d34a298c0be5d63b32',
        '3b2713fee873d93c306b4a39091199fb'
    ) then
        raise exception
            'SOURCE_SYNC_OPTIMIZATION_BASELINE_BODY_MISMATCH normalized_body_md5=%',
            v_normalized_body_md5;
    end if;
end
$preflight$;

create index concurrently if not exists tracked_job_source_scopes_scope_source_idx
    on public.tracked_job_source_scopes (source_scope_key, source_key);

do $index_check$
begin
    if not exists (
        select 1
        from pg_catalog.pg_index index_state
        join pg_catalog.pg_class index_class
          on index_class.oid=index_state.indexrelid
        join pg_catalog.pg_namespace index_namespace
          on index_namespace.oid=index_class.relnamespace
        join pg_catalog.pg_class table_class
          on table_class.oid=index_state.indrelid
        join pg_catalog.pg_namespace table_namespace
          on table_namespace.oid=table_class.relnamespace
        join pg_catalog.pg_am access_method
          on access_method.oid=index_class.relam
        where index_namespace.nspname='public'
          and index_class.relname='tracked_job_source_scopes_scope_source_idx'
          and index_class.relkind='i'
          and table_namespace.nspname='public'
          and table_class.relname='tracked_job_source_scopes'
          and access_method.amname='btree'
          and not index_state.indisunique
          and index_state.indnkeyatts=2
          and index_state.indnatts=2
          and index_state.indexprs is null
          and index_state.indpred is null
          and pg_catalog.pg_get_indexdef(index_state.indexrelid,1,true)='source_scope_key'
          and pg_catalog.pg_get_indexdef(index_state.indexrelid,2,true)='source_key'
          and index_state.indisvalid
          and index_state.indisready
          and index_state.indislive
    ) then
        raise exception 'SOURCE_SYNC_OPTIMIZATION_INDEX_INVALID_OR_WRONG_DEFINITION drop the named index concurrently and retry';
    end if;
end
$index_check$;

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

revoke all on function public.finalize_student_job_source_sync(uuid)
    from public,anon,authenticated;
grant execute on function public.finalize_student_job_source_sync(uuid)
    to service_role;

comment on function public.finalize_student_job_source_sync(uuid) is
    'Atomically applies one logical source scope, serializes finalization with retirement, and bounds canonical lifecycle reconciliation to observed or transitioned keys.';

commit;
