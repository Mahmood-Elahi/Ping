# Job Radar Batch 9 source-recovery implementation

Date: 2026-10-01 (America/Edmonton)

## Outcome

This pass adds `scripts/probe_batch9_recovery.py`, a bounded, read-only source
diagnostic for exactly Bosch, SGS, General Motors, Caterpillar, and Coupang.
No live request was executed during implementation.

No production configuration change is justified by the saved evidence. The
October 1 audit proves the current blockers, but no cached body proves a new
filter, pagination URL, structured endpoint, or terminal contract. The three
historic `?page=` templates are evidence of a disproven contract, not evidence
for a replacement.

| Company | Existing path reused | October 1 blocker | Deterministic change | Status |
|---|---|---|---|---|
| Bosch | SmartRecruiters v1 validation, totals, identities, canonical URLs | 4,814 advertised jobs exceed the reviewed 1,000-job/detail ceiling | Bounded offset and detail-evidence probe | Not yet repaired; live evidence required |
| SGS | Same SmartRecruiters path | 4,604 advertised jobs exceed the ceiling | Bounded offset and detail-evidence probe | Not yet repaired; live evidence required |
| General Motors | Static HTML selectors, record normalization, pagination evidence | The page advertises continuation; the removed `?page=` template repeated page 1 | Follow only one explicit same-host continuation from the current response | Not yet repaired; live evidence required |
| Caterpillar | Same static HTML path | Same disproven template and missing terminal proof | Same bounded source-advertised continuation probe | Not yet repaired; live evidence required |
| Coupang | Static HTML repeated-page and stable-ID checks | Configured second page repeats page 1 | Compare source-advertised pages and stop on repetition | Not yet repaired; live evidence required |

## Probe safety contract

- The command requires at least one exact `--company` choice from a five-name
  allowlist. It cannot select the registry or a group such as `all`.
- `--request-limit COMPANY=N` is enforced per company, including redirects and
  the optional SmartRecruiters detail sample. The hard maximum is eight.
- Only anonymous HTTPS `GET` requests are allowed. Source hosts are fixed from
  the effective target. Credential-bearing requests and disabled TLS
  verification fail closed.
- Static pagination follows only one unambiguous, source-advertised, same-host
  next link. Structured endpoint candidates are recorded but never requested.
- SmartRecruiters uses its existing target validator, listing parser, company
  identity checks, stable-ID checks, and detail normalization. It sends no
  unreviewed server-side filter.
- CAPTCHA, WAF, authentication, malformed, oversized, duplicate, contradictory,
  and missing-terminal conditions remain failures.
- Artifacts are compact JSON summaries. They contain no request headers,
  cookies, or response bodies. Secret-like query values are redacted.
- Every run gets a new directory below `output/batch9_recovery/`; files are
  opened in exclusive-create mode and cannot overwrite existing artifacts.
- The utility does not import or invoke dashboard, Supabase, Google Sheets,
  lifecycle, or full-scan entry points.

## Manual commands

Run these from `C:\Users\User\projects\job-radar`:

```powershell
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --company "Bosch" --request-limit "Bosch=4" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --company "SGS" --request-limit "SGS=4" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --company "General Motors" --request-limit "General Motors=3" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --company "Caterpillar" --request-limit "Caterpillar=3" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --company "Coupang" --request-limit "Coupang=3" --timeout 15
```

Each command prints its newly created run directory, for example:

```text
output\batch9_recovery\20261001T210000Z_a1b2c3
```

That directory contains `manifest.json`, `<company>.diagnostic.json`, and
`<company>.fixture.json`. A successful diagnostic is evidence for review, not
an automatic production repair or completion claim.

## Evidence captured

For SmartRecruiters the utility records up to three 100-row offset responses
and one corroborating detail response within the four-request command budget.
It captures echoed limits/offsets, advertised totals, stable IDs, duplicates,
company identity, publication-related fields, canonical detail/apply identity,
terminal status, and exact failure reason. It explicitly records that no
server-side filter is trusted yet.

