#!/usr/bin/env python3
"""Run a conservative student-role scan through RoleRadar's existing pipeline."""

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

from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.candidate_profile import (
    DEFAULT_CANDIDATE_PROFILE_PATH,
    load_candidate_profile,
)
from src.scraper.company_careers.models import CoverageStatus
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
from src.integrations.supabase_job_store import SupabaseJobStore
from src.utils.job_search_pipeline import JobSearchPipeline


DEFAULT_LOCATIONS = ["Canada", "United States", "Remote"]
DEFAULT_MAX_JOBS = None


def configure_console() -> None:
    """Prevent upstream emoji output from failing on legacy Windows code pages."""
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
        description="Scan direct company career sites for student roles.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--locations",
        nargs="+",
        default=DEFAULT_LOCATIONS,
        help="Locations retained by RoleRadar's existing location matcher",
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
        "--dry-run",
        action="store_true",
        help="Print the scan plan without fetching, writing artifacts, or updating the database",
    )
    return parser.parse_args(argv)


def print_plan(args: argparse.Namespace, company_count: int) -> None:
    print("Student job scan")
    print("  Job type: internship")
    print("  Experience level: entry")
    print(f"  Locations: {', '.join(args.locations)}")
    print("  Scraper: company_careers")
    print(f"  Maximum jobs per company: {args.max_jobs or 'unlimited'}")
    print("  Include new-grad roles: no")
    print(f"  Title terms: {', '.join(student_search_terms(args.include_new_grad))}")
    print(f"  Configured companies: {company_count}")
    print(f"  Candidate profile: {args.candidate_profile or 'disabled'}")


def print_summary(summary: dict[str, Any] | None, *, dry_run: bool) -> None:
    print("\nScan summary")
    if dry_run:
        not_run = "not run (dry-run)"
        print(f"  Companies checked: {not_run}")
        print(f"  Jobs fetched: {not_run}")
        print(f"  Jobs retained: {not_run}")
        print(f"  Jobs excluded by seniority: {not_run}")
        print(f"  Jobs excluded by student job type: {not_run}")
        print(f"  Jobs excluded by location: {not_run}")
        print(f"  Jobs excluded by restricted eligibility program: {not_run}")
        print(f"  Jobs omitted by per-company limit: {not_run}")
        print(f"  Database inserts: {not_run}")
        print(f"  Existing duplicates: {not_run}")
        print("  Coverage report path: not created (dry-run)")
        print("  Ranked-results path: not created (dry-run)")
        return

    if summary is None:
        raise RuntimeError("The company-careers pipeline did not expose a scan summary")

    exclusions = summary["exclusion_counts"]
    print(f"  Companies checked: {summary['companies_checked']}")
    print(f"  Jobs fetched: {summary['jobs_fetched']}")
    print(f"  Jobs retained: {summary['jobs_retained']}")
    print(f"  Jobs excluded by seniority: {exclusions.get('seniority', 0)}")
    print(f"  Jobs excluded by student job type: {exclusions.get('job_type', 0)}")
    print(f"  Jobs excluded by location: {exclusions.get('location', 0)}")
    print(
        "  Jobs excluded by restricted eligibility program: "
        f"{exclusions.get('eligibility', 0)}"
    )
    print(f"  Jobs omitted by per-company limit: {exclusions.get('limit', 0)}")
    print(f"  Database inserts: {summary['database_inserts']}")
    print(f"  Existing duplicates: {summary['existing_duplicates']}")
    print(f"  Supabase inserts: {summary['supabase_inserts']}")
    print(f"  Supabase updates: {summary['supabase_updates']}")
    print(f"  Supabase unchanged: {summary['supabase_unchanged']}")
    print(f"  Jobs newly closed: {summary['supabase_closed']}")
    print(f"  Coverage report path: {summary['coverage_report_path']}")
    print(f"  Ranked-results path: {summary['ranked_results_path']}")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv)
    candidate_profile = (
        load_candidate_profile(args.candidate_profile)
        if args.candidate_profile else None
    )
    targets = configure_student_targets(
        load_target_companies(apply_reliability_repairs=True),
        include_new_grad=args.include_new_grad,
    )
    print_plan(args, len(targets))

    if args.dry_run:
        print_summary(None, dry_run=True)
        return 0

    started_at = datetime.now(timezone.utc)
    job_store = SupabaseJobStore.from_env()
    scanner = CompanyCareersScanner(targets=targets)
    pipeline = JobSearchPipeline(
        keywords=student_search_query(args.include_new_grad),
        locations=args.locations,
        job_type="internship",
        experience_level="entry",
        max_jobs_per_site=args.max_jobs,
        scrapers=["company_careers"],
        use_database=True,
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
        ),
        company_careers_diagnostics_builder=partial(
            build_student_search_diagnostics,
            candidate_profile=candidate_profile,
        ),
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
        summary.update(
            {
                "supabase_inserts": sync_result.inserted,
                "supabase_updates": sync_result.updated,
                "supabase_unchanged": sync_result.unchanged,
                "supabase_closed": sync_result.closed,
            }
        )
    except Exception as exc:
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
        raise
    print_summary(pipeline.company_careers_summary, dry_run=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
