from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from scripts.validate_source_integrity_canaries import (
    BoundedSession,
    CanaryBudgetReached,
)
from src.scraper.company_careers.adapters import greenhouse
from src.scraper.company_careers.incremental import (
    CrawlMode,
    IncrementalCheckpoint,
    can_skip_hydration,
    choose_crawl_mode,
    configuration_fingerprint,
    material_content_fingerprint,
    source_checkpoint_key,
)
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, payload, *, status=200, url="", history=()):
        self.body = json.dumps(payload).encode("utf-8")
        self.status_code = status
        self.url = url
        self.history = history
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if not response.url:
            response.url = url
        return response


def target(**changes):
    values = {
        "name": "Example",
        "platform": "greenhouse",
        "slug": "example",
        "endpoint_variant": greenhouse.STRICT_BOARD_VARIANT,
        "allowed_apply_hosts": ["job-boards.greenhouse.io"],
        "max_pages": 1,
        "max_jobs": greenhouse.MAX_RESULTS,
        "max_response_bytes": 3_000_000,
    }
    values.update(changes)
    return CompanyTarget.from_dict(values)


def listing(job_id="1", **changes):
    value = {
        "id": int(job_id),
        "title": "Software Engineer",
        "location": {"name": "Calgary, AB"},
        "absolute_url": f"https://job-boards.greenhouse.io/example/jobs/{job_id}",
        "updated_at": "2026-10-09T12:00:00-06:00",
        "metadata": [{"name": "Employment Type", "value": "Full-time"}],
        "requisition_id": "REQ-1",
        "internal_job_id": 101,
    }
    value.update(changes)
    return value


def detail(job_id="1", **changes):
    value = {
        **listing(job_id),
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Calgary"}],
        "content": "<p>Build reliable systems.</p>",
    }
    value.update(changes)
    return value


