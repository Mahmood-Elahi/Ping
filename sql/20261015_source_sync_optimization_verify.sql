-- READ ONLY pre/post-deployment inspection for the source-sync optimization.
--
-- Run this file before deployment and save all result sets as the rollback
-- record. Only PASS_BASELINE_READY authorizes applying the migration. Run it
-- again afterward; only PASS_OPTIMIZED authorizes a controlled production
-- verification. Every STOP_* result requires review. Hashes below cover the
-- normalized deployed PL/pgSQL body (pg_proc.prosrc), not merely its signature.

begin transaction read only;
set local search_path = pg_catalog;

-- Exact deployed function and overload snapshot. Preserve this output with the
-- backup/recovery checkpoint so a non-baseline deployment can be restored byte
-- for byte rather than reconstructed from assumptions.
select
    pg_catalog.format(
        '%I.%I(%s)',
        procedure_namespace.nspname,
        procedure_state.proname,
        pg_catalog.pg_get_function_identity_arguments(procedure_state.oid)
    ) as function_identity,
    pg_catalog.pg_get_userbyid(procedure_state.proowner) as function_owner,
    language_state.lanname as function_language,
    procedure_state.prosecdef as security_definer,
    procedure_state.proconfig as function_config,
    procedure_state.proacl as function_acl,
    md5(pg_catalog.pg_get_functiondef(procedure_state.oid)) as function_definition_md5,
    md5(btrim(regexp_replace(btrim(procedure_state.prosrc), '[[:space:]]+', ' ', 'g')))
        as normalized_function_body_md5,
    pg_catalog.pg_get_functiondef(procedure_state.oid) as function_definition
from pg_catalog.pg_proc procedure_state
join pg_catalog.pg_namespace procedure_namespace
  on procedure_namespace.oid=procedure_state.pronamespace
join pg_catalog.pg_language language_state
  on language_state.oid=procedure_state.prolang
where procedure_namespace.nspname='public'
  and procedure_state.proname='finalize_student_job_source_sync'
order by procedure_state.oid::regprocedure::text;

-- Preserve checkpoint-trigger definitions and enabled state across rollout.
select
    trigger_namespace.nspname as trigger_table_schema,
    trigger_table.relname as trigger_table,
    trigger_state.tgname as trigger_name,
    trigger_state.tgenabled as trigger_enabled,
    trigger_state.tgfoid::regprocedure as trigger_function,
    pg_catalog.pg_get_triggerdef(trigger_state.oid,true) as trigger_definition
from pg_catalog.pg_trigger trigger_state
join pg_catalog.pg_class trigger_table
  on trigger_table.oid=trigger_state.tgrelid
join pg_catalog.pg_namespace trigger_namespace
  on trigger_namespace.oid=trigger_table.relnamespace
where not trigger_state.tgisinternal
  and (
      trigger_state.tgfoid=pg_catalog.to_regprocedure(
          'public.commit_student_job_incremental_checkpoint()'
      )
      or (
          trigger_state.tgrelid=pg_catalog.to_regclass('public.job_sync_sessions')
          and trigger_state.tgname='commit_student_job_incremental_checkpoint'
      )
  )
order by trigger_state.tgname;

