from dataclasses import replace

import pytest

from scripts import validate_followup_recovery as cert
from src.scraper.company_careers.adapters import smartrecruiters
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.registry import load_target_companies


def test_five_certificates_and_exact_rollback():
    assert cert.validate() == {"Bosch":5178,"AIMCo":7,"OpenAI":870,"ZF Friedrichshafen":776,"Celestica":1076}
    raw=(cert.ROOT/cert.REGISTRY).read_bytes()
    assert cert.digest(cert.prior_registry_bytes(raw))==cert.certificate()["registry_old"]
    with pytest.raises(ValueError):
        cert.prior_registry_bytes(raw+b"\n")
    for name,hashes in cert.certificate()["adapters"].items():
        assert cert.digest(cert.prior_adapter_bytes(name,(cert.ROOT/name).read_bytes()))==hashes["old"]


def test_bosch_bounds_stay_tied_to_exact_scope_and_listing_mode():
    target=next(t for t in load_target_companies(apply_reliability_repairs=True) if t.name=="Bosch")
    smartrecruiters.validate_target(target)
    for changed in (replace(target,name="Other"),replace(target,source_scope="student"),replace(target,max_jobs=6501),
                    replace(target,selective_detail_enrichment=True)):
        with pytest.raises(AdapterError):
            smartrecruiters.validate_target(changed)
