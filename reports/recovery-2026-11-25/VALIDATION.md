# Resumable source validation approval

The latest retained production run measured 372 COMPLETE, 122 INCOMPLETE and 39 FAILED across 533 sources. The validation manifest covers the original 21 staged sources, all eight historical certificate regressions, and three additional sources with a recent production COMPLETE and an applicable transport or mutation repair. Apple, Infineon Technologies, Halliburton and PCL Construction already completed in run #421 and their entries make zero requests. The remaining 20 entries could raise coverage to 392/533 if each reaches COMPLETE in production and the existing 372 hold. [Production run #421](https://github.com/Mahmood-Elahi/Ping/actions/runs/45707674434) · [Approval manifest](validation-approval.json)

## Production health approval

Authorize a read-only export using [validation-health-export.sql](validation-health-export.sql) before approving provider requests. The query runs inside a read-only transaction and returns UTC expiry, failure streak, classification, most recent authoritative success and snapshot time for 106 exact keys. Those keys cover the cooldown cases from both retained runs and the validation targets, including applicable candidate fingerprints. No provider request is needed for the export.

Save the result as CSV and use its absolute path in the validation command. Export just before each invocation or resume. The runner rejects a snapshot older than one hour, a snapshot predating run #421, inconsistent UTC data and missing validation keys. If another production pass ran after the export, refresh the snapshot before starting. An active deadline defers only its source. If a source has two relevant fingerprints, both must be eligible.

## Provider campaign approval

Approve the exact manifest digest below and the following bounds. The runner may start at most **592 physical HTTP attempts** across all invocations. Per-source request limits, worker timeouts, exact production or candidate target hashes, source scopes, HTTP methods and hostnames appear in the manifest. Workday POSTs are restricted to the exact CXS board paths. The transport meters redirects and retries and stops on access restrictions. It preserves HTTP 429 deadlines across workers and resumes, records status or transport exception, and uses the current Workday pacing policy.

From the checkout for the campaign PR, run this same command for the first invocation and any resume after supplying a fresh authorized export.

```sh
python scripts/run_recovery_validation.py \
  --approve-and-run f68e002b9e2dd7e35ee29eca9cca7bbf37998c6f11dfbf73eb210c20c7be2f82 \
  --health-export /absolute/path/to/authorized-current-source-health.csv
```

The runner stores its exclusive lock, SQLite checkpoint and results under `output/recovery-validation-45707674434/`. Each request is charged before it is sent. Every source has a separate time-bounded worker. A worker failure or an interrupted reservation is retained and a later invocation only considers never-attempted or deferred sources. Preserve the checkpoint directory between invocations. An uncertain Workday wire result holds other sources on the same host for review. Evidence records the actual physical status, exception class and provider deadline where observed. The campaign creates no production data, activates no source, and grants no lifecycle authority.

Review successful results for `PASS_COMPLETE`, exact source and target fingerprints, terminal evidence, unique inventory identifiers and canonical URLs. The private checkpoint records each successful stable ID and URL pair and its digest. Candidate activation requires fresh certification, change-specific rollback, and separate authorization for merge, deployment or a production workflow. These records are prepared for review and should stay out of Git.
