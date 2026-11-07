# Three recovered sources and production browser repair

Retained live canaries prove Adobe **278**, Workday **370**, and Lam Research
**1,487** complete jobs: **2,135** additional jobs, **4,245** across eleven
successful recoveries. No successful source was rescanned.

The proposal is based on `a81dd2de48fee26605adb9e34e8617b2c7316f29` (merged
PR #8). Only these three production rows change. The actual loader and student
preprocessing reproduce their exact tested targets; the other 530 effective
sources remain unchanged. Adobe/Workday receive reviewed identity bindings.
Lam receives its employer marker, apply host, tested enrichment setting and
an isolated, exact-contract parser opt-in. Other SuccessFactors sources keep
their existing dispatch and behavior. Bounds never prove completion.

[Evidence](evidence.json.gz.b64) contains redacted certificates, verified stable
identity digests, exact rollback rows/effective targets and predecessor runtime
bytes. Individual jobs and provider payloads remain private. The new validator
checks current applicability before composing historical eight-source and
ten-source checks. Historical locks and certificates remain unchanged.

Production [run #381](https://github.com/Mahmood-Elahi/Ping/actions/runs/42418305995)
failed before credentials/preflight/collection because Playwright rejected
Ubuntu 26.04. The workflow pins Ubuntu 24.04, separates browser caches by OS
version/architecture, and launches Chromium on an in-memory page before
credentials. Schedule, timeout and integration/logging boundaries are unchanged.
GitHub runner installation/launch and collection still require a post-merge
production run. **393 COMPLETE / 99 INCOMPLETE / 41 FAILED** remains the dated
baseline; no updated coverage is claimed.

ZF/Celestica remain excluded. Both failed at
`pagination_anchor / unsupported_query_keys`. Retained evidence lacks rejected
keys and anchor markup, so no locale/identity exemption is justified. Their
separate local diagnostic retest keeps 74/94-request caps and requires explicit
execution authorization. It cannot persist jobs or reconcile absence.

See [validation.txt](validation.txt). No live scans, production data writes,
merges, deployments or workflow dispatches occurred.
