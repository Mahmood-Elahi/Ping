# Ping

_New roles. Less noise._

Ping is a target-company job-discovery system that scans LinkedIn and direct
employer career sites, normalizes postings, ranks relevant roles, and reports
source coverage separately from search results. That separation is deliberate:
an empty result is not treated as proof that an employer has no open roles.

The repository keeps its established technical identifiers—such as the
`role-radar` package/repository name, `job_radar_*` metadata, database objects,
environment variables, and workflow identifiers—to avoid breaking automation.
**Ping** is the user-facing product name.

## What Ping does

- Runs LinkedIn and direct company-careers acquisition from one pipeline.
- Dispatches a YAML registry to 41 active platform keys covering common ATSs
  and reviewed custom sources.
- Normalizes source records while retaining stable provider identity and source
  diagnostics.
- Separates source completion, role relevance, candidate preference,
  freshness, urgency, and lifecycle decisions.
- Writes ranked jobs plus explicit coverage and failure artifacts.
- Optionally persists student-role lifecycle state in Supabase and publishes
  managed Google Sheets views.
- Fails closed when source identity, scope, pagination, terminal evidence, or
  inventory accounting is ambiguous.

The checked-in production registry currently contains 533 canonical companies,
533 configured sources, and 533 logical source scopes. Those are configuration
counts, not a claim that all sources are currently reachable or complete. The
latest retained maintenance closeout parks 36 unresolved sources; see
[Roadmap](docs/ROADMAP.md) for the dated evidence boundary.

## Safety model

Every direct source produces observations; only the central finalizer decides
whether they prove a complete authoritative inventory. A request or page limit
is a safety bound, never terminal evidence. Partial and discovery-only runs may
add or refresh jobs, but they cannot advance absence-based lifecycle removal.

A source may be activated only after exact employer/source identity, source
scope, stable job identity, authoritative terminal evidence, complete inventory
accounting, deterministic fixtures, and a bounded non-persistent canary all
pass. `PASS_COMPLETE` is the only generally activation-eligible canary result.
See [Source Contracts](docs/SOURCE_CONTRACTS.md) before changing any source.

## Quick start

Python 3.11 is the repository baseline.

```powershell
git clone https://github.com/Mahmood-Elahi/Job-Radar.git
Set-Location Job-Radar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install uv
uv sync --locked --dev
python -m playwright install chromium
Copy-Item .env.example .env
```

On macOS or Linux, activate with `source .venv/bin/activate` and copy the
environment template with `cp .env.example .env`.

`pyproject.toml` is the dependency declaration and `uv.lock` is the
cross-platform lock. `requirements.txt` is a generated, pinned runtime export
retained for Docker and the existing GitHub Actions workflow; do not edit it by
hand.

Docker is optional infrastructure, not Ping's production execution path. The
scheduled GitHub Actions workflow runs Python directly, and no current workflow
or production integration requires a container image. The retained
`Dockerfile` and `.dockerignore` preserve a security-hardened build context, but
an actual image build has not been verified for this release.

Run a direct company-careers search:

```powershell
.\.venv\Scripts\python.exe main.py search "Software Engineer" `
  --locations "Canada" "Remote" `
  --scrapers company_careers `
  --max-jobs 5
```

Run both acquisition paths:

```powershell
.\.venv\Scripts\python.exe main.py search "Software Engineer" `
  --locations "Canada" "Remote" `
  --scrapers linkedin company_careers `
  --max-jobs 5
```

These search commands contact external sites. For a no-network dashboard
preflight, use:

```powershell
.\.venv\Scripts\python.exe scripts\update_student_dashboard.py --dry-run
```

## Configuration and credentials

Copy `.env.example` to `.env` and set only the integrations you use. Important
groups are:

- LinkedIn: `LINKEDIN_USERNAME`, `LINKEDIN_PASSWORD`, or the documented cookie
  and profile alternatives.
- Document generation: `GOOGLE_API_KEY`, `GPT_4O_API`, `GPT_4_1_API`.
- API: `API_KEY`, explicit comma-separated `ALLOWED_ORIGINS`; administrative
  JSON-to-SQLite migration additionally requires `ADMIN_API_KEY` and
  `ENABLE_API_MIGRATIONS=true`.
- Supabase: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
- Google Sheets: `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_PATH`.

Never commit credentials. The Supabase service-role key is backend-only.

The active direct-source registry is `config/target_companies.yaml`. Candidate,
canary, rollback, and historical validation registries are intentionally
separate and are not normal production inputs. Student relevance, industry,
freshness, and retention settings live under `config/` as named in
[Operations](docs/OPERATIONS.md).

## Outputs

Commands write generated artifacts under `jobs/` or `output/`, including:

- normalized job JSON;
- ranked JSON and CSV;
- coverage Markdown, CSV, and JSON;
- source failure and student-search diagnostic JSON; and
- optional generated application documents.

Coverage states distinguish successful matching, successful no-match,
incomplete/manual coverage, technical failure, cooldown skip, and parent-source
coverage. Always review coverage alongside ranked jobs.

## Student dashboard

The opt-in dashboard scans configured sources, ranks eligible student roles,
performs source-scoped staged synchronization to Supabase, and publishes the
managed Sheets tabs `New Jobs`, `Active Jobs`, `Companies`, `System Status`, and
six industry views. The manually maintained `Applications` tab is never
managed by Ping.

Apply `sql/supabase_schema.sql` for a clean database or the ordered migrations
listed in [Architecture](docs/ARCHITECTURE.md). Verify with `--dry-run` before
any external operation. Normal updates can mutate Supabase and Sheets; see the
operations safety table before running them.

## API

`python main_api.py` starts the legacy FastAPI surface. Interactive OpenAPI docs
are exposed at `/docs`. The implemented surface includes asynchronous search,
WebSocket progress, search history/results, and SQLite job queries. The
repository does not implement the aspirational parse/process endpoints that
appeared in older documentation.

## Project map

```text
main.py                                  CLI entry point
main_api.py                              FastAPI entry point
config/target_companies.yaml             production source registry
src/scraper/company_careers/             scanner, contracts, lifecycle, ranking
src/scraper/company_careers/adapters/    ATS and custom adapters
src/scraper/search/linkedin_scraper/     Playwright LinkedIn implementation
src/integrations/                        Supabase and Google Sheets integrations
scripts/                                 audits, validators, replay, operations
sql/                                     bootstrap schema and migrations
tests/                                   deterministic regression fixtures
docs/                                    four authoritative supplementary docs
```

## Authoritative documentation

- [Architecture](docs/ARCHITECTURE.md) — data flow, components, persistence,
  lifecycle, and incremental-crawl design.
- [Source Contracts](docs/SOURCE_CONTRACTS.md) — mandatory identity, scope,
  completeness, activation, rollback, and retirement rules.
- [Operations](docs/OPERATIONS.md) — setup, configuration, commands, outputs,
  dashboard operation, and deterministic verification.
- [Roadmap](docs/ROADMAP.md) — the preserved seven-phase roadmap, historical
  validation record, recoveries, and unresolved work.

Codex sessions must also follow [AGENTS.md](AGENTS.md).

## Attribution

Ping began as an extension of the open-source
[JobSearch-Agent](https://github.com/sreekar2858/JobSearch-Agent) project by
`sreekar2858`, through the upstream RoleRadar project. This repository retains
that attribution and the inherited GNU General Public License v3.0 in
[LICENSE](LICENSE); package metadata identifies it as `GPL-3.0-only`. No
third-party material is relicensed by this statement.
