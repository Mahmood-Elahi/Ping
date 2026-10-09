# Architecture

Source enumeration, terminal evidence, registry activation, and lifecycle
absence invariants are documented in [SOURCE_INTEGRITY.md](SOURCE_INTEGRITY.md).

RoleRadar has one orchestration layer and two primary acquisition paths:

- LinkedIn scraping through the existing Playwright scraper
- direct company-careers scanning through reusable ATS and custom adapters

The important product idea is not just fetching jobs. It is fetching jobs while preserving source coverage: what matched, what returned no relevant roles, what failed, and what needs manual follow-up.

## Flow

```mermaid
flowchart TD
    A[CLI or API request] --> B[JobSearchPipeline]
    B --> C{Selected scrapers}
    C -->|linkedin| D[LinkedIn scraper]
    C -->|company_careers| E[CompanyCareersScanner]
    E --> F[Target company registry]
    E --> G[Adapter registry]
    G --> H[ATS and custom adapters]
    D --> I[Normalized job data]
    H --> I
    I --> J[Database and JSON outputs]
    I --> K[Eligibility and role relevance]
    K --> O[Candidate preferences]
    I --> P[Lifecycle, freshness, urgency]
    E --> L[Coverage report]
    O --> M[Ranked review artifacts]
    P --> M
    L --> N[Coverage and failure artifacts]
```

## Main Modules

`main.py`

CLI entry point. The `search` command accepts `--scrapers linkedin company_careers` so users can choose LinkedIn, direct company-careers scanning, or both.

`src/utils/job_search_pipeline.py`

Shared orchestration layer used by CLI/API flows. It invokes selected scrapers, normalizes results, writes output files, and handles database writes where enabled.

`src/scraper/search/linkedin_scraper/`

Playwright-based LinkedIn scraper inherited from the upstream project and adapted for the current workflow.

`src/scraper/company_careers/`

Direct company-careers scanner package. It loads target companies from YAML, calls the right adapter, filters/ranks relevant jobs, and emits coverage state.

`src/scraper/company_careers/freshness.py`

Config-backed deterministic lifecycle reconciliation. It reuses stable source identity, loads/persists history through the existing Supabase `tracked_jobs` store, advances missing evidence only for successful uncapped scans, parses bounded source dates, and supplies freshness/urgency annotations without changing ranking.

`src/scraper/company_careers/adapters/`

Source-specific adapters. Each adapter converts one source shape into normalized
`JobRecord` objects and reports the observations made while enumerating it.

`config/target_companies.yaml`

Company registry. This is where users add/remove targets and define platform-specific source metadata.

## Adapter Contract

The explicit company-careers flow is:

```text
provider adapter
    -> AdapterResult
       |-- jobs
       |-- inventory_items
       |-- scan observations
       |-- item_failures
       `-- capabilities_used (descriptive only)
    -> finalize_source_scan
    -> CompanyScanResult
    -> lifecycle / staged persistence
