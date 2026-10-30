# Ten verified SuccessFactors recoveries — activation PR

This PR proposes the ten independently verified sources: Keyence, TransAlta, Spartan Controls, ARC Resources, Gibson Energy, SaskPower, Hydro One, Omron, Canada Life and Hyundai. Their retained PASS_COMPLETE certificates account for520jobs and27physical requests. No successful canary was repeated. No new provider request, production scan, workflow trigger, database/Sheets write, merge or deployment was performed to prepare this PR.

Only eight production rows change: seven exact reviewed `identity_regex` additions and Hyundai's exact reviewed `identity_regex`/apply host. Gibson Energy and Hydro One already have the exact fields and recover through the parser change. [Raw before/after rows](raw_comparison.json) preserve all legacy operational defaults. All other523 raw and effective configurations remain exact. Source scope, requisition IDs, authoritative range/total/terminal accounting, mutation rejection, central finalization and two-observation absence remain unchanged.

The six runtime files are byte-identical to the independently reviewed consolidated implementation. An explicit exact-contract dispatcher opts these ten effective production targets into the repaired table parser. All other523 sources retain baseline dispatch. Festo, Volkswagen Group, TELUS, Hatch and VIA Rail therefore retain their original production implementation without invented identity markers. The sealed baseline adapter is byte-identical to production84b1655. The reserved candidate profiles and two locale self-anchor guards do not activate any other source; the two guard board URLs are unreachable for the certified ten.

[Code applicability](code_applicability.json), [source specifications](canary_sources.json), [certificate index](certificates.json) and [Astra extracts](astra_evidence.json) preserve the exact evidence. Historical Astra activation-status text remains historical; the dated live certificates supply subsequent completion proof. No full original investigation was rewritten or copied wholesale into this PR.

The offline validator verifies:

- all ten original certificate hashes, source-spec hashes, exact provider/host/identity/default-scope configuration and accounting;
- all533 real-loader effective targets and unique logical scopes, with10reviewed/523baseline dispatch;
- unchanged nonactivation configuration and production dependencies;
- equality of the live-validated adapter AST after removing only the explicit dispatcher and two locale self-anchor additions that cannot apply to these ten boards;
- immutable source/code/evidence/rollback hashes.

Deterministic tests additionally compare actual results, request traces and failure metadata against the retained live-validated engine and the production baseline. They cover positive, zero, continuation, missing totals, malformed/rejected records, repeated/conflicting IDs, limits, mutation, foreign destinations and identity failures. [Validation](VALIDATION.md) records the exact test scope, exclusions and initial failures.

The prior Workday history validator requires one narrowly scoped compatibility change: validate this entire newer activation against actual production, require the supplied registry payload to equal actual, reconstruct the exact prior registry from the ten rollback rows, then run the unchanged historical Workday artifact/hashes and actual Workday runtime checks. The test-only historical migration map admits only the eight locked after-rows. No production loader uses this projection, and no historical evidence hash was altered.

[Runtime and external latency risk](LATENCY_RISK.md): admission is conditional, with an approximately34m17s normal model against45minutes. Network/server tail latency has not been measured or bounded. Explicit acceptance of that residual risk, or separately approved external measurement, is required at the final merge/activation decision. No workflow timeout/concurrency adjustment is proposed.

[Independent activation review](REVIEW.md).

[Exact rollback](ROLLBACK.md) covers all ten sources, including the two code-only recoveries. Configuration-only reversal is insufficient for Gibson Energy/Hydro One.

Commands from the PR checkout:

```sh
env -u PYTHONPATH PYTHONWARNINGS=ignore LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python scripts/validate_source_integrity.py --strict-onboarding
env -u PYTHONPATH PYTHONWARNINGS=ignore LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python scripts/validate_successfactors_ten_activation.py
env -u PYTHONPATH PYTHONWARNINGS=ignore LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q
env -u PYTHONPATH PYTHONWARNINGS=ignore LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python scripts/benchmark_successfactors_downstream.py
```

The separately preserved eight-source manifest remains at `reports/successfactors-consolidated-2026-10-28/coordinated_canary_manifest.json` in the audit workspace, SHA256 `5f95bb6e27100d8eaa750a99a8ec82307617700a081542133067c2c5179c2e24`. It is outside this production activation diff, requires new approval, and must execute only from its own protected consolidation checkout. FortisBC and John Deere remain excluded pending missing evidence. No canary execution is authorized by this PR.

The last authoritative production result remains388/533. Potential gain is at most ten if other sources remain healthy. No production coverage increase is claimed until an authorized subsequent production run establishes authoritative completion.
