#!/usr/bin/env python3
"""Validate the scheduled production job without contacting external systems."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_source_integrity import validate_source_integrity
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_integrity import IntegrityAction


REQUIRED_ENVIRONMENT = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "GOOGLE_SHEET_ID",
    "GOOGLE_CREDENTIALS_PATH",
)
EXPECTED_SOURCE_COUNT = 533
EXPECTED_PARENT_MAPPING_COUNT = 5


def main() -> int:
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Required production configuration is incomplete")

    credentials_path = Path(os.environ["GOOGLE_CREDENTIALS_PATH"])
    if not credentials_path.is_file() or credentials_path.stat().st_size == 0:
        raise RuntimeError("The temporary Google credentials file is unavailable")
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    if not isinstance(credentials, dict):
        raise RuntimeError("The temporary Google credentials payload is invalid")

    integrity = validate_source_integrity(
        retained_pattern="__no_ci_retained_coverage__"
    )
    if integrity["summary"][IntegrityAction.ARCHITECTURAL.value]:
        return 2

    companies = load_target_companies()
    mappings = load_parent_source_mappings()
    if len(companies) != EXPECTED_SOURCE_COUNT:
        raise RuntimeError("Production registry count mismatch")
    if len(mappings) != EXPECTED_PARENT_MAPPING_COUNT:
        raise RuntimeError("Parent mapping count mismatch")

    # This detail is intentionally captured by run_public_ci_step.py rather
    # than published. The outer boundary emits the public success message.
    print("Production configuration and source-integrity preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
