#!/usr/bin/env python3
"""Scan student roles once, persist them, and publish the opt-in dashboard."""

from __future__ import annotations

import argparse
import sys
import time
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
from src.scraper.company_careers.freshness import (
    DEFAULT_FRESHNESS_CONFIG_PATH,
    FreshnessRuntime,
    empty_freshness_state,
    load_freshness_config,
    parse_reference_time,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.candidate_profile import (
    DEFAULT_CANDIDATE_PROFILE_PATH,
    CandidateProfile,
    load_candidate_profile,
)
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
        "--candidate-profile",
        default=DEFAULT_CANDIDATE_PROFILE_PATH,
        help="Candidate-preference YAML applied after role relevance",
    )
    parser.add_argument(
        "--no-candidate-profile",
        action="store_const",
        const=None,
        dest="candidate_profile",
        help="Disable candidate preferences and preserve legacy ranking",
    )
    parser.add_argument(
        "--freshness-config",
        default=DEFAULT_FRESHNESS_CONFIG_PATH,
        help="Freshness/lifecycle YAML applied before final ranking",
    )
    parser.add_argument(
        "--no-freshness",
        action="store_const",
        const=None,
        dest="freshness_config",
        help="Disable Freshness v1 and preserve the previous ranking path",
    )
    parser.add_argument(
        "--as-of",
        type=parse_reference_time,
        help="Deterministic ISO-8601 reference time for replay/testing",
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
    print(f"  Candidate profile: {args.candidate_profile or 'disabled'}")
    print(f"  Freshness: {args.freshness_config or 'disabled'}")
    if args.freshness_config:
        print("  Lifecycle state: Supabase tracked_jobs")
    if args.as_of:
        print(f"  Reference time: {args.as_of.isoformat()}")
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
    print("  CLOSE_AFTER_MISSING_RUNS (legacy --no-freshness path only)")
    if skip_sheets:
        print("  Google Sheets variables: not required with --skip-sheets")
    else:
        print("  GOOGLE_SHEET_ID")
        print("  GOOGLE_CREDENTIALS_PATH")
        print("  NEW_JOB_WINDOW_DAYS (optional; default 7)")


def build_pipeline(
    args: argparse.Namespace,
    targets: list[Any],
    candidate_profile: CandidateProfile | None,
    freshness_runtime: FreshnessRuntime | None = None,
    as_of: datetime | None = None,
) -> JobSearchPipeline:
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
            candidate_profile=candidate_profile,
        ),
        company_careers_location_filter=build_student_location_filter(
            locations=args.locations,
            allow_international=bool(
                candidate_profile and candidate_profile.allows_international
            ),
        ),
        company_careers_ranked_writer=partial(
            write_student_ranked_jobs_artifacts,
            include_new_grad=args.include_new_grad,
            candidate_profile=candidate_profile,
            freshness_by_key=(
                freshness_runtime.annotations if freshness_runtime else None
            ),
            now=as_of,
        ),
        company_careers_report_processor=(
            freshness_runtime.process_report if freshness_runtime else None
        ),
        company_careers_diagnostics_builder=partial(
            build_student_search_diagnostics,
            candidate_profile=candidate_profile,
        ),
    )


