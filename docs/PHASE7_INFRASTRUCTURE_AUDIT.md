# Phase 7 closes early: the remaining gap is coverage maintenance, not missing infrastructure

Date: 2026-10-10 (America/Edmonton)

## Executive summary

**Decision: `PHASE7_CLOSE_EARLY`. Primary recommendation:
`CLOSE_PHASE7_EARLY`.** The seven-phase Job Radar architecture roadmap is
complete. This does not mean every source is healthy; it means the remaining
problems are source-specific coverage and reliability work rather than a
missing shared infrastructure layer.

- Production remains 533 canonical companies, 533 configured sources, and 533
  source scopes. No production row, platform, scope, or lifecycle behavior was
  changed by this audit.
- The newest checked-in full runtime evidence is
  `jobs/company_careers_coverage_20261001_142550.json`: 472 complete, 47
  incomplete, and 14 failed sources. It is a 2026-10-01 snapshot, not a claim
  of live health on 2026-10-10.
- Later deterministic evidence resolves five rows from that problem set
  (Waymo, Wise, ASML, Dyson, and Box) and separately downgrades Nutrien's older
  zero-job browser result because terminal evidence is unproven. This yields a
  57-source **known maintenance queue**, not a replacement current-scan health
  total.
- Fifty-five of those 57 sources are ordinary adapter, provider-contract,
  identity, completeness, parsing, transient, or unsupported-provider work.
  Only GitHub and Parkland carry credible anti-bot/network-block evidence.
  Each affects one source; neither has deterministic evidence that alternate
  egress would work or preserve identity and completeness.
- No source currently proves a need for new rendering, interactive browser,
  authenticated-provider, or external-inventory infrastructure. The existing
  browser renderer already covers all four production browser sources.

The machine-readable evidence and classifications are in
`config/phase7_infrastructure_audit.yaml`. Run
`python scripts/report_phase7_audit.py` for the reproducible summary.

## Evidence boundary and counts

Current HEAD at audit start was `ef021b4635b036019a36e8770de4ee2ce473576d`
(`Complete Phase 6 capability triage`). The audit used only repository files,
tests, and the three sibling repositories in read-only mode.

| Measure | Repository-backed result | Interpretation |
|---|---:|---|
| Canonical production companies | 533 | Current registry |
| Configured production sources/scopes | 533 / 533 | Current registry; no multi-scope production company yet |
| Newest full-snapshot complete | 472 | Exact only for the 2026-10-01 snapshot |
| Newest full-snapshot incomplete | 47 | Exact only for the 2026-10-01 snapshot |
| Newest full-snapshot failed | 14 | Exact only for the 2026-10-01 snapshot |
| Runtime problem rows classified | 61 | Every incomplete/failed row in that snapshot |
| Later resolved runtime problem rows | 5 | Source-specific later evidence; not a new full scan |
| Later explicit problem finding | 1 | Nutrien browser terminal proof |
| Known maintenance queue | 57 | 61 - 5 + 1; do not treat as a current health denominator |
| Genuine infrastructure-block candidates | 2 | GitHub and Parkland, both network-block only |
| Retired/rejected production sources | 0 | All 533 production sources remain active |
| Rejected Phase 5 candidate rows | 5 | Four unique proposals; Lightspeed France occurs in both candidate registries |

The registry SHA-256 is
`b5e8e42e1047c03131480ea678e2ca42ba3dbf42bd3f6ca1d3e0290decfbff30`.
The runtime evidence SHA-256 is
`fb12864e7d1b84931d56c8a831a51572377f06e9eb6d8d8970b65b76fd5e7a7b`.
Focused tests fail if either audited input changes without a fresh review.

An exact present-day complete/incomplete/failed split cannot be established
without the prohibited live scan. In particular, adding later recoveries to
472 would be unsound because other post-snapshot protocol reviews changed the
standard of proof.

## Source-health taxonomy

- **COMPLETE / healthy:** runtime inventory has accepted terminal evidence and
  can authorize absence. The latest full snapshot recorded 472.
