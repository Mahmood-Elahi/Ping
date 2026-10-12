from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.scraper.company_careers.adapters import smartrecruiters
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "config" / "remaining_source_recovery_pass.yaml"
CANDIDATES = ROOT / "config" / "target_companies.remaining_source_recovery_canaries.yaml"
PRODUCTION = ROOT / "config" / "target_companies.yaml"
SGS_CANDIDATE = ROOT / "config" / "target_companies.pagination_completeness_batch4_canaries.yaml"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> dict[str, dict]:
    return {row["name"]: row for row in _yaml(path)["target_companies"]}


class Response:
    status_code = 200
    content = b"{}"
    text = ""
    headers = {"content-type": "application/json"}

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def _item(index: int, *, company_name: str = "ServiceNow") -> dict:
    job_id = str(744000000000000 + index)
    return {
        "id": job_id,
        "name": f"ServiceNow role {index}",
        "company": {"identifier": "ServiceNow", "name": company_name},
        "releasedDate": "2026-10-12T00:00:00Z",
        "location": {"fullLocation": "Calgary, AB, Canada"},
        "department": {"label": "Engineering"},
        "function": {"label": "Software Engineering"},
        "ref": (
            "https://api.smartrecruiters.com/v1/companies/ServiceNow/postings/"
            + job_id
        ),
    }


class GeneratedSession:
    def __init__(self, total: int):
        self.total = total
        self.calls: list[dict] = []

    def get(self, _url: str, **kwargs) -> Response:
        params = kwargs["params"]
        self.calls.append(params)
        offset = params["offset"]
        limit = params["limit"]
        count = max(0, min(limit, self.total - offset))
        return Response(
            {
                "offset": offset,
                "limit": limit,
                "totalFound": self.total,
                "content": [_item(offset + index) for index in range(count)],
            }
        )


class MovingTotalSession(GeneratedSession):
    def get(self, _url: str, **kwargs) -> Response:
        params = kwargs["params"]
        self.calls.append(params)
        offset = params["offset"]
        total = 697 if offset == 0 else 698
        return Response(
            {
                "offset": offset,
                "limit": params["limit"],
                "totalFound": total,
                "content": [_item(offset + index) for index in range(100)],
            }
        )


def _service_now():
    targets = load_target_companies(str(CANDIDATES))
    assert len(targets) == 1
    return targets[0]


def test_review_has_exact_starting_queue_one_recovery_and_36_unresolved() -> None:
    review = _yaml(REVIEW)
    starting = review["information"]["starting_classification"]
    starting_names = [name for names in starting.values() for name in names]
    final_rows = review["report"]["sources"]

    assert len(starting_names) == len(set(starting_names)) == 37
    assert len(final_rows) == 37
    assert {row["company"] for row in final_rows} == set(starting_names)
    assert review["report"]["status_counts"] == {
        "RECOVERED": 1,
        "READY_FOR_CANARY": 0,
        "RECOVERED_BY_SHARED_FIX": 0,
        "NEEDS_SOURCE_SPECIFIC_REPAIR": 17,
        "INSUFFICIENT_EVIDENCE": 11,
        "FAIL_IDENTITY": 7,
        "FAIL_CONTRACT": 1,
        "NO_SAFE_REPAIR_OR_OTHER": 0,
    }
    assert review["report"]["ready_for_canary"] == []
    assert review["report"]["newly_recovered"] == ["ServiceNow"]
    assert (
        review["report"]["original_unresolved_production_sources_before_servicenow_live_validation"]
        == 37
    )
    assert review["report"]["unresolved_production_sources_after_pass"] == 36
    assert len(review["report"]["parked_until_new_provider_evidence"]) == 36
    assert "ServiceNow" not in review["report"]["parked_until_new_provider_evidence"]


def test_sgs_closeout_matches_exact_tested_candidate_and_operator_evidence() -> None:
    review = _yaml(REVIEW)["implementation"]["sgs_closeout"]
    production = _rows(PRODUCTION)["SGS"]
    candidate = _rows(SGS_CANDIDATE)["SGS"]

    assert production == candidate
    assert review["status"] == "RECOVERED"
    assert review["evidence_state"] == "LIVE_VALIDATED"
    assert review["validation_status"] == "PASS_COMPLETE"
    assert review["production_before_exact_match"] is False
    assert review["production_now_exactly_matches_tested_candidate"] is True
    assert review["canary_rerun"] is False
    assert review["requests"] == {
        "started": 47,
        "budget": 50,
        "remaining_configured_capacity_jobs": 354,
    }


