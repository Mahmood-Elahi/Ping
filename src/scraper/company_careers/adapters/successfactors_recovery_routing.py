"""Explicit source-contract opt-in; every non-opted source keeps production84.

No report/candidate registry is read by normal runtime. The sealed profile table
binds exact baseline and reviewed identities. A changed known recovery contract
fails closed instead of silently selecting the older implementation.
"""
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from src.scraper.company_careers.models import AdapterError,CompanyTarget
from src.scraper.company_careers.source_cooldown import source_health_key
from src.utils.source_scope import normalize_canonical_company

# Existing source identity excludes operational budgets; implementation selection
# does too. Identity, scope, authority, markers and every other target field stay exact.
OPERATIONAL=frozenset({'max_pages','max_jobs','max_response_bytes','source_activation'})
PROFILES=Path(__file__).with_name('successfactors_recovery_profiles.json')


def contract(target:CompanyTarget)->dict:
    return {k:v for k,v in asdict(target).items() if k not in OPERATIONAL}


def contract_hash(target:CompanyTarget)->str:
    return hashlib.sha256(json.dumps(contract(target),sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


@lru_cache(maxsize=1)
def profiles():
    value=json.loads(PROFILES.read_text())
    if value['schema_version']!=1:raise AdapterError('SuccessFactors recovery profile identity schema is invalid')
    entries=value['sources']
    if len({normalize_canonical_company(e['company']) for e in entries})!=len(entries):raise AdapterError('SuccessFactors recovery profile identity is duplicated')
    return {normalize_canonical_company(e['company']):e for e in entries}


def mode(target:CompanyTarget)->str:
    if target.platform!='sap_successfactors':return 'baseline'
    profile=profiles().get(normalize_canonical_company(target.name))
    if profile is None:return 'baseline'
    actual=contract_hash(target)
    for name in ('reviewed','baseline'):
        expected=profile[name]
        if actual==expected['contract_sha256'] and source_health_key(target)==expected['source_config_key']:
            return name
    raise AdapterError('SAP SuccessFactors reviewed source profile identity did not match its exact contract')


def is_reviewed(target)->bool:
    if target is None:return False
    try:return mode(target)=='reviewed'
    except AdapterError:return False