- **INCOMPLETE, recoverable with existing adapter work:** positive inventory
  can be useful, but identity, traversal, normalization, or terminal evidence
  is not authoritative. This covers categories A-E and L below.
- **FAILED transient/provider-side:** a bounded request failed without evidence
  of a persistent infrastructure dependency. Category F.
- **FAILED identity/contract:** wrong/stale binding or a changed provider
  response. Categories B and C.
- **BLOCKED browser rendering / interaction:** categories G and H. None.
- **BLOCKED anti-bot/network:** category I. GitHub and Parkland only.
- **BLOCKED authentication:** category J. None.
- **BLOCKED missing external inventory:** category K. None.
- **BLOCKED unsupported provider:** category L. Siemens, Unitree Robotics, and
  u-blox; this means adapter coverage, not Phase 7 infrastructure.
- **Intentionally retired/rejected:** zero production sources. The four unique
  rejected Phase 5 proposals are Lightspeed France and three Coupang evergreen
  funnels (camp, driver, and Flex Assistant).
- **Insufficient evidence:** category N. None in the classified runtime problem
  set; uncertainty is recorded inside the two network candidates instead of
  inventing proxy effectiveness.

## Exactly-one blocker classification

Counts below use the 57-source known maintenance queue. The parenthetical
number is the count in the 61-row runtime problem snapshot before the five
later source-specific recoveries and Nutrien's later browser finding.

| Code | Primary blocker | Remaining (runtime) | Sources in the known maintenance queue |
|---|---|---:|---|
| A | `ADAPTER_BUG` | 0 (0) | None; observed defects fit narrower contract, pagination, or parsing categories |
| B | `PROVIDER_CONTRACT_CHANGE` | 3 (3) | Google; ARM; Schneider Electric |
| C | `IDENTITY_UNPROVEN` | 4 (4) | Magic Leap; Plaid; Kraken; YYC Calgary Airport Authority |
| D | `PAGINATION_OR_COMPLETENESS` | 41 (44) | Atlassian; Shopify; Meta; JPMorgan Chase; Emerson; Honeywell; Microsoft; Qualcomm; Micron; Eaton; Samsung Electronics; GE Vernova; Rivian; General Motors; Caterpillar; Intuitive Surgical; Coupang; Zalando; Capital One; Goldman Sachs; GlobalFoundries; Oracle; Stantec; Espressif Systems; Bosch; CPKC; Scotiabank; ecobee; Bombardier; Fortinet; ServiceNow; Nordic Semiconductor; CN Rail; Athabasca Oil; HP Inc.; Seagate; SGS; AstraZeneca; Kinross Gold; Cameco; Nutrien |
| E | `PARSING_OR_NORMALIZATION` | 2 (2) | Novarc Technologies; Chemelex |
| F | `RATE_LIMIT_OR_TRANSIENT` | 2 (3) | Etsy; SAP |
| G | `BROWSER_RENDER_REQUIRED` | 0 (0) | None |
| H | `INTERACTIVE_BROWSER_REQUIRED` | 0 (0) | None |
| I | `ANTI_BOT_OR_NETWORK_BLOCK` | 2 (2) | GitHub; Parkland |
| J | `AUTHENTICATED_PROVIDER_ACCESS` | 0 (0) | None |
| K | `EXTERNAL_AUTHORITATIVE_INVENTORY` | 0 (0) | None |
| L | `UNSUPPORTED_PROVIDER` | 3 (3) | Siemens; Unitree Robotics; u-blox |
| M | `LOW_VALUE_OR_NOT_RECOVERABLE` | 0 (0) | None |
| N | `UNKNOWN` | 0 (0) | None |

The five later recovered rows retain their historical primary classifications
for auditability: Waymo (F), Wise (D), ASML (D), Dyson (D), and Box (D).

Categories A-F and L are normal coverage/adapter backlog, **not Phase 7
infrastructure**. Specifically:

- SuccessFactors repeated-page and mutation outcomes (Bombardier and Seagate)
  are completeness failures; SAP's one checked-in HTTP 403 is insufficient to
  establish network discrimination.
- Google's malformed response is a provider-contract failure.
- ARM and Schneider Electric have contradictory iCIMS counts. GitHub is the
  only iCIMS row with explicit block-page evidence.
- Apple's newest full-snapshot result was complete with reported-total
  terminal evidence. Historical mutation handling remains fail-closed, but
  there is no current incomplete/failed Apple row to promote into Phase 7.
- Cameco remains incomplete because the production static source lacks
  terminal proof and the Jobvite replacement failed employer binding.
- Chemelex's later 68-row evidence still contains one malformed record and one
  duplicate, so parsing/normalization—not browser or proxy infrastructure—is
  the blocker.

## Browser-rendered production sources

The current renderer is already a bounded Playwright implementation. It uses
a fresh context and page per source, lazy-loads Chromium, blocks image/font/
media resources, enforces approved final hosts, caps navigation at two pages,
results at 20, rendered HTML at 3 MB, timeout at 30 seconds, and declarative
scrolling at three steps. It injects no JavaScript, stores no browser state,
and delegates job identity and terminal proof to the static adapter. Browser
failure remains incomplete.

| Company | Scope | Current implementation | Terminal evidence | Verdict |
|---|---|---|---|---|
| Goldman Sachs | `default` | `browser_rendered_html_v1`, render plus static extraction | Unproven | D: positive discovery works; expanding rendering does not prove exhaustion |
| MediaTek | `default` | Same | Reviewed single-page collection | Complete/healthy; renderer is sufficient |
| Nutrien | `default` | Same | Later Phase 4 review says unproven | D: older zero-job runtime completion is not retained as current authority |
| Stantec | `default` | Same | Unproven | D: current Calgary query yields jobs but no authoritative terminal state |

There is no production interactive-browser target, configured click workflow,
stateful-cookie requirement, or browser-only XHR contract. A larger browser
worker would add operational surface without fixing the missing per-source
terminal contracts. Goldman Sachs's first-party GraphQL lead, Stantec's global
sitemap, and Nutrien's SuccessFactors lead remain adapter/scope work: GraphQL
has no reviewed terminal contract, the sitemap is not scope-equivalent or
authoritative for timely absence, and the SuccessFactors variant is not bound.

**Browser verdict: do not expand or build browser infrastructure.** If a future
candidate proves a browser-only inventory, reuse the current fail-closed
contract first. Only then consider a one-shot isolated worker with approved
hosts and redirect enforcement, hard navigation/time/body/action limits,
fresh non-credentialed contexts, deterministic extraction, explicit terminal
evidence, mutation restart, crash-as-incomplete behavior, and bounded debug
artifacts.

## Proxy and network-egress audit

| Candidate | Evidence | Alternate egress | Correctness and compliance impact | Decision |
|---|---|---|---|---|
| GitHub | iCIMS returned an explicit blocked response after 30 jobs and bounded adapter progress | Plausible but unproven; no evidence distinguishes datacenter, region, session, or rate behavior | A proxy cannot make the partial inventory authoritative; provider identity and final-page proof still apply | Remain incomplete; no proxy canary |
| Parkland | TalentBrew/static retrieval returned HTTP 403; repository candidate evidence identifies Cloudflare | Plausible but unproven; residential/mobile need is unknown | Challenge handling and proxy origin would add vendor, security, compliance, and repeatability risk without proving inventory | Remain incomplete; no proxy canary |

Normal retries and cooldowns already handle transient requests. They cannot
safely reinterpret a challenge page or partial collection as success. That
proves these two are credible network-block candidates, but it does **not**
prove proxy effectiveness. No repository evidence supports regional blocking,
TLS-only blocking, a safe alternate first-party endpoint, or a residential/
mobile requirement.

For both category-I sources, existing capabilities are insufficient for a
specific, bounded reason:

