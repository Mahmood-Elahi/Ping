# Exact rollback

No rollback was executed against a working production tree or external store.

`source_rollback.yaml` preserves the exact ten original registry rows and original overlay states. All ten original overlay states are null; the existing overlay file remains byte-identical. Gibson Energy and Hydro One are code-only recoveries: restoring registry rows alone cannot undo them.

`production_runtime_rollback.patch` restores the seven source-runtime/configuration files to production84b16555e089a1d7f28bf5b547a3490917e42c78, including original registry bytes. `rollback_verification.json` pins each before/after byte hash or prior absence. Deterministic tests check matching after fingerprints, apply the patch in an isolated temporary copy and verify every prior byte/absence. It changes no lifecycle records, job IDs, persisted first-seen history or absence counters.

`complete_code_rollback.patch` additionally reverses the new offline validator/benchmark/test code and the historical assertion integration. Its separate verification record checks every code/test file against the same production baseline in an isolated copy. Audit evidence remains retained. Prefer a separately authorized revert of the complete merged PR when backtracking all code; the complete code patch supplies a reviewable equivalent for its code/config files.

After an authorized rollback, the original defects return; these sources may again fail/incomplete. No registry removal or source retirement is implied. Never apply an old whole-registry snapshot over subsequent unrelated changes. The patches use zero context to avoid whitespace-only context lines; matching after fingerprints supply the exact applicability guard. Require every applicable after hash to match; otherwise STOP and review a new exact rollback.

Commands are documentation only and grant no rollback authorization:

```sh
git apply --unidiff-zero --check reports/successfactors-ten-activation-2026-10-30/complete_code_rollback.patch
git apply --unidiff-zero reports/successfactors-ten-activation-2026-10-30/complete_code_rollback.patch
```

Historical Workday, Astra, SuccessFactors implementation/consolidation rollback packages remain unchanged in their original locations. The new package composes their history rather than regenerating it from modified configuration.
