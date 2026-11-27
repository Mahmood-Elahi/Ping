"""Prepare or run the bounded, read-only board batch from Run #427."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_source_integrity_canaries import BoundedSession, run_canary, selected_targets
from scripts.verify_run424_collection_recovery import revision, write_checkpoint
from src.scraper.company_careers.adapters.atlassian import LISTINGS_URL
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.source_cooldown import source_health_key
from src.scraper.company_careers.student_roles import configure_student_targets
from src.scraper.company_careers.workday_throttle import retry_seconds

BASELINE = datetime(2026, 11, 27, 18, 8, 50, tzinfo=timezone.utc)
SNAPSHOT_MAX_AGE = timedelta(hours=1)
STATE = ROOT / "output/recovery-board-batch-427"
DELIVEROO = "reports/recovery-2026-11-25/deliveroo-candidate.yaml"
DENIED = {401, 403, 407, 429, 451}


@dataclass(frozen=True)
class Source:
    key: str
    company: str
    request_budget: int
    hosts: tuple[str, ...]
    candidate: str | None = None


SOURCES = (
    Source("deliveroo", "Deliveroo", 32, ("careers.deliveroo.co.uk",), DELIVEROO),
    Source("atlassian", "Atlassian", 1, ("www.atlassian.com",)),
    Source("hexagon-novatel", "Hexagon Autonomy & Positioning / NovAtel", 3,
           ("careers-hexagonpositioning.icims.com", "www2.novatel.com")),
    Source("secure-energy-services", "SECURE Energy Services", 6,
           ("careers-canada-secure.icims.com", "secure.ca")),
    Source("aecon", "Aecon", 44, ("jobs.aecon.com", "www.aecon.com")),
    Source("infineon", "Infineon Technologies", 270, ("jobs.infineon.com",)),
)


def digest(data: object) -> str:
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reviewed_plan() -> tuple[dict, dict[str, CompanyTarget]]:
    targets = {}
    rows = []
    for source in SOURCES:
        production, = configure_student_targets(selected_targets([source.company], source_scope="default"))
        target = production
        if source.candidate is not None:
            target, = configure_student_targets(selected_targets(
                [source.company], registry=ROOT / source.candidate, source_scope="default",
            ))
        listing = (
            target.endpoint_url or target.search_url or target.listing_url or target.careers_url
            or (LISTINGS_URL if target.platform == "atlassian" else None)
        )
        if target.platform != production.platform or urlsplit(listing).hostname not in source.hosts:
            raise ValueError(f"Unexpected provider or listing for {source.company}")
        targets[source.key] = target
        rows.append({
            **asdict(source),
            "hosts": list(source.hosts),
            "listing": listing,
            "platform": target.platform,
            "source_scope": target.source_scope,
            "target_sha256": digest(asdict(target)),
            "production_target_sha256": digest(asdict(production)),
            "health_keys": sorted({source_health_key(target), source_health_key(production)}),
        })
    return {
        "revision": revision("rev-parse", "HEAD"),
        "baseline_run": 427,
        "physical_request_budget": sum(source.request_budget for source in SOURCES),
        "sources": rows,
    }, targets


def health_keys(plan: dict) -> set[str]:
    return {key for row in plan["sources"] for key in row["health_keys"]}


def health_sql(plan: dict) -> str:
    keys = ",\n".join("  ('" + key.replace("'", "''") + "')" for key in sorted(health_keys(plan)))
    return f"""BEGIN READ ONLY;
SET LOCAL TIME ZONE 'UTC';
WITH targets (source_key) AS (VALUES
{keys}
)
SELECT targets.source_key, statement_timestamp() AS checked_at_utc,
       health.cooldown_until
