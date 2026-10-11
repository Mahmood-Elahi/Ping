# Phase 5 multi-scope closeout

Date: 2026-10-10

Status: `PHASE5_COMPLETE`

Phase 5 multi-scope architecture and production lifecycle/storage support are
complete. Production migrations, bounded retention, pgcrypto resolution, and
the scope-local finalizer repair are deployed. The normal hourly production
workflow is operator-verified green.

Lightspeed France was the final rollout candidate. Its bounded live canary was
run manually twice and returned the same final result: `FAIL_IDENTITY`.
The strict Greenhouse boards API returned HTTP 404 for `lightspeedhqfr`, so
employer/source identity remained unproven, zero inventory was collected, and
no completeness or terminal authority was established. This is a successful
fail-closed outcome. It does not indicate an unfinished architecture defect.

## Final activation decision

- Candidate: `Lightspeed Commerce / france`
- Candidate ID: `lightspeed_fr_greenhouse`
- Proposed provider identity: `greenhouse:lightspeedhqfr`
- Logical source key: `lightspeed commerce::france`
- Final status: `REJECTED`
- Canary outcome: `FAIL_IDENTITY`
- Error: `AdapterError: Greenhouse HTTP 404 for lightspeedhqfr`
- Final endpoint: `https://boards-api.greenhouse.io/v1/boards/lightspeedhqfr/jobs`
- Requests: 1 of 21; budget respected
- Inventory: 0 jobs and 0 inventory items
- Authority: inventory incomplete, terminal evidence invalid, central finalizer
  not authoritative
- Safety: no persistence, lifecycle mutation, production-registry mutation, or
  dashboard publication
- Production activation: false
- Canary rerun in Phase 5: forbidden
- Retirement: not applicable because the source was never activated

The ignored local artifact
`output/phase5_lightspeed_france_canary.json` is operator evidence, not a
tracked runtime artifact. Its concise durable disposition is recorded here and
in the existing candidate, canary, proposal, and backlog metadata. Reconsider
the source only after new first-party identity evidence and a separately
reviewed proposal; archived discovery references alone are insufficient.

## Production registry

`config/target_companies.yaml` remains unchanged at 533 canonical companies,
533 configured sources, and 533 logical scopes. It has zero multi-scope
production companies and no `Lightspeed Commerce / france` row.

The existing Lightspeed source remains:

```yaml
name: Lightspeed Commerce
source_scope: default
platform: static_html
listing_url: https://www.lightspeedhq.com/careers/openings/
```

No substitute candidate was activated merely to create a production
multi-scope example. Architecture completion does not depend on unsafe source
activation.

## Final Phase 5 invariants

1. Source scopes
   - An omitted `source_scope` normalizes to `default`.
   - Explicit sibling scopes have independent configuration, health, cooldown,
     staging, ownership, and reconciliation identity.
2. Absence reconciliation
   - A complete authoritative scan reconciles only its owning logical scope.
   - Failed, incomplete, partial, or cooldown-skipped sources authorize no
     disappearance transitions.
3. Exact overlap
   - Cross-source equivalence requires an exact provider/global requisition ID,
     exact canonical apply URL, exact first-party job URL, or reviewed explicit
     mapping.
   - Title/location similarity never merges jobs; multiple owners are allowed.
4. Aggregate lifecycle
   - A canonical job remains active while any source-scope owner remains active.
5. Retirement
   - Retirement is an explicit approved operation, separate from failure and
     cooldown.
   - Candidate rejection never calls or implies retirement.
6. Retention
   - Stale and over-60-day jobs are not persisted or displayed under the
     deployed policy.
   - Provider identity required to prevent false absence and application-linked
     rows remain protected.
   - Removed rows and operational sync history are bounded.
7. Persistence
   - Source synchronization is staged and finalization is scope-aware.
   - Canonical lifecycle aggregation considers every owner only for canonical
     keys affected by the finalized scope; it does not aggregate the whole
     ownership table for each source.
8. Canary safety
   - Candidate registries are isolated from normal runtime, requests are
     bounded, and canaries cannot persist, publish dashboards, or mutate
     lifecycle/production configuration.
   - `PASS_COMPLETE` is the sole activation-eligible canary outcome. Missing,
     ambiguous, partial, contradictory, or failed evidence means
     `DO_NOT_ACTIVATE`.

## Persistence, migration, and storage state

The ordered repository migration chain is:

1. `20260924_staged_source_sync.sql`
2. `20260925_drop_legacy_source_sync.sql`
3. `20261009_phase3_source_reliability.sql`
4. `20261010_phase5_multi_scope_lifecycle.sql`
5. `20261010_phase5_bounded_retention.sql`
6. `20261010_phase5_pgcrypto_digest_resolution.sql`
7. `20261010_phase5_scope_local_finalizer.sql`

`sql/supabase_schema.sql` represents the same final state for clean bootstrap:
pgcrypto is installed in `extensions`; the finalizer search path resolves
`digest`; finalization uses an `affected` key set before aggregating owners;
the explicit `retire_student_job_source_scope` RPC is present; and bounded job,
ownership, source-health, staging, ledger, and sync-history maintenance is
present.

The deployed retention policy is:

```yaml
max_job_age_days: 60
drop_stale: true
removed_job_retention_days: 0
sync_history_retention_days: 7
```

Production Supabase is operator-reported healthy and comfortably below the
former 500 MB quota pressure. The finalizer repair bounds lifecycle aggregation
to affected canonical keys while still considering all owners of each affected
key.

## Source-level backlog boundary

Known provider/source failures such as SAP SuccessFactors contract drift,
blocked providers, Google contract issues, and Apple collection mutation remain
source-coverage backlog. They are not Phase 5 architecture blockers, and Phase
5 completion does not claim that every individual company source is healthy.

## Phase 6 handoff: incremental optimization

Phase 6 is `INCREMENTAL OPTIMIZATION`. Do not begin by assuming every adapter
can crawl incrementally. Full crawls remain the correctness baseline.

Incremental crawling is eligible only when a source exposes a trustworthy,
stable `source_updated_at`, posting timestamp, monotonic cursor, or equivalent
provider-native contract. Phase 6 should:

- inventory adapters with trustworthy timestamps or cursors;
- classify which sources can safely support incremental inventory;
- keep periodic full reconciliation crawls;
- store bounded cursor/state history and detect invalid, stale, or regressed
  cursors;
- fall back to a full crawl whenever identity, ordering, cursor, completeness,
  or terminal evidence is ambiguous;
- benchmark request and runtime savings against full-crawl baselines;
- never infer absence from an incremental slice alone; and
- keep retention policy independent from incremental cursor logic.

Incremental optimization must not weaken complete-inventory authority,
disappearance detection, terminal evidence, source identity, exact ownership,
or fail-closed semantics.
