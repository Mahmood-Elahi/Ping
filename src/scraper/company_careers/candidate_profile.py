"""Config-backed deterministic candidate-preference scoring for student jobs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from src.scraper.company_careers.models import JobRecord


DEFAULT_CANDIDATE_PROFILE_PATH = "config/candidate_profile.yaml"


@dataclass(frozen=True)
class TargetTerm:
    year: int
    season: str
    preferred_start_month: int
    preferred_score: float
    unknown_score: float
    mismatch_score: float
    explicit_mismatch_penalty: int


@dataclass(frozen=True)
class DurationPreference:
    scores: Mapping[int, float]
    unknown_score: float
    unlisted_score: float


@dataclass(frozen=True)
class GeographyPreference:
    preferred: tuple[str, ...]
    acceptable: tuple[str, ...]
    scores: Mapping[str, float]
    willing_to_relocate: bool
    international_work_authorization_required: bool


@dataclass(frozen=True)
class DomainCluster:
    name: str
    label: str
    weight: float
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class DefenseExclusion:
    company_names: tuple[str, ...]
    decisive_work_patterns: tuple[str, ...]
    context_work_patterns: tuple[str, ...]


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    target_term: TargetTerm
    duration: DurationPreference
    geography: GeographyPreference
    work_arrangement: Mapping[str, str]
    domains: tuple[DomainCluster, ...]
    defense: DefenseExclusion
    component_weights: Mapping[str, float]
    role_relevance_weight: float
    candidate_preference_weight: float
    multi_domain_points_per_weight: float
    multi_domain_bonus_cap: float

    @property
    def allows_international(self) -> bool:
        return self.geography.willing_to_relocate and "international" in self.geography.acceptable


def load_candidate_profile(path: str = DEFAULT_CANDIDATE_PROFILE_PATH) -> CandidateProfile:
    """Load and validate a candidate profile from YAML."""
    profile_path = Path(path)
    if not profile_path.exists():
        raise FileNotFoundError(f"Candidate profile not found: {path}")
    with profile_path.open("r", encoding="utf-8") as file:
        root = yaml.safe_load(file) or {}
    data = root.get("candidate_profile")
    if not isinstance(data, Mapping):
        raise ValueError("Candidate profile YAML must contain a candidate_profile mapping")

    term = _mapping(data, "target_term")
    duration = _mapping(data, "duration")
    geography = _mapping(data, "geography")
    scoring = _mapping(data, "scoring")
    defense = _mapping(data, "defense_exclusion")
    domain_data = data.get("domains")
    if not isinstance(domain_data, Mapping) or not domain_data:
        raise ValueError("candidate_profile.domains must be a non-empty mapping")

    clusters: list[DomainCluster] = []
    for name, raw in domain_data.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"Domain cluster {name!r} must be a mapping")
        patterns = _string_tuple(raw.get("patterns"), f"domains.{name}.patterns")
        for pattern in patterns:
            try:
                re.compile(pattern, flags=re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid regex in domains.{name}: {pattern!r}: {exc}") from exc
        clusters.append(
            DomainCluster(
                name=str(name),
                label=str(raw.get("label") or str(name).replace("_", " ").title()),
                weight=_unit_float(raw.get("weight"), f"domains.{name}.weight"),
                patterns=patterns,
            )
        )

    decisive_work_patterns = _string_tuple(
        defense.get("decisive_work_patterns"), "defense_exclusion.decisive_work_patterns"
    )
    context_work_patterns = _string_tuple(
        defense.get("context_work_patterns"), "defense_exclusion.context_work_patterns"
    )
    for pattern in decisive_work_patterns + context_work_patterns:
        try:
            re.compile(pattern, flags=re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid defense exclusion regex {pattern!r}: {exc}") from exc

    component_weights = {
        key: float(value)
        for key, value in _mapping(scoring, "component_weights").items()
    }
    required_components = {"domains", "term", "duration", "geography"}
    if set(component_weights) != required_components:
        raise ValueError(f"component_weights must contain exactly {sorted(required_components)}")
    if any(value < 0 for value in component_weights.values()) or abs(sum(component_weights.values()) - 1.0) > 1e-9:
        raise ValueError("candidate profile component_weights must be non-negative and sum to 1")

    blend = _mapping(scoring, "ranking_blend")
    role_weight = _unit_float(blend.get("role_relevance"), "ranking_blend.role_relevance")
    preference_weight = _unit_float(
        blend.get("candidate_preference"), "ranking_blend.candidate_preference"
    )
    if abs(role_weight + preference_weight - 1.0) > 1e-9:
        raise ValueError("ranking_blend weights must sum to 1")

    duration_scores_raw = _mapping(duration, "scores")
    duration_scores = {
        int(months): _unit_float(value, f"duration.scores.{months}")
        for months, value in duration_scores_raw.items()
    }
    if not duration_scores or any(months <= 0 for months in duration_scores):
        raise ValueError("duration.scores must contain positive month values")

    geography_scores = {
        str(key): _unit_float(value, f"geography.scores.{key}")
        for key, value in _mapping(geography, "scores").items()
    }
    for required in ("canada", "united_states", "international", "unknown"):
        if required not in geography_scores:
            raise ValueError(f"geography.scores is missing {required!r}")

    term_year = int(term["year"])
    term_season = str(term["season"]).strip().casefold()
    start_month = int(term["preferred_start_month"])
    mismatch_penalty = int(term.get("explicit_mismatch_penalty") or 0)
    if term_year < 2000 or not term_season:
        raise ValueError("target_term requires a valid year and non-empty season")
    _month_name(start_month)
    if mismatch_penalty < 0:
        raise ValueError("target_term.explicit_mismatch_penalty cannot be negative")
    multi_points = float(scoring.get("multi_domain_points_per_weight") or 0)
    multi_cap = float(scoring.get("multi_domain_bonus_cap") or 0)
    if multi_points < 0 or not 0 <= multi_cap <= 100:
        raise ValueError("multi-domain scoring values must be non-negative and capped at 100")

    return CandidateProfile(
        name=str(data.get("name") or profile_path.stem),
        target_term=TargetTerm(
            year=term_year,
            season=term_season,
            preferred_start_month=start_month,
            preferred_score=_unit_float(term.get("preferred_score"), "target_term.preferred_score"),
            unknown_score=_unit_float(term.get("unknown_score"), "target_term.unknown_score"),
            mismatch_score=_unit_float(term.get("mismatch_score"), "target_term.mismatch_score"),
            explicit_mismatch_penalty=mismatch_penalty,
        ),
        duration=DurationPreference(
            scores=duration_scores,
            unknown_score=_unit_float(duration.get("unknown_score"), "duration.unknown_score"),
            unlisted_score=_unit_float(
                duration.get("unlisted_score"), "duration.unlisted_score"
            ),
        ),
        geography=GeographyPreference(
            preferred=_string_tuple(geography.get("preferred"), "geography.preferred"),
            acceptable=_string_tuple(geography.get("acceptable"), "geography.acceptable"),
            scores=geography_scores,
            willing_to_relocate=bool(geography.get("willing_to_relocate", False)),
            international_work_authorization_required=bool(
                geography.get("international_work_authorization_required", False)
            ),
        ),
        work_arrangement={
            str(key): str(value) for key, value in _mapping(data, "work_arrangement").items()
        },
        domains=tuple(clusters),
        defense=DefenseExclusion(
            company_names=_string_tuple(defense.get("company_names"), "defense_exclusion.company_names"),
            decisive_work_patterns=decisive_work_patterns,
            context_work_patterns=context_work_patterns,
        ),
        component_weights=component_weights,
        role_relevance_weight=role_weight,
        candidate_preference_weight=preference_weight,
        multi_domain_points_per_weight=multi_points,
        multi_domain_bonus_cap=multi_cap,
    )


def score_candidate_preferences(
    job: JobRecord,
    profile: CandidateProfile,
    *,
    location_status: Optional[str] = None,
) -> dict[str, Any]:
    """Return bounded, explainable candidate-preference signals for one job."""
    matched_clusters, domain_score, multi_bonus = _score_domains(job, profile)
    term_match, term_score, term_evidence = _score_term(job, profile.target_term)
    duration_months = _detect_duration_months(job)
    duration_score = _duration_score(duration_months, profile.duration)
    geography_match, geography_score = _score_geography(job, profile, location_status)
    work_arrangement = _detect_work_arrangement(job)

    components = {
        "domains": domain_score / 100.0,
        "term": term_score,
        "duration": duration_score,
        "geography": geography_score,
    }
    score = round(
        100 * sum(profile.component_weights[key] * value for key, value in components.items())
    )
    reasons = _candidate_reasons(
        profile,
        matched_clusters=matched_clusters,
        term_match=term_match,
        duration_months=duration_months,
        duration_score=duration_score,
        geography_match=geography_match,
    )
    return {
        "score": max(0, min(100, score)),
        "term_match": term_match,
        "term_evidence": term_evidence,
        "duration_months": duration_months,
        "duration_match": _duration_label(duration_months, duration_score, profile.duration),
        "geography_match": geography_match,
        "work_arrangement": work_arrangement,
        "matched_domain_clusters": matched_clusters,
        "domain_match_score": round(domain_score, 2),
        "multi_domain_bonus": round(multi_bonus, 2),
        "reasons": reasons,
        "international_work_authorization_required": (
            profile.geography.international_work_authorization_required
            and geography_match in {"united_states", "international"}
        ),
    }


def combine_ranking_scores(
    role_relevance_score: int,
    preference: Mapping[str, Any],
    profile: CandidateProfile,
) -> int:
    """Blend role relevance and preferences, with a severe explicit term penalty."""
    score = (
        profile.role_relevance_weight * role_relevance_score
        + profile.candidate_preference_weight * int(preference["score"])
    )
    if preference["term_match"] == "mismatch":
        score -= profile.target_term.explicit_mismatch_penalty
    return max(0, min(100, round(score)))


def candidate_profile_exclusion_reason(
    job: JobRecord, profile: CandidateProfile
) -> Optional[str]:
    """Return the profile hard-exclusion category, if any."""
    company = _normalize_name(job.company)
    if any(company == _normalize_name(name) for name in profile.defense.company_names):
        return "defense"
    role_label = " ".join([job.title, " ".join(job.departments)])
    description = job.description or ""
    if any(
        re.search(pattern, f"{role_label} {description}", flags=re.IGNORECASE)
        for pattern in profile.defense.decisive_work_patterns
    ):
        return "defense"
    label_context = sum(
        bool(re.search(pattern, role_label, flags=re.IGNORECASE))
        for pattern in profile.defense.context_work_patterns
    )
    description_context = sum(
        bool(re.search(pattern, description, flags=re.IGNORECASE))
        for pattern in profile.defense.context_work_patterns
    )
    if label_context or description_context >= 2:
        return "defense"
    return None


def _score_domains(
    job: JobRecord, profile: CandidateProfile
) -> tuple[list[str], float, float]:
    label_text = " ".join([job.title, " ".join(job.departments)])
    detail_text = " ".join([job.description or "", " ".join(job.offices)])
    technical_title = bool(
        re.search(
            r"\b(?:engineer(?:ing)?|developer|scientist|research|firmware|embedded|software|"
            r"hardware|electrical|computer|robot(?:ics)?|autonom(?:y|ous)|perception|vision|"
            r"controls?|systems?|architect(?:ure)?|compiler|inference|silicon|asic|fpga|"
            r"verification|validation|data science|technical|compute|supercomputing|performance tools?)\b",
            label_text,
            flags=re.IGNORECASE,
        )
    )
    matched = [
        cluster for cluster in profile.domains
        if any(re.search(pattern, label_text, flags=re.IGNORECASE) for pattern in cluster.patterns)
        or (
            technical_title
            and any(re.search(pattern, detail_text, flags=re.IGNORECASE) for pattern in cluster.patterns)
        )
    ]
    if not matched:
        return [], 0.0, 0.0
    ordered = sorted(matched, key=lambda cluster: (-cluster.weight, cluster.name))
    base = ordered[0].weight * 80.0
    bonus = min(
        profile.multi_domain_bonus_cap,
        sum(cluster.weight for cluster in ordered[1:]) * profile.multi_domain_points_per_weight,
    )
    return [cluster.name for cluster in matched], min(100.0, base + bonus), bonus


def _score_term(job: JobRecord, term: TargetTerm) -> tuple[str, float, list[str]]:
    text = _job_text_with_metadata(job)
    year = str(term.year)
    season = re.escape(term.season)
    target_month = _month_name(term.preferred_start_month)
    evidence: list[str] = []
    target_patterns = (
        rf"\b{season}\s*[-/'’]?\s*{year}\b",
        rf"\b{year}\s*[-/'’]?\s*{season}\b",
        rf"\b{re.escape(target_month)}\s*[-/'’]?\s*{year}\b",
        rf"\b{year}\s*[-/'’]?\s*{re.escape(target_month)}\b",
        rf"\b{year}[-/]{term.preferred_start_month:02d}(?:[-/]\d{{1,2}})?\b",
    )
    for pattern in target_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            evidence.append(match.group(0))
    if evidence:
        return "preferred", term.preferred_score, _unique(evidence)

    explicit_terms = list(
        re.finditer(
            r"\b(fall|autumn|winter|summer|spring)\s*[-/'’]?\s*(20\d{2})\b|"
            r"\b(20\d{2})\s*[-/'’]?\s*(fall|autumn|winter|summer|spring)\b|"
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
            r"\s*[-/'’]?\s*(20\d{2})\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    if explicit_terms:
        values = _unique(match.group(0) for match in explicit_terms)
        return "mismatch", term.mismatch_score, values
    return "unknown", term.unknown_score, []


def _detect_duration_months(job: JobRecord) -> Optional[int]:
    structured = [
        str(value)
        for path, value in _metadata_items(job.raw_metadata)
        if "duration" in _normalize_key(path.rsplit(".", 1)[-1])
        and isinstance(value, (str, int, float))
    ]
    texts = structured + [job.title, job.description or ""]
    for text in texts:
        match = re.search(r"\b(\d{1,2})\s*[- ]?months?\b", text, flags=re.IGNORECASE)
        if match and 1 <= int(match.group(1)) <= 36:
            return int(match.group(1))
    for text in texts:
        match = re.search(
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
            r"(?:\s+20\d{2})?\s*(?:-|–|—|to|through|until)\s*"
            r"(january|february|march|april|may|june|july|august|september|october|november|december)"
            r"(?:\s+20\d{2})?\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            start = _month_number(match.group(1))
            end = _month_number(match.group(2))
            return ((end - start) % 12) + 1
    return None


def _duration_score(months: Optional[int], preference: DurationPreference) -> float:
    if months is None:
        return preference.unknown_score
    if months in preference.scores:
        return preference.scores[months]
    return preference.unlisted_score


def _duration_label(
    months: Optional[int], score: float, preference: DurationPreference
) -> str:
    if months is None:
        return "unknown"
    if months in preference.scores and score >= 0.99:
        return "preferred"
    if months in preference.scores and score >= 0.8:
        return "acceptable"
    if months not in preference.scores:
        return "other"
    return "conditional"


def _score_geography(
    job: JobRecord, profile: CandidateProfile, location_status: Optional[str]
) -> tuple[str, float]:
    status = location_status or ""
    if status in {"eligible_canada", "eligible_remote_canada"}:
        bucket = "canada"
    elif status in {"eligible_us_onsite", "eligible_us_hybrid", "eligible_remote_us"}:
        bucket = "united_states"
    elif status == "manual_review_missing" or not (job.location or "").strip():
        bucket = "unknown"
    elif status in {
        "eligible_north_america", "eligible_remote_north_america",
        "eligible_remote_unspecified", "eligible_worldwide",
    }:
        bucket = "unknown"
    else:
        bucket = "international"
    return bucket, profile.geography.scores[bucket]


def _detect_work_arrangement(job: JobRecord) -> str:
    text = f"{job.location} {_job_metadata_text(job.raw_metadata)}"
    if re.search(r"\bhybrid\b", text, flags=re.IGNORECASE):
        return "hybrid"
    if re.search(r"\bremote\b|\bvirtual\b|\bwork from home\b", text, flags=re.IGNORECASE):
        return "remote"
    if (job.location or "").strip():
        return "onsite_or_unspecified"
    return "unknown"


def _candidate_reasons(
    profile: CandidateProfile,
    *,
    matched_clusters: list[str],
    term_match: str,
    duration_months: Optional[int],
    duration_score: float,
    geography_match: str,
) -> list[str]:
    reasons: list[str] = []
    if term_match == "preferred":
        reasons.append(f"+ {profile.target_term.season.title()} {profile.target_term.year} start")
    elif term_match == "mismatch":
        reasons.append(f"- Outside {profile.target_term.season.title()} {profile.target_term.year} start term")
    else:
        reasons.append("? Start term not stated")
    by_name = {cluster.name: cluster for cluster in profile.domains}
    reasons.extend(f"+ {by_name[name].label} match" for name in matched_clusters)
    if not matched_clusters:
        reasons.append("- No preferred technical-domain evidence")
    if duration_months is None:
        reasons.append("? Duration not stated")
    elif duration_months in profile.duration.scores and duration_score >= 0.99:
        reasons.append(f"+ Preferred {duration_months}-month duration")
    elif duration_months not in profile.duration.scores:
        reasons.append(f"~ {duration_months}-month duration (not explicitly configured)")
    elif duration_score < 0.8:
        reasons.append(f"- Conditional {duration_months}-month duration")
    if geography_match == "canada":
        reasons.append("+ Canada")
    elif geography_match in {"united_states", "international"}:
        reasons.append(f"~ {geography_match.replace('_', ' ').title()} (relocation acceptable)")
    else:
        reasons.append("? Geography not specific")
    return reasons


def _job_text_with_metadata(job: JobRecord) -> str:
    return " ".join([job.title, job.description or "", _job_metadata_text(job.raw_metadata)])


def _job_metadata_text(metadata: Mapping[str, Any]) -> str:
    return " ".join(str(value) for _, value in _metadata_items(metadata) if isinstance(value, (str, int, float)))


def _metadata_items(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _metadata_items(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _metadata_items(child, f"{prefix}.{index}")
    else:
        yield prefix, value


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"candidate_profile.{key} must be a mapping")
    return value


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"candidate_profile.{field} must be a non-empty string list")
    return tuple(item.strip() for item in value)


def _unit_float(value: Any, field: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise ValueError(f"candidate_profile.{field} must be between 0 and 1")
    return parsed


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _month_number(name: str) -> int:
    months = (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    )
    return months.index(name.casefold()) + 1


def _month_name(number: int) -> str:
    if not 1 <= number <= 12:
        raise ValueError("preferred_start_month must be between 1 and 12")
    return (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )[number - 1]


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
