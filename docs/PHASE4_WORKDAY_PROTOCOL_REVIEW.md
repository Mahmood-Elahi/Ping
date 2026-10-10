# Phase 4 Batch 1: Workday protocol review

Date: 2026-10-09. Scope: deterministic/local Workday protocol hardening only.
No live career-site request, production scan, database mutation, migration,
workflow trigger, branch change, commit, or push was performed.

## Before and after

Before this batch, the adapter posted 20-row CXS searches to
`https://{tenant}.{wd_subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`,
advanced offsets, accepted either a stable reported total or a short page as
terminal evidence, and stopped at adaptive request bounds. It supported one
preconfigured exact facet and checked that facet's CXS echo. It never fetched
job details: listing `externalPath`, `bulletFields`, `locationsText`, and
`postedOn` were enough to normalize a job. Stable identity preferred a
requisition-like bullet value corroborated by the detail-path suffix, then the
path token. The normalized apply/detail URL used `/en-US/{site}{externalPath}`.

That traversal did not distinguish a Workday provider cap from an ordinary
total and recorded identity as `unproven`; central finalization did not treat
that unproven status as a contradiction. Therefore a CXS result could supply
absence authority without independent employer binding, and a boundary total
could be mistaken for the whole inventory.

After this batch, totals below 2,000 retain the existing listing path. A root
total at or above 2,000 is a provider-cap signal and can complete only through
the partition algorithm below. All Workday completion, including zero, is also
gated by the exact identity contract. The adapter now returns explicit
`InventoryItem`s, including partial discoveries from failed partition trees.

## Identity contract

Reachability is not identity. `workday_identity` is the smallest accepted
configuration proof:

```yaml
workday_identity:
  official_source_url: https://careers.example.com/jobs
  expected_workday_tenant: example
  expected_workday_site: External
  expected_workday_hostname: example.wd1.myworkdayjobs.com  # optional
```

The official URL must be first-party HTTPS and must not itself be a
`myworkdayjobs.com` URL. Tenant and site must exactly match the resolved source;
hostname must also match when configured. The entire block is included in the
source/config health fingerprint. Explicit malformed or mismatched evidence
fails before CXS traversal. Missing evidence permits positive discovery but
forces `workday_identity_unproven`, incomplete inventory, and zero absence
authority. CXS titles/descriptions, tenant-name similarity, and Workday shell
metadata are never employer proof. The decision is evaluated once for the
resolved site; no identity request is repeated inside partitions.

## Cap and partition semantics

`SAFE_QUERY_CAPACITY` is 2,000. A reported total of 1,999 may complete normally.
Totals of exactly 2,000, 2,001, 2,500, and 4,000+ enter partitioning. Empty or
repeated offsets at/after 2,000 are never terminal evidence.

Only CXS-advertised `facetParameter` and opaque value `id` pairs are used. A
candidate dimension needs at least two positive child counts, each smaller
than the parent, and aggregate advertised coverage at least as large as the
parent. Selected facets are sent in `appliedFacets`; every child response must
echo each selected value with a count exactly equal to that child total.

The traversal recursively splits every cap-bound child. A leaf is eligible
only when its validated total is strictly below 2,000. Every leaf is paginated
to its exact total. Results are unioned by case-insensitive, source-owned stable
Workday ID. Identical cross-partition overlap is counted and deduplicated;
identity-bearing path conflicts fail closed. Completion requires all leaves,
a stable root total and first-page identity recheck, and a unique union count
exactly equal to the root total. The sum of leaf totals is diagnostic only and
may exceed the unique count.

## Guardrails and mutation

- Maximum depth: 3.
- Maximum partition states: 128, including root.
- Maximum unique Workday jobs: 10,000.
- Default source-wide request budget: 500; configurable maximum: 2,000.
- Repeated canonical facet state: `workday_duplicate_partition_state`, never
  terminal evidence.
- Cap-bound child without another safe advertised dimension:
  `workday_unsplittable_partition`.
- Depth, count, job, or request-budget exhaustion is incomplete and has no
  absence authority.

The one budget covers site/identity discovery, root/facet discovery, partition
listing pages, root validation, and a mutation replacement traversal. A changed
root total/signature, changed child total, or conflicting stable identity marks
the tree mutable. The adapter restarts the whole partition traversal at most
once, discards first-traversal jobs, and never unions traversals. A second
mutation stands alone as incomplete.

## Failure, health, cooldown, and partial progress

