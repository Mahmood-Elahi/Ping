"""Validate the eight-source PR against retained live evidence; no HTTP."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import pytest
import yaml
from scripts import validate_recovery_eight_activation as activation
from scripts import validate_successfactors_ten_activation as historical

@pytest.fixture
def payload():
    return yaml.safe_load((activation.ROOT/'config/target_companies.yaml').read_text())

def test_eight_live_certificates_and_2110_jobs_apply_to_production():
    result=activation.validate()
    assert result['verified_jobs']==2110 and result['other_sources_unchanged']==525
    assert result['new_live_requests']==result['production_writes']==0
    assert result['retained_success_requests']==109

@pytest.mark.parametrize('name',activation.NAMES)
def test_exact_post_preprocessing_target_and_stable_identity(name,payload):
    proof=activation.evidence();cert=next(c for c in proof['certificates'] if c['source']['company']==name)
    after=activation.materialize(payload)[name]
    before=activation.materialize(activation.registry_before_eight(payload))[name]
    assert asdict(after)==cert['source']['exact_preprocessed_target']
    assert after.name==before.name and after.source_scope_key==before.source_scope_key
    assert (after.tenant,after.site_id,after.wd_subdomain,after.applied_facets)==(before.tenant,before.site_id,before.wd_subdomain,before.applied_facets)
    assert (after.max_pages,after.max_jobs)==(before.max_pages,before.max_jobs)

@pytest.mark.parametrize('field,value',[('source_scope','other'),('tenant','other'),('site_id','Other'),('applied_facets',{}),('max_jobs',1)])
def test_activation_rejects_target_tampering(payload,field,value):
    row=next(r for r in payload['target_companies'] if r['name']=='Target Tech');row[field]=value
    with pytest.raises(ValueError):activation.validate(payload)

@pytest.mark.parametrize('name',['ZF Friedrichshafen','Celestica','Airbnb'])
def test_no_failed_or_unrelated_source_changes(payload,name):
    row=next(r for r in payload['target_companies'] if r['name']==name);row['source_scope']='other'
    with pytest.raises(ValueError,match='Unrelated source changed'):activation.validate(payload)

def test_historical_ten_certificates_and_exact_predecessor_still_validate(payload):
    result=historical.validate()
    assert result['certificates_applicable']==10 and result['retained_jobs']==520
    prior=historical.registry_before_activation(payload)
    expected=json.loads((historical.ROOT/historical.PACKAGE/'baseline_raw_rows.json').read_text())
    assert prior['target_companies']==expected

@pytest.mark.parametrize('mutation',['incomplete','duplicate','engine','bundle'])
def test_evidence_and_engine_fail_closed(monkeypatch,mutation):
    if mutation=='bundle':
        monkeypatch.setattr(activation,'BUNDLE_SHA256','0'*64)
    else:
        proof=deepcopy(activation.evidence())
        if mutation=='incomplete':proof['certificates'][0]['result']['scan_complete']=False
        if mutation=='duplicate':proof['certificates'][1]['verified_identity_accounting']['unique_urls']-=1
        if mutation=='engine':proof['engine_files']['src/scraper/company_careers/adapters/workday.py']='0'*64
        monkeypatch.setattr(activation,'evidence',lambda:proof)
    with pytest.raises(ValueError):activation.validate()
