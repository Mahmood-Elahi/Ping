"""Stable logical source-scope identity for lifecycle authority."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


DEFAULT_SOURCE_SCOPE = "default"
_SOURCE_SCOPE_RE = re.compile(
    r"^[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?(?::[a-z0-9]+(?:[a-z0-9_]*[a-z0-9])?)?$"
)


def normalize_source_scope(value: Any, *, default: bool = True) -> str:
    """Return a fail-closed machine scope identifier."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        if default:
            return DEFAULT_SOURCE_SCOPE
        raise ValueError("source_scope is required")
    if text != text.casefold() or not _SOURCE_SCOPE_RE.fullmatch(text):
        raise ValueError(
            "source_scope must be a normalized lowercase machine identifier "
            "using letters, digits, underscores, and at most one namespace colon"
        )
    return text


def normalize_canonical_company(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def source_scope_key(company: Any, source_scope: Any = DEFAULT_SOURCE_SCOPE) -> str:
    canonical_company = normalize_canonical_company(company)
    if not canonical_company:
        raise ValueError("canonical company cannot be empty")
    return f"{canonical_company}::{normalize_source_scope(source_scope)}"
