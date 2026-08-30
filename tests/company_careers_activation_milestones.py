from __future__ import annotations

from pathlib import Path

from tests.text_bytes import canonical_text_bytes, canonical_text_sha256


WAND_B_MAPPING_MARKER = b"  - child_company: Weights & Biases\n"


def historical_parent_mapping_projection(data: bytes, *, source: object) -> bytes:
    """Project mapping text to the immutable pre-W&B rollout boundary."""
    canonical = canonical_text_bytes(data)
    marker_count = canonical.count(WAND_B_MAPPING_MARKER)
    if marker_count != 1:
        raise AssertionError(
            f"expected one W&B activation marker in {source}, found {marker_count}"
        )
    return canonical[: canonical.index(WAND_B_MAPPING_MARKER)]


def historical_parent_mapping_bytes(path: Path) -> bytes:
    """Return the pre-W&B production mapping bytes used by historical reports."""
    return historical_parent_mapping_projection(path.read_bytes(), source=path)


def historical_parent_mapping_sha256(path: Path) -> str:
    return canonical_text_sha256(historical_parent_mapping_bytes(path))
