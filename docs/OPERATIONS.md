# Operations

This guide covers local setup, configuration, safe execution, outputs,
persistence, and deterministic verification. Read
[Source Contracts](SOURCE_CONTRACTS.md) before changing a source or running a
canary.

## Setup

Use Python 3.11 and install from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install uv
uv sync --locked --dev
python -m playwright install chromium
Copy-Item .env.example .env
```

`pyproject.toml` declares runtime and development dependencies, `uv.lock`
pins the complete cross-platform resolution, and `requirements.txt` is a
generated runtime-only compatibility export used by Docker and the existing
workflow. Regenerate it with
`uv export --locked --no-dev --no-hashes --no-emit-project -o requirements.txt`
after any dependency change.

Docker is optional infrastructure. Production runs Python directly in GitHub
Actions; no active workflow or production integration depends on a container
image. The checked-in `Dockerfile` and `.dockerignore` retain their existing
security protections, but an actual image build has not been verified for this
release and container deployment must not be advertised as verified.

The base `.env.example` documents supported names. Do not rename environment
variables for branding and never commit real values.

| Integration | Variables |
|---|---|
| LinkedIn | `LINKEDIN_USERNAME`, `LINKEDIN_PASSWORD`; optional `LINKEDIN_LI_AT`, `LINKEDIN_COOKIES_FILE`, `LINKEDIN_CHROME_PROFILE_DIR` |
| Document generation | `GOOGLE_API_KEY`, `GPT_4O_API`, `GPT_4_1_API` |
| Optional enrichment | `TAVILY_API_KEY` |
| API | `API_KEY`, `ADMIN_API_KEY`, `ENABLE_API_MIGRATIONS`, `ALLOWED_ORIGINS`, `LOG_LEVEL` |
| Supabase | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` |
| Lifecycle/sync | `CLOSE_AFTER_MISSING_RUNS`, `SUPABASE_REQUEST_TIMEOUT_SECONDS`, `SUPABASE_SYNC_MAX_ATTEMPTS`, `SUPABASE_SYNC_RETRY_DELAY_SECONDS`, `SUPABASE_SYNC_CHUNK_ROWS`, `SUPABASE_SYNC_CHUNK_BYTES` |
| Google Sheets | `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_PATH`, `NEW_JOB_WINDOW_DAYS` |

The service-role key and Google service-account file are backend secrets. Do
not expose them to client code, logs, fixtures, or committed artifacts.

## Command safety

| Command class | Network | External mutation | Notes |
|---|---:|---:|---|
| `pytest`, registry/source-integrity validation, report scripts | No | No | Preferred verification path |
| Dashboard `--dry-run` | No scan | No | Prints the plan only |
| Dashboard `--audit` | Yes | No | Live scan and diagnostics; Supabase, Sheets, SQLite disabled |
| `main.py search` | Yes | May write local artifacts/SQLite | Contacts selected providers |
| Student scan | Yes | May update local/Supabase lifecycle | Review flags and credentials first |
| Dashboard normal run | Yes | Yes: Supabase and Sheets | Production-capable operation |
| Canary validator without `--run` | No | No | Lists/reviews plan |
| Canary validator with `--run` | Yes | No persistence by design | Requires explicit authorization and bounded target |
| Retention execution or scope-retirement RPC | Yes | Yes | Administrative; require exact reviewed target |

Never treat a dry deterministic validation as live source health. Never run a
full-company scan merely to validate documentation.

## Search CLI

Direct company sources:

```powershell
.\.venv\Scripts\python.exe main.py search "Software Engineer" `
  --locations "Canada" "Remote" `
  --scrapers company_careers `
  --max-jobs 5
```

LinkedIn only:

```powershell
.\.venv\Scripts\python.exe main.py search "Data Scientist" `
  --locations "Remote" `
  --scrapers linkedin `
  --max-jobs 5
```

Combined:

```powershell
.\.venv\Scripts\python.exe main.py search "Software Engineer" `
  --locations "Canada" "Remote" `
  --scrapers linkedin company_careers `
  --max-jobs 5
