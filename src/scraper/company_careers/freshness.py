"""Deterministic lifecycle, freshness, and application-urgency intelligence."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional

import yaml

from src.integrations.supabase_job_store import generate_source_key


DEFAULT_FRESHNESS_CONFIG_PATH = "config/freshness.yaml"
FRESHNESS_STATE_SCHEMA_VERSION = 1

ACTIVE = "active"
POSSIBLY_MISSING = "possibly_missing"
REMOVED = "removed"
LIFECYCLE_STATUSES = frozenset({ACTIVE, POSSIBLY_MISSING, REMOVED})
FRESHNESS_BANDS = ("new", "fresh", "normal", "aging", "stale")
URGENCY_BANDS = ("apply_now", "high", "normal", "low", "closed")

POSTED_METADATA_KEYS = frozenset(
    {
        "dateposted", "postedat", "posteddate", "postedon", "publishedat",
        "publisheddate", "publicationdate", "sourcepostedat",
    }
)
CLOSE_METADATA_KEYS = frozenset(
    {
        "applicationclose", "applicationclosedate", "applicationdeadline",
        "applicationdeadlineat", "closingdate", "deadline", "deadlineat",
        "sourceclosesat",
    }
)


class FreshnessConfigurationError(ValueError):
    """Raised when timing configuration is missing or inconsistent."""


class FreshnessStateError(ValueError):
    """Raised when persisted lifecycle data cannot be reconciled safely."""


@dataclass(frozen=True)
class FreshnessThresholds:
    new_hours: int
    fresh_days: int
    normal_days: int
    aging_days: int


@dataclass(frozen=True)
class UrgencyThresholds:
    apply_now_deadline_days: int
    high_deadline_days: int
    newly_seen_high_hours: int
    low_after_days: int
    scores: Mapping[str, int]


@dataclass(frozen=True)
class RemovalPolicy:
    consecutive_complete_scans: int


@dataclass(frozen=True)
class FreshnessConfig:
    freshness: FreshnessThresholds
    urgency: UrgencyThresholds
    removal: RemovalPolicy


@dataclass(frozen=True)
class ReconciliationResult:
    state: dict[str, Any]
    annotations_by_key: dict[str, dict[str, Any]]
    all_annotations_by_key: dict[str, dict[str, Any]]
    transitions: tuple[dict[str, Any], ...]
    summary: dict[str, int]
    date_parse_failures: tuple[dict[str, str], ...]
    date_diagnostic_counts: dict[str, int]


def load_freshness_config(path: str = DEFAULT_FRESHNESS_CONFIG_PATH) -> FreshnessConfig:
    """Load and strictly validate the timing policy."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Freshness config not found: {path}")
    with config_path.open("r", encoding="utf-8") as file:
        root = yaml.safe_load(file) or {}
    _require_keys(root, {"freshness", "urgency", "removal"}, "root")

    freshness = _mapping(root["freshness"], "freshness")
    _require_keys(
        freshness, {"new_hours", "fresh_days", "normal_days", "aging_days"},
        "freshness",
    )
    new_hours = _positive_int(freshness["new_hours"], "freshness.new_hours")
    fresh_days = _positive_int(freshness["fresh_days"], "freshness.fresh_days")
    normal_days = _positive_int(freshness["normal_days"], "freshness.normal_days")
    aging_days = _positive_int(freshness["aging_days"], "freshness.aging_days")
    if not new_hours / 24 < fresh_days < normal_days < aging_days:
        raise FreshnessConfigurationError("freshness thresholds must be strictly increasing")

    urgency = _mapping(root["urgency"], "urgency")
    _require_keys(
        urgency,
        {"apply_now_deadline_days", "high_deadline_days", "newly_seen_high_hours",
         "low_after_days", "scores"},
        "urgency",
    )
    apply_now_days = _nonnegative_int(
        urgency["apply_now_deadline_days"], "urgency.apply_now_deadline_days"
    )
    high_days = _positive_int(urgency["high_deadline_days"], "urgency.high_deadline_days")
    if apply_now_days >= high_days:
        raise FreshnessConfigurationError(
            "urgency.apply_now_deadline_days must be below high_deadline_days"
        )
    scores = _mapping(urgency["scores"], "urgency.scores")
    _require_keys(scores, set(URGENCY_BANDS), "urgency.scores")
    normalized_scores = {
        band: _bounded_int(scores[band], f"urgency.scores.{band}", 0, 100)
        for band in URGENCY_BANDS
    }

    removal = _mapping(root["removal"], "removal")
    _require_keys(removal, {"consecutive_complete_scans"}, "removal")
    return FreshnessConfig(
        freshness=FreshnessThresholds(new_hours, fresh_days, normal_days, aging_days),
        urgency=UrgencyThresholds(
            apply_now_deadline_days=apply_now_days,
            high_deadline_days=high_days,
            newly_seen_high_hours=_positive_int(
                urgency["newly_seen_high_hours"], "urgency.newly_seen_high_hours"
            ),
            low_after_days=_positive_int(urgency["low_after_days"], "urgency.low_after_days"),
            scores=normalized_scores,
        ),
        removal=RemovalPolicy(
            consecutive_complete_scans=_positive_int(
                removal["consecutive_complete_scans"], "removal.consecutive_complete_scans"
            )
        ),
    )


