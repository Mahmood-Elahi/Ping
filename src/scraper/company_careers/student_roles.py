"""Conservative title filters for student and internship searches."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, Optional

from src.scraper.company_careers.models import CompanyTarget, JobRecord


STUDENT_TITLE_PATTERNS = (
    r"\bintern\b",
    r"\binternship\b",
    r"\bco[ -]?op\b",
    r"\bstudent\b",
    r"\buniversity\b",
)
NEW_GRAD_TITLE_PATTERNS = (
    r"\bnew grad(?:uate)?\b",
    r"\bgraduate\b",
    r"\bentry[ -]?level\b",
)
SENIORITY_TITLE_PATTERN = re.compile(
    r"\b(?:senior|staff|principal|manager|director|lead|architect)\b",
    flags=re.IGNORECASE,
)
RESTRICTED_PROGRAM_PATTERNS = (
    (r"\b(?:dod\s+)?skillbridge\b", "skillbridge"),
    (r"\bmilitary fellowship\b", "military_fellowship"),
    (r"\btransitioning service member\b", "transitioning_service_member"),
    (r"\bveteran fellowship\b", "veteran_fellowship"),
)

CANADIAN_PROVINCE_TERMS = (
    "alberta",
    "british columbia",
    "manitoba",
    "new brunswick",
    "newfoundland and labrador",
    "nova scotia",
    "ontario",
    "prince edward island",
    "quebec",
    "saskatchewan",
    "northwest territories",
    "nunavut",
    "yukon",
)
CANADIAN_LOCATION_TERMS = CANADIAN_PROVINCE_TERMS + (
    "calgary",
    "edmonton",
    "fredericton",
    "halifax",
    "kitchener",
    "laval",
    "mississauga",
    "montreal",
    "ottawa",
    "quebec city",
    "regina",
    "saskatoon",
    "st. john's",
    "toronto",
    "vancouver",
    "waterloo",
    "winnipeg",
)
CANADIAN_PROVINCE_CODES = {
    "ab",
    "bc",
    "mb",
    "nb",
    "nl",
    "ns",
    "nt",
    "nu",
    "on",
    "pe",
    "qc",
    "sk",
    "yt",
}
US_LOCATION_TERMS = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "austin",
    "boston",
    "chicago",
    "denver",
    "los angeles",
    "new york city",
    "san diego",
    "san francisco",
    "seattle",
)
US_STATE_CODES = {
    "ak",
    "al",
    "ar",
    "az",
    "ca",
    "co",
    "ct",
    "dc",
    "de",
    "fl",
    "ga",
    "hi",
    "ia",
    "id",
    "il",
    "in",
    "ks",
    "ky",
    "la",
    "ma",
    "md",
    "me",
    "mi",
    "mn",
    "mo",
    "ms",
    "mt",
    "nc",
    "nd",
    "ne",
    "nh",
    "nj",
    "nm",
    "nv",
    "ny",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "va",
    "vt",
    "wa",
    "wi",
    "wv",
    "wy",
}

STUDENT_CATEGORY_PATTERNS = (
    ("software_engineering", r"\bsoftware\b|\bswe\b|\bsde\b|\bdeveloper\b", 20),
    ("machine_learning_ai", r"\bmachine learning\b|\bml\b|\bartificial intelligence\b|\bai\b", 18),
    ("robotics_autonomy", r"\brobot(?:ics)?\b|\bautonom(?:y|ous)\b", 18),
    ("computer_vision_perception", r"\bcomputer vision\b|\bperception\b", 18),
    (
        "embedded_firmware",
        r"\bembedded\b|\bfirmware\b|(?<!\w)c\+\+(?!\w)",
        20,
    ),
    (
        "hardware_silicon_electrical_computer",
        r"\bhardware\b|\bfpga\b|\bsilicon\b|\belectrical\b|\bcomputer engineering\b",
        18,
    ),
    ("research_applied_science", r"\bresearch(?:er)?\b|\bapplied scien(?:ce|tist)\b", 15),
)

# These adapters query by role text. Other adapters already fetch broad listings,
# while IBM's configured search term identifies the company rather than the role.
ROLE_QUERY_PLATFORMS = {
    "amazon",
    "custom:amazon",
    "google",
    "custom:google",
    "meta",
    "custom:meta",
}


def student_search_terms(include_new_grad: bool = False) -> list[str]:
    """Return the human-readable title terms covered by the student scan."""
    terms = ["Intern", "Internship", "Co-op", "Coop", "Student", "University"]
    if include_new_grad:
        terms.extend(["New Grad", "Graduate", "Entry Level"])
    return terms


def student_search_query(include_new_grad: bool = False) -> str:
    """Build one broad query for adapters that accept search text."""
    return " OR ".join(f'"{term}"' for term in student_search_terms(include_new_grad))


def configure_student_targets(
    targets: Iterable[CompanyTarget],
    *,
    include_new_grad: bool = False,
) -> list[CompanyTarget]:
    """Override senior-oriented role queries without mutating registry targets."""
    query = student_search_query(include_new_grad)
    return [
        replace(target, search_terms=[query])
        if target.platform in ROLE_QUERY_PLATFORMS
        else target
        for target in targets
    ]


def classify_student_location(
    job: JobRecord,
    *,
    locations: Optional[Iterable[str]] = None,
) -> str:
    """Classify location eligibility without inspecting the job description."""
    requested = [value.strip().lower() for value in (locations or []) if value.strip()]
    wants_canada = any(value == "canada" for value in requested)
    wants_remote = any(value == "remote" for value in requested)
    wants_united_states = any(
        re.search(r"\b(?:united states|u\.?s\.?a?)\b", value)
        for value in requested
    )

    location = (job.location or "").strip()
    location_lower = location.lower()
    title_location = f"{job.title} {location}".lower()
    remote = bool(re.search(r"\bremote\b", location_lower))
    worldwide = bool(
        re.search(r"\b(?:worldwide|global|anywhere)\b", location_lower)
    )
    north_america = "north america" in location_lower
    united_states = _has_us_location(location_lower) or bool(
        re.search(
            r"\b(?:united states|u\.?s\.?a?)\b(?:\s*-?\s*only)?",
            location_lower,
        )
        or re.search(
            r"\b(?:united states|u\.?s\.?a?)[ -]?only\b",
            (job.title or "").lower(),
        )
    )
    explicit_canada = bool(
        re.search(r"\b(?:canada|canadian)\b", title_location)
    ) or (
        any(term in location_lower for term in CANADIAN_PROVINCE_TERMS)
        or bool(_location_codes(location_lower) & CANADIAN_PROVINCE_CODES)
    )
    canada = explicit_canada or (
        _has_canadian_location(location_lower) and not united_states
    )

    # Explicit Canada/worldwide eligibility wins over a simultaneous US mention.
    if canada and wants_canada:
        return "eligible_remote_canada" if remote else "eligible_canada"
    if worldwide and (wants_canada or wants_remote):
        return "eligible_worldwide"
    if north_america and wants_canada:
        return "eligible_north_america"
    if united_states:
        return "eligible_requested" if wants_united_states else "excluded_us_only"
    if not location:
        return "manual_review_missing"
    if remote and wants_remote:
        return "eligible_remote_unspecified"
    if any(value in location_lower or location_lower in value for value in requested):
        return "eligible_requested"
    return "excluded_other_location"


def student_location_exclusion_reason(
    job: JobRecord,
    *,
    locations: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Return the scanner counter category for an ineligible location."""
    status = classify_student_location(job, locations=locations)
    return "location" if status.startswith("excluded_") else None


