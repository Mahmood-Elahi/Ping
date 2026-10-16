-- Read-only evidence collection for the Adobe, RBC, and CIBC migration review.
-- Do not run without explicit production database read authorization.
-- This file performs no source scan and no mutation. Save every result set.

begin transaction read only;

-- 1. Reconfirm the historical exposure and lifecycle distribution.
select
    owner.canonical_company,
    owner.source_scope,
    owner.source_scope_key,
    owner.lifecycle_status,
    count(*) as owner_count,
    count(*) filter (where owner.missing_successful_runs > 0) as with_missing_evidence,
    min(owner.first_seen_at) as earliest_first_seen_at,
    max(owner.last_seen_at) as latest_last_seen_at
from public.tracked_job_source_scopes owner
where owner.source_scope_key in ('adobe::default', 'rbc::default', 'cibc::default')
group by
    owner.canonical_company,
    owner.source_scope,
    owner.source_scope_key,
    owner.lifecycle_status
order by owner.canonical_company, owner.lifecycle_status;

-- 2. Export exact legacy ownership identities for offline comparison with the
-- saved non-persistent canary inventories. provider_source_key is the primary
-- comparison key; URLs are corroborating exact evidence, not fuzzy matching.
select
    owner.canonical_company,
    owner.source_scope,
    owner.source_scope_key,
    owner.provider_source_key,
    owner.source_key,
    owner.lifecycle_status as owner_lifecycle_status,
    owner.missing_successful_runs as owner_missing_successful_runs,
    tracked.lifecycle_status as canonical_lifecycle_status,
    tracked.apply_url,
    tracked.source_url,
    tracked.raw_data ->> 'job_id' as raw_job_id,
    tracked.raw_data ->> 'source' as raw_source
from public.tracked_job_source_scopes owner
join public.tracked_jobs tracked using (source_key)
where owner.source_scope_key in ('adobe::default', 'rbc::default', 'cibc::default')
order by owner.canonical_company, owner.provider_source_key, owner.source_key;

-- 3. Detect any already-existing sibling-scope ownership for the same exact
-- canonical source key. A nonzero result needs explanation before migration.
select
    left_owner.canonical_company,
    left_owner.source_key,
    left_owner.provider_source_key as left_provider_source_key,
    left_owner.source_scope_key as left_scope_key,
    right_owner.source_scope_key as right_scope_key,
    right_owner.provider_source_key as right_provider_source_key
from public.tracked_job_source_scopes left_owner
join public.tracked_job_source_scopes right_owner
  on right_owner.source_key = left_owner.source_key
 and right_owner.source_scope_key <> left_owner.source_scope_key
where left_owner.source_scope_key in ('adobe::default', 'rbc::default', 'cibc::default')
order by left_owner.canonical_company, left_owner.source_key, right_owner.source_scope_key;

-- 4. Inspect exact provider-ID reuse. Conflicting canonical source keys for one
-- provider key are a stop condition; title/location similarity is irrelevant.
select
    owner.canonical_company,
    owner.provider_source_key,
    count(distinct owner.source_key) as canonical_source_key_count,
    array_agg(distinct owner.source_key order by owner.source_key) as source_keys,
    array_agg(distinct owner.source_scope_key order by owner.source_scope_key) as scope_keys
from public.tracked_job_source_scopes owner
where owner.canonical_company in ('Adobe', 'RBC', 'CIBC')
group by owner.canonical_company, owner.provider_source_key
having count(distinct owner.source_key) > 1
    or count(distinct owner.source_scope_key) > 1
order by owner.canonical_company, owner.provider_source_key;

-- 5. CIBC campus evidence export. Compare this exact result offline with the
-- saved CIBC search-canary ID/URL set and calculate intersection, campus-only,
-- search-only, and conflicting-ID sets before proposing any replacement.
select
    owner.provider_source_key,
    owner.source_key,
    owner.lifecycle_status,
    owner.missing_successful_runs,
    tracked.title,
    tracked.apply_url,
    tracked.source_url,
    tracked.raw_data ->> 'job_id' as raw_job_id
from public.tracked_job_source_scopes owner
join public.tracked_jobs tracked using (source_key)
where owner.source_scope_key = 'cibc::default'
order by owner.provider_source_key, owner.source_key;

-- 6. Configuration fingerprint changes must not inherit stale health authority.
select
    health.company,
    health.source_scope,
    health.source_key as config_fingerprint_key,
    health.platform,
    health.consecutive_transient_failures,
    health.cooldown_until,
    health.last_failure_classification,
    health.last_successful_authoritative_inventory,
    health.updated_at
from public.job_source_health health
where health.company in ('Adobe', 'RBC', 'CIBC')
order by health.company, health.source_scope, health.source_key;

-- 7. An existing retirement record changes the migration analysis and must be
-- reviewed; this query does not create or modify one.
select
    retirement.source_scope_key,
    retirement.canonical_company,
    retirement.source_scope,
    retirement.reason,
    retirement.approved_by,
    retirement.retired_at
from public.source_scope_retirements retirement
where retirement.source_scope_key in (
    'adobe::default',
    'adobe::experienced',
    'rbc::default',
    'rbc::global',
    'cibc::default',
    'cibc::campus',
    'cibc::all-opportunities'
)
order by retirement.canonical_company, retirement.source_scope_key;

rollback;
