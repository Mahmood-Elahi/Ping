# Phase 5 Coverage Expansion Review - Batches 1 and 2

Date: 2026-10-10

## Final Phase 5 closeout

Phase 5 is complete. The multi-scope architecture, production persistence,
bounded retention, pgcrypto resolution, and affected-key-only lifecycle
finalizer are deployed, and the normal hourly workflow is operator-verified
green. Lightspeed France was the final rollout candidate. Its bounded live
canary returned `FAIL_IDENTITY` twice because the strict Greenhouse boards API
returned HTTP 404 for `lightspeedhqfr`. Identity remained unproven, zero
inventory was collected, and no completeness or terminal authority existed.

Lightspeed France is therefore `REJECTED` and `NOT_ACTIVATED`. The production
registry remains unchanged at 533 companies/sources/scopes with zero active
multi-scope companies. The historical batch sections below preserve the earlier
investigation sequence; their former readiness is superseded by the final
canary evidence.

## Batch 2 outcome

Phase 5 Batch 2 is complete. It re-investigated 21 existing or directly
adjacent scope candidates without making live requests, running canaries,
changing production, modifying an adapter, adding a provider, or starting
Phase 6. The production registry remains 533 companies/sources and its SHA-256
remains
`b5e8e42e1047c03131480ea678e2ca42ba3dbf42bd3f6ca1d3e0290decfbff30`.

Batch 1 and Batch 2 historically carried one `READY_FOR_CANARY`: Lightspeed
France. The final canary later rejected it with `FAIL_IDENTITY`.
There were no new promotions because the strongest blockers require first-party
identity or terminal fixtures that do not exist locally. Eight unsupported
provider questions advanced to explicit `DEFER` decisions, Coupang Flex
Assistant was rejected as an evergreen funnel, and Applied Materials was
resolved to `KEEP_CURRENT` while Eightfold remains identity/scope blocked.

The most important new evidence is architectural. A preserved GM core fixture
contains `JR-202620983`, "Health & Safety Supervisor - Ultium Cells LLC", while
three facility-specific Ultium ADP boards also exist. Historical production
artifacts also contain BlackRock internship-program roles and Chevron
internships while specialized boards remain separately identified. Multiple
sources can therefore overlap one canonical employer and job population. At
the Batch 2 checkpoint, company-wide absence reconciliation and
provider-namespaced job keys could not safely activate such boards together.
`MULTI_SCOPE_ARCHITECTURE_REQUIRED` was the Phase 5 activation prerequisite;
Batch 3 subsequently implemented it.

### Candidate progression

