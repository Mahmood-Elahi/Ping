#!/usr/bin/env python3
"""Deterministically backfill/replay Freshness & Urgency v1 on a ranked artifact."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.scraper.company_careers.candidate_profile import load_candidate_profile
from src.scraper.company_careers.freshness import (
    POSSIBLY_MISSING,
    REMOVED,
    empty_freshness_state,
    load_freshness_config,
    parse_reference_time,
    reconcile_lifecycle,
    stable_job_key,
)
from src.scraper.company_careers.models import JobRecord
from src.scraper.company_careers.student_roles import rank_student_jobs


DEFAULT_AS_OF = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
SOURCE_ARTIFACT = REPOSITORY_ROOT / "jobs" / "company_careers_ranked_20260829_164545.json"
JSON_REPORT = REPOSITORY_ROOT / "output" / "freshness_v1_replay.json"
MARKDOWN_REPORT = REPOSITORY_ROOT / "output" / "freshness_v1_replay.md"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_ARTIFACT)
    parser.add_argument("--as-of", type=parse_reference_time, default=DEFAULT_AS_OF)
    parser.add_argument("--json-output", type=Path, default=JSON_REPORT)
    parser.add_argument("--markdown-output", type=Path, default=MARKDOWN_REPORT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_rows = _load_source_rows(args.source)
    source_jobs = [_job_from_mapping(item) for item in source_rows]
    fixture_jobs = source_jobs[:12]
    if len(fixture_jobs) < 12:
        raise RuntimeError("Freshness replay requires at least 12 source jobs")

    config = load_freshness_config()
    profile = load_candidate_profile()
    prior = reconcile_lifecycle(
        fixture_jobs[1:10], empty_freshness_state(),
        complete_companies={item.company for item in fixture_jobs[1:10]},
        as_of=args.as_of - timedelta(days=60), config=config,
        first_seen_basis="backfilled_at_reference_time",
    ).state
    _shape_previous_state(prior, fixture_jobs, args.as_of)

    ages = [0, 2, 5, 15, 30]
    current = [
        _with_timing(fixture_jobs[index], posted=args.as_of - timedelta(days=days))
        for index, days in enumerate(ages)
    ]
    current.extend(
        [
            _with_timing(fixture_jobs[7], posted=args.as_of - timedelta(days=8)),
            _with_timing(
                fixture_jobs[8], posted=args.as_of - timedelta(days=35),
                closes=(args.as_of + timedelta(days=1)).date().isoformat(),
            ),
            _with_timing(
                fixture_jobs[9], posted=args.as_of - timedelta(days=4),
                closes=(args.as_of + timedelta(days=7)).date().isoformat(),
            ),
            _with_timing(
                fixture_jobs[10], posted=args.as_of - timedelta(days=3),
                closes=(args.as_of - timedelta(days=1)).date().isoformat(),
            ),
            replace(
                fixture_jobs[11],
                raw_metadata={
                    **fixture_jobs[11].raw_metadata,
                    "source_posted_at": "not-a-date",
                },
            ),
        ]
    )
    complete_companies = {item.company for item in fixture_jobs}
    result = reconcile_lifecycle(
        current, prior, complete_companies=complete_companies,
        as_of=args.as_of, config=config,
    )
    before = rank_student_jobs(current, candidate_profile=profile, now=args.as_of)
    after = rank_student_jobs(
        current, candidate_profile=profile, now=args.as_of,
        freshness_by_key=result.annotations_by_key,
    )
    fixture_ranking = _ranking_invariance(before, after)
    full_result = reconcile_lifecycle(
        source_jobs,
        empty_freshness_state(),
        complete_companies={item.company for item in source_jobs},
        as_of=args.as_of,
        config=config,
        first_seen_basis="backfilled_at_reference_time",
    )
    full_before = rank_student_jobs(
        source_jobs, candidate_profile=profile, now=args.as_of
    )
    full_after = rank_student_jobs(
        source_jobs,
        candidate_profile=profile,
        now=args.as_of,
        freshness_by_key=full_result.annotations_by_key,
    )
    full_artifact = _full_artifact_report(
        source_rows=source_rows,
        result=full_result,
        before=full_before,
        after=full_after,
    )

    report = {
        "as_of": _timestamp(args.as_of),
        "source_artifact": str(args.source.relative_to(REPOSITORY_ROOT)),
        "source_artifact_jobs": len(source_rows),
        "historical_lifecycle_available": False,
        "backfill_policy": (
            "Historical rows without lifecycle timestamps use the explicit replay reference "
            "time and first_seen_basis=backfilled_at_reference_time; this is not represented "
            "as the actual discovery time."
        ),
        "validation_subset": {
            "purpose": (
                "Deterministic 10-current-row fixture subset shaped from the first "
                "12 artifact rows to exercise every freshness and urgency band, parse "
                "failure handling, rediscovery, reappearance, and absence removal."
            ),
            "source_rows_loaded": len(source_rows),
            "source_rows_used_to_shape_fixtures": len(fixture_jobs),
            "current_fixture_rows": len(current),
            "prior_only_fixture_rows": len(result.all_annotations_by_key) - len(current),
        },
        "summary": result.summary,
        "transitions": list(result.transitions),
        "date_parse_failures": list(result.date_parse_failures),
        "ranking_semantics_unchanged": fixture_ranking["all_invariants_hold"],
        "ranking_invariance": fixture_ranking,
        "representative_rows": _representative_rows(after),
        "ranked_jobs": after,
        "full_artifact": full_artifact,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    print(_console_summary(report))
    print(f"JSON report: {args.json_output.relative_to(REPOSITORY_ROOT)}")
    print(f"Markdown report: {args.markdown_output.relative_to(REPOSITORY_ROOT)}")
    return 0


def _load_source_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Frozen ranked artifact not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
        raise ValueError("Frozen ranked artifact must contain a JSON job list")
    return [dict(item) for item in data]


def _job_from_mapping(item: Mapping[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=str(item.get("job_id") or ""), title=str(item.get("title") or ""),
        company=str(item.get("company") or ""), location=str(item.get("location") or ""),
        url=str(item.get("url") or ""), updated_at=str(item.get("updated_at") or ""),
        source=str(item.get("source") or ""), departments=list(item.get("departments") or []),
        offices=list(item.get("offices") or []), description=item.get("description"),
        raw_metadata=dict(item.get("raw_metadata") or {}),
    )


def _with_timing(
    item: JobRecord, *, posted: datetime, closes: str | None = None
) -> JobRecord:
    metadata = {**item.raw_metadata, "source_posted_at": _timestamp(posted)}
    if closes is not None:
        metadata["application_deadline"] = closes
    return replace(item, raw_metadata=metadata)


def _shape_previous_state(
    state: dict[str, Any], jobs: list[JobRecord], as_of: datetime
) -> None:
    entries = state["jobs"]
    for item in jobs[1:10]:
        entry = entries[stable_job_key(item)]
        entry["first_seen_at"] = _timestamp(as_of - timedelta(days=30))
        entry["last_seen_at"] = _timestamp(as_of - timedelta(days=1))
        entry["source_posted_at"] = None
    entries[stable_job_key(jobs[5])]["lifecycle_status"] = POSSIBLY_MISSING
    entries[stable_job_key(jobs[5])]["missing_scan_count"] = 1
    entries[stable_job_key(jobs[6])]["lifecycle_status"] = POSSIBLY_MISSING
    entries[stable_job_key(jobs[6])]["missing_scan_count"] = 1
    entries[stable_job_key(jobs[7])]["lifecycle_status"] = REMOVED
    entries[stable_job_key(jobs[7])]["missing_scan_count"] = 2
    entries[stable_job_key(jobs[7])]["removed_at"] = _timestamp(as_of - timedelta(days=5))
    state["updated_at"] = _timestamp(as_of - timedelta(days=1))


def _representative_rows(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for item in ranked:
        chosen.setdefault((item["freshness_band"], item["urgency_band"]), item)
    return [
        {
            "company": item["company"], "title": item["title"],
            "ranking_score": item["ranking_score"],
            "freshness_band": item["freshness_band"], "age_days": item["age_days"],
            "urgency_band": item["urgency_band"],
            "urgency_reasons": item["urgency_reasons"],
            "source_closes_at": item["source_closes_at"],
            "lifecycle_status": item["lifecycle_status"],
        }
        for _, item in sorted(chosen.items())
    ]


def _full_artifact_report(
    *,
    source_rows: list[dict[str, Any]],
    result: Any,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    annotations = list(result.annotations_by_key.values())
    lifecycle = Counter(item.get("lifecycle_status") for item in annotations)
    freshness = Counter(item.get("freshness_band") for item in annotations)
    urgency = Counter(item.get("urgency_band") for item in annotations)
    age_basis = Counter(item.get("freshness_age_source") for item in annotations)
    first_seen_basis = Counter(item.get("first_seen_basis") for item in annotations)
    valid_freshness = {"new", "fresh", "normal", "aging", "stale"}
    valid_urgency = {"apply_now", "high", "normal", "low", "closed"}
    unclassifiable = sum(
        item.get("freshness_band") not in valid_freshness
        or item.get("urgency_band") not in valid_urgency
        for item in annotations
    )
    source_keys = [stable_job_key(item) for item in source_rows]
    outcomes = [
        {
            "source_key": item["source_key"],
            "lifecycle_status": item["lifecycle_status"],
            "first_seen_basis": item["first_seen_basis"],
            "freshness_age_source": item["freshness_age_source"],
            "freshness_band": item["freshness_band"],
            "urgency_band": item["urgency_band"],
            "date_parse_errors": item["date_parse_errors"],
        }
        for item in sorted(annotations, key=lambda value: value["source_key"])
    ]
    return {
        "total_input_jobs": len(source_rows),
        "unique_canonical_jobs": len(set(source_keys)),
        "duplicate_canonical_jobs": len(source_keys) - len(set(source_keys)),
        "successfully_annotated": len(annotations),
        "missing_first_seen_at_in_input": sum(
            not item.get("first_seen_at") for item in source_rows
        ),
        "using_observed_first_seen_at": first_seen_basis["observed_at_scan"],
        "using_backfilled_first_seen_at": first_seen_basis[
            "backfilled_at_reference_time"
        ],
        "trusted_source_posted_at": sum(
            item.get("source_posted_at") is not None for item in annotations
        ),
        "source_closes_at": sum(
            item.get("source_closes_at") is not None for item in annotations
        ),
        "lifecycle_distribution": {
            "active": lifecycle["active"],
            "possibly_missing": lifecycle["possibly_missing"],
            "removed": lifecycle["removed"],
        },
        "freshness_distribution": {
            band: freshness[band]
            for band in ("new", "fresh", "normal", "aging", "stale")
        },
        "urgency_distribution": {
            band: urgency[band]
            for band in ("apply_now", "high", "normal", "low", "closed")
        },
        "timestamp_basis_distribution": dict(sorted(age_basis.items())),
        "first_seen_basis_distribution": dict(sorted(first_seen_basis.items())),
        "parse_error_count": len(result.date_parse_failures),
        "unknown_or_unclassifiable_count": unclassifiable,
        "ranking_invariance": _ranking_invariance(before, after),
        "annotation_outcomes": outcomes,
    }


def _ranking_invariance(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, Any]:
    score_fields = (
        "score",
        "relevance_score",
        "relevance_band",
        "role_relevance_score",
        "role_relevance_band",
        "candidate_preference_score",
        "ranking_score",
        "domain_priority",
        "season_boost",
    )
    before_keys = [stable_job_key(item) for item in before]
    after_keys = [stable_job_key(item) for item in after]
    before_by_key = dict(zip(before_keys, before))
    after_by_key = dict(zip(after_keys, after))
    common = sorted(set(before_by_key) & set(after_by_key))
    differences = [
        {"source_key": key, "field": field}
        for key in common
        for field in score_fields
        if before_by_key[key].get(field) != after_by_key[key].get(field)
    ]
    eligibility_identical = set(before_keys) == set(after_keys)
    ordering_identical = before_keys == after_keys
    result = {
        "before_count": len(before),
        "after_count": len(after),
        "common_jobs": len(common),
        "eligibility_identical": eligibility_identical,
        "ordering_identical": ordering_identical,
        "score_fields_compared": list(score_fields),
        "score_difference_count": len(differences),
        "score_differences": differences,
    }
    result["all_invariants_hold"] = (
        eligibility_identical and ordering_identical and not differences
    )
    return result


def _console_summary(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    full = report["full_artifact"]
    return "\n".join(
        [
            "Freshness/Urgency v1 deterministic replay",
            "Fixture subset: 10 current rows shaped from 12 of "
            f"{report['source_artifact_jobs']} artifact rows",
            "Freshness new/fresh/normal/aging/stale: "
            f"{summary['new']}/{summary['fresh']}/{summary['normal']}/"
            f"{summary['aging']}/{summary['stale']}",
            "Urgency apply_now/high/normal/low/closed: "
            f"{summary['urgency_apply_now']}/{summary['urgency_high']}/"
            f"{summary['urgency_normal']}/{summary['urgency_low']}/"
            f"{summary['urgency_closed']}",
            f"Possibly missing/removed: {summary['possibly_missing']}/{summary['removed']}",
            f"Untrusted dates: {len(report['date_parse_failures'])}",
            f"Ranking semantics unchanged: {report['ranking_semantics_unchanged']}",
            "Full artifact annotated/input: "
            f"{full['successfully_annotated']}/{full['total_input_jobs']}",
            "Full artifact freshness new/fresh/normal/aging/stale: "
            + "/".join(
                str(full["freshness_distribution"][band])
                for band in ("new", "fresh", "normal", "aging", "stale")
            ),
            "Full artifact urgency apply_now/high/normal/low/closed: "
            + "/".join(
                str(full["urgency_distribution"][band])
                for band in ("apply_now", "high", "normal", "low", "closed")
            ),
            "Full artifact ranking invariance: "
            f"{full['ranking_invariance']['all_invariants_hold']}",
        ]
    )


def _markdown(report: Mapping[str, Any]) -> str:
    full = report["full_artifact"]
    lines = [
        "# Freshness/Urgency v1 Replay", "", report["backfill_policy"], "",
        "The band-coverage replay is an intentional deterministic fixture subset: "
        "10 current rows shaped from the first 12 rows of the source artifact. The "
        "full artifact is validated separately below without dropping rows.", "",
        "```text", _console_summary(report), "```", "", "## Representative rows", "",
        "| Company | Title | Freshness | Age days | Urgency | Reason | Status |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for item in report["representative_rows"]:
        lines.append(
            f"| {item['company']} | {item['title']} | {item['freshness_band']} | "
            f"{item['age_days']} | {item['urgency_band']} | "
            f"{', '.join(item['urgency_reasons'])} | {item['lifecycle_status']} |"
        )
    lines.extend(
        [
            "",
            "## Full artifact coverage",
            "",
            "| Measure | Count |",
            "|---|---:|",
            f"| Input jobs | {full['total_input_jobs']} |",
            f"| Successfully annotated | {full['successfully_annotated']} |",
            f"| Missing first_seen_at in input | {full['missing_first_seen_at_in_input']} |",
            f"| Observed first_seen_at | {full['using_observed_first_seen_at']} |",
            f"| Backfilled first_seen_at | {full['using_backfilled_first_seen_at']} |",
            f"| Trusted source_posted_at | {full['trusted_source_posted_at']} |",
            f"| source_closes_at | {full['source_closes_at']} |",
            f"| Active | {full['lifecycle_distribution']['active']} |",
            f"| Possibly missing | {full['lifecycle_distribution']['possibly_missing']} |",
            f"| Confirmed removed | {full['lifecycle_distribution']['removed']} |",
            f"| Parse errors | {full['parse_error_count']} |",
            f"| Unknown/unclassifiable | {full['unknown_or_unclassifiable_count']} |",
            "",
            "Full-artifact ranking ordering, eligibility, and all compared legacy score "
            f"fields identical: **{full['ranking_invariance']['all_invariants_hold']}**.",
        ]
    )
    return "\n".join(lines) + "\n"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
