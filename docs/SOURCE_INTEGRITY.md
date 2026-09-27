# Source integrity

Job Radar treats source execution and source coverage as separate outcomes. A
process may finish while coverage is degraded. Only a source with independently
proven terminal evidence is `complete`; incomplete and failed sources can retain
useful jobs but cannot advance lifecycle absence.

## Completion contract

Adapters publish `SourceScanMetadata`. The scanner passes that evidence through
`finalize_source_scan`, which produces one canonical state:

- `complete`: reviewed terminal evidence is present and non-contradictory.
- `incomplete`: useful enumeration may exist, but the collection boundary was
  not proven.
- `failed`: the source could not produce a valid collection.

Accepted terminal strategies include an authoritative total reached exactly,
an explicit `has_more=false`, a terminal cursor/page contract, or a validated
complete-collection response. A safety budget (`max_jobs`, `max_pages`, request
count, timeout, byte limit) is never terminal evidence. Continuation,
collection mutation, malformed/rejected records, repeated pages/cursors, or a
reported total greater than the validated inventory always wins over a terminal
hint and makes the scan incomplete.

Zero jobs are complete only when a reviewed empty-collection contract proves
the empty inventory. HTTP 200, an empty parser result, a short page, or a missing
selector is not such proof.

## Source identity and redirects

Registry sources declare their platform identity, official endpoint or tenant,
approved hosts, stable job identity, enumeration strategy, terminal strategy,
and safety bounds. Redirects are accepted only inside the reviewed host
contract. A redirect to a new ATS, login provider, aggregator, generic homepage,
or unrelated host is non-authoritative and should be classified as stale,
blocked, or migration suspected. A candidate replacement is not trusted until
it completes the onboarding workflow below.

## New-company onboarding gate

Before adding a company to `config/target_companies.yaml`:

1. Verify the official careers page and record the official ATS/source URL.
2. Select an adapter present in `PLATFORM_SOURCE_CONTRACTS`; a new adapter must
   add a reviewed terminal strategy there.
3. Configure the tenant/site/board/slug or equivalent source identity.
4. Configure hostname-only `allowed_apply_hosts` (and source hosts where the
   adapter requires them).
5. Prove a stable source job ID and listing/detail identity relationship.
6. Add deterministic fixtures for enumeration, continuation, terminal evidence,
   zero jobs, malformed records, limits, and redirects relevant to the adapter.
7. Set positive safety bounds capable of reaching terminal evidence. If retained
   evidence exceeds a non-adaptive bound, fix the configuration before activation.
8. Run:

   ```text
   .venv\Scripts\python.exe scripts\validate_source_integrity.py --company "Company Name" --strict-onboarding
   ```

9. Run the focused adapter and lifecycle tests. Do not activate a source reported
   as `ARCHITECTURAL`, `SOURCE_STALE`, or `MIGRATION_REQUIRED`.
10. After offline review, run a bounded manual live canary. Do not write database,
    dashboard, or lifecycle state during onboarding validation.

A source with an unknown or incomplete declaration is derived as `partial` or
`quarantined`. It may return useful observations, but the scanner forces it
non-authoritative until the contract is repaired.

Registry `active` is a configuration/onboarding state: the entry declares a
supported source identity and reviewed adapter contract. It is not a claim that
the source is currently reachable, healthy, or complete. Those facts come only
from the current runtime scan's `scan_state`, `source_health_code`, and terminal
evidence. CI can validate the declared contract, but cannot claim live health.

## Lifecycle and repair

Only complete sources enter `complete_companies` during reconciliation. Missing
jobs require two consecutive complete absences (configured in
`config/freshness.yaml`) before removal. After repairing a broken source, obtain
a complete scan, preserve the first absence as `possibly_missing`, then require
the next complete absence before retirement. Never bulk-close jobs from the
first recovered scan.

## Offline audit

Run the registry and retained-artifact audit without network access:

```text
.venv\Scripts\python.exe scripts\validate_source_integrity.py
```

Use `--json` or `--output output/source_integrity.json` for machine-readable
results. Classifications are `OK`, `REVIEW_CONFIG`, `TERMINAL_UNPROVEN`,
`SOURCE_STALE`, `MIGRATION_REQUIRED`, and `ARCHITECTURAL`.

The bounded live-canary plan is list-only unless `--run` is supplied:

```text
.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py
.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --run
```

The default set contains five representatives and independently caps each at 15
HTTP requests. It performs no database, Sheets, lifecycle, or artifact writes.
ATCO is a permanent authoritative-total representative. A canary reports
`PASS_COMPLETE`, `PASS_PARTIAL`, `FAIL_SOURCE`, `FAIL_CONTRACT`, or
`MIGRATION_SUSPECTED`; an intentional request-budget stop is `PASS_PARTIAL`
with an incomplete scan and `safety_limit_reached`, never source-unreachable
and never authoritative for lifecycle absence.
