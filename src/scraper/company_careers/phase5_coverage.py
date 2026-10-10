"""Deterministic, configuration-only reporting for Phase 5 coverage work."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.scraper.company_careers.source_integrity import assess_registry
from src.utils.source_scope import (
    normalize_canonical_company,
    normalize_source_scope,
)


PRODUCTION_REGISTRY = Path("config/target_companies.yaml")
PHASE5_CANDIDATES = Path("config/target_companies.phase5_batch1_candidates.yaml")
PHASE5_CANARIES = Path("config/target_companies.phase5_batch1_canaries.yaml")
PHASE5_BATCH2_CANDIDATES = Path(
    "config/target_companies.phase5_batch2_candidates.yaml"
)
PHASE5_BATCH2_CANARIES = Path("config/target_companies.phase5_batch2_canaries.yaml")
PHASE5_STATUSES = (
    "READY_FOR_CANARY",
    "NEEDS_IDENTITY_EVIDENCE",
    "NEEDS_SCOPE_EVIDENCE",
    "DUPLICATE_SCOPE",
    "UNSUPPORTED_PROTOCOL",
    "PROVIDER_IMPLEMENTED",
    "DEFER",
    "REJECTED",
)


def build_registry_scope_metrics(path: str | Path) -> dict[str, Any]:
    """Count canonical companies and logical scopes in any target registry."""
    entries = _yaml(path).get("target_companies")
    if not isinstance(entries, list) or not all(
        isinstance(entry, Mapping) for entry in entries
    ):
        raise ValueError("Registry must contain target_companies")
    scopes: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        company = normalize_canonical_company(entry.get("name"))
        if not company:
            raise ValueError("Registry company name cannot be empty")
        scope = normalize_source_scope(entry.get("source_scope"))
        if scope in scopes[company]:
            raise ValueError(
                f"Duplicate configured source scope: {entry.get('name')} / {scope}"
            )
        scopes[company].add(scope)
    multi_scope = sorted(company for company, values in scopes.items() if len(values) > 1)
    return {
        "canonical_companies": len(scopes),
        "configured_sources": len(entries),
        "source_scopes": sum(len(values) for values in scopes.values()),
        "companies_with_multiple_recruiting_scopes": len(multi_scope),
        "multi_scope_companies": multi_scope,
    }


def _yaml(path: str | Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Expected a YAML mapping: {path}")
    return dict(value)


def load_phase5_candidates(
    path: str | Path = PHASE5_CANDIDATES,
) -> list[dict[str, Any]]:
    entries = _yaml(path).get("phase5_candidates")
    if not isinstance(entries, list) or not all(
        isinstance(entry, Mapping) for entry in entries
    ):
        raise ValueError("Phase 5 candidate registry must contain phase5_candidates")
    normalized: list[dict[str, Any]] = []
    for raw in entries:
        entry = dict(raw)
        canonical_company = str(
            entry.get("canonical_company") or entry.get("company") or ""
        ).strip()
        source_scope = normalize_source_scope(
            entry.get("source_scope") or entry.get("scope")
        )
        if not canonical_company:
            raise ValueError("Phase 5 candidate canonical_company cannot be empty")
        if entry.get("status") == "READY_FOR_CANARY" and (
            "canonical_company" not in entry or "source_scope" not in entry
        ):
            raise ValueError(
                "READY_FOR_CANARY candidates require explicit canonical_company and source_scope"
            )
        entry["canonical_company"] = canonical_company
        entry["source_scope"] = source_scope
        normalized.append(entry)
    return normalized


def load_phase5_canary_plans(
    path: str | Path = PHASE5_CANARIES,
) -> list[dict[str, Any]]:
    entries = _yaml(path).get("phase5_canary_plans")
    if not isinstance(entries, list) or not all(
        isinstance(entry, Mapping) for entry in entries
    ):
        raise ValueError("Phase 5 canary registry must contain phase5_canary_plans")
    return [dict(entry) for entry in entries]


def build_phase5_coverage_report(
    production_path: str | Path = PRODUCTION_REGISTRY,
    candidate_path: str | Path = PHASE5_CANDIDATES,
    canary_path: str | Path = PHASE5_CANARIES,
) -> dict[str, Any]:
    """Return offline metrics without importing adapters or making requests."""
    production = _yaml(production_path).get("target_companies")
    if not isinstance(production, list) or not all(
        isinstance(entry, Mapping) for entry in production
    ):
        raise ValueError("Production registry must contain target_companies")
    production_entries = [dict(entry) for entry in production]
    candidates = load_phase5_candidates(candidate_path)
    canaries = load_phase5_canary_plans(canary_path)
    assessments = assess_registry(production_entries)

    active = [item for item in assessments if item.activation_state.value == "active"]
    identity_status = Counter()
    completeness = Counter()
    for item in assessments:
        missing_identity = any("identity" in reason for reason in item.reasons)
        identity_status[
            "unresolved" if missing_identity else "contract_configured"
        ] += 1
        completeness[
            "authoritative_capable"
            if item.activation_state.value == "active"
            else "not_authoritative_capable"
        ] += 1

    active_scopes: dict[str, set[str]] = defaultdict(set)
    for entry in production_entries:
        company = str(entry.get("name") or "").strip()
        scope = normalize_source_scope(entry.get("source_scope"))
        active_scopes[normalize_canonical_company(company)].add(scope)
    active_multi_scope = sorted(
        company for company, scopes in active_scopes.items() if len(scopes) > 1
    )

    discovered_scopes: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        parent = normalize_canonical_company(candidate["canonical_company"])
        discovered_scopes[parent].add(candidate["source_scope"])
    phase5_multi_scope = sorted(
        company
        for company, candidate_scopes in discovered_scopes.items()
        if len(candidate_scopes | active_scopes.get(company, set())) > 1
    )

    status_counts = Counter(str(item.get("status") or "") for item in candidates)
    provider_counts = Counter(
        str(entry.get("platform") or "unconfigured") for entry in production_entries
    )
    return {
        "canonical_companies": len(active_scopes),
        "configured_sources": len(production_entries),
        "source_scopes": sum(len(scopes) for scopes in active_scopes.values()),
        "active_sources": len(active),
        "active_sources_by_provider": dict(sorted(provider_counts.items())),
        "sources_by_identity_status": dict(sorted(identity_status.items())),
        "sources_by_completeness_capability": dict(sorted(completeness.items())),
        "companies_with_multiple_recruiting_scopes": len(active_multi_scope),
        "multi_scope_companies": active_multi_scope,
        "phase5_discovered_multi_scope_company_count": len(phase5_multi_scope),
        "phase5_discovered_multi_scope_companies": phase5_multi_scope,
        "phase5_candidate_count": len(candidates),
        "candidate_source_scopes": len(
            {
                (
                    normalize_canonical_company(item["canonical_company"]),
                    item["source_scope"],
                )
                for item in candidates
            }
        ),
        "phase5_canary_count": len(canaries),
        "candidate_status_counts": {
            status: status_counts.get(status, 0)
            for status in PHASE5_STATUSES
            if status_counts.get(status, 0)
            or status
            in {
                "READY_FOR_CANARY",
                "NEEDS_IDENTITY_EVIDENCE",
                "NEEDS_SCOPE_EVIDENCE",
                "DUPLICATE_SCOPE",
                "UNSUPPORTED_PROTOCOL",
                "REJECTED",
            }
        },
    }


def build_phase5_batched_coverage_report(
    production_path: str | Path = PRODUCTION_REGISTRY,
    batch1_candidate_path: str | Path = PHASE5_CANDIDATES,
    batch1_canary_path: str | Path = PHASE5_CANARIES,
    batch2_candidate_path: str | Path = PHASE5_BATCH2_CANDIDATES,
    batch2_canary_path: str | Path = PHASE5_BATCH2_CANARIES,
) -> dict[str, Any]:
    """Return deterministic production, Batch 1, and Batch 2 metrics."""
    batch1 = build_phase5_coverage_report(
        production_path, batch1_candidate_path, batch1_canary_path
    )
    batch2 = build_phase5_coverage_report(
        production_path, batch2_candidate_path, batch2_canary_path
    )
    batch2_data = _yaml(batch2_candidate_path)
    batch1_by_id = {
        item["candidate_id"]: item
        for item in load_phase5_candidates(batch1_candidate_path)
    }
    batch2_candidates = load_phase5_candidates(batch2_candidate_path)
    promoted = [
        item["candidate_id"]
        for item in batch2_candidates
        if item.get("status") == "READY_FOR_CANARY"
        and item.get("previous_status") != "READY_FOR_CANARY"
    ]
    provider_decisions = batch2_data.get("provider_decisions") or {}
    if not isinstance(provider_decisions, Mapping):
        raise ValueError("Phase 5 provider_decisions must be a mapping")
    provider_counts = Counter(
        str(value.get("decision") or "")
        for value in provider_decisions.values()
        if isinstance(value, Mapping)
    )
    resolved_status_changes = [
        item["candidate_id"]
        for item in batch2_candidates
        if item.get("candidate_id") in batch1_by_id
        and item.get("status") != batch1_by_id[item["candidate_id"]].get("status")
    ]
    batch2_statuses = Counter(str(item.get("status") or "") for item in batch2_candidates)
    return {
        "production": {
            key: batch2[key]
            for key in (
                "canonical_companies",
                "configured_sources",
                "source_scopes",
                "active_sources",
                "active_sources_by_provider",
                "sources_by_identity_status",
                "sources_by_completeness_capability",
                "companies_with_multiple_recruiting_scopes",
                "multi_scope_companies",
            )
        },
        "batch1": {
            "candidate_count": batch1["phase5_candidate_count"],
            "candidate_source_scopes": batch1["candidate_source_scopes"],
            "canary_count": batch1["phase5_canary_count"],
            "status_counts": batch1["candidate_status_counts"],
            "discovered_multi_scope_company_count": batch1[
                "phase5_discovered_multi_scope_company_count"
            ],
        },
        "batch2": {
            "candidate_count": batch2["phase5_candidate_count"],
            "candidate_source_scopes": batch2["candidate_source_scopes"],
            "canary_count": batch2["phase5_canary_count"],
            "status_counts": batch2["candidate_status_counts"],
            "discovered_multi_scope_company_count": batch2[
                "phase5_discovered_multi_scope_company_count"
            ],
            "promoted_to_ready": promoted,
            "resolved_status_changes": resolved_status_changes,
            "provider_decision_counts": dict(sorted(provider_counts.items())),
            "providers_evaluated": len(provider_decisions),
            "providers_deferred": provider_counts.get("DEFER", 0),
            "provider_implementations": batch2_statuses.get(
                "PROVIDER_IMPLEMENTED", 0
            ),
            "still_blocked": sum(
                batch2_statuses.get(status, 0)
                for status in (
                    "NEEDS_IDENTITY_EVIDENCE",
                    "NEEDS_SCOPE_EVIDENCE",
                    "DUPLICATE_SCOPE",
                    "UNSUPPORTED_PROTOCOL",
                    "DEFER",
                )
            ),
            "rejected": batch2_statuses.get("REJECTED", 0),
            "multi_scope_architecture_blockers": int(
                batch2_data.get("architecture_conclusion")
                == "MULTI_SCOPE_ARCHITECTURE_REQUIRED"
            ),
            "architecture_conclusion": batch2_data.get("architecture_conclusion"),
            "phase6_started": bool(batch2_data.get("phase6_started")),
        },
    }
