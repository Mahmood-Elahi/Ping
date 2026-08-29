# Configuration

RoleRadar's direct company-careers scanner is configured through `config/target_companies.yaml`.

The goal is to keep source metadata in configuration instead of burying company-specific details in one-off scripts.

## Basic Shape

```yaml
target_companies:
  - name: OpenAI
    platform: ashby
    slug: openai

  - name: Stripe
    platform: greenhouse
    slug: stripe
```

Every target needs:

- `name`: display name used in outputs
- `platform`: adapter key used by the scanner

Many platforms also need a `slug`, `tenant`, `site_id`, or source-specific search terms.

## Supported Platforms

Current platform keys:

- `greenhouse`
- `lever`
- `ashby`
- `workday`
- `dayforce`
- `amazon`
- `shopify`
- `google`
- `meta`
- `atlassian`
- `uber`
- `ibm_search`
- `manual_review`

`manual_review` is useful when a company matters but does not yet have a stable adapter. It keeps the coverage gap visible instead of silently excluding the company.

## Examples

Greenhouse:

```yaml
- name: Databricks
  platform: greenhouse
  slug: databricks
```

Ashby:

```yaml
- name: Confluent
  platform: ashby
  slug: confluent
```

Workday:

```yaml
- name: Adobe
  platform: workday
  tenant: adobe
  wd_subdomain: wd5
  site_id: external_experienced
```

Dayforce:

```yaml
- name: WestJet
  platform: dayforce
  tenant: WestJet
  site_id: CANDIDATEPORTAL
  locale: en-CA
  search_url: https://jobs.dayforcehcm.com/en-CA/WestJet/CANDIDATEPORTAL
  endpoint_url: https://jobs.dayforcehcm.com/api/geo/WestJet/jobposting/search
  endpoint_variant: nextjs_jobposting_search
  allowed_apply_hosts: [jobs.dayforcehcm.com]
  page_size: 25
  max_pages: 2
  max_jobs: 50
```

Dayforce uses an anonymous public board session: the adapter reads tenant and
board identity from the server-rendered shell, obtains its public CSRF token,
and then pages the JSON search endpoint in fixed 25-job increments.

Custom source with search terms:

```yaml
- name: Meta
  platform: meta
  max_pages: 3
  search_terms:
    - Principal Engineer
    - Staff Software Engineer
```

Manual review:

```yaml
- name: ExampleCo
  platform: manual_review
```

## Search Terms

Some adapters fetch broad job lists and rely on RoleRadar's runtime `keywords`.

Other adapters query a company search endpoint directly. For those, `search_terms` can improve recall:

```yaml
search_terms:
  - Principal Engineer
  - Staff Software Engineer
  - Senior Staff
```

If `search_terms` is omitted, some adapters use source-specific defaults. Review adapter defaults before assuming a scan is role-neutral.

## Page Limits

Some custom adapters support `max_pages`:

```yaml
- name: Google
  platform: google
  max_pages: 3
```

Use higher page limits when recall matters. Use lower page limits for faster smoke tests.

## Adding A Company

1. Identify the company's careers platform.
2. Add a target entry to `config/target_companies.yaml`.
3. Run a focused scan with a small `--max-jobs`.
4. Check the coverage report.
5. If the status is `failed_technical`, inspect the adapter assumptions.

Example:

```bash
python main.py search "Staff Software Engineer" \
  --locations "Remote" "United States" \
  --scrapers company_careers \
  --max-jobs 3
```

## Common Configuration Mistakes

- Wrong `slug` for Greenhouse, Lever, or Ashby.
- Wrong Workday `tenant`, `wd_subdomain`, or `site_id`.
- Overly narrow `search_terms` that miss relevant roles.
- Treating `fetched_no_matching_roles` as a failure. It means the source responded but the current role/location/relevance criteria did not match returned jobs.
