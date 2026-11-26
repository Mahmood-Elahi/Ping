"""Validate and reverse the exact production changes from the completed campaign."""
from dataclasses import asdict
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REGISTRY = "config/target_companies.yaml"
PROOF = ROOT / "reports/recovery-2026-11-26/verified-activation.json"
PROOF_SHA256 = "e2731d8efe19a6286684dd909f7a667a59e115fd513e1386efe8464476ed68f2"


def digest(raw):
    return sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def proof():
    raw = PROOF.read_bytes()
    if digest(raw) != PROOF_SHA256:
        raise ValueError("Completed campaign evidence changed")
    return json.loads(raw)


def before_activation(path, raw):
    """Recover historical input bytes only from an exact recognized revision."""
    record = proof()
    loader = record["loader_rollback"]
    if path == loader["path"] and digest(raw) == loader["after_sha256"]:
        restored = raw.replace(loader["after"].encode(), loader["before"].encode(), 1)
        if digest(restored) != loader["before_sha256"]:
            raise ValueError("Certified activation loader rollback changed")
        return restored
    if path != REGISTRY or digest(raw) != record["registry_after_sha256"]:
        return raw
    for case in record["sources"].values():
        if case["registry_edit"]:
            after = case["after"].encode()
            if raw.count(after) != 1:
                raise ValueError("Certified activation cannot restore exact registry row")
            raw = raw.replace(after, case["before"].encode(), 1)
    if digest(raw) != record["registry_before_sha256"]:
        raise ValueError("Certified activation rollback hash mismatch")
    return raw


def predecessor_registry(payload):
    """Accept an exact prior or activated row and preserve unrelated rows."""
    changes = {
        name: (yaml.safe_load(case["before"])[0], yaml.safe_load(case["after"])[0])
        for name, case in proof()["sources"].items() if case["registry_edit"]
    }
    rows = []
    for row in payload["target_companies"]:
        if row["name"] in changes:
            before, after = changes[row["name"]]
            if row not in (before, after):
                raise ValueError("Unrelated source changed: " + row["name"])
            row = before
        rows.append(row)
    return {**payload, "target_companies": rows}


def validate():
    from src.scraper.company_careers.adapters.successfactors_table_recovery import is_reviewed
    from src.scraper.company_careers.registry import load_target_companies
    from src.scraper.company_careers.student_roles import configure_student_targets

    record = proof()
    raw = (ROOT / REGISTRY).read_bytes()
    if digest(raw) != record["registry_after_sha256"]:
        raise ValueError("Certified registry bytes changed")
    before_activation(REGISTRY, raw)
    loader = record["loader_rollback"]
    loader_bytes = (ROOT / loader["path"]).read_bytes()
    if digest(loader_bytes) != loader["after_sha256"]:
        raise ValueError("Certified production loader changed")
    before_activation(loader["path"], loader_bytes)
    targets = {t.name: t for t in configure_student_targets(load_target_companies(apply_reliability_repairs=True))}
    if len(targets) != 533:
        raise ValueError("Production target count changed")
    for name, case in record["sources"].items():
        target = targets[name]
        materialized = json.dumps(asdict(target), sort_keys=True, separators=(",", ":")).encode()
        if (digest(materialized) != case["target_sha256"] or target.source_scope != case["source_scope"]
                or case["authority"] != "PASS_COMPLETE"):
            raise ValueError("Certified effective target changed: " + name)
        if case["platform"] == "sap_successfactors" and not is_reviewed(target):
            raise ValueError("Certified SAP parser is unreachable: " + name)
    return {name: case["jobs"] for name, case in record["sources"].items()}


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
