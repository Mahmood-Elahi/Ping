# Phase 4 Batch 2: Dayforce and SAP SuccessFactors review

## Scope and safety

This batch hardens only the existing Dayforce and SAP SuccessFactors adapter
families. It adds no provider, performs no live request, changes no active
source disposition, and does not begin ADP Workforce Now, browser-navigation
redesign, or Phase 5 coverage expansion.

The active registry was audited from repository configuration only. Reference
repositories were not modified. The canary manifest is opt-in and is not read
by the normal registry loader.

## Dayforce protocol

```text
reviewed first-party identity config
              |
Candidate Portal shell (exact locale/tenant/board)
              |-- __NEXT_DATA__.query route binding
              `-- one site-info tenant/board/locale/numeric board ID
              |
same-session /api/auth/csrf
              |-- one bounded token
              |
POST /api/geo/{tenant}/jobposting/search
              |-- fixed page size 25 and echoed offset/count
              |-- stable posting ID + stable requisition ID
              |-- constant raw maxCount
              `-- optional identity-checked detail shell for location
```

### Variant matrix

| Variant | Bootstrap | Search | Detail | Identity | Status |
|---|---|---|---|---|---|
| `nextjs_jobposting_search` | Candidate Portal `__NEXT_DATA__` plus same-session NextAuth CSRF | JSON POST with client namespace, board, locale, and offset | Candidate Portal `/jobs/{postingId}` only when structured location is absent | Reviewed first-party URL exactly bound by `dayforce_identity` to tenant and board; shell revalidates route/site-info | Supported |
| No-CSRF Candidate Portal | None evidenced in current code/config/fixtures | Not modeled | Not modeled | Not modeled | Unsupported |
| Alternate API/search/detail routes | Not evidenced | Not modeled | Not modeled | Not modeled | Unsupported |

### Identity versus completion

The shell proves that the request stayed inside the configured Dayforce tenant,
board, and locale. It does **not** prove that the board belongs to the intended
employer. Employer proof is a reviewed `dayforce_identity` block containing:

- `official_source_url`: first-party HTTPS, not Dayforce-hosted;
- `expected_tenant`: exact configured tenant; and
- `expected_board`: exact configured board.

Missing evidence produces `source_identity_status=unproven`; contradictory
evidence produces identity failure. Jobs may still be returned for positive
review, but neither state can authorize absence. Job descriptions, tenant-name
similarity, and a working search endpoint are never identity evidence.

Completion is separate. It requires a valid bootstrap/token/search contract,
every raw posting through the constant `maxCount`, echoed offset/count, no
repeated page or posting ID, and terminal raw accounting equal to `maxCount`.
The page/job/request ceilings are not terminal evidence.

### Listing, detail, and accounting

The listing establishes existence through the namespaced requisition ID.
Detail is requested only when the non-virtual listing has no structured
location. Its Next.js payload must match posting/requisition/canonical route,
tenant, board, and title before location data is used.

Accounting fields are intentionally distinct:

| Observation | Meaning |
|---|---|
| `raw_source_postings` | Every provider row enumerated, including duplicate requisitions and rejected rows |
| `duplicate_requisitions_skipped` | A new posting ID repeated an already listed stable requisition |
| rejected item | Detail was retrieved and identity-validated, but deterministic normalization could not produce a record |
| unreadable item | Listing proved the requisition but transient detail transport/HTTP prevented hydration |
| normalized job | A valid `JobRecord` emitted to consumers |

The known fixture reconciles exactly as 68 raw postings = 66 normalized + 1
duplicate requisition + 1 rejected posting. It yields 67 unique inventory
items and `terminal_accounted_items=68`; there is no unexplained loss.

### Partial progress, mutation, and budget

A later search failure returns incomplete partial progress only when employer
identity is already validated, at least one earlier page is independently
valid, and the failure is a timeout, connection error, 429, or selected 5xx.
It sets `inventory_complete=false`, grants no absence authority, and retains
earlier jobs/inventory. Invalid CSRF, identity failure, repeated pages/IDs,
malformed JSON, contradictory pagination, and safety limits do not become
partial success.

A changed total marks the whole traversal mutated. Exactly one new traversal is
started with the same source-wide request budget; first-traversal jobs are
discarded and never unioned. A second mutation remains incomplete. Shell, CSRF,
search, detail, and retry categories share `protocol_request_budget`.

## SAP SuccessFactors protocol

```text
configured official/recruiting surface + reviewed identity_regex
              |
       exact RMK /search/ surface
        /             |                 \
  table HTML       tile HTML       columnized client
 headers+rows    tiles+fragment     marker+CSRF token
        |             |                 |
 native next URL  tile startrow     jobs JSON pageNumber
        |             |                 |
 listing record   listing record    stable listed ID
                                        |
                                  canonical detail/apply
```

### Variant matrix

| Variant | Listing | Pagination | Detail | Identity | Status |
|---|---|---|---|---|---|
| `rmk_table` | `#searchresults`, header-bound rows, stable same-host `/job/.../{id}/` URL | native next link with exact `startrow`; result range/total | none | reviewed `identity_regex` on exact RMK surface | Supported |
| `rmk_tile` | `#job-tile-list`, labeled section fields, stable same-host job URL | reviewed `tile-search-results` endpoint and `startrow` | none | reviewed `identity_regex` on exact RMK surface | Supported |
| `rmk_columnized` | named `jobSearchResult[].response` fields and numeric ID | RMK page 0, then 2..N; total/short locale terminal | canonical detail supplies tenant recheck, title, requisition/apply and location enrichment | surface marker + standard client + one CSRF token; detail identity retained | Supported |
| Other SuccessFactors/RMK surfaces | Not evidenced by current adapter/config | Not modeled | Not modeled | Not modeled | Unsupported |

