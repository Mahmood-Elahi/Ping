"""Offline validation and reporting for the Phase 7 infrastructure audit."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUDIT_PATH = ROOT / "config" / "phase7_infrastructure_audit.yaml"
INFRASTRUCTURE_CATEGORIES = frozenset({"G", "H", "I", "J", "K"})
BACKLOG_CATEGORIES = frozenset({"A", "B", "C", "D", "E", "F", "L"})


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return value


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classified_sources(audit: Mapping[str, Any]) -> dict[str, str]:
    classified: dict[str, str] = {}
    groups = audit.get("runtime_problem_classifications") or {}
    for category, companies in groups.items():
        for company in companies or []:
            name = str(company)
            if name in classified:
                raise ValueError(f"Source has multiple Phase 7 classifications: {name}")
            classified[name] = str(category)
    return classified


def validate_audit(path: Path = DEFAULT_AUDIT_PATH) -> dict[str, Any]:
    """Validate the audit against its immutable local evidence and registry."""

    audit = _load_yaml(path)
    evidence = audit["evidence"]
    registry_meta = evidence["production_registry"]
    snapshot_meta = evidence["newest_full_runtime_snapshot"]
    registry_path = ROOT / registry_meta["path"]
    snapshot_path = ROOT / snapshot_meta["path"]

    if _sha256(registry_path) != registry_meta["sha256"]:
        raise ValueError("Production registry changed after the Phase 7 audit")
    if _sha256(snapshot_path) != snapshot_meta["sha256"]:
        raise ValueError("Phase 7 runtime evidence snapshot changed")

    registry = _load_yaml(registry_path).get("target_companies") or []
    if len(registry) != int(registry_meta["configured_sources"]):
        raise ValueError("Configured production source count no longer matches the audit")
    canonical = {
        str(item.get("canonical_company") or item.get("name") or "").strip()
        for item in registry
    }
    canonical.discard("")
    if len(canonical) != int(registry_meta["canonical_companies"]):
        raise ValueError("Canonical company count no longer matches the audit")

    with snapshot_path.open("r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    runtime_problems = {
        str(row["company"])
        for row in snapshot["coverage"]
        if row.get("scan_health") in {"incomplete", "failed"}
    }
    classified = _classified_sources(audit)
    if set(classified) != runtime_problems:
        missing = sorted(runtime_problems - set(classified))
        extra = sorted(set(classified) - runtime_problems)
        raise ValueError(f"Phase 7 classifications disagree with snapshot: missing={missing}, extra={extra}")

    taxonomy = audit.get("taxonomy") or {}
    if set(taxonomy) != set("ABCDEFGHIJKLMN"):
        raise ValueError("Phase 7 taxonomy must define categories A through N")
    if not set(classified.values()).issubset(taxonomy):
        raise ValueError("A source uses an unknown Phase 7 blocker category")

    infrastructure_names = {
        name for name, category in classified.items() if category in INFRASTRUCTURE_CATEGORIES
    }
    candidate_names = {
        str(item["company"]) for item in audit.get("infrastructure_candidates") or []
    }
    if infrastructure_names != candidate_names:
        raise ValueError("Every infrastructure classification must have explicit evidence")
    return audit


def build_summary(path: Path = DEFAULT_AUDIT_PATH) -> dict[str, Any]:
    """Return the reproducible, repository-backed Phase 7 decision summary."""

    audit = validate_audit(path)
    classified = _classified_sources(audit)
    resolved = {
        str(item["company"]) for item in audit.get("resolved_after_runtime_snapshot") or []
    }
    post_snapshot = audit.get("post_snapshot_problem_findings") or []
    remaining = {name: category for name, category in classified.items() if name not in resolved}
    for item in post_snapshot:
        name = str(item["company"])
        if name in remaining:
            raise ValueError(f"Post-snapshot finding duplicates a runtime problem: {name}")
        remaining[name] = str(item["category"])

    return {
        "decision": audit["decision"],
        "primary_recommendation": audit["primary_recommendation"],
        "production": {
            "canonical_companies": audit["evidence"]["production_registry"]["canonical_companies"],
            "configured_sources": audit["evidence"]["production_registry"]["configured_sources"],
        },
        "newest_full_runtime_snapshot": {
            key: audit["evidence"]["newest_full_runtime_snapshot"][key]
            for key in ("path", "captured_at", "complete", "incomplete", "failed")
        },
        "runtime_problem_category_counts": dict(sorted(Counter(classified.values()).items())),
        "known_remaining_category_counts": dict(sorted(Counter(remaining.values()).items())),
        "known_remaining_problem_sources": len(remaining),
        "genuine_infrastructure_candidates": len(audit.get("infrastructure_candidates") or []),
        "production_retired_or_rejected": audit["candidate_and_retirement_counts"][
            "production_retired_or_rejected"
        ],
        "infrastructure_enabled": dict(audit["infrastructure_enabled"]),
    }
