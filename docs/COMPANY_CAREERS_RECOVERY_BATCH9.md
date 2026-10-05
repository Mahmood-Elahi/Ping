# Job Radar Company Careers Recovery Batch 9

Date: 2026-10-04 (America/Edmonton)

## Outcome

Batch 9 investigated exactly the 20 requested sources against audit
`20261001_142550`, the effective registry after reliability overlays, saved
validation artifacts, adapter contracts, and historical audit evidence. No
source can be safely promoted without new live evidence. The batch therefore
adds a deterministic evidence regression suite and preserves every fail-closed
condition instead of manufacturing completion.

| Metric | Result |
|---|---:|
| Attempted | 20 |
| Live-confirmed complete | 0 |
| Deterministically repaired, pending live validation | 0 |
| Still unresolved | 20 |
| Config-only repairs | 0 |
| Existing-adapter reuse repairs | 0 |
| Shared-adapter repairs | 0 |
| New endpoint variants | 0 |

The first three status counts total 20. No live requests were made by Batch 9.

## Repository preflight

- Branch: `main`, tracking `origin/main`.
- Batch 8 is committed as `13dd10d` (`Recover four career sources and harden collection validation`).
- Nothing was staged before Batch 9.
- Pre-existing changes preserved:
  - `src/scraper/company_careers/adapters/browser_rendered_html.py` (line-ending-only working-tree state)
  - `tests/test_browser_collector.py` (line-ending-only working-tree state)
  - `tests/test_sap_successfactors.py` (substantive user change: 5 insertions, 1 deletion)
  - `config/job_radar_run_history.json` (untracked)
- Batch 9 did not edit any of those files.

## Audited baseline and effective configuration

`Fetched/retained` is the audit's normalized source count followed by the jobs
retained after Job Radar filtering. `Pages` is requested/completed. A dash in
the terminal column means that the source supplied no accepted terminal proof.

