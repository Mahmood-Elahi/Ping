# Attribution

RoleRadar started from the open-source [JobSearch-Agent](https://github.com/sreekar2858/JobSearch-Agent) project by `sreekar2858`.

The upstream project provided the base job-search automation structure, LinkedIn scraping foundation, AI document-generation flows, and general project layout.

## What This Version Adds

This version extends the base with:

- config-driven direct company-careers scanning
- target-company registry in `config/target_companies.yaml`
- reusable `src/scraper/company_careers/` scanner package
- ATS and custom adapters for Greenhouse, Lever, Ashby, Workday, Amazon, Shopify, Google, Meta, Atlassian, Uber, and IBM-backed search
- coverage states for every configured company
- Markdown, CSV, JSON, and failure reporting artifacts
- deterministic relevance ranking with reasons and penalties
- CLI/pipeline support for `--scrapers company_careers`
- tests for scanner behavior, adapters, coverage reporting, and ranking

## Positioning

RoleRadar should be described as an extension and evolution of the original project, not as a from-scratch rewrite.

Suggested wording:

```text
RoleRadar started from the open-source JobSearch-Agent project and extends it with a config-driven company-careers scanner, ATS/custom adapters, relevance ranking, and transparent coverage reporting.
```

## License Note

Keep the upstream license and attribution intact. If the project is published publicly, verify the final license file and README attribution before release.