```

`--max-jobs` controls retained results per site/location in the general CLI;
provider adapters may need to enumerate more raw rows to establish their own
completion contract. Use `main.py search --help` for the current parser.

LinkedIn can also run through
`python -m src.scraper.search.linkedin_scraper`. It is browser automation and
may encounter authentication, rate limits, or challenges. Use small bounded
runs, comply with applicable terms and law, and never interpret a challenge or
empty page as authoritative coverage.

## Student workflow

Plan without scanning:

```powershell
.\.venv\Scripts\python.exe scripts\update_student_dashboard.py --dry-run
```

Live audit without Supabase, Sheets, or SQLite mutation:

```powershell
.\.venv\Scripts\python.exe scripts\update_student_dashboard.py --audit
```

Normal update:

```powershell
.\.venv\Scripts\python.exe scripts\update_student_dashboard.py
```

`--skip-sheets` still updates Supabase. `--max-jobs` limits retained eligible
jobs per company, not necessarily raw provider requests. `--as-of` supplies an
explicit UTC-aware reference time for deterministic replay/testing.

The managed Sheets tabs are `New Jobs`, `Active Jobs`, `Companies`, `System
Status`, `Tech & AI`, `Chips & Hardware`, `Mobility & Robotics`, `Energy &
Industrial`, `Finance & Fintech`, and `Consumer & Health`. `Applications` is
never modified.

The checked-in workflow `.github/workflows/hourly-job-radar.yml` retains its
legacy identifier intentionally. Despite the historical “hourly” name, its
cron is exactly `17 */6 * * *`, every six hours at minute 17 UTC. It expects
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, and `GOOGLE_SHEET_ID` to be supplied through
GitHub Secrets. The service-account JSON is materialized as a mode-restricted
temporary file; it is never checked in.

Production preflight and the dashboard update run behind a public-CI logging
boundary. Ordinary stdout, stderr, descendant-process output, traceback text,
raw job records, integration responses, and local paths remain in
runner-temporary private logs. The dashboard has a separate versioned JSON-lines
channel containing only validated company names, bounded counts, canonical scan
states, allowlisted reason categories, stage states, and durations. The wrapper
validates every event again before writing readable Actions output or the job
summary; malformed, multiline, oversized, unknown, or control-character-bearing
events are rejected without echoing their content. The operational child cannot
write GitHub command files directly.

Company events are flushed as each contained source result finishes. A later
ranking, Supabase, source-health, dashboard-read, or Sheets failure therefore
does not erase completed source results. When scanning stops early, the wrapper
reports only observed companies and explicitly leaves unattempted companies
unclassified. The channel never carries exception text, request or response
content, credentials, endpoints, job descriptions, or candidate data. The
workflow uploads no diagnostic artifacts and deletes its temporary credentials,
private logs, and reporting sidecars in an always-run cleanup step.

## Production registry and related configuration

`config/target_companies.yaml` is the normal production registry. Each row has
a canonical `name`, reviewed `platform`, optional `source_scope`, exact
provider identity fields, source URLs/hosts, and safety bounds.

Important companion files include:

- `config/company_industries.yaml` — exactly one industry group per production
  company;
- `config/candidate_profile.yaml` — deterministic student preferences;
- `config/freshness.yaml` — freshness, urgency, absence, and retention policy;
- `config/company_parent_source_mappings.yaml` — reviewed parent-source
  mappings;
- `config/phase7_infrastructure_audit.yaml` — reproducible architecture audit;
- `config/remaining_source_recovery_pass.yaml` — latest 37-source closeout and
  36-source parked queue; and
- `config/pagination_completeness_recovery_batch*.yaml` — preserved recovery
  evidence and rollback/accounting decisions.

Files named `*_candidates.yaml`, `*_canaries.yaml`, `*_proposed.yaml`, or
`*_rollback.yaml` are isolated review/evidence inputs. Do not make the normal
loader read them. A candidate entry is not active production configuration.

Add a company only through the onboarding sequence in
[Source Contracts](SOURCE_CONTRACTS.md). Also assign its exact canonical name to
one industry group; validation and publication fail closed on missing,
additional, or duplicate assignments.

## Offline validation

Audit the production registry and retained artifacts without network access:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity.py
```

Strict onboarding:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity.py `
  --strict-onboarding `
  --retained-pattern "__no_retained_coverage__"