def checkpoint(**changes):
    values = {
        "source_scope_key": "example::default",
        "provider_identity": "greenhouse:example",
        "config_fingerprint": "a" * 64,
        "strategy_version": greenhouse.INCREMENTAL_POLICY.strategy_version,
        "timestamp_watermark": NOW,
        "last_successful_full_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return IncrementalCheckpoint(**values)


def test_greenhouse_policy_is_explicitly_full_crawl_only():
    decision = choose_crawl_mode(
        source_scope_key_value="example::default",
        provider_identity="greenhouse:example",
        config_fingerprint="a" * 64,
        policy=greenhouse.INCREMENTAL_POLICY,
        checkpoint=checkpoint(),
        now=NOW,
    )

    assert decision.mode is CrawlMode.FULL_AUTHORITATIVE
    assert decision.reason == "capability_none"
    assert greenhouse.TRUSTED_LISTING_VERSION_FIELD is None


def test_production_greenhouse_inventory_and_bounds_are_deterministic():
    targets = [
        configured
        for configured in load_target_companies(apply_reliability_repairs=True)
        if configured.platform == "greenhouse"
    ]

    assert len(targets) == 102
    assert {configured.source_scope for configured in targets} == {"default"}
    assert {configured.max_pages for configured in targets} == {1}
    assert {configured.max_jobs for configured in targets} == {10_000}
    assert {configured.max_response_bytes for configured in targets} == {
        3_000_000,
        5_000_000,
    }
    assert sum(configured.endpoint_variant is None for configured in targets) == 92
    assert sum(
        configured.endpoint_variant == "boards_api_v1" for configured in targets
    ) == 3
    assert sum(
        configured.endpoint_variant == greenhouse.STRICT_BOARD_VARIANT
        for configured in targets
    ) == 7
    assert [configured.name for configured in targets if configured.department_allowlist] == [
        "CoreWeave"
    ]


def test_current_compact_request_requires_detail_and_has_no_update_filter():
    session = Session([Response({"jobs": [listing()]}), Response(detail())])
    result = greenhouse.fetch_jobs(session, target())

    assert len(result.jobs) == 1
    assert len(session.calls) == 2
    assert session.calls[0][0] == (
        "https://boards-api.greenhouse.io/v1/boards/example/jobs"
    )
    assert "content=true" not in session.calls[0][0]
    assert session.calls[1][0].endswith("/jobs/1")
    assert result.jobs[0].description == "<p>Build reliable systems.</p>"


def test_listing_and_detail_field_contract_is_explicit():
    session = Session([Response({"jobs": [listing()]}), Response(detail())])
    job = greenhouse.fetch_jobs(session, target()).jobs[0]

    assert job.job_id == "1"
    assert job.title == "Software Engineer"
    assert job.company == "Example"
    assert job.location == "Calgary, AB"
    assert job.url == "https://job-boards.greenhouse.io/example/jobs/1"
    assert job.updated_at == "2026-10-09T12:00:00-06:00"
    assert job.departments == ["Engineering"]
    assert job.offices == ["Calgary"]
    assert job.raw_metadata["requisition_id"] == "REQ-1"
    assert job.raw_metadata["internal_job_id"] == 101
    assert job.raw_metadata["greenhouse_metadata"] == {
        "Employment Type": "Full-time"
    }


def test_missing_or_malformed_updated_at_never_becomes_a_trusted_version():
    missing = listing()
    missing.pop("updated_at")
    malformed = listing(updated_at="not-a-material-version")

    missing_job = greenhouse.fetch_jobs(
        Session([Response({"jobs": [missing]}), Response(detail(updated_at=""))]),
        target(),
    ).jobs[0]
    malformed_job = greenhouse.fetch_jobs(
        Session(
            [
                Response({"jobs": [malformed]}),
                Response(detail(updated_at="not-a-material-version")),
            ]
        ),
        target(),
    ).jobs[0]

    assert missing_job.updated_at == ""
    assert malformed_job.updated_at == "not-a-material-version"
    assert greenhouse.TRUSTED_LISTING_VERSION_FIELD is None
    assert not can_skip_hydration(stored_version="same", inventory_version=None)


def test_same_id_and_listing_timestamp_cannot_hide_detail_change():
    first = greenhouse.fetch_jobs(
        Session([Response({"jobs": [listing()]}), Response(detail())]), target()
    ).jobs[0]
    changed = greenhouse.fetch_jobs(
        Session(
            [
                Response({"jobs": [listing()]}),
                Response(detail(content="<p>Materially changed.</p>")),
            ]
        ),
        target(),
    ).jobs[0]
    unchanged = greenhouse.fetch_jobs(
        Session([Response({"jobs": [listing()]}), Response(detail())]), target()
    ).jobs[0]

    assert first.job_id == changed.job_id
    assert first.updated_at == changed.updated_at
    assert material_content_fingerprint(first) != material_content_fingerprint(changed)
    assert material_content_fingerprint(first) == material_content_fingerprint(unchanged)


@pytest.mark.parametrize(
    "jobs",
    [
        [listing("1"), listing("1")],
        [listing("1"), listing("2", absolute_url=listing("1")["absolute_url"])],
    ],
)
def test_duplicate_provider_id_or_canonical_url_fails_closed_for_every_variant(jobs):
    non_strict = target(endpoint_variant=None, allowed_apply_hosts=[])
    with pytest.raises(AdapterError, match="repeated a stable ID or canonical URL"):
        greenhouse.fetch_jobs(Session([Response({"jobs": jobs})]), non_strict)


def test_wrong_slug_foreign_board_and_disallowed_apply_host_fail_closed():
    with pytest.raises(AdapterError, match="missing slug"):
        greenhouse.fetch_jobs(Session([]), target(slug=None))

    foreign = listing(
        absolute_url="https://job-boards.greenhouse.io/foreign/jobs/1"
    )
    with pytest.raises(AdapterError, match="exact board and stable ID"):
        greenhouse.fetch_jobs(Session([Response({"jobs": [foreign]})]), target())

    disallowed = listing(
        absolute_url="https://evil.example/example/jobs/1"
    )
    with pytest.raises(AdapterError, match="exact board and stable ID"):
        greenhouse.fetch_jobs(Session([Response({"jobs": [disallowed]})]), target())

    malformed = target(endpoint_variant=None, allowed_apply_hosts=[])
    with pytest.raises(AdapterError, match="canonical job URL is malformed"):
        greenhouse.fetch_jobs(
            Session([Response({"jobs": [listing(absolute_url="not a URL")]})]),
            malformed,
        )


def test_redirect_identity_and_hidden_continuation_fail_closed():
    requested = "https://boards-api.greenhouse.io/v1/boards/example/jobs"
    redirected = Response(
        {"jobs": [listing()]},
        url="https://foreign.example/v1/boards/example/jobs",
        history=(object(),),
    )
    with pytest.raises(AdapterError, match="exact request identity"):
        greenhouse.fetch_jobs(Session([redirected]), target())
    assert redirected.closed

    with pytest.raises(AdapterError, match="unsupported continuation"):
        greenhouse.fetch_jobs(
            Session(
                [
                    Response(
                        {"jobs": [listing()], "next": "/v1/boards/example/jobs?page=2"},
                        url=requested,
                    )
                ]
            ),
            target(),
        )


def test_collection_bound_is_incomplete_and_cannot_supply_terminal_authority():
    embedded = detail()
    result = greenhouse.fetch_jobs(
        Session([Response({"jobs": [embedded]})]), target(max_jobs=1)
    )

    assert not result.scan["scan_complete"]
    assert result.scan["safety_limit_reached"]
    assert result.scan["stop_reason"] == "maximum_results_reached"


def test_detail_transport_failure_is_bounded_without_adapter_retry():
    session = Session(
        [Response({"jobs": [listing()]}), requests.ConnectionError("offline fixture")]
    )
    result = greenhouse.fetch_jobs(session, target())

    assert len(session.calls) == 2
    assert [job.job_id for job in result.jobs] == ["1"]
    assert len(result.item_failures) == 1
    assert result.item_failures[0].retryable
    assert result.item_failures[0].outcome == "unreadable"


def test_independent_canary_request_budget_exhaustion_is_fail_closed():
    session = BoundedSession(1)
    session.requests_started = 1
    try:
        with pytest.raises(CanaryBudgetReached, match="request budget"):
            session.request("GET", "https://boards-api.greenhouse.io")
    finally:
        session.close()


def test_config_strategy_and_scope_remain_bound_but_no_checkpoint_can_advance():
    base = {
        "slug": "example",
        "source_scope": "default",
        "endpoint_variant": greenhouse.STRICT_BOARD_VARIANT,
        "max_pages": 1,
        "max_jobs": 10_000,
        "max_response_bytes": 3_000_000,
        "allowed_apply_hosts": ["job-boards.greenhouse.io"],
        "trusted_version_field": None,
    }

    assert configuration_fingerprint(base) != configuration_fingerprint(
        {**base, "max_response_bytes": 5_000_000}
    )
    assert source_checkpoint_key("Example", "default", "greenhouse:example") != (
        source_checkpoint_key("Example", "canada", "greenhouse:example")
    )
    assert checkpoint(strategy_version="greenhouse_v2").strategy_version != (
        greenhouse.INCREMENTAL_POLICY.strategy_version
    )
    assert greenhouse.INCREMENTAL_POLICY.capability.value == "none"


def test_generic_material_fingerprint_covers_scope_and_equivalence():
    job = greenhouse.fetch_jobs(
        Session([Response({"jobs": [listing()]}), Response(detail())]), target()
    ).jobs[0]
    original = material_content_fingerprint(job)
    changed_scope = job.to_dict() | {"source_scope": "canada"}
    changed_equivalence = job.to_dict() | {
        "cross_source_equivalence": {"provider_requisition_id": "REQ-OTHER"}
    }

    assert original != material_content_fingerprint(changed_scope)
    assert original != material_content_fingerprint(changed_equivalence)


def test_phase6_runbook_records_verdict_request_model_and_no_fake_canary():
    runbook = (ROOT / "docs" / "ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Greenhouse and SmartRecruiters were explicitly classified full-crawl-only" in runbook
    assert "102 Greenhouse sources" in runbook
    assert "Future incremental canary: none" in runbook
    assert "safe request savings remained zero" in runbook
    assert "production diff remained empty" in runbook

