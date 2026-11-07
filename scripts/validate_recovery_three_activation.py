"""Offline applicability and exact rollback for three retained complete canaries."""
from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = ROOT / 'reports/recovery-three-activation-2026-11-07'
NAMES = ('Adobe', 'Workday', 'Lam Research')
BUNDLE_SHA256 = '51cd48e06f22541f94263596b0107e43c773609054b02b8dc0c75342a61e333b'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def evidence():
    raw = (PACKAGE / 'evidence.json.gz.b64').read_bytes()
    if digest(raw) != BUNDLE_SHA256:
        raise ValueError('Three-source retained evidence changed')
    return json.loads(gzip.decompress(base64.b64decode(raw)))


@lru_cache(maxsize=8)
def _materialize(payload_bytes):
    # Validation checks every relevant runtime/config hash before using this
    # cache. Only identical registry payloads can reuse loader/preprocessor work.
    from scripts.validate_recovery_eight_activation import materialize
    return materialize(json.loads(payload_bytes))


def validate(payload=None):
    from src.scraper.company_careers.adapters.successfactors_table_recovery import is_reviewed
    from src.scraper.company_careers.source_cooldown import source_health_key
    proof = evidence()
    if payload is None:
        payload = yaml.safe_load((ROOT / 'config/target_companies.yaml').read_text())
    rows = payload['target_companies']
    names = [r['name'] for r in rows]
    before = yaml.safe_load(proof['predecessor_registry_utf8'])
    if len(rows) != 533 or len(set(names)) != 533 or names != [r['name'] for r in before['target_companies']]:
        raise ValueError('Registry membership changed')
    for row, old in zip(rows, before['target_companies'], strict=True):
        if row['name'] not in NAMES and row != old:
            raise ValueError('Unrelated source changed: ' + row['name'])
    for path, expected in proof['engine_files'].items():
        if digest((ROOT / path).read_bytes()) != expected:
            raise ValueError('Certified runtime changed: ' + path)
    after_targets, before_targets = _materialize(canonical(payload)), _materialize(canonical(before))
    jobs = requests = 0
    if tuple(c['source']['company'] for c in proof['certificates']) != NAMES:
        raise ValueError('Certificate roster changed')
    for cert in proof['certificates']:
        source, result = cert['source'], cert['result']
        name = source['company']
        target = after_targets[name]
        if asdict(target) != source['exact_preprocessed_target'] or source_health_key(target) != result['source_config_key']:
            raise ValueError('Exact tested target changed: ' + name)
        expected = {'company': name, 'platform': target.platform, 'source_scope': 'default',
                    'source_scope_key': target.source_scope_key, 'canary_outcome': 'PASS_COMPLETE',
                    'scan_state': 'complete', 'source_health_code': 'healthy',
                    'source_identity_status': 'validated', 'inventory_authority': 'authoritative',
                    'result_authority': 'authoritative_inventory'}
        if any(result.get(k) != v for k, v in expected.items()):
            raise ValueError('Certificate identity/authority mismatch: ' + name)
        required = ('scan_complete', 'inventory_complete', 'authoritative_for_absence', 'can_reconcile_absence',
                    'terminal_evidence_valid', 'hidden_continuation_absent', 'stable_ids_unique',
                    'canonical_urls_unique', 'normalized_jobs_attributable', 'apply_urls_contract_valid',
                    'runtime_inputs_verified', 'fresh_complete_evidence')
        if any(result.get(k) is not True for k in required):
            raise ValueError('Incomplete certificate: ' + name)
        if any(result.get(k) is not False for k in ('safety_limit_reached', 'collection_changed_during_scan', 'physical_count_is_upper_bound')):
            raise ValueError('Unsafe certificate: ' + name)
        if any(type(result.get(k)) is not int or result[k] != 0 for k in ('unreadable_items', 'rejected_items', 'rejected_record_count')):
            raise ValueError('Rejected inventory: ' + name)
        counts = [result.get(k) for k in ('jobs_collected', 'inventory_items_collected', 'total_available')]
        if any(type(n) is not int or n < 0 for n in counts) or len(set(counts)) != 1:
            raise ValueError('Inventory accounting mismatch: ' + name)
        accounting = cert['verified_identity_accounting']
        if accounting['identity_sets_equal'] is not True or any(accounting[k] != counts[0] for k in ('inventory_count', 'normalized_count', 'unique_urls')):
            raise ValueError('Stable identity accounting mismatch: ' + name)
        count = result.get('physical_requests_accounted')
        if type(count) is not int or not 0 < count <= source['physical_request_cap'] or result.get('requests_started') != count:
            raise ValueError('Physical request accounting mismatch: ' + name)
        if result.get('request_count_unit') != 'physical_http_attempts':
            raise ValueError('Physical accounting unit changed')
        if asdict(before_targets[name]) != proof['rollback'][name]['effective_target']:
            raise ValueError('Exact rollback target changed: ' + name)
        jobs += counts[0]
        requests += count
    for name, target in after_targets.items():
        if name not in NAMES and asdict(target) != asdict(before_targets[name]):
            raise ValueError('Unrelated production preprocessing changed: ' + name)
        if is_reviewed(target) != (name == 'Lam Research'):
            raise ValueError('Lam parser dispatch escaped its exact contract: ' + name)
    if jobs != 2135 or requests != 93:
        raise ValueError('Retained totals changed')
    return {'status': 'PASS', 'companies': list(NAMES), 'verified_jobs': jobs,
            'retained_success_requests': requests, 'other_sources_unchanged': 530,
            'new_live_requests': 0, 'production_writes': 0}


def registry_before_three(payload):
    before = yaml.safe_load(evidence()['predecessor_registry_utf8'])
    if payload == before:
        return deepcopy(before)
    validate(payload)
    restored = deepcopy(payload)
    rollback = evidence()['rollback']
    restored['target_companies'] = [deepcopy(rollback[r['name']]['raw_row']) if r['name'] in NAMES else r
                                    for r in restored['target_companies']]
    if restored != before:
        raise ValueError('Exact three-source rollback did not restore predecessor')
    return restored


def predecessor_runtime(name, raw):
    """Historical locks remain immutable; current exact applicability is checked first."""
    proof = evidence()
    old = proof['predecessor_runtime_utf8'].get(name)
    if old is None:
        return raw
    if digest(raw) != proof['engine_files'][name]:
        raise ValueError('Runtime drift before historical composition: ' + name)
    return old.encode()


if __name__ == '__main__':
    print(json.dumps(validate(), indent=2))
