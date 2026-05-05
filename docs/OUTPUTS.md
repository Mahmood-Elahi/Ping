# Outputs

RoleRadar writes runtime artifacts that are meant for review, debugging, and follow-up. Output directories are ignored by git because these files are generated and often contain search-specific data.

## Main Job Outputs

`job_postings_*.json`

Normalized jobs that matched the scan criteria. Use this when feeding jobs into downstream processing or document generation.

Typical fields:

- `job_id`
- `title`
- `company`
- `location`
- `url`
- `updated_at`
- `source`
- `departments`
- `offices`
- `description`

`company_careers_ranked_*.json`

Matched jobs with relevance metadata added:

- `relevance_score`
- `relevance_band`
- `relevance_reasons`
- `relevance_penalties`

`company_careers_ranked_*.csv`

Spreadsheet-friendly version of the ranked jobs. This is usually the easiest artifact to review manually.

## Coverage Outputs

`company_careers_coverage_*.md`

Readable coverage summary. Use this first when you want to understand the scan result at a source level.

`company_careers_coverage_*.csv`

Tabular coverage summary for spreadsheet review.

`company_careers_coverage_*.json`

Structured coverage state for automation or debugging.

`company_careers_failures_*.json`

Focused failure report. This separates technical and manual follow-up from normal no-match cases.

## Coverage Statuses

`fetched`

The source was scanned and returned at least one matching job.

`fetched_no_matching_roles`

The source was scanned successfully but returned no jobs matching the current search, location, and relevance criteria. This is not a scanner failure.

`manual_review_required`

The source is intentionally marked for manual follow-up.

`not_implemented`

The target's configured platform does not have a registered adapter.

`failed_technical`

The adapter, request, parser, or upstream source failed.

## Review Order

For a human review pass:

1. Open `company_careers_coverage_*.md`.
2. Check `company_careers_failures_*.json`.
3. Review `company_careers_ranked_*.csv`.
4. Use `job_postings_*.json` for downstream automation.

## Interpreting No-Match Results

No-match results are useful. They tell you a source was reachable and the scan did not find relevant jobs under the current criteria.

Common causes:

- the company has no matching open role
- location filters are too strict
- search terms are too narrow
- relevance scoring is tuned for a different role family
- the adapter fetched only the first page or a limited result set
