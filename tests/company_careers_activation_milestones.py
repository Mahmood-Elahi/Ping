from __future__ import annotations

import hashlib
from pathlib import Path


WAND_B_MAPPING_MARKER = b"  - child_company: Weights & Biases\n"


def historical_parent_mapping_bytes(path: Path) -> bytes:
    """Return the pre-W&B production mapping bytes used by historical reports."""
    current = path.read_bytes().replace(b"\r\n", b"\n")
    marker_count = current.count(WAND_B_MAPPING_MARKER)
    if marker_count != 1:
        raise AssertionError(
            f"expected one W&B activation marker in {path}, found {marker_count}"
        )
    return current[: current.index(WAND_B_MAPPING_MARKER)]


def historical_parent_mapping_sha256(path: Path) -> str:
    return hashlib.sha256(historical_parent_mapping_bytes(path)).hexdigest().upper()
