# Pagination / Completeness Recovery - Batch 1

Date: 2026-10-11 (America/Edmonton)

## Outcome and live closeout

The operator manually canaried the three Batch 1 candidates after the original
deterministic review. Shopify and Intuitive Surgical returned `PASS_COMPLETE`
and are recorded as `LIVE_VALIDATED` / `RECOVERED`. Intuitive Surgical's exact
tested SmartRecruiters contract is now the production row. Atlassian returned
`LIVE_CANARY_FAILED` and is `NEEDS_SOURCE_SPECIFIC_REPAIR` / `NOT_RECOVERED`;
no further Atlassian diagnosis or canary is part of Batch 2.

The 20 dispositions are:

| Outcome | Count | Companies |
|---|---:|---|
| A. Live validated and recovered | 2 | Shopify; Intuitive Surgical |
| B. Needs more deterministic repair | 11 | Atlassian; Meta; JPMorgan Chase; Emerson; Honeywell; Microsoft; Qualcomm; Micron; Eaton; Rivian; Goldman Sachs |
| C. Blocked / no safe repair | 1 | Coupang |
| D. Not actually pagination - reclassified | 6 | Samsung Electronics; GE Vernova; General Motors; Caterpillar; Zalando; Capital One |

Machine-readable source maps, budgets, status counts, pass gates, commands,
and rollback data are in
`config/pagination_completeness_recovery_batch1.yaml`.

## Evidence boundary

The review began from actual repository HEAD
`69bf3382a47781ae025c61a078fd6b5cbc0a1b70` (`Complete Phase 7
infrastructure audit`). The prompt's `ef021b4` is an ancestor; no checkout or
branch change was made. The production registry before Intuitive activation had
SHA-256 `b5e8e42e1047c03131480ea678e2ca42ba3dbf42bd3f6ca1d3e0290decfbff30`.
The newest full runtime evidence remains the checked-in 2026-10-01 snapshot,
SHA-256
`fb12864e7d1b84931d56c8a831a51572377f06e9eb6d8d8970b65b76fd5e7a7b`.
It is historical evidence, not a claim about live health on 2026-10-11.

The live validation evidence in this closeout was supplied by the operator;
this Batch 2 implementation did not run a scan, canary, company/provider
request, browser/proxy session, production mutation, migration, GitHub Action,
commit, push, merge, checkout, or branch change.

Live closeout evidence:

- Shopify: 115/115 jobs, one request, atomic `jobPostingsWithJobs` terminal,
  unique IDs and URLs, no mutation, budget 1/1, no lifecycle mutations. The
  existing production source was unchanged.
- Intuitive Surgical: 747/747 jobs, eight pages, authoritative `totalFound`,
  budget 8/8, unique IDs and URLs, no mutation, no lifecycle mutations. The
  exact Phase 3 candidate was activated in both the base production registry
  and its effective reliability overlay.
- Atlassian: identity validated and 352 reported rows arrived in one bounded
  response, but the canary failed with `AdapterError: Atlassian Careers job URL
  did not match the observed iCIMS host and stable-ID path contract`. Inventory
  was incomplete and central finalization was not authoritative.

## Source map and narrow diagnosis

All sources use `source_scope: default`. Unspecified registry limits below are
the model defaults: `max_pages=5`, `max_jobs=20`, and
`max_response_bytes=3,000,000`.

| Company | Platform / variant | Configured source and identifiers | Limits | Newest deterministic failure evidence | Sibling evidence |
|---|---|---|---|---|---|
| Atlassian | `atlassian` / atomic JSON | Fixed `/endpoint/careers/listings`; exact Atlassian target | defaults; one actual request | 349 raw rows, 331 unique jobs, 18 identical repeated representations, one atomic response | jobseek has the same endpoint and `portalJobPost.portalUrl` |
| Shopify | `shopify` / React Router atomic collection | `www.shopify.com/careers`; route `($locale)/careers`; `jobPostingsWithJobs` | defaults; 3 MB | Snapshot: 115/115, one page, terminal proof absent; current code post-dates snapshot | jobseek sitemap plus rendered detail, weaker than current atomic collection |
| Meta | `meta` / term search | fixed GraphQL search; three configured search terms | 3 pages, 20 jobs, 3 MB | 20 jobs; `has_more=true`; safety limit; terms do not enumerate the employer | jobseek/freehire know the first-party jobsearch sitemap |
| JPMorgan Chase | Oracle Recruiting | `jpmc.../recruitingCEJobRequisitions`; site `CX_1001`, locale `en` | 2 pages, 20 jobs, 3 MB | 2,448/7,393 over 102 pages; collection changed | jobseek Oracle partition code; same board |
| Emerson | Oracle Recruiting | `hdjq.../recruitingCEJobRequisitions`; site `CX_1`, locale `en` | 2 pages, 20 jobs, 3 MB | 983/1,058 over 41 pages; collection changed | jobseek same Oracle family; separate AspenTech board is not equivalent |
| Honeywell | Oracle Recruiting | `ibqbjb.../recruitingCEJobRequisitions`; `CX_1`; careers path `Honeywell` | 2 pages, 20 jobs, 3 MB | 72/1,265 over 3 pages; collection changed | jobseek same board plus distinct Aerospace boards |
| Microsoft | Eightfold PCSx | `apply.careers.microsoft.com/api/pcsx/search`; org `microsoft.com` | 2 pages, 20 jobs, 3 MB | 21/2,369 over 3 pages; overlap/mutation | jobseek watermark implementation |
| Qualcomm | Eightfold PCSx | `careers.qualcomm.com/api/pcsx/search`; org `qualcomm.com` | 2 pages, 20 jobs, 3 MB | 20/2,029 over 2 pages; overlap/mutation | jobseek PCSx plus first-party sitemap lead |
| Micron | Eightfold PCSx | `careers.micron.com/api/pcsx/search`; org `micron.com` | 2 pages, 20 jobs, 3 MB | 20/2,975 over 2 pages; overlap/mutation | jobseek uses a Micron Workday board; migration evidence only |
| Eaton | Eightfold PCSx | `eaton.eightfold.ai/api/pcsx/search`; org `eaton.com` | 2 pages, 20 jobs, 3 MB | 20/2,365 over 2 pages; overlap/mutation | jobseek PCSx with proxy/watermark behavior |
| Samsung Electronics | Workday | `sec.wd3.../Samsung_Careers`; tenant `sec`, site `Samsung_Careers` | 1 page, 20 jobs; Workday budget 500 | 663/664 over 34 pages; exact `workday_identity` absent | same jobseek board plus distinct Samsung boards |
| GE Vernova | Workday | `gevernova.wd5.../Vernova_ExternalSite` | 1 page, 20 jobs; Workday budget 500 | 780/2,166 over 39 pages; exact identity absent; total exceeds direct 2,000 cap | same jobseek board plus distinct FieldCore/Menk/Prolec sources |
| Rivian | iCIMS Jibe API | `careers.rivian.com/api/jobs`; tenant `rivian`, board `rivian-public` | 10/page, 2 pages, 20 jobs, 3 MB | 138/795 over 15 pages; mutation and 3 rejected rows | jobseek first-party sitemap; freehire fail-whole sitemap index |
| General Motors | static HTML cards | `search-careers.gm.com/en/jobs/` | 1 page, 20 jobs, 3 MB; no reviewed next contract | 20 jobs; active next link; identity unproven | jobseek Workday `generalmotors/careers_gm`; subsidiary scope unresolved |
| Caterpillar | static HTML cards | `careers.caterpillar.com/en/jobs/` | 1 page, 20 jobs, 3 MB; no reviewed next contract | 20 jobs; active next link; identity unproven | jobseek Workday `cat/CaterpillarCareers`; Progress Rail is distinct |
| Intuitive Surgical | SmartRecruiters | slug `intuitive`; identity `^Intuitive(?: Surgical)?$` | 100/page, 8 pages, 800 jobs, 3 MB | Live `PASS_COMPLETE`: 747/747 over 8 pages; exact candidate activated | jobseek independently records the same SmartRecruiters slug |
| Coupang | static HTML cards | `www.coupang.jobs/en/jobs/`; `?page={page}` | 1 page, 20 jobs, 3 MB | 20 jobs; second request repeated page 1; pagination loop | jobseek global Greenhouse plus four non-equivalent Korean sources |
| Zalando | static HTML links | `jobs.zalando.com/en/jobs` | 1 page, 20 jobs, 3 MB; no reviewed next contract | 15 jobs; no authoritative terminal proof; identity unproven | jobseek Workday `zalando/ZalandoSiteWD`; identity still unproven |
| Capital One | Workday | `capitalone.wd12.../Capital_One` | 2 pages, 20 jobs; Workday budget 500 | 1,419/1,883 over 71 pages; exact `workday_identity` absent | sibling Workday mechanics add no employer-binding evidence |
| Goldman Sachs | browser-rendered HTML | `higher.gs.com/results`; role path `/roles/{roleId}` | 1 page, 20 jobs, 3 MB | 20 jobs; continuation present; browser exhaustion unproven | jobseek first-party GraphQL `GetRoles` lead |

