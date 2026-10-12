from __future__ import annotations

from math import ceil
from pathlib import Path

import pytest
import yaml

from src.scraper.company_careers.adapters import smartrecruiters
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "config" / "pagination_completeness_recovery_batch3.yaml"
CANDIDATES = (
    ROOT / "config" / "target_companies.pagination_completeness_batch3_canaries.yaml"
)
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PLAYBOOK = ROOT / "docs" / "PAGINATION_COMPLETENESS_RECOVERY_BATCH1.md"

EXPECTED = [
    "Meta",
    "Rivian",
    "Goldman Sachs",
    "Stantec",
    "Qualcomm",
    "JPMorgan Chase",
    "Emerson",
    "Honeywell",
    "Oracle",
    "Fortinet",
    "Bosch",
    "CPKC",
    "Scotiabank",
    "Bombardier",
    "Micron",
    "GlobalFoundries",
    "ecobee",
    "Espressif Systems",
]


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> dict[str, dict]:
    return {row["name"]: row for row in _yaml(path)["target_companies"]}


class Response:
    content = b"{}"
    text = ""
    headers = {"content-type": "application/json"}
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class GeneratedBoschSession:
    def __init__(self, total: int):
        self.total = total
        self.calls: list[dict] = []

    def get(self, _url: str, **kwargs) -> Response:
        params = kwargs["params"]
        self.calls.append(params)
        offset = params["offset"]
        limit = params["limit"]
        count = max(0, min(limit, self.total - offset))
        content = [_bosch_item(offset + index) for index in range(count)]
        return Response(
            {
                "offset": offset,
                "limit": limit,
                "totalFound": self.total,
                "content": content,
            }
        )


class SequenceSession:
    def __init__(self, responses: list[Response]):
        self.responses = iter(responses)

    def get(self, _url: str, **_kwargs) -> Response:
        return next(self.responses)


def _bosch_item(index: int) -> dict:
    job_id = str(744000000000000 + index)
    return {
        "id": job_id,
        "name": f"Bosch role {index}",
        "company": {"identifier": "BoschGroup", "name": "Bosch Group"},
        "releasedDate": "2026-10-05T00:00:00Z",
        "location": {"fullLocation": "Calgary, AB, Canada"},
        "department": {"label": "Engineering"},
        "function": {"label": "Engineering"},
        "ref": (
            "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings/"
            + job_id
        ),
    }


def _page(start: int, count: int, total: int) -> Response:
    return Response(
        {
            "offset": start,
            "limit": 100,
            "totalFound": total,
            "content": [_bosch_item(start + index) for index in range(count)],
        }
    )


def _candidate() -> CompanyTarget:
    return load_target_companies(str(CANDIDATES))[0]


def test_batch3_scope_groups_and_statuses_are_exact() -> None:
    review = _yaml(REVIEW)
    rows = review["companies"]
    grouped = [name for names in review["adapter_groups"].values() for name in names]

    assert review["repository_head_reviewed"] == "7aca829"
    assert review["batch2_checkpoint"]["present"] is True
    assert review["scope"]["targets"] == EXPECTED
    assert [row["company"] for row in rows] == EXPECTED
    assert len(grouped) == len(set(grouped)) == 18
    assert set(grouped) == set(EXPECTED)
    assert review["status_counts"] == {
        "RECOVERED": 1,
        "READY_FOR_CANARY": 0,
        "RECOVERED_BY_SHARED_FIX": 0,
        "NEEDS_SOURCE_SPECIFIC_REPAIR": 9,
        "FAIL_IDENTITY": 0,
        "FAIL_CONTRACT": 0,
        "FAIL_TRANSIENT": 0,
        "UNSUPPORTED_PROVIDER": 0,
        "NO_SAFE_REPAIR": 0,
        "INSUFFICIENT_EVIDENCE": 8,
    }
    assert review["recovered"] == ["Bosch"]
    assert review["live_validated"] == ["Bosch"]
    assert review["ready_for_canary"] == []
    assert review["recovered_by_shared_fix"] == []


