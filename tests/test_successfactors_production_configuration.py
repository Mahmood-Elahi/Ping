"""Exercise production preprocessing and scanner orchestration using synthetic HTTP."""
from dataclasses import asdict, replace
from functools import lru_cache

import pytest
import requests

from scripts import validate_successfactors_ten_activation as activation
from scripts.validate_source_integrity_canaries import CanaryBudgetReached
from src.scraper.company_careers.adapters import sap_successfactors as sf
from src.scraper.company_careers.adapters import sap_successfactors_baseline_84b1655 as baseline
from src.scraper.company_careers.adapters import successfactors_recovery_routing as routing
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.source_cooldown import source_health_key
from src.scraper.company_careers.student_roles import configure_student_targets
from tests.successfactors_activation_fixtures import SnapshotSession


@lru_cache(maxsize=1)
def registry():
    return {t.name: t for t in load_target_companies(apply_reliability_repairs=True)}


@pytest.fixture(autouse=True)
def synthetic_transport_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A production-configuration test attempted external HTTP")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "send", forbidden)
    monkeypatch.setattr(sf.time, "sleep", lambda *_: None)


@pytest.mark.parametrize("name", activation.NAMES)
@pytest.mark.parametrize("include_new_grad", [False, True])
def test_actual_dashboard_preprocessing_preserves_certified_source(name, include_new_grad):
    original = registry()[name]
    target = configure_student_targets([original], include_new_grad=include_new_grad)[0]
    assert asdict(target) == asdict(original)
    assert routing.mode(target) == "reviewed"
    assert source_health_key(target) == source_health_key(original)
    assert asdict(configure_student_targets([target])[0]) == asdict(target)


@pytest.mark.parametrize("variant", [None, "rmk_table", "rmk_tile", "rmk_columnized", "unknown"])
@pytest.mark.parametrize("initial", [False, True])
def test_only_columnized_sources_gain_selective_detail_enrichment(variant, initial):
    target = CompanyTarget(name="Fixture", platform="sap_successfactors",
                           endpoint_variant=variant, selective_detail_enrichment=initial)
    configured = configure_student_targets([target])[0]
    assert configured.selective_detail_enrichment is (True if variant == "rmk_columnized" else initial)
    assert target.selective_detail_enrichment is initial
    expected = asdict(target)
    expected["selective_detail_enrichment"] = configured.selective_detail_enrichment
    assert asdict(configured) == expected


@pytest.mark.parametrize("name", activation.NAMES)
@pytest.mark.parametrize("scenario", ["success", "zero", "pagination", "mutation_once",
    "missing_total", "zero_missing_total", "premature_short", "duplicates", "conflicting_id",
    "repeated_page", "malformed", "foreign_apply", "bad_next", "identity", "timeout", "second_mutation"])
def test_production_scanner_retains_inventory_and_absence_gates(name, scenario):
    target = configure_student_targets([registry()[name]])[0]
    session = SnapshotSession(name, scenario)
    report = CompanyCareersScanner(targets=[target], session=session, parent_source_mappings=[]).scan()
    result = report.results[0]
    complete = scenario in {"success", "zero", "pagination", "mutation_once"}
    assert result.scan_complete is complete
    assert result.inventory_complete is complete
    assert result.authoritative_for_absence is complete
    assert bool(report.authoritative_source_scopes()) is complete
    assert not complete or result.fetched_count == {"success": 3, "zero": 0, "pagination": 26, "mutation_once": 26}[scenario]
    assert session.requests_started > 0
    if scenario == "bad_next":
        assert session.requests_started == 1


@pytest.mark.parametrize("name", activation.NAMES)
def test_production_physical_transport_cap_cannot_authorize_absence(name):
    class OneRequestSession(SnapshotSession):
        def get(self, url, **kwargs):
            if self.requests_started >= 1:
                raise CanaryBudgetReached("Synthetic physical transport cap")
            return super().get(url, **kwargs)
    target = configure_student_targets([registry()[name]])[0]
    session = OneRequestSession(name, "pagination")
    report = CompanyCareersScanner(targets=[target], session=session, parent_source_mappings=[]).scan()
    result = report.results[0]
    assert session.requests_started == 1
    assert not result.scan_complete and not result.authoritative_for_absence
    assert report.authoritative_source_scopes() == ()