### Adapter and failure grouping

- Atomic collections: Atlassian and Shopify. Their entire provider collection
  is returned in one response; in-array representation/accounting, not
  pagination, is decisive.
- Mutable offset pagination: Oracle (JPMorgan Chase, Emerson, Honeywell),
  Eightfold (Microsoft, Qualcomm, Micron, Eaton), and iCIMS/Jibe (Rivian).
  Overlap or total movement remains incomplete after one bounded restart.
- Workday identity before traversal: Samsung Electronics, GE Vernova, and
  Capital One. Phase 4 already supplied safe cap partitioning, but deliberately
  refuses absence authority without exact employer/tenant/site evidence.
- Static source contract: General Motors, Caterpillar, Coupang, and Zalando. A
  generic next link or short page is not an EOF.
- Source-specific inventory: Meta's search-term GraphQL and Goldman Sachs's
  browser results cannot represent complete employer inventory as configured.

The original deterministic causes were: one atomic duplicate-accounting defect
(Atlassian); one post-snapshot repair awaiting validation (Shopify); one ready
alternate candidate (Intuitive Surgical); eight observable mutation cases;
six identity failures; one ignored page parameter (Coupang); one incomplete
term inventory (Meta); and one browser result with no terminal contract
(Goldman Sachs).

## Repairs and prior Job Radar patterns reused

### Implemented

Atlassian now treats identical occurrences of the same stable ID in its one
atomic array as known duplicate representations rather than cross-page
mutation. It records:

```
raw_array_rows = unique_inventory_items + duplicate_representations
349            = 331                    + 18
```

The adapter makes one request, validates the exact Atlassian target, publishes
`complete_collection_response` with value `atomic_json_collection`, returns
explicit `InventoryItem` objects for every unique ID, and records
`terminal_accounted_items` for every raw row. A repeated ID with a conflicting
identity fingerprint still raises an adapter error. This is a company-specific
repair affecting one batch company, not a shared-family recovery.

The test gap was semantic: prior tests modeled an identical duplicate inside
one atomic response as if it were a duplicate observed across a paginated,
mutable traversal. Atomic-array duplication is representation accounting;
cross-page duplication remains mutation evidence in the paginated adapters.

No shared adapter repair was implemented. That is intentional. The tempting
shared changes all weaken authority:

| Family | Batch companies | Rejected shared change | Why unsafe |
|---|---|---|---|
| Oracle | JPMorgan Chase; Emerson; Honeywell | Accept overlap or a one-row total/shortfall tolerance | Hides movement in an offset-paginated collection |
| Eightfold | Microsoft; Qualcomm; Micron; Eaton | Treat watermarks, repeated pages, or overlap as full completion | Incremental discovery is positive-only |
| Workday | Samsung; GE Vernova; Capital One | Run partitioning without exact identity | Pagination cannot establish employer binding |
| Static HTML | GM; Caterpillar; Intuitive; Coupang; Zalando | Auto-follow generic next or infer EOF from short/empty HTML | Continuation and terminal semantics are source-specific |

### Existing repairs retained

- Workday Phase 4: 2,000-result cap, deterministic facet subdivision,
  root/partition accounting, request budget, one restart, and mutation as
  incomplete. It correctly exposes identity, rather than pagination, as the
  blocker for the three production Workday rows.
- Shopify: exact canonical route, named atomic collection, route identity,
  stable UUIDs, duplicate ID/URL rejection, link validation, and explicit
  complete-collection evidence. This implementation post-dates the runtime
  snapshot.
- SmartRecruiters Phase 3/4: `totalFound` accounting, offset pagination,
  listing/detail separation, and the existing listing-only Intuitive candidate.
- Static HTML: only reviewed pagination templates/selectors, explicit HTML
  terminal evidence, loop detection, and bounded response/page/job limits.
- Phase 3: explicit `AdapterResult`, inventory items, protected item failures,
  source health, partial-positive preservation, and central finalization.
- Phase 5/6: source-scope isolation, exact-only equivalence, aggregate
  lifecycle ownership, and positive-only partial discovery.
- Apple, Cameco, Chemelex, Etsy, and Lightspeed France supplied the governing
  precedents: mutation is incomplete; identity ambiguity blocks activation;
  malformed inventory must be accounted; weak binding is not recovery; and
  unsafe candidates are rejected rather than made green.

## Sibling-repository mining

### jobseek

