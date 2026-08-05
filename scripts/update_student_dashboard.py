#!/usr/bin/env python3
"""Scan student roles once, persist them, and publish the opt-in dashboard."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.integrations.google_sheets_publisher import GoogleSheetsPublisher
from src.integrations.supabase_job_store import SupabaseJobStore
from src.scraper.company_careers.models import CoverageStatus
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.student_roles import (
    build_student_job_filter,
    build_student_search_diagnostics,
    build_student_location_filter,
    configure_student_targets,
    rank_student_jobs,
    student_search_query,
    student_search_terms,
    write_student_ranked_jobs_artifacts,
)
from src.utils.job_search_pipeline import JobSearchPipeline


DEFAULT_LOCATIONS = ["Canada", "United States", "Remote"]
DEFAULT_MAX_JOBS = None


def configure_console() -> None:
    """Prevent upstream output from failing on legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update the student-job Supabase and Google Sheets dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--locations",
        nargs="+",
        default=DEFAULT_LOCATIONS,
        help="Locations retained by the existing student location matcher",
    )
    parser.add_argument(
        "--max-jobs",
        type=positive_integer,
        default=DEFAULT_MAX_JOBS,
        help="Optional maximum retained jobs per company (default keeps every eligible role)",
    )
    parser.add_argument(
        "--include-new-grad",
        action="store_true",
        help="Deprecated compatibility flag; new-grad roles remain excluded",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run the live scan and diagnostics with Supabase, Sheets, and SQLite disabled",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without scanning or calling external services",
    )
    parser.add_argument(
        "--skip-sheets",
        action="store_true",
        help="Update Supabase without publishing Google Sheets",
    )
    return parser.parse_args(argv)


def print_plan(args: argparse.Namespace, company_count: int) -> None:
    print("Student dashboard update")
    print("  Job type: internship")
    print("  Experience level: entry")
    print(f"  Locations: {', '.join(args.locations)}")
    print("  Scraper: company_careers")
    print(f"  Maximum jobs per company: {args.max_jobs or 'unlimited'}")
    print("  Include new-grad roles: no")
    print(f"  Title terms: {', '.join(student_search_terms(args.include_new_grad))}")
    print(f"  Configured companies: {company_count}")
    external_disabled = args.dry_run or args.audit
    print(f"  Supabase: {'disabled (read-only)' if external_disabled else 'enabled'}")
    sheets_status = (
        "disabled (--skip-sheets)"
        if args.skip_sheets
        else "disabled (read-only)" if external_disabled else "enabled"
    )
    print(f"  Google Sheets: {sheets_status}")
    print("  SQLite writes: disabled")


def print_required_environment(*, skip_sheets: bool) -> None:
    print("\nRequired environment variables")
    print("  SUPABASE_URL")
    print("  SUPABASE_SERVICE_ROLE_KEY")
    print("  CLOSE_AFTER_MISSING_RUNS (optional; default 2)")
    if skip_sheets:
        print("  Google Sheets variables: not required with --skip-sheets")
    else:
        print("  GOOGLE_SHEET_ID")
        print("  GOOGLE_CREDENTIALS_PATH")
        print("  NEW_JOB_WINDOW_DAYS (optional; default 7)")


def build_pipeline(args: argparse.Namespace, targets: list[Any]) -> JobSearchPipeline:
    scanner = CompanyCareersScanner(targets=targets)
    return JobSearchPipeline(
        keywords=student_search_query(args.include_new_grad),
        locations=args.locations,
        job_type="internship",
        experience_level="entry",
        max_jobs_per_site=args.max_jobs,
        scrapers=["company_careers"],
        use_database=False,
        company_careers_scanner=scanner,
        company_careers_job_filter=build_student_job_filter(
            include_new_grad=args.include_new_grad,
        ),
        company_careers_location_filter=build_student_location_filter(
            locations=args.locations,
        ),
        company_careers_ranked_writer=partial(
            write_student_ranked_jobs_artifacts,
            include_new_grad=args.include_new_grad,
        ),
        company_careers_diagnostics_builder=build_student_search_diagnostics,
    )


