# Relevance

RoleRadar separates source coverage from role relevance.

The student workflow can optionally apply [Candidate Profile v1](CANDIDATE_PROFILE.md) after role relevance. Candidate preferences augment the relevance model; they do not replace it or assess resume qualifications.

[Freshness / Urgency v1](FRESHNESS.md) is a separate factual layer. It does not change relevance, candidate-preference scores, or current ordering; downstream application-priority logic can consume it independently.

The scanner can fetch broad job records from company career systems. Relevance scoring decides which fetched jobs count as matches and how they should be ranked for review.

## Current Default

The current relevance model in `src/scraper/company_careers/relevance.py` is deterministic and rule-based.

It currently favors senior technical IC roles, especially titles and contexts containing terms such as:

- staff
- principal
- senior staff
- software engineer
- backend
- platform
- infrastructure
- distributed systems
- SRE / reliability
- data engineering
- ML / AI engineering

It currently penalizes or excludes titles such as:

- sales engineer
- support engineer
- technical account manager
- engineering manager
- director / VP
- product manager
- program manager
- recruiting
- finance
- legal

This behavior reflects the first workflow RoleRadar was built for. It is not a limitation of the adapter architecture.

## How Matching Works Today

For company-careers scans:

1. Adapters fetch jobs.
2. Jobs are normalized into `JobRecord`.
3. The scanner applies keyword and location criteria.
4. If keywords are present, `is_relevant_job(...)` scores the job.
5. Jobs above the relevance threshold are included as matches.

That means matching is not a plain substring search today. Keywords matter, but relevance scoring can include or exclude jobs based on seniority, role-family signals, and penalties.

## Relevance Bands

Current bands:

- `high`: score >= 80
- `good`: score >= 65
- `possible`: score >= 55
- `low`: score < 55

The default match threshold is `65`.

## Adapting To Other Role Families

To make RoleRadar truly role-agnostic, relevance should become profile-driven.

Possible future CLI shape:

```bash
python main.py search "Product Manager" --profile product --scrapers company_careers
python main.py search "Data Scientist" --profile data --scrapers company_careers
python main.py search "Designer" --profile design --scrapers company_careers
```

Possible profile configuration:

```yaml
profiles:
  product:
    positive_patterns:
      - pattern: "\\bproduct manager\\b"
        points: 35
      - pattern: "\\bplatform product\\b"
        points: 15
    negative_patterns:
      - pattern: "\\bsales\\b"
        points: -40
    min_score: 65
```

Until profiles are implemented, adapt relevance by editing `src/scraper/company_careers/relevance.py` and adding tests in `tests/test_company_careers.py`.

## What Not To Overclaim

RoleRadar is not currently a universal AI matching engine.

It is a practical, transparent job discovery pipeline with deterministic ranking. That is intentional: explanations like `relevance_reasons` and `relevance_penalties` make review behavior inspectable.
