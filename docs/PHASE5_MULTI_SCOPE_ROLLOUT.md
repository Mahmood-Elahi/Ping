# Phase 5 multi-scope rollout guide

This is a future manual runbook. Phase 5 Batch 4 did not run a live canary,
change `config/target_companies.yaml`, connect to production Supabase, or apply a
migration.

## Required order

1. Review `sql/migrations/20261010_phase5_multi_scope_lifecycle.sql` and record
   pre-change canonical-job, staged-session, and source-health counts.
2. Run the read-only migration preflight.
3. Apply the migration manually from a trusted administrative workstation.
4. Run the read-only post-migration verification and compare counts.
5. Run the bounded Lightspeed France canary manually with no retries.
6. Require the central result to be exactly `PASS_COMPLETE`.
7. Review the proposed production diff against
   `config/target_companies.phase5_batch4_lightspeed_proposed.yaml`.
8. Activate only the France row; do not change the existing default row.
9. Run targeted registry, scan, persistence, dashboard, and lifecycle checks.
10. Retain the source-level rollback path and its administrative evidence.

Steps must not be reordered. `PASS_PARTIAL`, any failure outcome, a missing
migration verification, an unreviewed config diff, or an unprepared rollback
means `DO_NOT_ACTIVATE`.

## Migration preflight and application

From the repository root in PowerShell, with a trusted direct PostgreSQL URL in
`SUPABASE_DB_URL`:

```powershell
psql $env:SUPABASE_DB_URL -X -v ON_ERROR_STOP=1 `
  -f .\sql\phase5_multi_scope_preflight.sql
```

Expected result: `PHASE3_READY_FOR_PHASE5`. The preflight uses a read-only
transaction and rolls it back. It rejects missing Phase 3 tables/RPCs,
`protected_source_keys` absence, missing `pgcrypto`, and every partial Phase 5
state. If it reports 7/7 Phase 5 artifacts, do not reclassify that as ready;
run post-migration verification to determine whether the migration was already
applied completely.

After a human reviews the SQL and preflight output, apply the migration:

```powershell
psql $env:SUPABASE_DB_URL -X -v ON_ERROR_STOP=1 `
  -f .\sql\migrations\20261010_phase5_multi_scope_lifecycle.sql
```

The migration is forward-only and transaction-wrapped. Do not reverse it when
one future source fails; the schema remains backward-compatible infrastructure.

## Post-migration verification

```powershell
psql $env:SUPABASE_DB_URL -X -v ON_ERROR_STOP=1 `
  -f .\sql\phase5_multi_scope_verify.sql

.\.venv\Scripts\python.exe .\scripts\report_phase5_coverage.py
```

Every `ok`/RPC/RLS/permission value must be true. Confirm the following against
the recorded pre-change values:

- canonical `tracked_jobs` count is unchanged;
- all legacy `tracked_jobs` and `job_source_health` rows have `default` scope;
- ownership-row count equals tracked-job count;
- no ownership is missing, duplicated, or orphaned;
- lifecycle status, missing count, and removal time match each legacy row;
- staged sessions/rows have default scope keys and provider identities;
- scope-aware begin/stage/finalize RPCs and the explicit retirement RPC exist;
- RLS is enabled and service-role DML/execute access remains intact;
- the local production registry still reports 533 canonical companies, 533
  configured sources, 533 scopes, and zero multi-scope companies.

The verification script is read-only and ends with `ROLLBACK`.

## Bounded Lightspeed France canary

Only after migration verification succeeds:

```powershell
.\.venv\Scripts\python.exe .\scripts\validate_company_registry.py `
  --current-config .\config\target_companies.yaml `
  --candidate-config .\config\target_companies.phase5_batch2_canaries.yaml `
  --only-additions `
  --batch-size 1 `
  --attempts 1 `
  --output .\output\phase5_lightspeed_france_canary.json `
  --validated-config .\output\phase5_lightspeed_france_validated.yaml
```

The target has one listing request, at most 20 detail hydration requests, no
retry allowance, and a hard maximum of 21 requests. This command is deliberately
not part of scheduled runtime or CI.

Classify the saved evidence using these exact rules:

| Result | Meaning | Activation decision |
|---|---|---|
| `PASS_COMPLETE` | Employer and France board identity valid; scope correct; inventory and terminal evidence complete; no identity/pagination/stable-ID/mutation/budget contradiction; central finalizer authoritative | Eligible only if every other gate passes |
| `PASS_PARTIAL` | Jobs observed, inventory incomplete | `DO_NOT_ACTIVATE` |
| `FAIL_IDENTITY` | Employer or board identity wrong/unproven | `DO_NOT_ACTIVATE` |
| `FAIL_CONTRACT` | Pagination, terminal, stable-ID, mutation, or budget contract invalid | `DO_NOT_ACTIVATE` |
| `FAIL_TRANSIENT` | Transient request failure | `DO_NOT_ACTIVATE`; retry manually later under a separately approved budget |

## Proposed configuration

The offline proposal contains two flat rows with the same canonical name:

```yaml
- name: Lightspeed Commerce
  source_scope: default
  platform: static_html
  listing_url: https://www.lightspeedhq.com/careers/openings/

- name: Lightspeed Commerce
  source_scope: france
  platform: greenhouse
  slug: lightspeedhqfr
  careers_url: https://job-boards.greenhouse.io/lightspeedhqfr
  endpoint_variant: boards_api_v1_strict
```

The complete reviewed fields are in the proposal fixture. The existing default
source retains its production source identity and stable UUID job identity.
The France source uses `greenhouse:lightspeedhqfr` provider identity, stable
Greenhouse job IDs, exact allow-listed Greenhouse URLs, logical key
`lightspeed commerce::france`, and an independent cooldown fingerprint.

## Rollback and retirement

Configuration rollback removes only the France row. It must not modify the
default Lightspeed source and must not reverse the multi-scope migration.

If France was never activated and owns no lifecycle observations, removing the
candidate has no lifecycle effect. If it was activated, configuration removal
alone is insufficient because an unscanned owner would otherwise remain active.
A trusted administrator must call `retire_student_job_source_scope` with exact
company/scope/key, a reason of at least eight characters, an approver, and an
aware timestamp. The RPC records immutable source-level evidence, retires only
France ownership rows, and recomputes canonical activity across all owners.
France-only jobs close; shared jobs remain active through the global/default
owner. Failure, cooldown, partial scans, and mere registry absence never call or
imply retirement.

Application code exposes the same deliberate boundary as
`SupabaseJobStore.retire_source_scope(...)`; normal scan and dashboard paths do
not invoke it.
