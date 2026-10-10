# Phase 5 Batch 3: Multi-Scope Lifecycle

## Decision

Job Radar uses one flat registry row per recruiting source. Rows may share the
same canonical company `name`, but each has a normalized `source_scope` such as
`professional`, `early_careers`, `university`, `france`, or `spring_hill`.
Legacy rows omit the field and deterministically resolve to `default`.

This was selected over a nested `sources` structure because the scanner,
integrity assessor, adapter dispatch, cooldown store, and coverage report are
already source-row oriented. A flat `(name, source_scope)` identity adds the
required boundary without rewriting adapter configuration or the 533-row
production registry.

## Old failure mode

Both local freshness and staged Supabase finalization treated a complete scan
as authority over every lifecycle row with the same normalized company name.
Provider identities and cooldowns were source-specific, but missing-count
increments were company-wide. Therefore a complete professional-board scan
that omitted a job still present on an early-career board could advance that
job to `possibly_missing` and then `removed`.

## Identity model

```text
canonical company
    -> logical source scope
       -> source/config identity and cooldown fingerprint
          -> provider-owned job observation
             -> optional exact cross-source canonical job key
```

- Logical scope key: normalized canonical company plus normalized
  `source_scope`.
- Source/config fingerprint: stable board, tenant, endpoint, host, and identity
  configuration. Operational budgets and retry tuning remain excluded.
- Provider job key: provider/source namespace plus stable external ID, then
  canonical URL, then the existing fallback.
- Canonical job key: provider key unless explicit exact equivalence evidence is
  supplied.

## Lifecycle rules

1. A complete authoritative scan can reconcile only its own logical scope.
2. Incomplete scans, partial inventories, and cooldown skips reconcile nothing.
3. Unreadable identities protect only their owning scope association.
4. Each scope association has its own missing count and lifecycle status.
5. The canonical job is active while at least one owner remains active.
6. Two authoritative misses still close a single-scope job exactly as before.
7. Rediscovery resets and reopens only the observing scope; aggregate activity
   is then derived across every owner.

## Exact equivalence and unknown overlap

Permitted evidence is limited to shared requisition IDs, provider-owned global
IDs, exact canonical apply URLs, exact first-party job URLs, and reviewed
explicit configuration. Title and location are never authoritative identity.
If overlap is probable but cannot be proven, records remain independent and
one source cannot close the other source's records.

## Persistence

The forward migration
`sql/migrations/20261010_phase5_multi_scope_lifecycle.sql` adds:

- defaulted scope columns on source health, tracked jobs, staged sessions,
  staged rows, and the idempotency ledger;
- `tracked_job_source_scopes`, keyed by canonical job and logical scope;
- source-scope indexes and service-role-only RLS/grants;
- scope-aware begin, stage, and finalize RPC contracts;
- a backward-compatible `default` ownership backfill for existing tracked jobs.

Finalization is transactional. It validates staged scope identity, upserts the
canonical row and its ownership association, advances missing state only for
the finalized scope, derives aggregate canonical activity across all owners,
records the scope-specific idempotency result, and clears staging in one
transaction.

## Deterministic fixtures

- BlackRock: professional and early careers, including one exact shared job
  and one title/location-similar non-equivalent pair.
- Chevron: professional and university; Chevron Stations remains a separate
  canonical entity.
- Lightspeed Commerce: global and France with unknown overlap.
- Ultium Cells: Spring Hill, Troy, and Warren with no assumed equivalence.
- CATL and Siemens Healthineers: global, regional, and specialized scope-family
  configuration coverage without invented source identity.

No fixture activates a source or makes a live request.

## Activation boundary

The architecture is ready to contain a future multi-scope canary. Activation
still requires the candidate's own identity, completeness, and exact-overlap
evidence. Lightspeed France remains `READY_FOR_CANARY` with a 21-request manual
budget and is not production-active. Phase 6 incremental scanning is untouched.
