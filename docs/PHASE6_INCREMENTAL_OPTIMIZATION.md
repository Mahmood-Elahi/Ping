# Phase 6 — Incremental Optimization

> **INCREMENTAL IS AN OPTIMIZATION, NOT A NEW SOURCE OF TRUTH.**

Phase 6 adds fail-closed domain contracts, bounded checkpoint persistence,
positive-only staged persistence, conditional-snapshot validation primitives,
and deterministic observability. It activates no production adapter.

## Architecture and existing integration points

`AdapterResult` remains the adapter boundary. Its existing `SourceScanMetadata`
continues to carry Phase 3 pagination and inventory evidence; Phase 6 adds the
orthogonal `crawl_mode` and `result_authority` fields. The central scanner still
calls `finalize_source_scan`, so adapters cannot manufacture lifecycle trust.
`CompanyScanResult.authoritative_for_absence` now additionally requires
`authoritative_inventory` authority.

The existing source-scoped staged path remains the only persistence path:

1. normalized observations are grouped by exact `source_scope_key`;
2. `source_authorities` is converted to the existing `reconcile_absence` gate;
3. positive-only rows are staged and upserted normally while that gate is false;
4. an optional checkpoint candidate rides on `job_sync_sessions`;
5. the exact `job_sync_sessions` row transitioning to `finalized` triggers the
   checkpoint upsert in the same database transaction;
6. a staging, normalization, persistence, or finalization failure therefore
   leaves the accepted checkpoint unchanged.

`tracked_job_source_scopes` continues to isolate scope ownership. Source
retirement remains a separate, deliberate RPC. Freshness and 60-day retention
remain output/storage policies and never become absence evidence.

## Domain contract

The types live in `src/scraper/company_careers/incremental.py`.

- Capability: `none`, `timestamp_watermark`, `cursor`,
  `inventory_plus_incremental_hydration`, `explicit_change_feed`, or
  `validated_snapshot_reuse`.
- Run mode: `full_authoritative`, `incremental_positive_only`, or
  `cached_authoritative_snapshot`.
- Result authority: `authoritative_inventory`, `positive_only`, or `incomplete`.
- `IncrementalCheckpoint` binds exact source scope, provider identity,
  configuration fingerprint, and strategy version. Cursor and validator values
  are length-bounded and the serialized candidate is capped.
- `IncrementalRequest` records the chosen mode and binding.
- `IncrementalResult` rejects authoritative incremental results and rejects a
  checkpoint candidate on an incomplete result.

No parallel lifecycle state machine was introduced. Authority is an additional
input to the Phase 3–5 lifecycle gate.

## Crawl-mode selection

`choose_crawl_mode(...)` chooses a full crawl for:

- no reviewed incremental capability;
- missing, corrupt, oversized, or identity-mismatched checkpoint;
- configuration fingerprint or strategy-version change;
- no successful full crawl;
- elapsed source-configured forced-full interval;
- ambiguous prior incremental result;
- provider regression; or
- an explicit reconciliation requirement.

Otherwise it may select positive-only incremental mode. The conservative
default forced-full interval is seven days, but it belongs to per-source policy
and is not a universal production cadence. Incremental successes do not move
`last_successful_full_at`, so they cannot postpone reconciliation indefinitely.

Cursor repetition/rollback, timestamp rollback, absent timestamps, malformed
continuation, caps, contraction, invalid 304 state, corruption, and exhausted
retry budgets must be returned by an adapter as incomplete or force a full
fallback. A repeated cursor is never end-of-file evidence.

## Positive-only semantics

Positive-only runs may insert or refresh jobs, advance `last_seen_at`, reset the
observed scope owner to active, protect observed provider identities, and accept
material changes. They pass `p_reconcile_absence = false`; therefore absent rows
are not visited by the missing-counter update and sibling scopes are untouched.

The database remains the enforcement boundary. `successful_source_scopes`
continues as the backward-compatible authoritative input. New callers should
pass `source_authorities`; conflicting positive-only and authoritative claims
are rejected before staging.

## Material changes and hydration avoidance

`material_content_fingerprint` hashes stable/material job fields and explicitly
excludes observation-only timestamps. `can_skip_hydration` requires an exact,
non-empty trustworthy provider version on both the stored and inventory item.
A previously seen job ID alone never permits a skip. If a reviewed version is
not available, normal hydration remains required.

This follows freehire's content-hash and cheap refresh separation while keeping
Job Radar's existing `source_updated_at` and `last_seen_at` columns distinct.
The current SQL upsert always advances `last_seen_at`; observation-only changes
are excluded from the material fingerprint.

## Validated conditional snapshots

`ValidatedSnapshot` and `reuse_validated_snapshot` are architecture only. A 304
is reusable only if all of these match:

- reviewed provider contract;
- exact source scope;
- exact provider identity;
- exact configuration fingerprint;
- exact response validator;
- a prior complete authoritative snapshot; and
- SHA-256 integrity of the cached payload.

Missing or corrupt cache state raises a fail-closed error. A 304 is never an
empty inventory. No HTTP adapter currently enables conditional snapshot reuse.

## Observability

`IncrementalResult.metrics()` emits only measured/domain values:

- selected crawl mode;
- checkpoint age and last-full age;
- fallback reason;
- requests saved;
- detail hydrations skipped;
- incremental items observed;
- full inventory size;
- checkpoint advanced; and
- absence authorized.

## Initial Stage A/B working inventory (superseded)

All entries below remain **full-crawl only in production**. “Promising” means a
future adapter-specific review has repository evidence worth testing; it is not
activation approval. This was the working queue before the exhaustive Stage B3
triage. The complete matrix and final ranking below supersede these preliminary
groupings.