Partition transport failures and transient HTTP 408/425/429/5xx responses
retain discovered inventory and participate in the Phase 3 transient cooldown
policy. Identity failure, identity unproven, provider cap, unsplittable
partition, partition contradiction, repeated state, and safety bounds are
deterministic/non-transient and do not advance cooldown streaks. If one leaf
fails, discovered jobs and inventory IDs remain useful positive progress, but
`inventory_complete=false` and global absence authority is zero.

## Known Phase 4 targets

These are deterministic repository-evidence fixtures, not activated Workday
sources. No hostname or identity evidence was invented.

| Company | Recorded tenant/site | Deterministic status |
|---|---|---|
| Applied Materials | `amat/External` | Identity ambiguous; Workday/Eightfold relationship remains unproven |
| BlackRock | `blackrock/BlackRock_Professional` | Identity unproven; professional and early-career scopes remain distinct |
| Caterpillar | `cat/CaterpillarCareers` | Identity unproven; Progress Rail remains separate |
| Chevron | `chevron/jobs`; distinct `University` site | Identity unproven; scopes must not be merged |
| General Motors | `generalmotors/careers_gm` | Identity unproven; subsidiary boards remain separate |
| Zalando | `zalando/ZalandoSiteWD` | Identity unproven; prior >500 evidence exercises risk, not proof of a 2,000 boundary |

## Canary registry and budgets

No Phase 4 canary registry was created. None of the current production Workday
entries contains the new exact official-source identity evidence, and the six
known targets lack sufficient repository evidence for a safe hostname-bound
activation. Therefore there are no defensible per-company root/partition/detail
budgets yet. Future canaries must be opt-in and calculate `1 root + all leaf
pages + 1 root validation + bounded retry allowance`; detail requests remain
zero because the listing contract supplies normalization and stable identity.

## Production Workday audit

All 108 active-registry Workday entries have an exact configured tenant/site
but no `workday_identity` block. They are therefore `IDENTITY_UNPROVEN` now;
none can produce absence authority. Offline repository evidence does not prove
which current inventories reach 2,000, so cap status is deliberately recorded
as not established rather than guessed.

