# Job Radar Company Careers Recovery Batch 10

Date: 2026-10-07 (America/Edmonton)

## Final scope and outcome

Batch 10 keeps the production changes whose deterministic contracts are
supported and leaves the remaining candidates at their pre-Batch-10 effective
configuration. No live scan was run while preparing this final working tree.

| Company | Final repository outcome | Status |
|---|---|---|
| Dyson | Keep the reviewed same-origin public JSON recovery and its bounded enumeration contract. | Production recovery retained. |
| Roblox | Keep the existing production source. It also benefits from the reusable public JSON integrity reporting fix. | Existing production source retained; no new activation. |
| Chemelex | Keep the reviewed four-page/100-job Dayforce bound and the bounded enumeration and diagnostic improvements. | Production configuration improvement retained; malformed enumerated records still fail completeness. |
| Wise | Restore the prior one-page/20-job static configuration, with no reviewed pagination contract and no pagination URL template. | Production activation not shipped; the live canary returned `FAIL_CONTRACT` because source identity and uniqueness evidence were not proven. |
| Box | Keep the prior one-page static configuration with no reviewed pagination contract. | Pagination activation not shipped. |
| Espressif Systems | Keep the prior one-page static configuration with no reviewed pagination contract. | Pagination activation not shipped. |
| Cameco | Keep the prior static configuration. | No migration shipped. |

## Retained reusable changes

The following fixes are adapter or validation infrastructure behavior, not
company-specific production activation:

- Public JSON integrity metadata reports unique stable IDs, unique canonical
  URLs, and bounded duplicate evidence. Duplicate or otherwise inconsistent
  counted collections continue to fail closed.
- Dayforce diagnostics expose bounded rejected-record and duplicate posting or
  requisition identifiers without relaxing identity, normalization,
  pagination, or completeness rules.
- Static HTML visible-total parsing remains generic. Conflicting visible totals
  are reported as conflicting pagination evidence and do not establish a
  complete scan.
- Deterministic static HTML and listing-completion fixes remain covered by
  adapter and completion regressions.

## Company-specific retained work

### Dyson

Dyson uses the reviewed `public_json` source at the official careers host. The
configuration binds the official search page to its same-origin jobs endpoint,
uses bounded pagination, and requires stable identity and canonical URL
integrity. Deterministic tests cover authoritative-total enumeration, official
endpoint binding, URL normalization, and duplicate-ID failure.

### Roblox

Roblox remains on its existing reviewed public JSON source. Batch 10 does not
claim a new Roblox activation. Its deterministic regression confirms the
existing atomic feed remains complete only when the official source binding is
present and that the generic integrity metadata is populated.

### Chemelex

Chemelex remains on the existing Dayforce adapter with `max_pages: 4` and
`max_jobs: 100`. The bound covers the supplied 68-row validation population.
The retained diagnostics account separately for malformed records and
duplicate requisitions; they do not turn either condition into completeness.

The supplied 68-row result reconciles as:

```text
68 enumerated source postings
- 1 identity-validated posting rejected for missing structured location
- 1 posting skipped because its requisition ID had already been seen
= 66 normalized jobs
```

## Non-shipping candidates

### Wise

Wise's effective production configuration is restored to its pre-Batch-10
state in `config/target_companies.yaml`, and the Batch 10 Wise entry is absent
from the reliability overlay. The generic visible-total parsing fix is retained
and tested with a synthetic static HTML target; that test does not assert that
Wise is production-recovered.

### Box and Espressif Systems

Both remain on their prior bounded static configurations. Neither has a
reviewed pagination contract or an activated next-page selector in the final
effective configuration.

### Cameco

Cameco remains at its prior static configuration. No Jobvite or
SuccessFactors migration is part of Batch 10.

## Files in Batch 10

- `config/target_companies.yaml` — retained Dyson and Chemelex production
  configuration changes; Wise, Box, Espressif Systems, and Cameco remain at
  their prior effective configurations.
- `config/company_careers_reliability_repairs_v1.yaml` — retained Dyson overlay
  entry; no Wise activation entry.
- `scripts/validate_source_integrity_canaries.py` — reusable bounded integrity
  and Dayforce diagnostic reporting.
- `src/scraper/company_careers/adapters/dayforce.py` — bounded duplicate and
  rejected-record diagnostics.
- `src/scraper/company_careers/adapters/public_json.py` — Dyson support and
  reusable uniqueness/integrity reporting.
- `src/scraper/company_careers/adapters/static_html.py` and
  `src/scraper/company_careers/listing_completion.py` — deterministic generic
  parsing and completion fixes.
- Batch 10 and focused adapter, completion, and integrity tests — deterministic
  coverage for the retained behavior and final production configuration.

The pre-existing unrelated files and Batch 9 artifacts identified by the
operator are not part of Batch 10 and were preserved.

## Deterministic verification

The final working tree was verified with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Results:

- Full deterministic suite: 2,170 passed, 869 subtests passed, and one existing
  `LangChainDeprecationWarning` from `src/agents/search_agents_adk.py:131`.
- `git diff --check`: passed; Git emitted only CRLF normalization notices for
  existing modified files.

These commands do not perform live source scans. A passing deterministic suite
does not promote Wise, Box, Espressif Systems, or Cameco.

## Repository safety

- No live source validation, source probe, or registry scan.
- No dashboard, run-history, lifecycle, or external-system mutation.
- No staging, commit, push, merge, or GitHub Actions trigger.
