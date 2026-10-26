"""Validate the approved 20-source activation and immutable live evidence offline.

No network, production writes, lifecycle updates, or canary execution. The old
activation package can reconstruct its historical registry through this module.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_cooldown import source_health_key

PACKAGE = Path('reports/workday-recovery-batch1-activation-2026-10-26')
APPROVED_MANIFEST = '9e4f501d72874fa7bbe973b0a189882f4d39981606369dad90bea0344124ae35'
# Hashes of the original evidence copies and the reviewed rollback/comparison.
ARTIFACT_HASHES = {'activation.patch': '67dbc832716b4f4fc51cf58d413b6207a3ec2253b47339532e769f65bb576426',
 'activation_proposal.yaml': '322951f49c338bfaff94d1be9e4e2fa67b7de613510fc097537b653a5ce6cf96',
 'baseline.json': 'c4bf4c8ca9d690906f406b3b3c562284576191a80be8362becf73cef1263ffba',
 'comparison.json': 'e892f8c79d1de9f2eac883d3f1ccdd5ca1e3b42995ae73d68708c2e557b4f936',
 'evidence.json': 'd9568f0eb6c23d1a3c59fddf2d7eb8b825bdd88f8ca8593074e9a16bd262939f',
 'execution_report.json': '48bf78f608efff0310c945df22882019023e47802e2e723eeb30b1a7183b1a9e',
 'manifest.json': '9e4f501d72874fa7bbe973b0a189882f4d39981606369dad90bea0344124ae35',
 'postflight.json': '55a3f725370e4e50ca6592edf1ab5e2b56e03ce2a24fa5cbfa2b4fb15f5ebaba',
 'preflight.json': '9f787a36aab58d5074f2d46f222c33014cade53d63b21d26bbc0e1302c601a37'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def read(path):
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def baseline_record(root=ROOT):
    return read(root / PACKAGE / 'baseline.json')


def validate_result(result, source):
    """Require the complete positive contract; a label alone is insufficient."""
    require(result['company'] == source['company'], 'Result employer mismatch')
    require(result['source_scope'] == source['source_scope'], 'Result scope mismatch')
    require(result['source_config_key'] == source['source_config_key'], 'Result fingerprint mismatch')
    for field in ('scan_complete', 'authoritative_for_absence', 'can_reconcile_absence',
                  'inventory_complete', 'terminal_evidence_valid', 'hidden_continuation_absent',
                  'stable_ids_unique', 'canonical_urls_unique', 'normalized_jobs_attributable',
                  'apply_urls_contract_valid', 'activation_proposal_eligible'):
        require(result.get(field) is True, f'{source["company"]}: missing positive {field}')
    for field in ('collection_changed_during_scan', 'safety_limit_reached',
                  'batch_access_stopped', 'physical_count_is_upper_bound'):
        require(result.get(field) is False, f'{source["company"]}: invalid {field}')
    for field in ('unreadable_items', 'rejected_items', 'rejected_record_count'):
        require(type(result.get(field)) is int and result[field] == 0, f'Rejected/unreadable {field}')
    for field, value in {'canary_outcome':'PASS_COMPLETE', 'scan_state':'complete',
                         'source_health_code':'healthy', 'inventory_authority':'authoritative',
                         'result_authority':'authoritative_inventory', 'source_identity_status':'validated',
                         'terminal_evidence_type':'authoritative_total',
                         'stop_reason':'reported_total_reached',
                         'request_count_unit':'physical_http_attempts'}.items():
        require(result.get(field) == value, f'{source["company"]}: invalid {field}')
    total = result['total_available']
    require(type(total) is int and total >= 0, 'Missing authoritative integer total')
    require(result['inventory_items_collected'] == result['jobs_collected'] == total, 'Inventory mismatch')
    require(result.get('failure_category') is None, 'Result contains failure')
    requests = result['requests_started']
    require(type(requests) is int and 0 < requests <= source['physical_request_cap'], 'Request cap exceeded')
    require(requests == result['physical_requests_accounted'], 'Unaccounted physical requests')
    require(result['request_budget'] == source['physical_request_cap'], 'Budget mismatch')


def restored_registry(production, proposal):
    data = deepcopy(production)
    old = proposal['rollback']
    data['target_companies'] = [deepcopy(old[r['name']]['prior_production_row'])
                                if r['name'] in old else r for r in data['target_companies']]
    return data


def historical_registry_sha256():
    """Pin a historical check without rejecting this separately approved change."""
    validate_package()
    return baseline_record()['sha256']['config/target_companies.yaml']


def registry_before_recovery():
    """Historical view only after the entire superseding activation validates."""
    validate_package()
    return restored_registry(read(ROOT / 'config/target_companies.yaml'),
                             read(ROOT / PACKAGE / 'activation_proposal.yaml'))


def validate_package(root=ROOT):
    package = root / PACKAGE
    for filename, expected in ARTIFACT_HASHES.items():
        require(hashlib.sha256((package / filename).read_bytes()).hexdigest() == expected,
                f'Immutable activation artifact changed: {filename}')
    manifest = read(package / 'manifest.json')
    require(hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest() == APPROVED_MANIFEST,
            'Approved canary manifest changed')
    proposal = read(package / 'activation_proposal.yaml')
    evidence = read(package / 'evidence.json')
    baseline = baseline_record(root)
    sources = manifest['sources']
    names = [s['company'] for s in sources]
    require(len(names) == len(set(names)) == 20, 'Expected the twenty approved companies')
    require([r['name'] for r in proposal['target_companies']] == names, 'Proposal source set/order mismatch')
    require(list(proposal['rollback']) == names, 'Rollback source set/order mismatch')
    require([e['company'] for e in evidence['sources']] == names, 'Evidence source set/order mismatch')
    for filename, expected in evidence['original_files_sha256'].items():
        require(hashlib.sha256((package/filename).read_bytes()).hexdigest() == expected,
                f'Original live evidence changed: {filename}')
    postflight = read(package / 'postflight.json')
    require(postflight['postflight'] == 'PASS' and postflight['execution_attempts'] == 1, 'Live postflight failed')
    for field in ('protected_files_unchanged', 'working_tree_unchanged_during_execution',
                  'historical_rollback_evidence_unchanged', 'proposal_exactly_matches_independently_eligible_sources'):
        require(postflight[field] is True, f'Failed historical postflight: {field}')
    production = read(root / 'config/target_companies.yaml')
    rows = production['target_companies']
    require(len(rows) == 533, 'Production source count changed')
    raw = {r['name']: r for r in rows}
    require(len(raw) == 533, 'Duplicate canonical employer')
    scope_keys = [(' '.join(r['name'].casefold().split()), str(r.get('source_scope','default')).casefold()) for r in rows]
    require(len(set(scope_keys)) == len(rows), 'Duplicate source scopes')
    restored = restored_registry(production, proposal)
    require(digest(restored) == baseline['registry_structural_sha256'], 'Rollback cannot reconstruct historical registry')
    require(digest([r for r in rows if r['name'] not in names]) == baseline['unrelated_raw_rows_sha256'],
            'Unrelated company configuration changed')
    overlay = read(root / 'config/company_careers_reliability_repairs_v1.yaml')
    repairs = overlay['company_careers_reliability_repairs']
    require(len(repairs) == 74 and all(r['name'] not in names for r in repairs), 'Candidate overridden by overlay')
    restored_overlay = {r['name']: r for r in repairs}
    for name in names:
        old = proposal['rollback'][name]['prior_overlay_row']
        if old is not None:
            restored_overlay[name] = old
    require(set(restored_overlay) == set(baseline['overlay_order']), 'Overlay membership changed')
    require(digest({'company_careers_reliability_repairs':[restored_overlay[n] for n in baseline['overlay_order']]})
            == baseline['overlay_structural_sha256'], 'Rollback cannot reconstruct exact original overlays')
    effective_rows = [next((o for o in repairs if o['name']==r['name']),r) for r in rows]
    require(digest([r for r in effective_rows if r['name'] not in names]) == baseline['unrelated_effective_rows_sha256'],
            'Unrelated effective configuration changed')
    providers = [(str(r.get('tenant','')).casefold(),str(r.get('wd_subdomain','')).casefold(),str(r.get('site_id','')).casefold())
                 for r in effective_rows if r.get('platform') == 'workday']
    require(len(providers)==len(set(providers)), 'Overlapping Workday recruiting boards')
    # Actual production loader includes overlay precedence, safety migration and activation assessment.
    # This utility is invoked from the repository root, matching production's loader contract.
    require(Path.cwd().resolve() == root.resolve(), 'Run from the repository root')
    effective = {t.name:t for t in load_target_companies(apply_reliability_repairs=True)}
    requests = jobs = 0
    for source, tested, proof in zip(sources, proposal['target_companies'], evidence['sources'], strict=True):
        name = source['company']
        require(tested == source['candidate_row'], f'Tested candidate drift: {name}')
        require(digest(tested) == source['candidate_row_sha256'], f'Candidate digest drift: {name}')
        candidate = CompanyTarget.from_dict(tested)
        require(asdict(effective[name]) == asdict(candidate), f'Effective production differs from tested source: {name}')
        require(source_health_key(candidate) == source['source_config_key'], f'Configuration fingerprint drift: {name}')
        prior = proposal['rollback'][name]['prior_production_row']
        for field in ('name','platform','tenant','wd_subdomain','site_id'):
            require(raw[name].get(field) == prior.get(field), f'Provider identity migrated: {name}/{field}')
        require(raw[name].get('applied_facets',{}) == prior.get('applied_facets',{}), f'Scope facets changed: {name}')
        require(raw[name].get('source_scope','default') == prior.get('source_scope','default'), f'Scope migrated: {name}')
        for field in ('max_pages','max_jobs'):
            require(raw[name].get(field) == prior.get(field), f'Legacy safety bound rewritten: {name}')
        hostname = f'{candidate.tenant}.{candidate.wd_subdomain}.myworkdayjobs.com'
        require(workday._configured_identity_evidence(candidate,candidate.site_id,hostname)[0] == 'validated',
                f'Unvalidated reviewed identity: {name}')
        observation = proof['official_evidence']['observation']
        observed = urlparse(observation['observed_target'])
        require(observation['company'] == name and observation['official_url'] == candidate.careers_url,
                f'Official employer evidence mismatch: {name}')
        require(observed.scheme == 'https' and observed.hostname == hostname and
                observed.path.strip('/').removeprefix('en-US/') == candidate.site_id,
                f'Official board ownership mismatch: {name}')
        validate_result(proof['result'], source)
        require(postflight['evidence_sha256'][Path(proof['result_file']).name] == proof['result_sha256'], 'Result provenance mismatch')
        requests += proof['result']['requests_started']; jobs += proof['result']['total_available']
    require(requests == postflight['physical_requests'] == 76 and jobs == postflight['total_inventory_items'] == 1303,
            'Live aggregate accounting mismatch')
    require(hashlib.sha256((root/'src/scraper/company_careers/adapters/workday.py').read_bytes()).hexdigest()
            == manifest['protected_artifacts']['src/scraper/company_careers/adapters/workday.py'], 'Tested Workday runtime changed')
    return {'status':'PASS','sources':20,'unchanged_companies':513,'live_jobs_accounted':jobs,
            'live_physical_requests':requests,'network_requests':0,'production_writes':0}


if __name__ == '__main__':
    print(json.dumps(validate_package(), indent=2))
