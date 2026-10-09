# Phase 3 final inventory, hydration, and reliability review

Types: **A** atomic inventory (complete normalized records arrive together);
**B** inventory is sufficient and details are optional; **C** inventory proves
existence but detail is required for a usable normalized record; **D** detail or
the surrounding traversal participates in identity/completeness proof; **E** is
nonstandard browser/custom behavior requiring provider-specific analysis.
Mixed adapters use the most conservative type supported by their current
variants.

## Final canonical 43-adapter matrix

The matrix counts the 43 registered canonical adapter names (the compatibility
`custom:*` aliases are not separate adapters). `workable.py` remains an
unregistered research module and is therefore not part of the canonical 43.

| Adapter | Type | Inventory source | Hydration source | Final disposition | Reason |
|---|---:|---|---|---|---|
| ADP | D | variant-specific public search | Workforce Now details only | COUPLED-BY-DESIGN | MyJobs rows are atomic and now retain valid earlier pages after late transport failure; Workforce Now still requires detail corroboration. |
| Amazon | A | first-party jobs search API | none | ATOMIC | Search rows are complete normalized records. |
| Apple | A | first-party search API | none | ATOMIC | Search rows are complete normalized records. |
| Ashby | B | compact hosted-board application data | selected posting pages | P3-SPLIT | Board data proves slug, stable ID, title, locations, URL, and company binding; detail is enrichment. |
| Atlassian | A | first-party GraphQL listing | none | ATOMIC | Listing response is atomic. |
| Avature | A | reviewed server-rendered search | none | ATOMIC | Validated cards normalize without per-item details. |
| BambooHR | B | `/careers/list` JSON | per-opening JSON detail | P3-SPLIT | Listing jobs are usable and details are optional enrichment. |
| BeeSite | A | GJB search API | none | ATOMIC | API rows are atomic. |
| browser-rendered HTML | E | rendered collection with browser terminal proof | navigation/static parse of rendered result | COUPLED-BY-DESIGN | Browser ownership, navigation, identity, and terminal evidence cannot be separated reliably. |
| ClearCompany | A | public career-site API | none | ATOMIC | API rows are atomic. |
| Cornerstone CSOD | A | identity-bound anonymous-token search API | none | P3-PARTIAL | After official-page and shell/token identity validation, valid earlier API pages survive only a later transport exception. |
| Dayforce | D | Candidate Portal search pages | conditional posting detail | COUPLED-BY-DESIGN | Account/client, board, route, duplicate requisitions, and missing-location detail checks remain coupled. |
| Deel | C | schema.org `ItemList` | public `JobPosting` page | P3-SPLIT | ItemList IDs protect prior rows on transient detail failure; details remain mandatory for creation/update. |
| Eightfold | A | PCSx search API | none | ATOMIC | Search records normalize atomically. |
| embedded data | A | reviewed embedded JSON | none | ATOMIC | One validated document contains complete records. |
| Exalead | A | career-card inventory | none | ATOMIC | Cards normalize atomically. |
| first-party JSON | A | reviewed company API | none | ATOMIC | Current variants return complete normalized rows. |
| Google | A | Careers search API | none | ATOMIC | Search rows are atomic. |
| Greenhouse | B | board jobs API/expanded board response | selected `/jobs/{id}` responses | P3-SPLIT | Listing proves stable identity and a usable base record; separately fetched detail only enriches. |
| HawkSearch v2 | A | search-index documents | none | ATOMIC | Documents normalize atomically. |
| HiBob | A | public career-site API | none | ATOMIC | API rows are atomic. |
| IBM Search | A | Careers search API | none | ATOMIC | Search records are atomic. |
| iCIMS/Jibe/Radancy | A | Jibe JSON, Radancy cards, or legacy portal cards | none | ATOMIC | Each variant validates its own tenant/site, stable ID, apply URL, and terminal rules; none has a separate detail stage. |
| Jobvite | A | counted tenant search page | none | ATOMIC | Search rows normalize atomically. |
| Lever | A | paginated postings API | none | P3-PARTIAL | Valid rows from earlier pages survive only a later transport exception. |
| M-Cloud CWS | A | Google Jobs search endpoint | none | ATOMIC | Endpoint rows normalize atomically. |
| Meta | A | Careers GraphQL pages | none | ATOMIC | GraphQL rows are atomic. |
| Oracle Recruiting | A | Candidate Experience API | none | ATOMIC | API rows are atomic. |
| Paycor | C | identity-bound CareerHome cards | per-job HTML detail/apply form | P3-SPLIT | Card IDs protect prior rows on transient detail failure; official apply evidence remains mandatory for creation/update. |
| PeopleSoft | C | identity-bound external-careers links | per-opening detail page | P3-SPLIT | Listing JobOpeningId/site/sequence protects prior rows on transient detail failure; detail/apply checks remain mandatory for creation/update. |
| Phenom | A | eager-load search contract | none | ATOMIC | Search contract returns complete records. |
| public JSON | A | variant-specific first-party JSON contract | none | ATOMIC | All eight supported variants are complete normalized-row contracts; they intentionally share no blanket inventory rule. |
| SAP SuccessFactors | D | RMK HTML or columnized listing pages | selected columnized detail pages | COUPLED-BY-DESIGN | Columnized detail supplies canonical/apply/requisition/tenant/location evidence; restart and mixed-variant rules remain coupled. |
| Shopify | A | embedded React Router postings | none | ATOMIC | Embedded postings are atomic. |
| SmartRecruiters | B | paginated company postings API | optional posting detail API | P3-SPLIT | Listing rows prove inventory and form usable base jobs. |
| source XML | A | first-party XML feed | none | ATOMIC | Feed entries are atomic. |
| StartDate | A | active-jobs API | none | ATOMIC | API rows are atomic. |
| static HTML | A | configured server-rendered cards | none | ATOMIC | Cards are atomic after terminal proof. |
| TalentBrew | A | server-rendered search | none | ATOMIC | Search records are atomic. |
| Teamtailor | A | server-rendered jobs | none | ATOMIC | Listing records are atomic. |
| Uber | A | same-origin search API | none | ATOMIC | API rows are atomic. |
| UKG Pro | A | public opportunity search | none | ATOMIC | Search records are atomic. |
| Workday | D | CXS search pages | requisition/detail-path corroboration | PHASE-4 | Identity and the 2,000-result cap remain intentionally fail-closed Phase 4 problems. |

