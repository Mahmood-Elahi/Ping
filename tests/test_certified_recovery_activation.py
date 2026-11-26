import requests
import yaml

from scripts import validate_certified_recovery_activation as activation
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.workday_throttle import WorkdayThrottle


def test_all_approved_targets_are_exact_and_rollback_changes_only_four_sources():
    assert activation.validate() == {
        "ARM": 447, "Aecon": 651, "HOOPP": 0, "Hydro Ottawa": 12,
        "John Deere": 339, "Mercado Libre": 743, "Volvo Cars": 49,
    }
    raw = (activation.ROOT / activation.REGISTRY).read_bytes()
    current = yaml.safe_load(raw)
    prior = activation.before_activation(activation.REGISTRY, raw)
    assert yaml.safe_load(prior) == activation.predecessor_registry(current)
    assert activation.before_activation(activation.REGISTRY, raw + b"\n") == raw + b"\n"


def test_hoopp_production_student_board_preserves_exact_bounds_and_proves_zero(monkeypatch):
    target, = (target for target in load_target_companies(apply_reliability_repairs=True)
               if target.name == "HOOPP")
    assert (target.max_pages, target.max_jobs) == (1, 20)
    calls = []

    def send(_adapter, request, **_kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.url = request.url
        response.status_code = 200
        response._content = b'{"total":0,"jobPostings":[]}'
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    result, = CompanyCareersScanner(targets=[target], session_factory=WorkdayThrottle().session,
                                    parent_source_mappings=[]).scan().results
    assert result.authoritative_for_absence and result.jobs_collected == 0
    assert calls == ["https://hoopp.wd10.myworkdayjobs.com/wday/cxs/hoopp/HOOPPStudent/jobs"]