| Company | Scope | Provider | Previous status | New status | What changed / next action |
|---|---|---|---|---|---|
| GM Financial | subsidiary global | Oracle Recruiting | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | Exact host/CX_1 confirmed; employer marker still absent. Capture identity fixture. |
| Ultium Cells | Spring Hill | ADP | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | GM core fixture proves partial overlap. Capture bootstrap identity and compare IDs. |
| Ultium Cells | Troy | ADP | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | Offset contract confirmed; identity/independence still absent. |
| Ultium Cells | Warren | ADP | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | No stronger employer or facility-independence evidence found. |
| General Motors | PPU | unresolved | not in Batch 1 | NEEDS_SCOPE_EVIDENCE | No exact URL/tenant/provider exists locally. Obtain first-party source. |
| Progress Rail | global | Salesforce fRecruit | UNSUPPORTED_PROTOCOL | DEFER | Reusable vacancy numbers exist; shared terminal/zero contract does not. |
| Progress Rail | United States | BrassRing | UNSUPPORTED_PROTOCOL | DEFER | Mature reference contract found; employer binding and approved browser integration remain missing. |
| BlackRock | early careers | tal.net | UNSUPPORTED_PROTOCOL | DEFER | 15 reference boards prove reuse; totals/terminal/zero remain unproven. |
| Chevron | university | Workday | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | Production internships prove scope overlap; official Workday binding remains absent. |
| Chevron Stations | separate entity | Workday | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | Separate tenant/site confirmed; first-party binding still absent. |
| CATL | global | source XML | NEEDS_IDENTITY_EVIDENCE | NEEDS_IDENTITY_EVIDENCE | Adapter needs publisher plus root/listing total; no fixtures supply them. |
| CATL | United States | browser-rendered HTML | UNSUPPORTED_PROTOCOL | DEFER | Existing browser contract fits navigation/IDs; terminal configuration is missing. |
| Mitsubishi Electric | non-US regions | unresolved directory | NEEDS_SCOPE_EVIDENCE | NEEDS_SCOPE_EVIDENCE | No exact regional board can be configured without guessing. |
| Siemens Healthineers | global HTML | browser/Avature-like HTML | NEEDS_SCOPE_EVIDENCE | NEEDS_SCOPE_EVIDENCE | SHSJB superset/facade relationship remains unknown. |
| Siemens Healthineers | China campus | Tupu360/DOM | UNSUPPORTED_PROTOCOL | DEFER | Stable detail IDs exist; listing completeness and zero do not. |
| Siemens Healthineers | China professional | Tupu360/DOM | not in Batch 1 | DEFER | Provider scope value exists, but exact Healthineers listing URL is absent. |
| Coupang Fulfillment Services | frontline | first-party JSON | UNSUPPORTED_PROTOCOL | DEFER | `postingId` is stable; no total/terminal/zero and no existing JSON variant fits. |
| Coupang Logistics Services | Flex Assistant evergreen | static HTML | not in Batch 1 | REJECTED | One evergreen funnel has no vacancy collection or stable per-job IDs. |
| Lightspeed Commerce | France | Greenhouse | READY_FOR_CANARY | REJECTED / FAIL_IDENTITY | Final bounded canary received strict boards API HTTP 404; identity remained unproven and no inventory/terminal authority existed. |
| Stantec | global | sitemap discovery | UNSUPPORTED_PROTOCOL | DEFER | Job URL filtering is discovery, not authoritative inventory/removal evidence. |
| Applied Materials | global/region unknown | Eightfold | not in Batch 1 | NEEDS_IDENTITY_EVIDENCE | Exact host exists, but organization identity/scope advantage do not. `KEEP_CURRENT`. |

Final Batch 2 totals are: 0 `READY_FOR_CANARY`, 8
`NEEDS_IDENTITY_EVIDENCE`, 3 `NEEDS_SCOPE_EVIDENCE`, 0
`DUPLICATE_SCOPE`, 0 `UNSUPPORTED_PROTOCOL`, 0 `PROVIDER_IMPLEMENTED`, 8
`DEFER`, and 2 `REJECTED`. There were 0 promotions and 0 provider
implementations. Nineteen candidates remain blocked/deferred; two are rejected.

### Provider decisions

| Provider/model | Decision | Reason |
|---|---|---|
| Salesforce fRecruit | DEFER | Three boards share `vacancyNo`, but pagination is board-specific DOM automation without a reusable authoritative total or zero contract. |
| BrassRing | DEFER | The reference has IDs, `JobsCount`, zero, stable pagination, duplicate checks, and mutation retry; employer binding is absent and execution is browser-interactive. |
| tal.net | DEFER | Fifteen boards establish value and `/opp/` identities, but no deterministic total, final page, or authoritative zero is preserved. |
| Stantec sitemap model | DEFER | A filtered sitemap can discover detail URLs but cannot prove job completeness, timely removal, or zero. |
| Coupang CFS protocol | DEFER | The singleton API has `postingId`, but no completeness contract and does not fit existing fixed JSON variants. |
| Tupu360 | DEFER | Reusable 24-hex detail identity exists; listing terminal and zero semantics remain unproven. |