@pytest.mark.parametrize("name", activation.NAMES)
@pytest.mark.parametrize("field,value", [("identity_regex", ".*"), ("source_scope", "other"),
    ("allowed_apply_hosts", ["unapproved.example"]), ("search_url", "https://unapproved.example/search/"),
    ("selective_detail_enrichment", True)])
def test_preprocessing_does_not_repair_or_hide_a_tampered_contract(name, field, value):
    target = configure_student_targets([replace(registry()[name], **{field: value})])[0]
    session = SnapshotSession(name)
    with pytest.raises(AdapterError, match="profile identity"):
        sf.fetch_jobs(session, target)
    assert session.requests_started == 0
    report = CompanyCareersScanner(targets=[target], session=session, parent_source_mappings=[]).scan()
    assert report.results[0].source_health_code == "identity_failure"
    assert not report.results[0].authoritative_for_absence
    assert not report.results[0].scan_complete
    assert report.authoritative_source_scopes() == ()


@pytest.mark.parametrize("name", activation.BASELINE_COMPANIES)
@pytest.mark.parametrize("scenario", ["success", "zero", "missing_total", "malformed", "foreign_apply", "timeout"])
def test_existing_sources_preserve_old_production_behavior_without_the_unused_flag(name, scenario):
    original = registry()[name]
    old_production = replace(original, selective_detail_enrichment=True)
    fixed_production = configure_student_targets([original])[0]
    assert fixed_production.endpoint_variant == "rmk_table"
    assert not fixed_production.selective_detail_enrichment
    assert routing.mode(fixed_production) == "baseline"
    def run(fn, target):
        session = SnapshotSession("Keyence", scenario)
        try:
            result = fn(session, target)
            return ("ok", [(j.job_id, j.url, j.company) for j in result.jobs], dict(result.scan), session.calls)
        except Exception as exc:
            return ("error", type(exc).__name__, str(exc), session.calls)
    assert run(sf.fetch_jobs, fixed_production) == run(baseline.fetch_jobs, old_production)


def test_all533_production_routes_are_checked_by_the_activation_validator():
    configured = configure_student_targets(list(registry().values()))
    modes = {t.name: routing.mode(t) for t in configured}
    assert sum(mode == "reviewed" for mode in modes.values()) == 10
    assert all(mode == ("reviewed" if name in activation.NAMES else "baseline") for name, mode in modes.items())
    result = activation.validate()
    assert result["production_preprocessing_verified"] is True
    assert result["production_certificates_applicable"] == 10
    assert result["production_parser_counts"] == {"reviewed": 10, "baseline": 523}


def test_exact_student_preprocessing_compatibility_cannot_authorize_other_drift():
    raw = (activation.ROOT / activation.STUDENT_PREPROCESSING).read_bytes()
    assert activation._artifact_matches(activation.STUDENT_PREPROCESSING, raw, activation.STUDENT_BEFORE_SHA256)
    assert not activation._artifact_matches("config/target_companies.yaml", raw, activation.STUDENT_BEFORE_SHA256)
    assert not activation._artifact_matches(activation.STUDENT_PREPROCESSING, raw, "0" * 64)
    assert not activation._artifact_matches(activation.STUDENT_PREPROCESSING, raw + b"\n# drift\n", activation.STUDENT_BEFORE_SHA256)


def test_validator_rejects_the_legacy_production_preprocessing_blind_spot(monkeypatch):
    def legacy_preprocessing(targets):
        return [replace(t, selective_detail_enrichment=True)
                if t.platform == "sap_successfactors" else t for t in targets]
    monkeypatch.setattr(activation, "configure_student_targets", legacy_preprocessing)
    with pytest.raises(ValueError, match="Production-preprocessed target differs from certificate"):
        activation.validate()
