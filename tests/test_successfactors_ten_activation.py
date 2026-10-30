"""Activation contract, exact historical proof and fail-closed runtime fixtures."""
from copy import deepcopy
from functools import lru_cache
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest
import requests
import yaml

from scripts import validate_source_integrity_canaries as canary
from scripts import validate_successfactors_ten_activation as activation
from src.scraper.company_careers.adapters import sap_successfactors as sf
from src.scraper.company_careers.adapters import sap_successfactors_baseline_84b1655 as baseline
from src.scraper.company_careers.adapters import successfactors_recovery_routing as routing
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies
from tests.successfactors_activation_fixtures import CANDIDATES, SnapshotSession, Response, table

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/activation.PACKAGE

@lru_cache(maxsize=1)
def targets():
    return {t.name:t for t in load_target_companies(apply_reliability_repairs=True)}

@pytest.fixture(autouse=True)
def no_delays(monkeypatch):
    monkeypatch.setattr(sf.time,'sleep',lambda *_:None)


def test_exact_ten_activation_all533_and_certificate_applicability():
    result=activation.validate()
    assert result['certificates_applicable']==10 and result['retained_jobs']==520
    assert result['retained_success_physical_requests']==27
    assert result['raw_rows_changed']==8 and result['unchanged_companies']==523
    assert result['parser_counts']=={'baseline':523,'reviewed':10}
    assert result['new_live_requests']==0


@pytest.mark.parametrize('name',activation.NAMES)
@pytest.mark.parametrize('scenario',['success','zero','pagination','premature_short','missing_total','zero_missing_total',
                                    'duplicates','conflicting_id','repeated_page','malformed','foreign_apply',
                                    'missing_next','bad_next','timeout','mutation_once','second_mutation','identity'])
def test_ten_exact_runtime_inventory_and_absence_gates(name,scenario,monkeypatch):
    target=targets()[name]
    session=SnapshotSession(name,scenario)
    monkeypatch.setattr(canary,'BoundedSession',lambda *a,**kw:session)
    result=canary.run_canary(target,40)
    passed=scenario in {'success','zero','pagination','mutation_once'}
    assert (result['canary_outcome']=='PASS_COMPLETE') is passed,(name,scenario,result)
    assert result['authoritative_for_absence'] is passed
    if passed:
        expected={'success':3,'zero':0,'pagination':26,'mutation_once':26}[scenario]
        assert result['jobs_collected']==result['inventory_items_collected']==result['total_available']==expected
        assert result['source_identity_status']=='validated' and result['terminal_evidence_valid'] is True
    if scenario=='bad_next':assert len(session.calls)==1
    assert all('locale' not in url for url in session.calls)
    assert result['persistence_mode']=='none'


@pytest.mark.parametrize('name',activation.NAMES)
def test_physical_canary_bound_never_becomes_terminal(name,monkeypatch):
    class BoundedSnapshot(SnapshotSession):
        def get(self,url,**kwargs):
            if self.requests_started>=1:raise canary.CanaryBudgetReached('fixture physical request cap')
            return super().get(url,**kwargs)
    session=BoundedSnapshot(name,'pagination');target=CompanyTarget.from_dict(CANDIDATES[name])
    monkeypatch.setattr(canary,'BoundedSession',lambda *a,**kw:session)
    row=canary.run_canary(target,1)
    assert row['canary_outcome']!='PASS_COMPLETE' and row['requests_started']==1
    assert not row['scan_complete'] and not row['authoritative_for_absence']


@pytest.mark.parametrize('name',activation.NAMES)
@pytest.mark.parametrize('field,value',[('identity_regex','.*'),('source_scope','other'),('allowed_apply_hosts',['evil.example']),
                                     ('search_url','https://evil.example/search/'),('inventory_authority','discovery_only')])
def test_exact_profile_alteration_never_falls_back_or_sends_request(name,field,value):
    target=CompanyTarget.from_dict({**CANDIDATES[name],field:value});session=SnapshotSession(name,'zero')
    with pytest.raises(AdapterError,match='profile identity'):sf.fetch_jobs(session,target)
    assert session.requests_started==0


@pytest.mark.parametrize('alias',[' KEYENCE ','Ｋｅｙｅｎｃｅ','Keyence\u00a0'])
def test_canonical_alias_does_not_bypass_exact_contract(alias):
    target=CompanyTarget.from_dict(CANDIDATES['Keyence']);target.name=alias
    with pytest.raises(AdapterError,match='profile identity'):routing.mode(target)


@pytest.mark.parametrize('name',activation.BASELINE_COMPANIES)
@pytest.mark.parametrize('scenario',['zero','success','malformed','missing_total','foreign_apply','timeout'])
def test_five_existing_sources_preserve_exact_baseline_behavior(name,scenario):
    target=targets()[name]
    def run(fn):
        session=SnapshotSession('Keyence',scenario)
        try:
            result=fn(session,target)
            return ('result',[(j.job_id,j.url,j.company) for j in result.jobs],dict(result.scan),session.calls)
        except Exception as exc:
            return ('error',type(exc).__name__,str(exc),session.calls)
    assert routing.mode(target)=='baseline'
    assert run(sf.fetch_jobs)==run(baseline.fetch_jobs)


@pytest.fixture(scope='module')
def canary_engine():
    path=PACKAGE/'evidence/canary_adapter.py.txt'
    from importlib.machinery import SourceFileLoader
    loader=SourceFileLoader('sf_certified_historical_engine',str(path))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;loader.exec_module(module)
    return module


