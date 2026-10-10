"""Load target company registry configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

import yaml

from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.source_integrity import assess_registry
from src.utils.source_scope import (
    normalize_canonical_company,
    normalize_source_scope,
    source_scope_key,
)


DEFAULT_REGISTRY_PATH = "config/target_companies.yaml"
DEFAULT_RELIABILITY_REPAIRS_PATH = "config/company_careers_reliability_repairs_v1.yaml"


class AmbiguousCompanySourceError(LookupError):
    """Raised when a company-only lookup has more than one logical source."""


class CompanySourceRegistry:
    """Deterministic lookup index for flat company + source-scope rows.

    Company-only lookup is retained for legacy callers, but it succeeds only
    when the canonical company has exactly one configured logical source.
    """

    def __init__(self, targets: Iterable[CompanyTarget]):
        self.targets = tuple(targets)
        by_scope: dict[str, CompanyTarget] = {}
        by_company: dict[str, list[CompanyTarget]] = {}
        for target in self.targets:
            logical_scope = target.source_scope_key
            if logical_scope in by_scope:
                raise ValueError(
                    "Duplicate configured source scope: "
                    f"{target.name} / {target.source_scope}"
                )
            by_scope[logical_scope] = target
            by_company.setdefault(normalize_canonical_company(target.name), []).append(
                target
            )
        self._by_scope = by_scope
        self._by_company = {
            company: tuple(sorted(values, key=lambda item: item.source_scope))
            for company, values in by_company.items()
        }

    def get_sources(self, company: str) -> tuple[CompanyTarget, ...]:
        """Return every source for one canonical company in scope order."""
        return self._by_company.get(normalize_canonical_company(company), ())

    def get_source(self, company: str, source_scope: str) -> CompanyTarget:
        """Return the exact company + scope source or raise ``KeyError``."""
        key = source_scope_key(company, normalize_source_scope(source_scope))
        try:
            return self._by_scope[key]
        except KeyError:
            raise KeyError(
                f"Unknown configured source scope: {company} / {source_scope}"
            ) from None

    def get_company(self, company: str) -> CompanyTarget:
        """Return a unique legacy company source; never select arbitrarily."""
        sources = self.get_sources(company)
        if not sources:
            raise KeyError(f"Unknown configured company: {company}")
        if len(sources) != 1:
            scopes = ", ".join(source.source_scope for source in sources)
            raise AmbiguousCompanySourceError(
                f"Configured company {company!r} has multiple source scopes: {scopes}; "
                "use get_source(company, source_scope) or get_sources(company)"
            )
        return sources[0]


def load_target_companies(
    path: Optional[str] = DEFAULT_REGISTRY_PATH,
    *,
    apply_reliability_repairs: bool = False,
) -> List[CompanyTarget]:
    registry_path = Path(path or DEFAULT_REGISTRY_PATH)
    if not registry_path.exists():
        raise FileNotFoundError(f"Company registry not found: {path}")

    with registry_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    entries = data.get("target_companies") or data.get("companies") or []
    if apply_reliability_repairs:
        if not _is_default_registry(registry_path):
            raise ValueError("Reliability repairs may only be applied to the production registry")
        entries = _apply_reliability_repairs(entries, Path(DEFAULT_RELIABILITY_REPAIRS_PATH))
    migrate_legacy_safety_defaults = _is_default_registry(registry_path)
    targets = [
        CompanyTarget.from_dict(
            dict(entry),
            apply_legacy_safety_defaults=migrate_legacy_safety_defaults,
        )
        for entry in entries
    ]
    assessments = assess_registry(
        entries,
        effective_bounds_by_company={
            source_scope_key(target.name, target.source_scope): {
                "max_pages": target.max_pages,
                "max_jobs": target.max_jobs,
            }
            for target in targets
        },
    )
    for target, assessment in zip(targets, assessments, strict=True):
        target.source_activation = assessment.activation_state.value
    return targets


def load_company_source_registry(
    path: Optional[str] = DEFAULT_REGISTRY_PATH,
    *,
    apply_reliability_repairs: bool = False,
) -> CompanySourceRegistry:
    return CompanySourceRegistry(
        load_target_companies(
            path,
            apply_reliability_repairs=apply_reliability_repairs,
        )
    )


def _is_default_registry(path: Path) -> bool:
    return path.resolve() == Path(DEFAULT_REGISTRY_PATH).resolve()


def _apply_reliability_repairs(
    entries: List[Mapping[str, Any]],
    repair_path: Path,
) -> List[Mapping[str, Any]]:
    """Overlay named production repairs without rewriting historical rollout evidence."""
    if not repair_path.exists():
        raise FileNotFoundError(f"Company reliability repairs not found: {repair_path}")
    with repair_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    repairs = data.get("company_careers_reliability_repairs") or []
    if not isinstance(repairs, list):
        raise ValueError("Company reliability repairs must be a list")
    by_name = {str(item.get("name") or "").strip().casefold(): dict(item) for item in repairs}
    if "" in by_name or len(by_name) != len(repairs):
        raise ValueError("Company reliability repairs require unique non-empty names")
    production_names = {str(item.get("name") or "").strip().casefold() for item in entries}
    missing = sorted(set(by_name) - production_names)
    if missing:
        raise ValueError(f"Company reliability repairs reference unknown targets: {missing}")
    return [
        by_name.get(str(item.get("name") or "").strip().casefold(), dict(item))
        for item in entries
    ]
