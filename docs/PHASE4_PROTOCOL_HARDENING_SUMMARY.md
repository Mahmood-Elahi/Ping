# Phase 4 protocol hardening summary

Phase 4 is complete. All intentionally owned protocol risks have a safe
implementation, coupled-by-design boundary, explicit unsupported disposition,
or later-infrastructure handoff. No Phase 5 coverage work is required for this
conclusion.

| Provider | Identity contract | Completeness contract | Mutation | Partial progress | Remaining limitation |
|---|---|---|---|---|---|
| Workday | reviewed official employer-to-tenant/site proof plus runtime CXS binding | cap-aware facet partition tree with source-native totals | one whole-tree restart | safe completed partitions/valid listings only | canaries and additional reviewed identities |
| Dayforce | reviewed official tenant/board proof plus runtime shell/site-info and CSRF | raw total reconciles normalized, duplicate, rejected, and unreadable rows | one whole traversal restart | safe earlier pages and transient detail failures | current production employer bindings remain unproven |
| SAP SuccessFactors | reviewed exact RMK surface and runtime variant markers | header/schema-bound table/tile/columnized pagination | one whole traversal restart | safe earlier pages and columnized unreadable details | unmodeled RMK variants and identity review |
| ADP | runtime Workforce Now tenant/site bootstrap or MyJobs slug/org/client bootstrap | provider total, offset, stable page/ID evidence | one whole traversal restart on same source budget | MyJobs pages; Workforce Now pages and unreadable detail inventory | other ADP variants; several production employer bindings unproven |
| browser-rendered HTML | final HTTPS URL restricted to reviewed hosts plus rendered employer marker | provider/runtime terminal signal parsed by static pagination owner | one fresh browser traversal | completed stable pages followed by transient navigation failure | click workflows, access blocks, generic infinite scroll; three terminal-unproven production sources |

Central `finalize_source_scan` remains the only authority gate. Adapters emit
runtime evidence; configuration describes expected identity but does not itself
prove a live response. Budgets never count as terminal evidence.

Cooldown is limited to comparable transient transport/upstream failures.
Identity, configuration, schema, pagination contradiction, mutation exhaustion,
unsupported protocol, access block, and safety limits are non-transient.

Detailed reviews:

- [Workday](PHASE4_WORKDAY_PROTOCOL_REVIEW.md)
- [Dayforce and SAP SuccessFactors](PHASE4_DAYFORCE_SUCCESSFACTORS_REVIEW.md)
- [ADP and browser-rendered HTML](PHASE4_ADP_BROWSER_REVIEW.md)