def test_production_bosch_exactly_matches_live_tested_candidate() -> None:
    candidate_rows = _rows(CANDIDATES)
    production_rows = _rows(PRODUCTION)

    assert set(candidate_rows) == {"Bosch"}
    assert candidate_rows["Bosch"]["page_size"] == 100
    assert candidate_rows["Bosch"]["max_pages"] == 50
    assert candidate_rows["Bosch"]["max_jobs"] == 5_000
    assert candidate_rows["Bosch"]["selective_detail_enrichment"] is False
    assert production_rows["Bosch"] == candidate_rows["Bosch"]
    assert production_rows["Bosch"]["max_response_bytes"] == 3_000_000
    assert production_rows["Bosch"]["identity_regex"] == "^Bosch Group$"
    assert production_rows["Bosch"]["allowed_apply_hosts"] == [
        "jobs.smartrecruiters.com"
    ]
    assert _yaml(REVIEW)["production_safety"]["production_registry_modified"] is True


def test_bosch_bound_is_derived_from_stored_total_and_canary_is_exact() -> None:
    review = _yaml(REVIEW)
    bound = review["bosch_candidate"]["bound_derivation"]
    canary = review["historical_manual_canaries"][0]

    assert bound["observed_high_total"] == 4_835
    assert bound["exact_pages_at_observed_high"] == ceil(4_835 / 100) == 49
    assert bound["candidate_max_pages"] == 50
    assert bound["candidate_max_jobs"] == 5_000
    assert bound["bounded_headroom_jobs"] == 165
    assert bound["hard_canary_request_budget"] == 50
    assert canary["company"] == "Bosch"
    assert canary["request_budget"] == 50
    assert canary["registry"].endswith("batch3_canaries.yaml")
    assert canary["command"].endswith("--request-budget 50 --run")
    assert canary["persistence"] is False
    assert canary["lifecycle_mutation"] is False


def test_operator_canary_closes_live_validation_and_activation_without_rerun() -> None:
    review = _yaml(REVIEW)
    bosch = review["bosch_candidate"]
    live = bosch["live_validation"]

    assert bosch["status"] == "RECOVERED"
    assert bosch["evidence_state"] == "LIVE_VALIDATED"
    assert bosch["validation_status"] == "PASS_COMPLETE"
    assert live["performed_by"] == "operator"
    assert live["rerun_by_repository_closeout"] is False
    assert live["jobs_collected"] == live["inventory_items_collected"] == 4_950
    assert live["total_available"] == live["terminal_evidence_value"] == 4_950
    assert live["unique_stable_ids"] == live["unique_canonical_urls"] == 4_950
    assert live["pages_requested"] == live["requests_started"] == live["request_budget"] == 50
    assert live["request_budget_respected"] is True
    assert live["collection_changed_during_scan"] is False
    assert live["hidden_continuation_absent"] is True
    assert live["safety_limit_reached"] is False
    assert live["unreadable_items"] == live["rejected_items"] == 0
    assert live["duplicate_ids"] == live["duplicate_canonical_urls"] == []
    assert live["lifecycle_mutations"] == 0
    assert live["persistence_mode"] == "none"
    assert live["dashboard_published"] is False
    assert live["error"] is None
    assert bosch["activation"]["identity_and_pagination_semantics_changed_from_candidate"] is False


def test_bosch_capacity_note_and_overflow_policy_are_locked() -> None:
    maintenance = _yaml(REVIEW)["bosch_candidate"]["maintenance"]

    assert maintenance == {
        "validation_ceiling_jobs": 5_000,
        "live_authoritative_total": 4_950,
        "remaining_capacity_jobs": 50,
        "current_failure": False,
        "overflow_policy": "fail_incomplete_without_truncation",
    }