```

`AdapterResult.jobs` contains normalized `JobRecord` objects with:

- `job_id`
- `title`
- `company`
- `location`
- `url`
- `updated_at`
- `source`
- optional `departments`, `offices`, and `description`

`AdapterResult.scan` contains `SourceScanMetadata`: request/page counters,
terminal observations, safety-limit state, source-identity evidence, mutation and
pagination evidence, and adapter diagnostics. These are observations, not a
claim of trust. `finalize_source_scan` remains the only component that decides
whether the evidence is complete, healthy, identity-valid, and authoritative
for absence.

`inventory_items` contains stable provider IDs and their source namespace.
`item_failures` distinguishes hydration/normalization stage, retryability, and
`unreadable` versus `rejected` outcomes. Capabilities remain descriptive only:
they cannot create terminal evidence, validate identity, make a scan
authoritative, or bypass central finalization.

Adapters should raise `AdapterError` for parse or endpoint failures that should
be recorded as source failures. Migrated adapters attach their current scan
observations to the same exception instance; exception classes and failure
policy are unchanged.

### Compatibility and diagnostics

Every production dispatcher boundary returns `AdapterResult`. The scanner still
accepts both:

- a legacy `list[JobRecord]`, whose metadata is read from the historical
  `job_radar_scan_metadata` session attribute; and
- an explicit `AdapterResult`, whose `scan` value is used directly while any
  session alias is ignored and cleared.

Raw-list support is retained only as a compatibility path for direct/custom
callers and deterministic injected-adapter tests; no production registry entry
uses it. Existing adapter bodies may still publish the generic diagnostic
session alias, but the exported boundary snapshots it into `AdapterResult`.
Named provider pagination aliases are retained for tests and debugging. Neither
alias class is authoritative after an explicit result is returned, and the
scanner never uses a named provider alias for finalization.

The scanner clears metadata before every invocation and after every explicit
success or failure. Consequently an explicit result cannot inherit evidence
from a prior source, even when a test or caller reuses one session.

Only a centrally finalized `CompanyScanResult` with healthy or explicitly
hydration-degraded source state,
validated identity, provider-native terminal evidence, active source state, and
`scan_complete=True` can be authoritative for absence. Request, page, and job
budgets remain safety limits and never terminal evidence. Incomplete and failed
scans retain zero absence authority, so missing-job, possibly-missing, closure,
reopening, and staged Supabase reconciliation behavior is unchanged.

### Final Phase 2 migration status

All 43 canonical dispatcher boundaries now return explicit results. The first
batch covers
Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Eightfold, Dayforce, SAP
SuccessFactors, Teamtailor, BambooHR, Jobvite, Oracle Recruiting, iCIMS/Jibe/
Radancy, static HTML, public JSON, JavaScript embedded data, browser-rendered
HTML, first-party JSON, source XML, and Phenom. The second batch covers Apple,
Avature, Exalead, ClearCompany, Cornerstone CSOD, Deel, BeeSite, StartDate,
TalentBrew, HawkSearch v2, HiBob, UKG Pro, Paycor, PeopleSoft, Amazon, Shopify,
Google, Atlassian, Uber, and IBM Search. The final batch covers ADP, Meta, and
M-Cloud CWS without changing their provider protocols. The registry contains
50 keys because `custom:amazon`, `custom:atlassian`, `custom:google`,
`custom:ibm_search`, `custom:meta`, `custom:shopify`, and `custom:uber` are
aliases of canonical boundaries rather than additional adapters.

The authoritative channel for every migrated boundary is
`AdapterResult.scan`. The boundary bridge still snapshots the generic
`job_radar_scan_metadata` alias inside decorator-migrated adapters, and some
providers retain named pagination aliases for deterministic diagnostics. These
aliases are temporary compatibility surfaces; the scanner ignores them after
receiving an explicit result.

`fetch_with_mutation_restart` returns an explicit `AdapterResult`. It still
reads the generic session alias between the first and optional replacement
traversals for Avature, Atlassian, Eightfold, first-party JSON, iCIMS, Oracle
Recruiting, public JSON, SAP SuccessFactors, SmartRecruiters, and Uber. This is
a restart-internal compatibility detail: replacing it would require changing
the internal traversal callbacks, while retaining it preserves exactly one
restart, discards first-traversal jobs, and keeps all existing mutation
diagnostics. Its caller and the scanner receive explicit observations.

## Phase 3 inventory and hydration

```text
provider inventory enumeration
        -> authoritative inventory observations
        -> optional / required bounded hydration
           |-- hydrated normalized jobs
           `-- unreadable or rejected item outcomes
        -> AdapterResult
        -> finalize_source_scan
        -> lifecycle reconciliation and staged Supabase sync
```

Inventory existence is not hydration success. `finalize_source_scan` compares
authoritative totals and terminal evidence with `inventory_items_collected`,
while `jobs_collected` remains the normalized output count. A complete inventory
may therefore be authoritative with `source_health_code=hydration_degraded`.
Incomplete inventory always has zero absence authority.

