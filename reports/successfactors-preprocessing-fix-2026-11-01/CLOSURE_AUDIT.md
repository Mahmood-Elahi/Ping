# Read-only audit of run #366's reported closed count

The audit used existing GitHub run records and bounded SELECT/CTE queries against existing scan, source-sync, retention and lifecycle metadata. No provider request, synchronization RPC, retention RPC, schema operation or data mutation was executed. Detailed query evidence remains local; only necessary aggregate evidence is published here. Job descriptions, applicant content and credentials were not queried.

## Verified source-level accounting

Run #366 is [GitHub run 41355003540](https://github.com/Mahmood-Elahi/Ping/actions/runs/41355003540), commit `41221f57195182425322806c6506610ae964809e`. Its source-sync aggregates exactly match the public log: 480 synchronized scopes, 112 reported closed-count units, 166 missing increments, 50 inserted jobs and 57,724 updates.

Thirty-nine source scopes account for the 112 reported units. All 39 satisfy the available source-level checks:

- the exact canonical source/scope exists in the production registry and is active and authoritative;
- the retained public source outcome is complete;
- the persisted source session is finalized with `reconcile_absence=True`;
- `close_after_missing_runs=2` is recorded.

The [source attribution record](closure_source_attribution.json) lists every employer and its count. All reported counts are attributed; this is not a reconstruction or clearance of individual job closures.

For Keyence, TransAlta, Spartan Controls, ARC Resources, Gibson Energy, SaskPower, Hydro One, Omron, Canada Life and Hyundai, both source-sync sessions and committed source-sync results are absent for run #366. Their public outcomes are failed/zero jobs, and exact production-path replay excludes them from authoritative scopes. Their direct absence-authority and reported-closure contribution is zero. No source-level attribution to these failed sources was found.

## Meaning of the 112 metric

The currently deployed `finalize_student_job_source_sync(uuid)` computes `closed` by counting canonical rows whose status is closed and which have ownership in the synchronized scope. It does not restrict that count to rows newly transitioned by that invocation. The repository function has the same counting semantics. Absence updates separately require the session's reconciliation flag and threshold.

Consequently,112 cannot be asserted to mean 112 distinct new closure transitions. Historical stock and multiple ownership can affect the metric; without the lost row identities, duplicates or transition timing cannot be reconstructed. The preprocessing PR does not alter this metric or any lifecycle SQL. Metric wording and audit-history retention are separate follow-up concerns.

## Retention evidence and unavailable individual histories

Run #366's retained receipt records:

| Recorded category | Count |
|---|---:|
| Selected removed/closed candidates | 112 |
| Selected age-based candidates | 90 |
| Total selected job candidates | 202 |
| Deleted canonical rows | 202 |
| Deleted ownership rows | 202 |
| Selected candidates remaining | 0 |

The checked-in policy specifies zero days of retention for removed jobs. These are historical execution receipts, not operations performed by this audit. The receipt is bounded (`counts_complete=False`); it is evidence about its selected candidates, not a complete global history. Its matching count does not establish an identity-level bijection between the reported stock and deleted candidates.

Current queries find no closed canonical rows in the 39 reporting scopes and no retained ownership removals at run #366's timestamp. A subsequent scan is recorded at 22:35:37 UTC and its receipt shows 77 further removed/closed deletions. The bounded catalog searches found no matching job-history/event or retention/audit tables. This does not prove that no historical evidence exists elsewhere.

The available evidence therefore does not identify the 112 deleted records or preserve their previous missing counters, exact closure mechanism, prior sightings, protected inventory membership, sibling ownership or before/after canonical status. Those checks cannot honestly be recreated from aggregate receipts or from successful synthetic tests.

## Separate dispositions

| Audit question | Result |
|---|---|
| Reported count reconciled by source | 112/112 count units across 39 scopes |
| Reporting scopes with recorded authority consistent with public completion | 39/39 |
| Reporting scopes violating the available authority checks | 0 identified |
| Sync sessions/results for the ten failed SuccessFactors sources | 0/0 |
| Individual historical closures independently verified | 0 |
| Reported units unresolved at individual-job level | 112 |
| Suspicious source-level attribution | None identified |
| Individual closures proven correct or proven suspicious | Neither conclusion is established |

The ten failed sources did not supply the source synchronization/absence authority that produced these recorded counts. This is narrower than proving that every historical closure elsewhere was correct. **All 112 remain job-level unverifiable, rather than cleared.** The absence of a detected source violation is not evidence of zero individual mistakes.

The fix is technically independent of these lifecycle records and changes no persistence or retention behavior. The PR remains draft. Unconditional historical-closure clearance and unconditional merge readiness are not claimed; separate approval must acknowledge this limit or additional historical evidence must resolve it. [Machine-readable summary](closure_audit_summary.json).