| Company | Effective source and variant | Health / scan / identity | Fetched/retained; total | Pages | Stop / terminal | Mutation; rejected | Exact blocker |
|---|---|---|---:|---:|---|---:|---|
| General Motors | `https://search-careers.gm.com/en/jobs/`; `server_rendered_cards` | terminal contradiction / incomplete / unproven | 20/0; — | 1/1 | `no_pagination_configured` / — | no; 0 | The page advertises continuation, but the old `?page=` template was proven to repeat page 1 and was correctly removed. No reviewed backend or terminal contract is cached. |
| Caterpillar | `https://careers.caterpillar.com/en/jobs/`; `server_rendered_cards` | terminal contradiction / incomplete / unproven | 20/0; — | 1/1 | `no_pagination_configured` / — | no; 0 | Same repeated-page failure as GM; the saved evidence has no total, result range, cursor, or disabled-next terminal. |
| Intuitive Surgical | `https://careers.intuitive.com/en/jobs/`; `server_rendered_cards` | terminal contradiction / incomplete / unproven | 20/0; — | 1/1 | `no_pagination_configured` / — | no; 0 | Same repeated-page failure; no trustworthy continuation or terminal contract is available. |
| Coupang | `https://www.coupang.jobs/en/jobs/`; `server_rendered_cards` | pagination loop / incomplete / unproven | 20/0; — | 2/2 | `repeated_page` / — | no; 0 | The configured `?page=` request returns the first page again while continuation remains present. |
| ASML | `https://www.asml.com/en/careers/find-your-job`; `browser_rendered_html_v1` | terminal contradiction / incomplete / unproven | 25/0; 25 | 1/1 | `browser_exhaustion_not_proven` / contradictory advertised total | no; 0 | The rendered snapshot reports continuation/page-count evidence despite 25 normalized rows; browser rendering alone does not prove enumeration. |
| Stantec | `https://stantec.jobs/jobs/?q=Calgary`; `browser_rendered_html_v1` | terminal contradiction / incomplete / unproven | 50/0; — | 1/1 | `browser_exhaustion_not_proven` / — | no; 0 | The rendered page exposes a next URL. The collector has no reviewed navigation URL contract for this board. |
| Dyson | `https://careers.dyson.com/en-gb/search-results/`; `server_rendered_cards` | pagination unproven / incomplete / unproven | 10/0; — | 1/1 | `no_pagination_configured` / — | no; 0 | Two older stable extraction runs prove record repeatability, not an atomic or terminal collection. |
| Athabasca Oil | `https://www.atha.com/careers-details/calgary-ab.html`; `server_rendered_cards` | pagination unproven / incomplete / unproven | 2/0; — | 1/1 | `no_pagination_configured` / — | no; 0 | Two older stable two-row runs do not establish that the page is the complete collection. |
| Bosch | `https://api.smartrecruiters.com/v1/companies/BoschGroup/postings`; `companies_postings_v1` | total not reached / incomplete / validated | 20/1; 4,814 | 1/1 | `maximum_results_reached` / — | no; 0 | The authoritative population exceeds the adapter's reviewed 1,000-job ceiling and each listing requires detail corroboration. |
| SGS | `https://api.smartrecruiters.com/v1/companies/SGS/postings`; `companies_postings_v1` | total not reached / incomplete / validated | 20/0; 4,604 | 1/1 | `maximum_results_reached` / — | no; 0 | Same bounded-detail constraint as Bosch; no reviewed server-side population filter exists. |
| CN Rail | `https://careers.cn.ca/services/recruiting/v1/jobs`; `rmk_columnized` | collection changed / incomplete / validated | 33/0; 33 | 4/4 | `collection_changed_during_scan` / total 33 reached | yes; 0 | Pagination reached the total but overlapping/changing stable IDs invalidated both bounded snapshots. The earlier recovery is not durable under the October 1 snapshot. |
| Scotiabank | `https://jobs.scotiabank.com/search/`; `rmk_table` | collection changed / incomplete / unproven | 926/4; 1,683 | 38/38 | `collection_changed_during_scan` / — | yes; 0 | The collection changed during traversal; the September 29 complete run cannot be substituted for the current snapshot. |
| AstraZeneca | derived Workday tenant `astrazeneca.wd3/Careers`; `wday_cxs_v1` | collection changed / incomplete / unproven | 600/3; 1,050 | 30/30 | `collection_changed_during_scan` / — | yes; 0 | The current Workday traversal changed before reaching its total. No supported snapshot or ordering token is known. |
| Capital One | `https://capitalone.wd12.myworkdayjobs.com/Capital_One`; `wday_cxs_v1` | collection changed / incomplete / unproven | 1,419/6; 1,883 | 71/71 | `collection_changed_during_scan` / — | yes; 0 | Same Workday mutation condition; previous complete audits are historical snapshots, not current terminal proof. |
| HP Inc. | `https://hp.eightfold.ai/api/pcsx/search`; `pcsx` | collection changed / incomplete / validated | 16/5; 911 | 3/3 | `collection_changed_during_scan` / — | yes; 0 | Overlap/change occurred far below the advertised total; the existing bounded restart was exhausted. |
| GlobalFoundries | `https://careers.gf.com/api/pcsx/search`; `pcsx` | collection changed / incomplete / validated | 20/3; 558 | 3/3 | `collection_changed_during_scan` / — | yes; 0 | Same Eightfold mutation condition; the September 23 complete snapshot does not prove the October 1 collection. |
| Atlassian | `https://www.atlassian.com/endpoint/careers/listings`; built-in atomic-list adapter | collection changed / incomplete / unproven | 331/9; 349 rows | 1/1 | `collection_changed_during_scan` / false atomic terminal | yes; 0 | The one response repeated 18 stable IDs. Both bounded attempts repeated the condition, so the 349-row response cannot be declared a clean 331-job atomic collection. |
| Meta | `https://www.metacareers.com/api/graphql/`; `graphql_search_terms_v1` | terminal contradiction / incomplete / unproven | 20/8; — | 1/1 | `maximum_results_reached` / `search_terms_incomplete=false` | no; 0 | The configured maximum was reached with `has_more=true`; globally raising the budget or treating the boundary as terminal is unsafe. |
| Rivian | `https://careers.rivian.com/api/jobs`; `icims_jibe_api` | collection changed / incomplete / unproven | 138/3; 795 | 15/15 | `collection_changed_during_scan` / — | yes; 3 | The Jibe collection changed and three records were rejected. Radancy HTML count semantics from Batch 8 do not apply to this JSON endpoint. |
| Google | `https://www.google.com/about/careers/applications/jobs/results/`; `search_terms_page_v1` | identity failure / failed / failed | 0/0; — | 4/3 | `identity_failure` / — | no; 0 | The third parsed response no longer matched the reviewed `ds:1` row schema. There is no cached fixture proving a replacement contract. |

## Company results

