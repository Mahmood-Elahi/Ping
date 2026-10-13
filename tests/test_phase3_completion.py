from __future__ import annotations

from pathlib import Path

from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs" / "ARCHITECTURE.md"
CANARIES = ROOT / "config" / "target_companies.phase3_smartrecruiters_canaries.yaml"


def test_final_review_has_exactly_43_canonical_adapter_dispositions() -> None:
    review = REVIEW.read_text(encoding="utf-8")
    normalized = " ".join(review.split())
    assert "historical 43-boundary Phase 3 review" in review
    assert "29 atomic" in normalized
    assert "seven split" in normalized
    assert "two partial-progress" in normalized
    assert "four coupled-by-design" in normalized
    assert "one Phase 4 handoff" in normalized


def test_remaining_eleven_provider_families_have_explicit_dispositions() -> None:
    review = REVIEW.read_text(encoding="utf-8")
    for provider in (
        "ADP Workforce Now",
        "ADP MyJobs",
        "browser-rendered HTML",
        "Cornerstone CSOD",
        "Dayforce",
        "Deel",
        "iCIMS Jibe",
        "iCIMS Radancy",
        "iCIMS legacy portal",
        "Paycor",
        "PeopleSoft",
        "public JSON variants",
        "SAP SuccessFactors",
        "Workday",
    ):
        assert provider in review


def test_phase3_canaries_are_explicit_and_both_match_later_live_promoted_rows() -> None:
    canaries = {target.name: target for target in load_target_companies(str(CANARIES))}
    production = {target.name: target for target in load_target_companies()}
    assert set(canaries) == {"ServiceNow", "Intuitive Surgical"}
    assert all(target.platform == "smartrecruiters" for target in canaries.values())
    assert all(not target.selective_detail_enrichment for target in canaries.values())
    promoted_fields = (
        "name",
        "source_scope",
        "platform",
        "slug",
        "endpoint_variant",
        "careers_url",
        "endpoint_url",
        "identity_regex",
        "allowed_apply_hosts",
        "page_size",
        "max_pages",
        "max_jobs",
        "max_response_bytes",
        "selective_detail_enrichment",
    )
    assert {
        company: {
            field: getattr(production[company], field)
            for field in promoted_fields
        }
        for company in ("ServiceNow", "Intuitive Surgical")
    } == {
        company: {
            field: getattr(canaries[company], field)
            for field in promoted_fields
        }
        for company in ("ServiceNow", "Intuitive Surgical")
    }
    assert [
        target.source_scope
        for target in load_target_companies()
        if target.name == "Intuitive Surgical"
    ] == ["default"]

    expected = {
        "Waymo": ("greenhouse", "waymo"),
        "ASML": ("smartrecruiters", "ASML1"),
        "SharkNinja": ("greenhouse", "sharkninjaoperatingllc"),
        "Box": ("greenhouse", "boxinc"),
        "Redis": ("ashby", "redis"),
        "Ripple": ("greenhouse", "ripple"),
        "Miro": ("greenhouse", "realtimeboardglobal"),
        "Wise": ("smartrecruiters", "wise"),
    }
    assert {
        name: (production[name].platform, production[name].slug)
        for name in expected
    } == expected
