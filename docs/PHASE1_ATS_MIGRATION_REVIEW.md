# Phase 1 ATS migration preparation

Date: 2026-10-07

> **Current disposition:** The section **Next Phase 1 screening batch
> (2026-10-07)** at the end of this file is authoritative for the latest
> 22-company review and closes the prior runnable-candidate decisions. Earlier
> sections are retained as the audit trail. Where sections conflict, the last
> dated batch section controls.

This document covers only Phase 1 of the approved roadmap. Three manually
verified Class A sources were activated in this step. Codex made no live
request, and no adapter architecture work was started.

## Status

- Remaining roadmap candidates reviewed: 22
- Pending candidate configurations: 8 configurations for 7 Class B companies
- Production migrations activated: 8 cumulative
- Scope/parity-gated companies: 7
- Proposed ATS replacements rejected in favor of an existing production feed: 7
- Additional generic-source candidates found by offline crosswalk: 1 (Coupang)
- Adapter changes: one fail-closed Greenhouse path-plus-query template extension;
  one Workday identity diagnostic; no Phase 2 architecture work

The local reference evidence came primarily from
`jobseek/apps/crawler/data/boards.csv`. The freehire Workday board tests also
corroborate the General Motors tenant/site shape. JobSpy contains no relevant
ATS mapping for this set. Reference implementations were not executed.

## Candidate decisions

| Company | Current source | Candidate ATS | Classification | Candidate prepared? | Manual canary required? | Scope concern | Notes |
|---|---|---|---|---:|---:|---|---|
| Ripple | `static_html` | Greenhouse `ripple` | A | Activated | Completed | None visible offline | Manual canary passed 121/121 with complete-collection evidence and the exact `ripple.com/.../{id}?gh_jid={id}` contract. |
| General Motors | `static_html` | Workday `generalmotors` / `careers_gm` | B | Yes | Yes | GM Financial, Cadillac F1, GM PPU, and Ultium use separate boards/providers | Candidate represents the primary GM board only. |
| Caterpillar | `static_html` | Workday `cat` / `CaterpillarCareers` | B | Yes | Yes | Progress Rail global and US boards are separate | Do not collapse Progress Rail into the Caterpillar Workday source. |
| Etsy | `static_html` | Workday `etsy` / `Etsy_Careers` | Rejected / deferred | No | No | No alternate board found | Complete 59/59 and healthy, but CXS exposes no independent employer identity proof. Do not weaken validation; retain production `static_html`. |
| Wise | `static_html` | SmartRecruiters `wise` | A | Activated | Completed | SmartRecruiters can be a subset behind mixed custom sites | Manual canary passed 402/402 in 5 pages with authoritative-total evidence. |
| ASML | `browser_rendered_html` | SmartRecruiters `ASML1` | A | Activated | Completed | No alternate board found in local references | Manual canary passed 17/17 with authoritative-total completion. |
| Aurora Innovation | `public_json` | Greenhouse `aurorainnovation` | D | No | No | Existing first-party `/api/jobs-index` may be broader/more authoritative | jobseek marks the Greenhouse mapping `skip`; retain the current high-confidence complete JSON feed. |
| Sanctuary AI | `javascript_embedded_data` | Lever `sanctuary` | Rejected | No | No | Current Kula account-scoped feed remains production | Lever returned HTTP 404; no stronger local alternate-tenant evidence exists. |
| SharkNinja | `static_html` | Greenhouse `sharkninjaoperatingllc` | A | Activated | Completed | None visible offline | Manual canary passed 151/151 with complete-collection evidence. |
| ServiceNow | `static_html` (Ontario page) | SmartRecruiters `ServiceNow` | Deferred to Phase 3 | No | No | Candidate is global while current source is regional | Board reported 709 postings; 300 were collected before one detail `ReadTimeout`. Preserve the candidate, but do not activate under current hydration/failure semantics. |
| Lightspeed Commerce | `static_html` | Greenhouse `lightspeedhq` | B | Yes | Yes | Separate `lightspeedhqfr` Greenhouse board and a `lightspeedhq` Ashby board exist | Determine whether global, France, and Ashby inventories overlap or are distinct. |
| Box | `static_html` | Greenhouse `boxinc` | A | Activated | Completed | None visible offline | Manual canary passed 129/129 with complete-collection evidence. |
| Miro | `javascript_embedded_data` | Greenhouse `realtimeboardglobal` | A | Activated | Completed | Legacy company/token relationship was manually validated | Manual canary passed 25/25 with complete-collection evidence and the exact `miro.com/.../{id}?gh_jid={id}` contract. |
| Redis | `javascript_embedded_data` | Ashby `redis` | A | Activated | Completed | Current first-party page parity | Manual canary passed 31/31 with complete-collection evidence. |
| Applied Materials | `static_html` | Workday `amat` / `External`; Eightfold also reported | B | Workday only | Yes | Workday and Eightfold may overlap, redirect, or represent different regions | Eightfold was not prepared because the reference has no observed `organization_id`, which Job Radar requires; guessing it would weaken identity. |
| Optiver | `public_json` | Greenhouse `optiverus` | D | No | No | Token strongly suggests US scope; current feed is explicitly global/same-origin | Retain the current first-party API. |
| BlackRock | `static_html` | Workday `blackrock` / `BlackRock_Professional` | B | Yes | Yes | `tal.net` early-careers board is separate | Workday professional inventory cannot replace early-careers scope. |
| Chevron | `static_html` | Workday `chevron` / `jobs` and `University` | B | Yes, as two distinct candidates | Yes | Professional, university, and Chevron Stations are separate scopes | Never merge `chevronstations.wd1.../CSI` into Chevron corporate scope. |
| Bolt | `javascript_embedded_data` | Greenhouse `boltv2` | D | No | No | Existing first-party collection may be broader | jobseek marks Greenhouse `skip`; no evidence supports replacing the complete embedded collection. |
| Roblox | `public_json` | Greenhouse `roblox` | D | No | No | Existing first-party CloudFront JSON may be broader/more authoritative | Retain the current high-confidence first-party feed. |
| Deliveroo | `public_json` | Ashby `deliveroo` | D | No | No | Existing first-party WordPress API may aggregate more than Ashby | jobseek marks Ashby `skip`; retain the paginated first-party source. |
| Zalando | `static_html` | Workday `zalando` / `ZalandoSiteWD` | B | Yes | Yes | First-party site parity and the local 500-result Workday ceiling | A total over 500 must remain incomplete and be deferred to protocol hardening. |

