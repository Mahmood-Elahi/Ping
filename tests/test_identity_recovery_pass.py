from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "config" / "identity_recovery_pass.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
CANDIDATES = ROOT / "config" / "target_companies.identity_recovery_canaries.yaml"
EXPECTED = [
    "Samsung Electronics",
    "GE Vernova",
    "General Motors",
    "Caterpillar",
    "Zalando",
    "Capital One",
    "Cameco",
]


def review() -> dict:
    return yaml.safe_load(REVIEW.read_text(encoding="utf-8"))


def identity() -> dict[str, str]:
    return {
        "evidence_source": "tests/fixtures/example_official_binding.html",
        "official_source_url": "https://careers.example.com/jobs",
        "official_binding_type": "direct_link",
        "official_binding_target_url": (
            "https://example.wd1.myworkdayjobs.com/External"
        ),
        "expected_employer_name": "Example",
        "expected_source_scope": "default",
        "expected_workday_tenant": "example",
        "expected_workday_site": "External",
        "expected_workday_hostname": "example.wd1.myworkdayjobs.com",
        "expected_apply_hostname": "example.wd1.myworkdayjobs.com",
    }


def target(**overrides) -> CompanyTarget:
    values = {
        "name": "Example",
        "platform": "workday",
        "tenant": "example",
        "wd_subdomain": "wd1",
        "site_id": "External",
        "source_scope": "default",
        "allowed_apply_hosts": ["example.wd1.myworkdayjobs.com"],
        "workday_identity": identity(),
    }
    values.update(overrides)
    return CompanyTarget(**values)


def test_exactly_seven_sources_are_reviewed_and_all_remain_fail_identity() -> None:
    data = review()
    rows = data["companies"]
    assert [row["company"] for row in rows] == EXPECTED
    assert {row["final_status"] for row in rows} == {"FAIL_IDENTITY"}
    assert data["report"]["status_counts"]["FAIL_IDENTITY"] == 7
    assert data["report"]["status_counts"]["READY_FOR_CANARY"] == 0
    assert data["report"]["unresolved_production_sources_after_deterministic_pass"] == 36


def test_production_sources_are_unchanged_and_no_speculative_registry_exists() -> None:
    configured = {target.name: target for target in load_target_companies(str(PRODUCTION))}
    expected = {
        "Samsung Electronics": ("workday", "sec", "Samsung_Careers"),
        "GE Vernova": ("workday", "gevernova", "Vernova_ExternalSite"),
        "General Motors": ("static_html", None, None),
        "Caterpillar": ("static_html", None, None),
        "Zalando": ("static_html", None, None),
        "Capital One": ("workday", "capitalone", "Capital_One"),
        "Cameco": ("static_html", None, None),
    }
    assert {
        name: (configured[name].platform, configured[name].tenant, configured[name].site_id)
        for name in EXPECTED
    } == expected
    assert not CANDIDATES.exists()
    assert review()["implementation"]["candidate_rows"] == []


def test_exact_reviewed_workday_identity_contract_validates() -> None:
    status, diagnostic = workday._configured_identity_evidence(
        target(), "External", "example.wd1.myworkdayjobs.com"
    )
    assert status == "validated"
    assert diagnostic["expected_employer_name"] == "Example"
    assert diagnostic["expected_source_scope"] == "default"
    assert diagnostic["expected_apply_hostname"] == (
        "example.wd1.myworkdayjobs.com"
    )


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("expected_workday_tenant", "other"),
        ("expected_workday_site", "Wrong"),
        ("expected_workday_hostname", "other.wd1.myworkdayjobs.com"),
        ("expected_employer_name", "Similar Example Holdings"),
        ("expected_source_scope", "subsidiary"),
        ("expected_apply_hostname", "apply.example.test"),
        ("official_binding_target_url", "https://other.wd1.myworkdayjobs.com/External"),
        ("official_binding_type", "name_similarity"),
    ],
)
def test_workday_identity_mismatches_fail_closed(field: str, wrong_value: str) -> None:
    evidence = deepcopy(identity())
    evidence[field] = wrong_value
    status, diagnostic = workday._configured_identity_evidence(
        target(workday_identity=evidence),
        "External",
        "example.wd1.myworkdayjobs.com",
    )
    assert status == "failed"
    assert diagnostic["exact_binding"] is False


