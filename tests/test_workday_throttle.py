from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch
import pytest
import requests

from src.scraper.company_careers.workday_throttle import WorkdayThrottle, retry_seconds
from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import source_scan_metadata
from tests.test_phase4_workday_protocol import target


def test_one_physical_429_stops_contract_fallback_and_keeps_source_incomplete(monkeypatch):
    policy = WorkdayThrottle()
    session = policy.session()
    calls = []
    def throttle(adapter, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '120'
        response._content = b""
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', throttle)
    with pytest.raises(requests.HTTPError, match='HTTP 429.*120s'):
        workday.fetch_jobs(session, target(workday_identity={}, site_ids=['Alternate']))
    assert len(calls)==1 and not source_scan_metadata(session)['scan_complete']
    with pytest.raises(requests.HTTPError, match='cooldown'):
        workday.fetch_jobs(policy.session(), target(workday_identity={}))
    assert len(calls)==1


def test_cross_tenant_slowdown_requires_two_recent_distinct_signals():
    now = [100.0]
    def wait(delta):
        now[0] += delta
    policy = WorkdayThrottle(clock=lambda:now[0], sleep=wait)
    policy.throttled('a', '120')
    policy.throttled('a', '2')
    policy.admit('b')
    assert now[0] == 100
    policy.throttled('b', None)
    policy.admit('c')
    policy.admit('d')
    assert now[0] == 102
    now[0] = 161
    policy.admit('e')
    assert now[0] == 161
    with pytest.raises(requests.HTTPError): policy.admit('a')


def test_headers_and_unrelated_routes():
    assert retry_seconds(format_datetime(datetime.fromtimestamp(300,timezone.utc)), 100)==200
    assert retry_seconds('broken', 100) is None
    assert retry_seconds('0', 100)==0
    policy = WorkdayThrottle()
    policy.throttled('x.wd1.myworkdayjobs.com', '10')
    with patch.object(requests.Session,'send',return_value='sent'):
        assert policy.session().get('https://x.wd1.myworkdayjobs.com/board')=='sent'
        assert policy.session().post('https://another.example/wday/cxs/x/Board/jobs')=='sent'
