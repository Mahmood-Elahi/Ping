"""Check five live certificates against the production loader and exact adapter bytes."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import validate_closeout_recovery as closeout

BUNDLE = ROOT / "reports/recovery-five-2026-11-24/certificates.json"
CERTIFICATE_HASH = "7cc37ab61c8e18352d401f23f315b6798d3fa60d2710680cfbeae63643857677"
REGISTRY = "config/target_companies.yaml"


def digest(raw):
    return sha256(raw).hexdigest()


def certificate():
    raw = BUNDLE.read_bytes()
    if digest(raw) != CERTIFICATE_HASH:
        raise ValueError("Five-source activation certificate changed")
    return json.loads(raw)


def prior_adapter_bytes(name, raw):
    if name.startswith("src/scraper/company_careers/adapters/"):
        raw = closeout.predecessor_bytes(name, raw)
    record = certificate()["adapters"].get(name)
    if record is None or digest(raw) == record["old"]:
        return raw
    if digest(raw) != record["new"]:
        raise ValueError("Unreviewed adapter change: " + name)
    lines = raw.decode().splitlines(True)
    for start, end, previous in reversed(record["restore"]):
        lines[start:end] = previous.splitlines(True)
    restored = "".join(lines).encode()
    if digest(restored) != record["old"]:
        raise ValueError("Adapter rollback changed: " + name)
    return restored


def prior_registry_bytes(raw):
    raw = closeout.predecessor_bytes(REGISTRY, raw)
    proof = certificate()
    if digest(raw) == proof["registry_old"]:
        return raw
    if digest(raw) != proof["registry_new"]:
        raise ValueError("Unreviewed five-source activation registry")
    for row in proof["certificates"]:
        before, after = row["raw_before"].encode(), row["raw_after"].encode()
        if raw.count(after) != 1:
            raise ValueError("Five-source activation row changed")
        raw = raw.replace(after, before, 1)
    if digest(raw) != proof["registry_old"]:
        raise ValueError("Five-source registry rollback changed")
    return raw


def prior_registry(payload):
    payload = closeout.prior_registry(payload)
    rows = {r["company"]:r for r in certificate()["certificates"]}
    current = [r for r in payload["target_companies"] if r["name"] in rows]
    for stage in ("raw_after","raw_before"):
        if len(current)==len(rows) and all(r==yaml.safe_load(rows[r["name"]][stage])[0] for r in current):
            return {**payload,"target_companies":[yaml.safe_load(rows[r["name"]]["raw_before"])[0]
                    if r["name"] in rows else r for r in payload["target_companies"]]}
    raise ValueError("Unrelated source changed in five-source activation rows")


def validate():
    from src.scraper.company_careers.registry import load_target_companies
    from src.scraper.company_careers.student_roles import configure_student_targets

    proof = certificate()
    raw = closeout.predecessor_bytes(REGISTRY, (ROOT / REGISTRY).read_bytes())
    if digest(raw) != proof["registry_new"]:
        raise ValueError("Five-source registry is not activated")
    prior_registry_bytes(raw)
    if digest((ROOT / "src/scraper/company_careers/adapters/smartrecruiters_profiles.py").read_bytes()) != proof["profile_sha256"]:
        raise ValueError("Certified SmartRecruiters profile changed")
    for path, hashes in proof["adapters"].items():
        if digest(closeout.predecessor_bytes(path, (ROOT / path).read_bytes())) != hashes["new"]:
            raise ValueError("Certified adapter changed: " + path)
    targets = {t.name:t for t in configure_student_targets(load_target_companies(apply_reliability_repairs=True))}
    for row in proof["certificates"]:
        target = targets[row["company"]]
        data = json.dumps(asdict(target), sort_keys=True, separators=(",",":")).encode()
        if digest(data) != row["target_sha256"] or target.source_scope_key != row["scope"]:
            raise ValueError("Certified effective target changed: " + row["company"])
    return {r["company"]:r["jobs"] for r in proof["certificates"]}


if __name__ == "__main__":
    print(json.dumps(validate()))
