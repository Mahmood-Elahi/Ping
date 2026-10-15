# Ping architecture

This document describes the current system structure. Normative source-safety
rules live in [Source Contracts](SOURCE_CONTRACTS.md); commands and deployment
procedures live in [Operations](OPERATIONS.md); historical decisions live in
[Roadmap](ROADMAP.md).

## System flow

```text
CLI / API / scheduled dashboard
            |
            v
    JobSearchPipeline
       /          \
LinkedIn       CompanyCareersScanner
Playwright        |
             target registry
                  |
             adapter dispatch
                  |
       AdapterResult + SourceScanMetadata
                  |
          finalize_source_scan
          /                  \
 normalized jobs       coverage/health
          |                  |
 eligibility + relevance    |
 candidate preference       |
 lifecycle/freshness        |
          \                  /
       artifacts / staged Supabase sync
                         |
                  Google Sheets views
```

The central design decision is that acquisition output and source authority are
different things. An adapter reports what it observed. The central finalizer
alone decides whether those observations prove a complete, healthy,
identity-valid inventory that may authorize absence.

## Primary components

| Path | Responsibility |
|---|---|
| `main.py` | CLI for search, parsing, and document-generation flows |
| `main_api.py` | FastAPI search, WebSocket progress, history, and SQLite job access |
| `src/utils/job_search_pipeline.py` | Shared CLI/API orchestration and artifact/database output |
| `src/scraper/search/linkedin_scraper/` | Playwright LinkedIn acquisition |
| `src/scraper/company_careers/scanner.py` | Registry dispatch, finalization, filtering, ranking, and coverage |
| `src/scraper/company_careers/adapters/` | Provider-specific enumeration and normalization |
| `src/scraper/company_careers/models.py` | Targets, jobs, adapter results, scan metadata, and coverage records |
| `src/scraper/company_careers/source_integrity.py` | Reviewed platform contracts and offline onboarding audit |
| `src/scraper/company_careers/source_health.py` | Canonical scan/health and absence-authority decisions |
| `src/scraper/company_careers/freshness.py` | Lifecycle reconciliation, date parsing, freshness, and urgency |
| `src/scraper/company_careers/incremental.py` | Disabled-by-default incremental/checkpoint domain primitives |
| `src/integrations/supabase_job_store.py` | Staged source-scope persistence and administrative retirement |
| `src/integrations/google_sheets_publisher.py` | Managed dashboard projections |
| `config/target_companies.yaml` | Normal production source registry |

## Adapter boundary

Production dispatchers return `AdapterResult`:

```text
AdapterResult
|-- jobs: normalized JobRecord values
|-- inventory_items: stable provider-owned identities
|-- scan: SourceScanMetadata observations
|-- item_failures: unreadable or rejected item outcomes
`-- capabilities_used: descriptive metadata only
```

`SourceScanMetadata` records counts, requests/pages, terminal observations,
identity, redirects, pagination evidence, mutation, safety limits, inventory
authority, and health diagnostics. These fields do not self-authorize. The
finalizer converts them into canonical `complete`, `incomplete`, or `failed`
state.

The scanner retains a legacy raw `list[JobRecord]` compatibility bridge for
direct/custom callers and injected tests. Production registry dispatch uses
explicit results. Session metadata aliases are cleared around every invocation
so evidence cannot leak between sources.

## Identity hierarchy

Ping deliberately separates four identities:

```text
canonical company
    -> logical source scope
       -> source/config fingerprint
          -> provider-owned job observation
             -> optional exact canonical job equivalence
