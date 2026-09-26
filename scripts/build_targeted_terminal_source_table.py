#!/usr/bin/env python3
"""Build the offline 29-source static/browser/API terminal-evidence table."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
STAMP = "20260924_005547"
DEFAULT_PARETO = ROOT / "output" / f"incomplete_source_pareto_{STAMP}.json"
DEFAULT_JSON = ROOT / "output" / f"targeted_terminal_sources_{STAMP}.json"
DEFAULT_CSV = ROOT / "output" / f"targeted_terminal_sources_{STAMP}.csv"
TARGET_CATEGORIES = {
    "static_html_bounded_boards",
    "browser_rendered_bounded_boards",
    "terminal_distance_unknown",
}


def _source_type(row: Mapping[str, Any]) -> str:
    category = str(row.get("primary_category") or "")
    if category == "static_html_bounded_boards":
        return "static_html"
    if category == "browser_rendered_bounded_boards":
        return "browser_rendered"
    return "api_listing"


def _structural_reason(row: Mapping[str, Any]) -> str:
    persisted = str(row.get("structural_incomplete_reason") or "")
    if persisted:
        return persisted
    source_type = _source_type(row)
    if source_type == "static_html":
        return "static_terminal_unproven"
    if source_type == "browser_rendered":
        return "browser_terminal_unproven"
    total = row.get("total_available")
    fetched = row.get("fetched_count") or 0
    if isinstance(total, int) and fetched < total:
        return "authoritative_total_not_reached"
    terminal = any(
        row.get(key) is not None
        for key in ("has_more", "next_url_present", "next_cursor_present")
    ) or any(
        bool(row.get(key))
        for key in (
            "terminal_page_observed",
            "complete_collection_response",
            "disabled_next_control",
            "explicit_final_page",
        )
    )
    return "pagination_contract_unknown" if terminal else "api_terminal_metadata_missing"


def _mechanism(row: Mapping[str, Any]) -> str:
    platform = str(row.get("platform") or "")
    if platform == "browser_rendered_html":
        return "rendered total/range or disabled next/load-more control"
    if platform in {"static_html", "talentbrew"}:
        return "canonical next link, page bounds, result range, or disabled next control"
    if platform in {"avature", "icims"}:
        return "shared HTML pagination metadata propagation"
    if platform == "javascript_embedded_data":
        return "shared JSON pagination metadata propagation"
    if platform in {"amazon", "google", "meta"}:
        return "per-query total/has-more/page metadata propagation"
    if platform == "lever":
        return "existing authoritative public-API short-page terminal contract"
    return "shared listing pagination metadata propagation"


def build_rows(pareto_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(pareto_path.read_text(encoding="utf-8"))
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("Pareto artifact must contain a sources list")
    rows: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if source.get("primary_category") not in TARGET_CATEGORIES:
            continue
        terminal_metadata = {
            key: source.get(key)
            for key in (
                "terminal_evidence_type",
                "terminal_evidence_value",
                "has_more",
                "next_url_present",
                "next_cursor_present",
                "page_count",
                "result_count",
                "end_index",
                "disabled_next_control",
                "explicit_final_page",
                "terminal_page_observed",
                "complete_collection_response",
            )
        }
        exposures = {
            "reported_total": source.get("total_available") is not None,
            "has_more": source.get("has_more") is not None,
            "next_url": source.get("next_url_present") is not None,
            "next_cursor": source.get("next_cursor_present") is not None,
            "page_count": source.get("page_count") is not None,
            "result_count": source.get("result_count") is not None,
            "disabled_next_control": bool(source.get("disabled_next_control")),
            "explicit_final_page_indicator": bool(source.get("explicit_final_page")),
        }
        next_evidence = any(
            exposures[key]
            for key in (
                "has_more",
                "next_url",
                "next_cursor",
                "page_count",
                "disabled_next_control",
                "explicit_final_page_indicator",
            )
        )
        rows.append(
            {
                "company": source.get("company"),
                "platform": source.get("platform"),
                "source_type": _source_type(source),
                "target_category": source.get("primary_category"),
                "jobs_fetched": source.get("fetched_count"),
                "known_total": source.get("total_available"),
                "pages_requests_made": source.get("pages_requested"),
                "pages_completed": source.get("pages_completed"),
                "scan_complete": bool(source.get("scan_complete")),
                "stop_reason": source.get("stop_reason"),
                "terminal_metadata": terminal_metadata,
                "next_page_evidence_exists": next_evidence,
                "exposes": exposures,
                "completeness_not_proven_reason": _structural_reason(source),
                "likely_shared_recovery_mechanism": _mechanism(source),
            }
        )
    rows.sort(key=lambda row: (str(row["source_type"]), str(row["company"])))
    if len(rows) != 29:
        raise ValueError(f"Expected exactly 29 targeted sources, found {len(rows)}")
    return rows


def _csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    exposures = result.pop("exposes")
    result["terminal_metadata"] = json.dumps(
        result["terminal_metadata"], sort_keys=True, separators=(",", ":")
    )
    for key, value in exposures.items():
        result[f"exposes_{key}"] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pareto", type=Path, default=DEFAULT_PARETO)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()
    rows = build_rows(args.pareto)
    document = {
        "artifact_stamp": STAMP,
        "source_count": len(rows),
        "target_categories": sorted(TARGET_CATEGORIES),
        "sources": rows,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    csv_rows = [_csv_row(row) for row in rows]
    with args.csv_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    print(args.json_output)
    print(args.csv_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