Ripple and Miro genuinely require one narrow extension to the existing
Greenhouse `apply_url_template` contract: the same API ID may appear once in
the exact path and once as the complete value of the sole query parameter.
Query-only Waymo templates and default strict Greenhouse board URLs remain
unchanged. Applied Materials Eightfold remains intentionally unrepresented
until its required organization identity is observed.

## Current Class A state

| State | Companies | Evidence / next step |
|---|---|---|
| Activated | Waymo, ASML, SharkNinja, Box, Redis, Ripple, Miro, Wise | All passed bounded manual canaries with validated identity and provider-native completion. |
| Rejected / production source retained | Etsy, Sanctuary AI | Etsy lacks independently verifiable Workday employer identity; Sanctuary AI's proposed Lever mapping is stale (HTTP 404). |
| Deferred to Phase 3 | ServiceNow | Mapping exists, but the current one-detail-request-per-posting model and whole-source failure semantics make activation unsafe. |

Class A is closed. Etsy and Sanctuary AI are final non-activations for this
phase, and ServiceNow has an explicit Phase 3 disposition rather than a failed
mapping classification.

## Additional candidate discovered

An offline name-to-board crosswalk found Coupang (`static_html` to Greenhouse
token `coupang`). It was not added to the candidate registry. The same local
reference lists separate Coupang fulfillment/frontline and logistics boards,
and the global board uses a first-party `www.coupang.jobs` URL. Under the
strict Greenhouse policy, Job Radar needs observed URL-identity evidence before
adding any custom host/template. Classify Coupang as B and revisit in Phase 1
after that evidence; separate employer scopes belong in Phase 5 coverage, not
in a silent global-board collapse.

## Candidate and rollback representation

`config/target_companies.phase1_ats_candidates.yaml` contains only the eight
explicit opt-in configurations for the seven remaining Class B companies.
All activated Class A companies and the rejected Etsy and Sanctuary AI
candidates were removed. ServiceNow was also removed because the registry has
no safe inactive/deferred state; its exact candidate definition is preserved
below for Phase 3.
Runtime loading defaults to `config/target_companies.yaml`. The canary script
uses the candidate file only when `--registry` is supplied, and the regression
test asserts the default path remains production.

Chevron professional and university sources have distinct candidate names so
each can be canaried independently. Applied Materials and BlackRock also use
scope-qualified candidate names. If activated later, the selected source must
be written back under the original production company name only after the
scope decision is made.

`config/target_companies.phase1_ats_rollback.yaml` preserves the exact prior
production definitions for all 17 companies that reached candidate review.
For pending, deferred, and rejected candidates it still equals production; for activated
companies it is intentionally different and is the rollback source of truth.

## ServiceNow Phase 3 evidence

ServiceNow is not a failed ATS mapping. The SmartRecruiters board exists and
reported 709 postings. The manual canary successfully collected 300 postings
before a single detail request raised a 20-second `ReadTimeout`; 347 requests
had started. There was no request-budget stop, pagination contradiction,
collection mutation, or rejected-record evidence. The failure reflects the
current one-detail-request-per-posting execution model and whole-source failure
semantics, not malformed inventory pagination.

Phase 3 should revisit this candidate only after adding inventory/hydration
separation, per-item unreadable outcomes, bounded hydration retry, and
preservation of successful partial discoveries without absence authority. The
exact deferred candidate is:

```yaml
- name: ServiceNow
  platform: smartrecruiters
  slug: ServiceNow
  endpoint_variant: companies_postings_v1
  careers_url: https://careers.servicenow.com/
  endpoint_url: https://api.smartrecruiters.com/v1/companies/ServiceNow/postings
  identity_regex: ^ServiceNow$
  allowed_apply_hosts:
    - jobs.smartrecruiters.com
  page_size: 100
  max_pages: 10
  max_jobs: 1000
  max_response_bytes: 3000000
```

## Remaining Class B canary commands

Run these from the repository root in a future Class B task. Commands
containing `--run` perform manual live canaries; Codex did not execute them in
this activation task.

```powershell
$candidateRegistry = (Resolve-Path .\config\target_companies.phase1_ats_candidates.yaml).Path
```

Class B candidates and their parity commands remain unchanged and were not run
as part of this activation step:

```powershell
.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $candidateRegistry `
  --company "General Motors" `
  --company "Caterpillar" `
  --company "Lightspeed Commerce" `
  --company "Applied Materials (Workday)" `
  --company "BlackRock (Professional)" `
  --company "Zalando" `
  --request-budget 52 `
  --run

.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $candidateRegistry `
  --company "Chevron (Professional)" `
  --company "Chevron (University)" `
  --request-budget 52 `
  --run

.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --company "General Motors" `
  --company "Caterpillar" `
  --company "Lightspeed Commerce" `
  --company "Applied Materials" `
  --company "BlackRock" `
  --company "Zalando" `
  --request-budget 52 `
  --run

