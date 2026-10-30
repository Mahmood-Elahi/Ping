# Runtime admission and remaining external latency risk

Admission is **conditional**, not an unconditional45minute guarantee. No provider or database request was made to measure external latency in this preparation. The workflow, its45minute hard timeout and six source workers remain unchanged.

Inputs are reproducible from [the retained run351 public extract](run_351_public_extract.json), [runtime assessment](runtime_assessment.json), certificate worker times and [the fresh in-memory orchestration measurement](downstream_revalidation.json). The original baseline reference is retained separately in the runtime record.

- Baseline whole-job upper estimate1723seconds already includes1140.695source seconds plus582.305seconds of downstream stages/workflow overhead. Baseline public job time1639.856seconds is retained separately.
- Add the sum122.345seconds of the ten successful canary workers, without subtracting the old failed attempts or dividing by six. Add180seconds reserve:2025.345seconds (33m45.345s), with674.655seconds headroom.
- The real local staged-sync implementation with an explicit in-memory client accounts for520records,31RPC operations,604810serialized bytes and ten finalizations. Socket connection attempts are denied; zero external requests/writes. This measures client orchestration/protocol volume, not server execution.
- Assign twice the historical synchronization mean per minimum baseline operation to31additional operations:31.274seconds, plus measured client CPU. Whole-job scenario approximately2057seconds (34m17s), with about643seconds headroom, including the180second reserve.
- An independent stress scenario doubles all baseline non-source cost and assumes all520jobs survive ranking:2645.344seconds (44m5.344s), leaving54.656seconds. Combining that stress with the separate31operation latency scenario is approximately2677seconds, leaving only23seconds. That combined scenario can overlap costs; it is a conservative stress illustration, not an upper bound or percentile estimate.

Unmeasured factors include request-tail latency, rate limiting/retries, existing database state/cardinality, server execution, dashboard retrieval, Sheets response time and future inventory growth. The historical synchronization mean is not a latency distribution. A request timeout does not cap the whole job's aggregate latency. The180second reserve cannot guarantee external service availability.

The five collateral identity-impact costs are removed by preserving baseline behavior for those sources. All other523 configurations/dispatch remain unchanged, so this PR does not silently add a shared parser enumeration workload elsewhere.

Final merge/activation requires explicit acceptance of this residual external-latency risk or separately approved nonmutating external measurements with their own bounds. No such measurement is authorized now. Do not extend the current ten-source admission to the remaining eight: their source-worker times, inventory sizes and downstream increment must be reassessed only after approved PASS_COMPLETE canaries. No timeout, retry, completeness, cooldown or lifecycle contract is relaxed to fit the model.
