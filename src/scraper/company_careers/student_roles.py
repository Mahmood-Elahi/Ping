"""Student-role eligibility, search scope, ranking, and diagnostics."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from src.scraper.company_careers.education_eligibility import (
    AMBIGUOUS,
    GRADUATE_ONLY,
    UNDERGRADUATE_ALLOWED,
    annotate_education_eligibility,
    classify_education_eligibility,
)
from src.scraper.company_careers.candidate_profile import (
    CandidateProfile,
    candidate_profile_exclusion_reason,
    combine_ranking_scores,
    score_candidate_preferences,
)
from src.scraper.company_careers.freshness import stable_job_key
from src.scraper.company_careers.models import CompanyTarget, JobRecord


STUDENT_ROLE_QUERIES = (
    "intern",
    "internship",
    "co-op",
    "student",
    "undergraduate",
    "engineering intern",
    "software intern",
    "research intern",
    "machine learning intern",
    "AI intern",
    "robotics intern",
    "embedded intern",
    "firmware intern",
    "hardware intern",
    "electrical engineering intern",
)

STRONG_STUDENT_PATTERNS = (
    ("university_intern", r"\buniversity\s*[-/]?\s*intern(?:ship)?s?\b"),
    ("student_engineer", r"\bstudents?\s*[-/]?\s*engineers?\b"),
    ("research_intern", r"\bresearch\s*[-/]?\s*intern(?:ship)?s?\b"),
    ("student_researcher", r"\bstudents?\s*[-/]?\s*researchers?\b"),
    ("summer_analyst", r"\bsummer\s*[-/]?\s*analysts?\b"),
    ("off_cycle_intern", r"\boff\s*[- /]?\s*cycle\s*[-/]?\s*intern(?:ship)?s?\b"),
    ("industrial_placement", r"\bindustrial\s*[-/]?\s*placements?\b"),
    ("year_in_industry", r"\byears?\s*(?:[-/]\s*|\s+)in\s*(?:[-/]\s*|\s+)(?:the\s+)?industry\b"),
    ("working_student", r"\bworking\s*[-/]?\s*students?\b"),
    ("undergraduate", r"\bundergraduates?\b"),
    ("internship", r"\binternships?\b"),
    ("intern", r"\binterns?\b"),
    ("co_op", r"\bco\s*(?:[-/]\s*)?op(?:s)?\b"),
    ("student", r"\bstudents?\b"),
)
CONTEXTUAL_STUDENT_PATTERNS = (
    ("summer", r"\bsummers?\b"),
    ("fall", r"\bfall\b"),
    ("winter", r"\bwinter\b"),
    ("university", r"\buniversit(?:y|ies)\b"),
    ("campus", r"\bcampus(?:es)?\b"),
    ("early_talent", r"\bearly\s*[-/]?\s*talent\b"),
    ("emerging_talent", r"\bemerging\s*[-/]?\s*talent\b"),
    ("placement", r"\bplacements?\b"),
    ("temporary", r"\btemporary\b|\btemp\b"),
)

NEW_GRAD_PATTERNS = (
    r"\bnew\s*[-/]?\s*grads?\b",
    r"\bnew\s*[-/]?\s*graduates?\b",
    r"\bgraduate\s*[-/]?\s*program(?:me)?s?\b",
    r"\bgraduate\s*[-/]?\s*scheme\b",
)
SENIORITY_TITLE_PATTERN = re.compile(
    r"\b(?:senior|staff|principal|manager|director|lead|architect|head|executive)\b|"
    r"(?:^|[^A-Za-z])(?:vp|v\.p\.)(?:[^A-Za-z]|$)|\bvice\s+president\b",
    flags=re.IGNORECASE,
)
RESTRICTED_PROGRAM_PATTERNS = (
    (r"\b(?:dod\s+)?skillbridge\b", "skillbridge"),
    (r"\bmilitary fellowship\b", "military_fellowship"),
    (r"\btransitioning service member\b", "transitioning_service_member"),
    (r"\bveteran fellowship\b", "veteran_fellowship"),
    (
        r"(?:\b(?:intern(?:ship)?|program)\b[^|]{0,60}\bmilitary veterans?\b|"
        r"\bmilitary veterans?\b[^|]{0,60}\b(?:intern(?:ship)?|program)\b|"
        r"\bveteran(?:\s*[-/]?\s*specific)?\s+(?:internship|program)\b|"
        r"\bveterans?\s+only\b)",
        "veteran_restricted_program",
    ),
    (
        r"\bmilitary spouses?\b[^|]{0,60}\b(?:internship|program)\b|"
        r"\bmilitary\s*[-/]?\s*affiliated\s+(?:internship|program)\b",
        "military_affiliated_program",
    ),
    (r"\bexperienced\s*[-/]?\s*(?:hires?|professionals?)\b", "experienced_hire"),
)
RESTRICTED_DESCRIPTION_PATTERNS = (
    (
        r"\b(?:eligibility|applicants?|candidates?|internship|program|role|position)\b"
        r"[^.;]{0,100}\b(?:limited|restricted|exclusive(?:ly)?|only|must\s+be)\b"
        r"[^.;]{0,100}\b(?:active\s+duty\s+transitioning\s+service\s+members?|"
        r"military\s+veterans?|veterans?|military\s+spouses?)\b|"
        r"\b(?:active\s+duty\s+transitioning\s+service\s+members?|military\s+veterans?|"
        r"veterans?|military\s+spouses?)\b[^.;]{0,60}\bonly\b",
        "military_veteran_eligibility_restriction",
    ),
)
CANADIAN_PROVINCE_TERMS = (
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "ontario",
    "prince edward island", "quebec", "saskatchewan",
    "northwest territories", "nunavut", "yukon",
)
CANADIAN_LOCATION_TERMS = CANADIAN_PROVINCE_TERMS + (
    "calgary", "edmonton", "fredericton", "halifax", "kitchener", "laval",
    "mississauga", "montreal", "ottawa", "quebec city", "regina",
    "saskatoon", "st. john's", "toronto", "vancouver", "waterloo", "winnipeg",
)
CANADIAN_PROVINCE_CODES = {
    "ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt",
}
US_LOCATION_TERMS = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district of columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "austin", "boston", "chicago", "denver", "los angeles", "new york city",
    "san diego", "san francisco", "san jose", "seattle",
)
US_STATE_CODES = {
    "ak", "al", "ar", "az", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi",
    "ia", "id", "il", "in", "ks", "ky", "la", "ma", "md", "me", "mi", "mn",
    "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj", "nm", "nv", "ny", "oh",
    "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "va", "vt", "wa",
    "wi", "wv", "wy",
}
OUTSIDE_NORTH_AMERICA_TERMS = (
    "australia", "austria", "belgium", "brazil", "china", "denmark", "finland",
    "france", "germany", "hong kong", "india", "ireland", "israel", "italy", "japan",
    "korea", "mexico", "netherlands", "new zealand", "norway", "poland", "portugal",
    "singapore", "spain", "sweden", "switzerland", "taiwan", "united kingdom",
    "england", "scotland", "wales", "europe", "emea", "apac", "asia", "africa",
    "bengaluru", "bangalore", "berlin", "dublin", "london", "melbourne", "munich",
    "paris", "sydney", "tokyo", "zurich",
    "baunatal", "bochum", "eschborn", "ingolstadt", "mönsheim", "monsheim",
    "münchen", "munchen", "wolfsburg", "north rhine-westphalia", "hesse",
)
OUTSIDE_COUNTRY_CODES = {
    "ae", "ar", "at", "au", "be", "br", "ch", "cn", "cz", "de", "dk",
    "es", "fi", "fr", "gb", "hk", "id", "ie", "il", "in", "it", "jp",
    "kr", "my", "nl", "no", "nz", "ph", "pl", "pt", "ro", "se", "sg",
    "th", "tw", "ua", "uk", "vn", "za",
}

TECHNICAL_RESEARCH_CONTEXT_PATTERN = (
    r"(?:machine learning|artificial intelligence|\bai\b|\bml\b|robotics?|software|hardware|"
    r"electrical|embedded|firmware|fpga|asic|computer vision|perception|autonom(?:y|ous)|"
    r"controls?|systems? engineering|computer science|compiler)"
)
TECHNICAL_RESEARCH_PATTERN = (
    rf"\bresearch\s+(?:engineer|scientist)\b|\bapplied scientist\b|"
    rf"(?:{TECHNICAL_RESEARCH_CONTEXT_PATTERN})[^,;:]{{0,50}}\bresearch(?:er)?\b|"
    rf"\bresearch(?:er)?\b[^,;:]{{0,50}}(?:{TECHNICAL_RESEARCH_CONTEXT_PATTERN})"
)

STUDENT_CATEGORY_PATTERNS = (
    ("software_engineering", r"\bsoftware\b|\bswe\b|\bsde\b|\bdeveloper\b", 24),
    ("machine_learning_ai", r"\bmachine learning\b|\bml\b|\bartificial intelligence\b|\bai\b", 24),
    ("computer_vision", r"\bcomputer vision\b", 23),
    ("robotics_autonomous_systems", r"\brobot(?:ics)?\b|\bautonom(?:y|ous)\b", 23),
    ("embedded_software", r"\bembedded(?: software| systems?)?\b", 23),
    ("firmware", r"\bfirmware\b", 24),
    ("embedded_firmware", r"\bembedded\b|\bfirmware\b", 0),
    ("electrical_engineering", r"\belectrical(?: engineering| engineer)?\b|\bece\b", 22),
    ("hardware_engineering", r"\bhardware(?: engineering| engineer)?\b|\bcomputer engineering\b", 22),
    ("fpga_asic", r"\bfpga\b|\basic\b", 24),
    ("semiconductor_design_verification", r"\bsemiconductor\b|\bsilicon\b|\bdesign verification\b|\bverification engineer", 24),
    (
        "controls",
        r"\bcontrols?\s+(?:systems?\s+)?engineer(?:ing)?\b|"
        r"\bcontrols?\s+systems?\b|"
        r"\b(?:motion|motor|flight|robotics?|automation)\s+controls?\b|"
        r"\b(?:intern(?:ship)?s?|co\s*(?:[-/]\s*)?ops?|students?)\b"
        r"\s*[-,:/]\s*controls?\b",
        21,
    ),
    ("perception", r"\bperception\b", 23),
    ("systems_engineering", r"\bsystems? engineering\b|\bsystems? engineer", 21),
    ("research_applied_science", TECHNICAL_RESEARCH_PATTERN, 14),
)

# Only these collectors search by role text. Complete-board collectors remain untouched.
ROLE_QUERY_PLATFORMS = {
    "amazon", "custom:amazon", "google", "custom:google", "meta", "custom:meta",
}
STRUCTURED_JOB_TYPE_KEYS = {
    "employmenttype", "employmenttypes", "jobtype", "jobtypes", "positiontype",
    "positiontypes", "workertype", "workertypes", "timetype", "schedule",
}
PROGRAM_METADATA_KEYS = {
    "program", "programname", "programtype", "talentprogram", "recruitingprogram",
    "candidateprogram", "earlycareerprogram",
}
RESTRICTED_PROGRAM_METADATA_KEYS = PROGRAM_METADATA_KEYS | {
    "category", "categories", "candidatecategory", "candidatecategories",
    "talentcategory", "talentcategories", "tags", "tags1", "tags2", "tags3",
}
SPONSORSHIP_METADATA_FRAGMENTS = (
    "sponsor", "visa", "workauthorization", "workauthorisation", "authorizedtowork",
    "authorisedtowork", "immigration",
)
TARGET_SEASONS = {
    "Fall 2026", "September 2026", "Winter 2027", "January 2027",
    "Summer 2027", "May 2027", "2027 internship",
}

STUDENT_ROLE_POINTS = {
    "explicit_title": 28,
    "contextual_title": 24,
    "structured_metadata": 22,
}
LOCATION_RANKING_POINTS = {
    "eligible_canada": 14,
    "eligible_remote_canada": 14,
    "eligible_north_america": 11,
    "eligible_remote_north_america": 11,
    "eligible_worldwide": 11,
    "eligible_remote_unspecified": 10,
    "eligible_remote_us": 9,
    "eligible_us_hybrid": 7,
    "eligible_us_onsite": 5,
}
DOMAIN_PRIORITY_CAP = 34
UNDERGRADUATE_CONFIDENCE_POINTS = 4
AMBIGUOUS_EDUCATION_PENALTY = 2
NO_TARGET_DOMAIN_PENALTY = 5
UNIVERSITY_PARTNER_PROGRAM_PENALTY = 3

UNIVERSITY_PARTNER_TITLE_PATTERNS = (
    r"\b(?:[Cc]o\s*(?:[-/]\s*)?[Oo]p|[Ii]nternship|[Pp]rogram)\s+[Ww]ith\s+(?:[Tt]he\s+)?"
    r"(?:[A-Z][A-Za-z&.'-]*\s+){1,6}(?:University|College|Institute)\b",
    r"\bUniversity\s+of\s+(?:[A-Z][A-Za-z&.'-]*\s*){1,6}"
    r"(?:[Cc]o\s*(?:[-/]\s*)?[Oo]p|[Ii]nternship|[Pp]rogram|[Pp]artnership)\b",
    r"\b(?:[A-Z][A-Za-z&.'-]*\s+){1,6}(?:University|College|Institute)\s+"
    r"(?:[Cc]o\s*(?:[-/]\s*)?[Oo]p|[Ii]nternship|[Pp]artnership)\b",
    r"\b[A-Z][A-Za-z&.'-]*\s+Tech\s+[Pp]artnership\s+[Ii]nternship\b",
)


def student_search_terms(include_new_grad: bool = False) -> list[str]:
    """Return the deterministic bounded role-query set."""
    del include_new_grad  # Retained for CLI compatibility; v1 always excludes new-grad roles.
    return list(STUDENT_ROLE_QUERIES)


def student_search_query(include_new_grad: bool = False) -> str:
    """Return a display/fallback OR query; query collectors use individual variants."""
    return " OR ".join(f'"{term}"' for term in student_search_terms(include_new_grad))


def configure_student_targets(
    targets: Iterable[CompanyTarget], *, include_new_grad: bool = False,
) -> list[CompanyTarget]:
    """Apply role queries only to collectors that do not enumerate the full board."""
    queries = student_search_terms(include_new_grad)
    return [
        replace(target, search_terms=list(queries))
        if target.platform in ROLE_QUERY_PLATFORMS else target
        for target in targets
    ]


def classify_student_location(
    job: JobRecord, *, locations: Optional[Iterable[str]] = None,
) -> str:
    """Classify the fixed v1 Canada/US/remote scope from explicit location data."""
    del locations  # Scope Expansion v1 is intentionally consistent across entry points.
    location = (job.location or "").strip()
    lowered = location.casefold()
    title = (job.title or "").casefold()
    if not location:
        return "manual_review_missing"

    remote = bool(re.search(r"\bremote\b", lowered))
    hybrid = bool(re.search(r"\bhybrid\b", lowered))
    worldwide = bool(re.search(r"\b(?:worldwide|global remote|remote globally|anywhere)\b", lowered))
    north_america = "north america" in lowered
    explicit_us = bool(
        re.search(r"(?<![A-Za-z])(?:united states|u\.?s\.?a?|us[- ]only)(?![A-Za-z])", lowered)
        or re.search(r"(?<![A-Za-z])(?:u\.?s\.?a?|united states)[- ]only(?![A-Za-z])", title)
    )
    us = _has_us_location(lowered) or explicit_us
    explicit_canada = bool(re.search(r"\b(?:canada|canadian)\b", f"{title} {lowered}")) or (
        any(term in lowered for term in CANADIAN_PROVINCE_TERMS)
        or bool(_location_codes(lowered) & CANADIAN_PROVINCE_CODES)
    )
    canada = explicit_canada or (_has_canadian_location(lowered) and not us)
    outside = _has_explicit_outside_location(lowered)

    if worldwide and remote:
        return "eligible_worldwide"
    if outside and not explicit_us and not explicit_canada and not any(
        term in lowered for term in US_LOCATION_TERMS
    ):
        return "excluded_other_location"
    if canada and us:
        return "eligible_remote_canada" if remote else "eligible_canada"
    if north_america:
        return "eligible_remote_north_america" if remote else "eligible_north_america"
    if canada:
        return "eligible_remote_canada" if remote else "eligible_canada"
    if us:
        if remote:
            return "eligible_remote_us"
        if hybrid:
            return "eligible_us_hybrid"
        return "eligible_us_onsite"
    if _has_explicit_outside_location(lowered):
        return "excluded_other_location"
    if remote:
        return "eligible_remote_unspecified"
    return "excluded_other_location"


def student_location_exclusion_reason(
    job: JobRecord, *, locations: Optional[Iterable[str]] = None,
    allow_international: bool = False,
) -> Optional[str]:
    status = classify_student_location(job, locations=locations)
    if allow_international and status == "excluded_other_location":
        return None
    return "location" if status.startswith("excluded_") else None


def build_student_location_filter(
    *, locations: Optional[Iterable[str]] = None, allow_international: bool = False,
) -> Callable[[JobRecord], Optional[str]]:
    return partial(
        student_location_exclusion_reason,
        locations=list(locations or []),
        allow_international=allow_international,
    )


def student_job_signals(job: JobRecord) -> dict[str, Any]:
    """Return explainable title and structured-metadata student signals."""
    title = job.title or ""
    strong_title = _matched_signals(title, STRONG_STUDENT_PATTERNS)
    contextual_title = _matched_signals(title, CONTEXTUAL_STUDENT_PATTERNS)
    structured_values = _metadata_values(job.raw_metadata, STRUCTURED_JOB_TYPE_KEYS)
    structured_strong = _unique(
        signal for value in structured_values
        for signal in _matched_signals(value, STRONG_STUDENT_PATTERNS)
    )
    structured_corroboration = [
        value for value in structured_values
        if re.search(
            r"\bintern(?:ship)?s?\b|\bco\s*(?:[-/]\s*)?op\b|\bstudents?\b|"
            r"\btemporary\b|\bseasonal\b|\bfixed\s*[- ]?term\b|\bplacement\b",
            value,
            flags=re.IGNORECASE,
        )
    ]
    program_values = _metadata_values(job.raw_metadata, PROGRAM_METADATA_KEYS)
    student_program_values = [
        value for value in program_values
        if _matched_signals(value, STRONG_STUDENT_PATTERNS + CONTEXTUAL_STUDENT_PATTERNS)
    ]

    if strong_title:
        matching_rule = f"strong_title:{strong_title[0]}"
    elif structured_strong:
        matching_rule = f"structured_job_type:{structured_strong[0]}"
    elif contextual_title and structured_corroboration:
        matching_rule = "contextual_title+structured_job_type"
    elif contextual_title and student_program_values:
        matching_rule = "contextual_title+program_metadata"
    else:
        matching_rule = None

    return {
        "strong_title": strong_title,
        "contextual_title": contextual_title,
        "structured_strong": structured_strong,
        "structured_job_types": structured_corroboration,
        "program_metadata": student_program_values,
        "matching_rule": matching_rule,
    }


def student_job_exclusion_reason(
    job: JobRecord, *, include_new_grad: bool = False,
    candidate_profile: Optional[CandidateProfile] = None,
) -> Optional[str]:
    """Return an exclusion category; sponsorship is deliberately never a gate."""
    del include_new_grad
    existing_reason = _student_job_exclusion_reason_before_education(job)
    if existing_reason:
        return existing_reason
    education = annotate_education_eligibility(job)
    if education["education_eligibility"] == GRADUATE_ONLY:
        return "graduate_only"
    if candidate_profile is not None:
        return candidate_profile_exclusion_reason(job, candidate_profile)
    return None


def build_student_job_filter(
    *, include_new_grad: bool = False,
    candidate_profile: Optional[CandidateProfile] = None,
) -> Callable[[JobRecord], Optional[str]]:
    return partial(
        student_job_exclusion_reason,
        include_new_grad=include_new_grad,
        candidate_profile=candidate_profile,
    )


def detect_student_seasons(job: JobRecord) -> list[str]:
    """Detect and normalize season/year annotations from title and metadata."""
    metadata_text = " ".join(_metadata_scalar_values(job.raw_metadata))
    text = f"{job.title} {metadata_text}"
    seasons: list[str] = []
    season_names = {"fall": "Fall", "autumn": "Fall", "winter": "Winter", "summer": "Summer", "spring": "Spring"}
    month_names = {
        "january": "January", "jan": "January", "may": "May",
        "september": "September", "sept": "September", "sep": "September",
    }
    for match in re.finditer(
        r"\b(fall|autumn|winter|summer|spring)\s*[-/'’]?\s*(20\d{2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        seasons.append(f"{season_names[match.group(1).casefold()]} {match.group(2)}")
    for match in re.finditer(
        r"\b(january|jan|may|september|sept|sep)\s*[-/'’]?\s*(20\d{2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        seasons.append(f"{month_names[match.group(1).casefold()]} {match.group(2)}")
    for match in re.finditer(r"\b(20\d{2})[-/](01|05|09)(?:[-/]\d{1,2})?\b", text):
        month = {"01": "January", "05": "May", "09": "September"}[match.group(2)]
        seasons.append(f"{month} {match.group(1)}")
    for match in re.finditer(r"\b(20\d{2})\s*[-/]?\s*intern(?:ship)?s?\b", text, flags=re.IGNORECASE):
        seasons.append(f"{match.group(1)} internship")
    return _unique(seasons)


def sponsorship_metadata(job: JobRecord) -> dict[str, Any]:
    """Extract explicit authorization/sponsorship fields without filtering on them."""
    found: dict[str, Any] = {}
    for path, value in _metadata_items(job.raw_metadata):
        normalized = _normalize_key(path.rsplit(".", 1)[-1])
        if any(fragment in normalized for fragment in SPONSORSHIP_METADATA_FRAGMENTS):
            found[path] = value
    return found


def score_student_job(
    job: JobRecord,
    *,
    include_new_grad: bool = False,
    locations: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
    candidate_profile: Optional[CandidateProfile] = None,
    include_legacy_recency: bool = True,
) -> dict[str, Any]:
    """Return deterministic student relevance with domain and season annotations."""
    del include_new_grad
    title = (job.title or "").casefold()
    education = annotate_education_eligibility(job)
    signal_info = student_job_signals(job)
    student_role_confidence, student_role_points = _student_role_confidence(signal_info)
    score = student_role_points
    positive_signals = _unique(
        signal_info["strong_title"]
        + signal_info["contextual_title"]
        + signal_info["structured_strong"]
    )
    if student_role_confidence:
        positive_signals.append(f"student_role:{student_role_confidence}")
    negative_signals: list[str] = []
    matched_categories: list[str] = []
    domain_priority = 0
    for category, pattern, points in STUDENT_CATEGORY_PATTERNS:
        if re.search(pattern, title, flags=re.IGNORECASE):
            matched_categories.append(category)
            domain_priority += points
    score += min(domain_priority, DOMAIN_PRIORITY_CAP)
    if not matched_categories:
        score -= NO_TARGET_DOMAIN_PENALTY
        negative_signals.append("no_target_domain_match")

    if _is_university_partner_program(job.title or ""):
        score -= UNIVERSITY_PARTNER_PROGRAM_PENALTY
        negative_signals.append("university_partner_program")

    location_status = classify_student_location(job, locations=locations)
    # Candidate-profile mode owns geography as a low-weight preference. The
    # legacy location points remain byte-for-byte compatible when it is off.
    location_points = (
        0 if candidate_profile is not None
        else LOCATION_RANKING_POINTS.get(location_status, 0)
    )
    if location_points:
        score += location_points
        positive_signals.append(location_status)
    elif candidate_profile is None and location_status.startswith("excluded_"):
        score -= 25
        negative_signals.append("outside_canada_us_scope")

    if education["education_eligibility"] == UNDERGRADUATE_ALLOWED:
        score += UNDERGRADUATE_CONFIDENCE_POINTS
        positive_signals.append("education:undergraduate_allowed")
    elif education["education_eligibility"] == AMBIGUOUS:
        score -= AMBIGUOUS_EDUCATION_PENALTY
        negative_signals.append("education:ambiguous")

    seniority_signals = _seniority_signals(title)
    if seniority_signals:
        score -= 50
        negative_signals.extend(seniority_signals)
    restricted_signals = _restricted_program_signals(job)
    if restricted_signals:
        score -= 60
        negative_signals.extend(restricted_signals)
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in NEW_GRAD_PATTERNS):
        score -= 55
        negative_signals.append("new_grad")

    seasons = detect_student_seasons(job)
    target_seasons = [season for season in seasons if season in TARGET_SEASONS]
    season_boost = (
        0 if candidate_profile is not None
        else (8 if target_seasons else (3 if seasons else 0))
    )
    score += season_boost
    if candidate_profile is None:
        positive_signals.extend(f"target_season:{season}" for season in target_seasons)

    if include_legacy_recency:
        recency_signal, recency_points = _recency_signal(job.updated_at, now=now)
        if recency_signal:
            score += recency_points
            positive_signals.append(recency_signal)

    score = max(0, min(100, score))
    result = {
        "score": score,
        "band": _student_relevance_band(score),
        "matched_categories": matched_categories,
        "domain_priority": domain_priority,
        "positive_signals": _unique(positive_signals),
        "negative_signals": _unique(negative_signals),
        "title_signals": _unique(signal_info["strong_title"] + signal_info["contextual_title"]),
        "matching_rule": signal_info["matching_rule"],
        "location_status": location_status,
        "seasons": seasons,
        "season_boost": season_boost,
        "work_authorization": sponsorship_metadata(job),
        "education_eligibility": education["education_eligibility"],
        "education_evidence": education["education_evidence"],
    }
    if candidate_profile is not None:
        preference = score_candidate_preferences(
            job,
            candidate_profile,
            location_status=location_status,
        )
        ranking_score = combine_ranking_scores(score, preference, candidate_profile)
        result.update(
            {
                "role_relevance_score": score,
                "role_relevance_band": _student_relevance_band(score),
                "candidate_preference_score": preference["score"],
                "ranking_score": ranking_score,
                "score": ranking_score,
                "band": _student_relevance_band(ranking_score),
                "term_match": preference["term_match"],
                "term_evidence": preference["term_evidence"],
                "duration_months": preference["duration_months"],
                "duration_match": preference["duration_match"],
                "geography_match": preference["geography_match"],
                "work_arrangement": preference["work_arrangement"],
                "matched_domain_clusters": preference["matched_domain_clusters"],
                "domain_match_score": preference["domain_match_score"],
                "multi_domain_bonus": preference["multi_domain_bonus"],
                "candidate_preference_reasons": preference["reasons"],
                "international_work_authorization_required": preference[
                    "international_work_authorization_required"
                ],
            }
        )
    return result


def rank_student_jobs(
    jobs: Iterable[JobRecord], *, include_new_grad: bool = False,
    locations: Optional[Iterable[str]] = None, limit: Optional[int] = None,
    candidate_profile: Optional[CandidateProfile] = None,
    now: Optional[datetime] = None,
    freshness_by_key: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    freshness_enabled = freshness_by_key is not None
    ranked: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, ...]] = set()
    for job in jobs:
        if candidate_profile is not None and candidate_profile_exclusion_reason(
            job, candidate_profile
        ):
            continue
        identity = _ranking_job_identity(job)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        relevance = score_student_job(
            job,
            include_new_grad=include_new_grad,
            locations=locations,
            candidate_profile=candidate_profile,
            now=now,
            # Freshness is an inspectable feature, not a relevance migration.
            # Keep the pre-existing relevance/candidate-preference semantics.
            include_legacy_recency=True,
        )
        freshness = None
        if freshness_enabled:
            source_key = stable_job_key(job)
            freshness = freshness_by_key.get(source_key)
            if freshness is None:
                raise ValueError(
                    f"Freshness lifecycle annotation missing for stable job identity {source_key!r}"
                )
        base_ranking_score = int(
            relevance["ranking_score"]
            if candidate_profile is not None
            else relevance["score"]
        )
        final_ranking_score = base_ranking_score
        item = job.to_dict()
        item.update({
            "score": final_ranking_score,
            "relevance_score": final_ranking_score,
            "relevance_band": _student_relevance_band(final_ranking_score),
            "matched_categories": relevance["matched_categories"],
            "domain_priority": relevance["domain_priority"],
            "positive_signals": relevance["positive_signals"],
            "negative_signals": relevance["negative_signals"],
            "title_signals": relevance["title_signals"],
            "matching_rule": relevance["matching_rule"],
            "location_status": relevance["location_status"],
            "seasons": relevance["seasons"],
            "season_boost": relevance["season_boost"],
            "work_authorization": relevance["work_authorization"],
            "education_eligibility": relevance["education_eligibility"],
            "education_evidence": relevance["education_evidence"],
        })
        if candidate_profile is not None:
            item.update({
                "role_relevance_score": relevance["role_relevance_score"],
                "role_relevance_band": relevance["role_relevance_band"],
                "candidate_preference_score": relevance["candidate_preference_score"],
                "ranking_score": final_ranking_score,
                "term_match": relevance["term_match"],
                "term_evidence": relevance["term_evidence"],
                "duration_months": relevance["duration_months"],
                "duration_match": relevance["duration_match"],
                "geography_match": relevance["geography_match"],
                "work_arrangement": relevance["work_arrangement"],
                "matched_domain_clusters": relevance["matched_domain_clusters"],
                "domain_match_score": relevance["domain_match_score"],
                "multi_domain_bonus": relevance["multi_domain_bonus"],
                "candidate_preference_reasons": relevance["candidate_preference_reasons"],
                "international_work_authorization_required": relevance[
                    "international_work_authorization_required"
                ],
            })
        if freshness is not None:
            item.update(dict(freshness))
        ranked.append(item)
    if candidate_profile is not None:
        ranked.sort(key=lambda item: (
            -item["ranking_score"], -item["role_relevance_score"],
            -item["candidate_preference_score"], -item["domain_match_score"],
            item["company"].casefold(), item["title"].casefold(), item["job_id"],
        ))
    else:
        ranked.sort(key=lambda item: (
            -item["relevance_score"], -item["domain_priority"], -item["season_boost"],
            item["company"].casefold(), item["title"].casefold(), item["job_id"],
        ))
    return ranked[:limit] if limit else ranked


def build_student_search_diagnostics(
    normalized_jobs: Iterable[JobRecord], *, locations: Optional[Iterable[str]] = None,
    example_limit: int = 10,
    candidate_profile: Optional[CandidateProfile] = None,
) -> dict[str, Any]:
    """Compare retained jobs before and after the education gate."""
    jobs = list(normalized_jobs)
    accepted: list[JobRecord] = []
    rejected: list[tuple[JobRecord, str]] = []
    newly_accepted: list[tuple[JobRecord, str]] = []
    exclusion_counts: Counter[str] = Counter()
    locations_seen: Counter[str] = Counter()
    title_signals: Counter[str] = Counter()
    seasons_seen: Counter[str] = Counter()
    before_count = 0
    graduate_only_removed: list[dict[str, str]] = []
    ambiguous_retained: list[dict[str, str]] = []
    false_positive_samples: list[dict[str, str]] = []

    for job in jobs:
        base_reason = _student_job_exclusion_reason_before_education(job)
        education = (
            annotate_education_eligibility(job)
            if base_reason is None else classify_education_eligibility(job)
        )
        reason = student_job_exclusion_reason(
            job, candidate_profile=candidate_profile
        )
        location_reason = student_location_exclusion_reason(
            job,
            locations=locations,
            allow_international=bool(
                candidate_profile and candidate_profile.allows_international
            ),
        )
        final_reason = reason or location_reason
        if base_reason is None and education["education_eligibility"] == GRADUATE_ONLY:
            graduate_only_removed.append(_education_diagnostic_example(job, education))
        if base_reason is None and location_reason is None:
            before_count += 1
            review = _education_diagnostic_example(job, education)
            if education["education_eligibility"] == AMBIGUOUS:
                ambiguous_retained.append(review)
                false_positive_samples.append(review)
            elif education["education_eligibility"] == UNDERGRADUATE_ALLOWED and re.search(
                r"\b(?:graduate|master|ph\.?\s?d|doctoral|mba|j\.?d\.?|m\.?d\.?)\b",
                education["education_evidence"], flags=re.IGNORECASE,
            ):
                false_positive_samples.append(review)
        if final_reason:
            exclusion_counts[final_reason] += 1
            rejected.append((job, final_reason))
        else:
            accepted.append(job)
            location_status = classify_student_location(job, locations=locations)
            locations_seen[location_status] += 1
            signals = student_job_signals(job)
            for signal in _unique(signals["strong_title"] + signals["contextual_title"] + signals["structured_strong"]):
                title_signals[signal] += 1
            detected_seasons = detect_student_seasons(job)
            for season in detected_seasons or ["unspecified"]:
                seasons_seen[season] += 1

        legacy_reason = _legacy_student_exclusion_reason(job) or _legacy_location_exclusion_reason(job)
        if legacy_reason is not None and final_reason is None:
            if legacy_reason == "location":
                rule = f"location:{classify_student_location(job, locations=locations)}"
            else:
                rule = f"student:{student_job_signals(job)['matching_rule']}"
            newly_accepted.append((job, rule))

    newly_by_rule = Counter(rule for _, rule in newly_accepted)
    return {
        "total_normalized_jobs": len(jobs),
        "before_retained_jobs": before_count,
        "accepted_jobs": len(accepted),
        "after_retained_jobs": len(accepted),
        "exclusions_by_reason": dict(sorted(exclusion_counts.items())),
        "newly_accepted_jobs_by_matching_rule": dict(sorted(newly_by_rule.items())),
        "location_distribution": dict(sorted(locations_seen.items())),
        "title_signal_distribution": dict(sorted(title_signals.items())),
        "season_distribution": dict(sorted(seasons_seen.items())),
        "newly_accepted_examples": [
            _diagnostic_example(job, rule) for job, rule in newly_accepted[:example_limit]
        ],
        "still_rejected_examples": [
            _diagnostic_example(job, reason) for job, reason in rejected[:example_limit]
        ],
        "graduate_only_roles_removed": graduate_only_removed,
        "ambiguous_roles_retained": ambiguous_retained,
        "false_positive_review_samples": false_positive_samples[:example_limit],
    }


def write_student_diagnostics(diagnostics: Mapping[str, Any], *, path: str) -> None:
    Path(path).write_text(json.dumps(dict(diagnostics), indent=2, ensure_ascii=False), encoding="utf-8")


def write_student_ranked_jobs_artifacts(
    jobs: Iterable[JobRecord], *, json_path: str, csv_path: str,
    keywords: Optional[str] = None, locations: Optional[Iterable[str]] = None,
    limit: Optional[int] = None, include_new_grad: bool = False,
    candidate_profile: Optional[CandidateProfile] = None,
    freshness_by_key: Optional[Mapping[str, Mapping[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> None:
    del keywords
    ranked = rank_student_jobs(
        jobs,
        include_new_grad=include_new_grad,
        locations=locations,
        limit=limit,
        candidate_profile=candidate_profile,
        freshness_by_key=freshness_by_key,
        now=now,
    )
    Path(json_path).write_text(json.dumps(ranked, indent=2, ensure_ascii=False), encoding="utf-8")
    fieldnames = [
        "company", "title", "location", "relevance_score", "relevance_band",
        "matched_categories", "title_signals", "matching_rule", "seasons",
        "positive_signals", "negative_signals", "location_status", "education_eligibility",
        "education_evidence", "url", "source", "updated_at",
    ]
    if candidate_profile is not None:
        fieldnames[3:3] = [
            "role_relevance_score", "candidate_preference_score", "ranking_score",
            "term_match", "duration_months", "duration_match", "geography_match",
            "work_arrangement", "matched_domain_clusters", "domain_match_score",
            "multi_domain_bonus", "candidate_preference_reasons",
            "international_work_authorization_required",
        ]
    if freshness_by_key is not None:
        freshness_fieldnames = [
            "freshness_band", "age_days", "freshness_age_source",
            "source_posted_at", "first_seen_at", "first_seen_age_hours",
            "last_seen_at", "first_seen_basis", "source_closes_at",
            "deadline_days_remaining", "urgency_band", "urgency_score",
            "urgency_reasons", "lifecycle_status", "missing_scan_count",
            "status", "is_active", "removed_at", "freshness_reasons", "date_parse_errors",
        ]
        fieldnames[3:3] = freshness_fieldnames
    with Path(csv_path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in ranked:
            writer.writerow({
                key: "; ".join(item[key]) if isinstance(item.get(key), list) else item.get(key, "")
                for key in fieldnames
            })


def _has_canadian_location(location: str) -> bool:
    return any(term in location for term in CANADIAN_LOCATION_TERMS) or bool(
        _location_codes(location) & CANADIAN_PROVINCE_CODES
    )


def _has_us_location(location: str) -> bool:
    return any(term in location for term in US_LOCATION_TERMS) or bool(
        _location_codes(location) & US_STATE_CODES
    )


def _has_explicit_outside_location(location: str) -> bool:
    return any(term in location for term in OUTSIDE_NORTH_AMERICA_TERMS) or bool(
        _location_codes(location) & OUTSIDE_COUNTRY_CODES
    )


def _location_codes(location: str) -> set[str]:
    return {
        match.group(1).casefold()
        for match in re.finditer(r"(?:^|[,/|])\s*([A-Za-z]{2})(?=\s*(?:$|[,/|]))", location)
    }


def _matched_signals(text: str, patterns: Iterable[tuple[str, str]]) -> list[str]:
    return [signal for signal, pattern in patterns if re.search(pattern, text, flags=re.IGNORECASE)]


def _student_role_confidence(signal_info: Mapping[str, Any]) -> tuple[str, int]:
    if signal_info["strong_title"]:
        confidence = "explicit_title"
    elif str(signal_info["matching_rule"] or "").startswith("contextual_title+"):
        confidence = "contextual_title"
    elif signal_info["matching_rule"]:
        confidence = "structured_metadata"
    else:
        return "", 0
    return confidence, STUDENT_ROLE_POINTS[confidence]


def _ranking_job_identity(job: JobRecord) -> tuple[str, ...]:
    company = _normalize_identity_text(job.company)
    source = _normalize_identity_text(job.source)
    if job.job_id:
        return "id", company, source, job.job_id.strip()
    if job.url:
        return "url", job.url.strip()
    return (
        "fallback",
        company,
        _normalize_identity_text(job.title),
        _normalize_identity_text(job.location),
    )


def _normalize_identity_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _restricted_program_signals(job: JobRecord) -> list[str]:
    text = " ".join(
        [job.title or ""]
        + _metadata_values(job.raw_metadata, RESTRICTED_PROGRAM_METADATA_KEYS)
    )
    signals = [
        signal for pattern, signal in RESTRICTED_PROGRAM_PATTERNS
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]
    description = job.description or ""
    signals.extend(
        signal for pattern, signal in RESTRICTED_DESCRIPTION_PATTERNS
        if re.search(pattern, description, flags=re.IGNORECASE)
    )
    return _unique(signals)


def _is_university_partner_program(title: str) -> bool:
    return any(re.search(pattern, title) for pattern in UNIVERSITY_PARTNER_TITLE_PATTERNS)


def _student_job_exclusion_reason_before_education(job: JobRecord) -> Optional[str]:
    """Return existing student-role exclusions without the education gate."""
    title = job.title or ""
    exempt_manager_intern = bool(re.search(
        r"\b(?:product|program|technical\s+program)\s+managers?\b.*\bintern(?:ship)?s?\b",
        title, flags=re.IGNORECASE,
    ))
    seniority_match = SENIORITY_TITLE_PATTERN.search(title)
    if seniority_match and not (
        seniority_match.group(0).strip().casefold() == "manager" and exempt_manager_intern
    ):
        return "seniority"
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in NEW_GRAD_PATTERNS):
        return "new_grad"
    if _restricted_program_signals(job):
        return "eligibility"
    if not student_job_signals(job)["matching_rule"]:
        return "job_type"
    return None


def _seniority_signals(title: str) -> list[str]:
    values = ["senior", "staff", "principal", "director", "head", "vice president", "executive"]
    found = [term for term in values if re.search(rf"\b{re.escape(term)}\b", title, flags=re.IGNORECASE)]
    if re.search(r"(?:^|[^A-Za-z])(?:vp|v\.p\.)(?:[^A-Za-z]|$)", title, flags=re.IGNORECASE):
        found.append("vp")
    return found


def _metadata_values(metadata: Mapping[str, Any], keys: set[str]) -> list[str]:
    values: list[str] = []
    for path, value in _metadata_items(metadata):
        if _normalize_key(path.rsplit(".", 1)[-1]) not in keys:
            continue
        if isinstance(value, (str, int, float, bool)):
            values.append(str(value))
        elif isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value if isinstance(item, (str, int, float, bool)))
    return values


def _metadata_items(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, item
            yield from _metadata_items(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _metadata_items(item, f"{prefix}[{index}]")


def _metadata_scalar_values(value: Any, parent_key: str = "") -> list[str]:
    if _normalize_key(parent_key) in {"description", "content", "qualifications", "responsibilities"}:
        return []
    if isinstance(value, Mapping):
        return [
            scalar for key, item in value.items()
            for scalar in _metadata_scalar_values(item, str(key))
        ]
    if isinstance(value, (list, tuple)):
        return [scalar for item in value for scalar in _metadata_scalar_values(item, parent_key)]
    return [str(value)] if isinstance(value, (str, int, float)) else []


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _legacy_student_exclusion_reason(job: JobRecord) -> Optional[str]:
    title = job.title or ""
    if re.search(r"\b(?:senior|staff|principal|manager|director|lead|architect)\b", title, flags=re.IGNORECASE):
        return "seniority"
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern, _ in RESTRICTED_PROGRAM_PATTERNS[:4]):
        return "eligibility"
    if not re.search(r"\bintern\b|\binternship\b|\bco[ -]?op\b|\bstudent\b|\buniversity\b", title, flags=re.IGNORECASE):
        return "job_type"
    return None


def _legacy_location_exclusion_reason(job: JobRecord) -> Optional[str]:
    location = (job.location or "").casefold()
    title_location = f"{job.title} {location}".casefold()
    remote = bool(re.search(r"\bremote\b", location))
    worldwide = bool(re.search(r"\b(?:worldwide|global|anywhere)\b", location))
    us = _has_us_location(location) or bool(re.search(r"\b(?:united states|u\.?s\.?a?)\b", location))
    canada = bool(re.search(r"\b(?:canada|canadian)\b", title_location)) or (_has_canadian_location(location) and not us)
    if canada or worldwide or "north america" in location or (remote and not us):
        return None
    if not location:
        return None
    return "location"


def _diagnostic_example(job: JobRecord, rule: str) -> dict[str, str]:
    return {"company": job.company, "title": job.title, "location": job.location, "rule": rule}


def _education_diagnostic_example(
    job: JobRecord, education: Mapping[str, str],
) -> dict[str, str]:
    return {
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "education_eligibility": education["education_eligibility"],
        "education_evidence": education["education_evidence"],
    }


def _recency_signal(updated_at: str, *, now: Optional[datetime] = None) -> tuple[Optional[str], int]:
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
    if score >= 57:
        return "high"
    if score >= 55:
        return "good"
    if score >= 54:
        return "possible"
    return "low"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