def build_student_location_filter(
    *,
    locations: Optional[Iterable[str]] = None,
) -> Callable[[JobRecord], Optional[str]]:
    """Create the opt-in student location callback for the scanner."""
    return partial(student_location_exclusion_reason, locations=list(locations or []))


def student_job_exclusion_reason(
    job: JobRecord,
    *,
    include_new_grad: bool = False,
) -> Optional[str]:
    """Return an exclusion category, or ``None`` when a title is retained.

    Seniority is checked only in the title. Descriptions and departments are
    intentionally ignored so unrelated prose cannot exclude a student role.
    """
    title = job.title or ""
    if SENIORITY_TITLE_PATTERN.search(title):
        return "seniority"
    if _restricted_program_signals(title):
        return "eligibility"

    patterns = list(STUDENT_TITLE_PATTERNS)
    if include_new_grad:
        patterns.extend(NEW_GRAD_TITLE_PATTERNS)
    if not any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in patterns):
        return "job_type"

    return None


def build_student_job_filter(
    *,
    include_new_grad: bool = False,
) -> Callable[[JobRecord], Optional[str]]:
    """Create the scanner callback for one student-search configuration."""
    return partial(
        student_job_exclusion_reason,
        include_new_grad=include_new_grad,
    )


def score_student_job(
    job: JobRecord,
    *,
    include_new_grad: bool = False,
    locations: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Return a deterministic, student-focused relevance score and explanation."""
    title = (job.title or "").lower()
    score = 0
    matched_categories: list[str] = []
    positive_signals: list[str] = []
    negative_signals: list[str] = []

    student_signals = (
        (r"\binternship\b", "internship", 34),
        (r"\bintern\b", "intern", 34),
        (r"\bco[ -]?op\b", "co_op", 34),
        (r"\bstudent\b", "student", 30),
        (r"\buniversity\b", "university", 28),
    )
    for pattern, signal, points in student_signals:
        if re.search(pattern, title):
            score += points
            positive_signals.append(signal)

    if include_new_grad:
        new_grad_signals = (
            (r"\bnew grad(?:uate)?\b", "new_grad"),
            (r"\bgraduate\b", "graduate"),
            (r"\bentry[ -]?level\b", "entry_level"),
        )
        for pattern, signal in new_grad_signals:
            if re.search(pattern, title):
                score += 28
                positive_signals.append(signal)

    for category, pattern, points in STUDENT_CATEGORY_PATTERNS:
        if re.search(pattern, title):
            score += points
            matched_categories.append(category)
            positive_signals.extend(_pattern_signal_words(category, title))

    location_status = classify_student_location(job, locations=locations)
    location_points = {
        "eligible_canada": 16,
        "eligible_remote_canada": 18,
        "eligible_worldwide": 14,
        "eligible_north_america": 12,
        "eligible_remote_unspecified": 10,
        "eligible_requested": 10,
        "manual_review_missing": 0,
    }
    if location_status in location_points:
        score += location_points[location_status]
        if location_status != "manual_review_missing":
            positive_signals.append(location_status)
    elif location_status == "excluded_us_only":
        score -= 40
        negative_signals.append("us_only")
    else:
        score -= 20
        negative_signals.append("outside_requested_locations")

    seniority_signals = _seniority_signals(title)
    if seniority_signals:
        score -= 45
        negative_signals.extend(seniority_signals)

    restricted_signals = _restricted_program_signals(title)
    if restricted_signals:
        score -= 60
        negative_signals.extend(restricted_signals)

    recency_signal, recency_points = _recency_signal(job.updated_at, now=now)
    if recency_signal:
        score += recency_points
        positive_signals.append(recency_signal)

    score = max(0, min(100, score))
    return {
        "score": score,
        "band": _student_relevance_band(score),
        "matched_categories": _unique(matched_categories),
        "positive_signals": _unique(positive_signals),
        "negative_signals": _unique(negative_signals),
        "location_status": location_status,
    }


def rank_student_jobs(
    jobs: Iterable[JobRecord],
    *,
    include_new_grad: bool = False,
    locations: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Rank normalized jobs using only the opt-in student scoring rules."""
    ranked = []
    for job in jobs:
        relevance = score_student_job(
            job,
            include_new_grad=include_new_grad,
            locations=locations,
        )
        item = job.to_dict()
        item.update(
            {
                "score": relevance["score"],
                "relevance_score": relevance["score"],
                "relevance_band": relevance["band"],
                "matched_categories": relevance["matched_categories"],
                "positive_signals": relevance["positive_signals"],
                "negative_signals": relevance["negative_signals"],
                "location_status": relevance["location_status"],
            }
        )
        ranked.append(item)

    ranked.sort(
        key=lambda item: (-item["relevance_score"], item["company"], item["title"])
    )
    return ranked[:limit] if limit else ranked


def write_student_ranked_jobs_artifacts(
    jobs: Iterable[JobRecord],
    *,
    json_path: str,
    csv_path: str,
    keywords: Optional[str] = None,
    locations: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    include_new_grad: bool = False,
) -> None:
    """Write student-scored JSON and CSV artifacts using existing output paths."""
    del keywords
    ranked = rank_student_jobs(
        jobs,
        include_new_grad=include_new_grad,
        locations=locations,
        limit=limit,
    )
    Path(json_path).write_text(
        json.dumps(ranked, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    fieldnames = [
        "company",
        "title",
        "location",
        "relevance_score",
        "relevance_band",
        "matched_categories",
        "positive_signals",
        "negative_signals",
        "location_status",
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
                    "matched_categories": "; ".join(item["matched_categories"]),
                    "positive_signals": "; ".join(item["positive_signals"]),
                    "negative_signals": "; ".join(item["negative_signals"]),
                    "location_status": item["location_status"],
                    "url": item["url"],
                    "source": item["source"],
                    "updated_at": item["updated_at"],
                }
            )


def _has_canadian_location(location: str) -> bool:
    if any(term in location for term in CANADIAN_LOCATION_TERMS):
        return True
    return bool(_location_codes(location) & CANADIAN_PROVINCE_CODES)


def _has_us_location(location: str) -> bool:
    if any(term in location for term in US_LOCATION_TERMS):
        return True
    return bool(_location_codes(location) & US_STATE_CODES)


def _location_codes(location: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(r"(?:^|[,/|])\s*([A-Za-z]{2})(?=\s*(?:$|[,/|]))", location)
    }


def _seniority_signals(title: str) -> list[str]:
    return [
        term
        for term in ("senior", "staff", "principal", "manager", "director", "lead", "architect")
        if re.search(rf"\b{term}\b", title)
    ]


def _restricted_program_signals(title: str) -> list[str]:
    return [
        signal
        for pattern, signal in RESTRICTED_PROGRAM_PATTERNS
        if re.search(pattern, title, flags=re.IGNORECASE)
    ]


def _pattern_signal_words(category: str, title: str) -> list[str]:
    signal_patterns = {
        "software_engineering": ("software", "swe", "sde", "developer"),
        "machine_learning_ai": ("machine learning", "ml", "artificial intelligence", "ai"),
        "robotics_autonomy": ("robotics", "robot", "autonomy", "autonomous"),
        "computer_vision_perception": ("computer vision", "perception"),
        "embedded_firmware": ("embedded", "firmware", "c++"),
        "hardware_silicon_electrical_computer": (
            "hardware",
            "fpga",
            "silicon",
            "electrical",
            "computer engineering",
        ),
        "research_applied_science": ("research", "researcher", "applied science"),
    }
    return [signal for signal in signal_patterns[category] if signal in title]


def _recency_signal(
    updated_at: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[Optional[str], int]:
    if not updated_at:
        return None, 0
    try:
        posted_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None, 0
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_days = (current - posted_at).days
    if age_days < 0:
        return None, 0
    if age_days <= 14:
        return "recent_source_date_within_14_days", 8
    if age_days <= 30:
        return "recent_source_date_within_30_days", 5
    if age_days <= 90:
        return "recent_source_date_within_90_days", 2
    return None, 0


def _student_relevance_band(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 65:
        return "good"
    if score >= 50:
        return "possible"
    return "low"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
