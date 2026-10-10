"""Central bounded-retention policy for current Job Radar postings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Optional


RETENTION_REASONS = ("removed_closed", "stale", "over_60_days")


@dataclass(frozen=True)
class RetentionPolicy:
    """Policy gates for current output, persistence, and maintenance pruning."""

    max_job_age_days: int = 60
    drop_stale: bool = True
    removed_job_retention_days: int = 0
    sync_history_retention_days: int = 7

    def __post_init__(self) -> None:
        if self.max_job_age_days < 1:
            raise ValueError("max_job_age_days must be at least 1")
        if self.removed_job_retention_days < 0:
            raise ValueError("removed_job_retention_days cannot be negative")
        if self.sync_history_retention_days < 1:
            raise ValueError("sync_history_retention_days must be at least 1")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RetentionPolicy":
        expected = {
            "max_job_age_days",
            "drop_stale",
            "removed_job_retention_days",
            "sync_history_retention_days",
        }
        actual = set(values)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                f"retention keys mismatch; missing={missing}, unexpected={unexpected}"
            )
        drop_stale = values["drop_stale"]
        if not isinstance(drop_stale, bool):
            raise ValueError("retention.drop_stale must be a boolean")
        return cls(
            max_job_age_days=_integer(
                values["max_job_age_days"], "retention.max_job_age_days", minimum=1
            ),
            drop_stale=drop_stale,
            removed_job_retention_days=_integer(
                values["removed_job_retention_days"],
                "retention.removed_job_retention_days",
                minimum=0,
            ),
            sync_history_retention_days=_integer(
                values["sync_history_retention_days"],
                "retention.sync_history_retention_days",
                minimum=1,
            ),
        )

    def age_is_eligible(
        self, values: Mapping[str, Any], *, as_of: datetime
    ) -> bool:
        """Use an inclusive maximum: exactly max_job_age_days is eligible."""
        age_days = effective_age_days(values, as_of=as_of)
        return age_days is None or age_days <= self.max_job_age_days

    def observation_is_persistable(
        self, values: Mapping[str, Any], *, as_of: datetime
    ) -> bool:
        """Keep closure evidence, but do not restage stale/over-age observations."""
        if self.drop_stale and str(values.get("freshness_band") or "") == "stale":
            return False
        return self.age_is_eligible(values, as_of=as_of)

    def current_output_is_eligible(
        self, values: Mapping[str, Any], *, as_of: datetime
    ) -> bool:
        """Return whether a row may appear in normal ranked/dashboard output."""
        if str(values.get("lifecycle_status") or "active") == "removed":
            return False
        if str(values.get("status") or "active") == "closed":
            return False
        if values.get("is_active") is False:
            return False
        return self.observation_is_persistable(values, as_of=as_of)

    def prune_reason(
        self, values: Mapping[str, Any], *, as_of: datetime
    ) -> Optional[str]:
        """Classify one canonical row with stable, non-overlapping precedence."""
        lifecycle_status = str(values.get("lifecycle_status") or "")
        status = str(values.get("status") or "")
        if lifecycle_status == "removed" and status == "closed":
            removed_at = _timestamp(values.get("removed_at"))
            if removed_at is None:
                return "removed_closed"
            removed_age = (_as_utc(as_of) - removed_at).total_seconds() / 86400
            if removed_age >= self.removed_job_retention_days:
                return "removed_closed"
        if self.drop_stale and str(values.get("freshness_band") or "") == "stale":
            return "stale"
        if not self.age_is_eligible(values, as_of=as_of):
            return "over_60_days"
        return None


def effective_age_days(
    values: Mapping[str, Any], *, as_of: datetime
) -> Optional[float]:
    """Prefer trustworthy source age, falling back only when it is absent.

    A present-but-malformed or future source timestamp is conflicting evidence,
    not evidence that the source timestamp is absent.  Keeping the row in that
    case is intentionally safer than deleting it from a weaker fallback.
    """
    reference = _as_utc(as_of)
    source_value = values.get("source_posted_at")
    if _is_present(source_value):
        source_posted_at = _timestamp(source_value)
        if source_posted_at is None or source_posted_at > reference:
            return None
        return (reference - source_posted_at).total_seconds() / 86400

    if _has_source_date_error(values.get("date_parse_errors")):
        return None

    first_seen_value = values.get("first_seen_at")
    if not _is_present(first_seen_value):
        return None
    first_seen_at = _timestamp(first_seen_value)
    if first_seen_at is None or first_seen_at > reference:
        return None
    return (reference - first_seen_at).total_seconds() / 86400


def _timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif value not in (None, ""):
        text = str(value).strip()
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _is_present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _has_source_date_error(value: Any) -> bool:
    if not isinstance(value, (list, tuple, set)):
        return False
    for item in value:
        if isinstance(item, Mapping):
            field = str(item.get("field") or "")
            reason = str(item.get("reason") or "")
            if field == "source_posted_at" and reason in {"unparseable", "future_date"}:
                return True
        elif str(item).startswith("source_posted_at:"):
            return True
    return False


def _integer(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed != value or parsed < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return parsed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("as_of must include a timezone offset")
    return value.astimezone(timezone.utc)