No provider is `IMPLEMENTED` or `NOT_WORTH_IT` in Batch 2. BrassRing is the
strongest future candidate, but it still fails employer identity. No
one-company scraper was added.

### Company and scope conclusions

- **GM family:** GM Financial remains identity-blocked. Spring Hill, Troy, and
  Warren retain separate ADP ccIds. GM core contains at least one Ultium job,
  so Spring Hill/core is `PARTIAL_OVERLAP`; cross-facility overlap is
  `UNKNOWN`. GM PPU has no exact local source.
- **Applied Materials:** `KEEP_CURRENT`. Production uses the first-party
  `jobs.appliedmaterials.com` collection with stable numeric IDs. Eightfold has
  no organization identity, parity, regional split, or completeness advantage.
- **BlackRock:** production already emits internship roles while tal.net is
  explicitly early careers. The relationship is at least scope-level
  `PARTIAL_OVERLAP`; exact posting overlap is unknown. tal.net is deferred.
- **Chevron:** production emits internships, so University is
  `PARTIAL_OVERLAP` by scope. Exact parity and Workday identity are unproven.
  Chevron Stations remains a separate `DISJOINT` employer/scope.
- **CATL:** China production, global XML, and US remain mutually `UNKNOWN`.
  XML publisher/total fixtures and US browser terminal fixtures are missing.
- **Siemens Healthineers:** SHSJB versus global HTML is `UNKNOWN`; no ID
  crosswalk proves any stronger classification. China campus and professional
  are distinct provider scopes but remain unknown relative to SHSJB/global.
- **Lightspeed:** `lightspeedhqfr` is `REJECTED / FAIL_IDENTITY`. Its strict
  Greenhouse boards API returned HTTP 404 twice; identity and inventory
  authority were not established. Its relationship to the production global
  collection remains unknown and irrelevant to the rejection decision.
- **Stantec:** production Calgary is a semantic `SUBSET` of global. The sitemap
  can seed positive detail discovery but is not authoritative for absence.

### Multi-scope and lifecycle decision

Batch 2 concluded `MULTI_SCOPE_ARCHITECTURE_REQUIRED` before overlapping boards
could be activated. The former company-wide reconciliation could authorize
missing transitions across sibling boards. Batch 3 implemented the required
model: canonical company, source scope, and source identity are separate;
absence is scope-local; observations remain inspectable; and only exact
requisition IDs or canonical URLs link jobs. All active owners now participate
in aggregate lifecycle authority.

### Canary readiness and budget

The historical `lightspeed_fr_greenhouse` definition remains in the isolated
Batch 2 evidence registry, but it is final `REJECTED`, cannot be rerun in Phase
5, and is not activation-eligible:

| Candidate | Identity | Listings | Details | Partitions | Retry | Hard budget |
|---|---:|---:|---:|---:|---:|---:|
| Lightspeed Commerce - France | 0 | 1 | 20 | 0 | 0 | 21 |

The final bounded canary was later run manually twice. Both attempts returned
`FAIL_IDENTITY` after one request with Greenhouse HTTP 404. No retry,
activation, persistence, dashboard publication, or lifecycle mutation followed.

### Deterministic coverage report

`scripts/report_phase5_coverage.py` reports both batches: production remains
533/533 with 0 active multi-scope companies; Batch 1 has 20 candidates and 1
historical canary plan; Batch 2 has 21 investigations, 1 historical canary plan,
0 promotions, 8 provider deferrals, 0 provider implementations, and
`MULTI_SCOPE_ARCHITECTURE_IMPLEMENTED`. Phase 6 started is false.

## Batch 1 outcome

Phase 5 Batch 1 prepares an offline, non-production backlog of 20 independently
reviewed company/scope pairs. It originally produced one `READY_FOR_CANARY`,
which the final canary later rejected. No source was activated, replaced, or
merged. Phase 6 incremental scan logic was not started.