def run_update(args: argparse.Namespace) -> int:
    targets = configure_student_targets(
        load_target_companies(),
        include_new_grad=args.include_new_grad,
    )
    print_plan(args, len(targets))

    if args.dry_run:
        print_required_environment(skip_sheets=args.skip_sheets)
        print("\nNo scan, artifacts, SQLite writes, Supabase calls, or Google calls were made.")
        return 0

    if args.audit:
        pipeline = build_pipeline(args, targets)
        pipeline.search_jobs()
        summary = pipeline.company_careers_summary
        if summary is None:
            raise RuntimeError("The company-careers pipeline did not expose its scan summary")
        print_audit_summary(summary)
        return 0

    started_at = datetime.now(timezone.utc)
    job_store = SupabaseJobStore.from_env()
    job_store.validate_setup()
    publisher = None if args.skip_sheets else GoogleSheetsPublisher.from_env()
    pipeline = build_pipeline(args, targets)

    try:
        pipeline.search_jobs()
        report = pipeline.company_careers_report
        summary = pipeline.company_careers_summary
        if report is None or summary is None:
            raise RuntimeError("The company-careers pipeline did not expose its scan report")

        ranked_jobs = rank_student_jobs(
            report.jobs,
            include_new_grad=args.include_new_grad,
            locations=args.locations,
        )
        successful_companies = [
            result.target.name
            for result in report.results
            if result.status
            in {CoverageStatus.FETCHED, CoverageStatus.FETCHED_NO_MATCHING_ROLES}
        ]
        sync_result = job_store.sync_student_jobs(
            ranked_jobs,
            successful_companies=successful_companies,
            started_at=started_at,
            companies_checked=summary["companies_checked"],
            jobs_fetched=summary["jobs_fetched"],
            exclusion_counts=summary["exclusion_counts"],
            coverage_report_path=summary["coverage_report_path"],
            ranked_artifact_path=summary["ranked_results_path"],
        )
    except Exception as exc:
        _record_failed_scan(job_store, pipeline, started_at, exc)
        raise

    company_statuses = {
        result.target.name: result.status for result in report.results
    }
    sheet_counts: dict[str, int] | None = None
    if publisher is not None:
        dashboard_data = job_store.fetch_dashboard_data()
        sheet_counts = publisher.publish(
            active_jobs=dashboard_data.active_jobs,
            scan_run=dashboard_data.latest_scan,
            companies=report.coverage(),
            company_statuses=company_statuses,
            coverage_summary=report.original_coverage,
        )

    print_summary(summary, sync_result, sheet_counts, skip_sheets=args.skip_sheets)
    return 0


def _record_failed_scan(
    job_store: SupabaseJobStore,
    pipeline: JobSearchPipeline,
    started_at: datetime,
    exc: Exception,
) -> None:
    summary = pipeline.company_careers_summary or {}
    try:
        job_store.record_failed_scan(
            started_at=started_at,
            error_message=f"{exc.__class__.__name__}: {exc}",
            companies_checked=summary.get("companies_checked", 0),
            jobs_fetched=summary.get("jobs_fetched", 0),
            jobs_retained=summary.get("jobs_retained", 0),
            exclusion_counts=summary.get("exclusion_counts", {}),
            coverage_report_path=summary.get("coverage_report_path"),
            ranked_artifact_path=summary.get("ranked_results_path"),
        )
    except Exception:
        pass


def print_summary(
    summary: dict[str, Any],
    sync_result: Any,
    sheet_counts: dict[str, int] | None,
    *,
    skip_sheets: bool,
) -> None:
    exclusions = summary["exclusion_counts"]
    print("\nDashboard update summary")
    print(f"  Companies checked: {summary['companies_checked']}")
    print(f"  Canonical sources checked: {summary.get('canonical_sources_checked', summary['companies_checked'])}")
    print(f"  Mapped child brands covered: {summary.get('mapped_child_brands_covered', 0)}")
    print(
        "  Original companies covered: "
        f"{summary.get('total_original_companies_covered', 0)}/"
        f"{summary.get('original_list_total', 0)}"
    )
    print(f"  Jobs fetched: {summary['jobs_fetched']}")
    print(f"  Jobs retained: {summary['jobs_retained']}")
    print(f"  Exclusion counts: {exclusions}")
    print(f"  Supabase inserts: {sync_result.inserted}")
    print(f"  Supabase updates: {sync_result.updated}")
    print(f"  Supabase unchanged: {sync_result.unchanged}")
    print(f"  Missing counts advanced: {sync_result.missing_incremented}")
    print(f"  Jobs newly closed: {sync_result.closed}")
    print(f"  Coverage report path: {summary['coverage_report_path']}")
    print(f"  Ranked-results path: {summary['ranked_results_path']}")
    if skip_sheets:
        print("  Google Sheets: skipped")
    elif sheet_counts is not None:
        print("  Google Sheets: published")
        print(f"  New Jobs rows: {sheet_counts['new_jobs']}")
        print(f"  Active Jobs rows: {sheet_counts['active_jobs']}")
        print(f"  Companies rows: {sheet_counts['companies']}")


def print_audit_summary(summary: dict[str, Any]) -> None:
    diagnostics = summary.get("diagnostics") or {}
    print("\nRead-only search audit")
    print("  Supabase: disabled")
    print("  Google Sheets: disabled")
    print("  SQLite: disabled")
    print(f"  Total normalized jobs: {diagnostics.get('total_normalized_jobs', 0)}")
    print(f"  Before retained jobs: {diagnostics.get('before_retained_jobs', 0)}")
    print(f"  After retained jobs: {diagnostics.get('after_retained_jobs', 0)}")
    print(f"  Exclusions by reason: {diagnostics.get('exclusions_by_reason', {})}")
    print(
        "  Newly accepted by rule: "
        f"{diagnostics.get('newly_accepted_jobs_by_matching_rule', {})}"
    )
    print(f"  Location distribution: {diagnostics.get('location_distribution', {})}")
    print(f"  Title-signal distribution: {diagnostics.get('title_signal_distribution', {})}")
    print(f"  Season distribution: {diagnostics.get('season_distribution', {})}")
    print(f"  Diagnostics path: {summary.get('diagnostics_path')}")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv)
    try:
        return run_update(args)
    except Exception as exc:
        print(f"[ERROR] Dashboard update failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