| Adapter/platform | Repository-only classification | Evidence / missing proof |
|---|---|---|
| Greenhouse | **FULL_CRAWL_ONLY** | Stable IDs and an atomic active-board collection exist, but `updated_at` is not proven to be a material version. There is no reviewed update filter, cursor, removal feed, or conditional representation validator. Compact production reads may hydrate up to 20 details; ID/timestamp equality cannot safely skip them. |
| SmartRecruiters | **FULL_CRAWL_ONLY** | The anonymous listing endpoint has stable posting IDs, complete `totalFound` plus offset/limit inventory, and a `releasedAfter` filter. The provider documents `releasedDate` as the date the posting was released, not a last-modified/material-version field. No reviewed update ordering, opaque cursor, deletion feed, or conditional-response contract exists for the anonymous production source. All production targets are already listing-only, so there are no detail requests to save. |
| Ashby | Promising inventory + selective hydration | Explicit inventory and separated summary/detail paths; summary timestamps vary by endpoint and need contract validation. |
| BambooHR | Promising inventory + selective hydration | Explicit inventory plus detail endpoint and `datePosted`; no proven material version yet. |
| SAP SuccessFactors | Promising inventory + selective hydration | Explicit inventory and configured selective detail enrichment; multiple protocol variants make a single generic watermark unsafe. |
| Workday | Promising inventory + selective hydration | Explicit inventories in reviewed flows; `postedOn` is not proven to be a material update version. Partition/truncation rules remain authoritative. |
| Dayforce | Promising inventory + selective hydration | Stable inventory identities and posting timestamp; no repository proof of change-version semantics. |
| ADP, Deel, Paycor, PeopleSoft, Cornerstone CSOD | Promising inventory + selective hydration | Explicit identities exist, but trustworthy item versions are absent or variant-specific. |
| Lever | Promising timestamp incremental | `createdAt` is captured and pagination is bounded. Newest-first ordering and edits-after-create are not proven, so no activation. |
| Eightfold | Promising cursor/incremental enrichment | Bounded pagination and posting timestamps exist; Jobseek demonstrates complete streaming inventory plus enrichment, but Job Radar has no reviewed incremental contract yet. |
| Shopify, Apple, Amazon, Atlassian, UKG Pro, Uber | Promising timestamp incremental | Listing timestamps exist, but semantics/order/rollback behavior are not proven from Job Radar code. The separate Workable module is dormant and is not scanner-dispatchable. |
| Public JSON, first-party JSON, embedded data, source XML | Insufficient deterministic evidence | These are configuration-driven families; capability must be reviewed per exact endpoint, not granted to the generic adapter. |
| Browser-rendered HTML, static HTML | Insufficient deterministic evidence | Page rendering and selectors supply no generic stable version or validator contract. |
| Avature, iCIMS, Oracle Recruiting, Phenom, HawkSearch v2, mCloud CWS, Beesite, ClearCompany, Exalead, HiBob, Jobvite, TalentBrew, Teamtailor, Startdate | Insufficient deterministic evidence | Full pagination contracts exist; repository code does not prove a safe incremental boundary. |
| Meta, Google, IBM Search, Amazon custom, Atlassian custom, Uber custom | Insufficient deterministic evidence | Bespoke feeds expose some dates/cursors, but no reviewed deletion, ordering, or checkpoint contract. |
| Source-specific manual/browser aliases | Full-crawl only | No deterministic incremental evidence. |

The working queue initially named Eightfold for the next review. Stage B3
completed that repository-level comparison and supersedes the queue with
`NEXT_DEEP_REVIEW = NONE`.

SmartRecruiters and Greenhouse are removed from the canary queue by their
adapter-specific reviews below. Either can re-enter only after stronger
provider evidence or a different reviewed endpoint proves exact safe savings.

## SmartRecruiters Stage B contract audit

### Verdict

Classification: **A. FULL_CRAWL_ONLY**.

The adapter policy is explicit and production-safe:

```python
INCREMENTAL_POLICY = IncrementalPolicy(
    capability=IncrementalCapability.NONE,
    strategy_version="smartrecruiters_v1",
)
TRUSTED_LISTING_VERSION_FIELD = None
```

No production registry entry enables incremental behavior. No checkpoint is
initialized, read, proposed, or advanced for SmartRecruiters.

### Evidence and rejected paths

