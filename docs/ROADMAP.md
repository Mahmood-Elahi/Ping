# Roadmap and historical record

This document preserves the seven-phase architecture roadmap, later source
recovery work, historical validation results, and unresolved decisions. Dates
and counts are evidence snapshots, not claims of current live source health.

## Current position

As of the repository state reviewed on 2026-10-12:

- The seven-phase architecture roadmap is complete. Phases 6 and 7 closed
  early because further shared optimization/infrastructure was not justified by
  deterministic evidence.
- The production registry contains 533 canonical companies, 533 configured
  sources, 533 logical scopes, 41 active platform keys, and no production
  multi-scope company.
- Incremental-crawl infrastructure exists, but zero production adapters enable
  it; full crawling remains the correctness baseline.
- The latest retained source-recovery closeout recovered ServiceNow from
  operator-provided `PASS_COMPLETE` evidence and parks 36 unresolved sources.
- `READY_FOR_CANARY` is empty in that closeout. Another broad recovery pass is
  not justified without new first-party identity, scope, terminal, active-only,
  or finite-partition evidence.

Architecture completion does not mean every source is healthy. The 36-source
queue is a dated maintenance set, not a present-day complete/incomplete/failed
denominator. Establishing current global health would require a live full scan,
which was not part of this consolidation.

## Seven-phase roadmap

| Phase | Goal | Final status | Preserved decision |
|---:|---|---|---|
| 1 | Screen generic/fragile sources for safer ATS migrations | Complete | All 48 original targets were screened. Eight bounded, identity-valid Class A migrations were activated historically: Waymo, ASML, SharkNinja, Box, Redis, Ripple, Miro, and Wise. Unsafe replacements were rejected or handed to later phases; no runnable Phase 1 canary remains. |
| 2 | Replace implicit/session result handoff with explicit adapter results | Complete | The historical 43 canonical dispatcher boundaries returned `AdapterResult`; compatibility aliases remained diagnostic only. Central finalization became the sole authority boundary. |
| 3 | Separate inventory from hydration and add source reliability | Complete | Stable inventory items, unreadable/rejected accounting, safe partial progress, source-local cooldowns, staged persistence, and lifecycle protection were implemented. A provider-wide circuit breaker was deliberately deferred. |
| 4 | Harden high-risk provider protocols | Complete | Workday cap/partition identity, Dayforce, SuccessFactors, ADP, and browser contracts were modeled and tested. Completion of the protocol layer did not activate identity-unproven sources. Unsupported variants remained explicit. |
| 5 | Add multi-scope lifecycle and coverage expansion | Complete | Scope-local ownership, exact equivalence, explicit retirement, bounded retention, and transactional finalization were deployed. Lightspeed France failed identity twice and was rejected; it was not activated. |
| 6 | Add safe incremental optimization | Closed early | Typed checkpoints, positive-only authority, forced-full fallback, material fingerprints, and snapshot primitives were added. All 41 active families remained capability `none`; no production optimization cleared both correctness and material-savings gates. |
| 7 | Decide whether new shared infrastructure was required | Closed early | Existing HTTP/adapters/browser paths were adequate. Two isolated network-block observations did not justify proxy, browser-worker, authentication, or external-inventory infrastructure. Remaining issues became source-specific maintenance. |

### Phase 1 — ATS migration screening

Phase 1 compared existing sources with concrete ATS leads, required current
source parity and rollback, and refused to weaken identity or scope. The final
screen accounted for all 48 original generic/fragile targets. Important
handoffs included Workday identity/cap work to Phase 4, inventory/detail-heavy
SmartRecruiters cases to Phase 3, and distinct subsidiary/regional/early-career
boards to Phase 5.

Rejected replacements did not downgrade retained sources or make them
authoritative. Examples included stale/404 mappings, narrower regional boards,
identity-unproven Workday/Eightfold candidates, and alternatives weaker than an
existing first-party feed. Exact prior definitions remain in
`config/target_companies.phase1_ats_rollback.yaml`.

### Phase 2 — explicit adapter results

The adapter boundary moved from session-side metadata to explicit
`AdapterResult` values containing jobs, inventory, scan observations, item
failures, and descriptive capabilities. All production dispatcher boundaries
were migrated. The remaining session aliases support internal mutation restart,
debugging, and legacy injected tests; the scanner ignores them after receiving
an explicit result.

### Phase 3 — inventory, hydration, and reliability

Phase 3 classified provider paths as atomic, inventory-plus-optional-detail,
inventory-plus-required-detail, coupled, or browser/custom. It established that
inventory existence and normalized detail success are different facts.
Unreadable identities protect prior rows without inventing placeholders;
incomplete positive observations may persist without absence authority.

Cooldown state became source/config-local and forward-deployed through
`20261009_phase3_source_reliability.sql`. Only comparable transient failures
participate; authoritative recovery clears the streak.

### Phase 4 — protocol hardening

Workday now separates CXS reachability from employer identity and treats totals
at or above 2,000 as a partition requirement. Exact facet echoes, leaf totals,
stable root recheck, mutation restart, and union equality are mandatory.

