# Phase 6 — Incremental Optimization

> **INCREMENTAL IS AN OPTIMIZATION, NOT A NEW SOURCE OF TRUTH.**

Phase 6 adds fail-closed domain contracts, bounded checkpoint persistence,
positive-only staged persistence, conditional-snapshot validation primitives,
and deterministic observability. It activates no production adapter.

## Architecture and existing integration points

`AdapterResult` remains the adapter boundary. Its existing `SourceScanMetadata`
continues to carry Phase 3 pagination and inventory evidence; Phase 6 adds the
orthogonal `crawl_mode` and `result_authority` fields. The central scanner still
calls `finalize_source_scan`, so adapters cannot manufacture lifecycle trust.
`CompanyScanResult.authoritative_for_absence` now additionally requires
`authoritative_inventory` authority.

The existing source-scoped staged path remains the only persistence path:

1. normalized observations are grouped by exact `source_scope_key`;
2. `source_authorities` is converted to the existing `reconcile_absence` gate;
3. positive-only rows are staged and upserted normally while that gate is false;
4. an optional checkpoint candidate rides on `job_sync_sessions`;
5. the exact `job_sync_sessions` row transitioning to `finalized` triggers the
   checkpoint upsert in the same database transaction;
6. a staging, normalization, persistence, or finalization failure therefore
   leaves the accepted checkpoint unchanged.

`tracked_job_source_scopes` continues to isolate scope ownership. Source
retirement remains a separate, deliberate RPC. Freshness and 60-day retention
remain output/storage policies and never become absence evidence.

## Domain contract

The types live in `src/scraper/company_careers/incremental.py`.

- Capability: `none`, `timestamp_watermark`, `cursor`,
  `inventory_plus_incremental_hydration`, `explicit_change_feed`, or
  `validated_snapshot_reuse`.
- Run mode: `full_authoritative`, `incremental_positive_only`, or
  `cached_authoritative_snapshot`.
- Result authority: `authoritative_inventory`, `positive_only`, or `incomplete`.
- `IncrementalCheckpoint` binds exact source scope, provider identity,
  configuration fingerprint, and strategy version. Cursor and validator values
  are length-bounded and the serialized candidate is capped.
- `IncrementalRequest` records the chosen mode and binding.
- `IncrementalResult` rejects authoritative incremental results and rejects a
  checkpoint candidate on an incomplete result.

No parallel lifecycle state machine was introduced. Authority is an additional
input to the Phase 3–5 lifecycle gate.

## Crawl-mode selection

`choose_crawl_mode(...)` chooses a full crawl for:

- no reviewed incremental capability;
- missing, corrupt, oversized, or identity-mismatched checkpoint;
- configuration fingerprint or strategy-version change;
- no successful full crawl;
- elapsed source-configured forced-full interval;
- ambiguous prior incremental result;
- provider regression; or
- an explicit reconciliation requirement.

Otherwise it may select positive-only incremental mode. The conservative
default forced-full interval is seven days, but it belongs to per-source policy
and is not a universal production cadence. Incremental successes do not move
`last_successful_full_at`, so they cannot postpone reconciliation indefinitely.

Cursor repetition/rollback, timestamp rollback, absent timestamps, malformed
continuation, caps, contraction, invalid 304 state, corruption, and exhausted
retry budgets must be returned by an adapter as incomplete or force a full
fallback. A repeated cursor is never end-of-file evidence.

## Positive-only semantics

Positive-only runs may insert or refresh jobs, advance `last_seen_at`, reset the
observed scope owner to active, protect observed provider identities, and accept
material changes. They pass `p_reconcile_absence = false`; therefore absent rows
are not visited by the missing-counter update and sibling scopes are untouched.

The database remains the enforcement boundary. `successful_source_scopes`
continues as the backward-compatible authoritative input. New callers should
pass `source_authorities`; conflicting positive-only and authoritative claims
are rejected before staging.

## Material changes and hydration avoidance

`material_content_fingerprint` hashes stable/material job fields and explicitly
excludes observation-only timestamps. `can_skip_hydration` requires an exact,
non-empty trustworthy provider version on both the stored and inventory item.
A previously seen job ID alone never permits a skip. If a reviewed version is
not available, normal hydration remains required.

This follows freehire's content-hash and cheap refresh separation while keeping
Job Radar's existing `source_updated_at` and `last_seen_at` columns distinct.
The current SQL upsert always advances `last_seen_at`; observation-only changes
are excluded from the material fingerprint.

## Validated conditional snapshots

`ValidatedSnapshot` and `reuse_validated_snapshot` are architecture only. A 304
is reusable only if all of these match:

- reviewed provider contract;
- exact source scope;
- exact provider identity;
- exact configuration fingerprint;
- exact response validator;
- a prior complete authoritative snapshot; and
- SHA-256 integrity of the cached payload.

Missing or corrupt cache state raises a fail-closed error. A 304 is never an
empty inventory. No HTTP adapter currently enables conditional snapshot reuse.

## Observability

`IncrementalResult.metrics()` emits only measured/domain values:

- selected crawl mode;
- checkpoint age and last-full age;
- fallback reason;
- requests saved;
- detail hydrations skipped;
- incremental items observed;
- full inventory size;
- checkpoint advanced; and
- absence authorized.

## Adapter capability inventory