| File / function | Classification | Useful prior art | Safeguard retained or added in Job Radar |
|---|---|---|---|
| `apps/crawler/src/core/monitors/workday.py`: `_paginate_query`, `_assert_complete_inventory`, `_direct_pagination_stream`, `_paginate_facet_union`, `_api_list_stream` | `REUSE_CONTRACT` concept; already independently implemented | Direct enumeration plus facet union around the 2,000 cap | Exact identity, root/partition equations, mutation restart, and request budgets remain mandatory |
| `.../oracle_hcm.py`: `discover_stream`, `_complete_facet_partition` | `ADAPT` mechanics | Offset belongs inside Oracle's finder; facet partitioning is possible | Do not import total/page tolerances or overlap-as-success |
| `.../eightfold.py`: `discover_stream` and board watermark configuration | `CONCEPT_ONLY` | PCSx shape and stable position IDs | Watermarks cannot authorize absence; total drift/overlap remain incomplete |
| `.../sitemap.py`: `_resolve_sitemap_index`, `_discover_sitemap` | `ADAPT` | Bounded sitemap index traversal for Meta/Rivian alternatives | Any cap or failed child sitemap must make inventory incomplete; hydrate IDs separately |
| `apps/crawler/data/boards.csv` | `ADAPT` discovery evidence | Exact ATS leads listed in the source map, including Goldman GraphQL and three Workday alternatives | Board catalog is not current first-party authority and cannot activate migrations |

The Goldman lead is
`https://api-higher.gs.com/gateway/api/v1/graphql`, operation `GetRoles`,
with `pageSize`, `pageNumber`, `totalCount`, EARLY_CAREER/PROFESSIONAL
experiences, stable `roleId`, and official `/roles/{roleId}` URLs. It still
needs Job Radar request/response fixtures, exact experience-union accounting,
pagination/mutation rules, and terminal proof.

### freehire

| File / function | Classification | Useful prior art | Unsafe behavior rejected / Job Radar addition required |
|---|---|---|---|
| `internal/ingest/sources/workday.go`: `listPostings`, `pageAll`, `splitByFacet` | `CONCEPT_ONLY`; Job Radar is stricter | 2,000-cap recognition and recursive facets | Depth-only union is insufficient without exact identity and complete partition equations |
| `.../oracle.go`: `listRequisitions` | `REUSE_CONTRACT` for request mechanics | Confirms offset must be inside the Oracle finder | Empty response and retry behavior cannot become clean completion; mutation must fail closed |
| `.../eightfold.go`: `listPositions`, `pageList`, `pcsxPage`, `v2Page` | `ADAPT` response mechanics | PCSx/v2 page shapes | List failures, total drift, duplicate pages, and overlaps need explicit incomplete outcomes |
| `.../icims.go`: `jobLocs` | `REUSE_CONTRACT` | Fails the whole sitemap inventory when any child sitemap fails | Preserve stable inventory IDs and separate later detail/normalization failures |
| `.../metacareers.go`: `Fetch`, `detail` | `ADAPT` inventory/detail split | Meta flat sitemap and JSON-LD details | Its failed-detail drop and clean empty semantics are rejected; failed stable IDs must be protected |
| `.../jibe.go`: `Fetch` | `CONCEPT_ONLY` | Numeric Jibe pages and total count | Mutation, rejected rows, and arbitrary empty pages still fail closed |

### JobSpy

`jobspy/__init__.py:scrape_jobs` dispatches third-party LinkedIn, Indeed,
Glassdoor, Google, ZipRecruiter and related scrapers. It contains no exact
Atlassian, Shopify, Oracle, Eightfold, Workday, iCIMS/Jibe, SmartRecruiters,
Meta-careers, or Goldman first-party inventory contract. Classification:
`REJECT` as absence authority for every batch company. Its search pagination,
proxy/fingerprint behavior, title/location matching, and aggregator exhaustion
cannot prove employer identity or first-party completeness.

### Code-touched adapter comparison: Atlassian

| Repository | Pagination | Identity | Completeness | Decision |
|---|---|---|---|---|
| Job Radar | One atomic JSON response | Exact target plus fixed first-party endpoint; stable job IDs | Every raw array row accounted; conflicting duplicate fails | Implemented |
| jobseek | API-sniffer atomic endpoint; portal URL extraction | Board catalog binding | Useful endpoint/field confirmation, but no Job Radar authority boundary | `ADAPT` |
| freehire | No exact Atlassian source implementation found | N/A | N/A | `CONCEPT_ONLY`/none |
| JobSpy | Third-party search sources | Search result attribution | Aggregator exhaustion only | `REJECT` |

## Alternate first-party inventories

| Company | Alternate | Assessment |
|---|---|---|
| Meta | `metacareers.com/jobsearch/sitemap.xml` plus JSON-LD detail | Promising first-party inventory; needs fail-whole sitemap traversal, protected IDs, hydration accounting, and a derived request budget |
| Qualcomm | official careers sitemap | Discovery lead only; terminal/freshness and exact job URL fixtures absent |
| Micron | `micron.wd1.../external` Workday | Sibling-only migration lead; no activation |
| Rivian | `careers.rivian.com/sitemap.xml` | Promising first-party inventory; needs exact sitemap completeness and item accounting |
| GM | Workday `generalmotors/careers_gm` | Identity/scope unproven; subsidiaries are separate |
| Caterpillar | Workday `cat/CaterpillarCareers` | Identity/scope unproven; Progress Rail is separate |
| Intuitive Surgical | SmartRecruiters `intuitive` | Live `PASS_COMPLETE`; exact tested contract activated in production |
| Coupang | Greenhouse `coupang` | Global/apply URL and operational-board equivalence unproven; current static page contract fails |
| Zalando | Workday `zalando/ZalandoSiteWD` | Exact Workday identity evidence absent |
| Goldman Sachs | first-party GraphQL `GetRoles` | Best repair path; still needs a reviewed adapter and fixtures |

Shopify's sibling sitemap is not preferred because Job Radar already has a
stronger named atomic collection. Separate boards for Honeywell, Samsung, GE
Vernova, GM, Caterpillar, and Coupang are not silently unioned with the parent
source.

## Request-budget audit

No production budget changed. A total-derived request count is a lower bound
for a stable traversal, not permission to call a moving collection complete.

| Source(s) | Current normal / hard | Deterministic stable lower bound | Decision |
|---|---:|---:|---|
| Atlassian; Shopify | 1 actual request | 1 | Canary budget 1 each |
| Intuitive candidate | 8 / 8 | 7 for reviewed 700, plus 1 bounded margin | Canary budget 8 |
| Meta | 9 normal (3 terms x 3 pages) | Unknown for full employer inventory | No increase |
| JPMorgan Chase | 2 / 1,000 per traversal | 309; 618 worst case with one full restart | No canary; mutation unresolved |
| Emerson | 2 / 1,000 | 45; 90 with restart | No canary; mutation unresolved |
| Honeywell | 2 / 1,000 | 53; 106 with restart | No canary; mutation unresolved |
| Microsoft | 2 / 1,000 | 237; 474 with restart | No canary; mutation unresolved |
| Qualcomm | 2 / 1,000 | 203; 406 with restart | No canary; mutation unresolved |
| Micron | 2 / 1,000 | 298; 596 with restart | No canary; mutation unresolved |
| Eaton | 2 / 1,000 | 237; 474 with restart | No canary; mutation unresolved |
| Rivian | 2 / 1,000 | 80; 160 with restart | No canary; mutation plus malformed rows |
| Samsung | Workday 500 | 34 direct pages | Identity blocks first |
| GE Vernova | Workday 500 | 109 direct pages plus unknown partition overhead | Identity blocks first; 2,000 cap requires partitions |
| Capital One | Workday 500 | 95 direct pages | Identity blocks first |
| GM; Caterpillar; Zalando | 1 | Unknown | No reviewed continuation/terminal contract |
| Coupang | 1 | None | More repeated first pages never prove EOF |
| Goldman Sachs | 1 rendered page | Unknown | GraphQL contract not reviewed |