### Column contract

Table values are bound using each `td[headers]` reference to a unique `th[id]`.
Column order and localized labels may change without shifting values. The parser
fails closed on missing/duplicate header IDs, duplicate labels, unknown or
duplicate cell attribution, row/header length mismatch, a missing title link,
or an invalid stable same-host job URL. Optional recognized fields may be
absent; values are never assigned by positional guessing.

Columnized JSON uses explicit named fields rather than positional indexes.
Required ID, title, URL title, and locale fields are validated before detail.
Location fields accept only their reviewed structured/scalar forms.

### Identity, detail, and partial progress

An RMK response or shared/recruiting hostname alone is not employer proof.
HTML variants require the configured exact surface to match a reviewed
`identity_regex`. Columnized additionally requires the standard client markers
and one CSRF token. Canonical detail, title, requisition (when present), tenant
marker, and job-specific apply initialization remain strict item contracts.

Table and tile listings already contain the stable normalized record and do
not require detail. A columnized listing establishes stable existence. If its
detail request fails transiently, that ID is returned as unreadable inventory
without a placeholder job; deterministic detail identity/apply/schema failures
still fail the source.

A later listing request may return incomplete partial progress only after
identity and earlier pages are valid and no contradiction exists. Repeated
pages/IDs, changed totals, malformed controls/schema, missing native
continuation, identity failures, and limits do not qualify. Mutation gets at
most one whole-traversal restart; the first traversal is discarded and a
second mutation remains incomplete.

## Failure and cooldown policy

The shared health vocabulary remains small: identity failure/unproven,
malformed collection/records, pagination loop/contradiction, collection
changed, safety limit, incomplete inventory, and hydration degraded. Adapter
diagnostics carry provider-specific detail such as `bootstrap_invalid`,
`protocol_contract_invalid`, or provider request-budget exhaustion.

Cooldown-eligible failures are timeout/connection failures, HTTP 429, and
selected HTTP 5xx responses. Identity-unproven/failure, bad CSRF, unsupported
variant, malformed columns/JSON, repeated pagination, contradictory totals,
mutation exhaustion, and all safety limits are non-transient and do not advance
the cooldown streak.

## Source/config fingerprint

Dayforce tenant, board, locale, endpoints, variant, and complete reviewed
identity block affect the fingerprint. SuccessFactors careers/search/endpoint
hosts, locale, variant, and `identity_regex` affect it. The operational
`protocol_request_budget` does not. Reconfiguration therefore cannot inherit a
stale cooldown or identity assumption, while ordinary budget tuning can.

## Production configuration audit

No active entry was enabled or disabled in this batch.

| Provider | SAFE_SUPPORTED | IDENTITY_UNPROVEN | UNSUPPORTED_VARIANT | CONFIG_INVALID | NEEDS_MANUAL_CANARY | Total |
|---|---:|---:|---:|---:|---:|---:|
| Dayforce | 0 | 5 | 0 | 0 | 0 | 5 |
| SAP SuccessFactors | 0 | 31 | 0 | 0 | 13 | 44 |

All five Dayforce entries use the supported protocol but lack a reviewed
`dayforce_identity` block. The 31 identity-unproven SuccessFactors entries use
supported RMK variants but lack a reviewed marker. Thirteen entries have exact
configuration plus deterministic identity markers and therefore qualify only
for a future bounded manual canary; a deterministic fixture is not recovery.

- Dayforce `IDENTITY_UNPROVEN`: WestJet; SMART Technologies; Ross Video;
  Payments Canada; Chemelex.
- SuccessFactors `IDENTITY_UNPROVEN`: Lam Research; KUKA; Festo; Hyundai;
  Volkswagen Group; Volvo Cars; Ferrari; ZF Friedrichshafen; Halliburton; John
  Deere; Pembina Pipeline; Teradyne; Omron; Keyence; Spartan Controls;
  TransAlta; ARC Resources; Precision Drilling; ExxonMobil; Celestica;
  Descartes Systems Group; TELUS; ATS Corporation; Scotiabank; Hatch; PCL
  Construction; SaskPower; FortisBC; VIA Rail; Canada Life; Aecon.
- SuccessFactors `NEEDS_MANUAL_CANARY`: Gibson Energy; NOVA Chemicals; Trans
  Mountain; CPKC; Hydro One; Enerflex; Bombardier; SAP; Qorvo; CN Rail; Rogers
  Communications; Seagate; Kinross Gold.

## Opt-in canary preparation

`config/target_companies.phase4_batch2_canaries.yaml` is not loaded by normal
runtime. No canary was executed. Prepared SuccessFactors candidates and hard
budgets are:

| Candidate | Variant | Budget |
|---|---|---:|
| Gibson Energy, NOVA Chemicals, Hydro One, SAP, Qorvo, Rogers Communications | table | 4 each |
| Trans Mountain, Enerflex | tile | 4 each |
| CPKC, Bombardier | columnized | 88 each |
| Seagate, Kinross Gold | columnized | 86 each |
| CN Rail | columnized | 2,044 |

No Dayforce canary is prepared because repository configuration does not yet
contain reviewed first-party tenant/board evidence.

## Explicit unsupported variants and remaining work

- Dayforce without the evidenced Next.js shell + NextAuth CSRF contract.
- Dayforce alternate search/detail/API versions or speculative no-CSRF flows.
- SuccessFactors surfaces other than the three configured RMK contracts.
- Positional/unlabeled SuccessFactors tables and unreviewed pagination controls.

After this batch, remaining Phase 4 work is primarily ADP Workforce Now and
browser navigation/terminal evidence. Neither is started here.