All entries below remain **full-crawl only in production**. “Promising” means a
future adapter-specific review has repository evidence worth testing; it is not
activation approval.

| Adapter/platform | Repository-only classification | Evidence / missing proof |
|---|---|---|
| Greenhouse | Promising inventory + selective hydration; promising timestamp | Explicit `InventoryItem`; listing summaries carry `updated_at`; detail hydration is separable. Ordering/version semantics still need a provider-contract review. |
| SmartRecruiters | Promising inventory + selective hydration | Explicit complete listing inventory and existing `selective_detail_enrichment`; `releasedDate` is present. Must prove it is a material version, not merely publish time. |
| Ashby | Promising inventory + selective hydration | Explicit inventory and separated summary/detail paths; summary timestamps vary by endpoint and need contract validation. |
| BambooHR | Promising inventory + selective hydration | Explicit inventory plus detail endpoint and `datePosted`; no proven material version yet. |
| SAP SuccessFactors | Promising inventory + selective hydration | Explicit inventory and configured selective detail enrichment; multiple protocol variants make a single generic watermark unsafe. |
| Workday | Promising inventory + selective hydration | Explicit inventories in reviewed flows; `postedOn` is not proven to be a material update version. Partition/truncation rules remain authoritative. |
| Dayforce | Promising inventory + selective hydration | Stable inventory identities and posting timestamp; no repository proof of change-version semantics. |
| ADP, Deel, Paycor, PeopleSoft, Cornerstone CSOD | Promising inventory + selective hydration | Explicit identities exist, but trustworthy item versions are absent or variant-specific. |
| Lever | Promising timestamp incremental | `createdAt` is captured and pagination is bounded. Newest-first ordering and edits-after-create are not proven, so no activation. |
| Eightfold | Promising cursor/incremental enrichment | Bounded pagination and posting timestamps exist; Jobseek demonstrates complete streaming inventory plus enrichment, but Job Radar has no reviewed incremental contract yet. |
| Shopify, Apple, Amazon, Atlassian, Workable, UKG Pro, Uber | Promising timestamp incremental | Listing timestamps exist, but semantics/order/rollback behavior are not proven from Job Radar code. |
| Public JSON, first-party JSON, embedded data, source XML | Insufficient deterministic evidence | These are configuration-driven families; capability must be reviewed per exact endpoint, not granted to the generic adapter. |
| Browser-rendered HTML, static HTML | Insufficient deterministic evidence | Page rendering and selectors supply no generic stable version or validator contract. |
| Avature, iCIMS, Oracle Recruiting, Phenom, HawkSearch v2, mCloud CWS, Beesite, ClearCompany, Exalead, HiBob, Jobvite, TalentBrew, Teamtailor, Startdate | Insufficient deterministic evidence | Full pagination contracts exist; repository code does not prove a safe incremental boundary. |
| Meta, Google, IBM Search, Amazon custom, Atlassian custom, Uber custom | Insufficient deterministic evidence | Bespoke feeds expose some dates/cursors, but no reviewed deletion, ordering, or checkpoint contract. |
| Source-specific manual/browser aliases | Full-crawl only | No deterministic incremental evidence. |

Best future canary candidates, in order:

1. **SmartRecruiters** — strongest existing inventory/detail split and explicit
   opt-in enrichment path.
2. **Greenhouse** — stable inventory IDs and listing `updated_at`, subject to
   proving timestamp semantics against recorded deterministic fixtures.
3. **Eightfold** — only after a dedicated contract review of pagination and
   enrichment, using Jobseek as reference rather than as lifecycle authority.

## Sibling-repository provenance

No sibling file was modified or copied wholesale.

- Jobseek `apps/crawler/src/core/monitors/_incremental.py` informed the bounded
  high-water pagination/fallback shape; `_pcsx.py` shows an actual caller.
- Jobseek `apps/crawler/src/core/monitors/eightfold.py` informed the separation
  of complete discovery from incremental enrichment.
- Jobseek's inventory-runner documentation (`docs/21-ats-inventory-runner.md`)
  informed fail-closed 304 reuse and integrity validation.
- freehire `cmd/ingest/store.go` and
  `internal/platform/db/queries/jobs.sql` informed content-hash cheap writes,
  `last_seen_at` touch behavior, and the cache-is-not-truth rule.
- JobSpy's Indeed/Glassdoor/Google pagination implementations were consulted
  only as cursor-shape references. Their stop/lifecycle semantics were not
  imported.

Explicitly rejected:

- repeated cursor means success;
- generic 4xx means EOF;
- trailing-window absence authority;
- elapsed grace as absence authority;
- seen ID alone skips hydration;
- cache/304 means empty inventory; and
- retention age means provider absence.

## Rollout plan

- **Stage A (this change):** generic architecture, schema, tests, documentation,
  and zero production activations.
- **Stage B:** one reviewed adapter canary with deterministic recorded fixtures,
  a source-specific forced-full interval, and no delete-event authority.
- **Stage C:** compare request/runtime savings and job/material equivalence to
  full crawls; audit every fallback and scheduled full reconciliation.
- **Stage D:** expand one adapter contract at a time only with equivalent proof.
- **Stage E:** periodically audit that full reconciliation cadence, protected
  identities, scope isolation, and retention separation have not weakened.
