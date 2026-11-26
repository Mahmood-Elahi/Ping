import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import time

import pytest
import requests

from scripts import run_recovery_validation as campaign


def test_approval_roster_matches_dispatch_and_bounds():
    plan, fingerprint = campaign.load_manifest()
    assert len(fingerprint) == 64
    assert len(campaign.targets_for(plan)) == 24
    rows = [row for row in plan["sources"] if row["disposition"] == "validate"]
    assert len(rows) == 20 and sum(row["request_cap"] for row in rows) == 592
    skipped = {row["company"] for row in plan["sources"] if row["disposition"] != "validate"}
    assert skipped == {"Apple", "Infineon Technologies", "Halliburton", "PCL Construction"}
    assert sum(row["previously_certified_and_regressed_in_run_420"] for row in plan["sources"]) == 8
    with pytest.raises(ValueError, match="Approval digest"):
        campaign.load_manifest("0" * 64)


def test_exact_health_snapshot_rejects_stale_and_inconsistent_rows(tmp_path):
    now = datetime(2026, 11, 26, tzinfo=timezone.utc).timestamp()
    fields = ("source_key", "checked_at_utc", "cooldown_until", "persisted_cooldown_state")
    file = tmp_path / "health.csv"
    stamp = campaign.format_utc(now)
    deadline = campaign.format_utc(now + 60)
    with file.open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerow(("active", stamp, deadline, "cooldown active"))
        writer.writerow(("missing", stamp, "", "no matching health row"))
    assert campaign.read_health(file, now, now - 10) == {"active": now + 60, "missing": None}
    with pytest.raises(ValueError, match="stale"):
        campaign.read_health(file, now + 3601, now - 10)
    file.write_text(file.read_text().replace("cooldown active", "cooldown inactive"))
    with pytest.raises(ValueError, match="contradictory"):
        campaign.read_health(file, now, now - 10)


def source(name):
    return {
        "company": name, "platform": "workday", "request_hosts": [f"{name}.wd1.myworkdayjobs.com"],
        "methods": ["POST"], "request_cap": 1, "timeout_seconds": 1,
        "cxs_host": f"{name}.wd1.myworkdayjobs.com", "cxs_paths": [f"/wday/cxs/{name}/board/jobs"],
        "disposition": "validate", "health_keys": [name],
    }


def test_transport_records_wire_429_and_cannot_retry_or_escape_bound(monkeypatch):
    db = sqlite3.connect(":memory:")
    campaign.initialize(db, "approved")
    row = source("testtenant")
    plan = {"total_request_cap": 1}
    real_calls = []

    def reply(_adapter, request, **_kwargs):
        real_calls.append(request.url)
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "180"
        response.url = request.url
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", reply)
    transport = campaign.CampaignTransport(db, plan, row)
    origin = "https://" + row["cxs_host"]
    request = requests.Request("POST", origin + row["cxs_paths"][0]).prepare()
    foreign = requests.Request("POST", origin + "/wday/cxs/testtenant/other/jobs").prepare()
    with pytest.raises(campaign.SourceStopped, match="outside"):
        transport.send(foreign)
    assert transport.send(request).status_code == 429
    with pytest.raises(campaign.SourceStopped, match="429"):
        transport.send(request)
    with pytest.raises(campaign.CanaryBudgetReached):
        campaign.CampaignTransport(db, plan, row).send(request)
    assert real_calls == [request.url]
    code, finish, deadline = db.execute("SELECT status,finished,retry_until FROM wire").fetchone()
    assert code == 429 and deadline - finish == 180


def test_campaign_resume_keeps_completed_interrupted_and_failed_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(campaign, "STATE", tmp_path / "checkpoint")
    names = ("previous", "interrupted", "broken", "healthy", "held", "done")
    sources = [source(name) for name in names]
    sources[-1]["disposition"] = "production_complete"
    plan = {"sources": sources}
    stamp = time.time()
    deadlines = {name: None for name in names}
    deadlines["held"] = stamp + 90
    call_order = []

    def worker(args, **_):
        name = args[args.index("--worker") + 1]
        call_order.append(name)
        if name == "broken":
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"source": name, "outcome": "PASS_COMPLETE"}))

    monkeypatch.setattr(campaign.subprocess, "run", worker)
    campaign.STATE.mkdir()
    with campaign.connect() as db:
        campaign.initialize(db, "locked")
        db.execute("INSERT INTO sources VALUES ('interrupted','running','{}')")
        db.execute("INSERT INTO sources VALUES ('previous','PASS_COMPLETE',?)",
                   (json.dumps({"source": "previous", "outcome": "PASS_COMPLETE"}),))
    result = campaign.run_campaign(plan, "locked", deadlines)
    outcomes = {row["source"]: row["outcome"] for row in result["companies"]}
    assert call_order == ["broken", "healthy"]
    assert outcomes["interrupted"] == "INTERRUPTED"
    assert outcomes["broken"] == "WORKER_ERROR"
    assert outcomes["done"] == "ALREADY_COMPLETE"
    assert outcomes["held"].startswith("DEFERRED_COOLDOWN_UNTIL")
    deadlines["held"] = stamp - 1
    result = campaign.run_campaign(plan, "locked", deadlines)
    assert call_order == ["broken", "healthy", "held"]
    assert result["production_writes"] == 0
    assert (campaign.STATE / "results.json").exists()
    with pytest.raises(ValueError, match="different|incompatible"):
        campaign.run_campaign(plan, "new", deadlines)
