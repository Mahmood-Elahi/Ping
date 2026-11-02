# SuccessFactors production-preprocessing correction

This draft PR applies the exact previously reviewed four-file patch to production base `41221f57195182425322806c6506610ae964809e`. Run #366 could not reach the reviewed table parser: dashboard preprocessing changed `selective_detail_enrichment` from False to True, invalidating each source's exact profile hash before HTTP. The earlier registry/canary checks did not exercise that production transformation.

The correction enables automatic selective enrichment only for `rmk_columnized`, the variant that consumes it. Explicit target values are preserved. The sealed profiles, router, provider adapters, canonical companies/scopes/IDs, source health keys, host/query contracts, budgets, retries and lifecycle rules remain unchanged. The production registry and overlay are byte-identical to main. The 60-minute workflow, schedule, credentials and concurrency remain unchanged.

The ten certified table targets now survive actual production preprocessing unchanged: Keyence, TransAlta, Spartan Controls, ARC Resources, Gibson Energy, SaskPower, Hydro One, Omron, Canada Life and Hyundai. The 520-job certificates remain immutable historical observations. Compatibility with those inputs and engine is not a claim of current provider identity, access or completeness; no successful canary was repeated.

All 533 post-transformation routes are verified: 10 reviewed and 523 baseline. Thirty-nine table/tile inputs lose only an unused automatic flag. The five legitimately enriched columnized sources—CPKC, Bombardier, CN Rail, Seagate and Kinross Gold—retain their exact previous inputs and `selective_detail_enrichment=True`. The five previously healthy table sources retain baseline request traces and decisions. The ten uncertified Batch2 boards return to baseline dispatch; this PR does not activate them or resolve their separate blockers.

## Exact implementation scope

- `src/scraper/company_careers/student_roles.py`: restrict automatic selective enrichment to the columnized variant.
- `scripts/validate_successfactors_ten_activation.py`: check actual production-preprocessed targets, not only raw registry inputs. Preserve the old immutable lock, with compatibility only for the exact reviewed preprocessing-module hash pair.
- `tests/test_search_scope_expansion_v1.py`: make the existing enrichment fixture explicitly columnized.
- `tests/test_successfactors_production_configuration.py`: 283 regression cases covering production preprocessing/scanner dispatch, successful/zero/multipage inventory, missing totals, mutation, duplicates, limits, bad destinations, identity failures and the original validator blind spot.

No other source-code change is included. The remaining files in this PR are this dated audit/validation/rollback package. No existing historical file is rewritten.

## Closure audit and approval limit

The read-only audit reconciles all 112 reported closed-count units to 39 source scopes. All 39 were complete in run #366, active/authoritative in its unchanged registry, and finalized with `reconcile_absence=True` and threshold 2. The ten failed SuccessFactors sources have zero source-sync sessions and zero source-sync results for that run; they supplied no absence authority or reported closed counts.

The deployed metric counts closed stock associated with scopes, not strictly new closure transitions. Run #366's retained retention receipt records 112 removed/closed candidates plus 90 age-based candidates, deleting 202 canonical and 202 ownership rows. Current rows and the bounded catalog searches cannot reconstruct the deleted job identities or their individual before/after histories. A later scan is also retained. Therefore **zero individual closures are cleared; all 112 reported units remain unverifiable at job level**. No source-level authority violation was identified. This task did not execute retention, restore rows or modify production data. [Full audit and limitations](CLOSURE_AUDIT.md), [safe summary](closure_audit_summary.json), [source attribution](closure_source_attribution.json).

The draft is ready for technical review. **Do not request unconditional merge clearance from this audit.** Separate user approval must acknowledge the unresolved historical job-level evidence limit, or additional historical evidence must establish individual correctness. No merge, activation, provider verification or workflow execution is authorized by this package.

## Validation

- Strict integrity: 533/533 OK; actual production-preprocessing applicability: 10 exact certified contracts / 523 baseline routes. [Applicability](applicability.json).
- Complete default offline suite: 3,482 tests and 854 subtests passed; 430 existing private-evidence deselections and one unavailable frozen replay skip. This is not an unfiltered/private-evidence suite pass. [Full log](full_offline.txt).
- Focused suite: 864 passed, 84 subtests passed, one existing private-evidence deselection. [Focused log](focused.txt).
- Independent review and 293 independently executed tests, exact reverse/forward rollback and tamper rejection, secret scan, diff checks and historical/Stage 3 preservation. [Review](INDEPENDENT_REVIEW.md), [validation record](VALIDATION.json).

## Rollback procedure

Rollback remains separately authorized. Before applying the [four-file reverse patch](rollback.patch), require every current after hash in [rollback_verification.json](rollback_verification.json) to match. If any differs, STOP and review a new rollback. Then check the zero-context patch with `git apply --unidiff-zero --check` before any authorized application. The patch check does not replace the byte-hash guard.

Temporary-copy verification proves that reverse application restores all four exact production files or prior absence; reapplying the forward patch restores the reviewed fix. The 60-minute workflow, its matching test, all source configurations and identity profiles remain unchanged. A future separately approved revert of this complete fix PR is equivalent. No lifecycle history, counters or production data are reset. Restoring this code also restores the preprocessing failure; it does not recover provider completeness.

## Evidence preservation

All previous certificates, Astra reports, activation/diagnostic evidence and rollback packages remain unchanged. The Stage 3 manifest remains unapproved, unchanged and unexecuted at SHA-256 `8e4a0e23ab23cd26573d00f22d0eb40c25974a4a425d4a6aaee862b8ae5fe5f2`. Its sealed execution checkout is untouched. A future canary still requires its own explicit approval and exact implementation fingerprints.
