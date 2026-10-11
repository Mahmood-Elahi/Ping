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
| SmartRecruiters | **FULL_CRAWL_ONLY** | The anonymous listing endpoint has stable posting IDs, complete `totalFound` plus offset/limit inventory, and a `releasedAfter` filter. The provider documents `releasedDate` as the date the posting was released, not a last-modified/material-version field. No reviewed update ordering, opaque cursor, deletion feed, or conditional-response contract exists for the anonymous production source. All production targets are already listing-only, so there are no detail requests to save. |
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

Remaining future canary candidates, in order:

1. **Greenhouse** — stable inventory IDs and listing `updated_at`, subject to
   proving timestamp semantics against recorded deterministic fixtures.
2. **Eightfold** — only after a dedicated contract review of pagination and
   enrichment, using Jobseek as reference rather than as lifecycle authority.

SmartRecruiters is removed from the canary queue by the adapter-specific review
below. It can re-enter only after stronger provider evidence or a different
reviewed endpoint proves exact new-and-changed discovery.

## SmartRecruiters Stage B contract audit

### Verdict

Classification: **A. FULL_CRAWL_ONLY**.

The adapter policy is explicit and production-safe:

```python
INCREMENTAL_POLICY = IncrementalPolicy(
    capability=IncrementalCapability.NONE,
    strategy_version="smartrecruiters_v1",
)
TRUSTED_LISTING_VERSION_FIELD = None
```

No production registry entry enables incremental behavior. No checkpoint is
initialized, read, proposed, or advanced for SmartRecruiters.

### Evidence and rejected paths