Dayforce retained only its evidenced Next.js/CSRF/search contract.
SuccessFactors retained three RMK variants with header/identity-safe parsing.
ADP retained Workforce Now and MyJobs variants. Browser collection requires
approved final URLs, employer markers, stable identity, and a real provider
terminal; rendering, empty DOM, or action exhaustion is insufficient.

The Phase 4 architecture was complete even though many audited production rows
lacked first-party identity blocks. No completion or activation was inferred
from protocol support alone.

### Phase 5 — multi-scope lifecycle

Phase 5 proved that one employer may have overlapping global, regional,
university, subsidiary, or facility authorities. The old company-wide absence
model was unsafe. The new model added flat `(company, source_scope)` registry
identity, per-scope ownership, exact-only equivalence, atomic scope-local
finalization, and explicit administrative retirement.

The migration, bounded-retention, pgcrypto-resolution, and affected-key-only
finalizer chain was deployed and operator-reported healthy. The final candidate,
Lightspeed France, returned `FAIL_IDENTITY` twice because the proposed
Greenhouse board returned HTTP 404. It was rejected and never activated; this
fail-closed outcome completed the phase.

### Phase 6 — incremental optimization

Phase 6 implemented architecture without enabling a production adapter.
Greenhouse and SmartRecruiters were explicitly classified full-crawl-only;
their available timestamps are posting dates, not proven material versions.
The exhaustive matrix found no remaining family with both a deterministic safe
change boundary and meaningful proven request savings.

At review time there were 102 Greenhouse sources and 17 SmartRecruiters
sources. For both families, expected safe request savings remained zero and the
production diff remained empty. Future incremental canary: none. No exact
manual incremental canary is published. There is no future activation diff.
Incremental logic must never infer absence from an incremental slice. Wise
remains research-only, not an incremental activation candidate.

The sibling Eightfold pattern in
`jobseek/apps/crawler/src/core/monitors/eightfold.py` was classified as an idea
to adapt only: it combines an independently complete sitemap with PCSx
enrichment. Ping has neither equivalent sitemap authority nor a separate costly
hydration path, so the pattern did not justify production work.

The preserved rollout stages are:

1. Stage A: generic architecture and zero activations — complete.
2. Stage B: one source-specific fixture/canary only after new evidence.
3. Stage C: compare requests/runtime and material equivalence with full crawl.
4. Stage D: expand one contract at a time with equivalent proof.
5. Stage E: periodically audit full reconciliation, protected identities,
   scope isolation, and retention separation.

`NEXT_DEEP_REVIEW = NONE` and `PHASE6_CLOSE_EARLY` remain the controlling
decision until new provider evidence appears.

### Phase 7 — infrastructure audit

The 2026-10-01 retained full snapshot recorded 472 complete, 47 incomplete,
and 14 failed sources. Later source-specific evidence changed individual rows,
so those counts must not be projected into a current global total. The audit
found no source proving the need for a new rendering worker, authenticated
provider integration, external inventory layer, or proxy system. GitHub and
Parkland were isolated network-block observations, not infrastructure canary
proposals.

Reopen Phase 7 only when at least three production sources show repeatable
infrastructure-only failures after source-specific causes are excluded, a
bounded canary preserves all authority contracts, existing paths cannot solve
the problem, and cost/security/compliance/correctness tradeoffs are explicitly
accepted.

## Post-roadmap source recovery

Roadmap completion was followed by narrower pagination/completeness batches and
source-specific closeouts. These did not reopen the seven architecture phases.

- Batch 9 originally investigated 20 sources and recovered none without new
  live evidence. Its deterministic work preserved fail-closed behavior for
  static/browser, SmartRecruiters, SAP, Workday, Eightfold, Atlassian, Meta,
  iCIMS, and Google cases.
- The Batch 9 bounded probe utility added strict allowlists, per-company request
  ceilings, safe redirect/XML handling, resumable partition evidence, and
  offline reconciliation. Diagnostic success was never automatic activation.
- Batch 10 retained Dyson and Chemelex work, kept Roblox's existing source, and
  explicitly did not claim recovery for Wise, Box, Espressif Systems, or
  Cameco.
- Pagination recovery activated only exact live-tested candidates. Bosch later
  passed at 4,950/4,950 within a 5,000-job bound; SGS passed at 4,646/4,646;
  ServiceNow passed at 697/697 in seven listing requests. Moving totals,
  duplicates, overlap, identity mismatches, or bound overflow still fail closed.
  Bosch therefore had 50 jobs of remaining capacity at that validation point;
  this was headroom, not a permanent completeness guarantee.
- The latest commits added discovery-only inventory authority. Discovery-only
  sources can make positive progress but cannot authorize absence or masquerade
  as a successful authoritative inventory.

## Parked 36-source maintenance queue

The latest retained closeout groups unresolved production sources as follows.
These statuses were not revalidated by this documentation change.

