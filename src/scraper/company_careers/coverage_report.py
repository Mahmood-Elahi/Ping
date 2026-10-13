"""Coverage report writers for company career-site scans."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from src.scraper.company_careers.models import CompanyCareersReport, CoverageStatus


STATUS_ORDER = [
    CoverageStatus.FETCHED,
    CoverageStatus.FETCHED_NO_MATCHING_ROLES,
    CoverageStatus.MANUAL_REVIEW_REQUIRED,
    CoverageStatus.FAILED_TECHNICAL,
    CoverageStatus.NOT_IMPLEMENTED,
    CoverageStatus.PARTIAL_LINKEDIN_ONLY,
    CoverageStatus.COVERED_VIA_PARENT_SOURCE,
    CoverageStatus.MAPPED_NO_CURRENT_JOBS,
    CoverageStatus.PARENT_FAILED_TECHNICAL,
    CoverageStatus.PARENT_SOURCE_NOT_CONFIGURED,
]

FAILURE_STATUSES = {
    CoverageStatus.MANUAL_REVIEW_REQUIRED,
    CoverageStatus.FAILED_TECHNICAL,
    CoverageStatus.NOT_IMPLEMENTED,
    CoverageStatus.PARTIAL_LINKEDIN_ONLY,
}


def coverage_markdown(
    report: CompanyCareersReport,
    *,
    jobs_path: Optional[str] = None,
    ranked_jobs_path: Optional[str] = None,
    coverage_json_path: Optional[str] = None,
    failures_json_path: Optional[str] = None,
    title: str = "Company Careers Coverage",
) -> str:
    coverage = report.coverage()
    counts = Counter(item["status"] for item in coverage)
    source_health = report.source_health_summary()
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        f"- Canonical sources checked: {len(report.results)}",
        f"- Mapped child brands represented: {len(report.mapped_coverage)}",
        f"- Matched jobs: {len(report.jobs)}",
        f"- Coverage health: {source_health['coverage_health']}",
        f"- Complete sources: {source_health['complete']}",
        f"- Incomplete sources: {source_health['incomplete']}",
        f"- Failed sources: {source_health['failed']}",
    ]
    if report.original_coverage:
        lines.extend(
            [
                "- Original-list companies covered: "
                f"{report.original_coverage.get('total_original_companies_covered', 0)}"
                f"/{report.original_coverage.get('original_list_total', 0)}",
                "- Mapped child brands covered: "
                f"{report.original_coverage.get('mapped_child_brands_covered', 0)}",
            ]
        )

    for status in STATUS_ORDER:
        if counts.get(status):
            lines.append(f"- {status}: {counts[status]}")

    if jobs_path or ranked_jobs_path or coverage_json_path or failures_json_path:
        lines.extend(["", "## Artifacts", ""])
        if jobs_path:
            lines.append(f"- Jobs output: `{jobs_path}`")
        if ranked_jobs_path:
            lines.append(f"- Ranked review output: `{ranked_jobs_path}`")
        if coverage_json_path:
            lines.append(f"- Coverage JSON: `{coverage_json_path}`")
        if failures_json_path:
            lines.append(f"- Failure JSON: `{failures_json_path}`")

    failures = failure_items(report)
    if failures:
        lines.extend(
            [
                "",
                "## Failures And Manual Follow-Up",
                "",
                "| Company | Status | Error Type | Error | Remediation |",
                "|---|---|---|---|---|",
            ]
        )
        for item in failures:
            lines.append(
                "| {company} | {status} | {error_type} | {error} | {remediation} |".format(
                    company=_escape_md(item["company"]),
                    status=_escape_md(item["status"]),
                    error_type=_escape_md(item.get("error_type") or ""),
                    error=_escape_md(item.get("error") or ""),
                    remediation=_escape_md(item.get("remediation") or ""),
                )
            )

    lines.extend(
        [
            "",
            "## Company Coverage",
            "",
            "| Company | Status | Matched | Fetched | Platform | Source | Notes |",
            "|---|---:|---:|---:|---|---|---|",
        ]
    )
    for item in sorted(coverage, key=_coverage_sort_key):
        lines.append(
            "| {company} | {status} | {matched_count} | {fetched_count} | {platform} | {source} | {notes} |".format(
                company=_escape_md(item["company"]),
                status=_escape_md(item["status"]),
                matched_count=item["matched_count"],
                fetched_count=item["fetched_count"],
                platform=_escape_md(item["platform"]),
                source=_escape_md(_source_label(item)),
                notes=_escape_md(_note_label(item)),
            )
        )

    if report.jobs:
        lines.extend(
            [
                "",
                "## Matched Jobs By Company",
                "",
                "| Company | Count |",
                "|---|---:|",
            ]
        )
        by_company = Counter(job.company for job in report.jobs)
        for company, count in sorted(by_company.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {_escape_md(company)} | {count} |")

    lines.append("")
    return "\n".join(lines)


def write_coverage_artifacts(
    report: CompanyCareersReport,
    *,
    markdown_path: str,
    csv_path: str,
    failures_json_path: Optional[str] = None,
    jobs_path: Optional[str] = None,
    ranked_jobs_path: Optional[str] = None,
    coverage_json_path: Optional[str] = None,
) -> None:
    Path(markdown_path).write_text(
        coverage_markdown(
            report,
            jobs_path=jobs_path,
            ranked_jobs_path=ranked_jobs_path,
            coverage_json_path=coverage_json_path,
            failures_json_path=failures_json_path,
        ),
        encoding="utf-8",
    )
    _write_coverage_csv(report.coverage(), csv_path)
    if failures_json_path:
        _write_failures_json(report, failures_json_path)


def failure_items(report: CompanyCareersReport) -> list[dict]:
    return [
        item
        for item in sorted(report.coverage(), key=_coverage_sort_key)
        if item["status"] in FAILURE_STATUSES
    ]


def _write_coverage_csv(coverage: Iterable[dict], csv_path: str) -> None:
    fieldnames = [
        "company",
        "status",
        "matched_count",
        "fetched_count",
        "scan_state",
        "source_health_code",
        "source_activation_state",
        "inventory_authority",
        "positive_inventory_usable",
        "result_authority",
        "authoritative_for_absence",
        "can_reconcile_absence",
        "scan_completeness_reason",
        "stop_reason",
        "terminal_evidence_type",
        "total_available",
        "platform",
        "source",
        "error",
        "error_type",
        "remediation",
        "coverage_relationship",
        "canonical_parent",
        "parent_scan_status",
        "mapped_jobs",
        "mapping_enabled",
        "match_field",
        "warnings",
    ]
    with Path(csv_path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in sorted(coverage, key=_coverage_sort_key):
            writer.writerow(
                {
                    "company": item["company"],
                    "status": item["status"],
                    "matched_count": item["matched_count"],
                    "fetched_count": item["fetched_count"],
                    "scan_state": item.get("scan_state") or item.get("scan_health") or "",
                    "source_health_code": item.get("source_health_code") or "",
                    "source_activation_state": item.get("source_activation_state") or "",
                    "inventory_authority": item.get("inventory_authority") or "authoritative",
                    "positive_inventory_usable": item.get("positive_inventory_usable", False),
                    "result_authority": item.get("result_authority") or "incomplete",
                    "authoritative_for_absence": item.get("authoritative_for_absence", False),
                    "can_reconcile_absence": item.get("can_reconcile_absence", False),
                    "scan_completeness_reason": item.get("scan_completeness_reason") or "",
                    "stop_reason": item.get("stop_reason") or "",
                    "terminal_evidence_type": item.get("terminal_evidence_type") or "",
                    "total_available": item.get("total_available", ""),
                    "platform": item["platform"],
                    "source": _source_label(item),
                    "error": item.get("error") or "",
                    "error_type": item.get("error_type") or "",
                    "remediation": item.get("remediation") or "",
                    "coverage_relationship": item.get("coverage_relationship") or "canonical_source",
                    "canonical_parent": item.get("canonical_parent") or "",
                    "parent_scan_status": item.get("parent_scan_status") or item.get("status") or "",
                    "mapped_jobs": item.get("mapped_jobs", ""),
                    "mapping_enabled": item.get("mapping_enabled", ""),
                    "match_field": item.get("match_field") or "",
                    "warnings": "; ".join(item.get("warnings") or []),
                }
            )


def _write_failures_json(report: CompanyCareersReport, failures_json_path: str) -> None:
    payload = {
        "total_companies": len(report.results),
        "failure_count": len(failure_items(report)),
        "failures": failure_items(report),
    }
    Path(failures_json_path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _coverage_sort_key(item: dict) -> tuple[int, str]:
    try:
        status_index = STATUS_ORDER.index(item["status"])
    except ValueError:
        status_index = len(STATUS_ORDER)
    return status_index, item["company"]


def _source_label(item: dict) -> str:
    if item.get("parent_source"):
        return str(item["parent_source"])
    if item.get("slug"):
        return str(item["slug"])
    if item.get("tenant"):
        site = item.get("site_id") or ""
        return f"{item['tenant']}:{site}" if site else str(item["tenant"])
    if item.get("site_ids"):
        return ", ".join(item["site_ids"])
    return ""


def _note_label(item: dict) -> str:
    if item.get("error"):
        return str(item["error"])
    if item["status"] == CoverageStatus.FETCHED_NO_MATCHING_ROLES:
        return "Fetched successfully; no roles matched current filters."
    if item["status"] == CoverageStatus.FETCHED:
        return "Fetched successfully."
    if item.get("coverage_relationship") == "parent_company_source":
        parent = item.get("canonical_parent") or "parent"
        count = item.get("mapped_jobs", 0)
        return f"Covered via {parent}; {count} exact mapped jobs."
    return ""


def _escape_md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
