"""Read-only verification of the ten certified SuccessFactors activations.

No provider, database or dashboard client is constructed. The historical
baseline loader uses a temporary local file and restores its module setting.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.scraper.company_careers import registry
from src.scraper.company_careers.adapters import sap_successfactors as sf
from src.scraper.company_careers.adapters import successfactors_recovery_routing as routing
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.source_cooldown import source_health_key

PACKAGE = Path('reports/successfactors-ten-activation-2026-10-30')
NAMES = ('Keyence', 'TransAlta', 'Spartan Controls', 'ARC Resources', 'Gibson Energy',
         'SaskPower', 'Hydro One', 'Omron', 'Canada Life', 'Hyundai')
BASELINE_COMPANIES = ('Festo', 'Volkswagen Group', 'TELUS', 'Hatch', 'VIA Rail')
# Generated once from the reviewed package. Never recompute a lock to accept drift.
LOCK_SHA256 = 'e868f7f0fcca66ca97d18abd93199d5139f2ddf11ad58891cf40e157f7bd03e5'


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(name, root=ROOT):
    return json.loads((root / PACKAGE / name).read_text())


def validate_result(row, source):
    required = ('scan_complete', 'inventory_complete', 'authoritative_for_absence',
                'can_reconcile_absence', 'terminal_evidence_valid', 'hidden_continuation_absent',
                'stable_ids_unique', 'canonical_urls_unique', 'normalized_jobs_attributable',
                'apply_urls_contract_valid')
    expected = {'company': source['company'], 'source_scope': source['source_scope'],
                'source_config_key': source['source_config_key'], 'canary_outcome': 'PASS_COMPLETE',
                'scan_state': 'complete', 'source_health_code': 'healthy', 'platform': 'sap_successfactors',
                'source_identity_status': 'validated', 'inventory_authority': 'authoritative',
                'result_authority': 'authoritative_inventory'}
    if any(row.get(k) != value for k, value in expected.items()) or any(row.get(k) is not True for k in required):
        raise ValueError('Certificate identity, completion or authority mismatch')
    counts = [row.get(k) for k in ('inventory_items_collected', 'jobs_collected', 'total_available')]
    if any(type(n) is not int or n < 0 for n in counts) or len(set(counts)) != 1:
        raise ValueError('Certificate inventory accounting mismatch')
    if any(type(row.get(k)) is not int or row[k] != 0 for k in ('unreadable_items', 'rejected_items', 'rejected_record_count')):
        raise ValueError('Certificate contains rejected or unreadable inventory')
    if any(row.get(k) is not False for k in ('safety_limit_reached', 'collection_changed_during_scan', 'physical_count_is_upper_bound')):
        raise ValueError('Certificate safety, mutation or accounting uncertainty')
    count = row.get('physical_requests_accounted')
    if type(count) is not int or not 0 < count <= source['physical_request_cap'] or row.get('requests_started') != count:
        raise ValueError('Certificate physical request accounting mismatch')
    if row.get('request_count_unit') != 'physical_http_attempts':
        raise ValueError('Certificate is not physically accounted')


def engine_ast(text, *, consolidated=False):
    tree = ast.parse(text)
    if consolidated:
        class RemoveUnreachableAdditions(ast.NodeTransformer):
            def visit_FunctionDef(self, node):
                if node.name in {'fetch_jobs', '_ignored_current_locale_anchor'}:
                    return None
                if node.name == '_fetch_reviewed_jobs':
                    node.name = 'fetch_jobs'
                return self.generic_visit(node)
            def visit_Assign(self, node):
                if any(isinstance(t, ast.Name) and t.id == '_REVIEWED_SELF_LOCALE_ANCHORS' for t in node.targets):
                    return None
                return self.generic_visit(node)
            def visit_ImportFrom(self, node):
                if node.module == 'urllib.parse':
                    node.names = [n for n in node.names if n.name != 'urlencode']
                return node
            def visit_If(self, node):
                if isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name) and node.test.func.id == '_ignored_current_locale_anchor':
                    return None
                return self.generic_visit(node)
        tree = RemoveUnreachableAdditions().visit(tree)
    return ast.dump(tree, include_attributes=False)


def load_baseline(rows):
    with tempfile.TemporaryDirectory(prefix='ping-sf-baseline-') as directory:
        path = Path(directory) / 'target_companies.yaml'
        path.write_text(yaml.safe_dump({'target_companies': rows}, sort_keys=False))
        with patch.object(registry, 'DEFAULT_REGISTRY_PATH', str(path)):
            return registry.load_target_companies(str(path), apply_reliability_repairs=True)


def validate(root=ROOT):
    root = Path(root).resolve()
    if root != ROOT or Path.cwd().resolve() != ROOT:
        raise ValueError('Run from the activation checkout root')
    lock_path = root / PACKAGE / 'artifact_lock.json'
    if digest(lock_path.read_bytes()) != LOCK_SHA256:
        raise ValueError('Immutable activation lock changed')
    lock = json.loads(lock_path.read_text())
    for name, expected in lock.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or digest(path.read_bytes()) != expected:
            raise ValueError('Immutable activation artifact changed: ' + name)
    for name, expected in read_json('unchanged_runtime.json')['unchanged_files'].items():
        if name != 'config/target_companies.yaml' and digest((root / name).read_bytes()) != expected:
            raise ValueError('Unrelated production runtime changed: ' + name)
    applicability = read_json('code_applicability.json')
    for name, expected in applicability['reviewed_runtime'].items():
        if digest((root / name).read_bytes()) != expected:
            raise ValueError('Runtime differs from consolidated review: ' + name)
    if engine_ast((root / 'src/scraper/company_careers/adapters/sap_successfactors.py').read_text(), consolidated=True) != engine_ast((root / PACKAGE / 'evidence/canary_adapter.py.txt').read_text()):
        raise ValueError('Certified source engine differs beyond unreachable additions')
    before_rows = read_json('baseline_raw_rows.json')
    current_rows = yaml.safe_load((root / 'config/target_companies.yaml').read_text())['target_companies']
    before = {r['name']: r for r in before_rows}
    current = {r['name']: r for r in current_rows}
    if len(current) != 533 or len(current_rows) != 533 or set(current) != set(before):
        raise ValueError('Canonical registry membership changed')
    changed = []
    for name, old in before.items():
        new = current[name]
        if name not in NAMES:
            if new != old:
                raise ValueError('Unapproved company configuration changed: ' + name)
        else:
            allowed = {'identity_regex', 'allowed_apply_hosts'}
            if {k:v for k,v in old.items() if k not in allowed} != {k:v for k,v in new.items() if k not in allowed}:
                raise ValueError('Unreviewed field changed: ' + name)
        if new != old:
            changed.append(name)
    if len(changed) != 8:
        raise ValueError('Expected eight row changes and two code-only recoveries')
    baseline_targets = {t.name:t for t in load_baseline(before_rows)}
    targets = registry.load_target_companies(apply_reliability_repairs=True)
    effective = {t.name:t for t in targets}
    if len({t.source_scope_key for t in targets}) != 533:
        raise ValueError('Duplicate logical identity or scope')
    certificates = read_json('certificates.json')
    sources = read_json('canary_sources.json')
    if tuple(s['company'] for s in sources) != NAMES or tuple(c['company'] for c in certificates) != NAMES:
        raise ValueError('Certificate membership or order changed')
    jobs = requests = 0
    for certificate, source in zip(certificates, sources, strict=True):
        if digest(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()) != certificate['source_spec_sha256']:
            raise ValueError('Original canary source specification fingerprint mismatch')
        target = effective[source['company']]
        if asdict(target) != asdict(CompanyTarget.from_dict(source['candidate_row'])):
            raise ValueError('Effective production target differs from certificate')
        if source_health_key(target) != certificate['source_config_key'] or routing.mode(target) != 'reviewed':
            raise ValueError('Certified source no longer selects its exact reviewed engine')
        if target.search_url in sf._REVIEWED_SELF_LOCALE_ANCHORS:
            raise ValueError('A locale exception affects a certified source')
        path = root / PACKAGE / certificate['certificate']
        if digest(path.read_bytes()) != certificate['certificate_sha256']:
            raise ValueError('Historical certificate changed')
        row = json.loads(path.read_text()); validate_result(row, source)
        jobs += row['jobs_collected']; requests += row['physical_requests_accounted']
    for target in targets:
        if target.name not in NAMES and (asdict(target) != asdict(baseline_targets[target.name]) or routing.mode(target) != 'baseline'):
            raise ValueError('Nonactivation effective configuration or dispatch changed')
    if any(routing.mode(effective[n]) != 'baseline' for n in BASELINE_COMPANIES):
        raise ValueError('Existing identity-impact source left baseline engine')
    if len({effective[n].search_url for n in NAMES}) != 10 or jobs != 520 or requests != 27:
        raise ValueError('Recovery identity or retained accounting changed')
    return {'status':'PASS', 'activation_companies':list(NAMES), 'raw_rows_changed':len(changed),
            'code_only_companies':['Gibson Energy','Hydro One'], 'unchanged_companies':523,
            'effective_sources':533, 'parser_counts':dict(Counter(routing.mode(t) for t in targets)),
            'certificates_applicable':10, 'retained_jobs':jobs, 'retained_success_physical_requests':requests,
            'canary_engine_ast_equivalent_for_certified_sources':True, 'baseline_identity_impacts_preserved':list(BASELINE_COMPANIES),
            'new_live_requests':0, 'external_writes':0, 'production_activation_executed':False}


def registry_before_activation(production, root=ROOT):
    """Compose immutable historical checks only after the entire new activation passes."""
    from copy import deepcopy
    validate(root)
    actual = yaml.safe_load((Path(root) / 'config/target_companies.yaml').read_text())
    if production != actual:
        raise ValueError('Supplied production payload differs from the validated actual registry')
    rollback = yaml.safe_load((Path(root) / PACKAGE / 'source_rollback.yaml').read_text())
    old = {row['name']: row for row in rollback['target_companies']}
    restored = deepcopy(production)
    restored['target_companies'] = [deepcopy(old.get(row['name'], row)) for row in production['target_companies']]
    if restored['target_companies'] != read_json('baseline_raw_rows.json', Path(root)):
        raise ValueError('SuccessFactors rollback cannot reconstruct the exact prior registry')
    return restored


if __name__ == '__main__':
    print(json.dumps(validate(), indent=2))