The production registry SHA-256 remains
`b5e8e42e1047c03131480ea678e2ca42ba3dbf42bd3f6ca1d3e0290decfbff30`.
The four pre-existing untracked historical artifacts were preserved.

## Coverage-expansion principles

- Employer identity and board scope are separate gates.
- A subsidiary with an independent recruiting surface remains an independent
  source even when it shares a parent brand.
- A global board is not assumed to supersede a regional board.
- Early-career, campus, professional, operational, and evergreen application
  surfaces are not interchangeable.
- Existing Job Radar provider contracts are preferred. A near-match does not
  justify weakening identity or terminal evidence.
- Stable provider IDs and canonical apply URLs are the overlap keys. Titles and
  locations are only diagnostics.
- Offline evidence can prepare a canary, but cannot activate production.
- Overlapping boards must not independently close the same logical job. Batch 1
  therefore leaves the production one-row-per-company lifecycle unchanged.

## Production coverage baseline

The configuration-only report in `scripts/report_phase5_coverage.py` reports:

- Canonical companies: 533
- Active configured sources: 533
- Identity status: 533 `contract_configured`
- Completeness capability: 533 `authoritative_capable`
- Active multi-scope companies: 0
- Phase 5 discovered multi-scope families: 10
- Phase 5 candidates: 20
- Opt-in canaries: 1

Provider distribution:

| Provider | Sources | Provider | Sources |
|---|---:|---|---:|
| workday | 108 | greenhouse | 102 |
| ashby | 72 | sap_successfactors | 44 |
| static_html | 27 | lever | 25 |
| oracle_recruiting | 16 | phenom | 16 |
| smartrecruiters | 15 | eightfold | 12 |
| icims | 12 | javascript_embedded_data | 10 |
| adp | 8 | avature | 7 |
| public_json | 7 | bamboohr | 7 |
| ukg_pro | 5 | dayforce | 5 |
| browser_rendered_html | 4 | talentbrew | 4 |
| hibob | 3 | ibm_search | 2 |
| jobvite | 2 | paycor | 2 |
| beesite | 2 | 16 single-source providers | 16 |

The 16 single-source providers are Amazon, Apple, Atlassian, ClearCompany,
Cornerstone CSOD, Deel, Exalead, first-party JSON, Google, mCloud CWS, Meta,
PeopleSoft, Shopify, StartDate, Teamtailor, and Uber. The JSON report is the
authoritative distribution if this prose becomes stale.

## Batch 1 selection and complete dispositions

