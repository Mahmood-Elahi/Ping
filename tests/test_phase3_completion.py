from __future__ import annotations

from pathlib import Path

from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs" / "PHASE3_INVENTORY_HYDRATION_REVIEW.md"
CANARIES = ROOT / "config" / "target_companies.phase3_smartrecruiters_canaries.yaml"


def test_final_review_has_exactly_43_canonical_adapter_dispositions() -> None:
    review = REVIEW.read_text(encoding="utf-8")
    matrix = review.split("## Final canonical 43-adapter matrix", 1)[1].split(
        "## Batch 2 migrations", 1
    )[0]
    rows = [
        line
        for line in matrix.splitlines()
        if line.startswith("|")
        and not line.startswith("|---")
        and "| Adapter |" not in line
    ]
    assert len(rows) == 43
    assert sum("P3-SPLIT" in row for row in rows) == 7
    assert sum("ATOMIC" in row for row in rows) == 29
    assert sum("P3-PARTIAL" in row for row in rows) == 2
    assert sum("COUPLED-BY-DESIGN" in row for row in rows) == 4
    assert sum("PHASE-4" in row for row in rows) == 1


def test_remaining_eleven_provider_families_have_explicit_dispositions() -> None:
    review = REVIEW.read_text(encoding="utf-8")
    section = review.split("## Final review of the remaining eleven", 1)[1].split(
        "## Paginated partial-progress audit", 1
    )[0]
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
        assert f"| {provider} |" in section


def test_phase3_canaries_are_explicit_and_production_mappings_are_unchanged() -> None:
    canaries = {target.name: target for target in load_target_companies(str(CANARIES))}
    production = {target.name: target for target in load_target_companies()}
    assert set(canaries) == {"ServiceNow", "Intuitive Surgical"}
    assert all(target.platform == "smartrecruiters" for target in canaries.values())
    assert all(not target.selective_detail_enrichment for target in canaries.values())
    assert production["ServiceNow"].platform != "smartrecruiters"
    assert production["Intuitive Surgical"].platform != "smartrecruiters"

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
