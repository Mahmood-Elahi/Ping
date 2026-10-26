"""Production activation: every exact live-tested row, synthetic HTTP only."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
import yaml
from scripts import validate_source_integrity_canaries as canary
from scripts import validate_workday_recovery_activation as activation
from src.scraper.company_careers.models import CompanyTarget
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "reports/workday-recovery-batch1-activation-2026-10-26"
CANDIDATES = {r["name"]:r for r in yaml.safe_load((PACKAGE / "activation_proposal.yaml").read_text())["target_companies"]}
NAMES = tuple(CANDIDATES)

class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def item(index):
    return {"title": f"Fixture role {index}", "locationsText": "Canada",
            "externalPath": f"/job/Canada/Role_R{100000+index}",
            "bulletFields": [f"R{100000+index}"]}


class SnapshotSession:
    """Synthetic CXS, including exact existing facets; never performs I/O."""
    last_final_url = None
    last_redirect_count = 0

    def __init__(self, target, scenario):
        self.target, self.scenario = target, scenario
        self.requests_started = 0
        self.calls = []

    def close(self):
        pass

    def post(self, url, **kwargs):
        self.requests_started += 1
        body = kwargs["json"]
        self.calls.append((url, body))
        offset = body["offset"]
        total = 21 if self.scenario in {"pagination", "limit", "mutation", "repeated_page", "conflicting_id", "short_page"} else 3
        if self.scenario in {"zero", "zero_missing_total"}:
            total = 0
        items = [item(i) for i in range(total)][offset:offset+20]
        if self.scenario == "duplicates":
            items[-1] = deepcopy(items[0])
        if self.scenario == "malformed":
            items[0] = {"title": "Missing stable identity"}
        if self.scenario == "repeated_page" and offset:
            items = [item(i) for i in range(20)]
        if self.scenario == "conflicting_id" and offset:
            items = [{**item(0), "title": "Conflicting title"}]
        if self.scenario == "short_page":
            items = items[:2]
        payload = {"total": total, "jobPostings": items}
        if self.scenario in {"missing_total", "zero_missing_total"}:
            payload.pop("total")
        if self.scenario == "bad_total":
            payload["total"] = True
        if self.scenario == "mutation" and offset:
            payload["total"] = total + 1
        if self.target.applied_facets and self.scenario != "missing_facet":
            key, values = next(iter(self.target.applied_facets.items()))
            payload["facets"] = [{"facetParameter": key, "values": [
                {"id": values[0], "count": total + (self.scenario == "wrong_facet_count")}
            ]}]
        return Response(payload)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("scenario", [
    "success", "zero", "pagination", "short_page", "missing_total",
    "zero_missing_total", "bad_total", "duplicates", "conflicting_id",
    "malformed", "mutation", "repeated_page", "limit",
])
def test_each_candidate_inventory_and_failure_authority(name, scenario, monkeypatch):
    target = CompanyTarget.from_dict(CANDIDATES[name])
    if scenario == "limit":
        target.workday_request_budget = 1
    session = SnapshotSession(target, scenario)
    monkeypatch.setattr(canary, "BoundedSession", lambda *args, **kwargs: session)
    result = canary.run_canary(target, 40)
    success = scenario in {"success", "zero", "pagination"}
    assert result["authoritative_for_absence"] is success, (scenario, result)
    assert (result["canary_outcome"] == "PASS_COMPLETE") is success
    if success:
        expected = {"success": 3, "zero": 0, "pagination": 21}[scenario]
        assert result["inventory_items_collected"] == result["jobs_collected"] == result["total_available"] == expected
        assert result["source_identity_status"] == "validated"
        assert result["terminal_evidence_valid"] is True
        assert result["stable_ids_unique"] is True
        assert result["apply_urls_contract_valid"] is True
    assert result["persistence_mode"] == "none"
    for url, body in session.calls:
        assert url == f"https://{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com/wday/cxs/{target.tenant}/{target.site_id}/jobs"
        assert body.get("appliedFacets", {}) == target.applied_facets


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("field,value", [
    ("expected_employer_name", "Other Employer"), ("expected_source_scope", "other"),
    ("expected_workday_tenant", "other"), ("expected_workday_site", "OtherBoard"),
    ("expected_workday_hostname", "other.wd1.myworkdayjobs.com"),
    ("expected_apply_hostname", "other.example.com"),
    ("official_binding_target_url", "https://other.example.com/External"),
    ("official_source_url", "http://insecure.example.com"),
    ("allowed_apply_hosts", ["other.example.com"]), ("workday_identity", {}),
])
def test_each_candidate_identity_negatives_deny_absence(name, field, value, monkeypatch):
    row = deepcopy(CANDIDATES[name])
    if field in {"workday_identity", "allowed_apply_hosts"}:
        row[field] = value
    else:
        row["workday_identity"][field] = value
    target = CompanyTarget.from_dict(row)
    session = SnapshotSession(target, "zero")
    monkeypatch.setattr(canary, "BoundedSession", lambda *args, **kwargs: session)
    result = canary.run_canary(target, 40)
    assert result["canary_outcome"] != "PASS_COMPLETE"
    assert result["authoritative_for_absence"] is False
    if field != "workday_identity":
        assert session.requests_started == 0


@pytest.mark.parametrize("scenario", ["missing_facet", "wrong_facet_count"])
def test_solera_facet_cannot_be_ignored_or_broadened(scenario, monkeypatch):
    target = CompanyTarget.from_dict(CANDIDATES["Solera Holdings"])
    session = SnapshotSession(target, scenario)
    monkeypatch.setattr(canary, "BoundedSession", lambda *args, **kwargs: session)
    result = canary.run_canary(target, 40)
    assert result["authoritative_for_absence"] is False
    assert result["canary_outcome"] != "PASS_COMPLETE"
    assert all(body["appliedFacets"] == target.applied_facets for _, body in session.calls)


def test_activation_package_preserves_all_other_513_effective_and_raw_rows():
    assert activation.validate_package() == {
        'status': 'PASS', 'sources': 20, 'unchanged_companies': 513,
        'live_jobs_accounted': 1303, 'live_physical_requests': 76,
        'network_requests': 0, 'production_writes': 0,
    }


@pytest.mark.parametrize('field,value', [
    ('canary_outcome','FAIL_INCOMPLETE'), ('source_identity_status','unproven'),
    ('can_reconcile_absence',False), ('terminal_evidence_valid',False),
    ('inventory_items_collected',146), ('total_available',None),
    ('unreadable_items',1), ('rejected_items',1), ('collection_changed_during_scan',True),
    ('safety_limit_reached',True), ('requests_started',29),
    ('source_config_key','workday:other:wrong'), ('source_scope','other'),
    ('company','Other Employer'), ('physical_count_is_upper_bound',True),
])
def test_activation_rejects_failed_or_mismatched_live_evidence(field,value):
    source=json.loads((PACKAGE/'manifest.json').read_text())['sources'][0]
    result=deepcopy(json.loads((PACKAGE/'evidence.json').read_text())['sources'][0]['result'])
    result[field]=value
    with pytest.raises(ValueError):
        activation.validate_result(result,source)


@pytest.mark.parametrize('filename', list(activation.ARTIFACT_HASHES))
def test_immutable_rollback_and_live_artifacts_cannot_be_rewritten(filename,tmp_path):
    import shutil
    package=tmp_path/activation.PACKAGE
    shutil.copytree(PACKAGE,package)
    with (package/filename).open('a') as f:
        f.write('\n# changed')
    with pytest.raises(ValueError,match='Immutable activation artifact changed'):
        activation.validate_package(tmp_path)