For static sources it records each requested/final URL, redirect outcome,
status, source-advertised next links, normalized IDs and canonical URLs,
duplicates, collisions, rejected records, pagination evidence, terminal
evidence, collection mutation, identity evidence, and unverified same-host
structured endpoint candidates. It never calls those candidate endpoints.

## Reuse potential

The hard-capped session, compact fixture format, redirect/host enforcement, and
production-parser replay are reusable patterns for the remaining Batch 9
sources. The executable allowlist intentionally remains limited to these five;
adding another source requires an explicit reviewed probe plan.

## Deterministic verification

`tests/test_batch9_probe.py` covers valid pagination, repeated pages,
contradictory totals, duplicate IDs, malformed responses, correct and incorrect
identity, explicit and missing terminal evidence, collection mutation,
canonical collisions, host/TLS restrictions, request ceilings, the explicit
allowlist, and non-overwriting artifacts. Existing
`tests/test_batch9_recovery.py` remains unchanged and retains the October 1
source-integrity baseline.

## October 4 recovery continuation

The saved live artifacts were reviewed without issuing another live request.
Their exact reusable evidence is:

- Bosch: three listing pages reported `totalFound=4813`; all 300 IDs were
  unique, the company identity and one detail sample (`744000153041729`) were
  validated, and the collection stopped without reaching the authoritative
  total.
- SGS: three listing pages reported `totalFound=4608`; all 300 IDs were unique,
  the company identity and one detail sample (`744000153037915`) were
  validated, and the collection stopped without reaching the authoritative
  total.
- General Motors, Caterpillar, and Coupang: page 2 repeated all 20 page-1 IDs.
  Each run therefore retrieved 40 rows but only 20 unique IDs. Identity remained
  unproven and no authoritative terminal was observed.

The earlier static diagnostics saved XML-looking strings as regex candidates.
They did not preserve the originating HTML element or a response body. Thus the
saved strings cannot by themselves prove that the source linked a feed. In
particular, the malformed General Motors string ending in `Jobs RSS feed for
General Motors` demonstrates why candidate extraction cannot be promoted to a
collection contract.

### XML/RSS inspection mode

`--mode xml` is limited to General Motors, Caterpillar, and Coupang. It first
loads the configured listing and accepts feed URLs only from actual same-host
HTTPS `<a href>` or `<link href>` elements. Generic strings and inferred URL
templates remain unverified and are never requested.

The mode:

- allows no more than two XML HTTP transactions per company, including XML
  redirects;
- retains the existing overall request, redirect, host, anonymous-GET, TLS,
  and 15-second timeout controls;
- stops reading each XML response at exactly 2,000,000 bytes;
- rejects DTD/entity declarations and parses with entity resolution, DTD
  loading, and network access disabled;
- inspects RSS, RSS/RDF, and Atom structure, IDs, canonical URLs, publication
  fields, source identity, continuation links, totals, duplicates, collisions,
  mutation, and apparent truncation;
- never treats the absence of a `next` link as proof of an atomic feed; and
- writes schema-version-2 sanitized diagnostics to a new exclusive directory,
  while leaving production configuration unchanged.

There is no existing production XML/RSS job adapter or shared feed parser in
Job Radar. The new diagnostic reuses the bounded HTTP session, static HTML
parser, URL sanitizer, identity/integrity concepts, and artifact writer. It is
deliberately diagnostic-only until a captured feed proves attribution and
authoritative completeness.

Safe manual commands (not executed during implementation):

```powershell
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode xml --company "General Motors" --request-limit "General Motors=3" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode xml --company "Caterpillar" --request-limit "Caterpillar=3" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode xml --company "Coupang" --request-limit "Coupang=3" --timeout 15
```

The commands may fail closed before two XML responses if a redirect consumes
the overall ceiling. Increase only the per-company overall ceiling, never the
hard XML phase ceiling, if a reviewed same-host redirect requires it.

### SmartRecruiters population-filter mode

The official Posting API documents `country` and `department` filters, as well
as `limit`, `offset`, and authoritative `totalFound` pagination:
`https://developers.smartrecruiters.com/reference/v1listpostings`.

