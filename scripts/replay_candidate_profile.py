#!/usr/bin/env python3
"""Deterministically compare legacy and candidate-profile ranking on one artifact."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.candidate_profile import (
    DEFAULT_CANDIDATE_PROFILE_PATH,
    candidate_profile_exclusion_reason,
    load_candidate_profile,
)
from src.scraper.company_careers.models import JobRecord
from src.scraper.company_careers.student_roles import (
    rank_student_jobs,
    student_job_exclusion_reason,
    student_location_exclusion_reason,
)


DEFAULT_INPUT = "jobs/company_careers_ranked_20260829_164545.json"
DEFAULT_OUTPUT = "output/candidate_profile_v1_replay.json"
DEFAULT_REPORT = "output/candidate_profile_v1_replay.md"
REPLAY_NOW = datetime(2026, 8, 29, 16, 45, 45, tzinfo=ZoneInfo("America/Edmonton"))
JOB_FIELDS = (
    "job_id", "title", "company", "location", "url", "updated_at", "source",
    "departments", "offices", "description", "raw_metadata",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--profile", default=DEFAULT_CANDIDATE_PROFILE_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--movement-threshold", type=int, default=20)
    return parser.parse_args(argv)


def reconstruct(item: dict[str, Any]) -> JobRecord:
    missing = [field for field in JOB_FIELDS if field not in item]
    if missing:
        raise ValueError(f"Job {item.get('job_id')!r} lacks replay fields: {missing}")
    return JobRecord(**{field: item[field] for field in JOB_FIELDS})


def identity(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("company") or "").casefold(),
        str(item.get("job_id") or ""),
        str(item.get("title") or "").casefold(),
        str(item.get("url") or "").casefold(),
    )


def movement_item(
    item: dict[str, Any], baseline_by_id: dict[tuple[str, str, str, str], dict[str, Any]]
) -> dict[str, Any]:
    before = baseline_by_id.get(identity(item))
    return {
        "company": item["company"],
        "title": item["title"],
        "location": item["location"],
        "before_rank": before["rank"] if before else None,
        "after_rank": item["rank"],
        "rank_delta": before["rank"] - item["rank"] if before else None,
        "role_relevance_score": item["role_relevance_score"],
        "candidate_preference_score": item["candidate_preference_score"],
        "ranking_score": item["ranking_score"],
        "candidate_preference_reasons": item["candidate_preference_reasons"],
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    def cell(value: Any) -> str:
        if isinstance(value, list):
            value = "; ".join(str(part) for part in value)
        return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")

    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(cell(value) for value in row) + " |" for row in rows),
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_path = ROOT / args.input
    source = json.loads(source_path.read_text(encoding="utf-8"))
    jobs = [reconstruct(item) for item in source]
    profile = load_candidate_profile(str(ROOT / args.profile))

    baseline_input = [
        job for job in jobs
        if student_job_exclusion_reason(job) is None
        and student_location_exclusion_reason(job) is None
    ]
    profiled_input = [
        job for job in jobs
        if student_job_exclusion_reason(job, candidate_profile=profile) is None
        and student_location_exclusion_reason(
            job, allow_international=profile.allows_international
        ) is None
    ]

    baseline = rank_student_jobs(
        baseline_input,
        locations=["Canada", "United States", "Remote"],
        now=REPLAY_NOW,
    )
    profiled = rank_student_jobs(
        profiled_input,
        locations=["Canada", "United States", "Remote"],
        candidate_profile=profile,
        now=REPLAY_NOW,
    )
    for rank, item in enumerate(baseline, start=1):
        item["rank"] = rank
    for rank, item in enumerate(profiled, start=1):
        item["rank"] = rank
    baseline_by_id = {identity(item): item for item in baseline}
    movements = [movement_item(item, baseline_by_id) for item in profiled]
    comparable = [item for item in movements if item["rank_delta"] is not None]
    newly_eligible = [item for item in movements if item["rank_delta"] is None]
    promoted = sorted(comparable, key=lambda item: (-item["rank_delta"], item["after_rank"]))
    demoted = sorted(comparable, key=lambda item: (item["rank_delta"], item["after_rank"]))
    significant_up = [item for item in promoted if item["rank_delta"] >= args.movement_threshold]
    significant_down = [item for item in demoted if item["rank_delta"] <= -args.movement_threshold]

    excluded = []
    profiled_ids = {identity(item) for item in profiled}
    for item, record in zip(source, jobs):
        reason = candidate_profile_exclusion_reason(record, profile)
        source_identity = (
            record.company.casefold(), record.job_id, record.title.casefold(), record.url.casefold()
        )
        if reason and source_identity not in profiled_ids:
            excluded.append(
                {"company": record.company, "title": record.title, "location": record.location, "reason": reason}
            )

    replay = {
        "metadata": {
            "source_artifact": args.input.replace("\\", "/"),
            "source_jobs": len(source),
            "source_jobs_removed_by_current_eligibility": len(source) - len(baseline_input),
            "baseline_jobs": len(baseline),
            "profiled_jobs": len(profiled),
            "profile": args.profile.replace("\\", "/"),
            "replay_now": REPLAY_NOW.isoformat(),
            "movement_threshold": args.movement_threshold,
            "significant_promotions": len(significant_up),
            "significant_demotions": len(significant_down),
            "defense_exclusions": len(excluded),
            "newly_eligible_international_jobs": len(newly_eligible),
        },
        "promotions": significant_up,
        "demotions": significant_down,
        "excluded": excluded,
        "newly_eligible": newly_eligible,
        "all_profiled_jobs": profiled,
    }
    output_path = ROOT / args.output
    report_path = ROOT / args.report
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(replay, indent=2, ensure_ascii=False), encoding="utf-8")

    row = lambda item: [
        item["after_rank"], item["before_rank"], item["rank_delta"], item["company"],
        item["title"], item["role_relevance_score"], item["candidate_preference_score"],
        item["ranking_score"], item["candidate_preference_reasons"],
    ]
    report = [
        "# Candidate Profile v1 deterministic replay", "",
        f"- Source: `{args.input}` ({len(source)} jobs)",
        f"- Jobs retained by current pre-profile eligibility: {len(baseline_input)}",
        f"- Frozen scoring time: `{REPLAY_NOW.isoformat()}`",
        f"- Profiled jobs: {len(profiled)}", f"- Defense exclusions: {len(excluded)}", "",
        f"## Largest promotions (threshold: {args.movement_threshold})", "",
        *md_table(
            ["After", "Before", "Delta", "Company", "Title", "Role", "Preference", "Final", "Reasons"],
            [row(item) for item in significant_up[:20]],
        ), "", f"## Largest demotions (threshold: {args.movement_threshold})", "",
        *md_table(
            ["After", "Before", "Delta", "Company", "Title", "Role", "Preference", "Final", "Reasons"],
            [row(item) for item in significant_down[:20]],
        ), "", "## Excluded", "",
        *md_table(
            ["Company", "Title", "Location", "Reason"],
            [[item["company"], item["title"], item["location"], item["reason"]] for item in excluded],
        ), "", "## Newly eligible international roles", "",
        *md_table(
            ["After", "Company", "Title", "Location", "Reasons"],
            [[item["after_rank"], item["company"], item["title"], item["location"], item["candidate_preference_reasons"]] for item in newly_eligible],
        ), "",
    ]
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(replay["metadata"], indent=2))
    print(f"Wrote {output_path}")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