The original budget review did not repair a source by itself. Intuitive's
tested bound was subsequently live-validated and activated; Shopify's atomic
production source live-validated without a configuration change. Atlassian's
atomic canary failed its source-specific URL/stable-ID contract.

## Final company statuses

| Company | Final status | Reason |
|---|---|---|
| Atlassian | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Live canary failed the observed iCIMS URL/stable-ID path contract; backlog, not recovered |
| Shopify | `RECOVERED` | Live `PASS_COMPLETE`; existing atomic production source unchanged |
| Meta | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Search terms cannot exhaust employer inventory; sitemap path needs safe hydration/accounting |
| JPMorgan Chase | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Oracle offset collection mutated |
| Emerson | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Oracle offset collection mutated |
| Honeywell | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Oracle offset collection mutated |
| Microsoft | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Eightfold overlap/mutation at very large total |
| Qualcomm | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Eightfold overlap/mutation; sitemap only a lead |
| Micron | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Eightfold overlap/mutation; Workday only a migration lead |
| Eaton | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Eightfold overlap/mutation |
| Samsung Electronics | `FAIL_IDENTITY` | Exact Workday employer/tenant/site evidence missing |
| GE Vernova | `FAIL_IDENTITY` | Exact Workday identity missing before cap partitioning can matter |
| Rivian | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Mutation and three malformed records are distinct blockers |
| General Motors | `FAIL_IDENTITY` | Current static and alternate Workday source lack exact scope proof |
| Caterpillar | `FAIL_IDENTITY` | Current static and alternate Workday source lack exact scope proof |
| Intuitive Surgical | `RECOVERED` | Live `PASS_COMPLETE`; exact Phase 3 SmartRecruiters contract activated in production |
| Coupang | `FAIL_CONTRACT` | Page parameter repeats page one; alternate scope/apply contract unproven |
| Zalando | `FAIL_IDENTITY` | Current static and alternate Workday identity are unproven |
| Capital One | `FAIL_IDENTITY` | Exact Workday employer/tenant/site evidence missing |
| Goldman Sachs | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Browser discovery cannot prove exhaustion; GraphQL needs an adapter |

`RECOVERED_BY_SHARED_FIX`: none. Shopify and Intuitive Surgical are production
recoveries because their manual canaries returned `PASS_COMPLETE`.

## Historical manual canaries - completed by operator

Run Atlassian and Shopify together, with one independent request permitted per
source:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --company Atlassian --company Shopify --request-budget 1 --run
```

Run Intuitive Surgical against the explicit non-production candidate registry:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --registry config\target_companies.phase3_smartrecruiters_canaries.yaml --company "Intuitive Surgical" --request-budget 8 --run
```

Both commands used the existing read-only canary harness: persistence was
`none`, lifecycle mutations were zero, and no dashboard was published. Do not
rerun Atlassian as part of Batch 2.

### `PASS_COMPLETE` gates

Every source must show: canonical employer and source identity validated;
complete inventory; valid structured terminal evidence; no hidden
continuation; no collection mutation; budget respected; no safety limit;
every inventory item accounted; unreadable stable IDs protected; required
stable-ID and canonical-URL uniqueness; valid apply URLs; correct company and
`source_scope` attribution; authoritative central finalization; and no
contradictory diagnostics. `PASS_PARTIAL`, any `FAIL_*`, mutation, unaccounted
rows, or ambiguous terminal evidence does not activate recovery.

## Rollback

- Atlassian: revert only the atomic duplicate-accounting adapter/test change.
  This restores the earlier fail-closed behavior; it does not delete lifecycle
  rows.
- Shopify: Batch 1 made no code/config change.
- Intuitive Surgical: restore only its prior static rows in
  `config/target_companies.yaml` and
  `config/company_careers_reliability_repairs_v1.yaml`. The Phase 3 candidate
  remains historical evidence and is not the production dispatch path.
- No production source migration or lifecycle deletion is part of rollback.

## Deterministic verification

All verification was local/offline.

| Matrix | Result |
|---|---|
| Focused Batch 1 + Batch 2 + production-contract regressions | 63 passed, 35 subtests passed |
| Source integrity, terminal evidence, inventory/hydration, AdapterResult, Phase 3-7, multi-scope lifecycle, incremental authority, retention | 375 passed, 60 subtests passed |
| Full pytest suite | 2,532 passed, 909 subtests passed in 188.83 seconds |
| `git diff --check` | Passed |

Pytest emitted one pre-existing LangChain deprecation warning from
`src/agents/search_agents_adk.py`; there were no test failures.

## Batch 1 repair playbook

1. Classify the response shape before interpreting duplicates. A duplicate in
   one fully read atomic array can be an accounted representation; the same ID
   across offset pages is mutation unless the provider contract proves
   otherwise.
2. A reported total derives a request lower bound, but total movement or page
   overlap invalidates the snapshot. Never spend through mutation.
3. For Workday, identity precedes the 2,000-cap partition algorithm. A correct
   partitioner cannot make an unbound tenant/site authoritative.
4. For Oracle, offset belongs inside the finder clause. Tolerances useful to a
   best-effort crawler are not acceptable absence semantics.
5. Eightfold watermarks are positive-only. Full-crawl absence authority still
   needs stable total, non-overlapping pages, and exact reconciliation.
6. Treat sitemap URLs as inventory identities before detail hydration. A
   failed detail protects the ID; a failed child sitemap invalidates the
   inventory.
7. Static HTML needs a reviewed continuation and terminal contract. Generic
   next controls, short pages, and empty pages are hints, not authority.
8. Repeated page parameters are a contract failure, not an invitation to raise
   `max_pages`.
9. Browser rendering can reveal jobs but cannot itself prove source
   exhaustion. Prefer the first-party API used by the frontend when its exact
   request, identity, total, and terminal contract can be fixture-tested.
10. Keep inventory, hydration, normalization, filtering, retention, and
    source-scope-local absence separate in code and evidence.

## Recommended Batch 2 composition

Do not mix identity research with pagination code. Use two tracks:

1. One mutation-heavy family: Oracle Recruiting first (three companies), then
   Eightfold PCSx (four). Build deterministic moving-window/partition fixtures
   and look for provider-supported snapshot or complete facet contracts. If no
   stable contract exists, retain positive discovery and keep absence blocked.
