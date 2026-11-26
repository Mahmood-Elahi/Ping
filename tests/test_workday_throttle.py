from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch
import pytest
import requests

from src.scraper.company_careers.workday_throttle import WorkdayRateLimit, WorkdayThrottle, retry_seconds
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


def test_cross_tenant_pause_requires_two_recent_distinct_signals():
    now = [100.0]

    def wait(delta):
        now[0] += delta

    policy = WorkdayThrottle(clock=lambda: now[0], sleep=wait, wall_clock=lambda: 1000 + now[0])
    policy.throttled("a", "120")
    policy.throttled("a", "2")
    policy.admit("b")
    assert now[0] == 100
    policy.throttled("b", None)
    with pytest.raises(WorkdayRateLimit) as shared:
        policy.admit("c")
    assert shared.value.reason == "rate_limit_backpressure"
    assert shared.value.retry_at is None
    assert now[0] == 100
    with pytest.raises(WorkdayRateLimit) as host:
        policy.admit("a")
    assert host.value.reason == "rate_limit_wait"
    assert datetime.fromisoformat(host.value.retry_at).timestamp() == 1220

    now[0] = 160
    policy.admit("c")
    policy.admit("d")
    assert now[0] == 163
    with pytest.raises(WorkdayRateLimit):
        policy.admit("a")
    now[0] = 220
    policy.admit("a")
    policy.admit("e")
    assert now[0] == 221


def test_headers_and_unrelated_routes():
    assert retry_seconds(format_datetime(datetime.fromtimestamp(300,timezone.utc)), 100)==200
    assert retry_seconds('broken', 100) is None
    assert retry_seconds('0', 100)==0
    policy = WorkdayThrottle()
    policy.throttled('x.wd1.myworkdayjobs.com', '10')
    with patch.object(requests.Session,'send',return_value='sent'):
        assert policy.session().get('https://x.wd1.myworkdayjobs.com/board')=='sent'
        assert policy.session().post('https://another.example/wday/cxs/x/Board/jobs')=='sent'


def test_distinct_sessions_space_first_requests_before_throttling():
    now = [0.0]
    def wait(seconds):
        now[0] += seconds
    policy = WorkdayThrottle(clock=lambda: now[0], sleep=wait)
    policy.admit("a")
    policy.admit("b")
    assert now[0] == 1


