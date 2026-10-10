# Phase 4 Batch 3: ADP and browser-rendered protocol review

## Scope and ownership

This batch hardens only the existing ADP and `browser_rendered_html` families.
It made no live request, ran no canary, changed no activation, added no provider,
and did not begin Phase 5.

| Concern | ADP owner | Browser owner |
|---|---|---|
| navigation/requests | ADP variant adapter | browser collector |
| source identity | ADP bootstrap plus exact configured tenant/site | browser collector final URL plus rendered employer marker |
| pagination | ADP variant adapter | delegated static HTML adapter |
| terminal evidence | ADP provider total/offset contract | delegated static HTML adapter |
| absence authority | central `finalize_source_scan` | central `finalize_source_scan` |

The scanner consumes the adapter result. It does not promote ordinary browser
output to complete or infer terminal state from an empty DOM.

## ADP variant matrix

| Variant | Search | Detail | Identity | Pagination | Disposition |
|---|---|---|---|---|---|
| `workforce_now_requisitions_v1` | tenant-bound public requisitions with `cid`, `ccId`, locale, stable `itemID`, title and locations | required to hydrate description and corroborate item/title/external apply ID; transient failure becomes unreadable inventory | exact configured host/cid/ccId plus career-center bootstrap matching reviewed `identity_regex` | `$skip`/`$top`, exact `startSequence`, stable total, stable page IDs | supported; detail-coupled hydration, listing-independent inventory |
| `myjobs_requisitions_v1` | tokenized public filter endpoint with stable `reqId` and complete listing data | no separate detail request | active external career-site config must exactly match slug, `orgoid`, and client identity | `$skip`/`$top`, constant `count`, stable page IDs | supported; safe partial progress |
| other ADP surfaces | not modeled | not modeled | not modeled | not modeled | `UNSUPPORTED_VARIANT` |

### Workforce Now coupling

The listing proves a tenant-namespaced `itemID` exists and is sufficient to
protect an existing lifecycle row. No placeholder job is fabricated. Detail
remains coupled only for emitting a hydrated `JobRecord`: it must repeat the
item ID, title, external job ID, and locations before the adapter builds the
official tenant/career-center apply URL. A timeout, connection failure, 429, or
selected 5xx detail response records an unreadable item. A deterministic
detail mismatch fails closed.

### MyJobs partial progress

The bootstrap must prove active external site, exact slug, organization, client
identity, and a public token. Each listing row must repeat the exact site domain
and expose `canApply=true`. After that identity is validated, an independently
valid earlier page may be returned when a later page has a transient transport
failure. The result is incomplete and never authoritative for absence.

### ADP pagination, mutation, zero, and budget

Both variants require provider totals and offsets; missing/malformed totals,
early empty pages, repeated pages/IDs, and total changes cannot prove
completion. Exact zero is authoritative only after the runtime identity
bootstrap and a structurally exact zero envelope. A working endpoint or an
employer-like string in job text is not identity.

Identity, listing, detail, and a whole-traversal retry share
`protocol_request_budget`. Exhaustion is a safety stop. Observable mutation
gets one restart using the remaining same budget. First-traversal records are
discarded, never unioned; a second mutation remains incomplete.

### ADP production audit

No activation was changed. Repository-only dispositions are intentionally
strict: a supported endpoint without first-party evidence remains
identity-unproven.

| Disposition | Count | Sources |
|---|---:|---|
| `SAFE_SUPPORTED` | 0 | none |
| `COUPLED_SUPPORTED` | 3 | Beckhoff Automation; Vector Institute; Diodes Incorporated |
| `IDENTITY_UNPROVEN` | 5 | Astrobotic; Intuitive Machines; FANUC; Paramount Resources; Novarc Technologies |
| `UNSUPPORTED_VARIANT` | 0 | none |
| `CONFIG_INVALID` | 0 | none |
| `NEEDS_MANUAL_CANARY` | 0 | none (candidates are listed separately below) |

The three coupled sources have repository first-party evidence and supported
Workforce Now configuration but still require future bounded runtime
validation. MyJobs is protocol-safe; FANUC remains identity-unproven because
the repository lacks an independently reviewed first-party employer binding.