@pytest.mark.parametrize('name',activation.NAMES)
@pytest.mark.parametrize('scenario',['success','zero','pagination','duplicates','missing_total','bad_next','mutation_once','second_mutation','timeout'])
def test_certified_engine_and_proposed_engine_have_identical_decisions(name,scenario,canary_engine):
    target=CompanyTarget.from_dict(CANDIDATES[name])
    def run(fn):
        session=SnapshotSession(name,scenario)
        try:
            out=fn(session,target)
            return ('ok',[(j.job_id,j.url,j.company,j.source_scope) for j in out.jobs],dict(out.scan),session.calls)
        except Exception as exc:return ('error',type(exc).__name__,str(exc),session.calls)
    assert run(sf.fetch_jobs)==run(canary_engine.fetch_jobs)


@pytest.mark.parametrize('field,value',[('canary_outcome','FAIL_INCOMPLETE'),('company','Other Employer'),('source_scope','other'),
 ('source_config_key','other'),('source_identity_status','unproven'),('inventory_items_collected',10),('total_available',None),
 ('scan_complete',False),('can_reconcile_absence',False),('terminal_evidence_valid',False),('rejected_items',1),
 ('physical_count_is_upper_bound',True),('requests_started',0),('collection_changed_during_scan',True)])
def test_rejected_or_mismatched_certificates_cannot_be_reused(field,value):
    certificate=activation.read_json('certificates.json')[0];row=json.loads((PACKAGE/certificate['certificate']).read_text())
    source=activation.read_json('canary_sources.json')[0];row[field]=value
    with pytest.raises(ValueError):activation.validate_result(row,source)


@pytest.mark.parametrize('record',activation.read_json('certificates.json'),ids=lambda c:c['company'])
def test_retained_certificate_bytes_are_immutable(record):
    assert hashlib.sha256((PACKAGE/record['certificate']).read_bytes()).hexdigest()==record['certificate_sha256']


@pytest.mark.parametrize('change',['schema','terminal','request'])
def test_code_applicability_ast_proof_rejects_unreviewed_engine_changes(change):
    text=(ROOT/'src/scraper/company_careers/adapters/sap_successfactors.py').read_text()
    if change=='schema':text=text.replace('has_layout = False','has_layout = True') if 'has_layout = False' in text else text.replace('PAGE_SIZE = 25','PAGE_SIZE = 26')
    if change=='terminal':text=text.replace('reported_total_reached','fabricated_total_reached')
    if change=='request':text=text.replace('MAX_ATTEMPTS = 2','MAX_ATTEMPTS = 3')
    assert activation.engine_ast(text,consolidated=True)!=activation.engine_ast((PACKAGE/'evidence/canary_adapter.py.txt').read_text())


def test_runtime_rollback_restores_exact_production_code_and_registry(tmp_path):
    record=activation.read_json('rollback_verification.json')
    for name,hashes in record['files'].items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        assert hashlib.sha256(path.read_bytes()).hexdigest()==hashes['after_sha256']
    patchfile=PACKAGE/'production_runtime_rollback.patch'
    subprocess.run(['git','apply','--unidiff-zero','--check',str(patchfile)],cwd=tmp_path,check=True)
    subprocess.run(['git','apply','--unidiff-zero',str(patchfile)],cwd=tmp_path,check=True)
    for name,hashes in record['files'].items():
        path=tmp_path/name;assert path.exists()==hashes['before_exists']
        if path.exists():assert hashlib.sha256(path.read_bytes()).hexdigest()==hashes['before_sha256']


def test_runtime_admission_remains_conditional_and_excludes_unverified_sources():
    record=activation.read_json('runtime_assessment.json')
    assert record['whole_job_projection_seconds']==pytest.approx(1723+122.345+180)
    assert record['whole_job_plus_incremental_RPC_scenario_seconds']<2700
    assert record['double_non_scan_plus_new_population_stress_seconds']<2700
    assert record['unconditional_production_time_guarantee'] is False
    assert record['actual_external_incremental_cost_measured'] is False
    assert record['remaining_eight_status']=='UNMEASURED_DO_NOT_EXTEND_CURRENT_TEN_ADMISSION_TO_EIGHT'


def test_current_pipeline_downstream_cost_measurement_uses_only_in_memory_client():
    from scripts.benchmark_successfactors_downstream import benchmark
    out=benchmark()
    assert out['jobs']==out['in_memory_tracked_rows']==520
    assert out['rpc_total']==31 and out['rpc_calls']['finalize_student_job_source_sync']==10
    assert out['external_requests']==out['external_writes']==0 and out['network_denied']


def test_workday_history_is_reconstructed_only_after_complete_successfactors_validation(monkeypatch):
    from scripts import validate_workday_recovery_activation as workday_activation
    production=yaml.safe_load((ROOT/'config/target_companies.yaml').read_text())
    restored=activation.registry_before_activation(production)
    assert restored['target_companies']==activation.read_json('baseline_raw_rows.json')
    assert workday_activation.production_before_successfactors()==restored
    def reject(*a,**kw):raise ValueError('fixture invalid newer activation')
    monkeypatch.setattr(activation,'validate',reject)
    with pytest.raises(ValueError,match='invalid newer activation'):
        workday_activation.production_before_successfactors()


@pytest.mark.parametrize('company',['Amazon','Hyundai'])
def test_historical_projection_does_not_hide_unapproved_configuration_changes(company):
    production=yaml.safe_load((ROOT/'config/target_companies.yaml').read_text())
    next(r for r in production['target_companies'] if r['name']==company)['unexpected_field']='bad'
    with pytest.raises(ValueError,match='differs from'):
        activation.registry_before_activation(production)