2. One identity-evidence family: the three production Workday rows plus the
   GM/Caterpillar/Zalando alternates. This is an evidence-binding batch, not a
   pagination batch; do not change traversal until exact employer/site/scope
   identity is established.

Meta, Rivian, and Goldman Sachs form a later first-party-inventory batch
(sitemap/sitemap/GraphQL) because they share inventory-vs-hydration work but
not a transport or terminal contract.

---

# Batch 2 extension

Batch 2 was performed from HEAD `51b443b` using repository state, checked-in
fixtures/history, and read-only inspection of jobseek, freehire, and JobSpy.
No live scan, canary, provider/company request, browser/proxy request,
production registry execution, Supabase mutation, migration, GitHub Action,
commit, push, merge, checkout, or branch change was performed.

The machine-readable evidence is
`config/pagination_completeness_recovery_batch2.yaml`. All 20 active targets
remain `NEEDS_SOURCE_SPECIFIC_REPAIR`. This is a conservative outcome: no
deterministic evidence supplied a provider-supported immutable snapshot,
complete exact partition set, or scope-equivalent alternate inventory. Batch 2
therefore has no safe manual canary command and is not ready for manual canary
validation.

## Batch 2 source and root-cause map

All rows use `source_scope: default`.

| Company | Provider / variant | Exact source identifiers | Current bounds | Latest deterministic failure | Narrow cause |
|---|---|---|---|---|---|
| Meta | Meta GraphQL term search | fixed endpoint; three configured terms | 3 pages/term; 20 jobs; 3 MB | 20 jobs; `has_more`; cap reached | incomplete term inventory |
| JPMorgan Chase | Oracle Recruiting | `jpmc...`; `CX_1001`; `en` | 2 pages; 20 jobs; 3 MB | 2,448/7,393; 102 pages; mutation | mutable offset pagination |
| Emerson | Oracle Recruiting | `hdjq...`; `CX_1`; `en` | 2 pages; 20 jobs; 3 MB | 983/1,058; 41 pages; mutation | mutable offset pagination |
| Honeywell | Oracle Recruiting | `ibqbjb...`; finder `CX_1`; careers path `Honeywell` | 2 pages; 20 jobs; 3 MB | 72/1,265; 3 pages; mutation | mutable offset pagination |
| Microsoft | Eightfold PCSx | `apply.careers.microsoft.com`; org `microsoft.com` | 2 pages; 20 jobs; 3 MB | 21/2,369; 3 pages; overlap | mutable PCSx pagination |
| Qualcomm | Eightfold PCSx | `careers.qualcomm.com`; org `qualcomm.com` | 2 pages; 20 jobs; 3 MB | 20/2,029; 2 pages; overlap | mutable PCSx pagination |
| Micron | Eightfold PCSx | `careers.micron.com`; org `micron.com` | 2 pages; 20 jobs; 3 MB | 20/2,975; 2 pages; overlap | mutable PCSx pagination |
| Eaton | Eightfold PCSx | `eaton.eightfold.ai`; org `eaton.com` | 2 pages; 20 jobs; 3 MB | 20/2,365; 2 pages; overlap | mutable PCSx pagination |
| Rivian | iCIMS Jibe API | tenant `rivian`; board `rivian-public` | 10/page; 2 pages; 20 jobs; 3 MB | 138/795; mutation; 3 rejected rows | mutation plus malformed provider rows |
| Goldman Sachs | browser-rendered HTML | `higher.gs.com/results`; `/roles/{roleId}` | 1 page; 20 jobs; 3 MB | 20 jobs; active continuation | browser terminal proof missing |
| GlobalFoundries | Eightfold PCSx | `careers.gf.com`; org `globalfoundries.com` | 2 pages; 20 jobs; 3 MB | 20/558; 3 pages; overlap | mutable PCSx pagination |
| Oracle | Oracle Recruiting | `eeho...`; `CX_45001`; `en` | 1 page; 20 jobs; 3 MB | 2,063/2,228; 86 pages; mutation | mutable offset pagination |
| Stantec | browser-rendered HTML | Calgary query; 32-hex job paths | 1 page; 20 jobs; 3 MB | 50 jobs; active continuation | browser terminal proof missing |
| Espressif Systems | static HTML | official job-search page; stable path slug | 1 page; 20 jobs; 3 MB | 15 jobs; no pagination contract | missing terminal evidence |
| Bosch | SmartRecruiters v1 | slug `BoschGroup`; exact identity | configured 1 page/20; effective 100/page | 20/4,814; total not reached | total exceeded the then-reviewed 1,000-row adapter bound |
| CPKC | SuccessFactors columnized | `careers.cpr.ca`; `en_US`; 10/page | 2 pages; 20 jobs; 3 MB | 150/150 plus mutation contradiction | mutable pagination |
| Scotiabank | SuccessFactors table | `jobs.scotiabank.com`; exact identity regex | 1 page; 20 jobs; 3 MB | 926/1,683; mutation | mutable pagination |
| ecobee | static HTML | Generac brand page; exact ecobee regex | 1 page; 20 jobs; 3 MB | 5 jobs; no pagination contract | brand-scope terminal missing |
| Bombardier | SuccessFactors columnized | `jobs.bombardier.com`; `en_US`; 10/page | 2 pages; 20 jobs; 3 MB | 110/841 then repeated page | duplicate traversal |
| Fortinet | Oracle Recruiting | `edel...`; `CX_2001`; `en` | 1 page; 20 jobs; 3 MB | 967/968; 41 pages; mutation | mutable offset pagination |

Adapter grouping is therefore Oracle (5), Eightfold (5), SuccessFactors (3),
browser/static (4), and one each for Meta, iCIMS, and SmartRecruiters. There is
no Batch 2 production Workday target: Micron and GlobalFoundries remain
Eightfold; their Workday entries are unproven sibling migration leads.

## Shared-fix decisions

No shared adapter code was changed.

- Oracle already puts `offset` inside `findReqs`, reconciles the reported total,
  rejects short/empty pages below total, detects overlap/total drift, and makes
  one clean restart. jobseek's exact-sum facet partition is useful prior art,
  but no saved response proves a complete, bounded, non-overlapping facet set
  for these five exact sites. Its configured overlap/shortfall/count tolerances
  are rejected.
- Eightfold already treats total drift and cross-page repeated stable IDs as
  mutation. jobseek's sitemap-first watermark and freehire's PCSX/v2 response
  mechanics help protocol discovery, but watermark state and an arbitrary
  empty-page backstop cannot authorize absence.
- SuccessFactors cannot let a final total override mutation (CPKC/Scotiabank)
  or treat a repeated complete page as EOF (Bombardier). A generic
  `job_sitemap.xml` convention is not exact current-inventory evidence for
  these configured scopes.
- Static/browser sources need source-specific terminal contracts. Render
  exhaustion, short pages, generic next controls, and mere sitemap presence
  are not terminal proof.

