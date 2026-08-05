"""Deterministic child-brand metadata for shared parent-company job sources.

This module is deliberately not wired into the production scanner yet.  It
loads validated mappings and can annotate already-normalized parent jobs while
preserving the canonical parent company, source, and stable job identity.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from src.scraper.company_careers.models import JobRecord


DEFAULT_PARENT_MAPPING_PATH = "config/company_parent_source_mappings.yaml"
SAFE_MAPPING_STATUS = "safe_with_metadata_mapping"
CHILD_BRANDS_METADATA_KEY = "child_brands"
ALLOWED_MATCH_FIELDS = {
    "departments",
    "raw_metadata.fields.Subsidiary",
}


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


@dataclass(frozen=True)
class ParentSourceMapping:
    """A reviewed exact-value mapping on a canonical parent job source."""

    child_company: str
    parent_company: str
    canonical_source: str
    official_child_careers_url: str
    official_parent_search_url: str
    match_field: str
    exact_values: tuple[str, ...]
    status: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParentSourceMapping":
        match = data.get("match")
        if not isinstance(match, Mapping):
            raise ValueError("Parent-source mapping must contain a match object")
        exact_values = tuple(
            str(value).strip() for value in match.get("exact_values") or [] if str(value).strip()
        )
        mapping = cls(
            child_company=str(data.get("child_company") or "").strip(),
            parent_company=str(data.get("parent_company") or "").strip(),
            canonical_source=str(data.get("canonical_source") or "").strip(),
            official_child_careers_url=str(
                data.get("official_child_careers_url") or ""
            ).strip(),
            official_parent_search_url=str(
                data.get("official_parent_search_url") or ""
            ).strip(),
            match_field=str(match.get("field") or "").strip(),
            exact_values=exact_values,
            status=str(data.get("status") or "").strip(),
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
                ("official_child_careers_url", self.official_child_careers_url),
                ("official_parent_search_url", self.official_parent_search_url),
                ("match.field", self.match_field),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Parent-source mapping is missing {', '.join(missing)}")
        if self.status != SAFE_MAPPING_STATUS:
            raise ValueError("Only validated safe parent-source mappings may be loaded")
        if self.match_field not in ALLOWED_MATCH_FIELDS:
            raise ValueError(
                "Parent-source mappings must use an approved structured exact-match field"
            )
        if not self.exact_values:
            raise ValueError("Parent-source mapping must contain exact_values")
        normalized_values = [_normalized(value) for value in self.exact_values]
        if len(normalized_values) != len(set(normalized_values)):
            raise ValueError("Parent-source mapping exact_values must be unique")


def load_parent_source_mappings(
    path: str | Path = DEFAULT_PARENT_MAPPING_PATH,
) -> list[ParentSourceMapping]:
    mapping_path = Path(path)
    data = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
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
    return mappings


def canonical_job_key(job: JobRecord) -> tuple[str, str]:
    """Return the parent-source identity used regardless of child-brand labels."""
    return (_normalized(job.source), str(job.job_id).strip())


def matching_child_brands(
    job: JobRecord,
    mappings: Sequence[ParentSourceMapping],
) -> list[str]:
    """Return mapping-order child brands using normalized exact equality only."""
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
    return matches


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
