# Waymo Greenhouse production source

Waymo's production entry in `config/target_companies.yaml` uses the strict
Greenhouse adapter. The temporary one-entry candidate registry was removed
after its bounded manual canary passed and the source was activated.

## Evidence and source contract

The read-only `jobseek/apps/crawler/data/boards.csv` reference registry contains
the row `waymo,waymo-greenhouse,https://job-boards.greenhouse.io/waymo,greenhouse`
with token `waymo`; its `scraper_type` is `skip`, so jobseek consumes the rich
Greenhouse monitor output without a second detail scraper. This was structural
evidence for the candidate before live validation.

Two bounded manual canaries established the current first-party URL identity.
The Greenhouse listing endpoint
`https://boards-api.greenhouse.io/v1/boards/waymo/jobs` returned API row ID
`8234553` with this `absolute_url`:

```text
https://careers.withwaymo.com/jobs?gh_jid=8234553
```

The production source explicitly opts into the reusable Greenhouse
query-stable-ID contract. `apply_url_template` is the opt-in: without it, the
adapter continues to require the canonical
`https://<allowed-host>/<board>/jobs/<same-api-id>` shape. With it, the template
must itself contain HTTPS, an allow-listed hostname, one exact non-root path,
and exactly one safe query parameter whose entire value is `{id}`.

For every returned Waymo row the adapter requires all of the following:

- the listing request still uses the configured `waymo` Greenhouse board token;
- the returned URL is exactly HTTPS on `careers.withwaymo.com` with no port or
  user information;
- the path is exactly `/jobs`;
- the raw query is exactly `gh_jid=<API-row-id>`, occurring once and with no
  other parameters;
- the row ID is non-empty and numeric, and the `gh_jid` value equals it exactly;
- no fragment is present.

The active production configuration is:

```yaml
- name: Waymo
  platform: greenhouse
  slug: waymo
  endpoint_variant: boards_api_v1_strict
  careers_url: https://careers.withwaymo.com/jobs/search
  search_url: https://job-boards.greenhouse.io/waymo
  original_workbook_rank: 12
  allowed_apply_hosts:
    - careers.withwaymo.com
  apply_url_template: https://careers.withwaymo.com/jobs?gh_jid={id}
  max_pages: 1
  max_jobs: 10000
  max_response_bytes: 3000000
```

## Activation evidence

The manually executed bounded live canary passed with `PASS_COMPLETE` on
2026-10-07. It collected all 375 jobs reported by the endpoint with 375 unique
stable IDs and canonical URLs, no rejected records, no URL identity failure,
no malformed or conflicting pagination evidence, no collection change, and no
safety-limit stop. The terminal evidence was `complete_collection_response`;
the run used its configured 21-request budget but did not terminate because of
that budget. Source health was `healthy` and source identity was `validated`.

A failed URL contract continues to publish one bounded
`greenhouse_url_identity_failure` object with the offending ID and URL, expected
board/host/path/query parameter, actual host/path, bounded query-parameter names,
stable-ID correlation evidence, and named violations. It never includes the
complete API payload or job description.

## Rollback

If parity fails after activation, replace the single active Waymo mapping with
this prior production block. Do not leave both sources active:

```yaml
- name: Waymo
  platform: static_html
  careers_url: https://careers.withwaymo.com/jobs/search
  listing_url: https://careers.withwaymo.com/jobs/search
  endpoint_variant: server_rendered_cards
  original_workbook_rank: 12
  allowed_apply_hosts:
    - careers.withwaymo.com
  max_pages: 1
  max_jobs: 20
  max_scripts: 20
  max_response_bytes: 3000000
  max_script_bytes: 1000000
  evidence_source: config/company_remaining_inventory_v2.csv
  evidence_confidence: medium
  evidence_timestamp: '2026-08-05T01:24:24+00:00'
  listing_selector: article.job-search-results-card-col
  job_card_selector: article.job-search-results-card-col
  title_selector: h3.job-search-results-card-title a
  detail_link_selector: h3.job-search-results-card-title a
  external_id_regex: /jobs/([^/?#]+)
  external_id_attribute: null
  location_selector: .job-search-results-card-location
  department_selector: null
  pagination_url_template: https://careers.withwaymo.com/jobs/search?page={page}
  next_page_selector: null
  allow_url_id_fallback: true
```
