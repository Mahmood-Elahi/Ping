"""Build the exact deterministic offline Pareto for the latest saved audit."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.models import SAFE_LISTING_PAGE_SIZE_DEFAULTS
from src.scraper.company_careers.registry import load_target_companies

JOBS = ROOT / "jobs"
OUTPUT = ROOT / "output"
SAVED_STAMP = "20260923_201051"
DEFAULT_COVERAGE = JOBS / f"company_careers_coverage_{SAVED_STAMP}.json"
DEFAULT_FAILURES = JOBS / f"company_careers_failures_{SAVED_STAMP}.json"
DEFAULT_DIAGNOSTICS = JOBS / f"student_search_diagnostics_{SAVED_STAMP}.json"
DEFAULT_OUTPUT = OUTPUT / f"incomplete_source_pareto_{SAVED_STAMP}.json"
DEFAULT_LARGE_BOARD_OUTPUT = OUTPUT / f"large_api_incomplete_recovery_{SAVED_STAMP}.json"
DEFAULT_LARGE_BOARD_CSV = OUTPUT / f"large_api_incomplete_recovery_{SAVED_STAMP}.csv"
LATEST_ARTIFACT_RUNTIME_SECONDS = {SAVED_STAMP: 711.0}
ABSOLUTE_JOB_CEILING = 5_000
ABSOLUTE_REQUEST_CEILING = 100
FIRST_ADAPTIVE_REQUEST_ALLOWANCE = 10
CURRENT_SECOND_STAGE_ADDITIONAL_ALLOWANCE = 50

MUTABLE_REASONS = {"collection_changed_during_scan"}
MALFORMED_REASONS = {"malformed_listing_records"}
MAX_JOB_STOPS = {"max_jobs", "maximum_results_reached"}
MAX_PAGE_STOPS = {"max_pages", "maximum_pages_reached"}
STATIC_PLATFORMS = {"static_html", "talentbrew"}
BROWSER_PLATFORMS = {"browser_rendered_html"}
FIXED_PAGE_SIZES = {
    "apple": 20,
    "eightfold": 10,
    "oracle_recruiting": 24,
    "phenom": 10,
    "workday": 20,
}
SECOND_STAGE_PLATFORMS = {
    "avature", "beesite", "eightfold", "exalead", "hawksearch_v2",
    "icims", "mcloud_cws", "oracle_recruiting", "phenom",
    "sap_successfactors", "workday",
}
DETAIL_COUPLED_PLATFORMS = {
    "bamboohr", "paycor", "peoplesoft", "smartrecruiters",
}
STRATEGY_ALLOWANCES = (10, 25, 50, 100)
COST_BAND_LIMITS = (25, 50, 75, 100)


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _find_runtime_seconds(value: Any) -> float | None:
    """Find a persisted run duration without mistaking per-source timings for it."""
    if not isinstance(value, Mapping):
        return None
    for key in ("scan_runtime_seconds", "runtime_seconds", "elapsed_seconds"):
        runtime = _number(value.get(key))
        if runtime is not None:
            return runtime
    for key in ("run", "run_summary", "summary", "metadata", "diagnostics"):
        nested = value.get(key)
        runtime = _find_runtime_seconds(nested)
        if runtime is not None:
            return runtime
    return None


def _runtime_seconds(
    coverage_payload: Mapping[str, Any], diagnostics_path: Path | None
) -> tuple[float, str]:
    runtime = _find_runtime_seconds(coverage_payload)
    if runtime is not None:
        return runtime, "coverage_artifact"
    if diagnostics_path is not None and diagnostics_path.exists():
        diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        runtime = _find_runtime_seconds(diagnostics)
        if runtime is not None:
            return runtime, "diagnostics_artifact"
    stamp = coverage_payload.get("artifact_stamp") or SAVED_STAMP
    return LATEST_ARTIFACT_RUNTIME_SECONDS.get(str(stamp), 711.0), "artifact_set_baseline"


def _is_hard_failed(row: Mapping[str, Any]) -> bool:
    return row.get("scan_health") == "failed" or row.get("status") == "failed_technical"


def _target_index() -> dict[tuple[str, str], Any]:
    return {(target.name, target.platform): target for target in load_target_companies()}


def _raw_target_index() -> dict[tuple[str, str], Mapping[str, Any]]:
    path = ROOT / "config" / "target_companies.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = payload.get("target_companies") or payload.get("companies") or []
    return {
        (str(item.get("name") or ""), str(item.get("platform") or "")): item
        for item in entries
        if isinstance(item, Mapping)
    }


def _variant(target: Any) -> str:
    return str(getattr(target, "endpoint_variant", None) or "")


def _current_page_size(
    row: Mapping[str, Any], target: Any, raw_target: Mapping[str, Any] | None
) -> int | None:
    platform = str(row.get("platform") or "")
    if platform == "sap_successfactors":
        return 10 if _variant(target) == "rmk_columnized" else 25
    if platform == "adp" and _variant(target) == "workforce_now_requisitions_v1":
        return 10
    if platform in FIXED_PAGE_SIZES:
        return FIXED_PAGE_SIZES[platform]
    raw = _integer((raw_target or {}).get("page_size"))
    if raw and raw > 0:
        return raw
    configured = getattr(target, "page_size", None)
    return configured if isinstance(configured, int) and configured > 0 else None


def _optimized_page_size(
    row: Mapping[str, Any], target: Any, current: int | None
) -> int | None:
    return SAFE_LISTING_PAGE_SIZE_DEFAULTS.get(str(row.get("platform") or ""), current)


def _requested_page_size(
    row: Mapping[str, Any], target: Any, raw_target: Mapping[str, Any] | None
) -> int | None:
    persisted = _integer(row.get("requested_page_size"))
    if persisted and persisted > 0:
        return persisted
    configured = _current_page_size(row, target, raw_target)
    return _optimized_page_size(row, target, configured)


def _observed_page_size(row: Mapping[str, Any], requested: int | None) -> int | None:
    persisted = _integer(row.get("observed_page_size"))
    if persisted and persisted > 0:
        return persisted
    # The saved coverage schema predates page-size telemetry. These adapter
    # contracts reject oversized pages and all large-board scans observed at
    # least one full page, so their validated request size is the exact
    # non-terminal observed size for this artifact set.
    return requested


def _effective_page_size(row: Mapping[str, Any], observed: int | None) -> int | None:
    persisted = _integer(row.get("effective_server_page_size"))
    if persisted and persisted > 0:
        return persisted
    return observed


def _detail_coupled(row: Mapping[str, Any], target: Any) -> bool:
    if "detail_requests_coupled" in row:
        return bool(row.get("detail_requests_coupled"))
    platform = str(row.get("platform") or "")
    if platform in DETAIL_COUPLED_PLATFORMS:
        return True
    if platform == "adp":
        return _variant(target) == "workforce_now_requisitions_v1"
    if platform == "sap_successfactors":
        return _variant(target) == "rmk_columnized"
    return False


def _second_stage_supported(row: Mapping[str, Any], target: Any) -> bool:
    platform = str(row.get("platform") or "")
    if platform == "adp":
        return _variant(target) == "myjobs_requisitions_v1"
    if platform == "sap_successfactors" and _variant(target) == "rmk_columnized":
        return False
    return platform in SECOND_STAGE_PLATFORMS


def _explicit_static_pagination(target: Any) -> bool:
    return bool(
        getattr(target, "pagination_url_template", None)
        or getattr(target, "next_page_selector", None)
    )


def _primary_category(row: Mapping[str, Any], target: Any) -> str:
    reason = str(row.get("scan_completeness_reason") or "")
    platform = str(row.get("platform") or "")
    if reason in MUTABLE_REASONS:
        return "mutable_collections"
    if reason in MALFORMED_REASONS:
        return "malformed_partial_collections"
    if platform in BROWSER_PLATFORMS:
        return "browser_rendered_bounded_boards"
    if platform in STATIC_PLATFORMS:
        return "static_html_bounded_boards"
    detail_coupled = _detail_coupled(row, target)
    if _integer(row.get("total_available")) is None and not _terminal_evidence_present(row):
        return "terminal_distance_unknown"
    if detail_coupled:
        return "remaining_arbitrary_cap_cases"
    return "large_api_boards"


def _terminal_evidence_present(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("terminal_page_observed")
        or row.get("complete_collection_response")
        or row.get("has_more") is False
        or row.get("next_cursor_present") is False
    )


def _static_browser_status(row: Mapping[str, Any], target: Any) -> str:
    platform = str(row.get("platform") or "")
    if platform == "static_html":
        if _explicit_static_pagination(target):
            return "explicit_pagination"
        if str(row.get("stop_reason")) in MAX_JOB_STOPS:
            return "complete_single_page_contract_result_cap"
        if str(row.get("scan_completeness_reason")) in MALFORMED_REASONS:
            return "malformed_partial_collection"
        return "bounded_html_prefix_or_unknown_terminal"
    if platform == "browser_rendered_html":
        if str(row.get("stop_reason")) in {"no_next_page", "validated_zero_jobs"}:
            return "rendered_terminal_evidence"
        return "further_rendering_or_scrolling_may_be_required"
    if platform == "talentbrew":
        return "explicit_html_pagination_terminal_not_observed"
    return "not_static_or_browser"


def _source_metrics(
    row: Mapping[str, Any], target: Any, raw_target: Mapping[str, Any] | None
) -> dict[str, Any]:
    total = _integer(row.get("total_available"))
    fetched = _integer(row.get("fetched_count")) or 0
    pages = _integer(row.get("pages_requested")) or 0
    configured_page = _current_page_size(row, target, raw_target)
    requested_page = _requested_page_size(row, target, raw_target)
    observed_page = _observed_page_size(row, requested_page)
    effective_page = _effective_page_size(row, observed_page)
    mutable = str(row.get("scan_completeness_reason")) in MUTABLE_REASONS
    malformed = str(row.get("scan_completeness_reason")) in MALFORMED_REASONS
    detail_coupled = _detail_coupled(row, target)
    required_configured = (
        math.ceil(total / configured_page)
        if total is not None and configured_page else None
    )
    required_effective = (
        math.ceil(total / effective_page)
        if total is not None and effective_page else None
    )
    remaining_requests = (
        max(0, required_effective - pages)
        if required_effective is not None else None
    )
    zero_network = bool(
        row.get("platform") == "static_html"
        and not _explicit_static_pagination(target)
        and str(row.get("stop_reason")) in MAX_JOB_STOPS
        and not malformed
    )
    hard_ceiling_eligible = bool(
        _second_stage_supported(row, target)
        and total is not None
        and required_effective is not None
        and total <= ABSOLUTE_JOB_CEILING
        and required_effective <= ABSOLUTE_REQUEST_CEILING
        and not mutable
        and not malformed
        and not detail_coupled
    )
    api_recovery = hard_ceiling_eligible
    recoverable = zero_network or api_recovery
    normal_request_budget = _integer(getattr(target, "max_pages", None)) or 1
    current_effective_ceiling = normal_request_budget
    if required_effective is not None:
        if required_effective <= min(
            ABSOLUTE_REQUEST_CEILING,
            normal_request_budget + FIRST_ADAPTIVE_REQUEST_ALLOWANCE,
        ):
            current_effective_ceiling = max(normal_request_budget, required_effective)
        elif (
            _second_stage_supported(row, target)
            and not mutable
            and not detail_coupled
            and total is not None
            and total <= ABSOLUTE_JOB_CEILING
            and required_effective <= ABSOLUTE_REQUEST_CEILING
            and required_effective - normal_request_budget
            <= CURRENT_SECOND_STAGE_ADDITIONAL_ALLOWANCE
        ):
            current_effective_ceiling = max(normal_request_budget, required_effective)
    return {
        "configured_page_size": configured_page,
        "requested_page_size": requested_page,
        "observed_page_size": observed_page,
        "effective_server_page_size": effective_page,
        "page_size_evidence": (
            "persisted_scan_metadata"
            if row.get("effective_server_page_size") is not None
            else "validated_adapter_contract_and_saved_full_page"
        ),
        "current_listing_page_size": configured_page,
        "maximum_safely_supported_page_size": requested_page,
        "current_projected_listing_requests": required_configured,
        "optimized_projected_listing_requests": required_effective,
        "calculated_listing_requests_needed": required_effective,
        "requests_avoided_by_larger_pages": (
            max(0, required_configured - required_effective)
            if required_configured is not None and required_effective is not None else 0
        ),
        "known_jobs_remaining": max(0, total - fetched) if total is not None else None,
        "remaining_listing_requests": remaining_requests,
        "additional_requests_from_current_state": 0 if zero_network else remaining_requests,
        "source_would_become_complete": recoverable,
        "jobs_added": max(0, total - fetched) if recoverable and total is not None else None,
        "zero_network_recovery": zero_network,
        "large_api_recovery": api_recovery,
        "hard_ceiling_eligible": hard_ceiling_eligible,
        "normal_request_budget": normal_request_budget,
        "current_adaptive_effective_ceiling": current_effective_ceiling,
        "mutable_status": "changed" if mutable else "stable_or_not_observed_changed",
        "mutable": mutable,
        "static_browser_status": _static_browser_status(row, target),
        "detail_coupled": detail_coupled,
        "listing_only": not detail_coupled,
        "authoritative_total_known": total is not None,
        "total_over_5000": bool(total is not None and total > ABSOLUTE_JOB_CEILING),
        "deterministic_pagination": _second_stage_supported(row, target),
        "expected_recoverable_under_100_listing_requests": hard_ceiling_eligible,
        "incomplete_reason": ":".join(
            filter(
                None,
                (
                    str(row.get("scan_completeness_reason") or ""),
                    str(row.get("stop_reason") or ""),
                ),
            )
        ),
        "cost_mode": "detail_coupled" if detail_coupled else "listing_only",
        "page_ceiling_hit": str(row.get("stop_reason")) in MAX_PAGE_STOPS,
        "result_ceiling_hit": str(row.get("stop_reason")) in MAX_JOB_STOPS,
        "primary_category": _primary_category(row, target),
    }


def _bucket(rows: Iterable[Mapping[str, Any]], key: Any, total: int) -> dict[str, Any]:
    items = list(rows)
    return {
        "key": list(key) if isinstance(key, tuple) else key,
        "source_count": len(items),
        "percentage_of_incomplete": round(100 * len(items) / total, 2),
        "jobs_fetched": sum(_integer(row.get("fetched_count")) or 0 for row in items),
        "known_unfetched_jobs": sum(_integer(row.get("known_jobs_remaining")) or 0 for row in items),
        "pages_fetched": sum(_integer(row.get("pages_completed")) or 0 for row in items),
        "max_jobs_hit": sum(str(row.get("stop_reason")) in MAX_JOB_STOPS for row in items),
        "max_pages_hit": sum(str(row.get("stop_reason")) in MAX_PAGE_STOPS for row in items),
        "representative_sources": sorted(str(row.get("company") or "") for row in items)[:10],
    }


def _grouping(rows: list[Mapping[str, Any]], key_fn: Callable[[Mapping[str, Any]], Any]) -> list[dict[str, Any]]:
    grouped: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return sorted(
        (_bucket(items, key, len(rows)) for key, items in grouped.items()),
        key=lambda item: (-item["source_count"], json.dumps(item["key"], sort_keys=True)),
    )


def _remaining_category(row: Mapping[str, Any]) -> str:
    total = _integer(row.get("total_available"))
    if total is not None and total > ABSOLUTE_JOB_CEILING:
        return "boards_over_5000_jobs"
    if row.get("mutable_status") == "changed":
        return "mutable_collections"
    if row.get("primary_category") == "malformed_partial_collections":
        return "malformed_collections"
    if row.get("platform") in STATIC_PLATFORMS | BROWSER_PLATFORMS:
        return "static_browser_unprovable_exhaustion"
    if row.get("detail_coupled"):
        return "detail_coupled_expense"
    return "other"


def _cost_band(row: Mapping[str, Any]) -> str:
    requests = _integer(row.get("calculated_listing_requests_needed"))
    if requests is None or requests > 100:
        return ">100"
    if requests <= 25:
        return "<=25"
    if requests <= 50:
        return "26-50"
    if requests <= 75:
        return "51-75"
    return "76-100"


def _source_names(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted(str(row.get("company") or "") for row in rows)


def _large_board_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "company",
        "platform",
        "fetched_count",
        "total_available",
        "known_jobs_remaining",
        "pages_requested",
        "pages_completed",
        "normal_request_budget",
        "current_adaptive_effective_ceiling",
        "calculated_listing_requests_needed",
        "configured_page_size",
        "requested_page_size",
        "observed_page_size",
        "effective_server_page_size",
        "page_size_evidence",
        "listing_only",
        "detail_coupled",
        "mutable",
        "total_over_5000",
        "deterministic_pagination",
        "authoritative_total_known",
        "expected_recoverable_under_100_listing_requests",
        "incomplete_reason",
    )
    result = {key: row.get(key) for key in keys}
    result["cost_band"] = _cost_band(row)
    return result


def _page_size_families(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("platform"))].append(row)
    result = []
    for platform, items in grouped.items():
        result.append({
            "platform": platform,
            "incomplete_sources": len(items),
            "current_listing_page_sizes": sorted({row.get("current_listing_page_size") for row in items}, key=str),
            "maximum_safely_supported_page_sizes": sorted({row.get("maximum_safely_supported_page_size") for row in items}, key=str),
            "current_requests_for_known_large_boards": sum(_integer(row.get("current_projected_listing_requests")) or 0 for row in items),
            "projected_requests_at_safe_page_size": sum(_integer(row.get("optimized_projected_listing_requests")) or 0 for row in items),
            "requests_avoided": sum(_integer(row.get("requests_avoided_by_larger_pages")) or 0 for row in items),
            "detail_requests_coupled": any(bool(row.get("detail_coupled")) for row in items),
        })
    return sorted(result, key=lambda item: (-item["incomplete_sources"], item["platform"]))


def build_report(
    coverage_path: Path = DEFAULT_COVERAGE,
    *,
    failures_path: Path | None = DEFAULT_FAILURES,
    diagnostics_path: Path | None = DEFAULT_DIAGNOSTICS,
) -> dict[str, Any]:
    payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    latest_runtime_seconds, runtime_source = _runtime_seconds(
        payload, diagnostics_path
    )
    coverage = payload.get("coverage")
    if not isinstance(coverage, list):
        raise ValueError("Coverage artifact must contain a coverage list")
    canonical = [row for row in coverage if isinstance(row, Mapping) and "scan_complete" in row]
    incomplete = [row for row in canonical if not bool(row.get("scan_complete")) and not _is_hard_failed(row)]
    targets = _target_index()
    raw_targets = _raw_target_index()
    enriched: list[dict[str, Any]] = []
    for original in incomplete:
        row = dict(original)
        key = (str(row.get("company") or ""), str(row.get("platform") or ""))
        target = targets.get(key)
        row["configured_max_pages"] = getattr(target, "max_pages", None)
        row["configured_max_jobs"] = getattr(target, "max_jobs", None)
        row["endpoint_variant"] = _variant(target)
        row.update(_source_metrics(row, target, raw_targets.get(key)))
        enriched.append(row)
    category_counts = Counter(str(row["primary_category"]) for row in enriched)
    recoveries = [row for row in enriched if row["source_would_become_complete"]]
    zero_network = [row for row in recoveries if row["zero_network_recovery"]]
    api_recoveries = [row for row in recoveries if row["large_api_recovery"]]
    hard_ceiling_candidates = [row for row in enriched if row["hard_ceiling_eligible"]]
    large_api_rows = [
        row for row in enriched if row["primary_category"] == "large_api_boards"
    ]
    ranked = sorted(
        api_recoveries,
        key=lambda row: (
            _integer(row.get("additional_requests_from_current_state")) or 0,
            -(_integer(row.get("jobs_added")) or 0),
            str(row.get("company")),
        ),
    )
    strategies = []
    for allowance in STRATEGY_ALLOWANCES:
        selected_api = [
            row
            for row in hard_ceiling_candidates
            if (_integer(row.get("calculated_listing_requests_needed")) or 0)
            <= allowance
        ]
        requests = sum(_integer(row.get("additional_requests_from_current_state")) or 0 for row in selected_api)
        completed = len(zero_network) + len(selected_api)
        strategies.append({
            "strategy": f"through_{allowance}_total_requests",
            "maximum_total_listing_requests_per_source": allowance,
            "sources_expected_complete": completed,
            "api_sources_expected_complete": len(selected_api),
            "zero_network_sources_expected_complete": len(zero_network),
            "additional_listing_requests": requests,
            "additional_jobs_enumerated": sum(_integer(row.get("known_jobs_remaining")) or 0 for row in selected_api),
            "sources_completed_per_extra_request": round(len(selected_api) / requests, 4) if requests else None,
            "projected_incomplete": len(enriched) - completed,
        })

    recovered_keys = {(row.get("company"), row.get("platform")) for row in recoveries}
    remaining = [row for row in enriched if (row.get("company"), row.get("platform")) not in recovered_keys]
    remaining_breakdown = Counter(_remaining_category(row) for row in remaining)
    additional = sum(_integer(row.get("additional_requests_from_current_state")) or 0 for row in api_recoveries)
    avoided = sum(_integer(row.get("requests_avoided_by_larger_pages")) or 0 for row in api_recoveries)
    runtime_high = round(latest_runtime_seconds + additional * 0.5 / 6, 1)
    runtime_low = round(latest_runtime_seconds + additional * 0.2 / 6, 1)
    complete_count = sum(bool(row.get("scan_complete")) for row in canonical)
    failed_count = sum(_is_hard_failed(row) for row in canonical)
    cost_bands = {
        band: _source_names(row for row in large_api_rows if _cost_band(row) == band)
        for band in ("<=25", "26-50", "51-75", "76-100", ">100")
    }
    large_table = [
        _large_board_row(row)
        for row in sorted(
            large_api_rows,
            key=lambda item: (
                _integer(item.get("calculated_listing_requests_needed")) or 10**9,
                str(item.get("company")),
            ),
        )
    ]
    selected_names = _source_names(api_recoveries)
    rejection_lists = {
        "over_100_requests": _source_names(
            row
            for row in large_api_rows
            if (_integer(row.get("calculated_listing_requests_needed")) or 10**9)
            > ABSOLUTE_REQUEST_CEILING
        ),
        "over_5000_jobs": _source_names(
            row
            for row in large_api_rows
            if (_integer(row.get("total_available")) or 0) > ABSOLUTE_JOB_CEILING
        ),
        "detail_coupled": _source_names(
            row
            for row in enriched
            if row.get("primary_category") == "remaining_arbitrary_cap_cases"
        ),
        "mutable": _source_names(
            row for row in enriched if row.get("primary_category") == "mutable_collections"
        ),
        "unknown_terminal": _source_names(
            row
            for row in enriched
            if row.get("primary_category") == "terminal_distance_unknown"
        ),
        "malformed": _source_names(
            row
            for row in enriched
            if row.get("primary_category") == "malformed_partial_collections"
        ),
    }

    return {
        "report_version": 5,
        "source_artifacts": {"coverage": str(coverage_path), "failures": str(failures_path) if failures_path else None, "student_search_diagnostics": str(diagnostics_path) if diagnostics_path else None},
        "summary": {
            "complete_sources": complete_count,
            "incomplete_nonfailed_sources": len(enriched),
            "hard_failed_sources": failed_count,
            "primary_category_counts": dict(sorted(category_counts.items())),
            "zero_network_recoveries": len(zero_network),
            "large_api_recoveries": len(api_recoveries),
            "large_api_incomplete_sources": len(large_api_rows),
            "selected_large_board_sources": len(api_recoveries),
        },
        "groupings": {
            "scan_completeness_reason": _grouping(enriched, lambda row: row.get("scan_completeness_reason")),
            "stop_reason": _grouping(enriched, lambda row: row.get("stop_reason")),
            "platform": _grouping(enriched, lambda row: row.get("platform")),
            "primary_category": _grouping(enriched, lambda row: row.get("primary_category")),
            "static_browser_status": _grouping(enriched, lambda row: row.get("static_browser_status")),
            "detail_cost": _grouping(enriched, lambda row: row.get("cost_mode")),
        },
        "page_size_families": _page_size_families(enriched),
        "ranked_recovery_candidates": ranked,
        "large_board_recovery": {
            "maximum_listing_requests_per_source": ABSOLUTE_REQUEST_CEILING,
            "maximum_jobs_per_source": ABSOLUTE_JOB_CEILING,
            "selected_source_count": len(api_recoveries),
            "selected_sources": selected_names,
            "cost_bands": cost_bands,
            "additional_listing_requests": additional,
            "additional_jobs_enumerated": sum(
                _integer(row.get("known_jobs_remaining")) or 0
                for row in api_recoveries
            ),
            "jobs_gained_per_additional_request": (
                round(
                    sum(
                        _integer(row.get("known_jobs_remaining")) or 0
                        for row in api_recoveries
                    )
                    / additional,
                    4,
                )
                if additional else None
            ),
            "sources_recovered_per_additional_request": (
                round(len(api_recoveries) / additional, 4) if additional else None
            ),
            "rejected_sources": rejection_lists,
            "table": large_table,
        },
        "budget_strategies": strategies,
        "selected_strategy": "absolute_100_total_listing_request_ceiling",
        "runtime_model": {
            "latest_actual_runtime_seconds": latest_runtime_seconds,
            "latest_actual_runtime_source": runtime_source,
            "workers": 6,
            "gross_additional_listing_requests_without_larger_pages": additional + avoided,
            "requests_avoided_through_larger_page_sizes": avoided,
            "net_additional_listing_requests": additional,
            "expected_additional_wall_time_seconds_range": [
                round(additional * 0.2 / 6, 1),
                round(additional * 0.5 / 6, 1),
            ],
            "expected_wall_time_seconds_range": [runtime_low, runtime_high],
            "scheduled_timeout_seconds": 45 * 60,
            "browser_work_delta": 0,
        },
        "projected_after": {
            "complete": complete_count + len(recoveries),
            "incomplete": len(remaining),
            "failed": failed_count,
            "remaining_breakdown": dict(sorted(remaining_breakdown.items())),
        },
        "sources": sorted(enriched, key=lambda row: (str(row.get("platform")), str(row.get("company")))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--failures", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--diagnostics", type=Path, default=DEFAULT_DIAGNOSTICS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--large-board-output", type=Path, default=DEFAULT_LARGE_BOARD_OUTPUT)
    parser.add_argument("--large-board-csv", type=Path, default=DEFAULT_LARGE_BOARD_CSV)
    args = parser.parse_args()
    report = build_report(args.coverage, failures_path=args.failures, diagnostics_path=args.diagnostics)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    large_board = report["large_board_recovery"]
    args.large_board_output.write_text(
        json.dumps(large_board, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    table = large_board["table"]
    with args.large_board_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(table[0]) if table else [])
        if table:
            writer.writeheader()
            writer.writerows(table)
    print(json.dumps(report["summary"], sort_keys=True))
    print(json.dumps(report["projected_after"], sort_keys=True))
    print(json.dumps(report["runtime_model"], sort_keys=True))
    print(args.output)
    print(args.large_board_output)
    print(args.large_board_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
