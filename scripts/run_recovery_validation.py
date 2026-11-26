"""Run one approved, nonpersistent campaign through the production adapters."""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_source_integrity_canaries import (
    BoundedSession, CanaryBudgetReached, run_canary, selected_targets,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_cooldown import source_health_key
from src.scraper.company_careers.student_roles import configure_student_targets
from src.scraper.company_careers.workday_throttle import (
    CORRELATED_SPACING, HOST, LISTING, SPACING, WINDOW, retry_seconds,
)

MANIFEST = ROOT / "reports/recovery-2026-11-25/validation-approval.json"
STATE = ROOT / "output/recovery-validation-45707674434"
DENIED = {401, 403, 407, 429, 451}
HEALTH_MAX_AGE = 3600


def hash_bytes(value):
    return sha256(value).hexdigest()


def digest(value):
    return hash_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def runtime_files():
    roots = (ROOT / "src/scraper/company_careers", ROOT / "config", MANIFEST.parent)
    paths = {Path(__file__), ROOT / "scripts/validate_source_integrity_canaries.py"}
    for folder in roots:
        paths.update(path for path in folder.rglob("*")
                     if path.is_file() and path.suffix in {".py", ".json", ".yaml"}
                     and path != MANIFEST)
    return {path.relative_to(ROOT).as_posix(): hash_bytes(path.read_bytes())
            for path in sorted(paths)}


def load_manifest(expected=None):
    raw = MANIFEST.read_bytes()
    fingerprint = hash_bytes(raw)
    if expected is not None and fingerprint != expected:
        raise ValueError("Approval digest does not match the exact manifest")
    plan = json.loads(raw)
    if plan["runtime_sha256"] != digest(runtime_files()):
        raise ValueError("Campaign runtime, registry, or candidate files differ from the approved inputs")
    rows = plan["sources"]
    if len({row["company"] for row in rows}) != len(rows):
        raise ValueError("Campaign contains duplicate companies")
    if not all(row["request_hosts"] and 0 < row["request_cap"] <= 1000
               and row["disposition"] in {"validate", "production_complete"} for row in rows):
        raise ValueError("Manifest has an invalid source disposition or request bound")
    if sum(row["request_cap"] for row in rows if row["disposition"] == "validate") != plan["total_request_cap"]:
        raise ValueError("Physical campaign request cap does not match the source limits")
    return plan, fingerprint


def targets_for(plan):
    results = {}
    production = {
        target.name: target for target in configure_student_targets(
            load_target_companies(apply_reliability_repairs=True)
        )
    }
    for row in plan["sources"]:
        company = row["company"]
        target = production[company]
        health_keys = {source_health_key(target)}
        if row["candidate"]:
            selected = selected_targets([company], ROOT / row["candidate"], row["scope"])
            target, = configure_student_targets(selected)
            health_keys.add(source_health_key(target))
        if digest(asdict(target)) != row["target_sha256"] or sorted(health_keys) != row["health_keys"]:
            raise ValueError(f"Approved effective target or cooldown keys changed for {company}")
        results[company] = target
    return results


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Health timestamps require a UTC offset")
    return parsed.timestamp()


def read_health(path, now, earliest):
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    stamps = {utc_timestamp(row["checked_at_utc"]) for row in rows}
    if len(stamps) != 1:
        raise ValueError("Health export must be from a single read-only snapshot")
    observed, = stamps
    if observed < earliest or now - observed > HEALTH_MAX_AGE or observed > now + 30:
        raise ValueError("Health export is stale or predates the production baseline")
    deadlines = {}
    for row in rows:
        key = row["source_key"]
        state = row["persisted_cooldown_state"]
        until = utc_timestamp(row["cooldown_until"]) if row["cooldown_until"] else None
        if key in deadlines or state not in {
            "cooldown active", "cooldown inactive", "no matching health row",
        } or (state == "cooldown active") != (until is not None and until > observed):
            raise ValueError("Health export contains duplicate or contradictory rows")
        deadlines[key] = until
    return deadlines


def format_utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def connect():
    db = sqlite3.connect(STATE / "progress.sqlite3")
    db.execute("PRAGMA synchronous=FULL")
    return db


def initialize(db, fingerprint):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS approval (id INTEGER PRIMARY KEY CHECK (id=1), digest TEXT);
        CREATE TABLE IF NOT EXISTS sources (
            company TEXT PRIMARY KEY, outcome TEXT NOT NULL, evidence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wire (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, host TEXT NOT NULL, cxs INTEGER NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, started REAL NOT NULL, status INTEGER,
            exception TEXT, finished REAL, retry_until REAL
        );
    """)
    db.execute("INSERT OR IGNORE INTO approval VALUES (1, ?)", (fingerprint,))
    db.commit()
    if db.execute("SELECT digest FROM approval").fetchone() != (fingerprint,):
        raise ValueError("Checkpoint belongs to a different approval manifest")


class SourceStopped(RuntimeError):
    pass


class CampaignTransport(requests.adapters.HTTPAdapter):
    """Charge a physical attempt before Requests can consume or follow its response."""

    def __init__(self, db, plan, row):
        super().__init__(max_retries=0)
        self.db, self.plan, self.row = db, plan, row
        self.blocked_status = None

    def send(self, request, **kwargs):
        row, db = self.row, self.db
        address = urlsplit(request.url)
        host = address.hostname
        cxs = row["platform"] == "workday" and request.method == "POST"
        if (address.scheme != "https" or address.username or address.password
                or address.port not in (None, 443) or host not in row["request_hosts"]
                or request.method not in row["methods"]
                or (cxs and (
                    host != row["cxs_host"] or not HOST.fullmatch(host or "")
                    or not LISTING.fullmatch(address.path) or address.path not in row["cxs_paths"]
                ))):
            raise SourceStopped("Request outside the approved HTTPS host and method contract")
        if self.blocked_status is not None:
            raise SourceStopped(f"Provider refused access with HTTP {self.blocked_status}")
        now = time.time()
        if cxs:
            last, = db.execute("SELECT COALESCE(MAX(started), 0) FROM wire WHERE cxs=1").fetchone()
            signals, = db.execute(
                "SELECT COUNT(DISTINCT host) FROM wire WHERE cxs=1 AND status=429 AND finished>?",
                (now - WINDOW,),
            ).fetchone()
            time.sleep(max(0, last + (CORRELATED_SPACING if signals >= 2 else SPACING) - now))
            now = time.time()
        count, = db.execute("SELECT COUNT(*) FROM wire WHERE company=?", (row["company"],)).fetchone()
        total, = db.execute("SELECT COUNT(*) FROM wire").fetchone()
        if count >= row["request_cap"] or total >= self.plan["total_request_cap"]:
            raise CanaryBudgetReached("Approved physical request cap exhausted")
        attempt = db.execute(
            """INSERT INTO wire(company,host,cxs,method,path,started) VALUES(?,?,?,?,?,?)""",
            (row["company"], host, int(cxs), request.method, address.path, now),
        ).lastrowid
        db.commit()
        try:
            response = super().send(request, **kwargs)
        except requests.RequestException as exc:
            db.execute("UPDATE wire SET exception=?, finished=? WHERE id=?",
                       (type(exc).__name__, time.time(), attempt))
            db.commit()
            raise
        finished = time.time()
        code = response.status_code
        delay = retry_seconds(response.headers.get("Retry-After"), finished) if code == 429 else None
        deadline = (
            min(finished + (WINDOW if delay is None else delay), datetime(9999, 12, 31, tzinfo=timezone.utc).timestamp())
            if code == 429 else None
        )
        db.execute("UPDATE wire SET status=?, finished=?, retry_until=? WHERE id=?",
                   (code, finished, deadline, attempt))
        db.commit()
        if code in DENIED:
            self.blocked_status = code
            if not (cxs and code == 429):
                response.close()
                raise SourceStopped(f"Provider refused access with HTTP {code}")
        return response


def execute_source(plan, row, target):
    with closing(connect()) as db:
        transport = CampaignTransport(db, plan, row)
        session = BoundedSession(row["request_cap"])
        session.trust_env = False
        session.mount("https://", transport)
        session.mount("http://", transport)
        pairs = []
        result = run_canary(target, row["request_cap"], session=session, capture_inventory=pairs)
        wire = db.execute(
            """SELECT method,host,path,status,exception,retry_until
               FROM wire WHERE company=? ORDER BY id""",
            (row["company"],),
        ).fetchall()
        outcome = result["canary_outcome"]
        if transport.blocked_status is not None:
            outcome = "BLOCKED"
        if outcome == "PASS_COMPLETE" and (
            not all(result.get(field) is True for field in (
                "central_finalizer_authoritative", "terminal_evidence_valid",
                "normalized_jobs_attributable", "apply_urls_contract_valid",
                "hidden_continuation_absent",
            ))
            or len(pairs) != result["inventory_items_collected"]
            or len(pairs) != len({item_id for item_id, _ in pairs})
            or len(pairs) != len({url for _, url in pairs})
        ):
            outcome = "FAIL_CONTRACT"
        if outcome != "PASS_COMPLETE":
            result.update(central_finalizer_authoritative=False, authoritative_for_absence=False,
                          can_reconcile_absence=False)
        return {
            "outcome": outcome,
            "source": row["company"],
            "target_sha256": row["target_sha256"],
            "canary": result,
            "inventory_pairs": pairs if outcome == "PASS_COMPLETE" else [],
            "inventory_sha256": digest(pairs) if outcome == "PASS_COMPLETE" else None,
            "physical_requests": len(wire),
            "receipts": [
                dict(method=m, host=h, path=p, status=s, exception=e,
                     provider_retry_until=format_utc(u) if u else None)
                for m, h, p, s, e, u in wire
            ],
            "production_authority": False,
        }


def write_summary(db, plan, fingerprint, deferred):
    results = {name: json.loads(raw) for name, raw in db.execute(
        "SELECT company,evidence FROM sources WHERE outcome != 'running'"
    )}
    rows = [
        results.get(row["company"], {
            "source": row["company"], "outcome": deferred.get(
                row["company"], "ALREADY_COMPLETE" if row["disposition"] == "production_complete" else "PENDING"
            ),
        }) for row in plan["sources"]
    ]
    summary = {
        "approval_sha256": fingerprint,
        "physical_requests": db.execute("SELECT COUNT(*) FROM wire").fetchone()[0],
        "production_writes": 0,
        "companies": rows,
    }
    temp = STATE / "results.tmp"
    temp.write_text(json.dumps(summary, indent=2) + "\n")
    temp.replace(STATE / "results.json")
    return summary


def run_campaign(plan, fingerprint, deadlines):
    missing = {
        key for row in plan["sources"] if row["disposition"] == "validate"
        for key in row["health_keys"] if key not in deadlines
    }
    if missing:
        raise ValueError("Export is missing required health keys: " + ", ".join(sorted(missing)))
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        bound = lock.read().strip()
        if bound and (bound != fingerprint or not (STATE / "progress.sqlite3").is_file()):
            raise ValueError("Bound checkpoint missing or incompatible")
        with closing(connect()) as db:
            initialize(db, fingerprint)
            if not bound:
                lock.seek(0)
                lock.write(fingerprint + "\n")
                lock.flush()
                os.fsync(lock.fileno())
            # A restart only processes never-attempted or previously deferred sources.
            for name, in db.execute("SELECT company FROM sources WHERE outcome='running'").fetchall():
                result = {"source": name, "outcome": "INTERRUPTED", "production_authority": False}
                db.execute("UPDATE sources SET outcome=?, evidence=? WHERE company=?",
                           (result["outcome"], json.dumps(result), name))
            db.commit()
            deferred = {}
            for row in plan["sources"]:
                name = row["company"]
                if row["disposition"] != "validate" or db.execute(
                    "SELECT 1 FROM sources WHERE company=?", (name,)
                ).fetchone():
                    continue
                now = time.time()
                until = max(deadlines[key] or 0 for key in row["health_keys"])
                hosts = row["request_hosts"]
                placeholders = ",".join("?" for _ in hosts)
                host_until, unknown_workday_attempt = db.execute(
                    f"""SELECT COALESCE(MAX(retry_until),0), MAX(cxs=1 AND finished IS NULL)
                        FROM wire WHERE host IN ({placeholders})""", hosts,
                ).fetchone()
                if unknown_workday_attempt:
                    deferred[name] = "DEFERRED_UNKNOWN_HOST_OUTCOME"
                    continue
                until = max(until, host_until)
                if until > now:
                    deferred[name] = f"DEFERRED_COOLDOWN_UNTIL {format_utc(until)}"
                    continue
                db.execute("INSERT INTO sources VALUES (?, 'running', '{}')", (name,))
                db.commit()
                try:
                    finished = subprocess.run(
                        [sys.executable, __file__, "--worker", name, "--approve-and-run", fingerprint],
                        cwd=ROOT, capture_output=True, text=True, timeout=row["timeout_seconds"],
                        pass_fds=(lock.fileno(),),
                    )
                    result = json.loads(finished.stdout) if finished.returncode == 0 else {
                        "source": name, "outcome": "WORKER_ERROR",
                        "exit_code": finished.returncode, "production_authority": False,
                    }
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    result = {"source": name, "outcome": "WORKER_ERROR",
                              "reason": type(exc).__name__, "production_authority": False}
                db.execute("UPDATE sources SET outcome=?, evidence=? WHERE company=?",
                           (result["outcome"], json.dumps(result), name))
                db.commit()
                write_summary(db, plan, fingerprint, deferred)
            return write_summary(db, plan, fingerprint, deferred)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-and-run", metavar="MANIFEST_SHA256")
    parser.add_argument("--health-export", type=Path)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.chdir(ROOT)
    plan, fingerprint = load_manifest(args.approve_and_run)
    targets = targets_for(plan)
    if args.worker:
        if not args.approve_and_run or args.worker not in targets:
            parser.error("Invalid worker invocation")
        row, = (row for row in plan["sources"] if row["company"] == args.worker)
        with closing(connect()) as db:
            if db.execute("SELECT outcome FROM sources WHERE company=?", (args.worker,)).fetchone() != ("running",):
                parser.error("Worker has no durable reservation")
        print(json.dumps(execute_source(plan, row, targets[args.worker])))
        return
    if not args.approve_and_run:
        print(json.dumps({"approval_sha256": fingerprint, "manifest": str(MANIFEST),
                          "physical_request_cap": plan["total_request_cap"],
                          "sources": len(plan["sources"])}, indent=2))
        return
    if args.health_export is None:
        parser.error("A current authorized read-only health export is required")
    deadlines = read_health(args.health_export, time.time(), utc_timestamp(plan["production"]["finished_at_utc"]))
    summary = run_campaign(plan, fingerprint, deadlines)
    print(json.dumps({"physical_requests": summary["physical_requests"],
                      "outcomes": {row["source"]: row["outcome"] for row in summary["companies"]},
                      "results": str(STATE / "results.json")}, indent=2))


if __name__ == "__main__":
    main()