## Sibling-repository decisions

jobseek:

- `ADAPT`: `core/monitors/oracle_hcm.py::_complete_facet_partition` and
  `discover_stream` for finder mechanics and exact-sum bounded facets.
- `CONCEPT_ONLY`: `core/monitors/eightfold.py::_sitemap_urls` and
  `discover_stream` for sitemap/PCSx joins and positive-only watermarking.
- `ADAPT`: `core/monitors/sitemap.py::_resolve_sitemap_index` and `discover`
  for fail-whole bounded child traversal.
- `REUSE_CONTRACT`: `core/monitors/workday.py` partition mechanics, already
  independently implemented with stricter Job Radar identity/accounting.
- `CONCEPT_ONLY`: `data/boards.csv` alternate leads for Meta, Qualcomm,
  Micron, GlobalFoundries, Rivian, Stantec, and Goldman Sachs.
- `REJECT`: Oracle `offset_overlap`, page-shortfall, duplicate-row, and
  total-count tolerances as absence authority.

freehire:

- `REUSE_CONTRACT`: `sources/oracle.go::listRequisitions` confirms finder-local
  offset.
- `ADAPT`: `sources/eightfold.go::pageList` confirms PCSX/v2 shapes, while its
  empty-page terminal and partial-detail behavior are rejected.
- `CONCEPT_ONLY`: `sources/workday.go::pageAll` / `splitByFacet`; Job Radar's
  Phase 4 protocol remains stricter.
- `ADAPT`: `sources/metacareers.go::Fetch` / `detail` confirms the sitemap and
  numeric stable ID, but failed details must become protected unreadables.
- `CONCEPT_ONLY`: `sources/successfactors.go::Fetch` / `detail` for a possible
  job-sitemap inventory after exact tenant/scope/active-state proof.
- `REJECT`: SmartRecruiters arbitrary empty-page completion below total.

JobSpy has no exact first-party provider contract for these targets.
`jobspy/__init__.py::scrape_jobs` is useful only as third-party search mechanics;
aggregator exhaustion, proxy behavior, and title/location matching are rejected
as employer identity or absence authority.

## Alternate inventory tracks

- Meta sitemap: exact first-party URL and numeric stable-ID shape are known,
  but Job Radar lacks a checked-in sitemap body/shard fixture, active-only proof,
  protected hydration accounting, and an item-derived request budget.
- Rivian sitemap: first-party lead, but no exact body/index/shard fixture,
  stale/active membership proof, or stable-ID hydration accounting.
- Goldman GraphQL: `https://api-higher.gs.com/gateway/api/v1/graphql`, operation
  `GetRoles`, page number starting at 0, page size 20, result
  `data.roleSearch.items`, total `data.roleSearch.totalCount`, stable `roleId`,
  and `/roles/{roleId}` URLs. The query unions `EARLY_CAREER` and
  `PROFESSIONAL`. No Job Radar fixture proves unauthenticated access, employer
  identity, union completeness, total stability, or terminal semantics.
- Stantec's global sitemap is discovery evidence and not authoritative for the
  configured Calgary query scope.
- Qualcomm sitemap, Micron/GlobalFoundries Workday boards, and
  Scotiabank/Bombardier RSS/sitemap paths remain migration/concept leads only.

No alternate was promoted and no candidate registry was created.

## Request budgets and terminal evidence

Stable lower bounds, before any provider mutation, are: Oracle—JPMorgan 309,
Emerson 45, Honeywell 53, Oracle 93, Fortinet 41 pages; Eightfold—Microsoft
237, Qualcomm 203, Micron 298, Eaton 237, GlobalFoundries 56 pages; Rivian 80;
SuccessFactors—CPKC 15, Scotiabank at least 68 at 25/page, Bombardier 85; Bosch
49 at 100/page, which exceeds the adapter's reviewed 1,000-job bound. One full
restart doubles each mutable-family traversal lower bound. None of these
calculations justifies a configuration increase: a larger budget does not make
a moving collection immutable.

Meta, Stantec, Espressif, ecobee, and Goldman remain `required: unknown`
because no authoritative page/partition/shard/total fixture bounds a complete
inventory.

Acceptable future terminal evidence is provider-specific:
`authoritative_total_reached`, `partition_set_complete`,
`sitemap_index_complete`, `sitemap_complete`, `GraphQL_total_reached`, or an
explicit provider final-page marker. Collection movement always produces
positive partial discovery plus `scan_complete=false` and
`absence_authorized=false` unless an immutable snapshot contract is proven.

## Inventory accounting and canary disposition

Every future candidate must satisfy:

```
provider inventory
= normalized jobs
+ protected unreadable identities
+ rejected records
+ known duplicate representations
```

Raw items, unique IDs, canonical URLs, duplicate IDs/URLs, rejections,
unreadables, and filter/retention exclusions must be explicit. No silent loss
is allowed.

`READY_FOR_CANARY`: none. `RECOVERED_BY_SHARED_FIX`: none.
`NEEDS_SOURCE_SPECIFIC_REPAIR`: all 20 Batch 2 targets. No target is assigned
`FAIL_IDENTITY`, `FAIL_CONTRACT`, `FAIL_TRANSIENT`, `UNSUPPORTED_PROVIDER`,
`NO_SAFE_REPAIR`, or `INSUFFICIENT_EVIDENCE`; the evidence points to specific
repair work rather than a terminal unsupported verdict.

There are therefore no future Batch 2 manual canary commands. A command will
be added only after a deterministic candidate exposes exact identity,
inventory accounting, a concrete terminal type, mutation tests, and an
item/page/partition/shard-derived request budget.

## Batch 2 playbook additions

11. Oracle facet partitioning is acceptable only when every provider-emitted
    bucket is valid and bounded, bucket counts sum exactly to one stable anchor
    total, IDs do not overlap, and all partitions terminate independently.
12. Eightfold sitemap/PCSx joining separates authoritative inventory from
    enrichment only after the exact sitemap is proven active, complete, and
    fail-whole. Watermarks remain positive-only.
13. Workday sibling boards are migrations, not repairs, until exact
    employer/site/scope identity is proven. The existing 2,000-cap protocol is
    retained unchanged.
14. Sitemap authority requires exact first-party binding, bounded index/shard
    traversal, no truncation or failed child, stable IDs before hydration, and
    an active-inventory contract. Sitemap presence alone proves nothing.
15. GraphQL authority requires a fixture for the exact operation and variables,
    identity-bearing fields, stable IDs, stable total/cursor semantics, complete
    filter/experience union, response bounds, and terminal reconciliation.
16. A terminal total cannot override contradictory overlap, repetition, total
    drift, malformed inventory, or unreadable stable identities.
17. Derive request budgets from exact totals, page size, partition count,
    sitemap shard count, and required hydration count. Unknown inventory means
    unknown budget, not a larger guess.

## Batch 3 recommendation

Use four evidence-focused groups:

