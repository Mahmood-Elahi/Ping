"""Fail-closed contracts for Phase 6 incremental source optimization.

Incremental crawling is an optimization over a periodically repeated full
inventory.  Nothing in this module grants lifecycle authority to an
incremental observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Optional, Sequence

from src.utils.source_scope import source_scope_key


class IncrementalCapability(str, Enum):
    NONE = "none"
    TIMESTAMP_WATERMARK = "timestamp_watermark"
    CURSOR = "cursor"
    INVENTORY_PLUS_INCREMENTAL_HYDRATION = "inventory_plus_incremental_hydration"
    EXPLICIT_CHANGE_FEED = "explicit_change_feed"
    VALIDATED_SNAPSHOT_REUSE = "validated_snapshot_reuse"


class CrawlMode(str, Enum):
    FULL_AUTHORITATIVE = "full_authoritative"
    INCREMENTAL_POSITIVE_ONLY = "incremental_positive_only"
    CACHED_AUTHORITATIVE_SNAPSHOT = "cached_authoritative_snapshot"


class ResultAuthority(str, Enum):
    AUTHORITATIVE_INVENTORY = "authoritative_inventory"
    POSITIVE_ONLY = "positive_only"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class IncrementalPolicy:
    capability: IncrementalCapability = IncrementalCapability.NONE
    strategy_version: str = "1"
    forced_full_interval: timedelta = timedelta(days=7)
    max_checkpoint_bytes: int = 16_384

    def __post_init__(self) -> None:
        if not str(self.strategy_version).strip():
            raise ValueError("strategy_version cannot be empty")
        if self.forced_full_interval <= timedelta(0):
            raise ValueError("forced_full_interval must be positive")
        if not 256 <= self.max_checkpoint_bytes <= 65_536:
            raise ValueError("max_checkpoint_bytes must be between 256 and 65536")


@dataclass(frozen=True)
class IncrementalCheckpoint:
    source_scope_key: str
    provider_identity: str
    config_fingerprint: str
    strategy_version: str
    cursor: Optional[str] = None
    timestamp_watermark: Optional[datetime] = None
    last_successful_full_at: Optional[datetime] = None
    last_successful_incremental_at: Optional[datetime] = None
    last_forced_full_reason: Optional[str] = None
    representation_token: Optional[str] = None
    snapshot_checksum: Optional[str] = None
    updated_at: Optional[datetime] = None
    ambiguous_since_last_full: bool = False

    def __post_init__(self) -> None:
        for name in (
            "source_scope_key",
            "provider_identity",
            "config_fingerprint",
            "strategy_version",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} cannot be empty")
        for name in (
            "timestamp_watermark",
            "last_successful_full_at",
            "last_successful_incremental_at",
            "updated_at",
        ):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must include timezone information")
        if self.cursor is not None and len(self.cursor) > 2048:
            raise ValueError("cursor exceeds the bounded checkpoint limit")
        if self.representation_token is not None and len(self.representation_token) > 2048:
            raise ValueError("representation_token exceeds the bounded checkpoint limit")
        if self.snapshot_checksum is not None and not _valid_sha256(self.snapshot_checksum):
            raise ValueError("snapshot_checksum must be a lowercase SHA-256 digest")

    def to_persistence_dict(self) -> dict[str, Any]:
        return {
            "source_scope_key": self.source_scope_key,
            "provider_identity": self.provider_identity,
            "config_fingerprint": self.config_fingerprint,
            "strategy_version": self.strategy_version,
            "cursor": self.cursor,
            "timestamp_watermark": _timestamp(self.timestamp_watermark),
            "last_successful_full_at": _timestamp(self.last_successful_full_at),
            "last_successful_incremental_at": _timestamp(
                self.last_successful_incremental_at
            ),
            "last_forced_full_reason": self.last_forced_full_reason,
            "representation_token": self.representation_token,
            "snapshot_checksum": self.snapshot_checksum,
            "updated_at": _timestamp(self.updated_at),
            "ambiguous_since_last_full": self.ambiguous_since_last_full,
        }

    def serialized_size(self) -> int:
        return len(
            json.dumps(
                self.to_persistence_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "IncrementalCheckpoint":
        return cls(
            source_scope_key=str(values.get("source_scope_key") or ""),
            provider_identity=str(values.get("provider_identity") or ""),
            config_fingerprint=str(values.get("config_fingerprint") or ""),
            strategy_version=str(values.get("strategy_version") or ""),
            cursor=_optional_text(values.get("cursor")),
            timestamp_watermark=_parse_timestamp(values.get("timestamp_watermark")),
            last_successful_full_at=_parse_timestamp(values.get("last_successful_full_at")),
            last_successful_incremental_at=_parse_timestamp(
                values.get("last_successful_incremental_at")
            ),
            last_forced_full_reason=_optional_text(values.get("last_forced_full_reason")),
            representation_token=_optional_text(values.get("representation_token")),
            snapshot_checksum=_optional_text(values.get("snapshot_checksum")),
            updated_at=_parse_timestamp(values.get("updated_at")),
            ambiguous_since_last_full=_strict_bool(
                values.get("ambiguous_since_last_full", False)
            ),
        )


@dataclass(frozen=True)
class IncrementalRequest:
    mode: CrawlMode
    source_scope_key: str
    config_fingerprint: str
    strategy_version: str
    checkpoint: Optional[IncrementalCheckpoint] = None
    selected_reason: str = "policy"


@dataclass(frozen=True)
class IncrementalResult:
    mode: CrawlMode
    authority: ResultAuthority
    checkpoint_candidate: Optional[IncrementalCheckpoint] = None
    requests_made: int = 0
    requests_saved: int = 0
    detail_hydrations_skipped: int = 0
    items_observed: int = 0
    full_inventory_size: Optional[int] = None
    fallback_reason: Optional[str] = None
    checkpoint_advanced: bool = False

    def __post_init__(self) -> None:
        for name in (
            "requests_made",
            "requests_saved",
            "detail_hydrations_skipped",
            "items_observed",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.full_inventory_size is not None and self.full_inventory_size < 0:
            raise ValueError("full_inventory_size cannot be negative")
        if (
            self.mode is CrawlMode.INCREMENTAL_POSITIVE_ONLY
            and self.authority is ResultAuthority.AUTHORITATIVE_INVENTORY
        ):
            raise ValueError("incremental_positive_only cannot authorize absence")
        if self.authority is ResultAuthority.INCOMPLETE and self.checkpoint_candidate:
            raise ValueError("incomplete results cannot advance a checkpoint")

    @property
    def absence_authorized(self) -> bool:
        return self.authority is ResultAuthority.AUTHORITATIVE_INVENTORY

    def metrics(self, *, now: datetime) -> dict[str, Any]:
        checkpoint = self.checkpoint_candidate
        return {
            "selected_crawl_mode": self.mode.value,
            "checkpoint_age_seconds": _age_seconds(
                checkpoint.updated_at if checkpoint else None, now
            ),
            "last_full_crawl_age_seconds": _age_seconds(
                checkpoint.last_successful_full_at if checkpoint else None, now
            ),
            "incremental_fallback_reason": self.fallback_reason,
            "requests_saved": self.requests_saved,
            "detail_hydrations_skipped": self.detail_hydrations_skipped,
            "incremental_items_observed": (
                self.items_observed
                if self.mode is CrawlMode.INCREMENTAL_POSITIVE_ONLY
                else 0
            ),
            "full_inventory_size": self.full_inventory_size,
            "checkpoint_advanced": self.checkpoint_advanced,
            "absence_authorized": self.absence_authorized,
        }


@dataclass(frozen=True)
class CrawlModeDecision:
    mode: CrawlMode
    reason: str
    checkpoint_valid: bool


@dataclass(frozen=True)
class ValidatedSnapshot:
    source_scope_key: str
    config_fingerprint: str
    provider_identity: str
    representation_token: str
    payload: bytes = field(repr=False)
    checksum: str = ""
    authoritative: bool = True

    def __post_init__(self) -> None:
        actual = hashlib.sha256(bytes(self.payload)).hexdigest()
        if not self.checksum:
            object.__setattr__(self, "checksum", actual)
        elif self.checksum != actual:
            raise ValueError("cached snapshot checksum mismatch")
        if not self.authoritative:
            raise ValueError("only a complete authoritative snapshot can be cached")


def choose_crawl_mode(
    *,
    source_scope_key_value: str,
    provider_identity: str,
    config_fingerprint: str,
    policy: IncrementalPolicy,
    checkpoint: Optional[IncrementalCheckpoint | Mapping[str, Any]],
    now: datetime,
    provider_state_regressed: bool = False,
    reconciliation_required: bool = False,
) -> CrawlModeDecision:
    """Choose conservatively; every uncertain state forces a full crawl."""
    reference = _aware(now, "now")
    full_reasons = (
        (policy.capability is IncrementalCapability.NONE, "capability_none"),
        (checkpoint is None, "checkpoint_missing"),
    )
    for condition, reason in full_reasons:
        if condition:
            return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, reason, False)
    try:
        if not isinstance(checkpoint, IncrementalCheckpoint):
            checkpoint = IncrementalCheckpoint.from_mapping(checkpoint)
        if checkpoint.serialized_size() > policy.max_checkpoint_bytes:
            return CrawlModeDecision(
                CrawlMode.FULL_AUTHORITATIVE, "checkpoint_oversized", False
            )
    except (TypeError, ValueError):
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "checkpoint_corrupt", False)
    expected = (
        source_scope_key_value,
        provider_identity,
        config_fingerprint,
        policy.strategy_version,
    )
    actual = (
        checkpoint.source_scope_key,
        checkpoint.provider_identity,
        checkpoint.config_fingerprint,
        checkpoint.strategy_version,
    )
    if actual[0] != expected[0] or actual[1] != expected[1]:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "checkpoint_identity_mismatch", False)
    if actual[2] != expected[2]:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "config_fingerprint_changed", False)
    if actual[3] != expected[3]:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "strategy_version_changed", False)
    if checkpoint.ambiguous_since_last_full:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "previous_incremental_ambiguous", False)
    if provider_state_regressed:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "provider_state_regressed", True)
    if reconciliation_required:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "reconciliation_required", True)
    if checkpoint.last_successful_full_at is None:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "full_crawl_never_completed", False)
    if reference - checkpoint.last_successful_full_at.astimezone(timezone.utc) >= policy.forced_full_interval:
        return CrawlModeDecision(CrawlMode.FULL_AUTHORITATIVE, "forced_full_interval_elapsed", True)
    return CrawlModeDecision(CrawlMode.INCREMENTAL_POSITIVE_ONLY, "healthy_checkpoint", True)


def validate_checkpoint_progress(
    previous: IncrementalCheckpoint,
    candidate: IncrementalCheckpoint,
    *,
    capability: IncrementalCapability,
    seen_cursors: Sequence[str] = (),
) -> Optional[str]:
    """Return a fail-closed reason when a candidate regresses or loops."""
    if (
        previous.source_scope_key,
        previous.provider_identity,
        previous.config_fingerprint,
        previous.strategy_version,
    ) != (
        candidate.source_scope_key,
        candidate.provider_identity,
        candidate.config_fingerprint,
        candidate.strategy_version,
    ):
        return "checkpoint_identity_changed"
    if candidate.serialized_size() > 65_536:
        return "checkpoint_oversized"
    if capability is IncrementalCapability.CURSOR:
        if not candidate.cursor:
            return "cursor_missing"
        if candidate.cursor == previous.cursor or candidate.cursor in set(seen_cursors):
            return "cursor_repeated"
        if _numeric(candidate.cursor) is not None and _numeric(previous.cursor) is not None:
            if _numeric(candidate.cursor) < _numeric(previous.cursor):
                return "cursor_rollback"
    if capability is IncrementalCapability.TIMESTAMP_WATERMARK:
        if candidate.timestamp_watermark is None:
            return "timestamp_missing"
        if previous.timestamp_watermark and candidate.timestamp_watermark < previous.timestamp_watermark:
            return "timestamp_rollback"
    return None


def configuration_fingerprint(values: Mapping[str, Any]) -> str:
    canonical = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def material_content_fingerprint(job: Mapping[str, Any] | Any) -> str:
    """Hash stable/material content; deliberately exclude observation timestamps."""
    values = _mapping(job)
    raw = values.get("raw_metadata")
    stable_raw = {}
    if isinstance(raw, Mapping):
        stable_raw = {
            str(key): value
            for key, value in raw.items()
            if str(key) not in {"last_seen_at", "first_seen_at", "observed_at", "scan_id"}
        }
    payload = {
        "job_id": values.get("job_id"),
        "source": values.get("source"),
        "title": values.get("title"),
        "company": values.get("company"),
        "location": values.get("location"),
        "url": values.get("url") or values.get("source_url") or values.get("apply_url"),
        "description": values.get("description"),
        "departments": values.get("departments"),
        "offices": values.get("offices"),
        "source_scope": values.get("source_scope"),
        "cross_source_equivalence": values.get("cross_source_equivalence"),
        "source_updated_at": values.get("source_updated_at") or values.get("updated_at"),
        "source_posted_at": values.get("source_posted_at"),
        "raw_metadata": stable_raw,
    }
    return configuration_fingerprint(payload)


def can_skip_hydration(
    *, stored_version: Optional[str], inventory_version: Optional[str]
) -> bool:
    """A seen ID alone is never enough; an exact trusted version is required."""
    return bool(
        stored_version
        and inventory_version
        and str(stored_version) == str(inventory_version)
    )


def reuse_validated_snapshot(
    *,
    response_status: int,
    source_scope_key_value: str,
    config_fingerprint: str,
    provider_identity: str,
    response_validator: Optional[str],
    cached: Optional[ValidatedSnapshot],
    contract_reviewed: bool,
) -> Optional[bytes]:
    if response_status != 304:
        return None
    if not contract_reviewed or cached is None:
        raise ValueError("invalid_304_state")
    if (
        cached.source_scope_key != source_scope_key_value
        or cached.config_fingerprint != config_fingerprint
        or cached.provider_identity != provider_identity
        or cached.representation_token != str(response_validator or "")
    ):
        raise ValueError("invalid_304_state")
    if hashlib.sha256(cached.payload).hexdigest() != cached.checksum:
        raise ValueError("cache_corruption")
    return bytes(cached.payload)


def source_checkpoint_key(company: str, source_scope: str, provider_identity: str) -> str:
    scope = source_scope_key(company, source_scope)
    provider = " ".join(str(provider_identity or "").casefold().split())
    if not provider:
        raise ValueError("provider_identity cannot be empty")
    return f"{scope}::{provider}"


def _mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise TypeError("value must be a mapping or expose to_dict()")


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _aware(value, "timestamp").isoformat().replace("+00:00", "Z")


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must include timezone information")
    return value.astimezone(timezone.utc)


def _age_seconds(value: Optional[datetime], now: datetime) -> Optional[int]:
    if value is None:
        return None
    return max(0, int((_aware(now, "now") - value.astimezone(timezone.utc)).total_seconds()))


def _numeric(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware(value, "checkpoint timestamp")
    text = str(value).strip()
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("checkpoint timestamp is malformed") from exc
    return _aware(parsed, "checkpoint timestamp")


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("ambiguous_since_last_full must be a boolean")
    return value