.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --company "Chevron" `
  --request-budget 52 `
  --run
```

Each company remains individually addressable by supplying only its own
`--company` argument. A canary-budget stop is incomplete evidence, never a
terminal condition.

## Activation criteria

Every candidate requires `status: ok`, `canary_outcome: PASS_COMPLETE`,
`scan_complete: true`, a healthy source-health code, validated source identity,
provider-native terminal evidence, no safety/budget termination, no malformed
or conflicting pagination, no mutation, no rejected rows, unique stable IDs
and canonical URLs, and a plausible inventory.

Additional gates:

- General Motors: compare primary GM inventory and confirm GM Financial,
  Cadillac F1, GM PPU, and Ultium remain distinct sources.
- Caterpillar: prove the Workday board is primary Caterpillar only; preserve
  Progress Rail global and US boards separately.
- Lightspeed Commerce: compare `lightspeedhq`, `lightspeedhqfr`, Ashby
  `lightspeedhq`, and the first-party page by stable IDs and geography.
- Applied Materials: establish the missing Eightfold organization identity,
  then compare Eightfold, Workday, and the first-party source by IDs, regions,
  and redirects. Do not choose based on raw count.
- BlackRock: prove the Workday professional source plus the separate tal.net
  early-careers source covers the intended production scope.
- Chevron: compare professional and university sites for overlap; preserve
  Chevron Stations as a distinct employer/source.
- Zalando: compare stable IDs and geography with the first-party source. If the
  authoritative total exceeds 500, defer completion to Phase 4 rather than
  treating the local adapter ceiling as terminal evidence.

## Deferred and rejected replacements

- Phase 1 after more evidence: General Motors, Caterpillar, Lightspeed
  Commerce, Applied Materials, BlackRock, Chevron, Zalando, and Coupang.
- Phase 3 reliability revisit: ServiceNow SmartRecruiters, after inventory and
  hydration are separated and partial successes no longer grant absence
  authority.
- Phase 4 only if their observed totals require it: large Workday candidates;
  Applied Materials Eightfold if its inventory approaches the provider cap.
- Phase 5 coverage: distinct subsidiary/regional boards found for GM,
  Caterpillar/Progress Rail, Lightspeed France, Chevron Stations, and Coupang.
- No migration currently recommended: Aurora Innovation, Optiver, Bolt,
  Roblox, Deliveroo, Etsy, and Sanctuary AI. The first five retain stronger
  first-party feeds; Etsy cannot prove independent Workday employer identity;
  Sanctuary AI's proposed Lever `sanctuary` endpoint returned HTTP 404, so its
  current Kula account-1484 feed remains authoritative.

## Rollback map

| Company | Old source | Candidate source | Rollback evidence location |
|---|---|---|---|
| Ripple (activated) | `static_html` `ripple.com/careers/` | Greenhouse `ripple` | `config/target_companies.phase1_ats_rollback.yaml` |
| General Motors | `static_html` `search-careers.gm.com` | Workday `generalmotors/careers_gm` | same |
| Caterpillar | `static_html` `careers.caterpillar.com` | Workday `cat/CaterpillarCareers` | same |
| Etsy (rejected) | `static_html` `careers.etsy.com` | Workday `etsy/Etsy_Careers` | same; production remains the old source |
| Wise (activated) | `static_html` `wise.jobs` | SmartRecruiters `wise` | same |
| ASML (activated) | `browser_rendered_html` `asml.com` | SmartRecruiters `ASML1` | same |
| Sanctuary AI (rejected) | embedded Kula account 1484 | Lever `sanctuary` HTTP 404 | same; production remains the old source |
| SharkNinja (activated) | `static_html` `careers.sharkninja.com` | Greenhouse `sharkninjaoperatingllc` | same |
| ServiceNow (deferred to Phase 3) | Ontario `static_html` page | SmartRecruiters `ServiceNow` | same; production remains the old source and candidate is preserved above |
| Lightspeed Commerce | first-party `static_html` | Greenhouse `lightspeedhq` | same |
| Box (activated) | `static_html` `careers.box.com` | Greenhouse `boxinc` | same |
| Miro (activated) | embedded first-party collection | Greenhouse `realtimeboardglobal` | same |
| Redis (activated) | embedded first-party collection | Ashby `redis` | same |
| Applied Materials | first-party `static_html` | Workday `amat/External` | same |
| BlackRock | first-party `static_html` | Workday `BlackRock_Professional` | same |
| Chevron | first-party `static_html` | Workday `jobs` plus distinct `University` candidate | same |
| Zalando | first-party `static_html` | Workday `ZalandoSiteWD` | same |

## Phase 1 completion assessment

Class A is closed: eight sources are activated, Etsy and Sanctuary AI retain
their production sources, and ServiceNow is explicitly deferred to Phase 3.
Phase 1 as a whole is not complete because the seven Class B companies still
require their canaries and scope/parity gates; Coupang remains documented as an
additional Class B discovery outside the runnable candidate registry. These
decisions do not change the approved seven-phase roadmap. No Class B, Phase 2,
or Phase 3 implementation began in this task.

## Current Phase 1 batch (2026-10-07)

This pass reviewed the seven required Class B companies and the eleven next
highest-value generic-source companies for which the local repositories contain
defensible replacement evidence. It did not pad the batch to twenty: the
remaining generic targets either lack a concrete better source or already use
a stronger complete first-party feed. No live source was contacted.

### Batch summary

- Companies reviewed: 18 (7 required plus 11 additional).
- Runnable configurations prepared: 3 (`Lightspeed Commerce`, `Cameco`, and
  `Intuitive Surgical`).
- Migrations rejected in favor of the current source: 3 (`Continental`, `Snap`,
  and `Imperial Oil`).
- Protocol/architecture deferrals: 10 companies, primarily Workday identity,
  unsupported structured endpoint contracts, or multi-source lifecycle limits.
- Phase 5 additions discovered: GM subsidiaries, Progress Rail, Lightspeed
  France/possible Ashby inventory, BlackRock early careers, Chevron University
  and Chevron Stations, Coupang frontline/logistics inventories, and Stantec
  global inventory.
- Production activations in this pass: 0.

### Batch table

| Company | Current source | Candidate/better source | Class | Migration or addition? | Runnable? | Main risk |
|---|---|---|---|---|---:|---|
| General Motors | `static_html` core GM page, rank 115 | Workday `generalmotors/careers_gm` | C | Replacement; other boards are additions | No | Workday identity is unproven; subsidiaries are separate scopes |
| Caterpillar | `static_html`, rank 161 | Workday `cat/CaterpillarCareers` | C | Replacement; Progress Rail is an addition | No | Workday identity is unproven; Progress Rail must not be collapsed |
| Lightspeed Commerce | first-party `static_html` | Greenhouse `lightspeedhq` | B | Replacement | Yes | France and Ashby inventory may be distinct or overlapping |
| Applied Materials | `static_html`, rank 27 | Workday `amat/External`; Eightfold also reported | C | Replacement candidate | No | Workday identity is unproven; Eightfold organization identity is unknown |
| BlackRock | `static_html`, rank 88 | Workday `BlackRock_Professional` | C | Professional replacement; early careers is an addition | No | Professional board would drop the separate student/early-career scope |
| Chevron | `static_html`, rank 171 | Workday `jobs` plus `University` | C | Two replacements/additions, not one merged source | No | Workday identity and multi-source lifecycle correctness |
| Zalando | `static_html`, rank 210 | Workday `ZalandoSiteWD` | C | Replacement | No | Workday identity plus a possible 500-result ceiling |
| Cameco | Jobvite page parsed as `static_html` | Jobvite `cameco` counted search | A | Replacement | Yes | Official page must bind the exact tenant and final range |
| Intuitive Surgical | `static_html`, rank 162 | SmartRecruiters `intuitive` | B | Replacement | Yes, discovery first | Unknown total; one detail request per posting |
| Coupang | paginated `static_html`, rank 202 | Greenhouse `coupang` | B | Global replacement; Korean operational boards are additions | No | Exact first-party Greenhouse apply-URL template is not established |
| Continental | complete first-party JSON, rank 124 | SmartRecruiters `continental` | D | Proposed replacement | No | Existing attributed JSON is stronger; reference has other distinct boards |
| Snap | exact first-party embedded collection | Workday `snapchat/snap` | D | Proposed replacement | No | Workday cannot prove identity; current feed has exact per-job attribution |
| Goldman Sachs | browser-rendered `higher.gs.com` | first-party GraphQL; Oracle `CampusHiring` | C | GraphQL replacement; Oracle is narrower | No | No supported GraphQL contract; Oracle drops professional roles |
| Unitree Robotics | single-page `static_html`, rank 156 | first-party jobs API with `data.count` | C | Replacement | No | No supported endpoint variant or reviewed terminal contract |
| MediaTek | `browser_rendered_html` | first-party paginated POST API | C | Replacement | No | No supported endpoint variant or proven terminal signal |
| Stantec | browser-rendered Calgary search | global sitemap | E | Global coverage addition | No | Global sitemap is not scope-equivalent to the Calgary target |
| Yaskawa Electric | first-party static list linking UKG details | UKG Pro | C | Replacement | No | Exact public UKG JobBoard UUID/tenant identity is absent |
| Imperial Oil | exact ExxonMobil category parsed as `static_html` | parent SuccessFactors source | D | Proposed replacement | No | Parent feed is broader and the exact Imperial category is already terminal |

### Ranked additional-candidate pool and selection rationale

The offline scan covered all 48 current `static_html`,
`browser_rendered_html`, `javascript_embedded_data`, and `public_json` targets.
The selected eleven rank as follows:

1. Cameco — supported Jobvite adapter, same observed tenant, counted terminal page.
2. Intuitive Surgical — supported SmartRecruiters identifier and high-ranked
   brittle static source; total/request cost needs one bounded discovery call.
3. Coupang — supported Greenhouse token and high-ranked paginated HTML, but the
   exact custom apply-URL contract and separate Korean scopes block execution.
4. Goldman Sachs — the reference records the first-party GraphQL inventory and
   a narrower campus Oracle board; high migration value but no safe current
   GraphQL contract.
5. Unitree Robotics — the reference records an attributed API and total field;
   Job Radar lacks a reviewed variant.
6. MediaTek — the reference records the exact first-party POST collection;
   terminal pagination is not yet modeled.
7. Stantec — the reference sitemap could remove browser rendering, but it is
   global while production intentionally searches Calgary.
8. Yaskawa Electric — current rows reach UKG details, but no exact board URL
   exists from which the UKG adapter can prove tenant and board identity.
9. Continental — a concrete SmartRecruiters board exists, but the current
   attributed first-party JSON is already stronger and the reference lists
   separate ContiTech/BestDrive scopes.
10. Snap — a concrete Workday board exists, but it cannot beat the current
    exact, employer-attributed first-party collection under the identity gate.
11. Imperial Oil — the parent SuccessFactors source is concrete but broader
    than the exact Imperial category already in production.

Two additional slots were deliberately left empty. H2O.ai, Ouster, and Symend
already parse exact unpaginated JazzHR boards and Job Radar has no JazzHR
adapter; Siemens has only another DOM representation; Nutrien has no concrete
tenant/board mapping in the references; and the retained public/embedded feeds
for Aurora, Optiver, Roblox, Bolt, Deliveroo, IMC Trading, and similar targets
are stronger than the narrower ATS suggestions. Those are not defensible
Phase 1 migrations merely to reach a numeric batch size.

### Required Class B analysis

- **General Motors:** `careers_gm` is the core GM board. GM Financial, Cadillac
  F1, GM PPU, and Ultium have separate provider identities in
  `jobseek/apps/crawler/data/boards.csv`; they are distinct Phase 5 coverage,
  not evidence that the core board is incomplete. Replacement cannot run now
  because the Workday adapter always reports independent identity as unproven.
- **Caterpillar:** `CaterpillarCareers` is the corporate board. Progress Rail's
  Salesforce and BrassRing boards are separate inventories and remain Phase 5
  additions. The Workday replacement is Phase 4-blocked on identity.
- **Lightspeed Commerce:** Greenhouse `lightspeedhq` is the strongest runnable
  global/core hypothesis. `lightspeedhqfr` and Ashby `lightspeedhq` require
  stable-ID, geography, and Canada-role comparison before replacement.
- **Applied Materials:** the reference contains both Workday `amat/External`
  and an Eightfold careers host. It does not establish whether they redirect,
  overlap, or split regions, and supplies no Eightfold `organization_id`.
  Neither should run under the current identity contract.
- **BlackRock:** Workday is explicitly professional inventory while the
  separate `blackrock.tal.net` board is early careers. Professional Workday
  cannot replace the current source if it removes student/intern roles.
- **Chevron:** professional and university Workday sites are distinct and both
  are needed to test intended scope. The current single-source lifecycle cannot
  safely collapse them; Chevron Stations is a distinct Phase 5 employer/source.
- **Zalando:** the Workday mapping is plausible, but current code cannot prove
  employer identity and cannot establish completeness above its 500-result
  ceiling. Both are Phase 4 blockers.

### Additional-company findings

- **Cameco:** Job Radar production already points at the `cameco` Jobvite
  tenant. The supported adapter adds official-page tenant binding, exact
  canonical paths, and the authoritative final result range. Canary it.
- **Intuitive Surgical:** jobseek row `intuitive-careers` identifies
  SmartRecruiters `intuitive`. This likely replaces the same global careers
  inventory, but first discover `totalFound`; canary only if the calculated
  detail cost remains within the standard Phase 1 budget.
- **Coupang:** jobseek records Greenhouse token `coupang` at the same global
  first-party page plus separate CFS/CLS operational inventories. Do not canary
  until an exact stable-ID custom apply URL is observed; additions are Phase 5.
- **Continental:** production's JSON pages require exact `client=continental`
  attribution. SmartRecruiters is not proven broader and the reference lists
  other Continental scopes, so retain production.
- **Snap:** production's server-authored cache requires exact
  `jobPostingSite=Snap Inc.` attribution. Workday would be identity-unproven;
  retain production.
- **Goldman Sachs:** the reference GraphQL query explicitly includes both
  `EARLY_CAREER` and `PROFESSIONAL`; Oracle is `CampusHiring` only. Model the
  first-party GraphQL terminal contract in Phase 4 rather than narrow scope.
- **Unitree Robotics:** the reference API exposes `data.items` and
  `data.count`; worth revisiting in Phase 4 after identity, pagination, and
  total mutation behavior are modeled.
- **MediaTek:** the reference POST endpoint is concrete, but no authoritative
  total/terminal rule is recorded. Revisit in Phase 4.
- **Stantec:** the sitemap is global and therefore a Phase 5 addition relative
  to the current Calgary query, not a silent replacement.
- **Yaskawa Electric:** UKG detail links alone do not supply the board UUID
  required by `ukg_pro.validate_target`; defer to Phase 4 discovery.
- **Imperial Oil:** the exact Imperial category already has result-range/final-
  page evidence. The parent ExxonMobil SuccessFactors feed is not equivalent.

### Runnable candidate registry

`config/target_companies.phase1_ats_candidates.yaml` now contains only:

1. `Cameco` — Jobvite `cameco`.
2. `Lightspeed Commerce` — Greenhouse `lightspeedhq`.
3. `Intuitive Surgical` — SmartRecruiters `intuitive`.

Each is independently addressable by `--company`. Normal execution still
loads `config/target_companies.yaml`; the candidate registry is explicit only.
Rollback snapshots for Cameco and Intuitive Surgical were added without
changing any production source.

### Manual canary commands

Run from the repository root. These are live commands for the operator; Codex
did not run them.

```powershell
$candidateRegistry = (Resolve-Path .\config\target_companies.phase1_ats_candidates.yaml).Path