| Rank | Company | Scope | Proposed platform | Identity | Scope | Status | Coverage value / blocker |
|---:|---|---|---|---|---|---|---|
| 1 | GM Financial | subsidiary global | Oracle Recruiting | medium | high | NEEDS_IDENTITY_EVIDENCE | Separate GM subsidiary; no preserved independent Oracle employer marker or organization ID. |
| 2 | Cadillac F1 Team | motorsport subsidiary | unsupported Voyse API | high | high | UNSUPPORTED_PROTOCOL | Distinct high-value engineering surface; provider has no reviewed Job Radar contract. |
| 3 | Ultium Cells | Spring Hill | ADP | medium | high | NEEDS_IDENTITY_EVIDENCE | Exact cid/ccId exists; bootstrap employer label for `identity_regex` does not. |
| 4 | Ultium Cells | Troy | ADP | medium | high | NEEDS_IDENTITY_EVIDENCE | Exact cid/ccId exists; bootstrap employer label for `identity_regex` does not. |
| 5 | Ultium Cells | Warren | ADP | medium | high | NEEDS_IDENTITY_EVIDENCE | Exact cid/ccId exists; bootstrap employer label for `identity_regex` does not. |
| 6 | Progress Rail | global subsidiary | unsupported Salesforce site | high | high | UNSUPPORTED_PROTOCOL | Distinct from Caterpillar; no authoritative Salesforce pagination contract. |
| 7 | Progress Rail | US subsidiary | unsupported BrassRing | high | high | UNSUPPORTED_PROTOCOL | Exact partner/site exists; Job Radar has no BrassRing contract. |
| 8 | BlackRock | early careers | unsupported tal.net | high | high | UNSUPPORTED_PROTOCOL | Explicitly distinct from professional Workday; terminal protocol unsupported. |
| 9 | Chevron | university | Workday | medium | high | NEEDS_IDENTITY_EVIDENCE | Exact tenant/site is known; Phase 4 employer identity remains unproven. |
| 10 | Chevron Stations | separate entity | Workday | medium | high | NEEDS_IDENTITY_EVIDENCE | Separate tenant/site; no configured Workday identity evidence. |
| 11 | CATL | global | source XML | medium | medium | NEEDS_IDENTITY_EVIDENCE | Exact official feed exists; employer/count/terminal fields are not preserved in fixtures. |
| 12 | CATL | United States | unsupported rendered site | high | high | UNSUPPORTED_PROTOCOL | Unique regional surface; no authoritative total/terminal evidence. |
| 13 | Mitsubishi Electric | non-US regions | unresolved regional directory | high | low | NEEDS_SCOPE_EVIDENCE | Production is US-only; no exact non-US entity/board/provider may be guessed. |
| 14 | Lightspeed Commerce | France | Greenhouse | high | high | REJECTED / FAIL_IDENTITY | Strict boards API returned HTTP 404; identity, inventory, and terminal authority were not established. |
| 15 | Siemens Healthineers | China campus | unsupported HTML variant | high | high | UNSUPPORTED_PROTOCOL | Campus scope is explicit; provider-native terminal evidence is absent. |
| 16 | Siemens Healthineers | global HTML | browser-rendered HTML | high | medium | NEEDS_SCOPE_EVIDENCE | May duplicate or front production Workday `SHSJB`. |
| 17 | Coupang Fulfillment Services | frontline | unsupported first-party API | high | high | UNSUPPORTED_PROTOCOL | Distinct CFS operational inventory; envelope completeness is unmodeled. |
| 18 | Coupang Logistics Services | camp evergreen | static HTML | high | high | REJECTED | Application funnel, not an inventory; no stable per-job identity. |
| 19 | Coupang Logistics Services | driver evergreen | static HTML | high | high | REJECTED | Application funnel, not an inventory; no stable per-job identity. |
| 20 | Stantec | global | unsupported sitemap inventory | high | high | UNSUPPORTED_PROTOCOL | Global contains Calgary; no reviewed sitemap inventory contract. |

Final status totals are: 0 `READY_FOR_CANARY`, 7 `NEEDS_IDENTITY_EVIDENCE`,
2 `NEEDS_SCOPE_EVIDENCE`, 0 `DUPLICATE_SCOPE`, 8 `UNSUPPORTED_PROTOCOL`,
and 3 `REJECTED`.

## Provider mappings and adapter decision

Existing Job Radar contracts were mapped without changes for Oracle Recruiting,
ADP, Workday, source XML, Greenhouse, browser-rendered HTML, and static HTML.
Lightspeed France had enough archived evidence to instantiate a bounded canary
configuration, but the live identity gate failed and the candidate is rejected.
The other supported-provider mappings remain blocked by identity or scope
evidence; their contracts were not loosened.

No Job Radar adapter was modified. No new Job Radar adapter was added.

All three read-only reference repositories were inspected. `jobseek` supplied
the concrete board crosswalk and Pinpoint deterministic fixtures. `freehire`
was checked for corroborating board/provider evidence and yielded no stronger
Batch 1 mapping. `JobSpy` was checked and contained no relevant canonical ATS
mapping for these scopes. No reference code was imported or changed.

## Parent and subsidiary decisions

- GM Financial, Cadillac F1 Team, and each Ultium facility remain separate from
  General Motors core. Ultium facility ccIds also remain separate from each
  other until stable-ID overlap is measured.