| Company | Tenant/site | Classification | Identity status | Cap status | Next action |
|---|---|---|---|---|---|
| Adobe | adobe/external_experienced | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| CrowdStrike | crowdstrike/crowdstrikecareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Nvidia | nvidia/NVIDIAExternalCareerSite | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| PayPal | paypal/jobs | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Salesforce | salesforce/External_Career_Site | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Boston Dynamics | bostondynamics/Boston_Dynamics | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Cadence Design Systems | cadence/External_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Blue Origin | blueorigin/BlueOrigin | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Enbridge | enbridge/enbridge_careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Medtronic | medtronic/MedtronicCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Shell | shell/shellcareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Samsung Electronics | sec/Samsung_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Visa | visa/Visa | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Rockwell Automation | rockwellautomation/External_Rockwell_Automation | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| GE Vernova | gevernova/Vernova_ExternalSite | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Valeo | valeo/valeo_jobs | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| PTC | ptc/PTC | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| TC Energy | tcenergy/CAREER_SITE_TC | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| ConocoPhillips | conocophillips/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Ovintiv | ovintiv/ovintivcareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Keyera | keyera/Keyera_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Sierra Space | snc/Sierra_Space_External_Career_Site | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Lattice Semiconductor | latticesemi/latticesemiconductorscareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| bp | bpinternational/bpCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Intel | intel/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| NXP Semiconductors | nxp/careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| KLA | kla/Search | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Capital One | capitalone/Capital_One | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Unity | unitytech/Unity | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Broadcom | broadcom/External_Career | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| JD.com | jd/Campus_Career_Site | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Analog Devices | analogdevices/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Microchip Technology | microchiphr/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Magna International | magna/Magna | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Autodesk | autodesk/Ext | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Marvell Technology | marvell/MarvellCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Sony | sonyglobal/SonyGlobalCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Suncor Energy | suncor/Suncor_External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| ENMAX | enmax/ENMAXCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| AESO (Alberta Electric System Operator) | aeso/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Cenovus Energy | cenovus/careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| AltaGas | wgl/AltaGas | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Whitecap Resources | wcap/Whitecap_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Vermilion Energy | vermilionenergy/VEI_EXTERNAL | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| BlackBerry/QNX | bb/QNX | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| CAE Inc. | cae/career | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Clio | clio/ClioCareerSite | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Silicon Labs | silabs/SiliconlabsCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Strathcona Resources | strathconaresources/Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Semtech | semtech/SemtechCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Aptiv | aptiv/APTIV_CAREERS | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| RBC | rbc/RBCGLOBAL1 | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| TD Bank | td/TD_Bank_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| CIBC | cibc/campus | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Thomson Reuters | thomsonreuters/External_Career_Site | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Sun Life | sunlife/Campus | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Ciena | ciena/Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| ApplyBoard | applyboard/ApplyBoardNH | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Arctic Wolf | arcticwolf/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Canadian Tire | canadiantirecorporation/Enterprise_External_Careers_Site | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| OMERS | omers/OMERS_External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Bruce Power | brucepower/BrucePower | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Multimatic | multimatic/MMEC | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Zebra Technologies | zebra/Zebra_careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Allegro MicroSystems | allegromicro/AllegroCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Target Tech | target/targetcareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Weir Group | weir/Weir_External_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Cineplex | cineplex/Cineplex | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Helm Operations | volarisgroup/HelmOperations | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Solera Holdings | solera/Global_Career_Site | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Logitech | logitech/Logitech | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Sonos | sonos/Sonos | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Bose | boseallaboutme/Bose_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Symbotic | symbotic/Symbotic | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Allstate Canada | allstate/allstate_careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Lumentum | lumentum/LITE | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Workday | workday/Workday | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Hydro Ottawa | hydroottawa/hydro_ottawa_careersite | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| NAV CANADA | navcanada/NAV_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Hitachi | hitachi/hitachi | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Intact Financial | intactfc/intactfc | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Interac | interac/Interac | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Finning | finning/External | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Trimble | trimble/TrimbleCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Schweitzer Engineering Laboratories | selinc/SEL | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Proofpoint | proofpoint/proofpointcareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Calix | calix/external | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Johnson Electric | johnsonelectric/Career_JE | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| IKO | iko/IKO_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Johnson & Johnson | jj/JJ | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Dow | dow/ExternalCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| CSA Group | csagroup/csagroup | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Bird Construction | bird/BirdConstructionCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Roche | roche/roche-ext | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| McKesson | mckesson/External_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Sanofi | sanofi/SanofiCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Ledcor | ledcor/ledcor_external | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| AstraZeneca | astrazeneca/Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Dragonfly | intelcomgroup/Intelcom | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| HOOPP | hoopp/HOOPPStudent | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| SOTI | soti/SOTI-Next-Gen | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| BCI | bci/BCI_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Ontario Teachers' Pension Plan | otppb/OntarioTeachers_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| PSP Investments | investpsp/psp_careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| AIMCo | aimco/AIMCoCareers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| Siemens Healthineers | onehealthineers/SHSJB | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| EXFO | exfo/EXFO_Careers | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |
| EllisDon | ellisdon/Ellis-Don | IDENTITY_UNPROVEN | no configured proof | not established | bind official source; canary |

## Deterministic tests

The dedicated fixture suite covers 1,999; 2,000; 2,001; 2,500; 4,001 with
overlap; duplicate identities; missing and transient partitions; unsplittable
children; depth, count, job, and request bounds; contradictory child totals;
root mutation and one restart; identity preflight failure; authoritative zero;
identity caching; repeated canonical state; partial inventory items; and refusal
to probe offset 2,000 as terminal proof. The known-company YAML fixture records
Applied Materials, BlackRock, Caterpillar, Chevron, General Motors, and Zalando
without creating a runnable registry.

Final deterministic results: 2,256 tests passed, 902 subtests passed, zero
failures. The Workday-focused command passed 49 tests (68 deselected). The
focused Phase 1 command passed 21 tests and 27 subtests, covering Waymo plus the
seven activated ATS migrations (ASML, SharkNinja, Box, Redis, Ripple, Miro,
Wise).

## Manual commands (not executed)

No canary command is currently safe because there is no prepared registry. Once
reviewed identity blocks and deterministic facet evidence exist, the intended
PowerShell shape is:

```powershell
$registry = "config/target_companies.phase4_workday_canaries.yaml"
.\.venv\Scripts\python.exe scripts\validate_source_integrity_canaries.py `
  --registry $registry `
  --company "<exact company>" `
  --request-budget <calculated-hard-budget> `
  --run
```

Do not run that shape until the registry exists and its per-company budget is
reviewed. Normal production runtime must never load the future canary file.