def run_update(args: argparse.Namespace) -> int:
    run_started = time.monotonic()
    candidate_profile = (
        load_candidate_profile(args.candidate_profile)
        if args.candidate_profile else None
    )
    freshness_config = (
        load_freshness_config(args.freshness_config)
        if args.freshness_config else None
    )
    targets = configure_student_targets(
        load_target_companies(apply_reliability_repairs=True),
        include_new_grad=args.include_new_grad,
    )
    print_plan(args, len(targets))

    if args.dry_run:
        print_required_environment(skip_sheets=args.skip_sheets)
        print("\nNo scan, artifacts, SQLite writes, Supabase calls, or Google calls were made.")
        return 0

    as_of = args.as_of or datetime.now(timezone.utc)
    if args.audit:
        freshness_runtime = (
            FreshnessRuntime(
                config=freshness_config,
                previous_state=empty_freshness_state(),
                as_of=as_of,
                first_seen_basis="audit_reference_time",
            )
            if freshness_config is not None else None
        )
        pipeline = build_pipeline(
            args, targets, candidate_profile, freshness_runtime, as_of
        )
        pipeline.search_jobs()
        summary = pipeline.company_careers_summary
        if summary is None:
            raise RuntimeError("The company-careers pipeline did not expose its scan summary")
        print_audit_summary(summary)
        return 0

    started_at = as_of
    job_store = SupabaseJobStore.from_env(
        now_factory=lambda: as_of,
        close_after_missing_runs=(
            freshness_config.removal.consecutive_complete_scans
            if freshness_config is not None else None
        ),
    )
    job_store.validate_setup()
    freshness_runtime = (
        FreshnessRuntime(
            config=freshness_config,
            previous_state=job_store.fetch_lifecycle_state(),
            as_of=as_of,
        )
        if freshness_config is not None else None
    )
    publisher = None if args.skip_sheets else GoogleSheetsPublisher.from_env()
    pipeline = build_pipeline(
        args, targets, candidate_profile, freshness_runtime, as_of
    )

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
            candidate_profile=candidate_profile,
            freshness_by_key=(
                freshness_runtime.annotations if freshness_runtime else None
            ),
            now=as_of,
        )
        summary["final_ranked_jobs"] = len(ranked_jobs)
        successful_companies = [
            result.target.name
            for result in report.results
            if result.status
            in {CoverageStatus.FETCHED, CoverageStatus.FETCHED_NO_MATCHING_ROLES}
            and result.scan_complete
        ]
        sync_result = job_store.sync_student_jobs(
            ranked_jobs,
            successful_companies=successful_companies,
            started_at=started_at,
            companies_checked=summary["companies_checked"],
            jobs_fetched=summary["jobs_fetched"],
            lifecycle_jobs=(
                freshness_runtime.lifecycle_jobs(report.normalized_jobs)
                if freshness_runtime else None
            ),
            exclusion_counts=summary["exclusion_counts"],
            coverage_report_path=summary["coverage_report_path"],
            ranked_artifact_path=summary["ranked_results_path"],
            normalized_jobs=summary.get("normalized_jobs", len(report.normalized_jobs)),
            source_summary=summary.get("scan_health_counts", {}),
            scan_runtime_seconds=summary.get("scan_runtime_seconds"),
        )
    except Exception as exc:
        _record_failed_scan(job_store, pipeline, started_at, exc)
        raise

    company_statuses = {
        result.target.name: result.status for result in report.results
    }
    sheet_counts: dict[str, int] | None = None
    if publisher is not None:
        try:
            dashboard_data = job_store.fetch_dashboard_data()
            sheet_scan_run = _publication_preview(
                dashboard_data.latest_scan,
                status="publishing",
                runtime_seconds=time.monotonic() - run_started,
            )
            sheet_counts = publisher.publish(
                active_jobs=dashboard_data.active_jobs,
                scan_run=sheet_scan_run,
                companies=report.coverage(),
                company_statuses=company_statuses,
                coverage_summary=report.original_coverage,
            )
            job_store.record_publication_result(
                started_at=started_at,
                status="published",
                success=True,
                runtime_seconds=time.monotonic() - run_started,
            )
        except Exception as exc:
            try:
                job_store.record_publication_result(
                    started_at=started_at,
                    status="failed",
                    success=False,
                    runtime_seconds=time.monotonic() - run_started,
                    error_message=f"{exc.__class__.__name__}: {exc}",
                )
            except Exception as record_exc:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "Publication failure status could not be persisted safely: "
                        f"{record_exc}"
                    )
            raise
    else:
        job_store.record_publication_result(
            started_at=started_at,
            status="skipped",
            success=True,
            runtime_seconds=time.monotonic() - run_started,
        )

    runtime_seconds = round(time.monotonic() - run_started, 3)
    print_summary(
        summary,
        sync_result,
        sheet_counts,
        skip_sheets=args.skip_sheets,
        runtime_seconds=runtime_seconds,
    )
    return 0


