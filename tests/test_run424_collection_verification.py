import io
import json
from dataclasses import replace

import pytest
import requests

from scripts import verify_run424_collection_recovery as verification
from scripts.validate_source_integrity_canaries import CanaryBudgetReached
from src.scraper.company_careers.models import AdapterError


def test_offline_plan_loads_exact_targets_without_requests_or_checkpoints(tmp_path, monkeypatch, capsys):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Planning must not call the canary")

    monkeypatch.setattr(verification, "run_canary", unexpected_run)
    checkpoint_dir = tmp_path / "checkpoint"
    assert verification.main(["--checkpoint-dir", str(checkpoint_dir)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [(source["company"], source["request_budget"]) for source in plan["sources"]] == [
        ("AMD", 250), ("Mercado Libre", 164), ("PCL Construction", 96),
    ]
    assert plan["total_request_budget"] == 510
    assert plan["source_scope"] == "default"
    assert not checkpoint_dir.exists()


def test_checkpoints_isolate_failures_and_prevent_replay(tmp_path, monkeypatch):
    pytest.importorskip("fcntl")
    targets = verification.reviewed_targets()
    calls = []

    def run(target, budget, *, session):
        marker = next(source.key for source in verification.SOURCES if source.company == target.name)
        receipt = json.loads((tmp_path / f"{marker}.json").read_text())
        assert receipt["state"] == "started"
        assert session.budget == budget
        calls.append((target.name, budget))
        if target.name == "AMD":
            raise RuntimeError("fixture failure")
        return {"canary_outcome": "PASS_COMPLETE", "requests_started": 2}

    monkeypatch.setattr(verification, "run_canary", run)
    plan = {"revision": "fixture"}
    assert verification.execute(targets, tmp_path, plan) == 1
    assert calls == [("AMD", 250), ("Mercado Libre", 164), ("PCL Construction", 96)]
    assert json.loads((tmp_path / "amd.json").read_text())["result"]["canary_outcome"] == "RUNNER_ERROR"

    verification.write_checkpoint(tmp_path / "pcl-construction.json", {
        "state": "started", "company": "PCL Construction",
    })
    assert verification.execute(targets, tmp_path, plan) == 1
    assert len(calls) == 3
    with pytest.raises(ValueError, match="different review plan"):
        verification.execute(targets, tmp_path, {"revision": "other"})


@pytest.mark.parametrize(
    ("location", "failure"),
    [
        ("/api/jobs?page=2", CanaryBudgetReached),
        ("https://unreviewed.invalid/api/jobs", AdapterError),
        ("/another-board", AdapterError),
    ],
)
def test_redirect_is_checked_and_charged_before_second_send(monkeypatch, location, failure):
    calls = []

    def send(_adapter, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code = 302
        response.url = request.url
        response.headers["Location"] = location
        response.raw = io.BytesIO()
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    source = replace(verification.SOURCES[0], request_budget=1)
    with verification.ReviewedListingSession(source) as session, pytest.raises(failure):
        session.get(source.listing_url)
    assert calls == [source.listing_url]
    assert session.requests_started == 1


def test_rate_limit_stops_source_without_adapter_retry(monkeypatch):
    def send(_adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 429
        response.url = request.url
        response.raw = io.BytesIO()
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    source = verification.SOURCES[0]
    target = verification.reviewed_targets()[source.company]
    result = verification.run_canary(
        target, source.request_budget, session=verification.ReviewedListingSession(source)
    )
    assert result["canary_outcome"] == "FAIL_TRANSIENT"
    assert result["requests_started"] == 1
    assert not result["authoritative_for_absence"]