`--mode smartrecruiters-partition` tests exactly one documented population at
a time. It reads an unfiltered 100-row anchor, refuses a filter value not
observed in that current anchor, enumerates the filtered population within the
company request ceiling, then repeats the unfiltered anchor. A partition union
is invalid if anchor totals or IDs changed, a partition total conflicts, IDs
overlap, an observed population is missing, absent dimension values are not
covered, or the aggregate does not equal the baseline total. Detail enrichment
is explicitly not requested by this mode.

The current fixtures establish only partial observed country samples: Bosch
includes `au, br, cl, cn, cr, de, mx, pe, pt, us, vn`; SGS includes `ca, cl,
co, ec, gb, pe, us`. They do not establish exhaustive country sets, contain no
saved department IDs, and provide no facet/list-values contract. Consequently,
documented filtering is technically usable for a bounded partition probe but
is not yet a practical proof of exhaustive Bosch or SGS enumeration. Unseen or
missing dimension values remain blockers, and results from inconsistent
anchors must never be unioned.

Safe first partition probes, using values observed in the saved fixtures (not
executed during implementation):

```powershell
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode smartrecruiters-partition --company "Bosch" --population-filter "country=de" --request-limit "Bosch=8" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode smartrecruiters-partition --company "SGS" --population-filter "country=us" --request-limit "SGS=8" --timeout 15
```

These commands diagnose one partition; they cannot recover either source or
prove that the chosen dimension exhausts the global population.

## October 4 extended evidence and bounded continuation

`batch9_extended_results.zip` was reviewed locally. No request was sent while
implementing this continuation.

- General Motors, Caterpillar, and Coupang each reached its source-linked HTTPS
  XML-looking endpoint with HTTP 200, then crossed the old 2,000,000-byte cap.
  The old exception path retained neither MIME/length metadata nor a prefix, so
  HTTP 200 remains no evidence that any response was XML.
- Bosch's stable bracketing anchors reported a global `totalFound` of 4,835.
  The `country=de` response consistently reported 720 and yielded 600 unique
  IDs before the eight-request ceiling; the partition is partial.
- SGS's stable bracketing anchors reported a global `totalFound` of 4,619.
  `country=us` consistently reported 346 and yielded all 346 unique IDs with
  authoritative-total terminal evidence. This is a completed U.S. partition,
  not global completion.
- The current unfiltered anchor samples exposed 26 Bosch country codes and 24
  SGS country codes. Those samples are seeds for manual probes, never an
  official or exhaustive country list.

### Metadata-first XML inspection

XML mode now reads at most 4,096 initial bytes before deciding whether a body
is a supported RSS, Atom, or RDF feed. Its diagnostic records status,
Content-Type, parsed and raw Content-Length, a short normalized prefix sample,
the prefix hash, classification, effective limit, and whether the complete
body was downloaded. HTML, JSON, unknown markup, empty responses, and XML with
DTD/entity declarations fail closed. A MIME type or HTTP 200 never confirms a
feed.

The default remains 2,000,000 bytes. An explicit company-scoped
`--xml-response-limit COMPANY=BYTES` is accepted only after the response prefix
confirms a supported XML-feed root, and is hard-capped at 8,000,000 bytes. The
stream is closed as soon as Content-Length or bytes read exceed the effective
limit. The two-request XML phase limit, redirect counting, approved-host rule,
safe parser configuration, and non-activation rule remain unchanged.

Run one metadata-first probe at a time:

```powershell
$run = .\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode xml --company "General Motors" --request-limit "General Motors=3" --timeout 15
$diagnostic = Get-Content (Join-Path $run "general_motors.diagnostic.json") -Raw | ConvertFrom-Json
$diagnostic | Select-Object status,failure_reason
$diagnostic.xml_response_inspections | Select-Object content_type,content_length,content_classification,initial_bytes_read,complete_body_downloaded
```

Repeat by substituting Caterpillar / `caterpillar.diagnostic.json` or Coupang /
`coupang.diagnostic.json`. Only after a human confirms
`content_classification=confirmed_xml_feed`, use the reported exact length when
it is within the hard cap:

```powershell
$bytes = [int64]$diagnostic.xml_response_inspections[-1].content_length
if ($bytes -gt 2000000 -and $bytes -le 8000000) {
  .\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode xml --company "General Motors" --request-limit "General Motors=3" --xml-response-limit "General Motors=$bytes" --timeout 15
}
```

If Content-Length is absent, a separately reviewed 4,000,000-byte attempt is
the smallest useful next step; do not jump directly beyond the 8 MB hard cap.

Activation requires parsed job records with stable IDs, approved canonical job
URLs, company identity from feed metadata and records, no rejected/duplicate or
colliding records, and authoritative completeness (a consistent total reached,
or an equally strong documented continuation/terminal contract). A missing
`next` link alone is insufficient. Stop pursuing this XML route when the body
is non-XML, exceeds 8 MB, repeatedly truncates or mutates, lacks attributable
records, or offers no authoritative completeness contract; resume only if the
source publishes a smaller first-party feed or documentation that resolves the
specific blocker.

### Resumable SmartRecruiters country partitions

Partition mode still sends only one reviewed country filter per invocation.
Every run brackets that partition with the same unfiltered offset-zero anchor,
validates the partition's advertised total and terminality, and now checks the
country value returned on every newly captured row. Missing or unexpected row
countries invalidate the partition.

`--resume-from` accepts one immutable incomplete diagnostic. Before requesting
the next offset it validates company, mode, filter, contiguous offsets, stable
IDs, totals, and matching saved anchors, then compares a fresh anchor with the
saved snapshot. It skips completed offsets only after those checks. A changed
snapshot makes no partition request, and an already completed partition is
never requested again. For example, after extracting the supplied archive:

```powershell
$evidence = Join-Path $PWD "output\batch9_extended_evidence"
Expand-Archive -LiteralPath .\batch9_extended_results.zip -DestinationPath $evidence
$boschDe = Join-Path $evidence "20261005T000605Z_6e8a18\bosch.diagnostic.json"
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode smartrecruiters-partition --company "Bosch" --population-filter "country=de" --resume-from $boschDe --request-limit "Bosch=4" --timeout 15
```

The four-request ceiling permits one fresh anchor, offsets 600 and 700, and one
final anchor. The supplied SGS U.S. artifact is already terminal and must be
kept for reconciliation rather than passed to `--resume-from`.

For another country, make one bounded probe and review it before choosing the
next country; do not put the observed country samples into an automatic loop:

```powershell
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode smartrecruiters-partition --company "Bosch" --population-filter "country=ar" --request-limit "Bosch=8" --timeout 15
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode smartrecruiters-partition --company "SGS" --population-filter "country=ae" --request-limit "SGS=8" --timeout 15
```

`smartrecruiters-reconcile` is offline-only. It reads repeated immutable
`--partition-artifact` inputs and rejects mixed companies/dimensions, duplicate
country artifacts, partial partitions, changed anchor snapshots, contradictory
global totals, missing observed populations, absent/unexpected country values,
legacy artifacts without row-level country validation, and cross-partition ID
overlap. It declares global coverage only when the non-overlapping unique-ID
union exactly equals one stable global authoritative total:

```powershell
.\.venv\Scripts\python.exe scripts\probe_batch9_recovery.py --mode smartrecruiters-reconcile --company "Bosch" --partition-artifact .\path\bosch-ca.diagnostic.json --partition-artifact .\path\bosch-us.diagnostic.json
```

The exact-total equality is the completeness proof; the observed country-code
set is explicitly recorded as non-exhaustive. If a reviewed set of country
partitions sums below the global total, coverage is still unknown and remains
failed. Do not manufacture missing country codes or merge different anchor
signatures.

Bosch or SGS is ready for a production recovery only after every used country
partition is terminal and row-validated, all artifacts share the same stable
global anchor and total, cross-partition IDs are disjoint, and the exact unique
union equals that global total. Production detail retrieval must also retain a
separate bounded and validated budget. Further country work is no longer
worthwhile when anchors keep changing faster than a complete evidence set can
be gathered, credible country partitions still leave an unexplained remainder,
filters return absent/unexpected countries, or the request/detail cost cannot
fit a reviewed production ceiling.