```

- A logical scope is normalized `canonical company + source_scope`; omitted
  scopes become `default`.
- The source/config fingerprint binds stable provider settings such as tenant,
  board, endpoint, host, and reviewed identity evidence. Operational request
  budgets do not define source identity.
- Provider job identity prefers a namespaced stable source ID, then normalized
  canonical URL, then the established deterministic fallback.
- Cross-source equivalence is opt-in and exact. Shared requisition/global IDs,
  exact apply URLs, exact first-party job URLs, or reviewed mappings qualify;
  title/location similarity does not.

Multiple source scopes may represent one employer without allowing one scope
to close another scope's jobs. The production registry currently has one scope
per company, but the storage model supports independently authoritative sibling
scopes.

## Inventory and hydration

Inventory existence is distinct from successful detail hydration. Adapters may
return a complete stable-ID inventory while some details are unreadable. The
central finalizer compares authoritative totals and terminal evidence with
`inventory_items_collected`, while `jobs_collected` remains the usable
normalized output count.

For an authoritative inventory, unreadable stable identities protect matching
prior lifecycle rows from false absence. They do not create placeholder jobs.
Rejected deterministic records, unexplained accounting gaps, incomplete
inventory, or contradictory identity still fail closed. Valid earlier pages
may be retained as positive-only partial progress for explicitly reviewed
late-transient-failure paths.

The historical 43-boundary Phase 3 review classified 29 atomic adapters, seven
split inventory/hydration adapters, two partial-progress adapters, four
coupled-by-design adapters, and one Phase 4 handoff:

- Split: Ashby, BambooHR, Greenhouse, SmartRecruiters, Deel, Paycor, and
  PeopleSoft.
- Partial-progress: Lever and Cornerstone CSOD.
- Coupled-by-design: ADP, browser-rendered HTML, Dayforce, and SAP
  SuccessFactors.
- Phase 4 handoff: Workday.
- Atomic: Amazon, Apple, Atlassian, Avature, BeeSite, ClearCompany, Eightfold,
  embedded data, Exalead, first-party JSON, Google, HawkSearch v2, HiBob, IBM
  Search, iCIMS/Jibe/Radancy, Jobvite, M-Cloud CWS, Meta, Oracle Recruiting,
  Phenom, public JSON variants, Shopify, source XML, StartDate, static HTML,
  TalentBrew, Teamtailor, Uber, and UKG Pro.

Provider-specific subpaths remain explicit: ADP Workforce Now and ADP MyJobs;
iCIMS Jibe, iCIMS Radancy, and the iCIMS legacy portal; plus the separately
reviewed browser-rendered HTML, public JSON variants, SAP SuccessFactors, and
Workday contracts. These labels describe architecture; each runtime result must
still pass central finalization.

## Lifecycle, freshness, and urgency

Production history lives in Supabase `tracked_jobs`; there is no second
lifecycle store. `tracked_job_source_scopes` records scope ownership.

- First observation creates `first_seen_at`; re-observation advances
  `last_seen_at`.
- A missing job becomes `possibly_missing` after one qualifying complete
  absence and `removed` after two by default.
- Failed, incomplete, capped, discovery-only, manual, unsupported, or
  cooldown-skipped runs do not increment missing evidence.
- Reappearance restores the observing scope to active, clears its missing
  count, and preserves first-seen/removal history.
- A reliable explicit closed/expired state or passed explicit deadline may
  close immediately.
- A canonical job remains active while any owning source scope remains active.
- Intentional source-scope retirement is a separate approved administrative
  RPC; registry removal is never inferred as retirement.

Freshness and urgency are deterministic annotations, not ranking authority.
Reliable `source_posted_at` is preferred for age, otherwise `first_seen_at` is
used. Malformed, naive, unknown, or future posting dates remain unknown rather
than being guessed. Urgency evaluates explicit closure/deadline evidence before
age. Relevance, candidate preference, freshness, urgency, and resume match stay
separate so downstream consumers can inspect each signal.

## Persistence boundary

Source synchronization is staged and scope-local:

1. Begin a session bound to exact source scope/config identity.
2. Stage normalized positive observations in bounded chunks.
3. Finalize the same scope transactionally.
4. Reconcile absence only when the final result is authoritative.
5. Derive canonical activity across all owners of affected jobs.
6. Commit the idempotency result and any accepted checkpoint atomically.

Current server-enforced staging ceilings are 200 rows and 524,288 serialized
bytes per chunk. Clients may lower but not raise them. The deployed retention
policy drops stale/over-60-day jobs, immediately removes retired job rows, and
keeps sync history for seven days while protecting identities required for
safe absence decisions and application-linked records.

For a clean database, `sql/supabase_schema.sql` is canonical. The ordered
migration path is:

1. `20260924_staged_source_sync.sql`
2. `20260925_drop_legacy_source_sync.sql`
3. `20261009_phase3_source_reliability.sql`
4. `20261010_phase5_multi_scope_lifecycle.sql`
5. `20261010_phase5_bounded_retention.sql`
6. `20261010_phase5_pgcrypto_digest_resolution.sql`
7. `20261010_phase5_scope_local_finalizer.sql`
8. `20261010_phase6_incremental_checkpoints.sql`
9. `20261015_optimize_source_sync_finalizer.sql`

The latest finalizer serializes each logical source scope across scans and with
explicit source retirement, uses a scope/key index for ownership enumeration,
and writes canonical lifecycle only when the ownership-derived state differs.
It validates observed keys and reconciles missing-owner transitions without a
full-table canonical update. Materially stable jobs and owners update only
`last_seen_at`, avoiding assignment of unchanged wide/material values.

That heartbeat update is **not generally HOT eligible** on `tracked_jobs`:
`tracked_jobs_retention_removed_idx` indexes
`coalesce(removed_at, last_seen_at, first_seen_at)`. The retained optimization
still removes speculative conflict work for ordinary existing rows, avoids
unnecessary wide-column assignments and no-op canonical writes, and bounds
ownership lookup to indexed affected keys. Those are verified structural work
reductions, not proof that the production timeout is resolved. Wide JSONB
comparison, the required `last_seen_at` write and its index maintenance, large
single-scope inventories, locking, and transaction duration remain rollout
measurements. Retention indexes and freshness semantics are unchanged.

Migrations are forward-only historical records. Do not edit an applied
migration to express a later change.

## Source health and cooldown

`job_source_health` stores a transient failure streak and cooldown per
source/config fingerprint. Comparable timeout, connection, HTTP 429, and
selected 5xx failures participate. Identity, schema, pagination, mutation,
configuration, and safety-limit failures do not.

Failures 1–2 do not pause a source; failure 3 pauses for 6 hours, failure 4 for
12 hours, and failure 5 or later for 24 hours. After expiry, the normal
scheduled scan is the recovery probe. Only a successful authoritative inventory
clears the streak. There is no background probe scheduler or provider-wide
circuit breaker.

## Incremental architecture

Phase 6 added typed run modes, result authority, bounded checkpoint storage,
material fingerprints, positive-only persistence, forced-full selection, and
validated-snapshot primitives. No production adapter enables incremental
crawling; every active family remains full-crawl-only.

If future evidence supports an incremental path, it must bind checkpoint state
to source scope, provider identity, config fingerprint, and strategy version;
fall back to full crawl on ambiguity; schedule full reconciliation; and never
infer absence from an incremental slice. A 304 is not an empty inventory, and
a previously seen ID alone cannot justify skipping hydration.

## Outputs and views

Runtime outputs preserve normalized jobs and rich diagnostics. Coverage states
include fetched, fetched with no matching roles, manual review required, not
implemented, failed technical, cooldown skipped, and parent-source variants.

The Sheets dashboard is a projection, not a second ranking or lifecycle store.
`New Jobs`, `Active Jobs`, and six industry tabs use the canonical active ranked
dataset. Industry comes from `config/company_industries.yaml`, not title
keywords. Publication fails before modifying Sheets when company-to-industry
mapping is incomplete or ambiguous. `Applications` remains user-controlled.

## Extension points

Adding a company on an existing reviewed platform is a registry/onboarding
change. Adding a source type requires an adapter, a `PLATFORM_SOURCE_CONTRACTS`
entry, deterministic fixtures, scanner registration, and an isolated candidate
before any production proposal. In either case, follow the complete onboarding,
activation, and rollback gates in [Source Contracts](SOURCE_CONTRACTS.md).
