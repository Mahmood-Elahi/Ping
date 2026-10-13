# Source contracts

This is the normative maintenance guide for direct company-careers sources.
When prose and executable behavior diverge, preserve fail-closed behavior and
reconcile the documentation with `source_integrity.py`, `source_health.py`, the
adapter, production configuration, and deterministic tests before activation.

## Core rule

Fetching records is not proof of coverage. A source has absence authority only
when the current run proves all of the following:

- the intended employer and exact recruiting source;
- the intended logical source scope;
- stable provider-owned job identity;
- a reviewed enumeration and terminal strategy;
- complete, internally consistent inventory accounting;
- no contradictory pagination, mutation, redirect, record, or safety evidence;
- healthy central-finalizer state; and
- `inventory_authority: authoritative` with
  `result_authority: authoritative_inventory`.

Any stronger negative observation wins over a terminal hint. Useful positive
jobs may survive an incomplete run, but the run cannot advance absence.

## Identity contract

Each registry row must bind the canonical company to a reviewed official
source using provider-specific tenant, board, site, slug, organization, locale,
endpoint, hostname, and employer marker fields where applicable.

- Reachability, an employer-like slug, job-description text, or an ATS title is
  not employer identity.
- Redirects are accepted only within exact reviewed HTTPS host contracts.
- Redirects to login/challenge pages, aggregators, generic homepages, unrelated
  hosts, or unreviewed ATSs fail identity.
- `allowed_apply_hosts` contains hostname-only approved destinations.
- A candidate replacement does not inherit authority from the old source.
- `active` in configuration means the row is supported and enabled; it is not
  a claim of current runtime health or completeness.

The source/config fingerprint includes stable identity fields and reviewed
evidence. Changing a tenant, board, endpoint, or identity binding creates a new
fingerprint so stale cooldown state is not inherited. Request/page budgets,
timeouts, and retry tuning are operational and excluded.

## Scope contract

One canonical company may have global, regional, professional, university,
subsidiary, facility, or other recruiting authorities. Each has its own
normalized `source_scope` and must be independently authoritative.

- An omitted scope resolves to `default`.
- A complete run reconciles only its exact logical scope.
- A discovery-only or incomplete scope never closes another scope's records.
- Parent/subsidiary attribution requires explicit reviewed mapping.
- Exact cross-source equivalence accepts shared requisition/global IDs, exact
  canonical apply or first-party job URLs, or reviewed explicit mapping.
- Similar title/location text is diagnostic only and never merges lifecycle
  identity.

Removing a registry row is not retirement evidence. Intentional retirement
uses `retire_student_job_source_scope` with exact company, scope, scope key,
human reason, approver, and timestamp.

## Completion contract

The finalizer produces `complete`, `incomplete`, or `failed`.

Accepted terminal evidence is provider-specific and may include:

- an authoritative total reached exactly;
- explicit `has_more=false` or terminal cursor/page semantics;
- a provider-reported final page or disabled reviewed next control;
- a validated complete/atomic collection response; or
- a reviewed deterministic single-page contract.

The following never prove completion by themselves:

- HTTP 200;
- a non-empty or empty parser result;
- a short page;
- no selector match;
- reaching `max_jobs`, `max_pages`, request, action, timeout, or byte limits;
- an old complete snapshot;
- a third-party inventory; or
- successful rendering without terminal enumeration evidence.

These conditions force incomplete or failed state even when a terminal hint is
present:

- continuation remains;
- authoritative total exceeds validated unique inventory;
- repeated pages, cursors, offsets, or stable IDs;
- changed totals, collection signatures, or conflicting duplicates;
- malformed/rejected records or unexplained accounting gaps;
- unapproved redirects or source identity failure;
- response truncation or safety-budget exhaustion; or
- non-terminal zero results.

Zero is authoritative only when the exact reviewed runtime identity and an
explicit empty-collection contract both pass.

## Inventory authority and partial progress

`authoritative` sources may authorize absence only after the full finalizer
gate. `discovery_only` sources may create or refresh exact positive ownership,
advance `last_seen_at`, and reopen observed jobs, but omission means nothing.
Expected bounded discovery stops do not count as transient failures and do not
update the last successful authoritative-inventory timestamp.

An adapter may retain valid earlier pages after a later transient transport
failure only where that provider path has an explicit partial-progress
contract. The result remains incomplete. Deterministic identity, schema,
pagination, mutation, or safety failures do not become partial success.

Inventory and detail hydration are accounted separately. An unreadable listed
identity may protect an existing row but does not create a placeholder. A
rejected item or unexplained loss blocks authoritative accounting.

## Lifecycle contract

- Only authoritative complete scopes enter absence reconciliation.
- Default removal requires two consecutive qualifying absences.
- The first absence becomes `possibly_missing`; the second becomes `removed`.
- After repairing a source, never bulk-close from its first recovered scan.
- Reappearance restores the observing owner while preserving first-seen and
  historical removal data.
- Explicit reliable closed/expired/filled status or a passed explicit deadline
  can close immediately.
- Retention age and stale-output filtering are not evidence of source
  exhaustion.

## Mutation and retry contract

Provider paths that support mutation recovery may restart the entire traversal
once. The first traversal is discarded and must never be unioned with the
replacement. A second mutation is incomplete. Request budgets cover both
attempts.