## Browser identity and redirects

Every navigation must finish on HTTPS and on the configured listing host or an
exact host in the reviewed source/apply allowlists. Same-host canonical
redirects and exact reviewed recruiting-host redirects are accepted. An
unexpected third-party host, login/session/challenge route, generic landing
redirect from a specific careers route, access block, error page, or retired
route fails identity and can never become authoritative zero. Rendered employer
identity nodes must all match the configured regex; mixed identity fails.

## Browser terminal evidence

Accepted runtime terminal signals are:

- disabled provider next/load-more control;
- explicit current-page equals final-page evidence;
- provider-reported total exactly reconciled to stable unique inventory;
- absence of the reviewed next control after pagination was observed;
- a reviewed deterministic single-page contract on the validated surface;
- a complete structured collection response.

No cards, a timeout, selector failure, JavaScript failure, click/navigation
failure, or action-budget exhaustion is not terminal evidence. Current
production has no configured infinite-scroll source. Configured scroll actions
without another terminal signal remain incomplete; generic infinite-scroll
support is intentionally not invented.

### Browser stable identity, partial progress, and mutation

Stable identity is supplied by the delegated static contract: provider ID,
canonical job URL, or reviewed stable attribute/regex. Title plus location is
not an identity contract. Equivalent duplicates do not increase inventory;
conflicting stable IDs or canonical URLs fail closed.

Partial progress is allowed only after final-URL and employer identity
validation, at least one completed page of stable jobs, and a later transient
navigation/transport failure. Structural selector/identity/redirect failures,
pagination contradictions, and budgets do not qualify. Repeated page/ID or
changed total marks mutation. One fresh context/traversal may replace it; the
first is discarded and a second mutation remains incomplete.

### Browser production audit

| Disposition | Count | Sources |
|---|---:|---|
| `SAFE_SUPPORTED` | 1 | MediaTek |
| `IDENTITY_UNPROVEN` | 0 | none |
| `TERMINAL_UNPROVEN` | 3 | Goldman Sachs; Nutrien; Stantec |
| `UNSUPPORTED_NAVIGATION` | 0 | none |
| `ACCESS_BLOCKED` | 0 | none |
| `NEEDS_MANUAL_CANARY` | 0 | none (MediaTek is prepared below) |

Goldman Sachs, Nutrien, and Stantec have reviewed host/identity configuration
but no deterministic production terminal contract, so positive discoveries
remain useful and absence remains non-authoritative.

## Failure and cooldown policy

Cooldown-eligible failures are timeouts, connection/request errors, HTTP 429,
and selected 5xx responses. Wrong tenant, identity unproven/mismatch,
unsupported variant/navigation, login/access block, malformed pagination,
stable-ID conflict, selector/contract failure, mutation exhaustion, and every
safety/request/action limit are non-transient.

## Source fingerprints

ADP fingerprints include variant, tenant/site identifiers embedded in the
source URL, source and endpoint URLs, organization/slug where applicable, and
identity evidence. Browser fingerprints include listing URL, allowed hosts,
navigation/static variant, identity selector/regex, reviewed pagination and
single-page contract. Operational budgets are excluded.

## Opt-in canaries

`config/target_companies.phase4_batch3_canaries.yaml` is never loaded by normal
runtime. No canary was executed.

| Candidate | Budget | Basis |
|---|---:|---|
| Beckhoff Automation | 46 | two traversals of identity + 2 pages + 20 details |
| Vector Institute | 44 | two traversals of identity + 1 page + 20 details |
| Diodes Incorporated | 46 | two traversals of identity + 2 pages + 20 details |
| MediaTek | 2 | one navigation/page load + one bounded retry allowance |

## Explicit handoffs

Unmodeled ADP variants, browser click workflows, challenge/login flows, and
generic infinite scroll are explicit unsupported or later-infrastructure
dispositions. Adding companies on already hardened contracts belongs to Phase
5 coverage expansion; it is not unfinished Phase 4 correctness.