For authoritative results, stable inventory IDs are converted to the same
namespaced source keys used by persisted jobs. Those keys protect prior records
from missing/closure transitions in both in-memory lifecycle reconciliation and
the staged Supabase transaction. Unreadable is never interpreted as gone, and
an unreadable item is not emitted as a successfully hydrated job. If no prior
record exists, protection does not invent one.

SmartRecruiters, BambooHR, Ashby, Greenhouse, Deel, Paycor, and PeopleSoft are
explicit split migrations. Deel, Paycor, and PeopleSoft use listing identity
only to protect an existing row when required detail transport is unreadable;
they never create a listing-only placeholder and still require detail/apply
evidence for creation or update. Lever, SmartRecruiters listing enumeration,
Cornerstone CSOD after bootstrap identity, and ADP MyJobs expose valid earlier
pages through an explicit incomplete result only when a later transport request
fails. Identity, HTTP/schema, duplicate, changed-total, pagination, and terminal
contradictions still fail closed.

Incomplete inventory discoveries can be inserted or refreshed by staged sync,
but their company is omitted from absence reconciliation. This means a partial
source can make positive progress without incrementing missing counters for any
unseen row. A cooldown skip follows the same no-authority rule and is not a
freshness observation.

### Persisted source cooldowns

The `job_source_health` Supabase table stores a source/config-local transient
failure streak, cooldown expiry, last failure classification, and the last
successful authoritative inventory time. Its primary key includes a short
SHA-256 fingerprint of stable source identity fields (company/platform,
tenant/slug/site/board identifiers, endpoint variant and source endpoints), so
a repaired configuration cannot inherit an obsolete cooldown. Volatile scan
budgets and selectors are excluded. Failures 1-2 do not pause a source; failure
3 backs off for 6 hours, failure 4 for 12 hours, and failure 5 or later for 24
hours. Only comparable transport/upstream failures participate. Identity,
malformed pagination/contract, contradictory totals, configuration, and safety
limit failures do not create cooldowns.

While `cooldown_until` is in the future, the scanner emits
`cooldown_skipped` without making a provider request. After expiry, the normal
scheduled scan is the recovery probe. It uses the same identity and completion
requirements as any other scan; only an authoritative inventory clears the
failure streak. Partial/non-authoritative results preserve it. All timestamps
are offset-aware UTC. There is no background probe scheduler. A provider-wide
circuit breaker is deliberately deferred because current evidence cannot
separate a provider outage from one bad tenant or configuration.

The chronological `20260924_staged_source_sync.sql` migration remains immutable.
Phase 3 deployment is forward-only through
`20261009_phase3_source_reliability.sql`; `sql/supabase_schema.sql` remains the
canonical final-state bootstrap for fresh databases. Both paths retain RLS and
service-role-only access to source health.

The full 43-adapter classification and remaining coupled work are recorded in
[PHASE3_INVENTORY_HYDRATION_REVIEW.md](PHASE3_INVENTORY_HYDRATION_REVIEW.md).

## Coverage States

The company-careers scanner records a status for every configured target:

- `fetched`: source returned at least one matching job
- `fetched_no_matching_roles`: source was reachable, but no jobs matched current search/relevance criteria
- `manual_review_required`: source needs manual follow-up or a future adapter
- `not_implemented`: configured platform has no adapter
- `failed_technical`: source or parser failed technically
- `cooldown_skipped`: source was deliberately not contacted during a persisted transient cooldown

This is the main difference between RoleRadar and a simple scraper. Missing results are not all treated the same way.

## Extension Points

Add a new company on an existing platform:

1. Edit `config/target_companies.yaml`.
2. Add the company name and platform-specific fields.
3. Run a company-careers scan.

Add a new source type:

1. Create an adapter under `src/scraper/company_careers/adapters/`.
2. Register it in `src/scraper/company_careers/scanner.py`.
3. Add tests in `tests/test_company_careers.py`.
4. Add one or more companies using that platform in `config/target_companies.yaml`.

Adjust ranking:

1. Edit `src/scraper/company_careers/relevance.py`.
2. Add tests for expected ranking and exclusion behavior.
3. Rerun `tests/test_company_careers.py`.
