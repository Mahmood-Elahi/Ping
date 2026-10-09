from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import validate_source_integrity_canaries as canaries
from src.scraper.company_careers.adapters import (
    ashby,
    greenhouse,
    jobvite,
    smartrecruiters,
    workday,
)
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.registry import DEFAULT_REGISTRY_PATH, load_target_companies
from src.scraper.company_careers.scanner import ADAPTERS


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = ROOT / "config" / "target_companies.phase1_ats_candidates.yaml"
ROLLBACK_PATH = ROOT / "config" / "target_companies.phase1_ats_rollback.yaml"
PRODUCTION_PATH = ROOT / "config" / "target_companies.yaml"
REVIEW_PATH = ROOT / "docs" / "PHASE1_ATS_MIGRATION_REVIEW.md"

EXPECTED_CANDIDATES: dict[str, tuple[str, str | None]] = {}

NEW_BATCH_COMPANIES = {
    "Bluesky",
    "Siemens",
    "IMC Trading",
    "Valve",
    "LINE",
    "Tourmaline Oil",
    "Nutrien",
    "YYC Calgary Airport Authority",
    "TEKTELIC Communications",
    "Espressif Systems",
    "Dyson",
    "Mitsubishi Electric",
    "Computer Modelling Group (CMG)",
    "Symend",
    "ecobee",
    "Ouster",
    "CATL",
    "DJI",
    "Athabasca Oil",
    "u-blox",
    "Zoom",
    "H2O.ai",
}

NON_RUNNABLE_BATCH_COMPANIES = {
    "General Motors",
    "Caterpillar",
    "Applied Materials",
    "BlackRock",
    "Chevron",
    "Zalando",
    "Coupang",
    "Continental",
    "Snap",
    "Goldman Sachs",
    "Unitree Robotics",
    "MediaTek",
    "Stantec",
    "Yaskawa Electric",
    "Imperial Oil",
    "Siemens",
    "Nutrien",
    "YYC Calgary Airport Authority",
    "Symend",
    "Ouster",
    "H2O.ai",
    "u-blox",
}

ACTIVATED_PRODUCTION = {
    "ASML": ("smartrecruiters", "ASML1"),
    "SharkNinja": ("greenhouse", "sharkninjaoperatingllc"),
    "Box": ("greenhouse", "boxinc"),
    "Redis": ("ashby", "redis"),
    "Ripple": ("greenhouse", "ripple"),
    "Miro": ("greenhouse", "realtimeboardglobal"),
    "Wise": ("smartrecruiters", "wise"),
}

CUSTOM_GREENHOUSE_PRODUCTION = {
    "Ripple": (
        "ripple.com",
        "https://ripple.com/careers/all-jobs/job/{id}?gh_jid={id}",
        "8225681",
        "https://ripple.com/careers/all-jobs/job/8225681?gh_jid=8225681",
    ),
    "Miro": (
        "miro.com",
        "https://miro.com/careers/vacancy/{id}?gh_jid={id}",
        "8805635002",
        "https://miro.com/careers/vacancy/8805635002?gh_jid=8805635002",
    ),
}

ROLLBACK_COMPANIES = {
    "Applied Materials",
    "Ripple",
    "General Motors",
    "Caterpillar",
    "Etsy",
    "Zalando",
    "Wise",
    "BlackRock",
    "Chevron",
    "ASML",
    "Sanctuary AI",
    "SharkNinja",
    "ServiceNow",
    "Lightspeed Commerce",
    "Box",
    "Miro",
    "Redis",
    "Cameco",
    "Intuitive Surgical",
    "Siemens",
    "Nutrien",
    "Symend",
    "Ouster",
    "u-blox",
    "H2O.ai",
    "YYC Calgary Airport Authority",
}


