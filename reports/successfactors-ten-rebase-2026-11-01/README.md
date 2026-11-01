# Ten-source activation after the timeout merge

PR #5 remains draft and requires separate merge/activation approval. This package records offline revalidation after PR #6 merged. No provider request, workflow dispatch, activation, database/Sheets write or merge was performed by this task.

Production main is exactly `956597386f889574ff0b65db01b34011bff91459`, PR #6's merge commit. Compared with prior main `84b16555e089a1d7f28bf5b547a3490917e42c78`, it changes only the workflow timeout and matching test assertion from 45 to 60. The workflow SHA-256 is `7a468eb1ba9480f51cf675c9ee41fd72c92fb725207cf4474653762f0f35ce2f`. Schedule, concurrency, budgets, retries, credential handling and failure behavior are unchanged.

The original activation commit `ac6e72078fd1e765a40881991f5e3371ab3700b4` was rebased cleanly onto that exact main. The only additional code change is the previously reviewed activation-validator compatibility guard. It accepts the original workflow hash unchanged, or the exact known 60-minute workflow hash only when the artifact name and original expected hash both match. Arbitrary workflow, source, scope, credential, permission, concurrency or other hash drift still fails closed. Nine deterministic tests cover the exact exception and rejected alternatives.

The ten sources are Keyence, TransAlta, Spartan Controls, ARC Resources, Gibson Energy, SaskPower, Hydro One, Omron, Canada Life and Hyundai. Their original certificates and source specifications are byte-identical and still apply: 520 jobs, 27 historically successful physical requests, no successful canary repeated. The source engine remains equivalent to the certified engine for these exact sources. Eight registry rows gain the original reviewed identity/apply-host fields; Gibson Energy and Hydro One need code only.

All other 523 effective configurations and parser routes remain baseline. Festo, Volkswagen Group, TELUS, Hatch and VIA Rail retain their original behavior and safeguards. All 533 logical identities/scopes remain unique. No completion, inventory-accounting, mutation, two-observation absence or cooldown protection is relaxed. [Applicability result](activation_readiness.json).

## Validation

- Strict source integrity: 533/533 OK.
- Complete default offline suite: 3,199 tests and 854 subtests passed; 430 existing private-evidence deselections and one unavailable frozen replay skip. This is not an unfiltered/private-evidence suite pass. [Complete log](full_offline.txt).
- Focused activation, operational policy, source-integrity, inventory and lifecycle checks: 512 passed, 36 subtests passed, one existing private-evidence deselection. [Focused log](focused.txt).
- Exact new 14-file code/configuration rollback applied and reversed only in a temporary copy; both sides match exact byte hashes. It preserves the already-merged 60-minute workflow and matching test assertion.
- Original seven-file source/runtime rollback remains applicable. Every original PR #5 historical report and certificate is unchanged. The new full-code rollback supersedes the old full-code applicability record for this rebased code; the old record remains historical evidence.
- Secret scanning and staged/unstaged diff checks passed. Independent review is recorded in [INDEPENDENT_REVIEW.md](INDEPENDENT_REVIEW.md). [Validation summary](VALIDATION.json).

## Runtime admission and residual risk

The accepted 60-minute policy removes the old 45-minute admission bottleneck for these ten recoveries. The retained combined stress illustration is 44m36.77s, leaving 15m23.23s of headroom including its existing 180-second reserve, under its stated one-second additional-RPC assumption. It is a sensitivity scenario, not a measured latency bound.

External database/server latency, Sheets response tails, quotas, setup delay and failure-recovery behavior remain operational risks. A hard cancellation or downstream outage can still leave committed source transactions with pending/partial dashboard publication. Existing staged-sync, idempotency and absence protections remain unchanged. No new external latency measurement was performed or inferred, and no admission is extended to unverified sources. The original [latency assessment](../successfactors-ten-activation-2026-10-30/LATENCY_RISK.md) remains intact as dated evidence; this package records the subsequently merged timeout policy.

Disposition: ready for a separate merge-approval decision for these ten sources, with the residual unmeasured-latency risk stated explicitly. No production coverage gain is claimed. Subsequent authoritative production completion still needs an independently authorized run.

## Exact rollback

Use this package's [complete_code_rollback.patch](complete_code_rollback.patch) and [rollback_verification.json](rollback_verification.json) for the rebased code. Require every matching after hash before considering application; if anything differs, STOP. The 14-file patch restores source/runtime/configuration/test changes to exact production main `956597386f889574ff0b65db01b34011bff91459` and removes newly introduced code/test files. It does not remove audit evidence or revert PR #6. No lifecycle history or external data is reset.

The patch uses zero context; `git apply --unidiff-zero --check` is necessary but does not replace the exact after-hash guard. All rollback execution requires separate authorization. After an eventual approved merge, a separately approved revert of the complete activation PR is the equivalent Git operation. Original [source rollback records](../successfactors-ten-activation-2026-10-30/source_rollback.yaml), [source/runtime rollback](../successfactors-ten-activation-2026-10-30/production_runtime_rollback.patch), and Astra evidence are preserved unchanged.

## Stage 3 remains separate

The sealed eight-source Workday manifest remains unchanged, unapproved and unexecuted at SHA-256 `8e4a0e23ab23cd26573d00f22d0eb40c25974a4a425d4a6aaee862b8ae5fe5f2`. Its 266-request allowance and all destination/budget/stop contracts remain unchanged. It cannot be executed against this changed checkout under its old implementation fingerprints; a future authorized execution must use its exact sealed checkout or a newly reviewed manifest. This PR does not activate Stage 3 or authorize a canary.
