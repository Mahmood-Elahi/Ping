"""Deterministic child-brand metadata for shared parent-company job sources.

The scanner applies these reviewed mappings after adapter normalization and
before filtering. Child brands remain metadata on one canonical parent job.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from src.scraper.company_careers.models import JobRecord


DEFAULT_PARENT_MAPPING_PATH = "config/company_parent_source_mappings.yaml"
DEFAULT_ORIGINAL_COMPANY_INVENTORY_PATH = "config/company_remaining_inventory_v2.csv"
CHILD_BRANDS_METADATA_KEY = "child_brands"
EXACT_NORMALIZED_MATCH_TYPE = "exact_normalized"
CHILD_BRAND_DISPLAY_BEHAVIOR = "child_brand_metadata"
ALLOWED_CONFIDENCE_VALUES = {"high"}
ALLOWED_MATCH_FIELDS = {
    "departments",
    "raw_metadata.fields.Subsidiary",
}
NEW_MAPPING_FIELDS = {
    "child_company",
    "parent_company",
    "enabled",
    "match_field",
    "match_type",
    "accepted_values",
    "canonical_source",
    "display_behavior",
    "confidence",
    "evidence",
}
LEGACY_MAPPING_FIELDS = {
    "child_company",
    "parent_company",
    "canonical_source",
    "official_child_careers_url",
    "official_parent_search_url",
    "match",
    "status",
}
LEGACY_SAFE_MAPPING_STATUS = "safe_with_metadata_mapping"


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


@dataclass(frozen=True)
class ParentSourceMapping:
    """A reviewed exact-value mapping on a canonical parent job source."""

    child_company: str
    parent_company: str
    enabled: bool
    canonical_source: str
    match_field: str
    exact_values: tuple[str, ...]
    match_type: str
    display_behavior: str
    confidence: str
    evidence: Mapping[str, Any]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParentSourceMapping":
        if not isinstance(data, Mapping):
            raise ValueError("Each parent-source mapping must be an object")
        fields = set(data)
        if "match" in data or "status" in data:
            unknown = fields - LEGACY_MAPPING_FIELDS
            if unknown:
                raise ValueError(f"Unknown parent-source mapping fields: {sorted(unknown)}")
            match = data.get("match")
            if not isinstance(match, Mapping):
                raise ValueError("Parent-source mapping must contain a match object")
            unknown_match = set(match) - {"field", "exact_values"}
            if unknown_match:
                raise ValueError(f"Unknown parent-source match fields: {sorted(unknown_match)}")
            if str(data.get("status") or "").strip() != LEGACY_SAFE_MAPPING_STATUS:
                raise ValueError("Only validated safe parent-source mappings may be loaded")
            match_field = str(match.get("field") or "").strip()
            raw_values = match.get("exact_values") or []
            enabled = True
            match_type = EXACT_NORMALIZED_MATCH_TYPE
            display_behavior = CHILD_BRAND_DISPLAY_BEHAVIOR
            confidence = "high"
            evidence = {
                "official_child_careers_url": str(
                    data.get("official_child_careers_url") or ""
                ).strip(),
                "official_parent_search_url": str(
                    data.get("official_parent_search_url") or ""
                ).strip(),
            }
        else:
            unknown = fields - NEW_MAPPING_FIELDS
            if unknown:
                raise ValueError(f"Unknown parent-source mapping fields: {sorted(unknown)}")
            match_field = str(data.get("match_field") or "").strip()
            raw_values = data.get("accepted_values") or []
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("Parent-source mapping enabled must be a boolean")
            match_type = str(data.get("match_type") or "").strip()
            display_behavior = str(data.get("display_behavior") or "").strip()
            confidence = str(data.get("confidence") or "").strip()
            evidence = data.get("evidence")
            if not isinstance(evidence, Mapping):
                raise ValueError("Parent-source mapping evidence must be an object")

        exact_values = _deduplicated_values(raw_values)
        mapping = cls(
            child_company=str(data.get("child_company") or "").strip(),
            parent_company=str(data.get("parent_company") or "").strip(),
            enabled=enabled,
            canonical_source=str(data.get("canonical_source") or "").strip(),
            match_field=match_field,
            exact_values=exact_values,
            match_type=match_type,
            display_behavior=display_behavior,
            confidence=confidence,
            evidence=dict(evidence),
        )
        mapping.validate()
        return mapping

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("child_company", self.child_company),
                ("parent_company", self.parent_company),
                ("canonical_source", self.canonical_source),
                ("match.field", self.match_field),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Parent-source mapping is missing {', '.join(missing)}")
        if not self.enabled:
            raise ValueError("Production parent-source mappings must be enabled")
        if self.match_type != EXACT_NORMALIZED_MATCH_TYPE:
            raise ValueError("Parent-source mappings must use normalized exact matching")
        if self.display_behavior != CHILD_BRAND_DISPLAY_BEHAVIOR:
            raise ValueError("Parent-source mappings must use child-brand metadata display")
        if self.confidence not in ALLOWED_CONFIDENCE_VALUES:
            raise ValueError("Only high-confidence parent-source mappings may be loaded")
        if self.match_field not in ALLOWED_MATCH_FIELDS:
            raise ValueError(
                "Parent-source mappings must use an approved structured exact-match field"
            )
        if not self.exact_values:
            raise ValueError("Parent-source mapping must contain exact_values")


def load_parent_source_mappings(
    path: str | Path = DEFAULT_PARENT_MAPPING_PATH,
) -> list[ParentSourceMapping]:
    mapping_path = Path(path)
    data = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
    unknown_root = set(data) - {"company_parent_source_mappings"}
    if unknown_root:
        raise ValueError(f"Unknown parent-source mapping document fields: {sorted(unknown_root)}")
    entries = data.get("company_parent_source_mappings")
    if not isinstance(entries, list):
        raise ValueError(f"Mapping file has no company_parent_source_mappings list: {path}")
    mappings = [ParentSourceMapping.from_dict(entry) for entry in entries]
    child_names: set[str] = set()
    identities: set[tuple[str, str, tuple[str, ...]]] = set()
    for mapping in mappings:
        child_key = _normalized(mapping.child_company)
        if child_key in child_names:
            raise ValueError(f"Duplicate normalized child company: {mapping.child_company}")
        child_names.add(child_key)
        identity = (
            _normalized(mapping.canonical_source),
            mapping.match_field,
            tuple(sorted(_normalized(value) for value in mapping.exact_values)),
        )
        if identity in identities:
            raise ValueError(f"Duplicate parent-source mapping identity: {mapping.child_company}")
        identities.add(identity)
    return sorted(mappings, key=lambda mapping: _normalized(mapping.child_company))


def canonical_job_key(job: JobRecord) -> tuple[str, str]:
    """Return the parent-source identity used regardless of child-brand labels."""
    return (_normalized(job.source), str(job.job_id).strip())


def original_company_coverage(
    targets: Iterable[Any],
    mappings: Sequence[ParentSourceMapping],
    *,
    inventory_path: str | Path = DEFAULT_ORIGINAL_COMPANY_INVENTORY_PATH,
) -> dict[str, Any]:
    """Count original-list coverage without treating mapped brands as scans."""
    with Path(inventory_path).open(encoding="utf-8-sig", newline="") as file:
        original_names = {
            _normalized(row.get("company")): str(row.get("company") or "").strip()
            for row in csv.DictReader(file)
            if str(row.get("company") or "").strip()
        }
    target_names = set()
    for target in targets:
        name = getattr(target, "name", None)
        if name is None and isinstance(target, Mapping):
            name = target.get("name")
        target_names.add(_normalized(name))
    canonical = {
        original_names[name]
        for name in target_names
        if name in original_names
    }
    mapped = {
        original_names[_normalized(mapping.child_company)]
        for mapping in mappings
        if mapping.enabled
        and _normalized(mapping.parent_company) in target_names
        and _normalized(mapping.child_company) in original_names
    }
    return {
        "original_list_total": len(original_names),
        "canonical_original_companies_covered": len(canonical),
        "mapped_child_brands_covered": len(mapped),
        "total_original_companies_covered": len(canonical | mapped),
        "mapped_child_companies": sorted(mapped, key=_normalized),
    }


def matching_child_brands(
    job: JobRecord,
    mappings: Sequence[ParentSourceMapping],
) -> list[str]:
    """Return deterministic child brands using normalized exact equality only."""
    matches: list[str] = []
    for mapping in mappings:
        if _normalized(job.source) != _normalized(mapping.canonical_source):
            continue
        if _normalized(job.company) != _normalized(mapping.parent_company):
            continue
        observed = {_normalized(value) for value in _field_values(job, mapping.match_field)}
        wanted = {_normalized(value) for value in mapping.exact_values}
        if observed.intersection(wanted):
            matches.append(mapping.child_company)
    return sorted(set(matches), key=_normalized)


def annotate_child_brand_metadata(
    job: JobRecord,
    mappings: Sequence[ParentSourceMapping],
) -> JobRecord:
    """Copy a job and add deterministic child brands without changing its identity."""
    brands = matching_child_brands(job, mappings)
    metadata = copy.deepcopy(job.raw_metadata)
    if brands:
        metadata[CHILD_BRANDS_METADATA_KEY] = brands
    else:
        metadata.pop(CHILD_BRANDS_METADATA_KEY, None)
    return replace(job, raw_metadata=metadata)


def _field_values(job: JobRecord, field: str) -> Iterable[str]:
    if field == "departments":
        return tuple(job.departments)
    if field == "raw_metadata.fields.Subsidiary":
        fields = job.raw_metadata.get("fields")
        if not isinstance(fields, Mapping):
            return ()
        values = fields.get("Subsidiary")
        if isinstance(values, list):
            return tuple(str(value) for value in values)
        if values not in (None, ""):
            return (str(values),)
        return ()
    raise ValueError(f"Unsupported parent-source match field: {field}")


def _deduplicated_values(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("Parent-source mapping accepted values must be a list")
    unique: dict[str, str] = {}
    for value in values:
        cleaned = str(value).strip()
        if cleaned:
            unique.setdefault(_normalized(cleaned), cleaned)
    return tuple(unique[key] for key in sorted(unique))
