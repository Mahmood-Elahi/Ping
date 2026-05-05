# Publishing Notes

These notes are for preparing RoleRadar for a public GitHub release or portfolio feature.

## Name

Use:

```text
RoleRadar
```

Recommended repository slug:

```text
role-radar
```

Why this works:

- short and easy to remember
- broad enough to support non-engineering role profiles later
- directly communicates role discovery
- leaves room for future alerts, dashboards, or relevance profiles

## Repository Description

Use a keyword-rich description:

```text
Config-driven job discovery across LinkedIn, Greenhouse, Ashby, Workday, and company career sites.
```

This carries the important search terms without overstating capability.

## GitHub Topics

Suggested topics:

```text
job-search
job-scraper
career-sites
linkedin
greenhouse
ashby
workday
job-automation
python
playwright
ats
career-tools
```

## Public Positioning

Use this concise description:

```text
RoleRadar is a target-company job discovery pipeline. It scans LinkedIn and company career systems, normalizes postings, ranks relevant roles, and writes coverage reports that show what matched, what returned no relevant roles, and what failed.
```

Avoid claiming:

- universal role matching
- guaranteed LinkedIn reliability
- real-time alerts unless they are added
- AI ranking unless the deterministic relevance layer is replaced or supplemented

## Good README Hooks

Strong:

```text
Most job-search workflows depend on LinkedIn alone or hide source failures.
```

Strong:

```text
Define target companies, run scans, review ranked jobs, and inspect source coverage.
```

Avoid:

```text
AI-powered job search revolution.
```

Avoid:

```text
Find every job automatically.
```

## Before Making Public

- Rename the repository to `role-radar`.
- Confirm the license and upstream attribution are correct.
- Remove private remotes, generated outputs, local notes, and candidate-specific files.
- Add screenshots or sample artifacts if available.
- Add GitHub topics from this file.
- Run the test suite and include a status badge only if CI exists.
