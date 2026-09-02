# Freshness / Urgency v1

This layer records job lifecycle facts and derives deterministic timing signals. It does not use an LLM, decide candidate fit, modify relevance or candidate-preference scores, or reorder ranked results. A future application-priority queue can consume these signals alongside fit, relevance, and resume match.

## Lifecycle persistence and identity

Production lifecycle history lives in the existing Supabase `tracked_jobs` table. There is no second lifecycle datastore. The scan loads prior rows, reconciles all normalized observations, annotates ranked output, and synchronizes normalized and ranked rows through source-scoped `sync_student_job_source` transactions. Each source's observations and qualifying absence reconciliation remain atomic, and an idempotency ledger makes interrupted requests safe to retry.

Stable identity uses the existing order: namespaced source job ID, normalized canonical URL, then a deterministic SHA-256 fallback.

- `first_seen_at` is set on first observation and never overwritten for the same identity.
- `last_seen_at` advances on every successful re-observation.
- `source_posted_at` and `source_closes_at` are retained only when a bounded parser accepts explicit source evidence.
- `active` becomes `possibly_missing` after one qualifying absence and `removed` after two by default.
- reappearance restores `active`, clears the missing count, advances `last_seen_at`, and preserves `first_seen_at`, `removed_at`, and reappearance history.
- a passed explicit deadline or explicit source status such as `closed`, `expired`, `inactive`, `removed`, or `filled` closes immediately.

### Exact absence-removal policy

Absence only counts when that company's result is `fetched` or `fetched_no_matching_roles` **and** the normalized result count stayed below the configured source cap. Reaching a cap is conservatively treated as partial. Failed, manual, unsupported, capped, or otherwise incomplete scans do not increment missing evidence. The default requires two consecutive qualifying absences. This protects against network failures, pagination changes, source caps, and scraper regressions.

The SQL RPC receives only companies meeting this completeness rule, so persistence enforces the same policy as in-memory reconciliation.

## Dates and time zones

Lifecycle timestamps are timezone-aware and stored in UTC. Naive reference timestamps are rejected. Date-only employer values remain `YYYY-MM-DD`; posting dates use UTC midnight for deterministic age, while closing dates use calendar-day semantics (`today = 0`, `tomorrow = 1`) and are considered passed only on the next UTC date.

Accepted source forms are ISO dates/timestamps, `August 28, 2026`, `Aug 28, 2026`, US `08/28/2026`, and explicit posted-relative values such as `Posted 3 days ago`. Unknown, malformed, naive, or future posting dates remain unknown and are listed in `date_parse_errors` and replay/scan diagnostics. They are never silently coerced.

Posting-date extraction is generic over recognized metadata keys. Workday, Lever, and Ashby currently expose representative trusted `source_posted_at` metadata. Explicitly worded application deadlines in structured metadata or descriptions are supported; ambiguous prose is not.

Effective posting age uses reliable `source_posted_at`, otherwise `first_seen_at`. `freshness_age_source` makes that choice visible.

## Exact freshness rules

Thresholds are in `config/freshness.yaml`; upper bounds are inclusive.

| Elapsed age | `freshness_band` |
|---|---|
| `<= 24 hours` | `new` |
| `> 24 hours` and `<= 3 days` | `fresh` |
| `> 3 days` and `<= 10 days` | `normal` |
| `> 10 days` and `<= 21 days` | `aging` |
| `> 21 days` | `stale` |

Outputs also expose `age_days`, `first_seen_age_hours`, `freshness_reasons`, and both underlying timestamps.

## Exact urgency rules

Rules are evaluated in this order, so an old posting with an imminent explicit deadline remains urgent.

1. `closed`: lifecycle is reliably removed/closed or an explicit deadline has passed.
2. `apply_now`: active with an explicit closing date/timestamp no more than 2 days away.
3. `high`: active with an explicit deadline no more than 7 days away, or first seen no more than 48 hours ago.
4. `low`: active and `aging`/`stale` (or older than the configured 21-day low threshold) without a more urgent deadline.
5. `normal`: any other active posting.

`urgency_score` is an inspectable convenience value (`100/75/50/25/0` for `apply_now/high/normal/low/closed`). It is not added to ranking. `urgency_reasons` contains deterministic codes such as `deadline_in_1_days`, `first_seen_6_hours_ago`, `posting_age_24_days`, `deadline_passed`, and `confirmed_removed`.

## Outputs and dashboard

Ranked JSON/CSV, the canonical ranked-job model, and Supabase records retain the full metadata set: ranking bands and categories, eligibility/location status, lifecycle status, first/last-seen and source timestamps, posting/closing dates, posting and discovery age, freshness, urgency and reason codes, removal time, parse errors, and source diagnostics. This metadata remains available for ranking, filtering, deduplication, replay, diagnostics, and future product features.

The production `New Jobs` and `Active Jobs` Sheets tabs intentionally project those rich records into eight decision-relevant columns: `Score`, `Company`, `Position`, `Location`, `Freshness`, `Urgency`, `Why`, and `Apply`. Freshness and urgency remain separate; deadline and lifecycle inputs remain in the urgency engine even though they are not displayed. The dashboard continues to publish only active ranked rows; removed and non-ranked normalized jobs remain in `tracked_jobs` history.

## Replay and backfill

Run:

```powershell
.\.venv\Scripts\python.exe scripts\replay_freshness_v1.py
```

Optional `--source`, `--as-of`, `--json-output`, and `--markdown-output` arguments make replay explicit and deterministic. Reports are written to `output/freshness_v1_replay.json` and `.md` by default.

The band-coverage portion is an intentional deterministic fixture subset: it
shapes 10 current rows from the first 12 source rows so every freshness and
urgency band, malformed-date handling, reappearance, and absence removal are
exercised. The same report separately annotates every row in the full source
artifact, reports lossless coverage/timestamp/lifecycle distributions, and
performs an automated before/after ranking equality check.

Historical artifacts do not contain trustworthy discovery timestamps. Backfilled rows therefore use the explicit replay reference time and `first_seen_basis: backfilled_at_reference_time`. This is deliberately not presented as the actual historical discovery time. Normal new observations use `observed_at_scan`; read-only audit rows use `audit_reference_time` and are not persisted.

Apply `sql/supabase_schema.sql` before a production run. Existing database `first_seen_at` values are preserved by the upsert.

## Limitations

- Adapter coverage for source posting/closing dates is incremental; unknown is preferred to a guess.
- Completeness uses the conservative source-cap signal available in the current scanner. A future adapter can provide stronger pagination-completion evidence without changing removal semantics.
- Date-only values use documented UTC calendar semantics because the source supplied no timezone.
- Resume match and the combined application-priority/action queue are intentionally out of scope. Downstream logic should read relevance, candidate preference, freshness, and urgency as separate fields.
