"""Deterministic source contracts used by registry onboarding and offline audit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import yaml


class SourceActivationState(str, Enum):
    ACTIVE = "active"
    PARTIAL = "partial"
    QUARANTINED = "quarantined"
    MIGRATION_REQUIRED = "migration_required"


class IntegrityAction(str, Enum):
    OK = "OK"
    REVIEW_CONFIG = "REVIEW_CONFIG"
    TERMINAL_UNPROVEN = "TERMINAL_UNPROVEN"
    SOURCE_STALE = "SOURCE_STALE"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    ARCHITECTURAL = "ARCHITECTURAL"


@dataclass(frozen=True)
class PlatformSourceContract:
    terminal_strategy: str
    identity_groups: tuple[tuple[str, ...], ...] = ()
    source_url_fields: tuple[str, ...] = ()
    require_allowed_apply_hosts: bool = False
    dynamic_enumeration: bool = True
    page_capacity: int | None = None


@dataclass(frozen=True)
class SourceIntegrityAssessment:
    company: str
    platform: str
    activation_state: SourceActivationState
    action: IntegrityAction
    terminal_strategy: str
    reasons: tuple[str, ...]
    source: str
    configured_max_pages: int
    configured_max_jobs: int
    retained_reported_total: int | None = None
    retained_scan_state: str | None = None
    retained_health_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        parsed_source = urlparse(self.source)
        return {
            "company": self.company,
            "platform": self.platform,
            "source": self.source,
            "activation_state": self.activation_state.value,
            "activation_scope": "declared_source_contract",
            "runtime_health_claimed": False,
            "health_classification": self.action.value,
            "terminal_strategy": self.terminal_strategy,
            "known_complete_capability": self.activation_state is SourceActivationState.ACTIVE,
            "configured_bounds": {
                "max_pages": self.configured_max_pages,
                "max_jobs": self.configured_max_jobs,
            },
            "retained_reported_total": self.retained_reported_total,
            "retained_scan_state": self.retained_scan_state,
            "retained_health_code": self.retained_health_code,
            "risk_reasons": list(self.reasons),
            "recommended_action_class": self.action.value,
            "source_contract_fingerprint": {
                "platform": self.platform,
                "official_hostname": (parsed_source.hostname or "").casefold() or None,
                "source_identity": self.source if not parsed_source.hostname else None,
                "terminal_strategy": self.terminal_strategy,
            },
        }


def _contract(
    strategy: str,
    *identity_groups: tuple[str, ...],
    urls: tuple[str, ...] = (),
    hosts: bool = False,
    dynamic: bool = True,
    page_capacity: int | None = None,
) -> PlatformSourceContract:
    return PlatformSourceContract(
        strategy, identity_groups, urls, hosts, dynamic, page_capacity
    )


# A source can inherit terminal semantics only from a reviewed adapter entry in
# this table.  Merely adding a platform string to YAML cannot activate it.
PLATFORM_SOURCE_CONTRACTS: dict[str, PlatformSourceContract] = {
    "greenhouse": _contract("complete_collection_response", ("slug",)),
    "ashby": _contract("complete_collection_response", ("slug",)),
    "lever": _contract("explicit_has_more_or_complete_collection", ("slug",)),
    "workday": _contract("authoritative_total_or_terminal_page", ("tenant",), ("wd_subdomain",), ("site_id", "site_ids")),
    "oracle_recruiting": _contract("authoritative_total", ("site_number",), urls=("endpoint_url", "careers_url")),
    "sap_successfactors": _contract("authoritative_total_or_terminal_page", urls=("search_url",)),
    "eightfold": _contract("authoritative_total_or_explicit_terminal", ("organization_id",), urls=("endpoint_url",), page_capacity=10),
    "phenom": _contract("authoritative_total", ("ref_num",), urls=("search_url",), hosts=True),
    "icims": _contract("authoritative_total_or_explicit_terminal", ("board_id",), urls=("endpoint_url", "search_url"), hosts=True),
    "jobvite": _contract("authoritative_result_range", ("slug",), ("identity_regex",), urls=("listing_url", "careers_url"), hosts=True, dynamic=False, page_capacity=500),
    "static_html": _contract("reviewed_structural_terminal", ("external_id_attribute", "external_id_selector", "external_id_regex", "allow_url_id_fallback"), urls=("listing_url",), hosts=True),
    "browser_rendered_html": _contract("reviewed_browser_structural_terminal", ("browser_identity_selector", "browser_identity_regex"), ("external_id_attribute", "external_id_selector", "external_id_regex"), urls=("listing_url",), hosts=True),
    "public_json": _contract(
        "authoritative_complete_response_or_total_pages",
        ("endpoint_variant",),
        urls=("endpoint_url",),
        hosts=True,
    ),
    "first_party_json": _contract(
        "authoritative_total",
        ("endpoint_variant", "identity_regex"),
        urls=("careers_url", "endpoint_url"),
        hosts=True,
        page_capacity=100,
    ),
    "source_xml": _contract(
        "authoritative_xml_total_or_same_run_official_listing_total",
        ("endpoint_variant", "identity_regex"),
        ("id_field",),
        ("title_field",),
        ("url_field",),
        ("complete_collection_total_field", "listing_total_regex"),
        ("complete_collection_total_field", "listing_identity_regex"),
        ("allowed_source_hosts",),
        urls=("endpoint_url",),
        hosts=True,
        dynamic=False,
        page_capacity=20_000,
    ),
    "javascript_embedded_data": _contract("validated_complete_embedded_collection", ("id_field", "external_id_regex"), ("embedded_json_path",), urls=("listing_url",), hosts=True),
    "adp": _contract("authoritative_total_or_terminal_page", ("slug", "organization_id", "endpoint_variant"), urls=("careers_url",), hosts=True),
    "apple": _contract("authoritative_total_or_terminal_page", urls=("endpoint_url",), hosts=True),
    "avature": _contract("authoritative_total_or_explicit_terminal", ("board_id", "site_id"), urls=("search_url",), hosts=True),
    "bamboohr": _contract("complete_collection_response", ("slug",), urls=("listing_url",), hosts=True),
    "beesite": _contract("authoritative_total_or_explicit_terminal", ("identity_regex",), urls=("endpoint_url",), hosts=True),
    "clearcompany": _contract("complete_collection_response", ("site_id",), urls=("endpoint_url",), hosts=True),
    "cornerstone_csod": _contract("authoritative_total_or_explicit_terminal", ("tenant",), ("site_id",), urls=("search_url", "endpoint_url"), hosts=True, page_capacity=25),
    "dayforce": _contract("authoritative_total_or_explicit_terminal", ("tenant",), ("site_id",), urls=("search_url", "endpoint_url"), hosts=True),
    "deel": _contract("validated_complete_collection", ("tenant",), urls=("listing_url",), hosts=True),
    "exalead": _contract("authoritative_total_or_explicit_terminal", urls=("endpoint_url",), hosts=True),
    "hawksearch_v2": _contract("authoritative_total", ("client_guid",), urls=("endpoint_url",), hosts=True),
    "hibob": _contract("complete_collection_response", ("slug",), urls=("endpoint_url",), hosts=True),
    "mcloud_cws": _contract("authoritative_total_or_explicit_terminal", ("organization_id",), urls=("endpoint_url",), hosts=True),
    "paycor": _contract("validated_complete_collection", urls=("endpoint_url",)),
    "peoplesoft": _contract("validated_listing_and_detail_collection", ("identity_regex",), urls=("listing_url",), hosts=True, dynamic=False),
    "smartrecruiters": _contract("authoritative_total", ("slug",), urls=("endpoint_url",), hosts=True),
    # StartDate downloads one complete list before applying its configured
    # normal bound, and safely expands to that observed list under the shared
    # absolute ceiling.
    "startdate": _contract("validated_complete_collection", ("identity_regex",), urls=("endpoint_url",), hosts=True, page_capacity=500),
    "talentbrew": _contract("authoritative_total_or_explicit_terminal", ("identity_regex",), urls=("listing_url",), hosts=True),
    "teamtailor": _contract("validated_complete_collection", ("identity_regex",), urls=("listing_url",), hosts=True),
    # UKG publishes totalCount and the adapter's listing-only completion stage
    # expands beyond legacy normal bounds without coupling detail requests.
    "ukg_pro": _contract("validated_complete_collection", ("identity_regex",), urls=("careers_url",), page_capacity=50),
    "amazon": _contract("all_configured_queries_terminal"),
    "shopify": _contract("validated_complete_collection"),
    "google": _contract("all_configured_queries_terminal", ("search_terms",)),
    "meta": _contract("explicit_pagination_terminal", ("search_terms",)),
    "atlassian": _contract("validated_complete_collection"),
    "uber": _contract("explicit_pagination_terminal"),
    "ibm_search": _contract("authoritative_total_or_terminal_page", ("search_terms",)),
}


def load_registry_entries(path: str | Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("target_companies") or data.get("companies")
    if not isinstance(entries, list):
        raise ValueError(f"Registry has no company list: {path}")
    if not all(isinstance(entry, Mapping) for entry in entries):
        raise ValueError(f"Registry entries must be mappings: {path}")
    return [dict(entry) for entry in entries]


def assess_registry_entry(
    entry: Mapping[str, Any],
    *,
    retained: Mapping[str, Any] | None = None,
    effective_bounds: Mapping[str, int] | None = None,
) -> SourceIntegrityAssessment:
    company = str(entry.get("name") or "").strip()
    platform = str(entry.get("platform") or "").strip().casefold()
    contract = PLATFORM_SOURCE_CONTRACTS.get(platform)
    reasons: list[str] = []
    bounds = effective_bounds or entry
    max_pages = _positive_int(bounds.get("max_pages"), 5)
    max_jobs = _positive_int(bounds.get("max_jobs"), 20)
    retained = retained or {}
    total = retained.get("total_available")
    retained_total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None
    retained_state = str(retained.get("scan_state") or retained.get("scan_health") or "").strip() or None
    retained_code = str(retained.get("source_health_code") or retained.get("error_type") or "").strip() or None

    if not company:
        reasons.append("company identity is missing")
    if contract is None:
        reasons.append("platform has no reviewed source-completeness contract")
        return _assessment(
            company, platform, SourceActivationState.QUARANTINED,
            IntegrityAction.ARCHITECTURAL, "none", reasons, entry,
            max_pages, max_jobs, retained_total, retained_state, retained_code,
        )

    for group in contract.identity_groups:
        if not any(_present(entry.get(field)) for field in group):
            reasons.append("missing source identity: one of " + ", ".join(group))
    if contract.source_url_fields and not any(
        _valid_https_url(entry.get(field)) for field in contract.source_url_fields
    ):
        reasons.append(
            "one explicit HTTPS source URL is required: "
            + ", ".join(contract.source_url_fields)
        )
    if contract.require_allowed_apply_hosts and not _valid_hosts(entry.get("allowed_apply_hosts")):
        reasons.append("allowed_apply_hosts must contain hostname-only approved destinations")
    if max_pages < 1 or max_jobs < 1:
        reasons.append("safety bounds must be positive")

    page_capacity = _positive_int(entry.get("page_size"), 0)
    if page_capacity < 1:
        page_capacity = int(contract.page_capacity or 0)
    unreachable_config_bound = bool(
        not contract.dynamic_enumeration
        and page_capacity > 0
        and max_pages > 0
        and max_jobs > max_pages * page_capacity
    )

    failure_text = " ".join(
        str(retained.get(field) or "")
        for field in ("error_type", "error", "stop_reason", "source_health_code")
    ).casefold()
    if "migration" in failure_text:
        return _assessment(
            company, platform, SourceActivationState.MIGRATION_REQUIRED,
            IntegrityAction.MIGRATION_REQUIRED, contract.terminal_strategy,
            [*reasons, "retained evidence indicates an ATS/source migration"], entry,
            max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    if any(token in failure_text for token in ("stale", "source_unresolved", "http 404", "http 410")):
        return _assessment(
            company, platform, SourceActivationState.QUARANTINED,
            IntegrityAction.SOURCE_STALE, contract.terminal_strategy,
            [*reasons, "retained evidence indicates a stale or expired source"], entry,
            max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    if reasons:
        return _assessment(
            company, platform, SourceActivationState.PARTIAL,
            IntegrityAction.ARCHITECTURAL, contract.terminal_strategy, reasons,
            entry, max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    if unreachable_config_bound:
        return _assessment(
            company, platform, SourceActivationState.PARTIAL,
            IntegrityAction.REVIEW_CONFIG, contract.terminal_strategy,
            [
                f"max_jobs {max_jobs} is unreachable with max_pages {max_pages} "
                f"and effective page capacity {page_capacity}"
            ],
            entry, max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    if retained_total is not None and retained_total > max_jobs and not contract.dynamic_enumeration:
        return _assessment(
            company, platform, SourceActivationState.PARTIAL,
            IntegrityAction.REVIEW_CONFIG, contract.terminal_strategy,
            [f"retained authoritative total {retained_total} exceeds max_jobs {max_jobs} and this contract cannot adapt"],
            entry, max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    if retained and retained_state != "complete":
        return _assessment(
            company, platform, SourceActivationState.ACTIVE,
            IntegrityAction.TERMINAL_UNPROVEN, contract.terminal_strategy,
            ["latest retained scan did not prove completeness"], entry,
            max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    if retained and retained_state == "complete" and not retained.get("terminal_evidence_type"):
        return _assessment(
            company, platform, SourceActivationState.ACTIVE,
            IntegrityAction.TERMINAL_UNPROVEN, contract.terminal_strategy,
            ["retained complete claim predates explicit terminal-evidence diagnostics"], entry,
            max_pages, max_jobs, retained_total, retained_state, retained_code,
        )
    return _assessment(
        company, platform, SourceActivationState.ACTIVE, IntegrityAction.OK,
        contract.terminal_strategy, reasons, entry, max_pages, max_jobs,
        retained_total, retained_state, retained_code,
    )


def assess_registry(
    entries: Iterable[Mapping[str, Any]],
    *,
    retained_by_company: Mapping[str, Mapping[str, Any]] | None = None,
    effective_bounds_by_company: Mapping[str, Mapping[str, int]] | None = None,
) -> list[SourceIntegrityAssessment]:
    retained_by_company = retained_by_company or {}
    effective_bounds_by_company = effective_bounds_by_company or {}
    results: list[SourceIntegrityAssessment] = []
    seen: set[str] = set()
    for entry in entries:
        company = str(entry.get("name") or "").strip()
        key = _company_key(company)
        assessment = assess_registry_entry(
            entry,
            retained=retained_by_company.get(key),
            effective_bounds=effective_bounds_by_company.get(key),
        )
        if key in seen:
            assessment = SourceIntegrityAssessment(
                **{
                    **assessment.__dict__,
                    "activation_state": SourceActivationState.QUARANTINED,
                    "action": IntegrityAction.ARCHITECTURAL,
                    "reasons": (*assessment.reasons, "duplicate normalized company identity"),
                }
            )
        seen.add(key)
        results.append(assessment)
    return results


def blocking_onboarding_issues(assessment: SourceIntegrityAssessment) -> tuple[str, ...]:
    """Return reasons that prevent a source from production-trusted activation."""
    if assessment.activation_state is SourceActivationState.ACTIVE and assessment.action in {
        IntegrityAction.OK,
        IntegrityAction.TERMINAL_UNPROVEN,
    }:
        # TERMINAL_UNPROVEN from historical runtime data is operationally
        # degraded, but the declared adapter/onboarding contract remains valid.
        return ()
    return assessment.reasons or (assessment.action.value,)


def _assessment(
    company: str,
    platform: str,
    activation: SourceActivationState,
    action: IntegrityAction,
    strategy: str,
    reasons: Sequence[str],
    entry: Mapping[str, Any],
    max_pages: int,
    max_jobs: int,
    retained_total: int | None,
    retained_state: str | None,
    retained_code: str | None,
) -> SourceIntegrityAssessment:
    return SourceIntegrityAssessment(
        company=company,
        platform=platform,
        activation_state=activation,
        action=action,
        terminal_strategy=strategy,
        reasons=tuple(dict.fromkeys(str(reason) for reason in reasons if str(reason))),
        source=_source_label(entry),
        configured_max_pages=max_pages,
        configured_max_jobs=max_jobs,
        retained_reported_total=retained_total,
        retained_scan_state=retained_state,
        retained_health_code=retained_code,
    )


def _source_label(entry: Mapping[str, Any]) -> str:
    for field in ("endpoint_url", "search_url", "listing_url", "careers_url", "slug", "tenant"):
        if _present(entry.get(field)):
            return str(entry[field]).strip()
    return ""


def _valid_https_url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.hostname)


def _valid_hosts(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        str(host).strip()
        and "/" not in str(host)
        and ":" not in str(host)
        and " " not in str(host)
        for host in value
    )


def _present(value: Any) -> bool:
    if isinstance(value, (list, tuple, set, Mapping)):
        return bool(value)
    return bool(str(value or "").strip())


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value if value is not None else fallback)
    except (TypeError, ValueError):
        return 0
    return parsed


def _company_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())
