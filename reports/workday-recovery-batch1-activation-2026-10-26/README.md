# Workday recovery Batch 1 production activation proposal

This PR prepares the exact twenty-source activation requested on 2026-10-26.
Merge and deployment await separate approval. No source has been verified
complete in production by this change; the retained baseline is 371/533.
The approved single canary execution completed at 2026-10-26T21:23:39Z with
20/20 PASS_COMPLETE, 1,303 jobs accounted and 76/412 physical requests.
No further live requests were made to prepare this PR.

## Evidence and source boundaries

[The exact live proposal](activation_proposal.yaml), [approved canary manifest](manifest.json),
[preflight](preflight.json), [postflight](postflight.json) and
[execution report](execution_report.json) are byte-identical copies of
`output/ping_batch1_canaries/20261026T211759Z-bec088a8/`.
Their historical `activation_approved: false` flags describe that completed run,
not the later request to prepare this PR. Historical records remain unchanged.

[Per-source evidence](evidence.json) contains all twenty original numbered result
objects, their original filenames/SHA-256s and exact saved first-party ownership
observations with their Astra evidence references. The original observations
predate inventory validation; the separate result objects establish completion.
This compact public package retains exact historical identity provenance strings,
including `reports/ping-coverage-recovery-2026-10-25/VERIFIED_REPLACEMENTS.md`
anchors, without adding the entire unrelated 162-source investigation to this PR.
The full investigation remains preserved in the original audit checkout.
The approved manifest SHA-256 is
`9e4f501d72874fa7bbe973b0a189882f4d39981606369dad90bea0344124ae35`.

Each source keeps its canonical employer, default scope, exact tenant/shard/site,
provider job namespace and exact apply host. Solera's existing locationCountry
facet `a30a87ed25634629aa6c3958aa2b91ea` is unchanged. Raw legacy page/job
values stay unchanged; the production loader continues to expand them to the
same 25/500 limits explicitly used by the tested candidates. Bounds are never
terminal evidence. The actual effective production loader equals every tested
candidate, including its complete configuration and source-health fingerprint.
All other 513 raw AND effective company configurations are unchanged. No duplicate
canonical scopes or overlapping effective Workday recruiting boards exist.

## Before/after comparison

Nineteen raw rows gain the reviewed identity/default-scope fields; search URLs,
apply hosts and official careers paths are materialized only where the exact
proposal requires them. Lattice's raw identity was already present, but its stale
overlay stripped it at runtime. Removing only that overlay restores the exact
tested effective source. Whole-row replacement semantics remain unchanged.
The [complete field-level comparison](comparison.json) includes every prior/new
raw row. Tenant/site in the table is unchanged before and after.

| Company | Unchanged tenant/site | Raw fields changed / effective repair | Live jobs | Requests |
|---|---|---|---:|---:|
| Lattice Semiconductor | `latticesemi/latticesemiconductorscareers` | Raw row unchanged; remove stale whole-row overlay | 147 | 8 |
| Keyera | `keyera/Keyera_Careers` | `source_scope`, `workday_identity` | 6 | 1 |
| Ovintiv | `ovintiv/ovintivcareers` | `source_scope`, `workday_identity` | 3 | 1 |
| AESO (Alberta Electric System Operator) | `aeso/External` | `source_scope`, `workday_identity` | 6 | 1 |
| Vermilion Energy | `vermilionenergy/VEI_EXTERNAL` | `source_scope`, `workday_identity` | 5 | 1 |
| Whitecap Resources | `wcap/Whitecap_Careers` | `source_scope`, `workday_identity` | 8 | 1 |
| Strathcona Resources | `strathconaresources/Careers` | `source_scope`, `workday_identity` | 18 | 1 |
| Hydro Ottawa | `hydroottawa/hydro_ottawa_careersite` | `source_scope`, `search_url`, `workday_identity` | 8 | 1 |
| BCI | `bci/BCI_Careers` | `careers_url`, `source_scope`, `search_url`, `workday_identity` | 10 | 1 |
| Lumentum | `lumentum/LITE` | `source_scope`, `search_url`, `workday_identity` | 270 | 14 |
| NAV CANADA | `navcanada/NAV_Careers` | `source_scope`, `search_url`, `workday_identity` | 27 | 2 |
| ApplyBoard | `applyboard/ApplyBoardNH` | `source_scope`, `workday_identity` | 64 | 4 |
| Arctic Wolf | `arcticwolf/External` | `source_scope`, `workday_identity` | 111 | 6 |
| Proofpoint | `proofpoint/proofpointcareers` | `source_scope`, `search_url`, `workday_identity` | 130 | 7 |
| Allegro MicroSystems | `allegromicro/AllegroCareers` | `source_scope`, `workday_identity` | 89 | 5 |
| Bruce Power | `brucepower/BrucePower` | `source_scope`, `workday_identity` | 20 | 1 |
| Cineplex | `cineplex/Cineplex` | `source_scope`, `workday_identity` | 213 | 11 |
| Multimatic | `multimatic/MMEC` | `careers_url`, `source_scope`, `workday_identity` | 115 | 6 |
| Solera Holdings | `solera/Global_Career_Site` | `source_scope`, `workday_identity` | 4 | 1 |
| Enbridge | `enbridge/enbridge_careers` | `source_scope`, `search_url`, `allowed_apply_hosts`, `workday_identity` | 49 | 3 |