def test_servicenow_production_exactly_matches_live_tested_candidate() -> None:
    candidate = _service_now()
    candidate_row = _rows(CANDIDATES)["ServiceNow"]
    production = _rows(PRODUCTION)["ServiceNow"]

    assert candidate.name == "ServiceNow"
    assert candidate.platform == "smartrecruiters"
    assert candidate.slug == "ServiceNow"
    assert candidate.identity_regex == "^ServiceNow$"
    assert candidate.allowed_apply_hosts == ["jobs.smartrecruiters.com"]
    assert (candidate.page_size, candidate.max_pages, candidate.max_jobs) == (100, 8, 800)
    assert candidate.max_response_bytes == 3_000_000
    assert candidate.selective_detail_enrichment is False
    assert production == candidate_row


def test_servicenow_live_697_shape_completes_in_seven_listing_requests(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    session = GeneratedSession(697)
    result = smartrecruiters.fetch_jobs(session, _service_now())

    assert len(session.calls) == 7
    assert len(result.inventory_items) == len(result.jobs) == 697
    assert result.item_failures == ()
    assert result.scan["scan_complete"] is True
    assert result.scan["terminal_evidence_type"] == "authoritative_total"
    assert result.scan["terminal_evidence_value"] == 697
    assert result.scan["source_identity_status"] == "validated"
    assert result.scan["detail_requests"] == 0
    assert all(job.url == f"https://jobs.smartrecruiters.com/ServiceNow/{job.job_id}" for job in result.jobs)


def test_servicenow_total_above_bound_fails_incomplete_without_truncation_authority(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    session = GeneratedSession(801)
    result = smartrecruiters.fetch_jobs(session, _service_now())

    assert len(session.calls) == 8
    assert len(result.inventory_items) == 800
    assert result.scan["scan_complete"] is False
    assert result.scan["safety_limit_reached"] is True
    assert result.scan["stop_reason"] == "maximum_results_reached"


def test_servicenow_total_movement_fails_after_bounded_restart(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    session = MovingTotalSession(697)
    result = smartrecruiters.fetch_jobs(session, _service_now())

    assert len(session.calls) == 4
    assert result.scan["scan_complete"] is False
    assert result.scan["collection_changed_during_scan"] is True
    assert result.scan["collection_retry_exhausted"] is True
    assert result.scan["stop_reason"] == "collection_changed_during_scan"


def test_servicenow_duplicate_and_identity_mismatch_fail_closed() -> None:
    duplicate = _item(1)
    duplicate_page = Response(
        {"offset": 0, "limit": 100, "totalFound": 2, "content": [duplicate, duplicate]}
    )
    wrong_identity_page = Response(
        {
            "offset": 0,
            "limit": 100,
            "totalFound": 1,
            "content": [_item(1, company_name="Not ServiceNow")],
        }
    )

    class OnePage:
        def __init__(self, response: Response):
            self.response = response

        def get(self, _url: str, **_kwargs) -> Response:
            return self.response

    with pytest.raises(AdapterError, match="repeated stable job ID"):
        smartrecruiters.fetch_jobs(OnePage(duplicate_page), _service_now())
    with pytest.raises(AdapterError, match="mixed or ambiguous company identity"):
        smartrecruiters.fetch_jobs(OnePage(wrong_identity_page), _service_now())


def test_servicenow_closeout_records_exact_operator_evidence_without_rerun() -> None:
    review = _yaml(REVIEW)
    candidate = review["implementation"]["candidates"][0]

    assert candidate["company"] == "ServiceNow"
    assert candidate["status"] == "RECOVERED"
    assert candidate["evidence_state"] == "LIVE_VALIDATED"
    assert candidate["validation_status"] == "PASS_COMPLETE"
    assert candidate["production_activated"] is True
    assert candidate["production_now_exactly_matches_tested_candidate"] is True
    assert candidate["candidate_registry_preserved_as_historical_live_validation_evidence"] is True
    assert candidate["canary_rerun_during_closeout"] is False
    assert candidate["live_authoritative_total"] == 697
    assert candidate["requests_started"] == 7
    assert candidate["request_budget"] == 8
    assert candidate["remaining_configured_capacity_jobs"] == 103
    assert candidate["new_attempt"] == {
        "mode": "inventory-only SmartRecruiters",
        "selective_detail_enrichment": False,
        "jobs_collected": 697,
        "total_available": 697,
        "listing_requests": 7,
        "outcome": "PASS_COMPLETE",
    }
    live = candidate["live_validation"]
    assert live["source_scope_key"] == "servicenow::default"
    assert live["inventory_complete"] is live["scan_complete"] is True
    assert live["source_health_code"] == "healthy"
    assert live["terminal_evidence_type"] == "authoritative_total"
    assert live["terminal_evidence_value"] == 697
    assert live["request_budget_respected"] is True
    assert live["collection_changed_during_scan"] is False
    assert live["unreadable_items"] == live["rejected_items"] == 0
    assert live["lifecycle_mutations"] == 0
    assert live["persistence_mode"] == "none"
    assert review["goal"]["live_network_work_performed"] is False