- Progress Rail is a separately recruiting Caterpillar subsidiary. Its global
  Salesforce and US BrassRing boards are not unioned with Caterpillar or with
  each other.
- Chevron Stations is separate from Chevron professional and university
  recruiting. Chevron University remains a scoped Chevron source.
- Coupang CFS and CLS are separate operational entities/surfaces. Their
  evergreen pages are not promoted into fake job inventories.
- No Mitsubishi-branded company is inferred to be Mitsubishi Electric. The
  unrelated Mitsubishi Motors and Mitsubishi Corporation reference rows were
  deliberately excluded from the Mitsubishi Electric expansion candidate.

No subsidiary was added to the canonical production company list. A future
activation proposal must document whether the entity becomes a canonical
company or a scoped source before changing naming semantics.

## Regional and global decisions

- CATL China production, CATL global feed, and CATL US remain separate. Their
  relationship is `UNKNOWN` pending exact ID/URL comparison.
- Mitsubishi Electric production is explicitly US. The global corporate page
  is a regional directory, not an inventory; exact non-US boards remain
  unresolved.
- Siemens Healthineers China campus is separate in intent. The global HTML to
  Workday relationship remains `UNKNOWN`.
- Lightspeed France was proposed as a separate board, but the strict API 404
  left identity unproven. It is rejected; overlap remains `UNKNOWN` and was not
  used to infer any production relationship.
- Stantec Calgary is a `SUBSET` of the proposed global scope by query intent.
  This does not prove that every Calgary job is present in the sitemap.

## Early-career and university coverage

Distinct early-career coverage was found for BlackRock, Chevron, and Siemens
Healthineers China. BlackRock tal.net is explicitly early careers and separate
from `BlackRock_Professional`. Chevron has an exact Workday `University` site.
The Healthineers China URL explicitly selects `CAMPUSRECRUITMENT`. None is
activated: BlackRock and Healthineers lack a safe terminal contract, while
Chevron still lacks fail-closed Workday employer identity.

## Overlap and lifecycle analysis

| Candidate relationship | Classification | Authoritative comparison key |
|---|---|---|
| GM core ↔ Cadillac F1 | DISJOINT | Voyse job ID/permalink |
| GM core ↔ Ultium Spring Hill | DISJOINT | ADP itemID/client requisition ID |
| Ultium Troy ↔ other Ultium ccIds | UNKNOWN | ADP itemID/client requisition ID |
| Ultium Warren ↔ other Ultium ccIds | UNKNOWN | ADP itemID/client requisition ID |
| Caterpillar ↔ Progress Rail global | DISJOINT | Salesforce vacancy number |
| Progress Rail global ↔ US | UNKNOWN | provider IDs plus canonical apply URL |
| BlackRock production ↔ early careers | UNKNOWN | tal.net opportunity ID and canonical URL |
| Chevron professional ↔ University | UNKNOWN | Workday requisition ID/externalPath |
| Chevron corporate ↔ Chevron Stations | DISJOINT | Workday requisition ID plus source identity |
| CATL China ↔ global ↔ US | UNKNOWN | source-owned job ID and canonical URL |
| Lightspeed production/global ↔ France | UNKNOWN; candidate rejected | No IDs or URLs were collected because the strict board endpoint returned 404 |
| Healthineers Workday ↔ global HTML | UNKNOWN | requisition/JobDetail ID and canonical URL |
| Coupang professional ↔ CFS/CLS | DISJOINT | postingId where an inventory exists |
| Stantec Calgary ↔ global | SUBSET | 32-hex URL job ID |

There are no Batch 1 `DUPLICATE_SCOPE` rows. The two rejected CLS surfaces are
rejected because they are evergreen application funnels, not because they are
duplicates. Potential facades/subsets remain `UNKNOWN` or `SUBSET`; no second
lifecycle authority is created.

## Canary readiness and budget