The exact anonymous production endpoint is
`GET /v1/companies/{companyIdentifier}/postings`.
The reviewed primary references are the provider's
[v1 list-postings contract](https://developers.smartrecruiters.com/reference/v1listpostings),
[posting object definition](https://developers.smartrecruiters.com/docs/objects),
[single-posting contract](https://developers.smartrecruiters.com/reference/v1getposting),
and the separate authenticated
[publication-feed contract](https://developers.smartrecruiters.com/reference/postingsjsonstream-1).

- Posting `id` is a stable provider posting identity. Job Radar validates an
  all-numeric ID on both listing and detail records and requires the exact
  company-scoped detail reference.
- The listing response supplies `totalFound`, `limit`, `offset`, and `content`.
  Job Radar pages at 100, rejects changing totals and duplicate/overlapping
  identities, and grants completion only when the collected unique inventory
  exactly reaches the advertised total.
- The endpoint supports offset/limit and a documented `releasedAfter` filter.
  It exposes no reviewed `updated_since`, last-modified ordering, or opaque
  continuation cursor.
- The provider object contract defines `releasedDate` as the date a posting was
  released. It is a posting timestamp, not a trustworthy job-level material
  update version. Job Radar therefore must not pass it to
  `can_skip_hydration(...)`.
- The provider notes that changing public posting content requires re-posting,
  but the reviewed contract does not guarantee whether every material edit
  changes `releasedDate`, retains/replaces the posting ID, or is returned by
  `releasedAfter`. A field name plus a re-post requirement is not a monotonic
  watermark contract.
- The authenticated `/feed/publications` job-board integration exposes statuses
  including unpost events and an `updatedAfter` parameter. It is a separate,
  tokened, stateful integration contract. Job Radar's production sources use the
  anonymous company postings endpoint and possess neither the credentials nor
  the state-transition protocol required by that feed. It cannot be borrowed as
  authority for these sources.
- No deterministic fixture or sibling implementation proves ETag,
  Last-Modified, 304, or another conditional representation contract.
- Freehire and Jobseek both enumerate complete offset inventories. Freehire
  hydrates every detail; Jobseek either returns the complete URL inventory or
  hydrates its configured canonical modes. Neither sibling supplies a
  SmartRecruiters update watermark or deletion authority.

Rejected classifications:

- **B. INVENTORY_PLUS_SELECTIVE_HYDRATION:** every current production
  SmartRecruiters target has `selective_detail_enrichment: false`; its normal
  result is already produced entirely from listing rows. There are no detail
  calls to skip. For hypothetical hydrated targets, `releasedDate` is not a
  trusted version, so ID-plus-release-date cannot authorize a skip.
- **C. TIMESTAMP_INCREMENTAL_POSITIVE_ONLY:** `releasedAfter` is release-time
  filtering, not proven complete new-and-materially-changed discovery.
- **D. CURSOR_INCREMENTAL_POSITIVE_ONLY:** the endpoint has page offsets only.
- **E. EXPLICIT_CHANGE_FEED:** the only reviewed removal/change-status feed is a
  different authenticated integration and is not the production source.
- **F. VALIDATED_SNAPSHOT_REUSE:** no reviewed response validator or 304
  representation contract exists.

Content fingerprints cannot replace discovery. They can prove that two fetched
records differ, but cannot reveal an edit to a posting that an incremental
request failed to return.

### Production inventory and request model

The production registry currently contains 15 SmartRecruiters sources: Wise,
ASML, Renesas Electronics, Bosch, Tower Research Capital, Western Digital,
Kioxia, Canva, Ubisoft, SGS, Avery Dennison, Genetec, Gameloft, Arista Networks,
and Assent. All use source scope `default`, the exact anonymous company endpoint,
and listing-only normalization.

For a complete board of `N` postings, the normal request model is exactly
`ceil(N / 100)` listing requests, bounded by the target's `max_pages` and
`max_jobs`. It makes zero detail requests in production. Completion requires
all `N` unique stable IDs and the authoritative total; reaching either safety
limit is incomplete.

There is no accepted optimized request model and therefore no defensible
expected savings. A `releasedAfter` request might be smaller on a quiet board,
but it could omit a material edit and is not an optimization Job Radar may use.

### Future company decision

No company is selected for a runnable Phase 6 SmartRecruiters A/B canary because
there is no accepted strategy to canary. Running two full crawls would not test
an optimization; running `releasedAfter` would test a contract already known to
be insufficient for exact changed-job discovery.

If stronger provider evidence later re-opens the review, **Wise / `default` /
tenant `wise`** is the preferred re-evaluation target. It has stable official
identity, a previously complete 403-posting observation, and a bounded five-page
full baseline, while ASML and the smallest boards often need only one listing
request and cannot demonstrate material savings. Wise is a blocked research
candidate, not an approved canary. Its full request budget is 5; an optimized
budget remains undefined until a valid strategy exists.

### Authority, checkpoints, fallback, and retention

- Only the complete offset/total inventory may authorize absence.
- A release-filtered slice, if ever used diagnostically, is positive-only and
  has `absence_authorized=false`; it cannot increment missing counters, close
  jobs, publish a dashboard, or mutate lifecycle state.
- The intended provider identity is `smartrecruiters:{tenant.casefold()}` and
  the checkpoint key remains bound to the exact canonical company plus
  `source_scope`.
- A future configuration fingerprint would include tenant/slug, endpoint
  variant and URL, source scope, page size and caps, enrichment mode, trusted
  version field, overlap rule, forced-full interval, and strategy version.
- There is no initial checkpoint for capability `none`. Missing/corrupt state,
  config-fingerprint change, strategy-version change, timestamp rollback,
  provider regression, pagination ambiguity, detail failure, or request-budget
  exhaustion must select or retain full-crawl behavior and cannot propose a
  checkpoint.
- Even after a future strategy is approved, a checkpoint candidate may advance
  only through the existing atomic persistence finalizer after all observations
  persist. Failed/incomplete canaries and persistence failures never advance it.
- Retention age is never provider absence. Forced full scans must occur more
  frequently than any retention window, and positive-only runs cannot defer the
  recorded last successful full crawl.

### Hydration and material fingerprint audit

Hydration skipping is rejected. SmartRecruiters has no trusted listing-level
version. Matching only posting ID or `releasedDate` is insufficient, including
when content happens to be unchanged in a fixture.

The generic material fingerprint covers all fields currently normalized by the
adapter: provider ID and source, title, company, location, canonical/apply URL,
description, department/function categories, offices, posting/release date, and
stable raw metadata including company identity, API detail URL, apply URL,
reference number, endpoint variant, and hydration mode. SmartRecruiters exposes
no normalized closing-date field. Observation-only timestamps remain excluded.
Deterministic tests additionally prove that a title, description, or apply URL
change alters the fingerprint even when ID and `releasedDate` remain unchanged.

### Canary, PASS/FAIL, activation, and rollback gates

No exact manual incremental canary command is published. The existing
`scripts/validate_source_integrity_canaries.py` command can run a bounded,
read-only normal source canary, but it does not implement a full-versus-
incremental A/B comparison and must not be represented as one. No new live
wrapper was added because there is no safe optimized branch to execute.

Current PASS is deliberately impossible. SmartRecruiters may return to canary
design only after repository evidence proves one of these contracts:

1. a trustworthy job-level material version plus complete inventory, allowing
   version-bound selective detail hydration; or
2. a change query that is complete for every new and material update after a
   watermark, with defined inclusivity/overlap and rollback behavior; or
3. an exact opaque change cursor or explicit change feed bound to the same
   public provider identity; or
4. a reviewed conditional representation validator for complete snapshots.

After one prerequisite is proven, an A/B canary must also satisfy the generic
Stage B gates: identical provider/scope binding; all expected identities and
material records equal to the full baseline; lower measured request count; no
hidden continuation, safety limit, unreadable/rejected optimized item, or
ambiguous evidence; valid checkpoint candidate; no checkpoint advance before
persistence; and no lifecycle, registry, database, or dashboard mutation.

FAIL is any missing prerequisite or A/B gate, including missing/malformed or
regressing timestamps, duplicate IDs, a changed record hidden behind an
unchanged alleged version, incomplete pagination, exhausted request/retry
budget, config/strategy mismatch, cross-scope state, any absence reconciliation
from a slice, or no measured request saving.

There is no future activation diff while capability remains `none`. The exact
production diff is empty. Rollback is likewise a no-op: production already uses
full authoritative crawls and has no SmartRecruiters checkpoint rows to delete
or disable. If a later reviewed strategy is implemented, activation must be one
source-specific `enabled: false -> true` change after a passing canary, and
rollback must restore `false`, ignore the bound checkpoint, and immediately use
the unchanged full authoritative path.

## Greenhouse Stage B contract audit

### Verdict

Classification: **A. FULL_CRAWL_ONLY**.

The adapter now records the decision explicitly:

```python
INCREMENTAL_POLICY = IncrementalPolicy(
    capability=IncrementalCapability.NONE,
    strategy_version="greenhouse_v1",
)
TRUSTED_LISTING_VERSION_FIELD = None
```

Greenhouse has a useful Phase 3 inventory/hydration split, but that does not by
itself create a Phase 6 incremental contract. The public board response exposes
stable IDs and a listing `updated_at`; no reviewed deterministic evidence proves
that `updated_at` changes for every material title, location, apply URL,
department, office, metadata, or description edit. Matching an ID and timestamp
therefore cannot skip `/jobs/{id}` safely.

No reviewed contract exposes `updated_since`, `created_since`, update ordering,
an opaque change cursor, webhook history, an archive/deletion feed, ETag reuse,
or Last-Modified/304 semantics for the complete active-board representation.
The adapter sends no conditional headers and accepts only HTTP 200.

### Provider and normalized-field contract

- Provider identity is `greenhouse:{slug}`. The stable item identity is the
  public board API row `id`, normalized to text. A separately fetched detail
  must return that same `id` or it is rejected.
- The strict path form requires `/slug/jobs/id` on an allowed Greenhouse host.
  The strict query-ID form requires an explicitly configured HTTPS template,
  one safe query parameter whose entire value is `{id}`, and optional one-time
  repetition of `{id}` in the exact path. Waymo, Ripple, and Miro use this form.
- The compact listing normalizes `id`, `title`, configured company, the
  `location.name`, `absolute_url`, `updated_at`, source namespace, and the full
  listing object as stable raw metadata. Named custom `metadata` values are also
  exposed as `greenhouse_metadata` for relevance and attribution.
- Separate detail hydration supplies normalized `content`, `departments`, and
  `offices`. Detail title and URL do not replace listing identity; the detail ID
  must correlate exactly. Requisition/internal IDs and other listing fields are
  retained in raw metadata when present, not promoted to separate JobRecord
  fields. No Greenhouse closing date or created/source-posted timestamp is
  normalized today.
- `absolute_url` is the one normalized canonical/apply URL. Strict variants
  correlate it to the board and ID; non-strict variants preserve the historical
  URL behavior and do not acquire stronger identity from this Phase 6 review.

The listing endpoint is modeled as one atomic active-board collection: one
`jobs` array, no pages, cursor, or authoritative total. Completeness depends on
receiving and validating the entire array and staying below `max_jobs`; exactly
reaching the safety bound is incomplete. Empty `jobs` is a valid complete
collection. Any newly exposed continuation marker now fails closed instead of
being mistaken for terminal success. Any HTTP redirect of an API request also
fails exact request identity. Duplicate stable IDs or canonical URLs fail every
variant, not only strict sources.

Greenhouse exposes no explicit removal event in the reviewed evidence.
Disappearances therefore remain authorized only after a complete full active-
board inventory is finalized for the same `source_scope`.

### Production inventory and request model

The effective production registry contains **102** Greenhouse sources. Every
source is active, uses `source_scope: default`, `max_pages: 1`, and
`max_jobs: 10000`. Ninety-nine use `max_response_bytes: 3000000`; Zipline,
CoreWeave, and Grafana Labs use `5000000`.

Endpoint variants are configuration/identity variants over the same
`https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` listing route:

- 92 have no explicit endpoint variant;
- 3 use `boards_api_v1`: Hudson River Trading, Phoenix Contact, and Agility
  Robotics; and
- 7 use `boards_api_v1_strict`: Waymo, Ripple, DRW, Flexport, SharkNinja, Box,
  and Miro.

The exact production company/slug inventory is:

Airbnb=`airbnb`; Stripe=`stripe`; Databricks=`databricks`;
Cloudflare=`cloudflare`; Datadog=`datadog`; MongoDB=`mongodb`;
Coinbase=`coinbase`; Elastic=`elastic`; GitLab=`gitlab`; Twilio=`twilio`;
Zscaler=`zscaler`; Anthropic=`anthropic`; xAI=`xai`; Figma=`figma`;
Robinhood=`robinhood`; Chime=`chime`; Brex=`brex`; Magic Leap=`magicleap`;
Rocket Lab=`rocketlab`; Vast=`vast`; DoorDash=`doordashusa`; Reddit=`reddit`;
Discord=`discord`; Pinterest=`pinterest`; Hudson River Trading=`hrttalentcommunity`;
Phoenix Contact=`phoenixcontact`; Agility Robotics=`agilityrobotics`;
Waymo=`waymo`; Ripple=`ripple`; DRW=`drweng`; Flexport=`flexport`;
Jump Trading=`jumptrading`; Relativity Space=`relativity`; Figure AI=`figureai`;
SpaceX=`spacex`; Adyen=`adyen`; Jane Street=`janestreet`;
Lucid Motors=`lucidmotors`; Instacart=`instacart`; Faire=`faire`;
Medium=`medium`; Block=`block`; Geotab=`geotab`; D2L=`d2l`;
Hootsuite=`hootsuite`; Apptronik=`apptronik`; Nuro=`nuro`;
Kodiak Robotics=`kodiak`; Gatik=`gatikaiinc`; Scale AI=`scaleai`;
SharkNinja=`sharkninjaoperatingllc`; PurposeMed=`purposemed`;
Tenstorrent=`tenstorrent`; Affirm=`affirm`; Motional=`motional`;
Locus Robotics=`locusrobotics`; indie Semiconductor=`indiesemiconductor`;
Twitch=`twitch`; Coconut Software=`coconutsoftware`; Astera Labs=`asteralabs`;
SK hynix=`skhynixamerica`; Roku=`roku`; Ambiq=`ambiqmicroinc`;
Diligent Robotics=`diligentrobotics`; Zipline=`flyzipline`;
HubSpot=`hubspotjobs`; Dropbox=`dropbox`; Okta=`okta`; Box=`boxinc`;
Asana=`asana`; Vercel=`vercel`; Airtable=`airtable`; AppLovin=`applovin`;
Appian=`appian`; Samsara=`samsara`; PagerDuty=`pagerduty`;
Miro=`realtimeboardglobal`; Fivetran=`fivetran`; Braze=`braze`;
DigitalOcean=`digitalocean98`; Fastly=`fastly`; CoreWeave=`coreweave`;
Cockroach Labs=`cockroachlabs`; Grafana Labs=`grafanalabs`;
Pulumi=`pulumicorporation`; Tailscale=`tailscale`; Netlify=`netlify`;
PlanetScale=`planetscale`; Together AI=`togetherai`;
SambaNova Systems=`sambanovasystems`; Glean=`gleanwork`; Arize AI=`arizeai`;
Snorkel AI=`snorkelai`; Labelbox=`labelbox`; SingleStore=`singlestore`;
Ada=`ada18`; StackAdapt=`stackadapt`; League=`leagueinc`;
Auvik Networks=`auviknetworks`; AbCellera=`abcellera`;
Aspect Biosystems=`aspectbiosystems`; Later=`later`.

Allowed-apply-host configuration has 24 empty lists, 32 exact
`job-boards.greenhouse.io` lists, 4 exact `boards.greenhouse.io` lists, 15
two-host Greenhouse lists in either order, and 27 patterns containing a
company-owned host. The company-owned-host cases are HRT, Agility Robotics,
Waymo, Ripple, Jump Trading, Jane Street, Instacart, Block, D2L, Nuro,
PurposeMed, Motional, indie Semiconductor, Roku, Zipline, HubSpot, Dropbox,
Okta, Asana, Samsara, Miro, Fivetran, DigitalOcean, Fastly, CoreWeave,
Cockroach Labs, and SambaNova. These lists are identity-enforcing in strict
mode; their presence must not be misreported as strict validation for the
historical non-strict variants.

All 102 use the same compact listing plus bounded-detail code path. With `D`
selected rows needing content, where `0 <= D <= 20`, the deterministic model is
`1 listing + D details`, or 1 to 21 requests. CoreWeave alone configures the
`Technology` department allowlist and exact Acquisition Company attribution;
its streamed compact path is `1 listing + 1 department index + D details`, or
2 to 22 requests. There is no pagination request. A detail transport failure
retains the listing as partial progress and records an unreadable item; the
adapter itself has no retry loop, so it cannot silently exceed this model.

Historical Phase 1 evidence activated Waymo, SharkNinja, Box, Ripple, Miro, and
other reviewed replacements without changing this request model. Known
non-production Greenhouse failures remain excluded: the Lightspeed global and
France tokens returned 404 in their recorded reviews, Groq and Hebbia have
stale/moved boards, Niantic has historical 404 evidence, and Coupang remains
pending exact custom-URL identity. None is a hidden 103rd active production
Greenhouse source, and no archived board is treated as removal authority.

### Candidate optimizations and savings

**Inventory plus selective hydration is rejected.** The theoretical current
model is `1 + D` (or CoreWeave `2 + D`). A valid trusted-version strategy could
be `1 + K`, with `K` changed rows and savings `D - K`; however, no trustworthy
material version exists, so `K` cannot be selected safely. ID equality,
`updated_at` equality, content-hash equality from an earlier detail, or a
missing/malformed timestamp cannot justify a skip.

**Timestamp and cursor incrementals are rejected.** `updated_at` may improve
change diagnostics, but without a complete server-side update filter it cannot
reduce the listing request or discover only changed records. No reviewed update
filter, ordering rule, overlap boundary, monotonic cursor, or explicit change
feed exists. Any hypothetical timestamp slice would be positive-only and could
never authorize absence.

**Validated snapshot reuse is rejected.** No reviewed ETag or Last-Modified
contract binds a validator to the complete active-board representation. An
observed header or fixture would not be enough. A 304 would require the exact
board/source-scope/config binding, a prior complete authoritative payload, and
a verified checksum; the Greenhouse adapter implements none of this.

**Expanded full-board representation is not accepted for activation.** Jobseek
and freehire independently use `?content=true` to receive content, departments,
offices, first-published data, and requisition metadata in one full-board
request. This is useful evidence for future non-incremental request-shape work,
not proof of a material version. Freehire also records that expanded boards can
reach tens of MiB, while Job Radar production caps are 3 or 5 MB. No local A/B
fixture proves exact normalized equivalence, strict URL behavior, and cap
compliance for one current production source. Consequently its theoretical
`1 request` model and possible savings of `D` are not claimed as safe savings.

The accepted optimized request model is therefore absent, and expected safe
request savings are exactly **0**.

### Material fingerprint, authority, and checkpoint audit

The generic material fingerprint covers Greenhouse stable provider ID and
source, title, company, location, canonical/apply URL, description,
departments, offices, source update/post dates when present, and stable raw
metadata including custom metadata and requisition/internal IDs. It now also
includes `source_scope` and exact `cross_source_equivalence`; observation times,
scan IDs, and last-seen/first-seen operational fields remain excluded.
Greenhouse produces no normalized closing date. Deterministic tests prove that
a changed description changes the fingerprint even when `id` and `updated_at`
do not, while unchanged material content remains stable.

Only a complete full active-board inventory for the exact logical scope may
authorize absence. Detail failures do not erase inventory protection. Retention
age remains independent and is never evidence of provider removal.

Capability `none` creates and advances no Greenhouse checkpoint. If a future
strategy is proven, its state must bind source-scope key, provider identity,
board slug, endpoint/config fingerprint, strategy version, and the exact
timestamp/cursor/representation token it uses. It may advance only through
accepted atomic persistence, never on incomplete scans, identity ambiguity,
malformed state, budget exhaustion, hydration failure, or persistence failure.

### Canary, PASS/FAIL, activation, and rollback gates

Future canary company: **NONE**. Waymo is not selected merely because it has
the strongest strict query-ID tests; no accepted optimized branch exists, so
its expected safe savings are zero. No exact manual Greenhouse A/B canary
command is published, and no canary tool or candidate config is fabricated.

Current PASS is not achievable. Re-evaluation requires at least one of:

1. a documented material job version that changes for every normalized edit;
2. a complete new-and-changed query with inclusive overlap and rollback rules;
3. a monotonic opaque cursor or explicit change/removal feed;
4. a reviewed complete-representation validator suitable for safe 304 reuse;
   or
5. for the non-incremental expanded representation only, deterministic proof
   that one bounded source returns exact full-baseline material records within
   its response cap and with a lower measured request count.

After a prerequisite exists, PASS must require identical employer identity,
source scope, stable IDs, complete required inventory, terminal evidence, and
material normalized records; strictly fewer measured requests; no omitted
changed jobs, continuation, malformed/rejected/unreadable item, safety stop,
or budget exhaustion; correct authority; valid but unadvanced checkpoint state
when applicable; and zero production mutation.

FAIL is any ambiguity: wrong slug/board/host/path/query, redirect, duplicate ID
or URL, missing or false material-version evidence, a changed detail hidden by
an alleged unchanged version, incomplete collection, hidden continuation,
oversized response, exhausted request/retry budget, config/strategy mismatch,
cross-scope state, any absence inference from a positive-only slice, failed
persistence, or no measured request saving.

There is no future activation diff while capability remains `none`: the exact
production diff is empty. Rollback is a no-op because production already runs
the unchanged full crawl and no Greenhouse checkpoint exists to delete or
disable.

## Sibling-repository provenance

No sibling file was modified or copied wholesale.

- Jobseek `apps/crawler/src/core/monitors/_incremental.py` informed the bounded
  high-water pagination/fallback shape; `_pcsx.py` shows an actual caller.
- Jobseek `apps/crawler/src/core/monitors/eightfold.py` informed the separation
  of complete discovery from incremental enrichment.
- Jobseek's inventory-runner documentation (`docs/21-ats-inventory-runner.md`)
  informed fail-closed 304 reuse and integrity validation.
- Jobseek `apps/crawler/go/greenhouse-monitor` demonstrates one bounded
  `?content=true` rich collection and stable listing fields, but supplies no
  material-version or removal semantics.
- freehire `cmd/ingest/store.go` and
  `internal/platform/db/queries/jobs.sql` informed content-hash cheap writes,
  `last_seen_at` touch behavior, and the cache-is-not-truth rule.
- freehire `internal/ingest/sources/greenhouse.go` independently demonstrates
  the expanded full-board shape; its HTTP layer records that such bodies can
  reach tens of MiB, so Job Radar's smaller cap remains a real gate.
- JobSpy's Indeed/Glassdoor/Google pagination implementations were consulted
  only as cursor-shape references. Their stop/lifecycle semantics were not
  imported.

Explicitly rejected:

- repeated cursor means success;
- generic 4xx means EOF;
- trailing-window absence authority;
- elapsed grace as absence authority;
- seen ID alone skips hydration;
- cache/304 means empty inventory; and
- retention age means provider absence.

## Stage B3: cross-adapter capability triage

### Scope, inventory, and scoring

This repository-only triage covers **41 production adapter families** and
**533 production sources**.  It also covers the dispatchable `source_xml` and
`hawksearch_v2` families, which currently have zero production sources.
`workable.py` was inspected but is not in `scanner.ADAPTERS` and has no registry
row, so it is a dormant module rather than a supported production family.
Scanner aliases such as `custom:amazon` and `custom:google` collapse into their
canonical adapter families; `javascript_embedded_data` is the production name
for the `embedded_data.py` implementation.

The score is `V/C/S/E/X/R -> P`: production volume, request cost, theoretical
savings, deterministic contract strength, implementation complexity, and
lifecycle risk, each 0-5.  Priority `P = max(0, V+C+S+E-X-R)`.  These are
repository-evidence scores, not runtime measurements.  Configured page/job
ceilings are treated as bounds, never as observed traffic.

Pattern names in an `INSUFFICIENT_EVIDENCE` row are research hypotheses, not
accepted capabilities.  Every adapter's executable policy remains
`IncrementalCapability.NONE`; no checkpoint can be initialized or advanced.

### Complete adapter capability matrix

| Adapter (production sources; endpoint variants) | Inventory and request model | Detail, browser, and change evidence | Classification / candidate pattern / theoretical saving | Score |
|---|---|---|---|---:|
| `adp` (8; Workforce Now 7, MyJobs 1) | Bootstrap + total/offset pages (1-3 configured) | Workforce Now hydrates every listed item; no browser; posting dates are not material versions | `INSUFFICIENT_EVIDENCE` / `COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION` / up to N details in theory, no safe K selector | 2/4/4/3/3/4 -> **6** |
| `amazon` (1; search-terms offset v1) | Authoritative only after every 100-row search-term total and union reconcile; 4-page config | Listing-only; no browser; dates do not prove edits | `FULL_CRAWL_ONLY` / `NONE` / 0 safe requests | 1/2/1/2/3/4 -> **0** |
| `apple` (1; `apple_search_api_v1`) | `ceil(totalRecords/page size)`, configured to 3 pages | Listing-only; no browser; posting date only | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/3/3/3 -> **1** |
| `ashby` (72; default 70, `posting_api_v1` 2) | One complete compact board collection | Up to 20 optional detail documents; no browser; `publishedDate` is not a material version | `INSUFFICIENT_EVIDENCE` / `COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION` / `D-K` details in theory, K unproven | 5/4/5/3/3/4 -> **10** |
| `atlassian` (1; bespoke default) | One complete JSON collection | No details/browser; `updatedDate` has no reviewed update query | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/3/1/1 -> **4** |
| `avature` (7; portal 4, Radancy 2, AEM 1) | Variant HTML terminal evidence, all capped at 2 pages | No detail/browser; no trusted version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 2/2/1/2/3/4 -> **0** |
| `bamboohr` (7; complete public collection) | One total-checked inventory | Up to 20 details; no browser; `datePosted` is not a material version | `INSUFFICIENT_EVIDENCE` / `COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION` / `D-K` in theory, K unproven | 2/4/4/3/2/4 -> **7** |
| `beesite` (2; `gjb_search_v1`) | `ceil(total/20)`, both capped at 2 pages | No details/browser/version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/2/2/3 -> **1** |
| `browser_rendered_html` (4; v1) | One isolated render plus delegated static extraction; only MediaTek has a reviewed single-page terminal contract | Browser cost is intrinsic; no source/config-bound validator or reusable snapshot | `INSUFFICIENT_EVIDENCE` / `BROWSER_RENDER_RESULT_REUSE`, `VALIDATED_CONDITIONAL_SNAPSHOT` / one render in theory, unproven | 2/5/4/1/5/5 -> **2** |
| `clearcompany` (1; default) | One complete collection | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/3/1/1 -> **4** |
| `cornerstone_csod` (1; mobile external search v1) | Offset inventory, production cap 1 page at 25 | No details/browser; stable IDs but no update/order contract | `INSUFFICIENT_EVIDENCE` / `PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY` / currently 0 pages | 1/1/1/2/3/4 -> **0** |
| `dayforce` (5; Next.js jobposting search) | Shell + CSRF + maxCount/offset, four sources cap 2 pages and one caps 4 | Detail only for missing locations; no browser; posting time is not a version; all five also lack reviewed employer identity | `INSUFFICIENT_EVIDENCE` / `COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION` / conditional details only, unproven | 2/4/3/2/4/5 -> **2** |
| `deel` (1; schema.org ItemList v1) | One complete structured response | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/3/1/1 -> **4** |
| `eightfold` (12; `pcsx`) | `ceil(count/10)`, nine sources cap 2 pages and three cap 1 | PCSX already carries Job Radar's normalized material; no separate hydration/browser; `postedTs` is posting time | `INSUFFICIENT_EVIDENCE` / timestamp/enrichment research only / at most 1 page per source under current caps | 3/2/2/2/4/4 -> **1** |
| `exalead` (1; career cards v1) | HTML total/terminal evidence, cap 2 pages | No details/browser/version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/2/3/3 -> **0** |
| `first_party_json` (1; Bytedance supplier search v1) | Up to 20 pages / 2,000 rows | No details/browser; provider dates lack edit-complete semantics | `INSUFFICIENT_EVIDENCE` / `PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY` / earlier discovery only | 1/4/3/2/3/4 -> **3** |
| `google` (1; bespoke default) | Up to 3 full-search requests | No details/browser; display timestamps only | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 network requests | 1/2/1/2/2/3 -> **1** |
| `greenhouse` (102; default 92, boards v1 3, strict v1 7) | One atomic board inventory | Up to 20 details; CoreWeave adds one department request; no browser; `updated_at` explicitly untrusted | `FULL_CRAWL_ONLY` / `NONE` / 0 | 5/4/4/4/3/5 -> **9** |
| `hawksearch_v2` (0; dispatchable) | 100-row total/offset pages when configured | No production detail/browser/version contract | `INSUFFICIENT_EVIDENCE` / pagination research / not estimable | 0/0/0/1/3/3 -> **0** |
| `hibob` (3; career-site job ads v1) | One complete collection | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/3/1/1 -> **4** |
| `ibm_search` (2; bespoke default) | One source caps 1 page, one caps 2 | No details/browser/trusted version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/2/3/4 -> **0** |
| `icims` (12; Jibe 6, legacy HTML 5, Radancy 1) | Variant-specific inventory, all capped at 1-2 pages | No details/browser; no cross-variant update/order contract | `INSUFFICIENT_EVIDENCE` / `PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY` / at most 1 page/source | 3/2/2/2/4/4 -> **1** |
| `javascript_embedded_data` (10; next-flight 3, next-data 2, public JSON 2, HTML-attribute JSON 2, Snap cache 1) | Nine sources use 1 request; one permits 2 | No details/browser; dates are variant-specific and untrusted | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 network requests | 2/1/1/2/2/3 -> **1** |
| `jobvite` (2; server search total v1) | One total-checked response, up to 500 rows | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/3/1/2 -> **3** |
| `lever` (25; default 12, postings v0 12, official-link v0 1) | Complete offset pagination, bounded at 25 pages / 500 jobs | Rich listing rows; no browser; `createdAt` is not an edit version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 4/4/3/3/3/4 -> **7** |
| `mcloud_cws` (1; Google jobs v1) | Total pagination, cap 2 requests | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 network requests | 1/2/1/2/2/3 -> **1** |
| `meta` (1; bespoke default) | Full search traversal, cap 3 pages | No details/browser; posting timestamps only | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/2/3/4 -> **0** |
| `oracle_recruiting` (16; default 8, CE v1 6, HCM REST 2) | `hasMore`/count/offset inventory, caps 1-5 pages | Listing-normalized; no browser; stable requisitions but no edit-complete filter/order | `INSUFFICIENT_EVIDENCE` / `PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY` / positive pages only | 3/3/3/3/4/4 -> **4** |
| `paycor` (2; default) | One bounded board collection | No details/browser/version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/1/1/3/1/2 -> **3** |
| `peoplesoft` (1; default) | Session-bound collection, cap 1 configured page | No separate current detail; no browser/version | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/2/3/4 -> **0** |
| `phenom` (16; embedded refine search) | Fifteen sources allow 50 pages/500 jobs; ATCO 10/100 | No details/browser; posting dates lack edit-complete ordering | `INSUFFICIENT_EVIDENCE` / `PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY` / potentially many pages, unproven | 3/5/4/2/4/5 -> **5** |
| `public_json` (7; seven distinct endpoint variants) | Variant-specific: 1-50 configured pages | No details/browser; date/version semantics differ per endpoint | `INSUFFICIENT_EVIDENCE` / conditional snapshot or positive pagination per endpoint / not generically estimable | 2/4/3/2/4/4 -> **3** |
| `sap_successfactors` (44; table 31, tile 8, columnized 5) | Variant-specific RMK terminal evidence, all capped at 20 listing pages | Columnized hydrates each item; table/tile listing-only; no browser/material version | `INSUFFICIENT_EVIDENCE` / selective hydration or positive pagination / potentially large, unproven | 4/5/5/3/5/5 -> **7** |
| `shopify` (1; bespoke default) | Full collection, cap 5 pages | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 network requests | 1/2/1/2/2/3 -> **1** |
| `smartrecruiters` (15; companies postings v1) | `ceil(totalFound/100)`, zero production detail requests | No browser; `releasedDate` is release time | `FULL_CRAWL_ONLY` / `NONE` / 0 | 3/3/1/4/3/4 -> **4** |
| `source_xml` (0; dispatchable) | Bounded XML feed/index traversal when configured | No details/browser; `lastmod` requires an exact feed contract | `INSUFFICIENT_EVIDENCE` / conditional snapshot or timestamp research / not estimable | 0/0/0/1/3/3 -> **0** |
| `startdate` (1; default) | One bounded collection | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/2/1/2 -> **2** |
| `static_html` (27; cards 24, links 3) | 24 cap at 1 page; BlackRock, Siemens, and Chevron cap at 2 | No details/browser; display dates are not versions | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 network requests | 4/2/1/2/2/4 -> **3** |
| `talentbrew` (4; search-results data-job-id v1) | Three cap at 1 page; Rakuten caps at 2 | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 network requests | 1/2/1/3/2/3 -> **2** |
| `teamtailor` (1; server-rendered jobs v1) | One complete page | No details/browser/version | `POSSIBLE_BUT_LOW_SAVINGS` / `CONTENT_HASH_DOWNSTREAM_SKIP` / 0 requests | 1/1/1/2/1/2 -> **2** |
| `uber` (1; bespoke default) | Full search, cap 5 pages | No details/browser; posting timestamps only | `FULL_CRAWL_ONLY` / `NONE` / 0 | 1/2/1/2/3/4 -> **0** |
| `ukg_pro` (5; default) | `ceil(totalCount/page size)`, capped at 1-3 pages | No fetched detail content/browser; `PostedDate` is posting time | `FULL_CRAWL_ONLY` / `NONE` / 0 | 2/2/2/3/3/4 -> **2** |
| `workday` (108; default 91, `wday_cxs_v1` 17) | 20-row pages; 25-page normal bound; exact facet subdivision above the 2,000-result safe capacity, within source request/partition/job budgets | Listing-normalized, no detail/browser; `postedOn` is display/posting age | `FULL_CRAWL_ONLY` / `NONE` / 0 | 5/5/4/4/5/5 -> **8** |

### Pattern authority and fallback rules

No pattern was accepted for activation.  If stronger evidence reopens a row:

- complete inventory plus selective hydration keeps the full inventory as
  absence authority, skips only detail calls whose exact provider material
  version is unchanged, binds version state to source/config/strategy, and
  falls back to hydrating every item on missing/regressing/ambiguous versions;
- timestamp, cursor, and pagination short-circuit slices are positive-only,
  skip later listing pages, never reconcile absence, require an overlap and a
  monotonic bounded checkpoint, and fall back to a full crawl on any ordering,
  cursor, timestamp, pagination, or persistence ambiguity;
- validated conditional snapshots keep the prior checksum-verified complete
  snapshot as authority, skip payload transfer only on a reviewed validator,
  bind cache state to exact source/provider/config, and make an invalid 304 a
  failure/full-refetch rather than an empty inventory;
- browser result reuse would require the same snapshot guarantees plus the
  rendered identity/terminal evidence.  Building the cache/worker/proxy layer
  is Phase 7 work; and
- content-hash downstream skip occurs only after the full source response has
  already been fetched and validated.  It cannot discover changes or removals
  and is not material Phase 6 value for the low-request rows above.

### Explicit high-cost family audit

| Family | Present cost driver | Safe Phase 6 reduction? |
|---|---|---|
| Workday | 108 sources, 20-row pages, and exact facet subdivision for boards at/above the 2,000-result provider boundary; retries/identity discovery share a 500-request ceiling | No. `postedOn` is not an update version; search bodies expose no reviewed update filter/cursor; complete partition union remains absence authority. |
| SAP SuccessFactors | 44 sources, up to 20 listing pages; five columnized sources can add one detail per listed row | Not yet. No listing material version selects unchanged details, and three pagination/detail variants prevent a generic watermark. |
| Phenom | 15 sources allow 50 pages/500 rows and one allows 10/100 | Not yet. Newest-first plus edit-complete semantics are not deterministically established. |
| Lever | 25 sources, up to 25 pages/500 rich rows | No. `createdAt` cannot discover later edits, so a creation boundary would violate exact equivalence. |
| Ashby | 72 one-request inventories plus up to 20 details each | Not yet. The split inventory is useful, but posting time is not a material detail version. |
| ADP | Seven Workforce Now sources add one detail request per listed item; MyJobs is listing-only | Not yet. Inventory is independently complete, but no trustworthy detail version exists. |
| browser-rendered HTML | Four isolated browser renders; three remain terminal-unproven | No Phase 6 reuse without a source-bound validated snapshot; browser/cache infrastructure belongs to Phase 7. |
| iCIMS | 12 sources across Jibe, legacy HTML, and Radancy; 1-2 pages | No material current saving and no common ordering/version contract. |
| Dayforce | Shell, CSRF, 2-4 listing pages, and conditional missing-location details | No. All five sources first lack reviewed employer identity and expose no material version. |
| Cornerstone/CSOD | One source and one configured page | Baseline is already minimal. |
| TalentBrew | Four sources, only Rakuten permits a second page | Baseline is already minimal. |
| Jobvite | Two complete single-response inventories | Baseline is already minimal. |

Retries and provider throttling can amplify attempts only within existing
budgets; they do not supply a safe optimization boundary.  No deterministic
runtime or payload-size measurements were invented.

### Workday verdict

Workday remains `FULL_CRAWL_ONLY`.  The current adapter requests 20-row CXS
offset pages, accepts an authoritative provider total, and uses the Phase 4
subdivision algorithm when a root query reaches the known 2,000-result safe
capacity.  It chooses exact facet partitions, validates child totals/filter
application, unions stable identities, rechecks the root, and fails closed on
an unsplittable partition, contradiction, mutation, depth/count/job/request
limit, or transient exhaustion.  That Phase 4 work makes previously capped
full inventory possible; it does not create an incremental partition.

The only normalized time is `postedOn`.  Repository code treats it as source
posting/display metadata and exposes no trustworthy update field, server-side
changed-since filter, opaque cursor, deterministic newest-first contract, or
separate detail hydration.  A first-page/postedOn slice could be positive-only
but cannot prove it contains edits to old requisitions and therefore has zero
defensible exact-equivalence savings.  Periodic full crawl would remain
necessary even if such a slice were later proven.  Workday's high cost merits
provider-contract monitoring, but it does **not** deserve the next deep review
without new deterministic endpoint evidence.

### Eightfold verdict and sibling precedent

Eightfold is `INSUFFICIENT_EVIDENCE`, not a high-value candidate.  Job Radar's
12 PCSX sources use complete `count` + 10-row offset inventory; nine cap at two
pages and three at one.  PCSX rows already carry title, locations, department,
description, stable URL/ID, and `postedTs`, so Job Radar has no secondary rich
detail request to avoid.  `postedTs` is normalized as a posting timestamp and
is not proven to change for every material edit.

The useful sibling concept is explicitly **adapt**, not reuse:
`jobseek/apps/crawler/src/core/monitors/eightfold.py` always fetches an
independently complete `/careers/sitemap.xml` URL set for lifecycle authority,
then uses `_pcsx.fetch_incremental(...)` only for enrichment; failures return
sitemap-only results without advancing the watermark.
`jobseek/apps/crawler/src/core/monitors/_pcsx.py` implements the bounded PCSX
watermark traversal, while
`jobseek/apps/crawler/src/core/scrapers/eightfold.py` performs separate JSON-LD
or apply-API detail enrichment.  Job Radar has no configured Eightfold sitemap
authority and no equivalent expensive secondary enrichment.  Adding one
sitemap request to save at most one PCSX listing request may increase traffic.
Sibling semantics therefore do not justify Stage C/D.

### Browser and static HTML verdicts

The four browser sources are Goldman Sachs, MediaTek, Nutrien, and Stantec.
MediaTek alone has a deterministic single-page terminal contract; the other
three retain positive discoveries but cannot grant absence authority.  The
collector already uses isolated browser contexts and delegates parsing to the
static adapter.  No deterministic plain-HTTP/API alternative or reviewed
ETag/Last-Modified/snapshot token is represented.  Source-bound render caching
would require config/provider identity, complete terminal evidence, checksum,
expiry, and fail-closed fallback.  That is Phase 7 infrastructure, not a Phase
6 activation.

There are no large-pagination production `static_html` sources: 24 of 27 cap
at one page; only BlackRock, Siemens, and Chevron cap at two.  Their terminal
evidence comes from reviewed static controls/totals/contracts.  No generic
newest-first ordering or trustworthy timestamp boundary exists.  An interim
positive-only first-page slice could save at most one request for three
sources, while absence still requires the full crawl; this is not material.

### Ranked shortlist and continuation decision

The shortlist ranks future evidence-gathering value, not activation readiness:

| Rank | Family | Sources | Cost model | Hypothesis | Theoretical saving | Risk / complexity / confidence | Recommended action |
|---:|---|---:|---|---|---|---|---|
| 1 | Ashby | 72 | 1 inventory + up to 20 details | complete inventory + selective hydration | up to `D-K` details | high correctness risk; medium complexity; low confidence | Reconsider only if Ashby documents a material posting version. |
| 2 | SAP SuccessFactors | 44 | up to 20 listing pages; columnized adds N details | variant-specific selective hydration or positive slice | potentially large | very high risk/complexity; low confidence | Require per-variant change/version evidence and Phase 4 identity first. |
| 3 | Workday | 108 | 20-row pages plus possible subdivision | positive-only filtered/ordered slice | potentially many pages | very high risk/complexity; low confidence | Do not deep-review again without a new provider endpoint/contract artifact. |
| 4 | ADP | 8 | bootstrap + pages + N Workforce Now details | selective detail hydration | up to N details | high risk; medium complexity; low confidence | Require a detail material version. |
| 5 | Phenom | 16 | up to 50 pages/500 jobs | positive-only pagination short circuit | potentially many pages | very high lifecycle risk; medium-high complexity; low confidence | Require deterministic order and edit-complete semantics. |

Eightfold is intentionally outside the top five after the architecture audit:
its current 1-2 request baseline and lack of separate enrichment make its
theoretical saving immaterial.

`NEXT_DEEP_REVIEW = NONE`

`PHASE6_CLOSE_EARLY`

The repository contains high configured costs, but no remaining adapter clears
both gates: a deterministic safe change boundary and materially non-zero
production savings.  Proceeding to Stage C/D would add checkpoint, fallback,
and lifecycle risk without a defensible optimized request model.  Phase 6
should close with its generic architecture and explicit disabled policies.
Rejected families may be reconsidered only after repository/provider evidence
proves a material version, edit-complete server filter/cursor/change feed,
reviewed complete-representation validator, or an independently complete
inventory that safely selects expensive hydration.

### Policy metadata and regression boundary

`src/scraper/company_careers/phase6_triage.py` is the canonical machine-readable
matrix.  It normalizes aliases, assigns one classification, records candidate
patterns and deterministic 0-5 scores, summarizes configured request ceilings,
and returns capability `none` for every family.  It is not imported by scanner
dispatch and activates nothing.  Tests lock the 533-source/41-family registry,
endpoint aliases, request-bound calculations, SmartRecruiters and Greenhouse
verdicts, disabled policies, and the production registry SHA-256.  No source
scope, retention, freshness, inventory/hydration, finalization, or persistence
code changed.

## Rollout plan

- **Stage A (this change):** generic architecture, schema, tests, documentation,
  and zero production activations.
- **Stage B:** one reviewed adapter canary with deterministic recorded fixtures,
  a source-specific forced-full interval, and no delete-event authority.
- **Stage C:** compare request/runtime savings and job/material equivalence to
  full crawls; audit every fallback and scheduled full reconciliation.
- **Stage D:** expand one adapter contract at a time only with equivalent proof.
- **Stage E:** periodically audit that full reconciliation cadence, protected
  identities, scope isolation, and retention separation have not weakened.