Final adapter-level counts are 7 P3-SPLIT, 29 ATOMIC, 2 P3-PARTIAL, 4
COUPLED-BY-DESIGN, and 1 PHASE-4. ADP is counted once as coupled because its
Workforce Now variant is the conservative adapter-wide contract; its MyJobs
variant is atomic and also partial-progress enabled. Four concrete
provider/variant paths support partial progress: SmartRecruiters, Lever,
Cornerstone CSOD, and ADP MyJobs.

## Batch 2 migrations

Ashby validates the entire compact board before optional details. Every stable
board ID is emitted as an `InventoryItem`. A separately fetched malformed,
mismatched, oversized, or unreachable detail becomes a rejected/unreadable item
outcome and the usable listing record is retained. Listing failures remain
source failures.

Greenhouse retains strict board identity, duplicate checks, per-job attribution,
custom `apply_url_template`, and stable-ID URL correlation. The listing supplies
a usable base record. Only separately fetched detail can degrade at item level;
malformed embedded detail remains a source contract failure. Waymo, Ripple, and
Miro contracts are unchanged.

Lever is Type A and has no detail stage. If a later page raises a transport
exception after valid pages were normalized, it returns those discoveries in an
explicit incomplete `AdapterResult`. HTTP, identity, schema, and pagination
contradictions still fail.

The final batch adds the same narrowly scoped late-transport behavior to
SmartRecruiters inventory enumeration, Cornerstone CSOD after bootstrap/token
identity is validated, and ADP MyJobs. ADP Workforce Now is excluded because
detail corroboration remains part of its contract. No HTTP error, identity
failure, malformed payload, duplicate/conflicting identity, changed total, or
pagination contradiction is converted to partial success.

Deel, Paycor, and PeopleSoft now emit stable inventory identities before
details. A timeout/connection failure or transient detail HTTP status becomes
an unreadable item and protects an existing row. Deterministic detail identity,
site, title, route, and official-apply failures still fail closed. No listing-
only placeholder is inserted.

## Final review of the remaining eleven