```

The concise classifications are `OK`, `REVIEW_CONFIG`, `TERMINAL_UNPROVEN`,
`SOURCE_STALE`, `MIGRATION_REQUIRED`, and `ARCHITECTURAL`. Use `--json` or
`--output` for machine-readable results.

The live-canary utility is safe to inspect without `--run`:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py
```

Do not add `--run` unless the exact isolated registry, company, request budget,
and authority to contact that provider have been reviewed.

Other report/validation scripts under `scripts/` are deliberately narrow.
Read their parser and matching config/test before running them; names beginning
with `probe`, research/fingerprint scripts, and flags such as `--audit` or
`--run` may contact external sites.

## Outputs and interpretation

Generated artifacts normally live under ignored `jobs/` or `output/` paths.

| Pattern | Meaning |
|---|---|
| `job_postings_*.json` | Normalized jobs from the selected pipeline |
| `company_careers_ranked_*.json/.csv` | Ranked review data |
| `company_careers_coverage_*.md/.csv/.json` | Per-source coverage, authority, and diagnostics |
| `company_careers_failures_*.json` | Technical/manual follow-up records |
| `student_search_diagnostics_*.json` | Student filter/ranking diagnostics |

`fetched_no_matching_roles` means the source ran but no role matched the
current query. It is not equivalent to failure. Conversely, a source may
produce useful jobs while remaining incomplete and non-authoritative for
absence. Review `scan_state`, `source_health_code`, `scan_complete`,
`authoritative_for_absence`, terminal evidence, stop reason, and accounting
together.

## Supabase and retention

For a clean project, apply `sql/supabase_schema.sql`. For an existing project,
apply every migration in filename order and run `sql/phase5_multi_scope_verify.sql`
after the Phase 5 chain. Migrations and the clean schema must converge on the
same RLS/service-role-only model.

### Source-sync optimization deployment and rollback

`20261015_optimize_source_sync_finalizer.sql` contains `CREATE INDEX
CONCURRENTLY`; run the file with autocommit enabled. A migration runner that
wraps the whole file in a transaction is not compatible. Do not raise
`statement_timeout` as the rollout and do not remove or weaken retention
indexes to manufacture HOT-update eligibility.

The exact production sequence is:

1. **Approval required:** confirm the production GitHub Actions workflow is
   disabled. Keep its `17 */6 * * *` schedule unchanged.
2. **Approval required:** confirm every external writer is idle, including
   dashboard updates, finalizers, retention execution, scope retirement, and
   operator SQL sessions that can mutate the synchronization tables.
3. **Approval required:** confirm the approved Supabase backup/recovery
   capability, and record the recovery-point identifier, restore owner, and
   tested restore procedure.
4. **Read-only after database-access approval:** run
   `sql/20261015_source_sync_optimization_verify.sql` and save every result set.
   Preserve the complete `function_definition`, not only its hashes. The
   verifier reports exact stale-active-`removed_at` drift separately from every
   other lifecycle inconsistency.
