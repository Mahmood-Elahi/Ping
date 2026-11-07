"""Retained live applicability, isolation, and real adapter regressions; no network."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest
import requests
import yaml

from scripts import validate_recovery_three_activation as activation
from src.scraper.company_careers.adapters import sap_successfactors as sf
from src.scraper.company_careers.adapters.successfactors_table_recovery import is_reviewed
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from tests.recovery_three_fixtures import SnapshotSession
from scripts import validate_source_integrity_canaries as canary


@pytest.fixture
def payload():
    return yaml.safe_load((activation.ROOT / 'config/target_companies.yaml').read_text())


def test_retained_live_identity_accounting_and_exact_rollback(payload):
    assert activation.validate()['verified_jobs'] == 2135
    before = activation.registry_before_three(payload)
    assert before == yaml.safe_load(activation.evidence()['predecessor_registry_utf8'])


@pytest.mark.parametrize('name', activation.NAMES)
@pytest.mark.parametrize('field,value', [('source_scope','other'), ('max_jobs',1), ('allowed_apply_hosts',['foreign.test'])])
def test_target_drift_cannot_activate(payload, name, field, value):
    next(r for r in payload['target_companies'] if r['name']==name)[field] = value
    with pytest.raises(ValueError):
        activation.validate(payload)


@pytest.mark.parametrize('name', ['ZF Friedrichshafen','Celestica','Airbnb','Keyence'])
def test_other_530_rows_unchanged(payload,name):
    next(r for r in payload['target_companies'] if r['name']==name)['source_scope']='other'
    with pytest.raises(ValueError,match='Unrelated source changed'):
        activation.validate(payload)


@pytest.mark.parametrize('mutation',['incomplete','inventory','engine','bundle'])
def test_evidence_drift_fails_closed(monkeypatch,mutation):
    if mutation=='bundle':
        monkeypatch.setattr(activation,'BUNDLE_SHA256','0'*64)
    else:
        proof=deepcopy(activation.evidence())
        if mutation=='incomplete': proof['certificates'][0]['result']['scan_complete']=False
        if mutation=='inventory': proof['certificates'][2]['verified_identity_accounting']['unique_urls']-=1
        if mutation=='engine': proof['engine_files']['src/scraper/company_careers/adapters/workday.py']='0'*64
        monkeypatch.setattr(activation,'evidence',lambda:proof)
    with pytest.raises(ValueError): activation.validate()


def test_lam_profile_is_exact_and_cannot_change_employer_or_scope():
    target=CompanyTarget.from_dict(activation.evidence()['certificates'][2]['source']['exact_preprocessed_target'])
    assert is_reviewed(target)
    for changed in (replace(target,name='Other'),replace(target,source_scope='other'),replace(target,identity_regex='.*')):
        with pytest.raises(AdapterError): is_reviewed(changed)


def test_supported_runner_and_launch_before_credentials():
    text=(activation.ROOT/'.github/workflows/hourly-job-radar.yml').read_text()
    assert 'runs-on: ubuntu-24.04' in text
    assert 'ubuntu-latest' not in text
    assert 'ubuntu-24.04-${{ runner.arch }}-playwright' in text
    assert text.index('Verify Chromium launches') < text.index('Prepare production credentials')
    assert 'page.set_content(' in text and 'page.goto(' not in text
    assert '17 */6 * * *' in text


@pytest.mark.parametrize('scenario', ['success','zero','pagination','duplicates','conflicting_id',
    'missing_total','zero_missing_total','repeated_page','malformed','foreign_apply','bad_next',
    'missing_next','premature_short','identity','mutation_once','second_mutation','timeout'])
def test_lam_real_adapter_finalization(scenario,monkeypatch):
    target=CompanyTarget.from_dict(activation.evidence()['certificates'][2]['source']['exact_preprocessed_target'])
    session=SnapshotSession('Lam Research',scenario)
    monkeypatch.setattr(sf.time,'sleep',lambda *_:None)
    monkeypatch.setattr(canary,'BoundedSession',lambda *a,**k:session)
    row=canary.run_canary(target,40)
    passed=scenario in {'success','zero','pagination','mutation_once'}
    assert (row['canary_outcome']=='PASS_COMPLETE') is passed,row
    assert row['authoritative_for_absence'] is passed
    assert row['can_reconcile_absence'] is passed
    assert row['persistence_mode']=='none'