| Provider | Listing independently proves existence? | Detail/traversal in identity? | Detail/traversal in terminal evidence? | Transient detail can be unreadable? | Late transport partial? | Blocker / disposition |
|---|---|---|---|---|---|---|
| ADP Workforce Now | Not by itself under the current contract | Yes: external ID, title, locations, tenant/apply corroboration | Listing total proves enumeration, but detail validates each usable identity | No | No | Provider contract; **COUPLED-BY-DESIGN** |
| ADP MyJobs | Yes; rows contain stable ID, site domain, and apply state | No separate detail | Authoritative listing total | n/a | Yes, transport exceptions after a valid page only | Reliability; **P3-PARTIAL** variant |
| browser-rendered HTML | Not before the owned render/navigation completes | Yes | Yes | No independently trusted item stage | No | Cannot distinguish late transport from ownership/navigation/terminal failure; **COUPLED-BY-DESIGN** |
| Cornerstone CSOD | Yes, after official page and anonymous shell/token bind tenant/site/API | Bootstrap participates; no per-job detail | Authoritative API total | n/a | Yes, transport exceptions after a valid page only | Reliability solved without token changes; **P3-PARTIAL** |
| Dayforce | Only for rows already carrying all required structured identity/location fields | Yes for rows requiring detail; shell/CSRF/board traversal also binds identity | Search total and duplicate requisition rules | Not safely for all rows under the current mixed contract | No | Protocol-specific row/detail semantics; **COUPLED-BY-DESIGN** |
| Deel | Yes: exact tenant-bound `ItemList` URL contains a stable UUID | Yes for creation/update | Complete ItemList is terminal | Yes, after bounded transient detail failure | Single-page contract | Reliability split completed; **P3-SPLIT** |
| iCIMS Jibe | Yes, using `client_code`, stable row identity, and allow-listed URL | No separate detail | Variant total/page rules | n/a | No; mutable-collection/rejected-row rules remain atomic | Current behavior is correct; **ATOMIC** |
| iCIMS Radancy | Yes, from card ID and canonical allow-listed path | No separate detail | Radancy total/next-page rules | n/a | No | Current behavior is correct; **ATOMIC** |
| iCIMS legacy portal | Yes, after tenant/board/card validation | No separate detail | Legacy indexed empty-page/next-page rules | n/a | No | Current behavior is correct; **ATOMIC** |
| Paycor | Yes: company-bound CareerHome card has client ID and stable job ID | Yes for usable title/description/apply record | Complete CareerHome container | Yes, for transient detail transport only | Single-page contract | Reliability split completed; **P3-SPLIT** |
| PeopleSoft | Yes: reviewed host plus site, JobOpeningId, and posting sequence | Yes for usable title and apply flow; stale detail is item-terminal evidence | Complete listing container plus explicit stale-detail route | Yes, for transient detail transport only | Single-page contract | Reliability split completed; **P3-SPLIT** |
| public JSON variants | Yes, separately for Aurora, WordPress, Shazamme, Optiver, Unitree, Valve, Roblox, and Dyson | Some variants use a separate identity request, but none has per-item hydration | Variant-specific index/count/atomic/terminal rules | n/a | No new partial path; mutation/count contracts remain fail-closed | Heterogeneous but correctly atomic; **ATOMIC** |
| SAP SuccessFactors | HTML cards do; columnized rows do not always prove the current usable identity record | Yes for selected columnized rows; restart traversal also matters | RMK total/page/restart evidence | Not without changing current detail identity contract | No | Provider protocol; **COUPLED-BY-DESIGN** |
| Workday | Stable rows exist, but source identity remains fail-closed and detail path corroborates requisition identity | Yes | Reported total/cap/traversal | No | No | Identity and 2,000-result cap; **PHASE-4** |

The table has fourteen rows because ADP and iCIMS are intentionally split into
their independently reviewed variants; it covers exactly the eleven requested
provider families.

## Paginated partial-progress audit

All registered paginated adapters were checked for valid page(s) followed by a
later transport failure. Partial progress is enabled only for:

- SmartRecruiters company postings: a timeout/connection exception after one or
  more identity-valid listing pages; earlier listing jobs are normalized
  without optional detail and the inventory remains incomplete.
- Lever postings: a timeout/connection exception after one or more valid pages.
- Cornerstone CSOD: a timeout/connection exception after official-page,
  shell/token, endpoint, and at least one result-page validation.
