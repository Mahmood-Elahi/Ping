# RoleRadar Documentation

This directory explains how RoleRadar is structured, configured, and operated.

Start with the main [README](../README.md) for the project story and quick start.

## Core Docs

- [Architecture](ARCHITECTURE.md): pipeline flow, adapter model, data model, and extension points
- [Configuration](CONFIGURATION.md): how to add companies and tune source behavior
- [Outputs](OUTPUTS.md): generated files, schemas, and coverage states
- [Relevance](RELEVANCE.md): ranking behavior and how to adapt it beyond technical roles
- [Candidate Profile v1](CANDIDATE_PROFILE.md): deterministic student-job preferences, output signals, and replay
- [Freshness / Urgency v1](FRESHNESS.md): lifecycle state, source dates, separate urgency signals, and replay
- [Attribution](ATTRIBUTION.md): upstream credit and what changed
- [Publishing Notes](PUBLISHING.md): public-facing name, description, and topic guidance

## Existing Reference Docs

- [LinkedIn Scraper Guide](LINKEDIN_SCRAPER.md): LinkedIn-specific scraper usage
- [API Reference](API.md): FastAPI endpoints and WebSocket behavior
- [Advanced Configuration](ADVANCED_CONFIGURATION.md): broader runtime and deployment settings
- [Testing Guide](TESTING.md): verification commands and test guidance

## Reading Paths

For users:

1. Read the main [README](../README.md).
2. Copy `.env.example` to `.env`.
3. Edit `config/target_companies.yaml`.
4. Read [Outputs](OUTPUTS.md) before reviewing generated files.

For contributors:

1. Read [Architecture](ARCHITECTURE.md).
2. Read [Configuration](CONFIGURATION.md).
3. Add or update an adapter under `src/scraper/company_careers/adapters/`.
4. Add tests in `tests/test_company_careers.py`.

For public positioning:

1. Use the project name `RoleRadar`.
2. Use the keyword-rich repository description from the main README.
3. Keep attribution to the upstream `JobSearch-Agent` project visible.