def test_workday_identity_rejects_ats_as_official_careers_source() -> None:
    evidence = deepcopy(identity())
    evidence["official_source_url"] = (
        "https://example.wd1.myworkdayjobs.com/External"
    )
    status, diagnostic = workday._configured_identity_evidence(
        target(workday_identity=evidence),
        "External",
        "example.wd1.myworkdayjobs.com",
    )
    assert status == "failed"
    assert diagnostic["official_source_is_first_party_https"] is False


def test_workday_identity_requires_every_proof_field() -> None:
    for field in identity():
        evidence = deepcopy(identity())
        evidence.pop(field)
        status, diagnostic = workday._configured_identity_evidence(
            target(workday_identity=evidence),
            "External",
            "example.wd1.myworkdayjobs.com",
        )
        assert status == "failed", field
        assert field in diagnostic["missing_fields"]


def test_sibling_catalogs_are_corroboration_and_scope_separations_fail_closed() -> None:
    data = review()["information"]
    assert data["sibling_evidence"]["jobseek"]["authority"] == "corroborating_only"
    assert data["sibling_evidence"]["freehire"]["authority"] == (
        "none_for_employer_or_scope"
    )
    assert data["sibling_evidence"]["JobSpy"]["used_for"] == "none"
    rows = {row["company"]: row for row in review()["companies"]}
    assert "not merged" in rows["Samsung Electronics"]["separation"]
    assert "not merged" in rows["GE Vernova"]["separation"]
    assert "remain distinct" in rows["General Motors"]["separation"]
    assert "remain distinct" in rows["Caterpillar"]["separation"]


def test_workday_completeness_and_mutation_requirements_remain_fail_closed() -> None:
    requirements = review()["implementation"]["candidate_requirements"]
    accounting = requirements["inventory_accounting"]
    assert any("authoritative root total" in item for item in accounting)
    assert any("every direct page or partition leaf" in item for item in accounting)
    assert any("overlap" in item for item in accounting)
    assert "one bounded Workday restart" in requirements["mutation_handling"]
    assert "second" in requirements["mutation_handling"]
    assert "fails closed" in requirements["mutation_handling"]


def test_cameco_static_source_has_no_terminal_contract() -> None:
    configured = {target.name: target for target in load_target_companies(str(PRODUCTION))}
    cameco = configured["Cameco"]
    assert cameco.platform == "static_html"
    assert cameco.pagination_url_template is None
    assert cameco.next_page_selector is None
    assert cameco.empty_selector is None
    assert cameco.single_page_collection is False
    row = next(
        item for item in review()["companies"] if item["company"] == "Cameco"
    )
    assert row["static_terminal_evidence"].startswith("none")


def test_static_terminal_policy_rejects_ambiguous_end_signals() -> None:
    policy = review()["implementation"]["candidate_requirements"][
        "terminal_evidence"
    ]
    rejected = set(policy["rejected_static_signals"])
    assert rejected == {
        "arbitrary short page",
        "arbitrary empty page",
        "repeated page",
        "zero new jobs",
        "stable repeated observation",
        "request-limit exhaustion",
        "hidden or active continuation",
    }
    assert "disabled-next" in policy["static_html"]
    assert "explicit final page/count" in policy["static_html"]


def test_no_request_budget_or_manual_canary_is_invented() -> None:
    implementation = review()["implementation"]
    assert all(
        value["canary_budget"] is None
        for value in implementation["request_budgets"].values()
    )
    assert review()["report"]["manual_canary_commands"] == []
    assert review()["report"]["potential_unresolved_after_all_prepared_canaries_pass"] == 36