- ADP MyJobs: a timeout/connection exception after exact career-site config and
  at least one valid requisition page.

Every other paginated adapter remains atomic/coupled because its current
mutation restart, identity bootstrap, record validation, count/offset, browser
ownership, detail corroboration, or terminal contract cannot be separated
without provider-specific protocol work. Identity failure, HTTP contract
failure, changed totals, duplicate/conflicting stable identity, repeated pages,
malformed payloads, and safety-limit stops always remain fail-closed.

## Partial progress and lifecycle protection

Normalized discoveries from an explicit incomplete inventory are inserted or
refreshed by staged sync, but that company is omitted from absence
reconciliation. Unseen rows receive no missing increment or closure and no
terminal evidence is fabricated. A complete `hydration_degraded` inventory
protects unreadable IDs using the same persisted source keys. Protection never
creates a placeholder job.

## Source cooldown and recovery probes

`job_source_health` stores source/config key, company, platform, consecutive
transient failure count, `cooldown_until`, last failure classification, last
successful authoritative inventory, and update time. The key contains a SHA-256
fingerprint of normalized stable source-defining configuration: company,
platform, tenant/slug/subdomain/site/board/organization identifiers, locale,
endpoint variant, source/search/listing endpoints, pagination template, and
applied source facets. It excludes volatile scan budgets and selectors. A
repaired source therefore cannot inherit an obsolete cooldown.

Timeout/connection, request, repeated 429, selected 5xx, and comparable
transient source failures advance the streak.
Identity, malformed contract/pagination, contradictory total, configuration,
and safety-limit failures never create cooldowns.

Failures 1-2 have no cooldown; failure 3 uses 6 hours, failure 4 uses 12 hours,
and failure 5+ uses 24 hours. An active cooldown returns `cooldown_skipped`
without a provider request, freshness observation, or absence authority. The
first normal scan after expiry is the recovery probe and must satisfy ordinary
identity/completeness rules; only an authoritative inventory clears the streak.
Partial and other non-authoritative results preserve the prior streak and
cooldown. Timestamps are normalized to offset-aware UTC. No background
scheduler was added.

## Provider-wide circuit breaker decision

Provider-wide circuit breaking is deferred. Current failure evidence cannot
reliably distinguish a provider outage from a bad tenant, expired anonymous
token, retired board, locale mismatch, or source-specific configuration error.
Aggregating those failures could suppress unrelated healthy companies using the
same provider. Source/config-local cooldowns provide the bounded recovery needed
for Phase 3 without that cross-tenant risk.

## Forward database deployment

`20260924_staged_source_sync.sql` is a committed chronological migration and
was restored byte-for-byte to its pre-Phase-3 `HEAD` state. The repository also
has a mutable final-state bootstrap at `sql/supabase_schema.sql`; that file
continues to describe fresh installations. Existing deployments receive Phase
3 through `20261009_phase3_source_reliability.sql`, which idempotently creates
`job_source_health`, enables RLS, revokes public/anon/authenticated access,
grants service-role access, adds `protected_source_keys`, and replaces the
staged begin/finalize RPC contracts. The migration is not applied by this work.

## Phase 4 boundary

Phase 4 owns Workday source identity and the 2,000-result cap, plus any future
provider-protocol change needed to separate Dayforce or SAP columnized detail,
browser ownership/navigation, or ADP Workforce Now corroboration. This phase
adds no Workday subdivision, new RMK/CSRF/provider variant, new ATS, JazzHR, or
Algolia support.

## Future manual canaries (not run)

ServiceNow and Intuitive Surgical use SmartRecruiters listing-only mode. A
700-item board at 100 items per page should use seven listing requests and zero
details. An eight-request future manual canary budget (seven expected plus one
diagnostic allowance) is appropriate for each.

The definitions live only in
`config/target_companies.phase3_smartrecruiters_canaries.yaml`; normal runtime
does not reference that file. The existing read-only canary runner is reused
because `--registry` is explicit and it performs no persistence:

```powershell
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --registry config\target_companies.phase3_smartrecruiters_canaries.yaml --company ServiceNow --request-budget 8 --run
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py --registry config\target_companies.phase3_smartrecruiters_canaries.yaml --company "Intuitive Surgical" --request-budget 8 --run
```
