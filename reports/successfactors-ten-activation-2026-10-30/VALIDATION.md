# Validation record

The exact activation checkout passed the complete repository default offline suite: **3190tests and854subtests**,1skipped unavailable frozen ranked replay and430private-evidence tests deselected by the existing `pyproject.toml` default. No additional exclusions were added, and no unfiltered-suite pass is claimed. Execution took179.08seconds outside the filesystem/network sandbox for local TestClient/AnyIO compatibility; standard pytest plugins were retained. No provider or external database/Sheets request was made.

Relevant focused adapter, explicit-boundary, inventory/hydration and multi-scope lifecycle checks passed550tests/29subtests with1default private-evidence deselection. Subsequent activation plus old Workday history checks passed873tests, and three fail-closed historical-projection guards passed. The complete final suite includes the final code, including the payload-equality guard and staged downstream benchmark test.

The first complete attempt encountered44collection errors from older whole-registry historical expectations. These were introduced compatibility failures, not pre-existing baseline failures. The fix validates the entire new activation, requires the supplied registry to equal actual production, restores only the exact ten historical rows in memory, and runs unchanged old artifact/hash/actual-Workday checks. Historical evidence was not rewritten, and failing tests were not excluded. The initial and final logs are retained separately.

Strict onboarding integrity:533/533OK. Actual loader verification:10certified targets and523unchanged targets, no duplicated logical scope or overlapping recovered board. All six runtime files match the prior consolidated review exactly; the certified engine AST is unchanged after the explicitly permitted additions that are unreachable for these ten boards.

All ten certificate bytes and source-spec fingerprints are pinned. Passing inventory520jobs/27historical physical requests remains unchanged; no successful canary was repeated. The five previously impacted sources preserve exact baseline dispatch/results/exception behavior. Two-observation absence, lifecycle, cooldown, central finalizer, source IDs and all unrelated source configuration remain unchanged.

Rollback checks apply both the seven-file runtime/config reverse and13file complete code/test reverse only in isolated temporary copies. Every before byte or prior absence matches production84b1655. No rollback was applied to a working source checkout.

[Structured validation](validation.json), [strict integrity](source_integrity.json), [complete final test log](full_offline_tests.txt), [initial collection failures](full_offline_initial.txt), [activation validation](offline_validation.json), [complete rollback verification](complete_code_rollback_verification.json).

The runtime model is conditional; [the external latency risk](LATENCY_RISK.md) remains unmeasured. Merge/activation and new canaries require separate approval. Production coverage remains unverified beyond the retained388/533 baseline.

Final package sealing checks passed387tests. New rollback patch files use zero context to avoid whitespace-only context lines; exact after fingerprints and the required `--unidiff-zero` flag were verified. Derived complete-code reverse metadata is outside the self-referential runtime lock and remains bound by the reviewed commit. The final complete suite was rerun after sealing and passed with the same3190test count.
