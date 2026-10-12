from __future__ import annotations

import hashlib
from math import ceil
from pathlib import Path

import yaml

from src.scraper.company_careers.adapters import smartrecruiters
from src.scraper.company_careers.registry import load_target_companies


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "config" / "pagination_completeness_recovery_batch4.yaml"
CANDIDATES = (
    ROOT / "config" / "target_companies.pagination_completeness_batch4_canaries.yaml"
)
PRODUCTION = ROOT / "config" / "target_companies.yaml"
EXPECTED = [
    "Nordic Semiconductor",
    "CN Rail",
    "Athabasca Oil",
    "HP Inc.",
    "Seagate",
    "SGS",
    "AstraZeneca",
    "Kinross Gold",
    "Cameco",
    "Nutrien",
]


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> dict[str, dict]:
    return {row["name"]: row for row in _yaml(path)["target_companies"]}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Response:
    content = b"{}"
    text = ""
    headers = {"content-type": "application/json"}
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class GeneratedSgsSession:
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
                "content": [_sgs_item(offset + index) for index in range(count)],
            }
        )


def _sgs_item(index: int) -> dict:
    job_id = str(744000000000000 + index)
    return {
        "id": job_id,
        "name": f"SGS role {index}",
        "company": {"identifier": "SGS", "name": "SGS"},
        "releasedDate": "2026-10-05T00:00:00Z",
        "location": {"fullLocation": "Calgary, AB, Canada"},
        "department": {"label": "Engineering"},
        "function": {"label": "Engineering"},
        "ref": f"https://api.smartrecruiters.com/v1/companies/SGS/postings/{job_id}",
    }


def _candidate():
    return load_target_companies(str(CANDIDATES))[0]


def test_batch4_scope_statuses_and_gated_lane_are_exact() -> None:
    review = _yaml(REVIEW)
    rows = review["companies"]
    grouped = [name for names in review["adapter_groups"].values() for name in names]

    assert review["scope"]["core_targets"] == EXPECTED
    assert [row["company"] for row in rows] == EXPECTED
    assert len(grouped) == len(set(grouped)) == 10
    assert set(grouped) == set(EXPECTED)
    assert review["scope"]["gated_prior_stragglers_admitted"] == []
    assert review["status_counts"] == {
        "READY_FOR_CANARY": 1,
        "RECOVERED_BY_SHARED_FIX": 0,
        "NEEDS_SOURCE_SPECIFIC_REPAIR": 5,
        "FAIL_IDENTITY": 1,
        "FAIL_CONTRACT": 0,
        "FAIL_TRANSIENT": 0,
        "UNSUPPORTED_PROVIDER": 0,
        "NO_SAFE_REPAIR": 0,
        "INSUFFICIENT_EVIDENCE": 3,
    }
    assert review["ready_for_canary"] == ["SGS"]


def test_batch4_evidence_hashes_are_immutable_and_offline() -> None:
    review = _yaml(REVIEW)

    for evidence in review["evidence"].values():
        if isinstance(evidence, dict) and "path" in evidence:
            assert evidence["sha256"] == _sha256(ROOT / evidence["path"])
    assert review["evidence"]["live_requests_during_batch4_review"] is False
    assert not any(review["production_safety"].values())


def test_sgs_candidate_is_isolated_and_production_is_unchanged() -> None:
    candidate = _rows(CANDIDATES)["SGS"]
    production = _rows(PRODUCTION)["SGS"]

    assert set(_rows(CANDIDATES)) == {"SGS"}
    assert candidate["platform"] == production["platform"] == "smartrecruiters"
    assert candidate["slug"] == production["slug"] == "SGS"
    assert candidate["endpoint_url"] == production["endpoint_url"]
    assert candidate["endpoint_variant"] == production["endpoint_variant"]
    assert candidate["allowed_apply_hosts"] == production["allowed_apply_hosts"]
    assert candidate["page_size"] == 100
    assert candidate["max_pages"] == 50
    assert candidate["max_jobs"] == 5_000
    assert candidate["max_response_bytes"] == 3_000_000
    assert candidate["selective_detail_enrichment"] is False
    assert production["page_size"] == 20
    assert production["max_pages"] == 1
    assert production["max_jobs"] == 20


def test_sgs_bound_and_manual_canary_are_derived() -> None:
    review = _yaml(REVIEW)
    bound = review["sgs_candidate"]["bound_derivation"]
    canary = review["manual_canaries"][0]

    assert bound["observed_high_total"] == 4_619
    assert bound["exact_pages_at_observed_high"] == ceil(4_619 / 100) == 47
    assert bound["bounded_headroom_jobs"] == 381
    assert bound["hard_canary_request_budget"] == 50
    assert canary["company"] == "SGS"
    assert canary["request_budget"] == 50
    assert canary["persistence"] is False
    assert canary["dashboard_publish"] is False
    assert canary["lifecycle_mutation"] is False


def test_sgs_stable_total_completes_and_overflow_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)

    complete_session = GeneratedSgsSession(4_619)
    complete = smartrecruiters.fetch_jobs(complete_session, _candidate())
    assert len(complete_session.calls) == 47
    assert len(complete.jobs) == len(complete.inventory_items) == 4_619
    assert complete.scan["scan_complete"] is True
    assert complete.scan["terminal_evidence_type"] == "authoritative_total"
    assert complete.scan["terminal_evidence_value"] == 4_619
    assert complete.scan["safety_limit_reached"] is False

    overflow_session = GeneratedSgsSession(5_001)
    overflow = smartrecruiters.fetch_jobs(overflow_session, _candidate())
    assert len(overflow_session.calls) == 50
    assert len(overflow.inventory_items) == 5_000
    assert overflow.scan["scan_complete"] is False
    assert overflow.scan["safety_limit_reached"] is True
    assert overflow.scan["stop_reason"] == "maximum_results_reached"
    assert _yaml(REVIEW)["overflow_rule"].endswith(
        "must fail incomplete rather than truncate"
    )
