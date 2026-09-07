"""Load and validate the company-level industry classification."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml


INDUSTRY_GROUP_NAMES = (
    "Tech & AI",
    "Chips & Hardware",
    "Mobility & Robotics",
    "Energy & Industrial",
    "Finance & Fintech",
    "Consumer & Health",
)
DEFAULT_COMPANY_INDUSTRIES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "company_industries.yaml"
)


class CompanyIndustryConfigurationError(ValueError):
    """Raised when company industry metadata is incomplete or ambiguous."""


def load_company_industry_groups(
    path: str | Path = DEFAULT_COMPANY_INDUSTRIES_PATH,
) -> dict[str, tuple[str, ...]]:
    """Load the six configured company groups and validate their structure."""
    config_path = Path(path)
    if not config_path.exists():
        raise CompanyIndustryConfigurationError(
            f"Company industry configuration not found: {config_path}"
        )

    with config_path.open("r", encoding="utf-8") as config_file:
        document = yaml.safe_load(config_file) or {}
    groups = document.get("industry_groups")
    if not isinstance(groups, Mapping):
        raise CompanyIndustryConfigurationError(
            "Company industry configuration must define an industry_groups mapping"
        )

    normalized_groups: dict[str, tuple[str, ...]] = {}
    for industry, companies in groups.items():
        if not isinstance(industry, str):
            raise CompanyIndustryConfigurationError(
                "Industry group names must be strings"
            )
        if isinstance(companies, (str, bytes)) or not isinstance(companies, Sequence):
            raise CompanyIndustryConfigurationError(
                f"Industry group {industry!r} must contain a company list"
            )
        normalized_groups[industry] = tuple(companies)

    build_company_industry_lookup(normalized_groups)
    return normalized_groups


def build_company_industry_lookup(
    industry_groups: Mapping[str, Iterable[Any]],
    *,
    expected_companies: Iterable[str] | None = None,
) -> dict[str, str]:
    """Derive one normalized company-to-industry lookup from the group lists."""
    configured_groups = tuple(industry_groups)
    if set(configured_groups) != set(INDUSTRY_GROUP_NAMES) or len(
        configured_groups
    ) != len(INDUSTRY_GROUP_NAMES):
        missing = sorted(set(INDUSTRY_GROUP_NAMES) - set(configured_groups))
        unexpected = sorted(set(configured_groups) - set(INDUSTRY_GROUP_NAMES))
        raise CompanyIndustryConfigurationError(
            "Company industry configuration must contain exactly the six approved "
            f"groups; missing={missing}; unexpected={unexpected}"
        )

    lookup: dict[str, str] = {}
    for industry in INDUSTRY_GROUP_NAMES:
        companies = industry_groups[industry]
        if isinstance(companies, (str, bytes)):
            raise CompanyIndustryConfigurationError(
                f"Industry group {industry!r} must contain a company list"
            )
        for company in companies:
            if not isinstance(company, str) or not company.strip():
                raise CompanyIndustryConfigurationError(
                    f"Industry group {industry!r} contains an invalid company name"
                )
            display_name = company.strip()
            normalized_name = normalize_company_name(display_name)
            previous = lookup.get(normalized_name)
            if previous is not None:
                raise CompanyIndustryConfigurationError(
                    f"Company {display_name!r} is assigned more than once "
                    f"({previous!r} and {industry!r})"
                )
            lookup[normalized_name] = industry

    if expected_companies is not None:
        missing_companies = sorted(
            {
                company.strip()
                for company in expected_companies
                if normalize_company_name(company) not in lookup
            },
            key=str.casefold,
        )
        if missing_companies:
            raise CompanyIndustryConfigurationError(
                "Target companies are missing industry assignments: "
                + ", ".join(missing_companies)
            )

    return lookup


def industry_for_company(company: Any, lookup: Mapping[str, str]) -> str:
    """Return a company's industry or fail instead of silently dropping its jobs."""
    company_name = str(company or "").strip()
    industry = lookup.get(normalize_company_name(company_name))
    if industry is None:
        label = company_name or "<blank>"
        raise CompanyIndustryConfigurationError(
            f"Company {label!r} has no industry assignment"
        )
    return industry


def normalize_company_name(company: Any) -> str:
    """Apply only non-fuzzy canonical-name normalization."""
    return str(company or "").strip().casefold()