def test_listing_redirect_never_sends_the_post_to_the_new_destination(monkeypatch):
    calls = []
    def redirect(adapter, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.url = request.url
        response.status_code = 307
        response.headers["Location"] = "https://different.example/jobs"
        response._content, response._content_consumed = b"", True
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", redirect)
    url = "https://x.wd1.myworkdayjobs.com/wday/cxs/x/Board/jobs"
    with pytest.raises(requests.HTTPError, match="redirect"):
        WorkdayThrottle().session().post(url)
    assert calls == [url]
    assert retry_seconds("9" * 40, 0) == float("inf")


def test_production_scanner_classifies_response_and_persists_real_retry_deadline(monkeypatch):
    from scripts.public_ci_report import classify_scan_reason
    from src.scraper.company_careers.scanner import CompanyCareersScanner
    from src.scraper.company_careers.source_cooldown import cooldown_active, reconcile_source_health, source_health_key

    observed = datetime(2026, 11, 25, 16, 15, tzinfo=timezone.utc)
    policy = WorkdayThrottle(clock=lambda: 1, wall_clock=lambda: observed.timestamp() + 45)
    requests_sent = []

    def send(adapter, request, **kwargs):
        requests_sent.append(request.url)
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "90000"
        response._content, response._content_consumed = b"", True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    scanner = CompanyCareersScanner(targets=[target()], session_factory=policy.session, as_of=observed)
    physical = scanner.scan().results[0]
    held = scanner.scan().results[0]
    assert len(requests_sent) == 1
    assert [r.error_type for r in (physical, held)] == ["rate_limited", "rate_limit_wait"]
    assert classify_scan_reason(physical) == "rate_limited"
    assert classify_scan_reason(held) == "rate_limit_wait"
    assert physical.scan_health == "failed"
    assert held.scan_health == "incomplete"
    assert not physical.authoritative_for_absence
    assert not held.authoritative_for_absence
    state = reconcile_source_health([physical], {}, as_of=observed)[0]
    assert state["consecutive_transient_failures"] == 1
    deadline = observed + timedelta(seconds=90045)
    assert datetime.fromisoformat(state["cooldown_until"]) == deadline
    assert not cooldown_active(state, as_of=deadline)
    assert cooldown_active(state, as_of=deadline - timedelta(microseconds=1))
    key = source_health_key(physical.target)
    held_state = reconcile_source_health([held], {key: state}, as_of=observed)[0]
    assert held_state["consecutive_transient_failures"] == 1
    assert held_state["cooldown_until"] == state["cooldown_until"]


@pytest.mark.parametrize("failure,public_reason,streak", [("redirect", "source_identity_failure", 0),
                                                          ("timeout", "timeout", 1)])
def test_scanner_classifies_redirect_and_timeout_without_replay(monkeypatch, failure, public_reason, streak):
    from scripts.public_ci_report import classify_scan_reason
    from src.scraper.company_careers.scanner import CompanyCareersScanner
    from src.scraper.company_careers.source_cooldown import reconcile_source_health

    calls = []
    def send(adapter, request, **kwargs):
        calls.append(request.url)
        if failure == "timeout":
            raise requests.ReadTimeout("upstream read timeout")
        response = requests.Response()
        response.status_code = 307
        response.headers["Location"] = "https://different.example/jobs"
        response._content, response._content_consumed = b"", True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    result = CompanyCareersScanner(targets=[target()], session_factory=WorkdayThrottle().session).scan().results[0]
    assert len(calls) == 1 and not result.authoritative_for_absence
    assert classify_scan_reason(result) == public_reason
    assert reconcile_source_health([result], {}, as_of=datetime.now(timezone.utc))[0]["consecutive_transient_failures"] == streak


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_access_or_transient_status_stops_body_and_site_fallback(status):
    from tests.test_phase4_workday_protocol import Response
    from src.scraper.company_careers.models import AdapterError

    class Session:
        def __init__(self):
            self.posts = 0

        def post(self, *args, **kwargs):
            self.posts += 1
            return Response({}, status_code=status)

    session = Session()
    with pytest.raises(AdapterError, match=f"HTTP {status}"):
        workday.fetch_jobs(session, target(workday_identity={}, site_ids=["Alternate"]))
    assert session.posts == 1


def test_count_covering_facet_fits_existing_state_limit_and_validates_union():
    from src.scraper.company_careers.scanner import CompanyCareersScanner
    from tests.test_phase4_workday_protocol import DatasetSession

    class Session(DatasetSession):
        def _payload(self, ids, offset, reported, *, selected):
            payload = super()._payload(ids, offset, reported, selected=selected)
            if selected is None:
                # The production heuristic prefers smaller bins. Three child
                # states cannot fit, whereas the two-location split can.
                payload["facets"].append({
                    "facetParameter": "department",
                    "values": [{"id": str(i), "count": 667} for i in range(3)],
                })
            return payload

    partitions = {"A": list(range(1000)), "B": list(range(1000, 2001))}
    session = Session(2001, partitions)
    result = CompanyCareersScanner(targets=[target(workday_max_partitions=3)], session=session).scan().results[0]
    assert result.authoritative_for_absence and result.jobs_collected == 2001
    assert all("department" not in call[1]["json"]["appliedFacets"] for call in session.post_calls)


def test_bounded_canary_charges_each_redirect_hop_before_dispatch(monkeypatch):
    from scripts.validate_source_integrity_canaries import BoundedSession, CanaryBudgetReached

    calls = []

    def redirect(adapter, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.url = request.url
        response.status_code = 302
        response.headers["Location"] = "/changed"
        response._content, response._content_consumed = b"", True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", redirect)
    with BoundedSession(1) as session:
        with pytest.raises(CanaryBudgetReached):
            session.get("https://example.test/start")
    assert calls == ["https://example.test/start"]
    cxs = "https://x.wd1.myworkdayjobs.com/wday/cxs/x/Board/jobs"
    with BoundedSession(3) as session, pytest.raises(requests.HTTPError, match="redirect"):
        session.post(cxs)
    assert calls == ["https://example.test/start", cxs]


def test_canary_rate_hold_preserves_physical_budget(monkeypatch):
    from scripts.validate_source_integrity_canaries import BoundedSession

    policy = WorkdayThrottle(clock=lambda: 10, wall_clock=lambda: 100)
    policy.throttled("x.wd1.myworkdayjobs.com", "20")
    with BoundedSession(1, policy) as session, pytest.raises(requests.HTTPError):
        try:
            session.post("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Board/jobs")
        finally:
            assert session.requests_started == 0


def test_correlated_429s_defer_new_listings_without_a_source_health_write(monkeypatch):
    from scripts.public_ci_report import classify_scan_reason
    from src.scraper.company_careers.scanner import CompanyCareersScanner
    from src.scraper.company_careers.source_cooldown import reconcile_source_health

    now = [100.0]
    def wait(delay):
        now[0] += delay

    policy = WorkdayThrottle(clock=lambda: now[0], sleep=wait)
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code = 429
        response._content, response._content_consumed = b"", True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    targets = [target(name=tenant, tenant=tenant, workday_identity={}) for tenant in ("a", "b", "c")]
    observed = datetime.now(timezone.utc)
    report = CompanyCareersScanner(
        targets=targets, max_workers=1, session_factory=policy.session, as_of=observed,
    ).scan()

    assert len(calls) == 2
    assert [r.scan_health for r in report.results] == ["failed", "failed", "incomplete"]
    assert [classify_scan_reason(r) for r in report.results] == [
        "rate_limited", "rate_limited", "rate_limit_backpressure",
    ]
    assert not report.authoritative_source_scopes()
    assert [r["company"] for r in reconcile_source_health(report.results, {}, as_of=observed)] == ["a", "b"]


@pytest.mark.parametrize("workers", [1, 3])
def test_expired_workday_cooldowns_reenter_in_bounded_oldest_first_batches(monkeypatch, workers):
    from scripts.public_ci_report import classify_scan_reason
    from src.scraper.company_careers import scanner
    from src.scraper.company_careers.source_cooldown import reconcile_source_health, source_health_key
    from tests.test_phase4_workday_protocol import DatasetSession

    ordinary, waiting, later, older, active = [
        target(name=name) for name in ("ordinary", "waiting", "later", "older", "active")
    ]
    observed = datetime(2026, 11, 26, 19, 13, tzinfo=timezone.utc)
    prior = {
        source_health_key(current): {
            "cooldown_until": (observed + timedelta(hours=delay)).isoformat(),
            "updated_at": (observed - timedelta(days=age)).isoformat(),
            "last_failure_classification": "transient_transport",
            "consecutive_transient_failures": 8,
        }
        for current, delay, age in ((waiting, 0, 1), (later, -1, 2), (older, -1, 3), (active, 1, 4))
    }
    calls = []
    seen = []

    def fetch(session, current, timeout):
        calls.append(current.name)
        return workday.fetch_jobs(session, current, timeout)

    monkeypatch.setitem(scanner.ADAPTERS, "workday", fetch)
    monkeypatch.setattr(scanner, "WORKDAY_RECOVERY_SOURCES_PER_SCAN", 2)
    targets = [waiting, later, active, ordinary, older]
    report = scanner.CompanyCareersScanner(
        targets=targets, max_workers=workers, session_factory=lambda: DatasetSession(0),
        cooldown_states=prior, as_of=observed, result_observer=seen.append,
    ).scan()

    assert calls == ["ordinary", "older", "later"]
    assert [r.target for r in report.results] == targets
    assert sorted(r.target.name for r in seen) == sorted(r.target.name for r in report.results)
    assert classify_scan_reason(report.results[0]) == "workday_recovery_deferred"
    assert classify_scan_reason(report.results[2]) == "cooldown_skipped"
    assert all(not report.results[i].authoritative_for_absence for i in (0, 2))
    updated = reconcile_source_health(report.results, prior, as_of=observed)
    assert {r["company"] for r in updated} == {"ordinary", "older", "later"}

    calls.clear()
    states = {**prior, **{row["source_key"]: row for row in updated}}
    scanner.CompanyCareersScanner(
        targets=targets, max_workers=workers, session_factory=lambda: DatasetSession(0),
        cooldown_states=states, as_of=observed + timedelta(minutes=1),
    ).scan()
    assert calls == ["ordinary", "waiting", "later"]