def _publication_preview(
    scan_run: dict[str, Any] | None,
    *,
    status: str,
    runtime_seconds: float,
) -> dict[str, Any]:
    values = dict(scan_run or {})
    diagnostics = dict(values.get("diagnostics") or {})
    diagnostics.update(
        {
            "scan_success": True,
            "publication_status": status,
            "runtime_seconds": round(runtime_seconds, 3),
        }
    )
    values["diagnostics"] = diagnostics
    return values


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
    runtime_seconds: float,
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
    print(f"  Normalized jobs: {summary.get('normalized_jobs', summary['jobs_fetched'])}")
    print(f"  Jobs retained: {summary['jobs_retained']}")
    print(f"  Eligible/student jobs: {summary.get('eligible_student_jobs', summary['jobs_retained'])}")
    print(f"  Final ranked jobs: {summary.get('final_ranked_jobs', summary['jobs_retained'])}")
    health = summary.get("scan_health_counts", {})
    print(f"  Sources attempted: {summary['companies_checked']}")
    print(f"  Complete sources: {health.get('complete', 0)}")
    print(f"  Incomplete sources: {health.get('incomplete', 0)}")
    print(f"  Failed sources: {health.get('failed', 0)}")
    print(f"  Exclusion counts: {exclusions}")
    print(f"  Supabase inserts: {sync_result.inserted}")
    print(f"  Supabase updates: {sync_result.updated}")
    print(f"  Supabase unchanged: {sync_result.unchanged}")
    print(f"  Missing counts advanced: {sync_result.missing_incremented}")
    print(f"  Jobs newly closed: {sync_result.closed}")
    print(f"  Coverage report path: {summary['coverage_report_path']}")
    print(f"  Ranked-results path: {summary['ranked_results_path']}")
    freshness = summary.get("freshness")
    if freshness:
        print(f"  Freshness new/fresh: {freshness['new']}/{freshness['fresh']}")
        print(
            "  Possibly missing/removed: "
            f"{freshness['possibly_missing']}/{freshness['removed']}"
        )
        print(
            "  Apply now/high urgency: "
            f"{freshness['urgency_apply_now']}/{freshness['urgency_high']}"
        )
        print("  Lifecycle state: Supabase tracked_jobs")
    if skip_sheets:
        print("  Google Sheets: skipped")
    elif sheet_counts is not None:
        print("  Google Sheets: published")
        print(f"  New Jobs rows: {sheet_counts['new_jobs']}")
        print(f"  Active Jobs rows: {sheet_counts['active_jobs']}")
        print(f"  Companies rows: {sheet_counts['companies']}")
    print(f"  Runtime seconds: {runtime_seconds}")


def print_audit_summary(summary: dict[str, Any]) -> None:
    diagnostics = summary.get("diagnostics") or {}
    print("\nRead-only search audit")
    print("  Supabase: disabled")
    print("  Google Sheets: disabled")
    print("  SQLite: disabled")
    print(f"  Total normalized jobs: {diagnostics.get('total_normalized_jobs', 0)}")
    print(f"  Source health: {summary.get('scan_health_counts', {})}")
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
    freshness = summary.get("freshness")
    if freshness:
        print(
            "  Freshness new/fresh/normal/aging/stale: "
            f"{freshness['new']}/{freshness['fresh']}/{freshness['normal']}/"
            f"{freshness['aging']}/{freshness['stale']}"
        )
        print(
            "  Lifecycle/date parse errors: "
            f"{summary.get('date_parse_error_count', len(summary.get('date_parse_failures', [])))}"
        )
        print(
            "  Lifecycle/date diagnostics: "
            f"{summary.get('date_diagnostic_counts', {})}"
        )
        print("  Lifecycle state: evaluated but not changed (audit)")
    removed = diagnostics.get("graduate_only_roles_removed", [])
    ambiguous = diagnostics.get("ambiguous_roles_retained", [])
    false_positive_samples = diagnostics.get("false_positive_review_samples", [])
    print(f"  Graduate-only roles removed: {len(removed)}")
    for item in removed:
        print(
            f"    - {item['company']} | {item['title']} | {item['education_reason']} | "
            f"evidence: {item['education_evidence']}"
        )
    print(f"  Ambiguous roles retained for review: {len(ambiguous)}")
    for item in ambiguous:
        print(
            f"    - {item['company']} | {item['title']} | {item['education_reason']} | "
            f"evidence: {item['education_evidence']}"
        )
    print(f"  False-positive review samples: {len(false_positive_samples)}")
    for item in false_positive_samples:
        print(
            f"    - {item['company']} | {item['title']} | "
            f"{item['education_eligibility']} | {item['education_reason']} | "
            f"evidence: {item['education_evidence']}"
        )
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