Transient source cooldowns apply only to comparable transport/upstream errors.
Identity, malformed contracts, contradictory pagination, mutation exhaustion,
unsupported variants, and safety stops are deterministic failures and do not
advance the cooldown streak.

## Activation gate

Before adding or replacing a row in `config/target_companies.yaml`:

1. Verify the official careers page and exact source URL.
2. Select a platform in `PLATFORM_SOURCE_CONTRACTS`; add and review a contract
   before using any new platform key.
3. Bind the exact source identity and source scope.
4. Configure approved source/apply hosts.
5. Prove stable job identity and listing/detail relationships.
6. Add deterministic fixtures for success, continuation, terminal state, zero,
   malformed records, duplicates, mutation, limits, and redirects as relevant.
7. Set positive bounds capable of reaching the observed terminal; bounds remain
   safety controls, not terminal proof.
8. Run strict offline onboarding and focused tests.
9. Prepare one isolated candidate/canary registry that normal runtime cannot
   load.
10. After review and explicit authorization, run one bounded read-only canary
    with no persistence, dashboard publication, lifecycle mutation, or
    production-config mutation.
11. Activate only the exact tested configuration when the canary returns
    `PASS_COMPLETE`, all accounting is exact, and every identity/scope gate
    passes.

`PASS_PARTIAL`, `FAIL_IDENTITY`, `FAIL_CONTRACT`, and `FAIL_SOURCE` are
`DO_NOT_ACTIVATE`. `FAIL_TRANSIENT` is also non-activation and may be retried
manually later. A deterministic fixture alone is not live recovery evidence.

The primary offline command is:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity.py `
  --company "Company Name" `
  --strict-onboarding
```

The canary validator is list-only unless `--run` is supplied. Supplying `--run`
performs external requests and requires an explicitly reviewed registry,
company, and budget.

## Rollback contract

Every replacement proposal must preserve the exact prior production row in a
rollback registry or review record. Rollback replaces only the affected source
row; never leave old and new definitions active together unless they are
explicitly reviewed as distinct scopes under the multi-scope model.

A rollback restores configuration; it does not erase lifecycle history,
retire a scope, or imply that either source is healthy. Multi-scope schema and
forward database migrations are infrastructure and are not rolled back for a
single source. Record the reason and retain candidate/canary evidence.

## Provider-specific retained decisions

### Workday

Employer identity requires a reviewed first-party binding to exact tenant,
site, hostname, apply host, employer, and source scope. CXS reachability is not
identity. Totals below 2,000 may use ordinary bounded pagination. A total at or
above 2,000 is a cap signal and requires CXS-advertised recursive facets, exact
leaf totals, stable root recheck, and a unique-ID union exactly equal to the
root total. Hard bounds are depth 3, 128 states, 10,000 unique jobs, default
500 requests, and maximum 2,000 requests. Unsplittable cap-bound leaves fail
closed.

### Dayforce and SAP SuccessFactors

Dayforce supports only the reviewed `nextjs_jobposting_search` flow with exact
Candidate Portal route/site data, same-session CSRF, tenant/board identity,
fixed pagination, and raw-item accounting. The five audited production rows
historically lacked reviewed first-party identity; no general recovery should
be inferred.

SuccessFactors supports `rmk_table`, `rmk_tile`, and `rmk_columnized`. HTML
columns bind by unique headers, not position. Columnized detail must preserve
requisition, tenant, canonical URL, and apply identity. Other variants remain
unsupported until reviewed.

### ADP and browser-rendered HTML

ADP supports `workforce_now_requisitions_v1` and
`myjobs_requisitions_v1` with variant-specific bootstrap, identity, totals, and
detail rules. Other ADP surfaces are unsupported.

For browser sources, the collector owns navigation/final URL identity and the
delegated static adapter owns stable IDs, pagination, and terminal proof. Empty
DOM, timeout, selector/action failure, and scroll exhaustion are not terminal.
Generic infinite-scroll authority is not implemented.

### Greenhouse, SmartRecruiters, and XML

Greenhouse complete-board responses still require exact board/company/apply URL
identity. SmartRecruiters requires a stable authoritative `totalFound`, exact
company identity, non-overlapping unique IDs, and exact total reconciliation;
server-side partitions are authoritative only when their finite universe and
union are proven.

`source_xml` accepts only a reviewed first-party feed contract. The strict
same-run listing-count path requires exact listing identity before and after
the complete XML response, stable equal totals, safe XML parsing, unique IDs
and URLs, no continuation/rejection/truncation, and exact inventory equality.
HTTP 200, XML MIME, or a feed root alone is insufficient.

## Contract authority map

- Platform identity and terminal declarations:
  `src/scraper/company_careers/source_integrity.py`
- Canonical scan and absence decisions:
  `src/scraper/company_careers/source_health.py`
- Result, inventory, and target models:
  `src/scraper/company_careers/models.py`
- Source scope normalization: `src/utils/source_scope.py`
- Lifecycle reconciliation: `src/scraper/company_careers/freshness.py`
- Activation decisions: `src/scraper/company_careers/phase5_rollout.py`
- Provider implementation: the corresponding adapter and focused tests
- Production state: `config/target_companies.yaml`
- Historical status and unresolved work: [Roadmap](ROADMAP.md)