# Two expected requests (official identity + counted tenant page), plus one margin.
.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $candidateRegistry --company "Cameco" --request-budget 3 --run

# One collection request + at most 20 bounded details + two margin.
.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $candidateRegistry --company "Lightspeed Commerce" `
  --request-budget 23 --run

# Discovery deliberately stops after the listing response. Its budget stop is
# incomplete evidence; total_available is used only to price the real canary.
$intuitiveDiscovery = @(
  .\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
    --registry $candidateRegistry --company "Intuitive Surgical" `
    --request-budget 1 --run | ConvertFrom-Json
)[0]
if ($intuitiveDiscovery.canary_outcome -eq 'PASS_COMPLETE') {
  $intuitiveDiscovery
} else {
  if ($intuitiveDiscovery.stop_reason -ne 'canary_budget_reached' -or
      $null -eq $intuitiveDiscovery.total_available) {
    throw 'Intuitive discovery did not return a trustworthy totalFound.'
  }
  $intuitiveTotal = [int]$intuitiveDiscovery.total_available
  $intuitiveBudget = $intuitiveTotal + [math]::Ceiling($intuitiveTotal / 100) + 2
  if ($intuitiveBudget -gt 52) {
    throw "Intuitive requires $intuitiveBudget requests; defer to Phase 3."
  }
  .\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
    --registry $candidateRegistry --company "Intuitive Surgical" `
    --request-budget $intuitiveBudget --run
}
```

### Current-source parity commands and methods

The bounded canary currently reports integrity/count metadata, not complete
job payloads. Save these current-source results and compare counts plus the
source pages' stable IDs, titles, locations, countries, departments/business
units, and student/intern titles against each future candidate result. Exact
raw count equality is not required where the old source is known to cap at 20.

```powershell
$productionRegistry = (Resolve-Path .\config\target_companies.yaml).Path

.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $productionRegistry `
  --company "General Motors" --company "Caterpillar" `
  --company "Lightspeed Commerce" --company "Applied Materials" `
  --company "BlackRock" --company "Zalando" `
  --request-budget 3 --run | Tee-Object .\output\phase1-current-six.json

.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $productionRegistry --company "Chevron" `
  --request-budget 3 --run | Tee-Object .\output\phase1-current-chevron.json

.\.venv\Scripts\python.exe .\scripts\validate_source_integrity_canaries.py `
  --registry $productionRegistry --company "Intuitive Surgical" `
  --request-budget 2 --run | Tee-Object .\output\phase1-current-intuitive.json
```

Candidate-side parity for General Motors, Caterpillar, Applied Materials,
BlackRock, Chevron, and Zalando must wait for Phase 4 identity support; issuing
those Workday calls now cannot produce an activation-eligible result.

### Acceptance gates

All runnable candidates must pass the integrity gate: `PASS_COMPLETE`,
`status=ok`, `scan_complete=true`, healthy source, validated identity,
provider-native terminal evidence, no safety/budget stop, no pagination
conflict or mutation, no rejected rows, and unique stable IDs/URLs.

Scope/parity gates are independent:

- General Motors — core GM parity; subsidiaries remain separate.
- Caterpillar — corporate parity; Progress Rail remains separate.
- Lightspeed Commerce — preserve Canada and global roles; explain France/Ashby
  overlap by stable IDs and geography.
- Applied Materials — establish Workday/Eightfold relationship, region parity,
  and Eightfold organization identity without guessing.
- BlackRock — professional plus early-career/student coverage must not shrink.
- Chevron — professional and university inventories remain distinct; preserve
  student roles and keep Chevron Stations separate.
- Zalando — preserve first-party geography and stable IDs; totals above 500 are
  incomplete and Phase 4-blocked.
- Intuitive Surgical — same global scope and locations; if the calculated
  SmartRecruiters budget exceeds 52 or a detail request fails, defer to Phase 3.

### Deferred and rejected disposition

- **Phase 3:** ServiceNow remains deferred; Intuitive Surgical also moves here
  if discovery shows a high-volume detail workload or a detail timeout.
- **Phase 4:** General Motors, Caterpillar, Applied Materials, BlackRock,
  Chevron, Zalando, Goldman Sachs GraphQL, Unitree API, MediaTek API, and
  Yaskawa UKG identity. Workday provider-cap cases also stay here.
- **Phase 5:** distinct GM subsidiaries, Progress Rail, Lightspeed France or
  Ashby if non-overlapping, BlackRock early careers, Chevron University/Stations,
  Coupang operational boards, and Stantec global inventory.
- **No migration:** Continental, Snap, Imperial Oil, Aurora Innovation,
  Optiver, Bolt, Roblox, Deliveroo, Etsy, and Sanctuary AI retain production.
- **Phase 1 evidence needed:** Coupang exact Greenhouse custom URL contract;
  Lightspeed and the two new runnable candidates need manual results.

### Files and verification for this pass

- `config/target_companies.phase1_ats_candidates.yaml` — removed seven
  impossible-to-pass Workday configurations; retained Lightspeed; added
  Cameco and Intuitive Surgical.
- `config/target_companies.phase1_ats_rollback.yaml` — added exact production
  snapshots for Cameco and Intuitive Surgical.
- `tests/test_phase1_ats_candidates.py` — updated the exact registry/rollback
  sets, provider validation, non-runnable exclusion, and per-company isolation.
- `docs/PHASE1_ATS_MIGRATION_REVIEW.md` — added this authoritative batch review.

The current production registry still contains 48 generic/fragile sources.
This batch reviewed 18 of them, leaving 30 not covered by this batch's migration
analysis. Another approximately twenty-company Phase 1 screening batch is
therefore warranted, but only companies with concrete better-source evidence
should be selected. Phase 1 can close after all runnable candidates have a
manual integrity result and a scope decision, every failure has a rollback or
later-phase disposition, and no remaining high-value immediate migration is
left unclassified. Phase 2 has not started.

## Next Phase 1 screening batch (2026-10-07)

This offline-only pass closes the three decisions returned from the preceding
manual canaries and reviews the remaining 22 companies that were neither in
the prior 18-company batch nor already conclusively classified elsewhere. No
live source was contacted, no production mapping was changed, and no Phase 2
work was started.

### Previous-batch closure

- **Cameco:** reject Jobvite "cameco". The official careers page did not bind
  the configured tenant, so source identity is unproven. Preserve the failed
  canary and retain production "static_html".
- **Lightspeed Commerce:** reject Greenhouse "lightspeedhq". The board returned
  HTTP 404. The production source enumerated 108 jobs but still reported
  unproven source identity, so it is retained without calling it authoritative.
- **Intuitive Surgical:** defer SmartRecruiters "intuitive" to Phase 3. The
  bounded discovery result prices the current listing-plus-detail execution at
  exactly 772 requests. Do not execute that canary before inventory/hydration
  separation and per-item unreadable outcomes exist.

All three were removed from the runnable registry. Their production mappings
and rollback evidence remain unchanged.

### New-batch summary

- Companies reviewed: 22.
- Class A/B runnable candidates: 0.
- Phase 3 deferrals: 0 new (Intuitive Surgical is the closed prior-batch
  Phase 3 deferral).
- Phase 4 deferrals: 7.
- Phase 5 additions: 2 company-level classifications, plus distinct Siemens
  Healthineers and Coupang boards recorded below.
- Rejected/no-better-source replacements: 13.
- Production activations: 0.

### Full new-batch table

| Company | Current source | Candidate/better source | Class | Runnable? | Main reason |
|---|---|---|---|---:|---|
| Bluesky | first-party "next_data", rank 246 | None concrete | D | No | Exact embedded jobs collection already yields Gem apply URLs; local “Bluesky” Greenhouse rows are different employers. |
| Siemens | "static_html" Avature portal, rank 91 | Avature tenant "jobs.siemens.com", board "144", site "en_US/externaljobs" | C / Phase 4 | No | Historical "avature_folder_portal" mapping is unsupported and current Avature identity/terminal semantics cannot make it activation-eligible. |
| IMC Trading | attributed first-party Next flight collection | None stronger | D | No | Current exact first-party collection is structured, bounded to 1,000, and stronger than generic replacement evidence. |
| Valve | same-origin "public_json" | None stronger | D | No | Current official JSON endpoint has a reviewed atomic collection contract and stable IDs. |
| LINE | first-party embedded JSON endpoint, rank 237 | None stronger | D | No | Current payload exposes "totalCount", stable Strapi IDs, and public/published attribution. |
| Tourmaline Oil | official "next_data", rank 181 | None stronger | D | No | Current exact first-party Prismic-backed collection is already structured and identity-bound. |
| Nutrien | browser-rendered North America search | SAP SuccessFactors RMK on "jobs.nutrien.com" | C / Phase 4 | No | Provider family is concrete, but no offline evidence chooses a supported RMK variant or endpoint/terminal contract. |
| YYC Calgary Airport Authority | exact "yyc.careers" static links | Dayforce-backed detail lifecycle | C / Phase 4 | No | Local evidence names Dayforce but does not establish the exact public client/tenant contract required by the adapter. |
| TEKTELIC Communications | official unpaginated "static_html" | None stronger | D | No | Official careers page links the exact single-page view-all vacancies collection. |
| Espressif Systems | official "static_html" cards | None concrete | D | No | No local ATS/structured mapping is stronger than the exact first-party job-search page. |
| Dyson | same-origin paginated "public_json" | None stronger | D | No | Current reviewed API proves identity and enumerates against the provider total within explicit bounds. |
| Mitsubishi Electric | exact US HRMDirect collection, rank 97 | Other regional Mitsubishi boards | E / Phase 5 | No | Other regions/subsidiaries are additions, not a scope-equivalent replacement for the US board. |
| Computer Modelling Group (CMG) | official single-page "static_html" | None concrete | D | No | Exact company-owned collection has stable post IDs and no pagination state. |
| Symend | exact unpaginated JazzHR board parsed as "static_html" | Native JazzHR contract | C / Phase 4 | No | Job Radar has no JazzHR adapter; the current direct board remains the safe source. |
| ecobee | exact Generac brand page parsed as "static_html" | None stronger | D | No | Current cards carry exact ecobee brand attribution; a parent-wide source would broaden scope. |
| Ouster | exact unpaginated JazzHR board parsed as "static_html" | Native JazzHR contract | C / Phase 4 | No | Job Radar has no JazzHR adapter; keep the exact current board. |
| CATL | attributed China Mokahr bootstrap, rank 134 | SuccessFactors XML global board and separate US site | E / Phase 5 | No | Global and US sources are distinct scope additions, not replacements for the "catlhr" China inventory. |
| DJI | attributed China Mokahr bootstrap, rank 150 | None concrete | D | No | Current rows repeat exact organization "dji"; no safer equivalent-scope mapping was found. |
| Athabasca Oil | company-owned Calgary "static_html" | None concrete | D | No | Retain the current two-row source; terminal completeness remains unproven, but no concrete replacement exists. |
| u-blox | Salesforce "static_html" | first-party Algolia index "open_positions_en-US" | C / Phase 4 | No | Exact API/index evidence exists, but Job Radar has no reviewed Algolia pagination, identity, total, or hydration contract. |
| Zoom | first-party Shazamme "public_json" | None stronger | D | No | Current site UUID, official host, per-job attribution, and atomic collection contract are already exact. |
| H2O.ai | exact unpaginated JazzHR board parsed as "static_html" | Native JazzHR contract | C / Phase 4 | No | Job Radar has no JazzHR adapter; keep the exact current board. |

### Candidate details

There are no A/B candidates in this batch, so there is no Phase 1 request
budget or manual live command to issue. The strongest non-runnable mappings
are preserved for their later phases:

- **Siemens / Avature:** tenant "jobs.siemens.com", board "144", site
  "en_US/externaljobs". Job Radar's historical candidate uses unsupported
  variant "avature_folder_portal"; the same portal exposes six-row
  "folderOffset" pagination in jobseek. Identity and terminal proof must be
  hardened in Phase 4 before any request budget is meaningful.
- **Nutrien / SAP SuccessFactors:** official North America search
  "https://jobs.nutrien.com/North-America/search/". The family and scope are
  concrete, but the local evidence does not prove "rmk_table", "rmk_tile", or
  "rmk_columnized", nor the corresponding endpoint. Phase 4 must discover and
  bind the exact variant.
- **YYC / Dayforce:** the official page delegates to "yyc.careers", and local
  records describe a Dayforce-backed lifecycle. No exact client/tenant key is
  present; guessing it would weaken identity.
- **Symend, Ouster, H2O.ai / JazzHR:** the exact "applytojob.com" boards are
  known and already parsed directly as unpaginated static collections. A
  future native adapter should preserve official-page binding, stable IDs,
  zero-job proof, and single-page terminal evidence.
- **u-blox / Algolia:** jobseek records application
  "L87M8KNOMJ", index "open_positions_en-US", "hitsPerPage=100", result path
  "hits", and detail hydration from official URLs. Phase 4 must model total and
  pagination consistency, index identity, API-key exposure, and per-detail
  failure semantics before this becomes runnable.

Expected terminal evidence for each future candidate must be provider-native:
an authoritative total reached, an explicit final page/"has_more=false", or a
reviewed atomic/single-page contract. A budget stop, a capped page, or a
successful non-empty response is not terminal evidence.

### Deferred and rejected findings

- **Phase 4:** Siemens, Nutrien, YYC, Symend, Ouster, u-blox, and H2O.ai.
  These require protocol/identity support, not a Phase 1 canary.
- **Phase 5:** Mitsubishi Electric regional inventories and CATL global/US
  inventories. Siemens Healthineers China/global/Workday boards are also
  distinct coverage, not Siemens-core replacements.
- **Retain current source:** Bluesky, IMC Trading, Valve, LINE, Tourmaline Oil,
  TEKTELIC Communications, Espressif Systems, Dyson, CMG, ecobee, DJI,
  Athabasca Oil, and Zoom. “Retain” does not upgrade Athabasca's terminal
  completeness; it only records that no safer replacement exists.

Coupang was rechecked. The local row still provides only Greenhouse token
"coupang" at "https://www.coupang.jobs/en/jobs/"; it does not prove an exact
stable-ID custom apply URL template. Coupang remains Phase 1-pending and
non-runnable. Its CFS frontline API and CLS camp/driver/flex boards remain
distinct Phase 5 additions.

### Candidate and rollback registries

"config/target_companies.phase1_ats_candidates.yaml" is now intentionally
empty. There are no exact runnable entries remaining after the failed Cameco
and Lightspeed canaries and Intuitive's Phase 3 deferral.

The rollback registry retains every earlier snapshot and adds exact current
production snapshots for Siemens, Nutrien, YYC, Symend, Ouster, u-blox, and
H2O.ai. These additions preserve rollback evidence for the concrete
later-phase provider families without activating them.

### Manual commands and acceptance criteria

There are no runnable Phase 1 candidates, so no live command should be run
from the candidate registry. In particular, do not rerun Cameco or Lightspeed,
do not execute Intuitive's 772-request workload, and do not use protocol
families above as ad hoc candidates.

If a later phase makes one of the seven protocol candidates runnable, it must
pass the common integrity gate: "PASS_COMPLETE", "status=ok",
"scan_complete=true", healthy source, validated identity, unique stable IDs
and canonical URLs, zero rejected records, no mutation/pagination conflict,
no safety stop, and provider-native terminal evidence. Scope gates are:

- Siemens must match the core Siemens portal without absorbing Healthineers or
  the Czech AlmaCareer board.
- Nutrien must preserve the North America scope represented by production.
- YYC must bind the exact Calgary Airport Authority tenant/client.
- JazzHR candidates must match the same exact employer board and prove a
  legitimate empty collection when applicable.
- u-blox must match the global Salesforce inventory by stable IDs and
  geography, with detail failures represented without absence authority.

Any unproven identity, unsupported redirect, contradictory total, repeated
page, capped/incomplete collection, or scope loss forces Phase 4 retention or
rejection rather than activation.

### Phase 1 progress

Combining the prior 18-company batch, the eight generic/fragile targets that
already had conclusive earlier dispositions, and these 22 reviews accounts for
all 48 original generic/fragile production targets. Approximately zero remain
unscreened, so another approximately 20-company Phase 1 screening batch is not
needed.

Phase 1 has no runnable manual canary left. The next source work is evidence
review rather than execution: keep Coupang pending until an exact custom
Greenhouse apply URL is observed; carry Intuitive Surgical and ServiceNow to
Phase 3; carry the protocol cases above and the established Workday cases to
Phase 4; and treat the recorded distinct boards as Phase 5 additions. Do not
start Phase 2 from this record.