def test_bosch_stable_authoritative_total_completes_at_49_pages(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    session = GeneratedBoschSession(4_835)

    result = smartrecruiters.fetch_jobs(session, _candidate())

    assert len(session.calls) == 49
    assert len(result.inventory_items) == 4_835
    assert len(result.jobs) == 4_835
    assert result.scan["scan_complete"] is True
    assert result.scan["stop_reason"] == "reported_total_reached"
    assert result.scan["terminal_evidence_type"] == "authoritative_total"
    assert result.scan["terminal_evidence_value"] == 4_835
    assert result.scan["collection_changed_during_scan"] is False
    assert result.scan["safety_limit_reached"] is False


def test_bosch_candidate_cap_before_total_remains_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    session = GeneratedBoschSession(5_001)

    result = smartrecruiters.fetch_jobs(session, _candidate())

    assert len(session.calls) == 50
    assert len(result.inventory_items) == 5_000
    assert result.scan["scan_complete"] is False
    assert result.scan["safety_limit_reached"] is True
    assert result.scan["stop_reason"] == "maximum_results_reached"


def test_bosch_total_movement_retries_once_then_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    responses = [
        _page(0, 100, 150),
        _page(100, 50, 151),
        _page(0, 100, 150),
        _page(100, 50, 151),
    ]
    target = CompanyTarget.from_dict(
        {
            **_candidate().__dict__,
            "max_pages": 2,
            "max_jobs": 200,
        }
    )

    result = smartrecruiters.fetch_jobs(SequenceSession(responses), target)

    assert len(result.jobs) == 100
    assert result.scan["scan_complete"] is False
    assert result.scan["collection_changed_during_scan"] is True
    assert result.scan["collection_retry_exhausted"] is True
    assert result.scan["stop_reason"] == "collection_changed_during_scan"


def test_smartrecruiters_ceiling_is_narrow_and_config_opt_in_is_required() -> None:
    assert smartrecruiters.MAX_PAGES == 50
    assert smartrecruiters.MAX_RESULTS == 5_000
    smartrecruiters.validate_target(_candidate())

    too_large = CompanyTarget.from_dict(
        {**_candidate().__dict__, "max_pages": 51, "max_jobs": 5_001}
    )
    with pytest.raises(AdapterError, match="max_pages cannot exceed 50"):
        smartrecruiters.validate_target(too_large)


def test_rejected_tracks_record_the_exact_missing_authority() -> None:
    review = _yaml(REVIEW)

    assert review["alternate_first_party_inventories"]["Meta"]["shape_evidence"].startswith(
        "freehire models a flat urlset"
    )
    assert review["alternate_first_party_inventories"]["Goldman Sachs"][
        "required_experiences"
    ] == ["EARLY_CAREER", "PROFESSIONAL"]
    assert review["oracle_partition_review"]["generic_repair_implemented"] is False
    assert all(
        row["partition_count"] == "unknown"
        for row in review["oracle_partition_review"]["targets"]
    )
    assert review["successfactors_review"]["generic_alternate_inventory_implemented"] is False
    assert review["alternate_ats_migrations"]["Micron"]["decision"] == "not_proposed"
    assert review["alternate_ats_migrations"]["GlobalFoundries"]["decision"] == "not_proposed"
    assert review["static_terminal_review"]["ecobee"]["accepted_terminal_patterns"] == []
    assert review["static_terminal_review"]["Espressif Systems"][
        "accepted_terminal_patterns"
    ] == []


def test_pass_gates_accounting_rollback_and_batch4_are_durable() -> None:
    review = _yaml(REVIEW)
    gates = set(review["pass_complete_gates"])

    assert {
        "canonical_employer_identity_validated",
        "exact_provider_source_validated",
        "source_scope_correct",
        "complete_provider_inventory",
        "explicit_terminal_evidence_valid",
        "collection_mutation_absent",
        "request_budget_respected",
        "stable_provider_ids_accounted",
        "unreadable_identities_protected",
        "central_finalizer_authoritative",
        "contradictory_diagnostics_absent",
    }.issubset(gates)
    assert review["inventory_accounting_contract"]["silent_loss_allowed"] is False
    assert review["mutation_contract"]["outcome"]["absence_authorized"] is False
    assert review["rollback"]["Bosch"]["production_action"].startswith(
        "restore only the prior Bosch production limits"
    )
    assert review["rollback"]["source_migrations"] == "none_proposed"
    assert review["batch4_recommendation"]["exact_composition_count"] == 13
    assert len(review["batch4_recommendation"]["core_targets"]) == 10
    assert review["batch4_recommendation"]["exclusions"]["identity_batch"] == [
        "Samsung Electronics",
        "GE Vernova",
        "General Motors",
        "Caterpillar",
        "Zalando",
        "Capital One",
    ]


def test_playbook_contains_batch3_acceptance_rules_and_no_false_recovery() -> None:
    text = PLAYBOOK.read_text(encoding="utf-8")

    assert "## Batch 3 deterministic closeout" in text
    assert "Accepted candidate: Bosch bound-only repair" in text
    assert "Accept a first-party sitemap for absence only" in text
    assert "GraphQL total authorizes absence only" in text
    assert "Oracle partitioning requires an authoritative finite facet universe" in text
    assert "SuccessFactors RSS or sitemap" in text
    assert "Migration from an unstable ATS" in text
    assert "Static completion needs a provider-visible final page" in text
    assert "deterministic `READY_FOR_CANARY`" in text
    assert "live `PASS_COMPLETE`" in text
    assert "production activation" in text
    assert "50 jobs of remaining capacity" in text