- Static HTTP and the provider adapters already reached the configured public
  sources and detected the blocked response; changing parsers cannot turn the
  blocked/partial body into an inventory.
- The current browser renderer is intentionally not a challenge solver and
  rejects login, CAPTCHA, session, consent, and block pages. Rendering the same
  response would therefore remain incomplete.
- Bounded retries and cooldowns reduce load and allow later recovery; they do
  not establish that another IP, region, TLS fingerprint, or session would be
  accepted.
- Neither source produced explicit final-page or authoritative-total evidence,
  so no absence or removal authority exists. Scope-aware lifecycle and source
  health correctly contain the failure but cannot supply missing inventory.

No G, H, J, or K source exists, so there is no corresponding proof burden to
paper over with a speculative architecture.

**Proxy verdict: do not add alternate egress.** Revisit only after repeatable,
source-specific failures from at least three production sources and a bounded
test showing that alternate egress preserves exact identity, stable IDs,
complete traversal, and terminal evidence.

## External inventory and authentication

No current candidate satisfies all four external-inventory requirements:
first-party authority, exact employer identity, bounded completeness, and a
stable terminal contract.

- Stantec's sitemap is a discovery hint for a broader global scope, not
  authoritative Calgary absence.
- Goldman Sachs GraphQL, Unitree's API, and u-blox's Algolia index are concrete
  first-party protocol leads. Implementing their exact bounded contracts is
  ordinary adapter work (B/L), not an external inventory layer.
- Cameco's Jobvite lead failed exact employer binding.
- Sibling board catalogs and third-party aggregators may guide discovery but
  cannot authorize absence.

No remaining production source deterministically requires an API key, private
ATS tenant, account login, recruiter integration, or organization-owned
credential. Login/session/challenge pages continue to fail closed. If a future
provider genuinely requires organization credentials, it should remain
unsupported until credentials are explicitly supplied and isolated; a public
first-party source remains preferable.

**External inventory verdict: no layer. Authentication verdict: no integration.**

## Sibling-repository findings

The sibling repositories were inspected read-only.

| Repository pattern | Classification | Phase 7 judgment |
|---|---|---|
| jobseek's one-shot Lightpanda runtime, typed result, strict egress deny policy, fresh process, byte/time limits, environment scrubbing, and cleanup proof | `REUSE_CONTRACT` | Strong safety contract if a future browser-only candidate exists; do not copy the dormant Go/Lightpanda service now |
| jobseek's bounded API capture/replay and network discovery | `ADAPT` | Useful future discovery pattern only after a named source proves browser-only XHR; captured APIs still need Job Radar identity, pagination, mutation, and terminal gates |
| jobseek's Dayforce session/cookie pilot | `CONCEPT_ONLY` | Demonstrates a bounded session concept, but current Chemelex evidence is parsing-related and other Dayforce rows lack identity evidence |
| jobseek's pool/service deployment and density work | `CONCEPT_ONLY` | Operational isolation is thoughtful but disproportionate for zero browser-worker candidates |
| freehire's browser-driven application/autofill and CAPTCHA refusal | `REJECT` | It is an application workflow, not authoritative job-inventory enumeration; user/session and interaction semantics do not transfer |
| freehire/jobseek stored board catalogs and cached inventories | `REJECT` as absence authority | Useful discovery/reference inputs only; they cannot establish current first-party completeness |
| JobSpy's rotating proxies, TLS/browser fingerprints, and aggregator pagination | `REJECT` | It lacks Job Radar's employer-identity and terminal-evidence guarantees; challenge bypass or third-party result exhaustion cannot authorize absence |

Patterns that silently return partial results, infer employer identity from a
slug/title, treat challenge pages as empty success, or treat third-party
aggregator exhaustion as first-party absence are explicitly rejected.

## Relative cost/benefit ranking

Scores are relative from 1 (low) to 5 (high). No repository evidence contains
reliable dollar prices, so no monetary amounts are invented.

