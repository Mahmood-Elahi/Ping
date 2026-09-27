"""Load target company registry configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Mapping, Optional

import yaml

from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.source_integrity import assess_registry


DEFAULT_REGISTRY_PATH = "config/target_companies.yaml"
DEFAULT_RELIABILITY_REPAIRS_PATH = "config/company_careers_reliability_repairs_v1.yaml"


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
            " ".join(target.name.casefold().split()): {
                "max_pages": target.max_pages,
                "max_jobs": target.max_jobs,
            }
            for target in targets
        },
    )
    for target, assessment in zip(targets, assessments, strict=True):
        target.source_activation = assessment.activation_state.value
    return targets


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