| Company | Root cause | Existing implementation reused | Batch 9 changes | Tests | Remaining evidence | Final status |
|---|---|---|---|---|---|---|
| General Motors | Removed pagination template had repeated page 1 | Static HTML, URL/ID integrity, pagination evidence | Evidence test only | Static + Batch 9 | Reviewed first-party structured backend or real next/terminal URL contract | Still unresolved |
| Caterpillar | Removed pagination template had repeated page 1 | Static HTML and completion helpers | Evidence test only | Static + Batch 9 | Same | Still unresolved |
| Intuitive Surgical | Removed pagination template had repeated page 1 | Static HTML and completion helpers | Evidence test only | Static + Batch 9 | Same | Still unresolved |
| Coupang | Current continuation repeats page 1 | Static HTML repeated-page detection | Evidence test only | Static + Batch 9 | Correct continuation contract plus terminal proof | Still unresolved |
| ASML | Rendered first snapshot contradicts continuation metadata | Browser collector over static parser | Evidence test only; protected dirty browser files | Browser + Batch 9 | Reviewed navigation/backend contract and identity on every page | Still unresolved |
| Stantec | Rendered page has unenumerated next URL | Browser collector over static parser | Evidence test only; protected dirty browser files | Browser + Batch 9 | Reviewed pagination URL/control and explicit terminal | Still unresolved |
| Dyson | Stable first page is not an atomic contract | Static HTML and identity regex | Evidence test only | Static + Batch 9 | Explicit no-next/total/atomic evidence | Still unresolved |
| Athabasca Oil | Stable two-row page is not an atomic contract | Static HTML and identity regex | Evidence test only | Static + Batch 9 | Explicit no-next/total/atomic evidence | Still unresolved |
| Bosch | 4,814 exceeds safe 1,000-job/detail budget | SmartRecruiters; Renesas/Arista/Avery/Western Digital references | Evidence test only | SmartRecruiters + Batch 9 | Official filtered population or bounded bulk contract | Still unresolved |
| SGS | 4,604 exceeds safe 1,000-job/detail budget | Same | Evidence test only | SmartRecruiters + Batch 9 | Same | Still unresolved |
| CN Rail | Both bounded columnized traversals changed | SAP adapter, mutation restart, totals, stable IDs | Evidence test only; protected dirty SAP test | SAP + Batch 9 | Two stable current traversals or supported snapshot token | Still unresolved |
| Scotiabank | Large RMK table changed during traversal | SAP adapter and mutation restart | Evidence test only | SAP + Batch 9 | Same | Still unresolved |
| AstraZeneca | Large Workday result changed during traversal | Workday totals, overlap detection, bounded restart | Evidence test only | Workday + Batch 9 | Supported stable ordering/snapshot and two consistent traversals | Still unresolved |
| Capital One | Large Workday result changed during traversal | Same | Evidence test only | Workday + Batch 9 | Same | Still unresolved |
| HP Inc. | Eightfold pages changed before total | Eightfold PCSx, totals, bounded restart | Evidence test only | Eightfold + Batch 9 | Stable supported endpoint/snapshot | Still unresolved |
| GlobalFoundries | Eightfold pages changed before total | Same | Evidence test only | Eightfold + Batch 9 | Same | Still unresolved |
| Atlassian | Atomic response repeats 18 stable IDs on both attempts | Atlassian adapter, immutable fingerprint, bounded restart | Evidence test only | Specialized + Batch 9 | Clean atomic response or documented duplicate semantics that preserve integrity | Still unresolved |
| Meta | Hard result boundary reached with continuation | Meta search terms, pagination evidence, adaptive budgets | Evidence test only | Specialized + Batch 9 | Official bounded collection or reviewed search partition with exhaustive terminals | Still unresolved |
| Rivian | Jibe JSON changed and rejected three rows | iCIMS Jibe, totals, restart, URL identity | Evidence test only | iCIMS + Batch 9 | Stable traversal and valid records; Batch 8 Radancy rules are inapplicable | Still unresolved |
| Google | Embedded row schema changed; identity failed | Existing Google parser and search-term pagination | Evidence test only | Specialized + Batch 9 | One bounded official response fixture establishing the current schema and canonical URLs | Still unresolved |

## Reuse report

- Existing infrastructure was investigated for all 20 targets.
- New shared logic: 0 targets.
- New source-specific logic: 0 targets.
- Current evidence insufficient for safe recovery: 20 targets.
- No code duplication or new endpoint variant was introduced.

Reuse by family:

- Static/browser (8): `static_html`, the browser wrapper, stable-ID and canonical-URL normalization, HTML pagination evidence, repeated-page protection, identity checks, and source finalization. Applied Materials, Palo Alto Networks, Rakuten, Goldman Sachs, and MediaTek served as existing compatibility references where their contracts were applicable.
- SmartRecruiters (2): the existing v1 adapter, exact company identity, authoritative totals, duplicate suppression, mutation restart, and detail corroboration. Renesas Electronics, Arista Networks, Avery Dennison, and Western Digital were reviewed as the requested working references.
- SAP (2): the RMK table/columnized variants, exact totals, stable IDs, overlap detection, mutation restart, and terminal validation. Existing completed SAP fixtures supplied compatibility coverage.
- Workday (2): the CXS adapter, total observations, stable IDs, bounded restart, natural terminal, and reported-total completion. Large completed Workday fixtures supplied regression coverage.
- Eightfold (2): the existing PCSx adapter, organization identity, totals, overlap detection, and bounded restart.
- Specialized (4): Atlassian atomic parsing and mutation fingerprints; Meta adaptive term pagination; iCIMS Jibe; and Google's existing embedded-data parser. Instagram, WhatsApp, and Google DeepMind parent mappings were explicitly retained.

## Shared improvements

No shared production adapter was changed. The new Batch 9 regression module
records the audited blockers and prevents later configuration-only work from
silently converting them into false completions. It covers:

- all 20 exact platform/health/stop-reason baselines;
- static single-page shortcuts and repeated-page continuation;
- browser identity versus exhaustion;
- SmartRecruiters authoritative totals above the reviewed hard ceiling;
- SAP, Workday, Eightfold, and iCIMS mutation fail-closed behavior;
- Atlassian duplicate atomic rows, Meta's hard boundary, Rivian rejected rows,
  Google's schema/identity failure, and the three requested parent mappings.

## Separate live-validation requests

No request below was executed. These are the smallest useful follow-ups for a
separately authorized live investigation.

- GM, Caterpillar, Intuitive Surgical, Coupang: one GET of each configured listing plus at most two reviewed continuation/backend probes per company (maximum 3 requests each) to identify a real continuation and terminal contract.
- Dyson and Athabasca Oil: one listing GET plus at most one terminal/atomic-contract probe each (maximum 2 requests each).
- ASML and Stantec: one rendered listing navigation plus one reviewed continuation/backend probe each (maximum 2 navigations each); do not edit the currently dirty browser files without isolating the user's work.
- Bosch and SGS: one schema request to the configured SmartRecruiters endpoint and one reviewed filtered-query probe each (maximum 2 requests each). Do not enumerate 4,000+ details unless a new budget is separately approved.
- CN Rail: two isolated four-page traversals (maximum 8 listing requests) to test snapshot stability.
- Scotiabank: two isolated full traversals would require about 136 listing requests; this is intentionally not authorized by Batch 9.
- AstraZeneca and Capital One: two isolated full traversals would require about 106 and 190 listing requests respectively; neither is authorized here.
- HP Inc. and GlobalFoundries: two isolated full traversals would require about 184 and 112 listing requests respectively; neither is authorized here.
- Atlassian: two atomic GETs to `https://www.atlassian.com/endpoint/careers/listings` (2 requests) to determine whether the 18 duplicate IDs persist.
- Meta: one search-context GET and at most 12 GraphQL listing requests, stopping on the first unreviewed/contradictory condition.
- Rivian: two full traversals would require about 160 listing requests and is not authorized here; a two-request schema/count check can only diagnose, not recover.
- Google: at most two GETs to the exact official results route for one query/page to capture and review the current embedded schema; this must not be treated as complete enumeration.

## Modified files

Production adapter changes: none.

Configuration changes: none.

Test changes:

- `tests/test_batch9_recovery.py` (new)

New documentation:

- `docs/COMPANY_CAREERS_RECOVERY_BATCH9.md` (this report)

Unrelated pre-existing modifications remain separate and unchanged, as listed
in the preflight section.

## Deterministic test results

| Scope | Result |
|---|---|
| Focused Batch 9 | 7 passed, 37 subtests passed, 1 warning |
| Static HTML | 57 passed, 6 subtests passed, 1 warning |
| Browser | 22 passed, 14 subtests passed, 1 warning |
| SmartRecruiters focus | 2 passed, 8 deselected, 3 subtests passed, 1 warning |
| SAP SuccessFactors | 62 passed, 22 subtests passed, 1 warning |
| Workday focus | 17 passed; plus 4 passed/6 deselected with 7 subtests; 1 warning per invocation |
| Eightfold | 38 passed, 16 deselected, 22 subtests passed, 1 warning |
| iCIMS | 28 passed, 2 subtests passed, 1 warning |
| Specialized Google/Atlassian/Meta/Workday selection | 19 passed, 57 deselected, 1 warning |
| Specialized reliability + parent mapping | 54 passed, 19 subtests passed, 1 warning |
| Source integrity and pagination | 79 passed, 56 subtests passed, 1 warning |
| Full `pytest -q` | 2,097 passed, 868 subtests passed, 1 warning in 170.54s |
| Whitespace (`git diff --check`) | Passed; only the three preserved CRLF warnings on pre-existing files |