def _entries(path: Path) -> list[dict[str, object]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["target_companies"]


def test_phase1_candidate_registry_is_explicit_and_complete() -> None:
    assert DEFAULT_REGISTRY_PATH == "config/target_companies.yaml"
    assert Path(DEFAULT_REGISTRY_PATH).name != CANDIDATE_PATH.name

    targets = load_target_companies(str(CANDIDATE_PATH))
    actual = {
        target.name: (
            target.platform,
            target.slug or target.site_id,
        )
        for target in targets
    }
    assert actual == EXPECTED_CANDIDATES
    assert all(target.source_activation == "active" for target in targets)
    assert all(target.platform in ADAPTERS for target in targets)


def test_closed_previous_batch_candidates_are_not_runnable() -> None:
    candidate_names = {entry["name"] for entry in _entries(CANDIDATE_PATH)}
    assert candidate_names.isdisjoint(
        {"Cameco", "Lightspeed Commerce", "Intuitive Surgical"}
    )

    production = {entry["name"]: entry for entry in _entries(PRODUCTION_PATH)}
    assert production["Cameco"]["platform"] == "static_html"
    assert production["Lightspeed Commerce"]["platform"] == "static_html"
    assert production["Intuitive Surgical"]["platform"] == "static_html"


def test_phase1_review_tracks_closure_and_every_new_batch_company() -> None:
    review = REVIEW_PATH.read_text(encoding="utf-8")
    assert "Companies reviewed: 22." in review
    assert "Class A/B runnable candidates: 0." in review
    assert "exactly 772 requests" in review
    assert "is now intentionally" in review
    assert "empty. There are no exact runnable entries remaining" in review
    for name in NEW_BATCH_COMPANIES:
        assert f"| {name} |" in review


def test_phase1_candidates_satisfy_provider_specific_offline_validation() -> None:
    targets = load_target_companies(str(CANDIDATE_PATH))
    for target in targets:
        if target.platform == "smartrecruiters":
            smartrecruiters.validate_target(target)
        elif target.platform == "ashby":
            ashby._validate_target(target)
        elif target.platform == "workday":
            assert workday._candidate_site_ids(target)
        elif target.platform == "greenhouse":
            assert target.endpoint_variant == "boards_api_v1_strict"
            assert target.allowed_apply_hosts
            assert target.apply_url_template is None
        elif target.platform == "jobvite":
            jobvite.validate_target(target)


def test_phase1_candidates_are_independently_addressable_and_isolated() -> None:
    candidate_names = set(EXPECTED_CANDIDATES)
    assert candidate_names.isdisjoint(NON_RUNNABLE_BATCH_COMPANIES)
    for name in sorted(candidate_names):
        selected = canaries.selected_targets([name], registry=CANDIDATE_PATH)
        assert [target.name for target in selected] == [name]


def test_phase1_rollback_snapshot_matches_current_production_sources() -> None:
    production = {entry["name"]: entry for entry in _entries(PRODUCTION_PATH)}
    rollback = {entry["name"]: entry for entry in _entries(ROLLBACK_PATH)}
    assert set(rollback) == ROLLBACK_COMPANIES
    unchanged = ROLLBACK_COMPANIES - set(ACTIVATED_PRODUCTION)
    assert {name: rollback[name] for name in unchanged} == {
        name: production[name] for name in unchanged
    }
    for name in ACTIVATED_PRODUCTION:
        assert rollback[name] != production[name]


def test_activated_class_a_sources_use_exact_production_adapters() -> None:
    for apply_repairs in (False, True):
        targets = {
            target.name: target
            for target in load_target_companies(
                apply_reliability_repairs=apply_repairs,
            )
        }
        for name, expected in ACTIVATED_PRODUCTION.items():
            target = targets[name]
            assert (target.platform, target.slug) == expected
            assert ADAPTERS[target.platform]
        assert targets["ASML"].endpoint_url == (
            "https://api.smartrecruiters.com/v1/companies/ASML1/postings"
        )
        assert targets["SharkNinja"].allowed_apply_hosts == ["job-boards.greenhouse.io"]
        assert targets["Box"].allowed_apply_hosts == ["job-boards.greenhouse.io"]
        assert targets["Redis"].search_url == "https://jobs.ashbyhq.com/redis"
        assert targets["Wise"].endpoint_url == (
            "https://api.smartrecruiters.com/v1/companies/wise/postings"
        )
        assert targets["Wise"].allowed_apply_hosts == ["jobs.smartrecruiters.com"]


def test_custom_greenhouse_production_requires_exact_path_and_query_ids() -> None:
    targets = {target.name: target for target in load_target_companies()}
    for name, (_host, _template, job_id, url) in CUSTOM_GREENHOUSE_PRODUCTION.items():
        target = targets[name]
        contract = greenhouse._apply_url_contract(target)
        item = {
            "id": job_id,
            "title": f"{name} role",
            "location": {"name": "Remote"},
            "absolute_url": url,
        }
        assert greenhouse._listing_record(item, target, {}, contract).url == url
        bad_item = {**item, "absolute_url": url.replace(f"/{job_id}?", "/999?")}
        with pytest.raises(AdapterError, match="exact board and stable ID"):
            greenhouse._listing_record(bad_item, target, {}, contract)


def test_reviewed_high_volume_smartrecruiters_budgets_are_exact_and_scoped() -> None:
    assert canaries.smartrecruiters_canary_request_budget(403) == 5
    assert canaries.smartrecruiters_canary_request_budget(708) == 8
    wise = SimpleNamespace(name="Wise", platform="smartrecruiters")
    service_now = SimpleNamespace(name="ServiceNow", platform="smartrecruiters")
    canaries.validate_request_budget(5, [wise])
    canaries.validate_request_budget(8, [service_now])
    with pytest.raises(ValueError, match="between 1 and 52"):
        canaries.validate_request_budget(53, [service_now])


def test_phase1_rejected_replacements_remain_on_first_party_sources() -> None:
    production = {entry["name"]: entry for entry in _entries(PRODUCTION_PATH)}
    assert production["Aurora Innovation"]["platform"] == "public_json"
    assert production["Optiver"]["platform"] == "public_json"
    assert production["Bolt"]["platform"] == "javascript_embedded_data"
    assert production["Roblox"]["platform"] == "public_json"
    assert production["Deliveroo"]["platform"] == "public_json"
    assert production["Etsy"]["platform"] == "static_html"
    assert production["Sanctuary AI"]["platform"] == "javascript_embedded_data"
    assert production["ServiceNow"]["platform"] == "static_html"
