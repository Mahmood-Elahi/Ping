from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from src.scraper.company_careers.models import CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"


def with_reliability_repairs(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply only the validated in-place repairs to a historical registry view."""
    data = yaml.safe_load(REPAIRS.read_text(encoding="utf-8")) or {}
    repairs = data.get("company_careers_reliability_repairs") or []
    by_name = {str(item["name"]).casefold(): item for item in repairs}
    return [
        deepcopy(by_name.get(str(item.get("name") or "").casefold(), item))
        for item in entries
    ]


def assert_historical_prefix(
    testcase: Any,
    production: Sequence[Mapping[str, Any]],
    historical: Sequence[Mapping[str, Any]],
) -> None:
    testcase.assertGreaterEqual(len(production), len(historical))
    testcase.assertEqual(
        list(production[: len(historical)]),
        with_reliability_repairs(historical),
    )


def assert_historical_registry_bytes(
    testcase: Any,
    production_bytes: bytes,
    historical_bytes: bytes,
) -> None:
    production = (yaml.safe_load(production_bytes) or {})["target_companies"]
    historical = (yaml.safe_load(historical_bytes) or {})["target_companies"]
    assert_historical_prefix(testcase, production, historical)


def assert_historical_target_prefix(
    testcase: Any,
    production: Sequence[CompanyTarget],
    historical: Sequence[CompanyTarget],
) -> None:
    repaired = [CompanyTarget.from_dict(item) for item in with_reliability_repairs(
        [vars(target) for target in historical]
    )]
    testcase.assertGreaterEqual(len(production), len(historical))
    testcase.assertEqual(list(production[: len(historical)]), repaired)
