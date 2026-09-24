"""Small, deterministic diagnostics for untrusted ATS response shapes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


MAX_SAMPLE_LENGTH = 80
MAX_FIELDS = 16
MAX_ENTRIES = 3
_OMITTED_FIELD_TOKENS = (
    "description",
    "summary",
    "content",
    "html",
    "token",
    "password",
)


def diagnostic_shape(value: Any) -> str:
    """Return a stable type/size label without serializing ``value``."""
    if isinstance(value, Mapping):
        return f"object[{len(value)}]"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if value is None:
        return "null"
    return type(value).__name__


def bounded_scalar_sample(value: Any, *, max_length: int = MAX_SAMPLE_LENGTH) -> str:
    """Escape and truncate a scalar for logs; containers are represented by shape."""
    limit = max(1, min(int(max_length), MAX_SAMPLE_LENGTH))
    if isinstance(value, (Mapping, list, tuple, set)):
        return diagnostic_shape(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = str(value)
    else:
        # JSON string escaping makes newlines/control characters safe and keeps
        # the output deterministic across terminals without retaining quotes.
        rendered = json.dumps(str(value), ensure_ascii=True)[1:-1]
    if len(rendered) <= limit:
        return rendered
    suffix = "..."
    return rendered[: max(0, limit - len(suffix))] + suffix


def scalar_diagnostic(value: Any, *, max_length: int = MAX_SAMPLE_LENGTH) -> str:
    """Return a bounded scalar type/sample pair."""
    return (
        f"{diagnostic_shape(value)}"
        f"(sample=\"{bounded_scalar_sample(value, max_length=max_length)}\")"
    )


def structure_diagnostic(
    value: Any,
    *,
    max_entries: int = MAX_ENTRIES,
    max_fields: int = MAX_FIELDS,
    max_sample_length: int = MAX_SAMPLE_LENGTH,
) -> str:
    """Describe shallow object/list structure without dumping arbitrary payloads.

    Field names, scalar types/samples, and one nested field-name level are
    retained. Description/body/content-like fields and secrets are named only;
    their values are never sampled.
    """
    entry_limit = max(1, min(int(max_entries), MAX_ENTRIES))
    field_limit = max(1, min(int(max_fields), MAX_FIELDS))
    values = value if isinstance(value, list) else [value]
    parts = [f"entries={len(values)}", f"shape={diagnostic_shape(value)}"]
    for index, entry in enumerate(values[:entry_limit]):
        if not isinstance(entry, Mapping):
            parts.append(
                f"entry[{index}]={scalar_diagnostic(entry, max_length=max_sample_length)}"
            )
            continue
        ordered = sorted((str(key), item) for key, item in entry.items())[:field_limit]
        fields = ",".join(key for key, _ in ordered) or "none"
        scalars: list[str] = []
        nested: list[str] = []
        for key, item in ordered:
            omitted = any(token in key.casefold() for token in _OMITTED_FIELD_TOKENS)
            if isinstance(item, Mapping):
                child_names = sorted(str(child) for child in item)[:field_limit]
                nested.append(
                    f"{key}({','.join(child_names) if child_names else 'none'})"
                )
                for child_name, child_value in sorted(
                    ((str(child), child_value) for child, child_value in item.items()),
                    key=lambda pair: pair[0].casefold(),
                )[:field_limit]:
                    child_omitted = omitted or any(
                        token in child_name.casefold() for token in _OMITTED_FIELD_TOKENS
                    )
                    if isinstance(child_value, Mapping):
                        grandchild_names = sorted(
                            str(grandchild) for grandchild in child_value
                        )[:field_limit]
                        nested.append(
                            f"{key}.{child_name}"
                            f"({','.join(grandchild_names) if grandchild_names else 'none'})"
                        )
                        if not child_omitted:
                            for grandchild_name, grandchild_value in sorted(
                                (
                                    (str(grandchild), grandchild_value)
                                    for grandchild, grandchild_value in child_value.items()
                                ),
                                key=lambda pair: pair[0].casefold(),
                            )[:field_limit]:
                                if isinstance(grandchild_value, (Mapping, list)):
                                    continue
                                if any(
                                    token in grandchild_name.casefold()
                                    for token in _OMITTED_FIELD_TOKENS
                                ):
                                    continue
                                scalars.append(
                                    f"{key}.{child_name}.{grandchild_name}="
                                    f"{scalar_diagnostic(grandchild_value, max_length=max_sample_length)}"
                                )
                        continue
                    if isinstance(child_value, list):
                        nested.append(f"{key}.{child_name}[{len(child_value)}]")
                        continue
                    if child_omitted:
                        continue
                    scalars.append(
                        f"{key}.{child_name}="
                        f"{scalar_diagnostic(child_value, max_length=max_sample_length)}"
                    )
            elif isinstance(item, list):
                nested.append(f"{key}[{len(item)}]")
            elif not omitted:
                scalars.append(
                    f"{key}={scalar_diagnostic(item, max_length=max_sample_length)}"
                )
        parts.append(
            f"entry[{index}] fields=[{fields}] "
            f"scalars=[{'; '.join(scalars) if scalars else 'none'}] "
            f"nested=[{'; '.join(nested) if nested else 'none'}]"
        )
    if len(values) > entry_limit:
        parts.append(f"entries_omitted={len(values) - entry_limit}")
    return "; ".join(parts)
