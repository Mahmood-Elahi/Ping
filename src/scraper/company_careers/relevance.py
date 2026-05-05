"""Relevance scoring for candidate-facing company-careers review."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable, Optional

from src.scraper.company_careers.models import JobRecord


POSSIBLE_RELEVANCE_SCORE = 55
DEFAULT_MIN_RELEVANCE_SCORE = 65


POSITIVE_PATTERNS = [
    (r"\bsenior staff\b", 38, "senior staff level"),
    (r"\bprincipal\b", 39, "principal level"),
    (r"\bstaff\b", 42, "staff level"),
    (r"\btechnical leadership\b|\bleadership\b", 24, "technical leadership scope"),
    (r"\btech lead\b|\blead engineer\b", 18, "technical lead scope"),
    (r"\bsenior\b", 10, "senior level"),
    (r"\bsoftware engineer\b|\bswe\b|\bsde\b", 30, "software engineering role"),
    (r"\bbackend\b|\bback-end\b", 14, "backend systems"),
    (r"\bplatform\b", 14, "platform engineering"),
    (r"\binfrastructure\b", 14, "infrastructure engineering"),
    (r"\bdistributed systems?\b", 14, "distributed systems"),
    (r"\breliability\b|\bsre\b|\bproduction engineer\b", 14, "reliability engineering"),
    (r"\bdata engineer\b|\bdata platform\b", 12, "data engineering"),
    (r"\bmachine learning\b|\bml\b|\bai\b", 12, "ML/AI engineering"),
    (r"\bcloud\b|\bkubernetes\b|\bcompute\b", 10, "cloud infrastructure"),
    (r"\bsecurity engineer\b", 10, "security engineering"),
    (r"\bengineer\b|\bengineering\b", 8, "engineering role"),
]

NEGATIVE_PATTERNS = [
    (r"\bsolutions engineer\b|\bsolution engineer\b", -70, "solutions engineering"),
    (r"\bsales engineer\b|\bpre-sales\b|\bpresales\b", -70, "sales engineering"),
    (r"\bsupport engineer\b|\btechnical support\b", -65, "support role"),
    (r"\bcustomer engineer\b|\bcustomer success\b|\baccount executive\b", -60, "customer-facing role"),
    (r"\btechnical account\b|\btam\b", -55, "technical account role"),
    (r"\bconsultant\b|\bprofessional services\b", -50, "consulting/services role"),
    (r"\bengineering manager\b|\bmanager[, ]|\bmanager\b", -45, "people management"),
    (r"\bdirector\b|\bhead of\b|\bvp\b|\bvice president\b", -55, "leadership management"),
    (r"\bproduct manager\b|\bprogram manager\b|\bproject manager\b", -60, "product/program management"),
    (r"\brecruit", -60, "recruiting role"),
    (r"\bhr\b|\bpeople operations\b", -60, "HR role"),
    (r"\bfinance\b|\blegal\b|\bprocurement\b", -60, "business operations role"),
]

HARD_EXCLUSION_PATTERNS = [
    r"\bsolutions engineer\b",
    r"\bsales engineer\b",
    r"\bsupport engineer\b",
    r"\btechnical account\b",
    r"\bengineering manager\b",
    r"\bdirector\b",
    r"\bproduct manager\b",
    r"\bprogram manager\b",
    r"\brecruit",
    r"\bfinance\b",
    r"\blegal\b",
]

TARGET_SENIORITY_PATTERNS = [
    r"\bprincipal\b",
    r"\bstaff\b",
    r"\btechnical leadership\b",
    r"\bleadership\b",
    r"\btech lead\b",
    r"\blead engineer\b",
]


def score_job(
    job: JobRecord,
    *,
    keywords: Optional[str] = None,
    locations: Optional[Iterable[str]] = None,
) -> dict:
    """Return a deterministic relevance score and explanation for one job."""
    title = job.title.lower()
    context = " ".join(
        [
            job.title,
            " ".join(job.departments),
            " ".join(job.offices),
        ]
    ).lower()
    score = 0
    reasons: list[str] = []
    penalties: list[str] = []

    for pattern, points, reason in POSITIVE_PATTERNS:
        if re.search(pattern, title) or re.search(pattern, context):
            score += points
            reasons.append(reason)

    for pattern, points, reason in NEGATIVE_PATTERNS:
        if re.search(pattern, title):
            score += points
            penalties.append(reason)

    keyword_bonus = _keyword_bonus(title, keywords)
    if keyword_bonus:
        score += keyword_bonus
        reasons.append("keyword intent match")

    location_bonus = _location_bonus(job.location, locations)
    if location_bonus:
        score += location_bonus
        reasons.append("target location match")

    if any(re.search(pattern, title) for pattern in HARD_EXCLUSION_PATTERNS):
        score = min(score, DEFAULT_MIN_RELEVANCE_SCORE - 1)
    if not any(re.search(pattern, title) for pattern in TARGET_SENIORITY_PATTERNS):
        score = min(score, DEFAULT_MIN_RELEVANCE_SCORE - 1)

    score = max(0, min(100, score))
    return {
        "score": score,
        "band": relevance_band(score),
        "is_relevant": score >= DEFAULT_MIN_RELEVANCE_SCORE,
        "reasons": _unique(reasons),
        "penalties": _unique(penalties),
    }


def is_relevant_job(
    job: JobRecord,
    *,
    keywords: Optional[str] = None,
    locations: Optional[Iterable[str]] = None,
    min_score: int = DEFAULT_MIN_RELEVANCE_SCORE,
) -> bool:
    return score_job(job, keywords=keywords, locations=locations)["score"] >= min_score


def rank_jobs(
    jobs: Iterable[JobRecord],
    *,
    keywords: Optional[str] = None,
    locations: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    ranked = []
    for job in jobs:
        relevance = score_job(job, keywords=keywords, locations=locations)
        item = job.to_dict()
        item.update(
            {
                "relevance_score": relevance["score"],
                "relevance_band": relevance["band"],
                "relevance_reasons": relevance["reasons"],
                "relevance_penalties": relevance["penalties"],
            }
        )
        ranked.append(item)

    ranked.sort(key=lambda item: (-item["relevance_score"], item["company"], item["title"]))
    return ranked[:limit] if limit else ranked


def write_ranked_jobs_artifacts(
    jobs: Iterable[JobRecord],
    *,
    json_path: str,
    csv_path: str,
    keywords: Optional[str] = None,
    locations: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> None:
    ranked = rank_jobs(jobs, keywords=keywords, locations=locations, limit=limit)
    Path(json_path).write_text(json.dumps(ranked, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = [
        "company",
        "title",
        "location",
        "relevance_score",
        "relevance_band",
        "relevance_reasons",
        "relevance_penalties",
        "url",
        "source",
        "updated_at",
    ]
    with Path(csv_path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in ranked:
            writer.writerow(
                {
                    "company": item["company"],
                    "title": item["title"],
                    "location": item["location"],
                    "relevance_score": item["relevance_score"],
                    "relevance_band": item["relevance_band"],
                    "relevance_reasons": "; ".join(item["relevance_reasons"]),
                    "relevance_penalties": "; ".join(item["relevance_penalties"]),
                    "url": item["url"],
                    "source": item["source"],
                    "updated_at": item["updated_at"],
                }
            )


def relevance_band(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 65:
        return "good"
    if score >= POSSIBLE_RELEVANCE_SCORE:
        return "possible"
    return "low"


def _keyword_bonus(title: str, keywords: Optional[str]) -> int:
    phrases = [phrase.strip().lower() for phrase in re.split(r"[,;]", keywords or "") if phrase.strip()]
    if not phrases:
        return 0
    bonus = 0
    for phrase in phrases:
        words = phrase.split()
        if phrase in title:
            bonus = max(bonus, 18)
        elif words and all(word in title for word in words):
            bonus = max(bonus, 12)
        elif any(word in title for word in words):
            bonus = max(bonus, 4)
    return bonus


def _location_bonus(job_location: str, locations: Optional[Iterable[str]]) -> int:
    requested = [loc.strip().lower() for loc in (locations or []) if loc and loc.strip()]
    if not requested:
        return 0
    location = (job_location or "").lower()
    if not location:
        return 0
    if any(loc in location or location in loc for loc in requested):
        return 8
    if "remote" in location:
        return 6
    return 0


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
