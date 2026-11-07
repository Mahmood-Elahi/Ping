"""Offline production applicability of eight retained PASS_COMPLETE sources."""
import ast
import base64
from copy import deepcopy
from dataclasses import asdict
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.scraper.company_careers import registry
from src.scraper.company_careers.student_roles import configure_student_targets
from src.scraper.company_careers.source_cooldown import source_health_key
PACKAGE=ROOT/'reports/recovery-eight-activation-2026-11-05'
NAMES=('ENMAX','Rockwell Automation','Thomson Reuters','Canadian Tire','EXFO',"Ontario Teachers' Pension Plan",'RBC','Target Tech')
BUNDLE_SHA256='e711926db9dab41daee7ff4dc918a6f69b4541219d06309983d933170de5a10a'
def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def evidence():
    raw=(PACKAGE/'evidence.json.gz.b64').read_bytes()
    if digest(raw)!=BUNDLE_SHA256:raise ValueError('Live evidence changed')
    proof=json.loads(gzip.decompress(base64.b64decode(raw)))
    if tuple(proof['names'])!=NAMES:raise ValueError('Activation roster changed')
    return proof

def materialize(payload):
    with tempfile.TemporaryDirectory(prefix='ping-eight-') as folder:
        path=Path(folder)/'targets.yaml';path.write_text(yaml.safe_dump(payload,sort_keys=False))
        with patch.object(registry,'DEFAULT_REGISTRY_PATH',str(path)):
            targets=registry.load_target_companies(str(path),apply_reliability_repairs=True)
        return {t.name:t for t in configure_student_targets(targets)}

def validate(payload=None):
    proof=evidence()
    if payload is None:payload=yaml.safe_load((ROOT/'config/target_companies.yaml').read_text())
    from scripts.validate_recovery_three_activation import registry_before_three
    payload=registry_before_three(payload)
    rows=payload['target_companies'];by_name={r['name']:r for r in rows}
    if len(rows)!=533 or len(by_name)!=533 or not set(NAMES).issubset(by_name):raise ValueError('Registry membership changed')
    if digest(canonical([r for r in rows if r['name'] not in NAMES]))!=proof['other_525_sha256']:raise ValueError('Unrelated source changed')
    repairs=yaml.safe_load((ROOT/registry.DEFAULT_RELIABILITY_REPAIRS_PATH).read_text())['company_careers_reliability_repairs']
    if any(r['name'] in NAMES for r in repairs):raise ValueError('Unreviewed activation overlay')
    for path,expected in proof['engine_files'].items():
        if digest((ROOT/path).read_bytes())!=expected:raise ValueError('Collection engine changed: '+path)
    for path,expected in proof['engine_nodes'].items():
        nodes={n.name:ast.dump(n,include_attributes=False) for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
        if any(digest(nodes.get(name,'').encode())!=value for name,value in expected.items()):raise ValueError('Model/finalizer changed: '+path)
    targets=materialize(payload);jobs=requests=0
    if tuple(c['source']['company'] for c in proof['certificates'])!=NAMES:raise ValueError('Certificate roster changed')
    for cert in proof['certificates']:
        source,row=cert['source'],cert['result'];name=source['company'];target=targets[name]
        old=proof['rollback'][name]['raw_row'];allowed={'workday_identity','search_url'}
        if {k:v for k,v in by_name[name].items() if k not in allowed}!={k:v for k,v in old.items() if k not in allowed}:raise ValueError('Source scope/identity/bounds changed: '+name)
        if asdict(target)!=source['exact_preprocessed_target'] or source_health_key(target)!=source['source_config_key']:raise ValueError('Tested target/preprocessing mismatch: '+name)
        expected={'company':name,'platform':'workday','source_scope':'default','source_scope_key':target.source_scope_key,'source_config_key':source_health_key(target),'canary_outcome':'PASS_COMPLETE','scan_state':'complete','source_health_code':'healthy','source_identity_status':'validated','inventory_authority':'authoritative','result_authority':'authoritative_inventory'}
        if any(row.get(k)!=v for k,v in expected.items()):raise ValueError('Certificate identity/authority mismatch: '+name)
        true=('scan_complete','inventory_complete','authoritative_for_absence','can_reconcile_absence','terminal_evidence_valid','hidden_continuation_absent','stable_ids_unique','canonical_urls_unique','normalized_jobs_attributable','apply_urls_contract_valid')
        if any(row.get(k) is not True for k in true):raise ValueError('Incomplete certificate: '+name)
        if any(row.get(k) is not False for k in ('safety_limit_reached','collection_changed_during_scan','physical_count_is_upper_bound')):raise ValueError('Unsafe certificate: '+name)
        if any(type(row.get(k)) is not int or row[k]!=0 for k in ('unreadable_items','rejected_items','rejected_record_count')):raise ValueError('Rejected/unreadable inventory: '+name)
        counts=[row.get(k) for k in ('jobs_collected','inventory_items_collected','total_available')]
        if any(type(n) is not int or n<0 for n in counts) or len(set(counts))!=1:raise ValueError('Inventory accounting mismatch: '+name)
        accounting=cert['verified_identity_accounting']
        if accounting['identity_sets_equal'] is not True or any(accounting[k]!=counts[0] for k in ('inventory_count','normalized_count','unique_urls')):raise ValueError('Retained job identity mismatch: '+name)
        if any(len(accounting[k])!=64 for k in ('identity_pairs_sha256','normalized_jobs_sha256')):raise ValueError('Missing private identity proof: '+name)
        count=row.get('physical_requests_accounted')
        if type(count) is not int or not 0<count<=source['physical_request_cap'] or row.get('requests_started')!=count:raise ValueError('Physical accounting mismatch: '+name)
        jobs+=counts[0];requests+=count
    if jobs!=2110:raise ValueError('Verified job total changed')
    return {'status':'PASS','companies':list(NAMES),'verified_jobs':jobs,'retained_success_requests':requests,'production_preprocessing_exact':True,'certified_engine_applicable':True,'other_sources_unchanged':525,'new_live_requests':0,'production_writes':0,'activation_executed':False}

def registry_before_eight(payload):
    """Validate the new activation before composing older immutable proofs."""
    from scripts.validate_recovery_three_activation import registry_before_three
    payload=registry_before_three(payload)
    validate(payload);proof=evidence();restored=deepcopy(payload)
    restored['target_companies']=[deepcopy(proof['rollback'][r['name']]['raw_row']) if r['name'] in NAMES else r for r in payload['target_companies']]
    return restored

def historical_registry(payload):
    restored=registry_before_eight(payload)
    raw=evidence()['predecessor_registry_utf8'].encode()
    if yaml.safe_load(raw)!=restored:raise ValueError('Exact predecessor bytes do not match rollback')
    return restored,raw

if __name__=='__main__':print(json.dumps(validate(),indent=2))
