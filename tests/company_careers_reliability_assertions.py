from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from src.scraper.company_careers.models import (
    ADAPTER_SAFETY_BUDGET_DEFAULTS,
    CompanyTarget,
)


ROOT = Path(__file__).resolve().parents[1]
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"

# Historical rollout registries intentionally preserve their original bytes.
# Approved in-place Phase 1 source migrations intentionally differ from the
# immutable historical rollout registries. Prefix assertions compare only these
# named companies to their reviewed production contracts while continuing to
# compare every other entry byte-for-byte after reliability repairs.
APPROVED_SOURCE_MIGRATIONS: dict[str, dict[str, Any]] = {
    "bosch": {
        "name": "Bosch",
        "platform": "smartrecruiters",
        "slug": "BoschGroup",
        "endpoint_variant": "companies_postings_v1",
        "careers_url": "https://jobs.bosch.com/en/",
        "endpoint_url": "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings",
        "identity_regex": "^Bosch Group$",
        "allowed_apply_hosts": ["jobs.smartrecruiters.com"],
        "page_size": 100,
        "max_pages": 50,
        "max_jobs": 5_000,
        "max_response_bytes": 3_000_000,
        "selective_detail_enrichment": False,
    },
    "waymo": {
        "name": "Waymo",
        "platform": "greenhouse",
        "slug": "waymo",
        "endpoint_variant": "boards_api_v1_strict",
        "careers_url": "https://careers.withwaymo.com/jobs/search",
        "search_url": "https://job-boards.greenhouse.io/waymo",
        "original_workbook_rank": 12,
        "allowed_apply_hosts": ["careers.withwaymo.com"],
        "apply_url_template": "https://careers.withwaymo.com/jobs?gh_jid={id}",
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
        "evidence_source": (
            "jobseek apps/crawler/data/boards.csv waymo-greenhouse entry "
            "(local reference only)"
        ),
        "evidence_confidence": "low",
    },
    "ripple": {
        "name": "Ripple",
        "platform": "greenhouse",
        "slug": "ripple",
        "endpoint_variant": "boards_api_v1_strict",
        "careers_url": "https://ripple.com/careers/",
        "search_url": "https://boards.greenhouse.io/ripple",
        "original_workbook_rank": 70,
        "allowed_apply_hosts": ["ripple.com"],
        "apply_url_template": (
            "https://ripple.com/careers/all-jobs/job/{id}?gh_jid={id}"
        ),
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
    },
    "wise": {
        "name": "Wise",
        "platform": "smartrecruiters",
        "slug": "wise",
        "endpoint_variant": "companies_postings_v1",
        "careers_url": "https://wise.jobs/jobs",
        "endpoint_url": "https://api.smartrecruiters.com/v1/companies/wise/postings",
        "identity_regex": "^Wise$",
        "original_workbook_rank": 65,
        "allowed_apply_hosts": ["jobs.smartrecruiters.com"],
        "page_size": 100,
        "max_pages": 10,
        "max_jobs": 1_000,
        "max_response_bytes": 3_000_000,
    },
    "asml": {
        "name": "ASML",
        "platform": "smartrecruiters",
        "slug": "ASML1",
        "endpoint_variant": "companies_postings_v1",
        "careers_url": "https://www.asml.com/en/careers/find-your-job",
        "endpoint_url": "https://api.smartrecruiters.com/v1/companies/ASML1/postings",
        "identity_regex": "^ASML$",
        "allowed_apply_hosts": ["jobs.smartrecruiters.com"],
        "original_workbook_rank": 26,
        "page_size": 100,
        "max_pages": 10,
        "max_jobs": 1_000,
        "max_response_bytes": 3_000_000,
    },
    "sharkninja": {
        "name": "SharkNinja",
        "platform": "greenhouse",
        "slug": "sharkninjaoperatingllc",
        "endpoint_variant": "boards_api_v1_strict",
        "careers_url": "https://careers.sharkninja.com/",
        "search_url": "https://job-boards.greenhouse.io/sharkninjaoperatingllc",
        "allowed_apply_hosts": ["job-boards.greenhouse.io"],
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
    },
    "box": {
        "name": "Box",
        "platform": "greenhouse",
        "slug": "boxinc",
        "endpoint_variant": "boards_api_v1_strict",
        "careers_url": "https://careers.box.com/en/",
        "search_url": "https://job-boards.greenhouse.io/boxinc",
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
        "allowed_apply_hosts": ["job-boards.greenhouse.io"],
    },
    "miro": {
        "name": "Miro",
        "platform": "greenhouse",
        "slug": "realtimeboardglobal",
        "endpoint_variant": "boards_api_v1_strict",
        "careers_url": "https://miro.com/careers/",
        "search_url": "https://job-boards.greenhouse.io/realtimeboardglobal",
        "original_workbook_rank": None,
        "allowed_apply_hosts": ["miro.com"],
        "apply_url_template": "https://miro.com/careers/vacancy/{id}?gh_jid={id}",
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
    },
    "redis": {
        "name": "Redis",
        "platform": "ashby",
        "slug": "redis",
        "careers_url": "https://redis.io/company/careers/current-job-openings/",
        "search_url": "https://jobs.ashbyhq.com/redis",
        "original_workbook_rank": None,
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
        "allowed_apply_hosts": ["jobs.ashbyhq.com"],
    },
}


def with_reliability_repairs(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply validated repairs and explicitly approved in-place migrations."""
    data = yaml.safe_load(REPAIRS.read_text(encoding="utf-8")) or {}
    repairs = data.get("company_careers_reliability_repairs") or []
    by_name = {str(item["name"]).casefold(): item for item in repairs}
    result = []
    for item in entries:
        key = str(item.get("name") or "").casefold()
        migrated = APPROVED_SOURCE_MIGRATIONS.get(key)
        result.append(deepcopy(migrated or by_name.get(key, item)))
    return result


def assert_historical_prefix(
    testcase: Any,
    production: Sequence[Mapping[str, Any]],
    historical: Sequence[Mapping[str, Any]],
) -> None:
    testcase.assertGreaterEqual(len(production), len(historical))
    testcase.assertEqual(
        list(production[: len(historical)]),
        with_reliability_repairs(historical),
    )


def assert_historical_registry_bytes(
    testcase: Any,
    production_bytes: bytes,
    historical_bytes: bytes,
) -> None:
    production = (yaml.safe_load(production_bytes) or {})["target_companies"]
    historical = (yaml.safe_load(historical_bytes) or {})["target_companies"]
    assert_historical_prefix(testcase, production, historical)


def assert_historical_target_prefix(
    testcase: Any,
    production: Sequence[CompanyTarget],
    historical: Sequence[CompanyTarget],
) -> None:
    repaired_items = with_reliability_repairs(
        [vars(target) for target in historical]
    )
    for item in repaired_items:
        defaults = ADAPTER_SAFETY_BUDGET_DEFAULTS.get(str(item.get("platform")))
        if defaults and item.get("name") != "ATCO":
            item.update(defaults)
    repaired = [CompanyTarget.from_dict(item) for item in repaired_items]
    testcase.assertGreaterEqual(len(production), len(historical))
    testcase.assertEqual(list(production[: len(historical)]), repaired)