1. First-party inventory fixtures: Meta, Rivian, Goldman Sachs, Stantec, and
   Qualcomm.
2. Provider partition evidence: JPMorgan Chase, Emerson, Honeywell, Oracle,
   Fortinet, and Bosch.
3. SuccessFactors contract work: CPKC, Scotiabank, and Bombardier.
4. Identity/scope migrations and static terminals: Micron, GlobalFoundries,
   ecobee, and Espressif Systems.

Keep Atlassian in the separate source-specific straggler backlog with its exact
live iCIMS URL/stable-ID contract failure.

## Batch 3 deterministic closeout and live activation

Batch 3 began at actual HEAD `7aca829` (`Complete pagination completeness
recovery batch 2`). Its parent is the requested Batch 1 checkpoint `51b443b`.
The deterministic review itself performed no checkout, reset, branch change,
live scan, live canary, provider request, browser/proxy session, production
mutation, migration, GitHub Action, commit, push, or merge. On 2026-10-12 the
operator supplied the later live canary evidence used for this closeout; the
repository work did not rerun Bosch.

The 18-source outcome is deliberately narrow:

| Status | Companies |
|---|---|
| `RECOVERED` / `LIVE_VALIDATED` / `PASS_COMPLETE` | Bosch |
| `NEEDS_SOURCE_SPECIFIC_REPAIR` | Stantec; JPMorgan Chase; Emerson; Honeywell; Oracle; Fortinet; CPKC; Scotiabank; Bombardier |
| `INSUFFICIENT_EVIDENCE` | Meta; Rivian; Goldman Sachs; Qualcomm; Micron; GlobalFoundries; ecobee; Espressif Systems |

There are no `RECOVERED_BY_SHARED_FIX`, `FAIL_IDENTITY`, `FAIL_CONTRACT`,
`FAIL_TRANSIENT`, `UNSUPPORTED_PROVIDER`, or `NO_SAFE_REPAIR` outcomes. Bosch
followed the required three-stage path: deterministic `READY_FOR_CANARY`, then
operator-provided live `PASS_COMPLETE` / `LIVE_VALIDATED` evidence, then
production activation of the exact tested candidate.

Machine-readable evidence, source-by-source missing proof, provenance,
budgets, the canary command, rollback, and the final queue are in
`config/pagination_completeness_recovery_batch3.yaml`. The isolated Bosch
candidate is in
`config/target_companies.pagination_completeness_batch3_canaries.yaml`.

### Accepted candidate: Bosch bound-only repair

The stored SmartRecruiters probes establish the exact `BoschGroup` source and
`Bosch Group` identity. On 2026-10-01, three consecutive 100-row pages carried
one stable `totalFound=4813`, 300 unique IDs, and no observed mutation. On
2026-10-05, two independent offset-zero anchors both carried
`totalFound=4835`. The latter partition probe did not complete its 720-row
country partition and is not authority; only its repeated global anchor and
page-size evidence are used for the bound.

At 100 rows per page, 4,835 jobs require 49 requests. The candidate rounds to
the next full page boundary: `max_pages=50`, `max_jobs=5000`, and an independent
50-request canary ceiling. This gave 165 jobs (3.41%) of bounded headroom at
candidate preparation time, not an arbitrary large limit. SmartRecruiters'
adapter validation ceiling permits 50 pages / 5,000 rows.

The operator canary collected all 4,950 advertised jobs in 50 requests, with
4,950 unique stable IDs and canonical URLs, no rejected or unreadable items,
no mutation, no hidden continuation, and valid authoritative-total terminal
evidence. The production Bosch row is now the exact tested candidate:
`page_size=100`, `max_pages=50`, `max_jobs=5000`,
`max_response_bytes=3000000`, and inventory-only normalization. Its
SmartRecruiters identity, endpoint, pagination semantics, and apply-URL host
contract are unchanged from the tested candidate.

