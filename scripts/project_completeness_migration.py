"""Project completeness recovery from saved artifacts and current configuration.

This module is deliberately offline.  It reads the saved coverage artifact and
the active registry; it never constructs an adapter session or makes a request.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.registry import load_target_companies


SAVED_STAMP = "20260922_210359"
DEFAULT_COVERAGE = ROOT / "jobs" / f"company_careers_coverage_{SAVED_STAMP}.json"
DEFAULT_OUTPUT = ROOT / "output" / f"projected_completeness_pareto_{SAVED_STAMP}.json"
PRIOR_MIGRATED = {"oracle_recruiting", "smartrecruiters"}
INTENTIONALLY_UNRESOLVED = {"browser_rendered_html"}


CONTRACTS: dict[str, dict[str, Any]] = {
    "static_html": {
        "mechanism": "reviewed single-page listing container or bounded explicit pagination",
        "terminal_contract": "complete container, explicit next-link exhaustion, or validated structured ItemList",
    },
    "eightfold": {
        "mechanism": "offset pagination with required stable count",
        "terminal_contract": "unique jobs reach the stable reported total exactly",
    },
    "icims": {
        "mechanism": "Jibe total/page API or reviewed server-rendered page sequence",
        "terminal_contract": "stable reported total; explicit zero; legacy nonzero HTML remains fail-closed unless capped",
    },
    "browser_rendered_html": {
        "mechanism": "bounded browser render and scroll delegated to the static parser",
        "terminal_contract": None,
        "safe": False,
        "runtime": "unchanged; no migration",
    },
    "javascript_embedded_data": {
        "mechanism": "complete embedded/endpoint JSON collection, optionally bounded by explicit page URLs",
        "terminal_contract": "complete reviewed collection or validated empty terminal page",
    },
    "adp": {
        "mechanism": "offset pagination with stable totalNumber/count",
        "terminal_contract": "reported total reached exactly",
    },
    "avature": {
        "mechanism": "offset/page-number HTML pagination",
        "terminal_contract": "stable reported total or validated short/empty terminal page",
    },
    "bamboohr": {
        "mechanism": "single public collection with totalCount equal to result length",
        "terminal_contract": "complete collection response",
    },
    "dayforce": {
        "mechanism": "offset pagination with stable TotalCount",
        "terminal_contract": "reported total reached exactly",
    },
    "ukg_pro": {
        "mechanism": "Skip/Top pagination with stable totalCount",
        "terminal_contract": "raw offset reaches reported total exactly",
    },
    "beesite": {
        "mechanism": "first-item pagination with stable total",
        "terminal_contract": "reported total reached exactly",
    },
    "hibob": {
        "mechanism": "complete single-response jobAdDetails collection",
        "terminal_contract": "complete collection response",
    },
    "ibm_search": {
        "mechanism": "configured search-term offsets with fixed page size",
        "terminal_contract": "every configured term reaches an empty/short terminal page",
    },
    "paycor": {
        "mechanism": "complete server-rendered CareerHome listing container",
        "terminal_contract": "complete listing container or explicit zero marker",
    },
    "talentbrew": {
        "mechanism": "server-rendered pages with explicit next link",
        "terminal_contract": "next link absent or disabled",
    },
    "amazon": {
        "mechanism": "configured search-term offsets with result_limit=100",
        "terminal_contract": "every configured term reaches an empty/short terminal page",
    },
    "apple": {
        "mechanism": "page-number API with stable totalRecords",
        "terminal_contract": "records seen reach totalRecords exactly",
    },
    "atlassian": {
        "mechanism": "complete single-response listings array",
        "terminal_contract": "complete collection response",
    },
    "clearcompany": {
        "mechanism": "pageIndex/pageSize API with stable totalCount",
        "terminal_contract": "reported total reached exactly",
    },
    "cornerstone_csod": {
        "mechanism": "page-number API with stable totalCount",
        "terminal_contract": "reported total reached exactly",
    },
    "deel": {
        "mechanism": "schema.org ItemList with bounded detail fetches",
        "terminal_contract": "complete ordered ItemList",
    },
    "exalead": {
        "mechanism": "offset API with stable nhits",
        "terminal_contract": "reported total reached exactly",
    },
    "google": {
        "mechanism": "configured search-term page sequence",
        "terminal_contract": "every configured term reaches an empty terminal page",
    },
    "hawksearch_v2": {
        "mechanism": "page-number API with stable NofResults/NofPages",
        "terminal_contract": "final reported page reaches NofResults exactly",
    },
    "mcloud_cws": {
        "mechanism": "offset API with stable totalHits",
        "terminal_contract": "reported total reached exactly",
    },
    "meta": {
        "mechanism": "configured search-term GraphQL page sequence",
        "terminal_contract": "every configured term reaches an empty terminal page",
    },
    "peoplesoft": {
        "mechanism": "complete public listing container with bounded details",
        "terminal_contract": "complete listing container or explicit zero marker",
    },
    "shopify": {
        "mechanism": "complete embedded React Router collection",
        "terminal_contract": "complete embedded collection",
    },
    "startdate": {
        "mechanism": "complete single-response active-jobs array",
        "terminal_contract": "complete collection response",
    },
    "teamtailor": {
        "mechanism": "server-rendered pages with rel=next",
        "terminal_contract": "next link absent",
    },
}


def _hard_failed(row: Mapping[str, Any]) -> bool:
    return row.get("scan_health") == "failed" or row.get("status") == "failed_technical"


def _contract_breakdown(platform: str, targets: list[Any]) -> dict[str, int]:
    if platform == "static_html":
        return dict(
            sorted(
                Counter(
                    "pagination_url_template"
                    if target.pagination_url_template
                    else "explicit_next_link"
                    if target.next_page_selector
                    else "complete_single_page_container"
                    for target in targets
                ).items()
            )
        )
    if platform == "icims":
        return dict(sorted(Counter(str(target.endpoint_variant) for target in targets).items()))
    if platform == "javascript_embedded_data":
        return dict(sorted(Counter(str(target.endpoint_variant) for target in targets).items()))
    return {}


def build_projection(
    coverage_path: Path = DEFAULT_COVERAGE,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    rows = [
        row
        for row in payload.get("coverage", [])
        if isinstance(row, Mapping)
        and row.get("scan_completeness_reason") == "adapter_completeness_not_proven"
        and not _hard_failed(row)
    ]
    targets = load_target_companies(str(registry_path) if registry_path else None)
    by_key = {(target.name, target.platform): target for target in targets}
    by_name = {target.name: target for target in targets}
    missing = [
        (str(row.get("company")), str(row.get("platform")))
        for row in rows
        if str(row.get("company")) not in by_name
    ]
    if missing:
        raise ValueError(f"Saved avoidable sources missing from active registry: {missing}")

    # A retained audit can legitimately name an older adapter after an exact
    # source has moved to a stronger reviewed contract.  Do not feed that stale
    # platform row back into the old-family projection, and do not silently
    # treat an actually removed company the same way.
    contract_migrations = [
        row
        for row in rows
        if (row.get("company"), row.get("platform")) not in by_key
    ]
    current_contract_rows = [
        row
        for row in rows
        if (row.get("company"), row.get("platform")) in by_key
    ]

    remaining_after_prior = [
        row
        for row in current_contract_rows
        if row.get("platform") not in PRIOR_MIGRATED
    ]
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in remaining_after_prior:
        grouped[str(row.get("platform"))].append(row)

    platforms: list[dict[str, Any]] = []
    for platform, platform_rows in grouped.items():
        contract = CONTRACTS.get(platform)
        if contract is None:
            raise ValueError(f"No reviewed completeness contract classification for {platform}")
        platform_targets = [
            by_key[(row.get("company"), row.get("platform"))] for row in platform_rows
        ]
        safe = bool(contract.get("safe", True))
        platforms.append(
            {
                "platform": platform,
                "affected_sources": len(platform_rows),
                "percentage_of_remaining_avoidable": round(
                    100 * len(platform_rows) / len(remaining_after_prior), 2
                ),
                "listing_or_pagination_mechanism": contract["mechanism"],
                "deterministic_terminal_contract": contract["terminal_contract"],
                "additional_network_requests": False,
                "estimated_runtime_impact": contract.get(
                    "runtime", "negligible; metadata and validation only"
                ),
                "safe_to_migrate_in_this_pass": safe,
                "contract_breakdown": _contract_breakdown(platform, platform_targets),
            }
        )
    platforms.sort(key=lambda item: (-item["affected_sources"], item["platform"]))

    prior_count = sum(row.get("platform") in PRIOR_MIGRATED for row in rows)
    additional_count = sum(
        item["affected_sources"]
        for item in platforms
        if item["safe_to_migrate_in_this_pass"]
    )
    unresolved = [
        item for item in platforms if not item["safe_to_migrate_in_this_pass"]
    ]
    return {
        "report_version": 1,
        "projection_basis": {
            "coverage": str(coverage_path),
            "registry": str(registry_path or ROOT / "config" / "target_companies.yaml"),
            "network_requests_performed": 0,
        },
        "summary": {
            "original_adapter_completeness_not_proven": len(rows),
            "migrated_prior_oracle_smartrecruiters": prior_count,
            "migrated_to_stronger_active_contract": len(contract_migrations),
            "active_contract_migrations": sorted(
                {
                    f"{row.get('company')}: {row.get('platform')} -> "
                    f"{by_name[str(row.get('company'))].platform}"
                    for row in contract_migrations
                }
            ),
            "remaining_avoidable_before_this_pass": len(remaining_after_prior),
            "additional_sources_migrated_this_pass": additional_count,
            "projected_adapter_completeness_not_proven_remaining": sum(
                item["affected_sources"] for item in unresolved
            ),
            "original_intentional_safety_limit_sources_remaining": 43,
        },
        "platforms": platforms,
        "largest_unresolved_platforms": unresolved,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_projection(args.coverage, args.registry)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], sort_keys=True))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
