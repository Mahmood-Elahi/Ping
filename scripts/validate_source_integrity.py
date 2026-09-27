"""Offline source-integrity and onboarding validator.

The tool reads only repository configuration and retained coverage JSON.  It
does not import persistence integrations and performs no network requests.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any, Mapping

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_integrity import (
    IntegrityAction,
    assess_registry,
    load_registry_entries,
)


DEFAULT_REGISTRY = Path("config/target_companies.yaml")
DEFAULT_HISTORY_GLOB = "jobs/company_careers_coverage_*.json"


def _company_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def load_latest_retained_coverage(pattern: str = DEFAULT_HISTORY_GLOB) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    candidates = sorted(Path().glob(pattern))
    if not candidates:
        return None, {}
    path = candidates[-1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    coverage = payload.get("coverage") if isinstance(payload, Mapping) else None
    if not isinstance(coverage, list):
        raise ValueError(f"Retained coverage has no coverage list: {path}")
    return path, {
        _company_key(row.get("company")): dict(row)
        for row in coverage
        if isinstance(row, Mapping) and str(row.get("company") or "").strip()
    }


def validate_source_integrity(
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
    retained_pattern: str = DEFAULT_HISTORY_GLOB,
    company: str | None = None,
) -> dict[str, Any]:
    entries = load_registry_entries(registry_path)
    retained_path, retained = load_latest_retained_coverage(retained_pattern)
    effective = load_target_companies(
        str(registry_path),
        apply_reliability_repairs=Path(registry_path).resolve() == DEFAULT_REGISTRY.resolve(),
    )
    bounds = {
        _company_key(target.name): {
            "max_pages": target.max_pages,
            "max_jobs": target.max_jobs,
        }
        for target in effective
    }
    assessments = assess_registry(
        entries,
        retained_by_company=retained,
        effective_bounds_by_company=bounds,
    )
    if company:
        requested = _company_key(company)
        assessments = [
            assessment
            for assessment in assessments
            if _company_key(assessment.company) == requested
        ]
        if not assessments:
            raise ValueError(f"Company is not present in the registry: {company}")
    rows = [assessment.to_dict() for assessment in assessments]
    counts = Counter(row["health_classification"] for row in rows)
    return {
        "schema_version": 1,
        "network_requests": 0,
        "registry": str(Path(registry_path)),
        "retained_coverage": str(retained_path) if retained_path else None,
        "company_filter": company,
        "summary": {
            "sources": len(rows),
            **{action.value: counts[action.value] for action in IntegrityAction},
        },
        "sources": rows,
    }


def render_summary(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "Source integrity (offline)",
        f"  Sources: {summary['sources']}",
        f"  Retained coverage: {payload.get('retained_coverage') or 'none'}",
    ]
    lines.extend(
        f"  {action.value}: {summary[action.value]}" for action in IntegrityAction
    )
    risky = [
        row
        for row in payload.get("sources", [])
        if row.get("health_classification") != IntegrityAction.OK.value
    ]
    if risky:
        lines.append("  Actionable sources:")
        for row in risky[:25]:
            reason = "; ".join(row.get("risk_reasons") or [])
            lines.append(
                f"    {row['company']} [{row['platform']}] "
                f"{row['health_classification']}: {reason}"
            )
        if len(risky) > 25:
            lines.append(f"    ... {len(risky) - 25} additional sources; use --json for all rows")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--retained-pattern", default=DEFAULT_HISTORY_GLOB)
    parser.add_argument("--company")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the concise summary")
    parser.add_argument(
        "--strict-onboarding",
        action="store_true",
        help="Exit nonzero when a source lacks a reviewed activation contract",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = validate_source_integrity(
        registry_path=args.registry,
        retained_pattern=args.retained_pattern,
        company=args.company,
    )
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered if args.json else render_summary(payload))
    if args.strict_onboarding and payload["summary"][IntegrityAction.ARCHITECTURAL.value]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