FROM targets LEFT JOIN public.job_source_health AS health USING (source_key)
ORDER BY targets.source_key;
ROLLBACK;"""


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Health timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def fresh(observed: datetime, now: datetime) -> bool:
    return now - SNAPSHOT_MAX_AGE <= observed <= now + timedelta(seconds=30) and observed >= BASELINE


def read_health(path: Path, plan: dict) -> tuple[datetime, dict[str, datetime | None]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"source_key", "checked_at_utc", "cooldown_until"} <= rows[0].keys():
        raise ValueError("Expected a CSV snapshot from the batch health query")
    observed = {timestamp(row["checked_at_utc"]) for row in rows}
    deadlines = {
        row["source_key"]: timestamp(row["cooldown_until"]) if row["cooldown_until"] else None
        for row in rows
    }
    if len(observed) != 1 or len(deadlines) != len(rows) or deadlines.keys() != health_keys(plan):
        raise ValueError("Health export must cover each exact key once from a single snapshot")
    checked_at, = observed
    if not fresh(checked_at, datetime.now(timezone.utc)):
        raise ValueError("Health export is stale or predates the baseline")
    return checked_at, deadlines


class ReviewedBoardSession(BoundedSession):
    def __init__(self, source: Source) -> None:
        super().__init__(source.request_budget)
        self.trust_env = False
        self.hosts = source.hosts
        self.denied_status = None
        self.retry_until = None
        self.destination_denied = False

    def send_admitted(self, request, **kwargs):
        destination = urlsplit(request.url)
        if (request.method != "GET" or request.body is not None
                or destination.scheme != "https" or destination.hostname not in self.hosts
                or destination.username is not None or destination.password is not None
                or destination.port not in (None, 443)):
            self.destination_denied = True
            raise AdapterError("Batch request outside the reviewed HTTPS listing hosts")
        if self.denied_status is not None or self.destination_denied:
            raise AdapterError("Provider access is held for this source")
        response = super().send_admitted(request, **kwargs)
        if response.status_code in DENIED:
            self.denied_status = response.status_code
            if self.denied_status == 429:
                now = datetime.now(timezone.utc)
                delay = retry_seconds(response.headers.get("Retry-After"), now.timestamp())
                if delay is not None:
                    try:
                        self.retry_until = (now + timedelta(seconds=delay)).isoformat()
                    except OverflowError:
                        self.retry_until = datetime.max.replace(tzinfo=timezone.utc).isoformat()
            response.close()
            raise AdapterError(f"Provider refused access with HTTP {self.denied_status}")
        return response


def validate_source(source: Source, target: CompanyTarget) -> dict:
    session = ReviewedBoardSession(source)
    result = run_canary(target, source.request_budget, session=session)
    outcome = result["canary_outcome"]
    if session.denied_status is not None:
        outcome = "BLOCKED"
    elif session.destination_denied:
        outcome = "FAIL_IDENTITY"
    elif outcome == "PASS_COMPLETE":
        inventory = result["inventory_items_collected"]
        exact_inventory = (
            type(inventory) is int
            and inventory == result["jobs_collected"]
            == result["unique_stable_ids"] == result["unique_canonical_urls"]
            and not result["unreadable_items"] and not result["rejected_items"]
            and not result["rejected_record_count"]
        )
        if not exact_inventory or not all(result[field] is True for field in (
            "stable_ids_unique", "canonical_urls_unique", "terminal_evidence_valid",
            "hidden_continuation_absent", "normalized_jobs_attributable",
            "apply_urls_contract_valid", "central_finalizer_authoritative",
        )):
            outcome = "FAIL_CONTRACT"
    return {
        "outcome": outcome,
        "requests_started": session.requests_started,
        "denied_status": session.denied_status,
        "retry_until_utc": session.retry_until,
        "canary": result,
    }


def execute(plan: dict, targets: dict, observed: datetime, deadlines: dict) -> dict[str, str]:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "campaign.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = STATE / "campaign.json"
        paths = [STATE / f"{source.key}.json" for source in SOURCES]
        if manifest.exists():
            if json.loads(manifest.read_text()) != plan:
                raise ValueError("Checkpoint belongs to a different reviewed revision or plan")
        elif any(path.exists() for path in paths):
            raise ValueError("Receipts exist without the original plan")
        else:
            write_checkpoint(manifest, plan)

        results = {}
        held_platforms = set()
        for source, row, path in zip(SOURCES, plan["sources"], paths, strict=True):
            if not path.exists():
                continue
            record = json.loads(path.read_text())
            if (record.get("source") != source.key
                    or record.get("target_sha256") != row["target_sha256"]
                    or record.get("state") not in {"started", "finished"}):
                raise ValueError(f"Invalid receipt for {source.company}")
            if record["state"] == "started":
                results[source.key] = "INTERRUPTED"
                held_platforms.add(row["platform"])
            else:
                result = record["result"]
                if (type(result["requests_started"]) is not int
                        or not 0 <= result["requests_started"] <= source.request_budget):
                    raise ValueError(f"Invalid request count for {source.company}")
                results[source.key] = result["outcome"]
                if result["denied_status"] in DENIED:
                    held_platforms.add(row["platform"])

        for source, row, path in zip(SOURCES, plan["sources"], paths, strict=True):
            if source.key in results:
                continue
            now = datetime.now(timezone.utc)
            if not fresh(observed, now):
                results[source.key] = "DEFERRED_STALE_HEALTH"
            elif any(deadlines[key] and deadlines[key] > now for key in row["health_keys"]):
                results[source.key] = "DEFERRED_COOLDOWN"
            elif row["platform"] in held_platforms:
                results[source.key] = "DEFERRED_PROVIDER_HOLD"
            else:
                record = {
                    "source": source.key,
                    "target_sha256": row["target_sha256"],
                    "state": "started",
                    "started_at": now.isoformat(),
                }
                # Reserve the entire source budget before any physical attempt.
                write_checkpoint(path, record)
                result = validate_source(source, targets[source.key])
                write_checkpoint(path, {
                    **record, "state": "finished", "result": result,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                results[source.key] = result["outcome"]
                if result["denied_status"] in DENIED:
                    held_platforms.add(row["platform"])
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--health-sql", action="store_true")
    mode.add_argument("--approve-and-run", metavar="PLAN_SHA256")
    parser.add_argument("--health-export", type=Path)
    args = parser.parse_args(argv)
    if args.health_export is not None:
        args.health_export = args.health_export.resolve()
    os.chdir(ROOT)
    try:
        plan, targets = reviewed_plan()
        if args.health_sql:
            print(health_sql(plan))
            return 0
        fingerprint = digest(plan)
        if not args.approve_and_run:
            print(json.dumps({"plan_sha256": fingerprint, **plan}, indent=2))
            return 0
        if args.approve_and_run != fingerprint or args.health_export is None:
            raise ValueError("Execution requires the exact plan digest and a fresh authorized health export")
        if revision("status", "--porcelain"):
            raise ValueError("Execution requires a clean checkout of the approved revision")
        observed, deadlines = read_health(args.health_export, plan)
        outcomes = execute(plan, targets, observed, deadlines)
        print(json.dumps(outcomes, indent=2))
        return 0 if all(value == "PASS_COMPLETE" for value in outcomes.values()) else 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