| Status | Count | Sources |
|---|---:|---|
| `NEEDS_SOURCE_SPECIFIC_REPAIR` | 17 | Atlassian; JPMorgan Chase; Emerson; Honeywell; Microsoft; Eaton; Oracle; Stantec; CPKC; Scotiabank; Bombardier; Fortinet; CN Rail; HP Inc.; Seagate; AstraZeneca; Kinross Gold |
| `INSUFFICIENT_EVIDENCE` | 11 | Meta; Qualcomm; Micron; Rivian; Goldman Sachs; GlobalFoundries; Espressif Systems; ecobee; Nordic Semiconductor; Athabasca Oil; Nutrien |
| `FAIL_IDENTITY` | 7 | Samsung Electronics; GE Vernova; General Motors; Caterpillar; Zalando; Capital One; Cameco |
| `FAIL_CONTRACT` | 1 | Coupang |

There are no ready canaries in this queue. Resume a source only when genuinely
new authority addresses its exact blocker. In particular:

- Oracle-family mutable offsets still lack an exhaustive finite facet universe.
- Eightfold mutable PCSx traversal lacks an independently authoritative
  inventory/change contract.
- SuccessFactors mutation/repeated-page sources lack a stable exact alternative.
- Sitemap, RSS, and GraphQL leads lack active-only completeness, exact union, or
  stable public replay evidence.
- Workday alternatives lack exact first-party employer/site/scope binding.
- Static/browser stragglers lack reviewed source-local terminal evidence.

## Historical validation ledger

These results are retained as reproducibility evidence. They describe their
original repository states; they are not a substitute for tests on current
changes.

| Milestone | Recorded result |
|---|---|
| Phase 4 Workday closeout | 2,256 tests and 902 subtests passed; focused Workday 49 passed; zero failures |
| Phase 7 infrastructure audit | 2,509 tests and 911 subtests passed; combined requested slice 425 tests and 104 subtests; `git diff --check` passed |
| Batch 9 initial audit | 2,097 tests and 868 subtests passed; no live Batch 9 recovery |
| Batch 9 extended continuation | 2,130 tests and 868 subtests passed; no activation |
| Batch 9 XML implementation | 2,157 tests and 867 subtests passed; no live completion claim |
| Batch 10 closeout | 2,170 tests and 869 subtests passed; `git diff --check` passed |
| Latest remaining-source closeout | 2,557 tests and 909 subtests passed; focused recovery 36 tests/4 subtests; Phase 3–7 regressions 229; zero failures |

The repeatedly recorded warning was the existing LangChain deprecation warning
for `TavilySearchResults` in `src/agents/search_agents_adk.py`. Counts naturally
change as tests are added; preserve the dated ledger rather than rewriting old
results as current.

## Reproducibility and retained evidence

Historical Markdown reports were consolidated, but the evidence required to
reproduce decisions remains in:

- `config/phase7_infrastructure_audit.yaml` and
  `src/scraper/company_careers/phase7_audit.py`;
- `config/phase5_batch4_backlog.yaml`, Phase 5 candidate/canary/proposal files,
  and `src/scraper/company_careers/phase5_rollout.py`;
- `config/pagination_completeness_recovery_batch1.yaml` through `batch4.yaml`;
- `config/remaining_source_recovery_pass.yaml` and its isolated canary registry;
- Phase 1 candidate and rollback registries;
- source-specific candidate/validated YAML and CSV files;
- focused tests named for phases, adapters, recoveries, identity, lifecycle,
  retention, and integrity;
- `sql/supabase_schema.sql`, immutable migrations, and verification SQL;
- retained `jobs/` snapshots when present;
- protected `batch9_*_results.zip`, `config/job_radar_run_history.json`, and
  current `evidence/` files; and
- Git history for the original detailed prose.

The removed generated `company_uncovered_summary_v4` prose recorded an older
research reconciliation of 246 original companies: 118 covered canonically,
four covered through mappings, and 124 uncovered. It activated no source and
claimed no permanent ceiling. Its row-level evidence remains in
`config/company_uncovered_inventory_v4.csv`,
`config/company_uncovered_fingerprints_v4.csv`, and
`config/company_uncovered_clusters_v4.csv`; later production/recovery records
supersede its planning recommendations.

The uncommitted working copy at the former Batch 9 recovery-report path did not
contain the committed recovery report. Its exact bytes were preserved as
`evidence/preserved_uncommitted_company_careers_recovery_batch9.txt` before the
obsolete Markdown path was removed.

Do not delete these artifacts merely because the narrative is now concise.
Candidate, rollback, and historical configs are evidence, not production
activation.

## Remaining manual review

- The 36 parked sources require new source-specific evidence, not a bulk scan.
- Internal identifiers still contain `role-radar`, `job-radar`, `JobRadar`, and
  historical RoleRadar/Job Radar prose. Package names, modules, database tables,
  environment variables, workflow identifiers, user agents, repository URLs,
  artifact names, and evidence text are intentionally unchanged unless a
  separately planned compatibility migration is approved.
- The asset filenames remain `roleradar-*.svg` for path compatibility, while
  their visible identity is Ping.
- The workflow name `Hourly Job Radar` and `job-radar-hourly` concurrency group
  are stable workflow identifiers and were not renamed; the current schedule is
  every six hours.