The exact anonymous production endpoint is
`GET /v1/companies/{companyIdentifier}/postings`.
The reviewed primary references are the provider's
[v1 list-postings contract](https://developers.smartrecruiters.com/reference/v1listpostings),
[posting object definition](https://developers.smartrecruiters.com/docs/objects),
[single-posting contract](https://developers.smartrecruiters.com/reference/v1getposting),
and the separate authenticated
[publication-feed contract](https://developers.smartrecruiters.com/reference/postingsjsonstream-1).

- Posting `id` is a stable provider posting identity. Job Radar validates an
  all-numeric ID on both listing and detail records and requires the exact
  company-scoped detail reference.
- The listing response supplies `totalFound`, `limit`, `offset`, and `content`.
  Job Radar pages at 100, rejects changing totals and duplicate/overlapping
  identities, and grants completion only when the collected unique inventory
  exactly reaches the advertised total.
- The endpoint supports offset/limit and a documented `releasedAfter` filter.
  It exposes no reviewed `updated_since`, last-modified ordering, or opaque
  continuation cursor.
- The provider object contract defines `releasedDate` as the date a posting was
  released. It is a posting timestamp, not a trustworthy job-level material
  update version. Job Radar therefore must not pass it to
  `can_skip_hydration(...)`.
- The provider notes that changing public posting content requires re-posting,
  but the reviewed contract does not guarantee whether every material edit
  changes `releasedDate`, retains/replaces the posting ID, or is returned by
  `releasedAfter`. A field name plus a re-post requirement is not a monotonic
  watermark contract.
- The authenticated `/feed/publications` job-board integration exposes statuses
  including unpost events and an `updatedAfter` parameter. It is a separate,
  tokened, stateful integration contract. Job Radar's production sources use the
  anonymous company postings endpoint and possess neither the credentials nor
  the state-transition protocol required by that feed. It cannot be borrowed as
  authority for these sources.
- No deterministic fixture or sibling implementation proves ETag,
  Last-Modified, 304, or another conditional representation contract.
- Freehire and Jobseek both enumerate complete offset inventories. Freehire
  hydrates every detail; Jobseek either returns the complete URL inventory or
  hydrates its configured canonical modes. Neither sibling supplies a
  SmartRecruiters update watermark or deletion authority.

Rejected classifications:

- **B. INVENTORY_PLUS_SELECTIVE_HYDRATION:** every current production
  SmartRecruiters target has `selective_detail_enrichment: false`; its normal
  result is already produced entirely from listing rows. There are no detail
  calls to skip. For hypothetical hydrated targets, `releasedDate` is not a
  trusted version, so ID-plus-release-date cannot authorize a skip.
- **C. TIMESTAMP_INCREMENTAL_POSITIVE_ONLY:** `releasedAfter` is release-time
  filtering, not proven complete new-and-materially-changed discovery.
- **D. CURSOR_INCREMENTAL_POSITIVE_ONLY:** the endpoint has page offsets only.
- **E. EXPLICIT_CHANGE_FEED:** the only reviewed removal/change-status feed is a
  different authenticated integration and is not the production source.
- **F. VALIDATED_SNAPSHOT_REUSE:** no reviewed response validator or 304
  representation contract exists.

Content fingerprints cannot replace discovery. They can prove that two fetched
records differ, but cannot reveal an edit to a posting that an incremental
request failed to return.

### Production inventory and request model

The production registry currently contains 15 SmartRecruiters sources: Wise,
ASML, Renesas Electronics, Bosch, Tower Research Capital, Western Digital,
Kioxia, Canva, Ubisoft, SGS, Avery Dennison, Genetec, Gameloft, Arista Networks,
and Assent. All use source scope `default`, the exact anonymous company endpoint,
and listing-only normalization.

For a complete board of `N` postings, the normal request model is exactly
`ceil(N / 100)` listing requests, bounded by the target's `max_pages` and
`max_jobs`. It makes zero detail requests in production. Completion requires
all `N` unique stable IDs and the authoritative total; reaching either safety
limit is incomplete.

There is no accepted optimized request model and therefore no defensible
expected savings. A `releasedAfter` request might be smaller on a quiet board,
but it could omit a material edit and is not an optimization Job Radar may use.

### Future company decision

No company is selected for a runnable Phase 6 SmartRecruiters A/B canary because
there is no accepted strategy to canary. Running two full crawls would not test
an optimization; running `releasedAfter` would test a contract already known to
be insufficient for exact changed-job discovery.

If stronger provider evidence later re-opens the review, **Wise / `default` /
tenant `wise`** is the preferred re-evaluation target. It has stable official
identity, a previously complete 403-posting observation, and a bounded five-page
full baseline, while ASML and the smallest boards often need only one listing
request and cannot demonstrate material savings. Wise is a blocked research
candidate, not an approved canary. Its full request budget is 5; an optimized
budget remains undefined until a valid strategy exists.

### Authority, checkpoints, fallback, and retention

- Only the complete offset/total inventory may authorize absence.
- A release-filtered slice, if ever used diagnostically, is positive-only and
  has `absence_authorized=false`; it cannot increment missing counters, close
  jobs, publish a dashboard, or mutate lifecycle state.
- The intended provider identity is `smartrecruiters:{tenant.casefold()}` and
  the checkpoint key remains bound to the exact canonical company plus
  `source_scope`.
- A future configuration fingerprint would include tenant/slug, endpoint
  variant and URL, source scope, page size and caps, enrichment mode, trusted
  version field, overlap rule, forced-full interval, and strategy version.
- There is no initial checkpoint for capability `none`. Missing/corrupt state,
  config-fingerprint change, strategy-version change, timestamp rollback,
  provider regression, pagination ambiguity, detail failure, or request-budget
  exhaustion must select or retain full-crawl behavior and cannot propose a
  checkpoint.
- Even after a future strategy is approved, a checkpoint candidate may advance
  only through the existing atomic persistence finalizer after all observations
  persist. Failed/incomplete canaries and persistence failures never advance it.
- Retention age is never provider absence. Forced full scans must occur more
  frequently than any retention window, and positive-only runs cannot defer the
  recorded last successful full crawl.

### Hydration and material fingerprint audit

Hydration skipping is rejected. SmartRecruiters has no trusted listing-level
version. Matching only posting ID or `releasedDate` is insufficient, including
when content happens to be unchanged in a fixture.

The generic material fingerprint covers all fields currently normalized by the
adapter: provider ID and source, title, company, location, canonical/apply URL,
description, department/function categories, offices, posting/release date, and
stable raw metadata including company identity, API detail URL, apply URL,
reference number, endpoint variant, and hydration mode. SmartRecruiters exposes
no normalized closing-date field. Observation-only timestamps remain excluded.
Deterministic tests additionally prove that a title, description, or apply URL
change alters the fingerprint even when ID and `releasedDate` remain unchanged.

### Canary, PASS/FAIL, activation, and rollback gates

No exact manual incremental canary command is published. The existing
`scripts/validate_source_integrity_canaries.py` command can run a bounded,
read-only normal source canary, but it does not implement a full-versus-
incremental A/B comparison and must not be represented as one. No new live
wrapper was added because there is no safe optimized branch to execute.

Current PASS is deliberately impossible. SmartRecruiters may return to canary
design only after repository evidence proves one of these contracts:

1. a trustworthy job-level material version plus complete inventory, allowing
   version-bound selective detail hydration; or
2. a change query that is complete for every new and material update after a
   watermark, with defined inclusivity/overlap and rollback behavior; or
3. an exact opaque change cursor or explicit change feed bound to the same
   public provider identity; or
4. a reviewed conditional representation validator for complete snapshots.

After one prerequisite is proven, an A/B canary must also satisfy the generic
Stage B gates: identical provider/scope binding; all expected identities and
material records equal to the full baseline; lower measured request count; no
hidden continuation, safety limit, unreadable/rejected optimized item, or
ambiguous evidence; valid checkpoint candidate; no checkpoint advance before
persistence; and no lifecycle, registry, database, or dashboard mutation.

FAIL is any missing prerequisite or A/B gate, including missing/malformed or
regressing timestamps, duplicate IDs, a changed record hidden behind an
unchanged alleged version, incomplete pagination, exhausted request/retry
budget, config/strategy mismatch, cross-scope state, any absence reconciliation
from a slice, or no measured request saving.

There is no future activation diff while capability remains `none`. The exact
production diff is empty. Rollback is likewise a no-op: production already uses
full authoritative crawls and has no SmartRecruiters checkpoint rows to delete
or disable. If a later reviewed strategy is implemented, activation must be one
source-specific `enabled: false -> true` change after a passing canary, and
rollback must restore `false`, ignore the bound checkpoint, and immediately use
the unchanged full authoritative path.

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
