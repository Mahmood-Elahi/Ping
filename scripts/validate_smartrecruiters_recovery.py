"""Validate the three bounded SmartRecruiters recoveries and their exact rollback."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import validate_followup_recovery as followup

PACKAGE = ROOT / 'reports/recovery-smartrecruiters-2026-11-23/certificates.json'
BUNDLE_SHA256 = 'a2dead1d85ff2a108c16b280e8982afd7ffafae4dbd987c2a289de627daae21b'
OVERLAY = 'config/company_careers_reliability_repairs_v1.yaml'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def certificate():
    raw = PACKAGE.read_bytes()
    if digest(raw) != BUNDLE_SHA256:
        raise ValueError('SmartRecruiters activation certificate changed')
    return json.loads(raw)


def predecessor_bytes(name, raw):
    """Reconstruct exactly the old YAML bytes for earlier, immutable activation locks."""
    filename = Path(name).name
    if filename == 'target_companies.yaml':
        raw = followup.prior_registry_bytes(raw)
    proof = certificate()
    hashes = proof['files'][filename]
    if digest(raw) != hashes['new']:
        raise ValueError('Unreviewed SmartRecruiters activation file: ' + name)
    for record in proof['certificates']:
        before, after = record['rollback'][filename], record['proposed'][filename]
        if before is None and after is None:
            continue
        start = raw.index(b'  - name: ' + record['company'].encode() + b'\n')
        end = raw.find(b'\n  - name:', start)
        end = end if end != -1 else len(raw)
        block = raw[start:end]
        for key in ('max_jobs', 'max_pages'):
            pattern = rb'(?m)^(    ' + key.encode() + rb': )' + str(after[key]).encode() + rb'$'
            block, count = re.subn(pattern, lambda m: m[1] + str(before[key]).encode(), block)
            if count != 1:
                raise ValueError('Missing exact bound: ' + record['company'])
        raw = raw[:start] + block + raw[end:]
    if digest(raw) != hashes['old']:
        raise ValueError('Exact SmartRecruiters rollback failed: ' + name)
    return raw


def registry_before_bounds(payload):
    """Restore only the certified three rows while leaving unrelated drift visible."""
    predecessor_bytes('config/target_companies.yaml', (ROOT / 'config/target_companies.yaml').read_bytes())
    proof = certificate()
    records = {r['company']: r for r in proof['certificates']}
    rows = deepcopy(followup.prior_registry(payload)['target_companies'])
    current = {r['name']: r for r in rows if r['name'] in records}
    for stage in ('proposed', 'rollback'):
        if all(current.get(n) == r[stage]['target_companies.yaml'] for n, r in records.items()):
            return {**payload, 'target_companies': [
                records[r['name']]['rollback']['target_companies.yaml'] if r['name'] in records else r for r in rows]}
    raise ValueError('Unreviewed change to SmartRecruiters activation rows')


def validate():
    from src.scraper.company_careers.adapters import smartrecruiters
    from src.scraper.company_careers.registry import load_target_companies
    from src.scraper.company_careers.student_roles import configure_student_targets
    proof = certificate()
    for filename in proof['files']:
        predecessor_bytes('config/' + filename, (ROOT / 'config' / filename).read_bytes())
    engine = followup.prior_adapter_bytes("src/scraper/company_careers/adapters/smartrecruiters.py", Path(smartrecruiters.__file__).read_bytes())
    if digest(engine) != proof['engine_sha256']:
        raise ValueError('SmartRecruiters collection engine changed')
    targets = {t.name: t for t in configure_student_targets(load_target_companies(apply_reliability_repairs=True))}
    for record in proof['certificates']:
        target = targets[record['company']]
        smartrecruiters.validate_target(target)
        raw = json.dumps(asdict(target), sort_keys=True, separators=(',', ':')).encode()
        if digest(raw) != record['target_sha256'] or target.source_scope_key != record['source_scope_key']:
            raise ValueError('Tested employer, board or effective target changed: ' + target.name)
    return {r['company']: r['jobs'] for r in proof['certificates']}


if __name__ == '__main__':
    print(json.dumps(validate()))