5. Resolve any function-body mismatch before data remediation or migration.
   Save the deployed `pg_get_functiondef` result as plain SQL and run:

   ```powershell
   .\.venv\Scripts\python.exe scripts\compare_source_sync_finalizer.py `
     --deployed <captured-function.sql>
   ```

   `TOKEN_EQUIVALENT` proves only that the difference is whitespace, comments,
   or unquoted identifier casing. `TOKEN_DIFFERENCE_STOP`, a parse failure, or
   a missing deployed definition is a substantive/unresolved blocker. The tool
   never updates an accepted hash. Even for token equivalence, record the
   deployed definition and both fingerprints and make a separately reviewed
   repository change before accepting its exact normalized-body hash.
6. **Separate data-remediation approval required:** while the same writer
   freeze and recovery point remain valid, run
   `sql/20261015_canonical_removed_at_remediation.sql` as `postgres`. It must
   report exactly 81 eligible rows, zero other lifecycle inconsistencies, 81
   snapshotted rows, 81 updates, and zero postflight drift. The transaction
   creates
   `public.canonical_removed_at_remediation_20261015_snapshot`, revokes API and
   service-role access to it, and changes only canonical
   `tracked_jobs.removed_at`. Keep the snapshot through the observation window.
   Any count or guard mismatch rolls back the whole transaction and requires
   new diagnosis; never edit `81` merely to make it run.
7. **Read-only after database-access approval:** rerun the verifier and save all
   results. Require `canonical_lifecycle_inconsistency_count=0`,
   `canonical_stale_active_removed_at_count=0`, and
   `canonical_other_lifecycle_inconsistency_count=0`.
8. Require `PASS_BASELINE_READY` and `safe_to_apply_migration=true`. The
   verifier requires the sole overload to be exactly
   `public.finalize_student_job_source_sync(uuid) -> jsonb`; hashes the actual
   deployed PL/pgSQL body; checks owner `postgres`, `SECURITY DEFINER`, search
   path `public, extensions, pg_temp`, exact materialized execute ACL
   `postgres:EXECUTE:false` plus `service_role:EXECUTE:false`, effective
   execution for those two roles, and no `PUBLIC`, `anon`, or `authenticated`
   access. The owner's materialized grant-option flag is not ownership
   authority. The verifier also checks the exact checkpoint trigger, retention
   index, candidate scope index, and canonical/ownership lifecycle parity.
9. Preserve the deployed function definition, normalized body hash, owner,
   function configuration, execute ACL, overload inventory, trigger definitions,
   and index definitions with the recovery record. A signature alone is not a
   rollback record.
10. **Migration approval required:** recheck writer idleness, then apply
   `sql/migrations/20261015_optimize_source_sync_finalizer.sql` with autocommit
   enabled. The preflight compatibility block runs before `CREATE INDEX
   CONCURRENTLY`; the index statement runs outside an explicit transaction, and
   the function replacement runs in its own later transaction.
11. **Read-only after database-access approval:** rerun
   `sql/20261015_source_sync_optimization_verify.sql` and save every result set.
12. Require `PASS_OPTIMIZED` and `post_deployment_verified=true`. Confirm the
   exact two-column B-tree scope index is valid, ready, and live with no
   predicate, expression, included column, or reordered key; reconfirm the
   function body/security contract, sole overload, exact checkpoint trigger,
   exact retention index, and lifecycle parity.
13. **Separate approval required:** perform one controlled, bounded production
    synchronization for a reviewed source scope. Confirm lifecycle and
    source-ownership counts, replay idempotency, and the absence of unrelated
    scope changes.
14. Monitor PostgreSQL `57014`/statement-timeout events, finalizer duration,
    locks and lock waits, failed staging sessions, row/index write volume, and
    lifecycle integrity through the approved observation window. Offline tests
    do not prove production performance or that the timeout is resolved.
15. **Approval required:** re-enable the unchanged six-hour GitHub Actions
    schedule only after the controlled synchronization and monitoring checks
    succeed.

Stop immediately and keep all writers disabled for any verifier `STOP_*`
result, either required boolean being false, a backup/recovery gap, writer
activity, a baseline-preflight diagnostic, a concurrent-index failure, an
invalid/not-ready/not-live index, a definition/owner/configuration/ACL/overload
mismatch, trigger drift or duplication, lifecycle drift, a timeout, or
unexpected lifecycle/ownership changes. `IF NOT EXISTS` is not proof that a
same-named index has the required definition. If concurrent creation leaves the
exact named scope index invalid or unfinished, inspect its catalog identity;
drop only that index with `DROP INDEX CONCURRENTLY` and retry only under a new
approval. Never weaken a contract to continue the rollout.

Rollback the two changes independently:

- To reverse only the lifecycle remediation, keep writers disabled and run
  `sql/20261015_canonical_removed_at_remediation_rollback.sql`. It aborts unless
  all 81 canonical rows and their ownership aggregates still exactly match the
  snapshot, and it restores only the captured `removed_at` timestamps. This
  deliberately restores the known 81-row drift, so the verifier must return
  `STOP_CANONICAL_LIFECYCLE_DRIFT`; do not proceed to migration. Do not use this
  rollback after legitimate lifecycle changes.
- To reverse the optimization, disable writers again, restore the captured
  pre-deploy function definition and its owner/configuration/ACL, verify it,
  and then decide whether to retain the additive index. If and only if the saved
  baseline verifier output exactly matched the repository baseline, reapplying
  `sql/migrations/20261010_phase5_scope_local_finalizer.sql` is the verified
  function rollback. Dropping the additive index is optional; if required, use
  `DROP INDEX CONCURRENTLY IF EXISTS
  public.tracked_job_source_scopes_scope_source_idx;` in autocommit mode after
  checking its exact identity.

After either rollback, rerun the read-only verifier and save the result before
re-enabling writers. Keep the remediation snapshot as audit evidence. The prior
function also restores the prior timeout risk, so rollback is not evidence of
recovery.

Normal source sync uses bounded retry and staged chunks. The default absence
threshold is two qualifying runs. Retention is configured for a 60-day maximum
job age, stale-output filtering, zero-day removed-row retention, and seven-day
sync-history retention. These storage policies never create source-completion
evidence.

`scripts/prune_supabase_retention.py` and
`retire_student_job_source_scope` can mutate production data. Inspect their
plan/read-only modes and exact target before execution. Retirement requires an
explicit reason and approver; it is not a recovery shortcut.

## Replays

Candidate-profile and freshness behavior have offline replay scripts:

```powershell
.\.venv\Scripts\python.exe scripts\replay_candidate_profile.py
.\.venv\Scripts\python.exe scripts\replay_freshness_v1.py
```

Freshness replay supports `--source`, `--as-of`, `--json-output`, and
`--markdown-output`. Historical records without trustworthy discovery times
are marked `backfilled_at_reference_time`; that must never be presented as an
actual first-observed timestamp.

## API

Start with:

```powershell
.\.venv\Scripts\python.exe main_api.py
```

The current implemented routes include public health route `/`, authenticated
`POST /search`, authenticated `/ws`, `POST /search/check`,
`GET /search/history`, `GET /search/{search_id}`, and authenticated
SQLite-backed `/jobs` routes. There is no fallback credential: missing
`API_KEY` configuration makes operational HTTP routes return 503 and WebSocket
connections close before acceptance. Browser clients use the `api_key`
WebSocket query parameter; non-browser clients should prefer the `X-API-Key`
header. Operational output is not mounted as a public static directory.

Set `ALLOWED_ORIGINS` to an explicit comma-separated list of HTTP(S) origins;
wildcards and values with paths fail startup. `POST /jobs/migrate` also
requires `X-Admin-Key` matching `ADMIN_API_KEY` and remains disabled unless
`ENABLE_API_MIGRATIONS=true`. Keep that endpoint disabled in normal service.
Use generated OpenAPI at `/docs` as the route-level reference. Older examples
of active `/parse` and `/process` HTTP endpoints were removed because those
handlers are commented out.

## Deterministic verification

Run focused tests for changed behavior first, then the full suite when risk or
scope warrants it:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The default command is the supported public offline suite. Pytest deselects
tests marked `private_evidence`; their exact selectors and required local
inputs are recorded in `tests/private_evidence_manifest.json`. Those tests
retain their original assertions and can be run by an authorized operator who
has restored the ignored evidence files:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m private_evidence
```

Missing private inputs remain hard failures when that category is selected;
they are never converted into passing tests or runtime skips. Historical
validation reports, coverage snapshots, raw provider responses, rankings, and
operator evidence must not be copied into the public repository merely to make
that optional category green.

Documentation/reference checks should confirm:

- exactly `README.md`, `AGENTS.md`, and the four files under `docs/` remain as
  project Markdown;
- all relative Markdown links resolve;
- no removed document is referenced by active source, config, tests, or docs;
- the README/config counts agree with `config/target_companies.yaml`; and
- user-facing branding says Ping while stable technical/historical identifiers
  remain unchanged.

Always finish with:

```powershell
git diff --check
git status --short
```

Do not commit, push, merge, deploy, trigger workflows, or mutate external state
as part of documentation verification.

## Evidence retention

Historical Markdown was consolidated, but reproducibility remains in code,
tests, YAML/CSV/JSON fixtures, SQL migrations, retained runtime artifacts, and
operator-provided evidence. Do not delete recovery configs, candidate/rollback
registries, focused tests, `batch9_*_results.zip`,
`config/job_radar_run_history.json`, or `evidence/` merely because their prose
summary moved to [Roadmap](ROADMAP.md).