def parse_reference_time(value: str) -> datetime:
    """Parse an explicit ISO-8601 reference time and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reference time must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("reference time must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def parse_source_date(value: Any, *, as_of: datetime) -> Optional[str]:
    """Normalize only allowlisted source date formats, retaining date-only precision."""
    if value is None or isinstance(value, bool):
        return None
    reference = _as_utc(as_of)
    if isinstance(value, datetime):
        return _timestamp_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return _timestamp_text(datetime.fromtimestamp(number, tz=timezone.utc))
        except (OSError, OverflowError, ValueError):
            return None

    text = " ".join(str(value).strip().split())
    if not text:
        return None
    text = re.sub(
        r"^(?:date\s+posted|posted(?:\s+on)?|published|application\s+deadline|"
        r"closing\s+date|deadline)\s*:?\s*", "", text, flags=re.IGNORECASE,
    )
    relative = re.fullmatch(
        r"(?:(\d+)\s+days?\s+ago|today|yesterday)", text, re.IGNORECASE
    )
    if relative:
        lowered = text.casefold()
        days = 0 if lowered == "today" else 1 if lowered == "yesterday" else int(relative.group(1))
        return (reference.date() - timedelta(days=days)).isoformat()
    bounded_days = _bounded_relative_days(text)
    if bounded_days is not None:
        # Workday's "30+" label is a lower bound, not an exact timestamp.
        # Using the boundary date conservatively prevents an old posting from
        # being treated as newly seen while never making it appear older than
        # the source proves.
        return (reference.date() - timedelta(days=bounded_days)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    try:
        parsed_datetime = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed_datetime = None
    if parsed_datetime is not None:
        if parsed_datetime.tzinfo is None:
            return None
        return _timestamp_text(parsed_datetime)
    for date_format in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _bounded_relative_days(value: Any) -> Optional[int]:
    text = " ".join(str(value or "").strip().split())
    text = re.sub(
        r"^(?:date\s+posted|posted(?:\s+on)?|published)\s*:?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    match = re.fullmatch(r"(\d{1,4})\+\s+days?\s+ago", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def stable_job_key(job: Mapping[str, Any] | Any) -> str:
    return generate_source_key(job)


def empty_freshness_state() -> dict[str, Any]:
    """Return the in-memory shape also used by the Supabase adapter."""
    return {"schema_version": FRESHNESS_STATE_SCHEMA_VERSION, "updated_at": None, "jobs": {}}


def reconcile_lifecycle(
    current_jobs: Iterable[Mapping[str, Any] | Any],
    previous_state: Mapping[str, Any],
    *,
    complete_companies: Iterable[str],
    protected_source_keys: Iterable[str] = (),
    as_of: datetime,
    config: FreshnessConfig,
    first_seen_basis: str = "observed_at_scan",
) -> ReconciliationResult:
    """Reconcile observations with persisted state using injected time and evidence."""
    _validate_state(previous_state)
    reference = _as_utc(as_of)
    seen_at = _timestamp_text(reference)
    prior_jobs = {str(key): dict(value) for key, value in previous_state["jobs"].items()}
    incoming: MutableMapping[str, dict[str, Any]] = {}
    for raw_job in current_jobs:
        values = _job_mapping(raw_job)
        source_key = stable_job_key(values)
        if source_key in incoming:
            if _identity_payload(incoming[source_key]) != _identity_payload(values):
                raise FreshnessStateError(f"Conflicting jobs share stable identity {source_key!r}")
            continue
        incoming[source_key] = values

    complete = {_normalize_company(value) for value in complete_companies if str(value).strip()}
    protected = {
        str(value).strip() for value in protected_source_keys if str(value).strip()
    }
    state_jobs: dict[str, dict[str, Any]] = {}
    transitions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    date_diagnostics: Counter[str] = Counter()

    for source_key in sorted(incoming):
        values = incoming[source_key]
        posted_at, posted_failures, posted_status = _extract_source_date(
            values, direct_fields=("source_posted_at", "date_posted"),
            metadata_keys=POSTED_METADATA_KEYS, as_of=reference, kind="source_posted_at",
        )
        closes_at, close_failures, close_status = _extract_source_date(
            values, direct_fields=("source_closes_at", "source_deadline_at"),
            metadata_keys=CLOSE_METADATA_KEYS, as_of=reference, kind="source_closes_at",
        )
        date_diagnostics[f"source_posted_at_{posted_status}"] += 1
        date_diagnostics[f"source_closes_at_{close_status}"] += 1
        if closes_at is None:
            closes_at = _description_deadline(values.get("description"), as_of=reference)
        failures.extend(
            {"source_key": source_key, **item} for item in posted_failures + close_failures
        )
        prior = prior_jobs.get(source_key)
        explicit_closed = _explicitly_closed(values) or _deadline_has_passed(closes_at, reference)
        error_codes = sorted(
            {item["field"] + ":" + item["reason"] for item in posted_failures + close_failures}
        )
        if prior is None:
            entry = {
                **_descriptive_fields(source_key, values),
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "first_seen_basis": first_seen_basis,
                "source_posted_at": posted_at,
                "source_closes_at": closes_at,
                "lifecycle_status": REMOVED if explicit_closed else ACTIVE,
                "missing_scan_count": 0,
                "removed_at": seen_at if explicit_closed else None,
                "reappeared_at": None,
                "reappearance_count": 0,
                "date_parse_errors": error_codes,
            }
            transition = "explicitly_closed" if explicit_closed else "new"
        else:
            entry = _canonical_entry(prior, source_key)
            old_status = entry["lifecycle_status"]
            entry.update(_descriptive_fields(source_key, values))
            entry["last_seen_at"] = seen_at
            if posted_at is not None and entry.get("source_posted_at") is None:
                entry["source_posted_at"] = posted_at
            if closes_at is not None:
                entry["source_closes_at"] = closes_at
            entry["missing_scan_count"] = 0
            entry["date_parse_errors"] = error_codes
            if explicit_closed:
                entry["lifecycle_status"] = REMOVED
                entry["removed_at"] = entry.get("removed_at") or seen_at
                transition = "explicitly_closed"
            else:
                entry["lifecycle_status"] = ACTIVE
                transition = "rediscovered"
                if old_status in {POSSIBLY_MISSING, REMOVED}:
                    transition = "reappeared"
                    entry["reappeared_at"] = seen_at
                    entry["reappearance_count"] = int(entry.get("reappearance_count", 0)) + 1
        state_jobs[source_key] = entry
        transitions.append(_transition(source_key, entry, transition))

    current_keys = set(incoming)
    for source_key in sorted(set(prior_jobs) - current_keys):
        entry = _canonical_entry(prior_jobs[source_key], source_key)
        transition: Optional[str] = None
        if source_key in protected:
            # Listing presence is not hydration success. Preserve the prior
            # record verbatim instead of refreshing descriptive fields or
            # advancing absence state.
            state_jobs[source_key] = entry
            continue
        if _normalize_company(entry["company"]) in complete and entry["lifecycle_status"] != REMOVED:
            entry["missing_scan_count"] += 1
            if entry["missing_scan_count"] >= config.removal.consecutive_complete_scans:
                entry["lifecycle_status"] = REMOVED
                entry["removed_at"] = entry.get("removed_at") or seen_at
                transition = "removed_after_complete_absence"
            else:
                entry["lifecycle_status"] = POSSIBLY_MISSING
                transition = "possibly_missing"
        state_jobs[source_key] = entry
        if transition:
            transitions.append(_transition(source_key, entry, transition))

    state = {
        "schema_version": FRESHNESS_STATE_SCHEMA_VERSION,
        "updated_at": seen_at,
        "jobs": {key: state_jobs[key] for key in sorted(state_jobs)},
    }
    _validate_state(state)
    all_annotations = annotations_for_state(state, as_of=reference, config=config)
    current_annotations = {key: all_annotations[key] for key in sorted(current_keys)}
    return ReconciliationResult(
        state=state,
        annotations_by_key=current_annotations,
        all_annotations_by_key=all_annotations,
        transitions=tuple(transitions),
        summary=_build_summary(current_annotations, all_annotations),
        date_parse_failures=tuple(
            sorted(failures, key=lambda item: (item["source_key"], item["field"], item["value"]))
        ),
        date_diagnostic_counts={
            key: date_diagnostics[key]
            for key in (
                "source_posted_at_missing",
                "source_posted_at_malformed",
                "source_posted_at_approximate",
                "source_closes_at_missing",
                "source_closes_at_malformed",
                "source_closes_at_approximate",
            )
        }
        | {"lifecycle_failures": 0},
    )


def annotations_for_state(
    state: Mapping[str, Any], *, as_of: datetime, config: FreshnessConfig
) -> dict[str, dict[str, Any]]:
    _validate_state(state)
    return {
        key: lifecycle_annotation(entry, as_of=as_of, config=config)
        for key, entry in sorted(state["jobs"].items())
    }


def lifecycle_annotation(
    entry: Mapping[str, Any], *, as_of: datetime, config: FreshnessConfig
) -> dict[str, Any]:
    reference = _as_utc(as_of)
    first_seen_age_hours = max(0.0, _hours_since(entry["first_seen_at"], reference) or 0.0)
    source_age = _hours_since(entry.get("source_posted_at"), reference)
    if source_age is not None and source_age >= 0:
        age_hours = source_age
        age_source = "source_posted_at"
    else:
        age_hours = first_seen_age_hours
        age_source = "first_seen_at"
    age_days = round(age_hours / 24, 3)
    freshness_band = classify_freshness(age_hours, config)
    lifecycle_status = str(entry["lifecycle_status"])
    closes_at = entry.get("source_closes_at")
    deadline_hours = _deadline_hours(closes_at, reference)
    deadline_days = None if deadline_hours is None else round(deadline_hours / 24, 3)
    urgency_band, urgency_reasons = classify_urgency(
        lifecycle_status=lifecycle_status,
        deadline_hours_remaining=deadline_hours,
        first_seen_age_hours=first_seen_age_hours,
        age_days=age_days,
        freshness_band=freshness_band,
        config=config,
    )
    return {
        "source_key": entry["source_key"],
        "first_seen_at": entry["first_seen_at"],
        "last_seen_at": entry["last_seen_at"],
        "first_seen_basis": entry["first_seen_basis"],
        "source_posted_at": entry.get("source_posted_at"),
        "source_closes_at": closes_at,
        "removed_at": entry.get("removed_at"),
        "is_active": lifecycle_status != REMOVED,
        "status": "closed" if lifecycle_status == REMOVED else "active",
        "lifecycle_status": lifecycle_status,
        "missing_scan_count": int(entry["missing_scan_count"]),
        "age_days": age_days,
        "first_seen_age_hours": round(first_seen_age_hours, 3),
        "freshness_band": freshness_band,
        "freshness_age_source": age_source,
        "freshness_reasons": [_age_reason(age_source, age_hours), f"freshness_band_{freshness_band}"],
        "deadline_days_remaining": deadline_days,
        "urgency_band": urgency_band,
        "urgency_score": int(config.urgency.scores[urgency_band]),
        "urgency_reasons": urgency_reasons,
        "date_parse_errors": list(entry.get("date_parse_errors") or []),
    }


def classify_freshness(age_hours: float, config: FreshnessConfig) -> str:
    """Classify elapsed hours with inclusive upper bounds."""
    if age_hours <= config.freshness.new_hours:
        return "new"
    if age_hours <= config.freshness.fresh_days * 24:
        return "fresh"
    if age_hours <= config.freshness.normal_days * 24:
        return "normal"
    if age_hours <= config.freshness.aging_days * 24:
        return "aging"
    return "stale"


def classify_urgency(
    *, lifecycle_status: str, deadline_hours_remaining: Optional[float],
    first_seen_age_hours: float, age_days: float, freshness_band: str,
    config: FreshnessConfig,
) -> tuple[str, list[str]]:
    """Classify urgency independently from candidate relevance and ranking."""
    if lifecycle_status == REMOVED:
        reason = (
            "deadline_passed"
            if deadline_hours_remaining is not None and deadline_hours_remaining < 0
            else "confirmed_removed"
        )
        return "closed", [reason]
    if deadline_hours_remaining is not None:
        deadline_days = deadline_hours_remaining / 24
        deadline_reason = f"deadline_in_{_reason_number(deadline_days)}_days"
        if deadline_hours_remaining < 0:
            return "closed", ["deadline_passed"]
        if deadline_hours_remaining <= config.urgency.apply_now_deadline_days * 24:
            return "apply_now", [deadline_reason]
        if deadline_hours_remaining <= config.urgency.high_deadline_days * 24:
            return "high", [deadline_reason]
    if first_seen_age_hours <= config.urgency.newly_seen_high_hours:
        return "high", [f"first_seen_{_reason_number(first_seen_age_hours)}_hours_ago"]
    if freshness_band in {"aging", "stale"} or age_days > config.urgency.low_after_days:
        return "low", [f"posting_age_{_reason_number(age_days)}_days"]
    return "normal", ["no_strong_urgency_signal"]


class FreshnessRuntime:
    """One-scan bridge from persisted Supabase state to ranked annotations."""

    def __init__(
        self, *, config: FreshnessConfig, previous_state: Optional[Mapping[str, Any]],
        as_of: datetime, first_seen_basis: str = "observed_at_scan",
    ) -> None:
        self.config = config
        self.as_of = _as_utc(as_of)
        self.previous_state = dict(previous_state or empty_freshness_state())
        self.first_seen_basis = first_seen_basis
        self.annotations: dict[str, dict[str, Any]] = {}
        self.result: Optional[ReconciliationResult] = None

    def process_report(self, report: Any) -> ReconciliationResult:
        complete_companies = [
            result.target.name for result in report.results
            if bool(getattr(result, "authoritative_for_absence", False))
        ]
        self.result = reconcile_lifecycle(
            report.normalized_jobs, self.previous_state,
            complete_companies=complete_companies, as_of=self.as_of,
            protected_source_keys={
                key
                for keys in report.protected_source_keys_by_company().values()
                for key in keys
            },
            config=self.config, first_seen_basis=self.first_seen_basis,
        )
        self.annotations.clear()
        self.annotations.update(self.result.annotations_by_key)
        self.previous_state = self.result.state
        return self.result

    def lifecycle_jobs(self, jobs: Iterable[Mapping[str, Any] | Any]) -> list[dict[str, Any]]:
        if self.result is None:
            raise RuntimeError("process_report must run before lifecycle jobs are requested")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for job in jobs:
            values = _job_mapping(job)
            key = stable_job_key(values)
            if key not in seen:
                rows.append({**values, **self.result.annotations_by_key[key]})
                seen.add(key)
        return rows


def _extract_source_date(
    values: Mapping[str, Any], *, direct_fields: tuple[str, ...],
    metadata_keys: frozenset[str], as_of: datetime, kind: str,
) -> tuple[Optional[str], list[dict[str, str]], str]:
    candidates: list[tuple[str, Any]] = []
    for field in direct_fields:
        if values.get(field) not in (None, ""):
            candidates.append((field, values[field]))
    candidates.extend(_metadata_date_candidates(values.get("raw_metadata"), metadata_keys))
    if not candidates:
        return None, [], "missing"
    failures: list[dict[str, str]] = []
    for _field, value in candidates:
        parsed = parse_source_date(value, as_of=as_of)
        if parsed is None:
            failures.append({"field": kind, "reason": "unparseable", "value": _safe_value(value)})
            continue
        if kind == "source_posted_at" and _date_is_future(parsed, as_of):
            failures.append({"field": kind, "reason": "future_date", "value": _safe_value(value)})
            continue
        status = "approximate" if _bounded_relative_days(value) is not None else "parsed"
        return parsed, failures, status
    return None, failures, "malformed"


def _metadata_date_candidates(
    metadata: Any, accepted_keys: frozenset[str]
) -> list[tuple[str, Any]]:
    return sorted(
        ((path, value) for path, value in _metadata_items(metadata)
         if _normalize_key(path.rsplit(".", 1)[-1]) in accepted_keys and value not in (None, "")),
        key=lambda item: (
            _normalize_key(item[0].rsplit(".", 1)[-1])
            not in {"sourcepostedat", "sourceclosesat"},
            item[0].casefold(),
        ),
    )


def _description_deadline(value: Any, *, as_of: datetime) -> Optional[str]:
    match = re.search(
        r"\b(?:application\s+deadline|closing\s+date|applications?\s+close)\b"
        r"\s*(?:is|on|:|-)?\s*"
        r"(\d{4}-\d{2}-\d{2}|[A-Z][a-z]+\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        str(value or ""), flags=re.IGNORECASE,
    )
    return parse_source_date(match.group(1), as_of=as_of) if match else None


def _explicitly_closed(values: Mapping[str, Any]) -> bool:
    for path, value in _metadata_items(values.get("raw_metadata")):
        key = _normalize_key(path.rsplit(".", 1)[-1])
        if key in {"isactive", "active"} and value is False:
            return True
        if key in {"status", "jobstatus", "postingstatus"} and str(value).strip().casefold() in {
            "closed", "expired", "inactive", "removed", "filled",
        }:
            return True
    return False


def _deadline_has_passed(value: Optional[str], as_of: datetime) -> bool:
    hours = _deadline_hours(value, as_of)
    return hours is not None and hours < 0


def _deadline_hours(value: Any, as_of: datetime) -> Optional[float]:
    parsed = parse_source_date(value, as_of=as_of)
    if parsed is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed):
        # Date-only source deadlines are calendar-day promises: today=0,
        # tomorrow=1, and the named day is not passed until the next UTC date.
        return float((date.fromisoformat(parsed) - _as_utc(as_of).date()).days * 24)
    else:
        deadline = parse_reference_time(parsed)
    return (deadline - _as_utc(as_of)).total_seconds() / 3600


def _hours_since(value: Any, as_of: datetime) -> Optional[float]:
    parsed = parse_source_date(value, as_of=as_of)
    if parsed is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed):
        observed = datetime.combine(date.fromisoformat(parsed), time.min, tzinfo=timezone.utc)
    else:
        observed = parse_reference_time(parsed)
    return (_as_utc(as_of) - observed).total_seconds() / 3600


def _date_is_future(value: str, as_of: datetime) -> bool:
    hours = _hours_since(value, as_of)
    return hours is not None and hours < 0


def _canonical_entry(raw: Mapping[str, Any], source_key: str) -> dict[str, Any]:
    return {
        **_descriptive_fields(source_key, raw),
        "first_seen_at": raw.get("first_seen_at"),
        "last_seen_at": raw.get("last_seen_at"),
        "first_seen_basis": str(raw.get("first_seen_basis") or "observed_at_scan"),
        "source_posted_at": raw.get("source_posted_at"),
        "source_closes_at": raw.get("source_closes_at") or raw.get("source_deadline_at"),
        "lifecycle_status": str(raw.get("lifecycle_status") or ACTIVE),
        "missing_scan_count": int(raw.get("missing_scan_count", raw.get("missing_successful_runs", 0)) or 0),
        "removed_at": raw.get("removed_at"),
        "reappeared_at": raw.get("reappeared_at"),
        "reappearance_count": int(raw.get("reappearance_count", 0) or 0),
        "date_parse_errors": list(raw.get("date_parse_errors") or []),
    }


def _descriptive_fields(source_key: str, values: Mapping[str, Any]) -> dict[str, str]:
    return {
        "source_key": source_key,
        "identity_strength": source_key.split(":", 1)[0],
        "company": str(values.get("company") or "").strip(),
        "source": str(values.get("source") or values.get("source_platform") or "").strip(),
        "job_id": str(values.get("job_id") or "").strip(),
        "url": str(values.get("source_url") or values.get("apply_url") or values.get("url") or "").strip(),
        "title": str(values.get("title") or values.get("position") or "").strip(),
        "location": str(values.get("location") or "").strip(),
    }


def _transition(source_key: str, entry: Mapping[str, Any], transition: str) -> dict[str, Any]:
    return {
        "source_key": source_key, "company": entry["company"], "title": entry["title"],
        "transition": transition, "lifecycle_status": entry["lifecycle_status"],
        "missing_scan_count": entry["missing_scan_count"],
    }


def _build_summary(
    current: Mapping[str, Mapping[str, Any]], all_annotations: Mapping[str, Mapping[str, Any]]
) -> dict[str, int]:
    freshness = Counter(item["freshness_band"] for item in current.values())
    urgency = Counter(item["urgency_band"] for item in current.values())
    lifecycle = Counter(item["lifecycle_status"] for item in all_annotations.values())
    return {
        "total_jobs": len(all_annotations), "current_jobs": len(current),
        **{band: freshness[band] for band in FRESHNESS_BANDS},
        **{f"urgency_{band}": urgency[band] for band in URGENCY_BANDS},
        "possibly_missing": lifecycle[POSSIBLY_MISSING], "removed": lifecycle[REMOVED],
    }


def _age_reason(source: str, age_hours: float) -> str:
    if source == "first_seen_at":
        return f"first_seen_{_reason_number(age_hours)}_hours_ago"
    return f"posted_{_reason_number(age_hours / 24)}_days_ago"


def _reason_number(value: float) -> str:
    rounded = round(max(0.0, value), 1)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"


def _validate_state(state: Any) -> None:
    if not isinstance(state, Mapping):
        raise FreshnessStateError("Freshness state root must be an object")
    if state.get("schema_version") != FRESHNESS_STATE_SCHEMA_VERSION:
        raise FreshnessStateError(
            f"Unsupported freshness state schema_version: {state.get('schema_version')!r}"
        )
    if not isinstance(state.get("jobs"), Mapping):
        raise FreshnessStateError("state.jobs must be an object")
    for source_key, raw in state["jobs"].items():
        if not isinstance(source_key, str) or not isinstance(raw, Mapping):
            raise FreshnessStateError("state.jobs must map source keys to objects")
        entry = _canonical_entry(raw, source_key)
        if not entry["first_seen_at"] or not entry["last_seen_at"]:
            raise FreshnessStateError(f"state.jobs[{source_key!r}] lacks lifecycle timestamps")
        parse_reference_time(str(entry["first_seen_at"]))
        parse_reference_time(str(entry["last_seen_at"]))
        if entry["lifecycle_status"] not in LIFECYCLE_STATUSES:
            raise FreshnessStateError(f"state.jobs[{source_key!r}] has invalid lifecycle_status")


def _job_mapping(job: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(job, Mapping):
        return dict(job)
    to_dict = getattr(job, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if is_dataclass(job):
        return asdict(job)
    raise TypeError("job must be a mapping or expose to_dict()")


def _identity_payload(values: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        str(values.get(field) or "").strip().casefold()
        for field in ("company", "title", "location", "url")
    )


def _metadata_items(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item).casefold()):
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _metadata_items(value[key], path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _metadata_items(child, f"{prefix}.{index}")
    else:
        yield prefix, value


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _normalize_company(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _safe_value(value: Any) -> str:
    return " ".join(str(value).split())[:120]


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FreshnessConfigurationError(f"{field} must be an object")
    return value


def _require_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise FreshnessConfigurationError(
            f"{field} keys must be exactly {sorted(expected)}; got {sorted(actual)}"
        )


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise FreshnessConfigurationError(
            f"{field} must be an integer from {minimum} to {maximum}"
        )
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    return _bounded_int(value, field, 0, 1_000_000)


def _positive_int(value: Any, field: str) -> int:
    return _bounded_int(value, field, 1, 1_000_000)