with function_target as (
    select
        procedure_state.*,
        pg_catalog.pg_get_userbyid(procedure_state.proowner) as owner_name,
        language_state.lanname as language_name,
        md5(btrim(regexp_replace(btrim(procedure_state.prosrc), '[[:space:]]+', ' ', 'g')))
            as normalized_body_md5,
        pg_catalog.has_function_privilege(
            'postgres',procedure_state.oid,'EXECUTE'
        ) as postgres_can_execute,
        pg_catalog.has_function_privilege(
            'service_role',procedure_state.oid,'EXECUTE'
        ) as service_role_can_execute,
        pg_catalog.has_function_privilege(
            'anon',procedure_state.oid,'EXECUTE'
        ) as anon_can_execute,
        pg_catalog.has_function_privilege(
            'authenticated',procedure_state.oid,'EXECUTE'
        ) as authenticated_can_execute
    from pg_catalog.pg_proc procedure_state
    join pg_catalog.pg_language language_state
      on language_state.oid=procedure_state.prolang
    where procedure_state.oid=pg_catalog.to_regprocedure(
        'public.finalize_student_job_source_sync(uuid)'
    )
), function_acl as (
    select coalesce(
        array_agg(
            (case when expanded_acl.grantee=0 then 'PUBLIC'
                  else grantee_role.rolname end)
            || ':EXECUTE:' || expanded_acl.is_grantable::text
            order by case when expanded_acl.grantee=0 then 'PUBLIC'
                          else grantee_role.rolname end
        ) filter (where expanded_acl.privilege_type='EXECUTE'),
        array[]::text[]
    ) as execute_acl
    from function_target target
    cross join lateral pg_catalog.aclexplode(
        coalesce(target.proacl,pg_catalog.acldefault('f',target.proowner))
    ) expanded_acl
    left join pg_catalog.pg_roles grantee_role
      on grantee_role.oid=expanded_acl.grantee
), overload_state as (
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
        ) as overloads,
        count(*)=1 and bool_and(
            procedure_state.oid=pg_catalog.to_regprocedure(
                'public.finalize_student_job_source_sync(uuid)'
            )
            and procedure_state.pronargs=1
            and procedure_state.proargtypes[0]='uuid'::regtype::oid
            and procedure_state.proallargtypes is null
            and procedure_state.proargmodes is null
            and procedure_state.provariadic=0
            and procedure_state.pronargdefaults=0
            and not procedure_state.proretset
            and procedure_state.prorettype='jsonb'::regtype
        ) as exact_inventory
    from pg_catalog.pg_proc procedure_state
    join pg_catalog.pg_namespace procedure_namespace
      on procedure_namespace.oid=procedure_state.pronamespace
    where procedure_namespace.nspname='public'
      and procedure_state.proname='finalize_student_job_source_sync'
), scope_index_state as (
    select
        count(*) > 0 as name_present,
        count(*)=1 and bool_and(
            index_class.relkind='i'
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
        ) as exact_definition_ready
    from pg_catalog.pg_class index_class
    join pg_catalog.pg_namespace index_namespace
      on index_namespace.oid=index_class.relnamespace
    left join pg_catalog.pg_index index_state
      on index_state.indexrelid=index_class.oid
    left join pg_catalog.pg_class table_class
      on table_class.oid=index_state.indrelid
    left join pg_catalog.pg_namespace table_namespace
      on table_namespace.oid=table_class.relnamespace
    left join pg_catalog.pg_am access_method
      on access_method.oid=index_class.relam
    where index_namespace.nspname='public'
      and index_class.relname='tracked_job_source_scopes_scope_source_idx'
), retention_index_state as (
    select count(*)=1 and bool_and(
        index_class.relkind='i'
        and table_namespace.nspname='public'
        and table_class.relname='tracked_jobs'
        and access_method.amname='btree'
        and not index_state.indisunique
        and index_state.indnkeyatts=2
        and index_state.indnatts=2
        and index_state.indexprs is not null
        and index_state.indpred is not null
        and index_state.indkey[0]=0
        and source_key_attribute.attnum is not null
        and index_state.indkey[1]=source_key_attribute.attnum
        and index_state.indoption[0]=0
        and index_state.indoption[1]=0
        and lower(pg_catalog.pg_get_indexdef(index_state.indexrelid,1,true))=
            'coalesce(removed_at, last_seen_at, first_seen_at)'
        and pg_catalog.pg_get_indexdef(index_state.indexrelid,2,true)='source_key'
        and lower(pg_catalog.regexp_replace(
            pg_catalog.pg_get_expr(index_state.indexprs,index_state.indrelid,false),
            '[[:space:]]+', ' ', 'g'
        ))='coalesce(removed_at, last_seen_at, first_seen_at)'
        and lower(pg_catalog.regexp_replace(
            pg_catalog.pg_get_expr(index_state.indpred,index_state.indrelid,false),
            '[[:space:]]+', ' ', 'g'
        ))=(
            '((lifecycle_status = ''removed''::text) and '
            '(status = ''closed''::text))'
        )
        and index_state.indisvalid
        and index_state.indisready
        and index_state.indislive
    ) as exact_definition_ready
    from pg_catalog.pg_class index_class
    join pg_catalog.pg_namespace index_namespace
      on index_namespace.oid=index_class.relnamespace
    left join pg_catalog.pg_index index_state
      on index_state.indexrelid=index_class.oid
    left join pg_catalog.pg_class table_class
      on table_class.oid=index_state.indrelid
    left join pg_catalog.pg_namespace table_namespace
      on table_namespace.oid=table_class.relnamespace
    left join pg_catalog.pg_am access_method
      on access_method.oid=index_class.relam
    left join pg_catalog.pg_attribute source_key_attribute
      on source_key_attribute.attrelid=table_class.oid
     and source_key_attribute.attname='source_key'
     and not source_key_attribute.attisdropped
    where index_namespace.nspname='public'
      and index_class.relname='tracked_jobs_retention_removed_idx'
), owner_aggregate as (
    select owner.source_key,
        bool_or(owner.lifecycle_status='active') as has_active,
        bool_or(owner.lifecycle_status='possibly_missing') as has_possible,
        min(owner.missing_successful_runs) as minimum_missing,
        bool_or(
            owner.lifecycle_status='active'
            and owner.missing_successful_runs<>0
        ) as has_invalid_active_owner_counter
    from public.tracked_job_source_scopes owner
    group by owner.source_key
), lifecycle_assessment as (
    select
        tracked.source_key,
        (
            aggregate.source_key is null
            or tracked.status is distinct from case
                when aggregate.has_active or aggregate.has_possible then 'active'
                else 'closed' end
            or tracked.lifecycle_status is distinct from case
                when aggregate.has_active then 'active'
                when aggregate.has_possible then 'possibly_missing'
                else 'removed' end
            or tracked.missing_successful_runs is distinct from
                aggregate.minimum_missing
            or ((aggregate.has_active or aggregate.has_possible)
                and tracked.removed_at is not null)
        ) as is_inconsistent,
        (
            aggregate.source_key is not null
            and aggregate.has_active
            and not aggregate.has_invalid_active_owner_counter
            and aggregate.minimum_missing=0
            and tracked.status='active'
            and tracked.lifecycle_status='active'
            and tracked.missing_successful_runs=0
            and tracked.missing_successful_runs is not distinct from
                aggregate.minimum_missing
            and tracked.removed_at is not null
        ) as is_exact_stale_active_removed_at
    from public.tracked_jobs tracked
    left join owner_aggregate aggregate using(source_key)
), lifecycle_drift as (
    select
        count(*) filter (where is_inconsistent) as inconsistent_rows,
        count(*) filter (where is_exact_stale_active_removed_at)
            as exact_stale_active_removed_at_rows,
        count(*) filter (
            where is_inconsistent and not is_exact_stale_active_removed_at
        ) as other_inconsistent_rows
    from lifecycle_assessment
), checkpoint_trigger_state as (
    select
        count(*) as inventory_count,
        coalesce(count(*)=1 and bool_and(
            trigger_namespace.nspname='public'
            and trigger_table.relname='job_sync_sessions'
            and trigger_state.tgname='commit_student_job_incremental_checkpoint'
            and trigger_state.tgfoid=pg_catalog.to_regprocedure(
                'public.commit_student_job_incremental_checkpoint()'
            )
            -- AFTER + UPDATE + ROW, with no INSERT/DELETE/TRUNCATE event bits.
            and trigger_state.tgtype=17
            and trigger_state.tgattr::text=status_attribute.attnum::text
            and trigger_state.tgenabled='O'
            and trigger_state.tgnargs=0
            and pg_catalog.octet_length(trigger_state.tgargs)=0
            and trigger_state.tgconstraint=0
            and not trigger_state.tgdeferrable
            and not trigger_state.tginitdeferred
            and trigger_state.tgoldtable is null
            and trigger_state.tgnewtable is null
            and pg_catalog.regexp_replace(
                pg_catalog.lower(
                    pg_catalog.pg_get_triggerdef(trigger_state.oid,false)
                ),
                '[[:space:]]+', ' ', 'g'
            )=(
                'create trigger commit_student_job_incremental_checkpoint '
                'after update of status on public.job_sync_sessions for each row '
                'when (((new.status = ''finalized''::text) and '
                '(old.status is distinct from new.status))) execute function '
                'public.commit_student_job_incremental_checkpoint()'
            )
        ),false) as exact_and_enabled
    from pg_catalog.pg_trigger trigger_state
    join pg_catalog.pg_class trigger_table
      on trigger_table.oid=trigger_state.tgrelid
    join pg_catalog.pg_namespace trigger_namespace
      on trigger_namespace.oid=trigger_table.relnamespace
    left join pg_catalog.pg_attribute status_attribute
      on status_attribute.attrelid=trigger_table.oid
     and status_attribute.attname='status'
     and not status_attribute.attisdropped
    where not trigger_state.tgisinternal
      and (
          trigger_state.tgfoid=pg_catalog.to_regprocedure(
              'public.commit_student_job_incremental_checkpoint()'
          )
          or (
              trigger_state.tgrelid=pg_catalog.to_regclass('public.job_sync_sessions')
              and trigger_state.tgname='commit_student_job_incremental_checkpoint'
          )
      )
), verified as (
    select
        target.oid is not null as function_exists,
        target.owner_name='postgres'
            and target.language_name='plpgsql'
            and target.prokind='f'
            and target.prosecdef
            and target.proconfig=array['search_path=public, extensions, pg_temp']::text[]
            and pg_catalog.pg_get_function_result(target.oid)='jsonb'
            and acl.execute_acl=array[
                'postgres:EXECUTE:false',
                'service_role:EXECUTE:false'
            ]::text[]
            and target.postgres_can_execute
            and target.service_role_can_execute
            and not target.anon_can_execute
            and not target.authenticated_can_execute
            as exact_function_contract,
        target.normalized_body_md5,
        acl.execute_acl,
        target.postgres_can_execute,
        target.service_role_can_execute,
        target.anon_can_execute,
        target.authenticated_can_execute,
        overload.overloads,
        coalesce(overload.exact_inventory,false) as exact_overload_inventory,
        scope_index.name_present as scope_index_name_present,
        coalesce(scope_index.exact_definition_ready,false) as scope_index_exact_and_ready,
        coalesce(retention_index.exact_definition_ready,false)
            as retention_index_exact_and_ready,
        drift.inconsistent_rows as canonical_lifecycle_inconsistency_count,
        drift.exact_stale_active_removed_at_rows
            as canonical_stale_active_removed_at_count,
        drift.other_inconsistent_rows
            as canonical_other_lifecycle_inconsistency_count,
        checkpoint.inventory_count as checkpoint_trigger_inventory_count,
        checkpoint.exact_and_enabled as checkpoint_trigger_exact_and_enabled
    from (values(1)) singleton(value)
    left join function_target target on true
    cross join function_acl acl
    cross join overload_state overload
    cross join scope_index_state scope_index
    cross join retention_index_state retention_index
    cross join lifecycle_drift drift
    cross join checkpoint_trigger_state checkpoint
)
select
    case
        when not function_exists then 'STOP_FUNCTION_MISSING'
        when not exact_overload_inventory then 'STOP_FUNCTION_OVERLOAD_MISMATCH'
        when not exact_function_contract then 'STOP_FUNCTION_CONTRACT_MISMATCH'
        when not retention_index_exact_and_ready then 'STOP_RETENTION_INDEX_MISMATCH'
        when not checkpoint_trigger_exact_and_enabled then 'STOP_CHECKPOINT_TRIGGER_MISMATCH'
        when canonical_lifecycle_inconsistency_count<>0 then 'STOP_CANONICAL_LIFECYCLE_DRIFT'
        when scope_index_name_present and not scope_index_exact_and_ready
            then 'STOP_SCOPE_INDEX_WRONG_OR_INVALID'
        when normalized_body_md5='9516024979ccc1d34a298c0be5d63b32'
            then 'PASS_BASELINE_READY'
        when normalized_body_md5='3b2713fee873d93c306b4a39091199fb'
             and scope_index_exact_and_ready then 'PASS_OPTIMIZED'
        else 'STOP_FUNCTION_DEFINITION_MISMATCH'
    end as verification_result,
    normalized_body_md5,
    exact_function_contract,
    execute_acl,
    postgres_can_execute,
    service_role_can_execute,
    anon_can_execute,
    authenticated_can_execute,
    overloads,
    exact_overload_inventory,
    scope_index_name_present,
    scope_index_exact_and_ready,
    retention_index_exact_and_ready,
    checkpoint_trigger_inventory_count,
    checkpoint_trigger_exact_and_enabled,
    canonical_lifecycle_inconsistency_count,
    canonical_stale_active_removed_at_count,
    canonical_other_lifecycle_inconsistency_count,
    normalized_body_md5='9516024979ccc1d34a298c0be5d63b32'
        and exact_overload_inventory
        and exact_function_contract
        and retention_index_exact_and_ready
        and checkpoint_trigger_exact_and_enabled
        and canonical_lifecycle_inconsistency_count=0
        and (not scope_index_name_present or scope_index_exact_and_ready)
        as safe_to_apply_migration,
    normalized_body_md5='3b2713fee873d93c306b4a39091199fb'
        and exact_overload_inventory
        and exact_function_contract
        and scope_index_exact_and_ready
        and retention_index_exact_and_ready
        and checkpoint_trigger_exact_and_enabled
        and canonical_lifecycle_inconsistency_count=0
        as post_deployment_verified
from verified;

rollback;