Only the historical `lightspeed_fr_greenhouse` definition is present in the
opt-in canary registry. Normal runtime does not load that file. The plan is now
marked `REJECTED`, `FAIL_IDENTITY`, and no-rerun. Its original budget was:

| Candidate | Identity/bootstrap | Listing | Details | Partitions | Retry | Hard budget |
|---|---:|---:|---:|---:|---:|---:|
| Lightspeed Commerce — France | 0 | 1 | 20 | 0 | 0 | 21 |

The result failed before any success gate: HTTP 404, unproven identity, zero
inventory, and no terminal/completeness authority. Activation remains false.

No rollback or retirement is required because `lightspeedhqfr` was never
activated or persisted. The production Lightspeed first-party source remains
untouched.

## Pinpoint decision: NOT_WORTH_IT

1. Meaningful Job Radar targets using Pinpoint: none found. The jobseek
   reference has 99 Pinpoint boards, but an offline normalized-name crosswalk
   found zero matches among Job Radar's 533 canonical companies.
2. Deterministic/reference evidence: sufficient. The reference implementation
   and tests define `GET https://{slug}.pinpointhq.com/postings.json`.
3. Stable IDs: posting URLs carry stable posting identities, and reference
   fixtures exercise them.
4. Completeness: the public endpoint returns one `data` collection; no
   pagination is required in the preserved contract.
5. Employer identity: it can be fail-closed against the exact subdomain plus
   native careers-page title; slug prettification is explicitly rejected by the
   reference evidence.
6. Listing completeness: listing objects contain rich descriptions, locations,
   compensation, and metadata; detail hydration is not required.
7. Coverage value: currently negligible for Job Radar targets. Building and
   maintaining an unused adapter is not justified in Batch 1.

The decision is value-based, not protocol-based. Pinpoint may be reconsidered
if a concrete target board is identified in a later Phase 5 batch.

## Known deferred candidates

- General Motors additional boards: GM Financial needs identity; Cadillac F1 is
  unsupported; three Ultium boards need ADP identity; GM PPU lacks an exact
  locally preserved board.
- Progress Rail: global Salesforce and US BrassRing remain unsupported and
  distinct from Caterpillar.
- BlackRock early career: exact tal.net scope exists; protocol unsupported.
- Chevron university: exact Workday site exists; employer identity unresolved.
- CATL regional/global: global feed needs identity/terminal fixtures; US needs a
  terminal contract; neither replaces CATL China.
- Mitsubishi regional: the global directory proves fragmentation but not an
  exact non-US board; no identifiers were guessed.
- Siemens Healthineers: China campus protocol unsupported; global HTML/Workday
  scope relationship unresolved.
- Coupang CFS/CLS: CFS API protocol unsupported; CLS evergreen funnels rejected
  as inventories. Flex Assistant remains in the later backlog.
- Stantec additional scopes: global sitemap is a real expansion over Calgary,
  but sitemap completeness is not modeled.

## Deterministic configuration-only reporting

`python scripts/report_phase5_coverage.py` reads only local YAML and source
contract metadata. It performs no HTTP, browser, Supabase, or GitHub action.
It reports the production baseline, provider distribution, identity and
completeness status, active and discovered multi-scope counts, candidate status
counts, and canary count.

## Remaining Phase 5 backlog (do not start in Batch 1)

The next approximately 20 rows should be drawn from: GM PPU; Coupang Flex
Assistant; Lightspeed global Greenhouse; Lightspeed Ashby; Applied Materials
Eightfold; Siemens Healthineers China professional; exact Mitsubishi Electric
Canada/Japan/Europe boards; Progress Rail provider support; BlackRock tal.net
provider support; CATL global feed fixture capture; Stantec sitemap support;
Chevron professional/University parity; GM core/subsidiary parity; and other
explicit scope additions discovered in the remaining historical review files.

The list above is the preserved Batch 1 handoff. Batch 2 dispositions and the
remaining backlog are recorded at the top of this review and in the Batch 2
candidate registry.