The canary command that the operator ran was:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --registry config\target_companies.pagination_completeness_batch3_canaries.yaml --company Bosch --request-budget 50 --run
```

The repository closeout did not rerun it. The validator was non-persistent,
selected only Bosch, published no dashboard, and performed no lifecycle or
production-registry mutation. A moving total, overlap, repeated ID, request
exhaustion, or a total above the candidate bound must still fail closed.

Maintenance note: Bosch currently has only **50 jobs of remaining capacity**
beneath the 5,000-job validation ceiling. This is not a current failure. If a
later authoritative total exceeds 5,000, the source must report incomplete
rather than truncate or treat the configured ceiling as terminal evidence.

### First-party inventory results

- **Meta:** freehire proves a flat first-party sitemap, numeric
  `/profile/job_details/{id}` IDs, and JSON-LD hydration mechanics. It also
  drops unreadable details and accepts an empty sitemap. No stored evidence
  proves current-active membership, stale URL behavior, or protected hydration
  accounting. The source remains insufficient for absence authority.
- **Rivian:** the first-party sitemap is still only a catalog lead. No stored
  index/shard fixture proves exact job filtering, active membership, stable ID
  equivalence to the Jibe source, duplicates, or malformed-detail outcomes.
- **Goldman Sachs:** jobseek records the exact `GetRoles` query, page zero,
  20-row size, `data.roleSearch.items`, `totalCount`, `roleId`, the official
  `/roles/{roleId}` path, and the combined `EARLY_CAREER` / `PROFESSIONAL`
  experiences list. There is no response fixture proving unauthenticated
  replay, stable totals, required category coverage, or exact overlap in the
  union. Adding category totals would be unsafe.
- **Stantec:** stored rendered runs stop at `maximum_results_reached` for the
  Calgary query. The global sitemap is not scope-equivalent. No existing
  source-local API, XHR, total, or explicit final-page contract is recorded.
- **Qualcomm:** the first-party sitemap lead does not prove active-only
  membership, complete traversal, stable ID equivalence, duplicates, detail
  behavior, or a terminal contract. PCSx mutation remains authoritative only
  for positive discovery.

No first-party alternate inventory was accepted for absence reconciliation.

### Oracle partition result

The jobseek `_complete_facet_partition` mechanic accepts a facet only when
every bucket is well formed and bounded, bucket IDs are unique, and bucket
counts sum exactly to the root total. Its `discover_stream` keeps offset inside
the Oracle finder. freehire's `listRequisitions` independently confirms that
finder-local offset placement.

None of the stored JPMorgan Chase (`jpmc.fa.oraclecloud.com`, `CX_1001`),
Emerson (`hdjq.fa.us2.oraclecloud.com`, `CX_1`), Honeywell
(`ibqbjb.fa.ocs.oraclecloud.com`, `CX_1` / `Honeywell`), Oracle
(`eeho.fa.us2.oraclecloud.com`, `CX_45001`), or Fortinet
(`edel.fa.us2.oraclecloud.com`, `CX_2001`) evidence contains an exhaustive root
facet payload or per-bucket totals. Consequently the partition dimension and
partition count are unknown for every target. No generic partition repair was
implemented, and jobseek's overlap/count tolerances were not ported.

### SuccessFactors result

CPKC still reaches its advertised 150 only under contradictory collection
movement. Scotiabank remains at 926/1,683 with mutation. Bombardier repeats a
page after 110/841. jobseek's RSS implementation is a streaming XML parser but
does not supply an authoritative total, continuation, or active-inventory
contract. freehire's generic `job_sitemap.xml` implementation supplies useful
enumeration/detail mechanics but no checked-in target contract showing that
the three feeds are current, active-only, and complete. Search mutation and
repetition therefore remain visible; no alternate feed was adopted.

### Alternate ATS and static-terminal results

The Micron Workday lead is `micron.wd1.myworkdayjobs.com/external`; the
GlobalFoundries lead is
`globalfoundries.wd1.myworkdayjobs.com/External`. Both are mechanically
compatible with the existing Phase 4 Workday pagination and subdivision
architecture. Neither has stored first-party tenant binding, response identity,
maximum result count, apply URL evidence, or proof that its inventory and scope
equal the production Eightfold source. No migration config was created.

Two historical ecobee runs each found the same five positive jobs and two
Espressif runs each found the same 15. All four runs stopped with
`no_pagination_configured`. Stability of positives is not a terminal. Neither
source has a stored explicit page count, disabled-next marker, stable next URL,
atomic embedded collection, or finite category/section union. No static
adapter relaxation was made.

### Batch 3 playbook additions

18. Accept a first-party sitemap for absence only when exact employer binding,
    job-only selection, active-membership semantics, complete index/shard
    enumeration, duplicate accounting, stable IDs, and protected hydration are
    all explicit. A flat sitemap is not stronger merely because it is flat.
19. A GraphQL total authorizes absence only for the exact operation, variables,
    and complete required filter/category union. Reconcile unique IDs against
    category totals after exact overlap accounting; never add totals blindly.
20. Oracle partitioning requires an authoritative finite facet universe. Every
    bucket must carry an exact total and complete independently; the deduped
    union must reconcile to the root inventory, and any partition mutation
    invalidates the whole result.
21. SuccessFactors RSS or sitemap can replace mutable search pagination only
    with a target-specific current-active completeness contract. Successful XML
    parsing, a large feed, or a stable sample is not terminal evidence.
22. Migration from an unstable ATS to an alternate ATS requires exact
    first-party tenant/site identity and proof of intended-inventory and
    source-scope equivalence before a candidate config is created.
23. Static completion needs a provider-visible final page, disabled next,
    stable continuation exhausted by contract, atomic collection, or complete
    finite section union. Short, empty, or unchanged pages never suffice.
24. A bound-only repair is allowed when a trusted adapter already exposes an
    authoritative total and stored evidence determines the exact required
    pages. Headroom must be small and derived; the source remains candidate-only
    until a bounded canary reaches that total without mutation.

### Remaining queue

The recommended final Batch 4 has a ten-source core: Nordic Semiconductor, CN
Rail, Athabasca Oil, HP Inc., Seagate, SGS, AstraZeneca, Kinross Gold, Cameco,
and Nutrien. Add a gated prior-straggler lane for Atlassian, Microsoft, and
Eaton only if new deterministic evidence is available; do not repeat Batch 2
discovery. Coupang remains separate contract backlog. Samsung Electronics, GE
Vernova, General Motors, Caterpillar, Zalando, and Capital One remain a
separate identity-recovery batch; Batch 3 found no reason to reclassify them.

## Batch 4 deterministic recovery

Batch 4 started immediately after the Bosch activation closeout at repository
HEAD `4de6b6d`. It reused the immutable October 1 runtime snapshot and the two
retained SGS SmartRecruiters diagnostics. No Batch 4 source request or live
canary was run, and the production registry was not changed for Batch 4.

The ten-source core was reviewed in the recorded order. The gated Atlassian,
Microsoft, and Eaton lane was not admitted because no new deterministic
evidence supersedes the Batch 2/3 findings.

| Status | Companies |
|---|---|
| `READY_FOR_CANARY` | SGS |
| `NEEDS_SOURCE_SPECIFIC_REPAIR` | CN Rail; HP Inc.; Seagate; AstraZeneca; Kinross Gold |
| `FAIL_IDENTITY` | Cameco |
| `INSUFFICIENT_EVIDENCE` | Nordic Semiconductor; Athabasca Oil; Nutrien |

The exact evidence and source-specific missing proof are in
`config/pagination_completeness_recovery_batch4.yaml`. The only isolated
candidate is
`config/target_companies.pagination_completeness_batch4_canaries.yaml`.

### SGS bound-only candidate

The existing source identity remains exact: SmartRecruiters company slug
`SGS`, the company-scoped public v1 postings endpoint, full-match identity
`^SGS$`, and `jobs.smartrecruiters.com` apply URLs. The October 1 probe
recorded three 100-row pages, 300 unique IDs, no duplicate or mutation, and a
validated detail/apply-URL sample. The October 5 probe recorded two stable
global anchors at `totalFound=4619`; its completed 346-job U.S. partition is
corroboration, not global authority.

At 100 rows per page, the stored high-water total requires 47 requests. The
candidate therefore uses the already reviewed SmartRecruiters ceiling of
`page_size=100`, `max_pages=50`, `max_jobs=5000`, and
`max_response_bytes=3000000`, with inventory-only normalization and a strict
50-request canary budget. Production SGS remains unchanged at one page and 20
jobs pending live `PASS_COMPLETE` evidence.

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --registry config\target_companies.pagination_completeness_batch4_canaries.yaml --company SGS --request-budget 50 --run
```

An authoritative total above 5,000, any mutation or overlap, request-budget
exhaustion, identity/apply-URL failure, unreadable or rejected item, hidden
continuation, or safety-limit stop must fail incomplete. Deterministic
`READY_FOR_CANARY` is not production recovery; only the exact candidate that
returns operator-confirmed `PASS_COMPLETE` may be separately activated.

### Remaining Batch 4 findings

- Nordic Semiconductor may benefit from the shared Teamtailor absent-next
  terminal rule, but no retained current target response proves the exact live
  page and identity. It needs a bounded target canary before reclassification.
- CN Rail and Kinross Gold reached their reported totals only while the
  collection changed. Seagate repeated a complete page after 97 of 284 jobs.
  None is absence-authoritative without a stable SuccessFactors traversal or a
  target-specific complete active feed.
- HP Inc. retained only 16 of 911 advertised Eightfold records alongside
  mutation. AstraZeneca retained 600 of 1,050 Workday records alongside
  mutation and lacks validated runtime identity. Larger bounds cannot repair
  either condition.
- Athabasca Oil's two static cards remain positive discovery only. Cameco's
  alternate Jobvite tenant failed official binding, while the retained static
  source remains terminal-unproven. Nutrien's old zero-job browser result had
  unproven identity and is superseded by the later finding that the exact
  SuccessFactors endpoint, North America scope, and terminal contract are not
  yet established.
