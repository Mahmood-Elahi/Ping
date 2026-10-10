"""Persistable, source-local cooldown policy for comparable transient failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping

from src.scraper.company_careers.models import CompanyScanResult, CompanyTarget


COOLDOWN_HOURS = {3: 6, 4: 12}
MAX_COOLDOWN_HOURS = 24
TRANSIENT_HEALTH_CODES = {
    "temporary_unexpected_response",
}
NON_TRANSIENT_HEALTH_CODES = {
    "authentication_or_block",
    "source_stale",
    "source_migration_suspected",
    "identity_failure",
    "malformed_collection",
    "malformed_records",
    "pagination_loop",
    "safety_limit_reached",
    "collection_changed",
    "terminal_contradiction",
    "authoritative_total_not_reached",
    "pagination_unproven",
    "unproven_empty",
    "provider_cap_requires_partition",
    "unsplittable_partition",
    "partition_incomplete",
    "partition_contradiction",
}
TRANSIENT_ERROR_TYPES = {
    "request_error",
    "transient_upstream_http",
    "unexpected_upstream_http",
}
NON_TRANSIENT_ERROR_TYPES = {
    "access_blocked",
    "adapter_error",
    "adapter_protocol_failure",
    "adapter_schema_mismatch",
    "external_access_blocked",
    "official_page_blocked",
    "probable_ats_migration",
    "response_size_safety_rejection",
    "source_unresolved",
    "stale_board_identifier",
    "unsupported_ats_variant",
    "validation_identity_mismatch",
}


def source_health_key(target: CompanyTarget) -> str:
    """Return a source/config-local key used by cooldown persistence.

    A company/platform pair is not sufficient: a repaired tenant, board, or
    endpoint must not inherit the cooldown of the obsolete configuration.  The
    fingerprint deliberately excludes scan budgets, selectors, and other
    operational knobs so ordinary tuning does not discard comparable health.
    """
    company = " ".join(str(target.name or "").casefold().split())
    platform = " ".join(str(target.platform or "").casefold().split())
    source_fields = {
        name: _stable_config_value(getattr(target, name, None))
        for name in (
            "slug",
            "tenant",
            "wd_subdomain",
            "site_id",
            "site_ids",
            "site_number",
            "organization_id",
            "board_id",
            "client_guid",
            "index_name",
            "ref_num",
            "locale",
            "careers_url",
            "search_url",
            "endpoint_variant",
            "endpoint_url",
            "listing_url",
            "pagination_url_template",
            "applied_facets",
            "workday_identity",
            "dayforce_identity",
            "identity_regex",
            "allowed_source_hosts",
            "allowed_apply_hosts",
            "browser_static_variant",
            "browser_identity_selector",
            "browser_identity_regex",
            "single_page_collection",
            "pagination_contract",
            "next_page_selector",
        )
        if _stable_config_value(getattr(target, name, None)) not in (None, "", [], {})
    }
    canonical = json.dumps(
        {"company": company, "platform": platform, **source_fields},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{platform}:{company}:{fingerprint}"


def cooldown_hours(failure_count: int) -> int:
    count = max(0, int(failure_count))
    if count >= 5:
        return MAX_COOLDOWN_HOURS
    return COOLDOWN_HOURS.get(count, 0)


def cooldown_active(state: Mapping[str, Any] | None, *, as_of: datetime) -> bool:
    if not state:
        return False
    until = _parse_timestamp(state.get("cooldown_until"))
    return bool(until and _as_utc(as_of) < until)


def classify_failure(result: CompanyScanResult) -> str | None:
    """Classify only failures suitable for waiting and later normal probing."""
    if result.authoritative_for_absence:
        return None
    health = str(result.source_health_code or "").casefold()
    reason = str(result.stop_reason or "").casefold()
    error_type = str(result.error_type or "").casefold()
    message = str(result.error or "").casefold()
    if health in NON_TRANSIENT_HEALTH_CODES or result.safety_limit_reached:
        return "non_transient"
    if error_type in NON_TRANSIENT_ERROR_TYPES:
        return "non_transient"
    if reason in {
        "identity_failure",
        "repeated_page",
        "duplicate_inventory_ids",
        "item_failure_outside_inventory",
    }:
        return "non_transient"
    if reason == "request_failure" or error_type in TRANSIENT_ERROR_TYPES:
        return "transient_transport"
    if reason in {
        "workday_partition_transport_failure",
        "workday_partition_transient_http",
    }:
        return "transient_transport"
    if health in TRANSIENT_HEALTH_CODES:
        return "transient_source"
    if any(
        marker in message
        for marker in (
            "timeout",
            "timed out",
            "connection error",
            "connection reset",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    ):
        return "transient_upstream"
    return "non_transient"


def reconcile_source_health(
    results: Iterable[CompanyScanResult],
    previous: Mapping[str, Mapping[str, Any]],
    *,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Advance source health after a run without granting absence authority."""
    observed_at = _as_utc(as_of)
    rows: list[dict[str, Any]] = []
    for result in results:
        key = source_health_key(result.target)
        prior = dict(previous.get(key) or {})
        if result.source_health_code == "cooldown_skipped":
            continue
        base = {
            "source_key": key,
            "company": result.target.name,
            "platform": result.target.platform,
            "updated_at": observed_at.isoformat(),
            "last_successful_authoritative_inventory": prior.get(
                "last_successful_authoritative_inventory"
            ),
        }
        if result.authoritative_for_absence:
            rows.append(
                {
                    **base,
                    "consecutive_transient_failures": 0,
                    "cooldown_until": None,
                    "last_failure_classification": None,
                    "last_successful_authoritative_inventory": observed_at.isoformat(),
                }
            )
            continue
        classification = classify_failure(result)
        if classification and classification.startswith("transient_"):
            count = int(prior.get("consecutive_transient_failures") or 0) + 1
            hours = cooldown_hours(count)
            rows.append(
                {
                    **base,
                    "consecutive_transient_failures": count,
                    "cooldown_until": (
                        (observed_at + timedelta(hours=hours)).isoformat()
                        if hours
                        else None
                    ),
                    "last_failure_classification": classification,
                }
            )
        else:
            rows.append(
                {
                    **base,
                    # A partial or deterministic non-authoritative result is
                    # not recovery evidence.  Preserve any existing streak;
                    # only an authoritative inventory may clear it.
                    "consecutive_transient_failures": int(
                        prior.get("consecutive_transient_failures") or 0
                    ),
                    "cooldown_until": prior.get("cooldown_until"),
                    "last_failure_classification": prior.get(
                        "last_failure_classification"
                    ) or classification,
                }
            )
    return rows


def _stable_config_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key).casefold(): _stable_config_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]).casefold())
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_stable_config_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if value is None:
        return None
    return " ".join(str(value).strip().casefold().split())


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