## Batch 3 completion: source-scoped lifecycle authority

Batch 2's `MULTI_SCOPE_ARCHITECTURE_REQUIRED` conclusion is now implemented.
The deterministic regression confirmed the old mismatch: provider job keys and
cooldown fingerprints were source-specific, but a successful scan authorized
missing transitions for every row sharing `company`. That allowed a
professional board to advance or close a sibling early-career, university, or
regional record.

Batch 3 introduces a normalized `source_scope` with legacy `default` behavior,
flat multiple-source registry rows, per-scope ownership state, and exact-only
cross-source equivalence. Staged synchronization now identifies company,
logical scope, and source/provider identity. Finalization reconciles only the
owning scope and derives canonical job activity across all owners atomically.
Unknown overlap remains independent; title/location similarity cannot create
lifecycle equivalence.

Deterministic production metrics remain:

| Metric | Count |
|---|---:|
| Canonical production companies | 533 |
| Configured production sources | 533 |
| Production source scopes | 533 |
| Multi-scope production companies | 0 |
| Batch 2 candidate multi-scope companies under the new model | 11 |

The Batch 2 candidate registry now carries explicit `canonical_company` and
`source_scope` fields. The opt-in Lightspeed France target shares canonical
company `Lightspeed Commerce` and uses logical scope `france`. Its final status
is `REJECTED` with `FAIL_IDENTITY`; activation remains false. No production
registry row was converted or activated.

The detailed identity, schema, reconciliation, and fixture contracts are in
`docs/PHASE5_MULTI_SCOPE_LIFECYCLE.md`. The final candidate evidence is closed;
remaining source-specific work is backlog, not a Phase 5 architecture blocker.

## Batch 4 completion: first multi-scope rollout readiness

Batch 4 prepares, but does not perform, the first production multi-scope
rollout. The forward migration now has a read-only Phase 3 preflight, explicit
transaction boundary, deterministic legacy ownership backfill, post-migration
verification contract, RLS/service-role checks, and explicit administrative
source retirement. The 533 current production rows retain the legacy default
scope and their pre-Phase-5 cooldown fingerprints; no production config row or
database was changed.

The registry now provides exact `(company, source_scope)` lookup and ordered
multi-source lookup. Legacy company-only lookup raises on ambiguity instead of
selecting a row. The offline Lightspeed proposal proves one canonical company,
two independently dispatched sources/scopes, distinct cooldown identities, and
unchanged default-source configuration. Company-facing metrics remain grouped;
per-source coverage diagnostics expose scope.

Lightspeed France's final bounded canary returned `FAIL_IDENTITY` for Greenhouse
slug `lightspeedhqfr`: the strict boards API returned HTTP 404. It is rejected,
not production-active, and no retirement applies. `PASS_COMPLETE` remains the
sole general activation-eligible canary result.

The post-architecture backlog is machine-readable in
`config/phase5_batch4_backlog.yaml`:

- `VALIDATION_ONLY`: none.
- `IDENTITY_RESEARCH`: GM Financial; three Ultium facilities; Chevron
  University and Chevron Stations; CATL global feed.
- `PROTOCOL_DEFERRED`: Progress Rail Salesforce and BrassRing; BlackRock
  tal.net; CATL US; two Healthineers China scopes; Coupang CFS; Stantec sitemap.
- `SCOPE_UNRESOLVED`: GM PPU; Mitsubishi Electric regions; Healthineers global
  HTML; Applied Materials Eightfold.
- `REJECTED`: Coupang Flex Assistant evergreen funnel; Lightspeed France
  (`FAIL_IDENTITY`).

No further Phase 5 action is pending for Lightspeed France. The fail-closed
rejection completes the rollout decision without activation. Deferred provider
work is source-coverage backlog, not an architecture blocker.

`docs/PHASE5_MULTI_SCOPE_ROLLOUT.md` is the final closeout and Phase 6 handoff.
