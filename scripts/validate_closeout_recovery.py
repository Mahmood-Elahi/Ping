"""Offline applicability of three live campaigns and reversal of the last eleven activations."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = ROOT / "reports/recovery-closeout-activation-2026-11-24/certificates.json"
SEAL = "c00334b890658dceb71e964ad6ee47541089274f6a416366c874223def577842"
REGISTRY = "config/target_companies.yaml"
OVERLAY = "config/company_careers_reliability_repairs_v1.yaml"


def digest(raw):
    return sha256(raw).hexdigest()


def certificate():
    raw = PACKAGE.read_bytes()
    if digest(raw) != SEAL:
        raise ValueError("Closeout certificate changed")
    return json.loads(raw)


def predecessor_bytes(path, raw):
    proof = certificate()
    record = proof["files"].get(path)
    if record is None or digest(raw) == record["previous"]:
        return raw
    if digest(raw) != record["current"] or record["previous"] is None:
        raise ValueError("Unexpected closeout file: " + path)
    if path in (REGISTRY, OVERLAY):
        for source in proof["sources"]:
            row = source.get("rows", {}).get(path)
            if row:
                after = row["after"].encode()
                if raw.count(after) != 1:
                    raise ValueError("Cannot restore exact row: " + source["company"])
                raw = raw.replace(after, row["before"].encode(), 1)
    else:
        lines = raw.decode().splitlines(True)
        for start, end, previous in reversed(record["reverse_edits"]):
            lines[start:end] = previous.splitlines(True)
        raw = "".join(lines).encode()
    if digest(raw) != record["previous"]:
        raise ValueError("Closeout rollback mismatch: " + path)
    return raw


def prior_registry(payload):
    rows = {s["company"]: s["rows"][REGISTRY] for s in certificate()["sources"] if "rows" in s}
    current = {r["name"]: r for r in payload["target_companies"] if r["name"] in rows}
    for stage in ("after", "before"):
        if all(current.get(name) == yaml.safe_load(row[stage])[0] for name, row in rows.items()):
            return {**payload, "target_companies": [
                yaml.safe_load(rows[r["name"]]["before"])[0] if r["name"] in rows else r
                for r in payload["target_companies"]
            ]}
    raise ValueError("Unexpected change in a closeout registry row")


def validate():
    from src.scraper.company_careers.adapters import static_html, successfactors_table_recovery
    from src.scraper.company_careers.registry import load_target_companies
    from src.scraper.company_careers.student_roles import configure_student_targets

    proof = certificate()
    for path, record in proof["files"].items():
        raw = (ROOT / path).read_bytes()
        if digest(raw) != record["current"]:
            raise ValueError("Activated file differs from reviewed version: " + path)
        if record["previous"]:
            predecessor_bytes(path, raw)
    for path, expected in proof["production_runtime_sha256"].items():
        if digest((ROOT / path).read_bytes()) != expected:
            raise ValueError("Reviewed production runtime changed: " + path)
    targets = {t.name: t for t in configure_student_targets(load_target_companies(apply_reliability_repairs=True))}
    if len(targets) != 533:
        raise ValueError("Production registry membership changed")
    for source in proof["sources"]:
        target = targets[source["company"]]
        data = json.dumps(asdict(target), sort_keys=True, separators=(",", ":")).encode()
        if digest(data) != source["target_sha256"] or target.source_scope_key != source["scope"]:
            raise ValueError("Certified target or scope changed: " + target.name)
        if source["campaign"] == 3:
            if target.platform == "static_html":
                static_html.validate_target(target)
            elif not successfactors_table_recovery.is_reviewed(target):
                raise ValueError("Certified table parser is unreachable: " + target.name)
    return {s["company"]: s["jobs"] for s in proof["sources"]}


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
