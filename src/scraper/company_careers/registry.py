"""Load target company registry configuration."""

from __future__ import annotations

from pathlib import Path
from typing import List

import yaml

from src.scraper.company_careers.models import CompanyTarget


DEFAULT_REGISTRY_PATH = "config/target_companies.yaml"


def load_target_companies(path: str = DEFAULT_REGISTRY_PATH) -> List[CompanyTarget]:
    registry_path = Path(path)
    if not registry_path.exists():
        raise FileNotFoundError(f"Company registry not found: {path}")

    with registry_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    entries = data.get("target_companies") or data.get("companies") or []
    return [CompanyTarget.from_dict(entry) for entry in entries]