| Rank | Candidate | Sources helped | Jobs evidenced | Failure frequency evidenced | Complexity | Runtime cost | Fragility | Maintenance | Security | Compliance | Correctness | Vendor dependency / recurring cost |
|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | GitHub alternate egress | 1 | 30 partial, not complete | One checked-in failed scan | 4 | 3 | 5 | 4 | 4 | 4 | 5 | High / present but unpriced |
| 2 | Parkland alternate egress | 1 | 0 in failed scan | One checked-in failed scan | 4 | 3 | 5 | 4 | 4 | 5 | 5 | High / present but unpriced |

No browser, interactive-browser, authentication, or external-inventory
candidate reaches the ranking because none is supported by current evidence.
There is therefore no safe first infrastructure canary. **Top candidates:
NONE.** GitHub and Parkland are observations to monitor, not canary proposals.

## Option decision and close criteria

- **Option A — `CLOSE_PHASE7_EARLY`: selected.** Two isolated network-block
  observations do not justify shared infrastructure; all other problems are
  ordinary source maintenance.
- Option B — `EXPAND_EXISTING_BROWSER`: rejected. Rendering already works;
  source-specific terminal proof is missing.
- Option C — `BUILD_BROWSER_WORKER`: rejected. Zero production sources require
  a new browser execution boundary.
- Option D — `ADD_EXTERNAL_INVENTORY_LAYER`: rejected. Zero qualifying
  first-party authoritative feeds require a shared layer.
- Option E — `ADD_PROXY_EGRESS`: rejected. Two single-source observations do
  not prove effectiveness or justify cost/risk.
- Option F — `COMBINED_MINIMAL_INFRASTRUCTURE`: rejected because combining
  unsupported paths compounds risk without increasing evidence.

Phase 7 should be reopened only if all of the following become true:

1. At least three production sources show repeatable infrastructure-only
   failures after adapter, identity, endpoint, pagination, and transient causes
   are excluded.
2. A source-specific bounded canary can preserve employer identity, stable
   inventory identity, terminal evidence, mutation detection, and absence
   authority.
3. Existing static HTTP, ATS adapters, the current browser renderer, retries,
   health/cooldown behavior, and a direct first-party feed cannot solve it.
4. Runtime, maintenance, security, compliance, correctness, vendor, and
   recurring-cost tradeoffs are accepted explicitly.

The current 533-source registry is adequate bounded coverage for roadmap
completion. Remaining failures are a maintenance queue, not evidence of a
missing architecture layer.

## Verification and repository safety

The focused Phase 7 tests cover complete classification, adapter-versus-
infrastructure separation, explicit browser candidates, proxy evidence,
first-party external-inventory rules, registry immutability, infrastructure
disablement, and unchanged lifecycle semantics.

| Verification | Result |
|---|---|
| Focused Phase 7 audit | 10 passed |
| Source integrity | 36 passed, 36 subtests passed |
| Source health/cooldown | 28 passed, 26 subtests passed |
| Adapter classification | 32 passed |
| Phase 3 | 12 passed, 4 subtests passed |
| Phase 4 | 143 passed, 38 subtests passed |
| Phase 5 | 84 passed |
| Phase 6 | 96 passed |
| Retention | 26 passed |
| Combined requested regression slice | 425 passed, 104 subtests passed |
| Full deterministic suite | 2,509 passed, 911 subtests passed in 223.51s |
| Warnings | One existing `LangChainDeprecationWarning` at `src/agents/search_agents_adk.py:131` |
| `git diff --check` | Passed |

No live scan, canary, provider/company request, browser session, proxy request,
Supabase mutation, migration execution, GitHub Action, commit, push, merge,
checkout, or branch change is part of this audit.

**Recommended next step:** stage only the Phase 7 audit metadata, offline
reporting module/script, focused tests, and this document after review; then
mark the seven-phase architecture roadmap complete and handle the 57-source
queue through ordinary source-specific coverage maintenance. Keep the named
Batch 9 artifacts and run history unstaged.
