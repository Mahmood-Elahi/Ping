import csv
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from scripts import verify_recovery_board_batch as batch
from scripts.validate_source_integrity_canaries import CanaryBudgetReached
from src.scraper.company_careers.models import AdapterError


def test_plan_resolves_exact_production_scope_and_candidate_bound(monkeypatch):
    monkeypatch.chdir(batch.ROOT)
    plan, targets = batch.reviewed_plan()
    assert plan["physical_request_budget"] == 356
    assert len(targets) == len(plan["sources"]) == 6
    assert targets["deliveroo"].max_pages == 16
    assert all(row["source_scope"] == "default" for row in plan["sources"])
    assert all(len(row["target_sha256"]) == 64 for row in plan["sources"])
    assert "BEGIN READ ONLY" in batch.health_sql(plan)


def test_health_export_checks_exact_keys_and_snapshot_freshness(tmp_path):
    path = tmp_path / "health.csv"
    now = datetime.now(timezone.utc)
    plan = {"sources": [{"health_keys": ["production", "candidate"]}]}

    def write(rows):
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["source_key", "checked_at_utc", "cooldown_until"])
            writer.writerows(rows)

    write([("production", now, ""), ("candidate", now, now + timedelta(hours=3))])
    checked_at, deadlines = batch.read_health(path, plan)
    assert checked_at == now
    assert deadlines == {"production": None, "candidate": now + timedelta(hours=3)}
    write([("production", now, ""), ("production", now, "")])
    with pytest.raises(ValueError, match="exact key"):
        batch.read_health(path, plan)
    stale = now - timedelta(hours=2)
    write([("production", stale, ""), ("candidate", stale, "")])
    with pytest.raises(ValueError, match="stale"):
        batch.read_health(path, plan)


def test_physical_budget_and_destination_policy(monkeypatch):
    seen = []

    def send(adapter, request, **kwargs):
        seen.append(request.url)
        response = requests.Response()
        response.url = request.url
        response._content = b"{}"
        response._content_consumed = True
        response.status_code = 200
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    source = batch.SOURCES[1]
    url = "https://" + source.hosts[0] + "/listing"
    with batch.ReviewedBoardSession(source) as session:
        session.get(url)
        with pytest.raises(CanaryBudgetReached):
            session.get(url)
        assert session.requests_started == 1
    with batch.ReviewedBoardSession(source) as session:
        with pytest.raises(AdapterError, match="outside"):
            session.get("https://unreviewed.invalid/listing")
        assert session.requests_started == 0
    assert seen == [url]


def test_checkpoints_hold_cooldown_provider_and_interrupted_reservations(monkeypatch, tmp_path):
    monkeypatch.setattr(batch, "STATE", tmp_path)
    now = datetime.now(timezone.utc)
    platforms = ("public_json", "atlassian", "icims", "icims", "sap", "eightfold")
    rows = [{
        "platform": platform, "health_keys": [source.key], "target_sha256": source.key,
    } for platform, source in zip(platforms, batch.SOURCES, strict=True)]
    plan = {"sources": rows}
    deadlines = {source.key: None for source in batch.SOURCES}
    deadlines["deliveroo"] = now + timedelta(hours=2)
    batch.write_checkpoint(tmp_path / "campaign.json", plan)
    batch.write_checkpoint(tmp_path / "aecon.json", {
        "source": "aecon", "target_sha256": "aecon", "state": "started",
    })
    calls = []

    def validate(source, _target):
        calls.append(source.key)
        receipt = json.loads((tmp_path / f"{source.key}.json").read_text())
        assert receipt["state"] == "started"
        blocked = source.key == "hexagon-novatel"
        return {"outcome": "BLOCKED" if blocked else "PASS_COMPLETE",
                "denied_status": 429 if blocked else None, "requests_started": 1}

    monkeypatch.setattr(batch, "validate_source", validate)
    targets = dict.fromkeys(deadlines)
    outcomes = batch.execute(plan, targets, now, deadlines)
    assert outcomes == {
        "deliveroo": "DEFERRED_COOLDOWN", "atlassian": "PASS_COMPLETE",
        "hexagon-novatel": "BLOCKED", "secure-energy-services": "DEFERRED_PROVIDER_HOLD",
        "aecon": "INTERRUPTED", "infineon": "PASS_COMPLETE",
    }
    assert calls == ["atlassian", "hexagon-novatel", "infineon"]
    deadlines["deliveroo"] = None
    outcomes = batch.execute(plan, targets, now, deadlines)
    assert outcomes["deliveroo"] == "PASS_COMPLETE"
    assert outcomes["secure-energy-services"] == "DEFERRED_PROVIDER_HOLD"
    assert calls == ["atlassian", "hexagon-novatel", "infineon", "deliveroo"]
