# Recovery validation follow-up

The approved campaign used 179 physical requests. Its result and SQLite checkpoint agree on all 16 attempted sources. The four deferred entries had no request starts or attempt reservations. The campaign checkpoint stays in the original PR #13 checkout. [Campaign PR](https://github.com/Mahmood-Elahi/Ping/pull/13) · [Activation results](ACTIVATION.md)

## Five failure dispositions

| Source | Retained evidence | Implementation and required evidence |
|---|---|---|
| Deliveroo | Ten HTTP 200 pages returned 198 jobs, 198 inventory items, 198 distinct IDs and URLs, an authoritative total, and validated identity. Strict certification also required the adapter's explicit collection integrity record. | Publish and enforce that record after the complete reviewed WordPress traversal. Tests reproduce 198 jobs and reject a duplicate canonical URL. The checkpoint discarded the rejected attempt's inventory pairs, so the isolated 16-page candidate still needs a fresh approved PASS_COMPLETE. The production registry retains nine pages. |
| Atlassian | An HTTP 200 atomic array had 224 entries. A posting failed the exact iCIMS host and stable-ID path check. The adapter error was classified as transient. | Classify this deterministic failure as identity failure. Capture the row number, numeric ID and sanitized URL field shape. Obtain the exact first-party ID and approved portal fixture for the rejected row before extending the allowed URL forms. |
| Hexagon / NovAtel | Two HTTP 200 responses yielded nine valid jobs and an indexed short-page probe without terminal proof. | Record the exact-index, container, explicit-zero and continuation checks. A retained approved terminal HTML sample or a provider total is needed. |
| SECURE Energy Services | Two HTTP 200 responses yielded 50 valid jobs, followed by an indexed page without reviewed terminal evidence. | Record the same probe checks. A reviewed range or portal-bound empty marker is needed before authorizing a 50-job inventory. |
| ATCO | Nine HTTP 200 pages reported 88 jobs. Requisition P-100020, with Phenom sequence AGZAGAGLOBALP100020, had no official apply URL. There were 87 valid normalized jobs. | Obtain the employer-approved link for that exact posting, or an independently reviewed detail-route contract linking the Phenom sequence to the same requisition and employer. Inventory remains incomplete until the rejected record is accounted for. |

The iCIMS changes add evidence without changing the terminal rule. Four HTTP 429 responses established that ENMAX, Ovintiv, Enbridge and Cenovus Energy hit provider restrictions during the campaign. The wire ledger recorded the following local hold deadlines. It stores the computed deadline, which can include the 60-second fallback when the provider omitted Retry-After.

| Source | Captured hold through UTC on November 26 |
|---|---|
| ENMAX | 06:30:20.488439 |
| Ovintiv | 06:30:25.815337 |
| Enbridge | 06:30:31.212602 |
| Cenovus Energy | 06:30:42.015723 |

Run #422 independently measured Ovintiv and Cenovus COMPLETE. ENMAX and Enbridge still need restored access and a separately approved check. Passing the captured hold time alone grants no access. The existing checkpoint skips all four attempted sources.

## Resume the four unused reservations

| Source | Observed persisted cooldown end on November 26 (UTC) | Remaining physical cap |
|---|---|---:|
| Capital One | 16:16:26.248030 | 52 |
| Solera Holdings | 08:48:54.669452 | 12 |
| Suncor Energy | 08:48:54.669452 | 12 |
| Bruce Power | 08:48:54.669452 | 12 |

Authorize a fresh read-only UTC export using the reviewed SQL in PR #13. Those timestamps describe the campaign snapshot and a later production run may have extended them. Once the fresh export and resume are authorized, run the following from the original PR #13 checkout that holds the existing checkpoint. The runner skips all prior reservations and attempts only these four sources when eligible, for at most 88 additional requests. Preserve the existing lock, SQLite ledger and results.

```sh
python scripts/run_recovery_validation.py \
  --approve-and-run f68e002b9e2dd7e35ee29eca9cca7bbf37998c6f11dfbf73eb210c20c7be2f82 \
  --health-export /absolute/path/to/fresh-authorized-source-health.csv
```

A later Deliveroo retry belongs in its own approval using the corrected adapter and the isolated candidate. The completed checkpoint is authoritative for prior attempts and remains separate from that verification.
