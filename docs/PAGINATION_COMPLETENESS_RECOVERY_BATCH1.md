# Pagination / Completeness Recovery - Batch 1

Date: 2026-10-11 (America/Edmonton)

## Outcome

Batch 1 is ready for **bounded manual canary validation of three sources only**:
Atlassian, Shopify, and the existing non-production Intuitive Surgical
SmartRecruiters candidate. No company is claimed production-recovered because
no live canary was run.

The 20 dispositions are:

| Outcome | Count | Companies |
|---|---:|---|
| A. Ready for manual canary | 3 | Atlassian; Shopify; Intuitive Surgical |
| B. Needs more deterministic repair | 10 | Meta; JPMorgan Chase; Emerson; Honeywell; Microsoft; Qualcomm; Micron; Eaton; Rivian; Goldman Sachs |
| C. Blocked / no safe repair | 1 | Coupang |
| D. Not actually pagination - reclassified | 6 | Samsung Electronics; GE Vernova; General Motors; Caterpillar; Zalando; Capital One |

Machine-readable source maps, budgets, status counts, pass gates, commands,
and rollback data are in
`config/pagination_completeness_recovery_batch1.yaml`.

## Evidence boundary

The review began from actual repository HEAD
`69bf3382a47781ae025c61a078fd6b5cbc0a1b70` (`Complete Phase 7
infrastructure audit`). The prompt's `ef021b4` is an ancestor; no checkout or
branch change was made. The production registry SHA-256 remains
`b5e8e42e1047c03131480ea678e2ca42ba3dbf42bd3f6ca1d3e0290decfbff30`.
The newest full runtime evidence remains the checked-in 2026-10-01 snapshot,
SHA-256
`fb12864e7d1b84931d56c8a831a51572377f06e9eb6d8d8970b65b76fd5e7a7b`.
It is historical evidence, not a claim about live health on 2026-10-11.

No live Job Radar scan, canary, company/provider request, browser session,
proxy request, production mutation, migration, GitHub Action, commit, push,
merge, checkout, or branch change occurred.

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
| Intuitive Surgical | production static; candidate SmartRecruiters | candidate slug `intuitive`; identity `^Intuitive(?: Surgical)?$` | candidate 100/page, 8 pages, 800 jobs, 3 MB | Production static: 20 plus continuation; Phase 3 candidate is listing-only and total-accounted | jobseek independently records the same SmartRecruiters slug |
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
- Static source contract: General Motors, Caterpillar, Intuitive Surgical,
  Coupang, and Zalando. A generic next link or short page is not an EOF.
- Source-specific inventory: Meta's search-term GraphQL and Goldman Sachs's
  browser results cannot represent complete employer inventory as configured.

The narrow causes are therefore: one atomic duplicate-accounting defect
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
| Intuitive Surgical | SmartRecruiters `intuitive` | Existing reviewed non-production candidate; ready for one bounded canary |
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

Config adjustment alone fixes none of the 20. The existing Intuitive candidate
already carries its justified bound; Atlassian and Shopify are atomic.

## Final company statuses

| Company | Final status | Reason |
|---|---|---|
| Atlassian | `READY_FOR_CANARY` | Atomic duplicate accounting is deterministic; live current payload still must pass |
| Shopify | `READY_FOR_CANARY` | Existing post-snapshot atomic repair is deterministic |
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
| Intuitive Surgical | `READY_FOR_CANARY` | Existing Phase 3 SmartRecruiters candidate has total-based listing contract |
| Coupang | `FAIL_CONTRACT` | Page parameter repeats page one; alternate scope/apply contract unproven |
| Zalando | `FAIL_IDENTITY` | Current static and alternate Workday identity are unproven |
| Capital One | `FAIL_IDENTITY` | Exact Workday employer/tenant/site evidence missing |
| Goldman Sachs | `NEEDS_SOURCE_SPECIFIC_REPAIR` | Browser discovery cannot prove exhaustion; GraphQL needs an adapter |

`RECOVERED_BY_SHARED_FIX`: none. No repair is production recovery until a
manual canary returns `PASS_COMPLETE`.

## Manual canaries - prepared, not run

Run Atlassian and Shopify together, with one independent request permitted per
source:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --company Atlassian --company Shopify --request-budget 1 --run
```

Run Intuitive Surgical against the explicit non-production candidate registry:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --registry config\target_companies.phase3_smartrecruiters_canaries.yaml --company "Intuitive Surgical" --request-budget 8 --run
```

Both commands use the existing read-only canary harness: persistence is
`none`, lifecycle mutations are zero, and no dashboard is published.

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
- Intuitive Surgical: production remains unchanged. If the candidate is later
  changed, restore only its stanza in the Phase 3 candidate registry.
- No production source migration or lifecycle deletion is part of rollback.

## Deterministic verification

All verification was local/offline.

| Matrix | Result |
|---|---|
| Focused Atlassian + Batch 1 review | 16 passed (7 adapter, 9 review-contract) |
| Source integrity, terminal evidence, inventory/hydration, AdapterResult, Phase 3-7, multi-scope lifecycle, incremental authority, retention | 356 passed, 60 subtests passed |
| Full pytest suite | 2,522 passed, 911 subtests passed in 196.23 seconds |
| `git diff --check` | Passed; only a Git CRLF-to-LF informational warning for the edited Atlassian file |

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