The only adapter change retains short/empty CXS responses without an authoritative
provider total as incomplete (`missing_authoritative_total`). No count is
synthesized. Explicit zero, pagination, duplicates, malformed records, mutation,
limits, and identity failures are covered independently for all twenty sources.
The finalizer, cooldown, source-health policies, two-observation absence policy,
lifecycle history and exact-only equivalence code are unchanged.

The four unrelated discovery-only overlay corrections from the earlier local
implementation are deliberately outside this PR; their existing uncommitted
work is preserved. The historical baseline overlay limitations for those four
and CN Rail/Kinross Gold remain separate work. This PR does not claim to resolve
those existing issues or to establish completeness for the other 513 sources.

## Exact rollback

The proposal's `rollback[company]` records each exact prior raw production row
and prior overlay (null means absent). They match Astra's original rollback.
[Baseline fingerprints](baseline.json) pin commit
`1295831cae96a6552a98b3687c76bdef2b835ef8`, original byte hashes, structural
hashes, untouched raw/effective rows and exact overlay ordering.
[activation.patch](activation.patch) is the exact forward patch for the two
production YAMLs, the Workday adapter and its existing regression test.
The patch uses zero context to avoid trailing-whitespace context records inside
the committed patch file. Reverse application with `--unidiff-zero` restores
those four files byte for byte, without reading
current settings as historical inputs or modifying retained evidence.

To inspect rollback applicability (offline, no changes):

```sh
git apply --unidiff-zero --reverse --check reports/workday-recovery-batch1-activation-2026-10-26/activation.patch
```

Only after an explicit rollback decision, the corresponding reverse apply without
`--check` restores the recorded baseline. Keep all evidence; never reset job IDs,
lifecycle history or absence counters. Restoring the prior adapter/overlay also
restores their documented defects. No rollback was executed in the actual PR tree.
Later unrelated changes must be preserved; a patch conflict requires review.

## Validation and approval

Run from the repository root in the locked Python environment:

```sh
python scripts/validate_workday_recovery_activation.py
python scripts/validate_source_integrity.py --strict-onboarding --retained-pattern __no_retained_coverage__
python -m pytest -q
git diff --check
```

The new validator is read-only and checks the immutable evidence hashes,
positive completion contract, all twenty effective configurations, exact rollback,
513 unchanged raw/effective rows, unique scopes and Workday ownership.
Older activation tests reconstruct their frozen historical registry only after
the full superseding package validates; their historical files are never edited.
[Validation results](validation.json) record the exact runs and environment.
[Independent review](REVIEW.md) records the review and its resolved test issue.

No workflow, canary, scan, database/Sheets write, merge or deployment was run.
After separate merge/deployment approval, only a later authoritative production
run can establish a coverage gain, at most twenty sources. Recovered absence
observations still follow the unchanged two-complete-observation rule.