All warnings above are the same existing `LangChainDeprecationWarning` for
`TavilySearchResults` in `src/agents/search_agents_adk.py:131`.

## Projected coverage

No additional recovery has sufficient evidence. The projection therefore
remains the post-Batch-8 baseline:

| State | Projected count |
|---|---:|
| Complete | 476 |
| Incomplete | 45 |
| Failed | 12 |

Deterministic-only candidates: 0. Live-confirmed Batch 9 improvements: 0.

## Repository safety

- No full Job Radar scan.
- No dashboard audit.
- No production write.
- No Supabase modification.
- No Google Sheets publication.
- No SQLite production-state or lifecycle mutation.
- No GitHub Actions trigger.
- No unauthorized live enumeration.
- `config/job_radar_run_history.json` untouched.
- Existing unrelated changes preserved.
- Nothing staged by Batch 9.
- Nothing committed, pushed, or merged.

Final repository commands and their exact output are recorded in the handoff
message after the final test and whitespace checks.

## October 4 extended-evidence continuation

The later `batch9_extended_results.zip` was inspected offline. It does not
justify a production activation: the three XML-looking responses crossed the
old 2 MB cap before retaining type/length/prefix evidence; Bosch Germany is a
partial 600/720 partition against a stable 4,835 global total; and SGS United
States is a terminal 346/346 partition against a stable 4,619 global total.
The SGS result is successful partition evidence, not worldwide completion.

The diagnostic utility now provides metadata-first XML classification, an
XML-confirmed company-scoped response override capped at 8 MB, immutable
SmartRecruiters partition resume, row-country checks, and offline multi-artifact
reconciliation. Reconciliation fails closed unless every partition is terminal
and row-validated, snapshots and global totals agree, IDs do not overlap, and
the exact unique-ID union reaches the stable global authoritative total.
Observed anchor country codes remain explicitly non-exhaustive.

Post-continuation verification: the focused Batch 9 suite passed 40 tests and
37 subtests; the full repository suite passed 2,130 tests and 868 subtests in
160.40 seconds. Both reported only the existing `TavilySearchResults`
deprecation warning.

No live probe, scan, dashboard audit, publication, production-state edit, source
activation, staging, commit, push, merge, or GitHub Actions run was performed.
The detailed operator workflow, activation criteria, stopping rules, and bounded
PowerShell commands are in
`docs/COMPANY_CAREERS_RECOVERY_BATCH9_SOURCE_PROBES.md`.

## October 4 XML recovery implementation

The production `source_xml` path now supports a strict same-run completeness
contract for General Motors, Caterpillar, and Coupang. It reads the official
unfiltered listing count immediately before and after one complete XML response
and completes only when both listing observations have exact company identity,
the same total, and that total exactly equals the fully validated XML inventory.
Filtered or redirected listings, changed or conflicting totals, continuation
metadata, rejected records, duplicate posting identities, duplicate canonical
URLs, and response truncation all remain fail-closed.

Coupang now derives its stable posting identity from the official canonical URL
path and verifies the matching `gh_jid` when present. The shared requisition ID
is retained as `requisition_id` metadata and is not modified or used to collapse
distinct posting URLs. General Motors and Caterpillar continue to use their
unique requisition IDs.

The prior guessed `totalResults` configuration was removed because the captured
production root schema did not establish it as present or authoritative. Exact
non-job root metadata is recorded for review, and a configured root count (for
example, `expectedJobCount`) is accepted only when that exact field is explicitly
configured and well-formed.

Offline verification after this implementation: the focused XML suite passed
27 tests; the full repository suite passed 2,157 tests and 867 subtests with the
single pre-existing `TavilySearchResults` deprecation warning. `git diff --check`
passed with only the three pre-existing CRLF conversion warnings.

No current live completion claim is made by this implementation-only update.
One separately authorized, read-only validation is still required to establish
that each current public listing exposes the configured exact title/count
contract and agrees with its same-run XML collection.
