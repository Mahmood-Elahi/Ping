from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.validate_source_integrity_canaries import (
    BoundedSession,
    CanaryBudgetReached,
)
from src.scraper.company_careers.adapters import smartrecruiters
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
    content = b"{}"
    text = ""
    headers = {"content-type": "application/json"}

    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def target(*, hydrate=False, max_pages=10, max_jobs=1_000):
    return CompanyTarget.from_dict(
        {
            "name": "Example Group",
            "platform": "smartrecruiters",
            "slug": "ExampleGroup",
            "careers_url": "https://careers.example.test/jobs",
            "endpoint_url": (
                "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings"
            ),
            "endpoint_variant": "companies_postings_v1",
            "identity_regex": r"^Example Group$",
            "allowed_apply_hosts": ["jobs.smartrecruiters.com"],
            "page_size": 100,
            "max_pages": max_pages,
            "max_jobs": max_jobs,
            "selective_detail_enrichment": hydrate,
        }
    )


def item(job_id="744000000000001", **changes):
    value = {
        "id": job_id,
        "name": "Software Engineer",
        "refNumber": "REF-1",
        "company": {"identifier": "ExampleGroup", "name": "Example Group"},
        "releasedDate": "2026-08-10T10:43:36.683Z",
        "location": {"fullLocation": "Calgary, AB, Canada"},
        "department": {"label": "Engineering"},
        "function": {"label": "Software"},
        "ref": (
            "https://api.smartrecruiters.com/v1/companies/ExampleGroup/postings/"
            + job_id
        ),
    }
    value.update(changes)
    return value


def page(content, *, total=None, offset=0):
    return Response(
        {
            "offset": offset,
            "limit": 100,
            "totalFound": len(content) if total is None else total,
            "content": content,
        }
    )


def detail(listing):
    value = dict(listing)
    value.update(
        {
            "applyUrl": (
                "https://jobs.smartrecruiters.com/ExampleGroup/"
                f"{listing['id']}-software-engineer"
            ),
            "jobAd": {"sections": {"description": {"text": "<p>Build.</p>"}}},
        }
    )
    return Response(value)


def checkpoint(**changes):
    values = {
        "source_scope_key": "example group::default",
        "provider_identity": "smartrecruiters:examplegroup",
        "config_fingerprint": "a" * 64,
        "strategy_version": smartrecruiters.INCREMENTAL_POLICY.strategy_version,
        "timestamp_watermark": NOW,
        "last_successful_full_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return IncrementalCheckpoint(**values)


def test_smartrecruiters_policy_is_explicitly_full_crawl_only():
    decision = choose_crawl_mode(
        source_scope_key_value="example group::default",
        provider_identity="smartrecruiters:examplegroup",
        config_fingerprint="a" * 64,
        policy=smartrecruiters.INCREMENTAL_POLICY,
        checkpoint=checkpoint(),
        now=NOW,
    )

    assert decision.mode is CrawlMode.FULL_AUTHORITATIVE
    assert decision.reason == "capability_none"
    assert smartrecruiters.TRUSTED_LISTING_VERSION_FIELD is None


def test_all_production_smartrecruiters_sources_remain_listing_only():
    targets = [
        configured
        for configured in load_target_companies(apply_reliability_repairs=True)
        if configured.platform == "smartrecruiters"
    ]

    assert len(targets) == 17
    assert all(not configured.selective_detail_enrichment for configured in targets)
    assert all(configured.endpoint_variant == "companies_postings_v1" for configured in targets)
    service_now = next(configured for configured in targets if configured.name == "ServiceNow")
    assert (service_now.page_size, service_now.max_pages, service_now.max_jobs) == (
        100,
        8,
        800,
    )


def test_listing_uses_only_complete_offset_pagination_not_cursor_or_update_filter():
    session = Session([page([item()])])
    result = smartrecruiters.fetch_jobs(session, target(max_jobs=1))

    assert len(result.inventory_items) == 1
    assert session.calls[0][1]["params"] == {"limit": 100, "offset": 0}


def test_missing_release_timestamp_is_not_promoted_to_a_trusted_version():
    listing = item()
    listing.pop("releasedDate")
    result = smartrecruiters.fetch_jobs(Session([page([listing])]), target(max_jobs=1))

    assert result.jobs[0].updated_at == ""
    assert smartrecruiters.TRUSTED_LISTING_VERSION_FIELD is None
    assert not can_skip_hydration(stored_version=None, inventory_version=None)


def test_malformed_release_timestamp_is_a_rejected_item_not_a_watermark():
    listing = item(releasedDate="not-a-timestamp")
    result = smartrecruiters.fetch_jobs(Session([page([listing])]), target(max_jobs=1))

    assert result.jobs == []
    assert result.item_failures[0].outcome == "rejected"
    assert "releasedDate is not ISO formatted" in result.item_failures[0].reason


def test_same_id_and_release_time_do_not_hide_material_changes():
    baseline = smartrecruiters._normalize_listing(
        item(), target(max_jobs=1), "ExampleGroup", "744000000000001"
    )
    changed = smartrecruiters._normalize_listing(
        item(name="Principal Software Engineer"),
        target(max_jobs=1),
        "ExampleGroup",
        "744000000000001",
    )
    unchanged = smartrecruiters._normalize_listing(
        item(), target(max_jobs=1), "ExampleGroup", "744000000000001"
    )

    assert baseline.job_id == changed.job_id
    assert baseline.updated_at == changed.updated_at
    assert material_content_fingerprint(baseline) != material_content_fingerprint(changed)
    assert material_content_fingerprint(baseline) == material_content_fingerprint(unchanged)
    assert smartrecruiters.TRUSTED_LISTING_VERSION_FIELD is None


def test_duplicate_provider_id_fails_closed():
    duplicate = item()
    with pytest.raises(AdapterError, match="repeated stable job ID"):
        smartrecruiters.fetch_jobs(
            Session([page([duplicate, duplicate], total=2)]), target(max_jobs=2)
        )


def test_incomplete_pagination_fails_closed():
    first = [item(str(744000000000000 + index)) for index in range(100)]
    session = Session([page(first, total=101), page([], total=101, offset=100)])

    with pytest.raises(AdapterError, match="ended before totalFound"):
        smartrecruiters.fetch_jobs(session, target(max_jobs=101))


def test_page_cap_is_incomplete_and_cannot_authorize_absence():
    first = [item(str(744000000000000 + index)) for index in range(100)]
    result = smartrecruiters.fetch_jobs(
        Session([page(first, total=101)]),
        target(max_pages=1, max_jobs=101),
    )

    assert not result.scan["scan_complete"]
    assert result.scan["safety_limit_reached"]
    assert result.scan["stop_reason"] == "maximum_pages_reached"


def test_independent_canary_request_budget_exhaustion_is_fail_closed():
    session = BoundedSession(1)
    session.requests_started = 1
    try:
        with pytest.raises(CanaryBudgetReached, match="request budget"):
            session.request("GET", "https://api.smartrecruiters.com")
    finally:
        session.close()


def test_detail_retry_exhaustion_remains_bounded_and_unreadable(monkeypatch):
    monkeypatch.setattr(smartrecruiters.time, "sleep", lambda _seconds: None)
    listing = item()
    session = Session([page([listing]), Response({}, status=503), Response({}, status=503)])
    result = smartrecruiters.fetch_jobs(
        session, target(hydrate=True, max_jobs=1)
    )

    assert len(session.calls) == 3
    assert result.jobs == []
    assert result.item_failures[0].outcome == "unreadable"
    assert result.item_failures[0].retryable


def test_config_and_strategy_identity_remain_checkpoint_bound_and_scope_isolated():
    base = {
        "endpoint_variant": "companies_postings_v1",
        "slug": "ExampleGroup",
        "source_scope": "default",
        "page_size": 100,
        "max_pages": 10,
        "max_jobs": 1_000,
        "selective_detail_enrichment": False,
    }

    assert configuration_fingerprint(base) != configuration_fingerprint(
        {**base, "max_pages": 9}
    )
    assert source_checkpoint_key("Example Group", "default", "smartrecruiters:examplegroup") != (
        source_checkpoint_key("Example Group", "canada", "smartrecruiters:examplegroup")
    )
    changed = checkpoint(strategy_version="smartrecruiters_v2")
    assert changed.strategy_version != smartrecruiters.INCREMENTAL_POLICY.strategy_version


def test_hydrated_detail_material_is_covered_by_the_generic_fingerprint():
    listing = item()
    hydrated = smartrecruiters.fetch_jobs(
        Session([page([listing]), detail(listing)]),
        target(hydrate=True, max_jobs=1),
    ).jobs[0]
    title_changed = hydrated.to_dict()
    title_changed["title"] = "Changed title"
    description_changed = hydrated.to_dict()
    description_changed["description"] = "Changed description"
    metadata_changed = hydrated.to_dict()
    metadata_changed["raw_metadata"] = {
        **hydrated.raw_metadata,
        "apply_url": "https://jobs.smartrecruiters.com/ExampleGroup/744000000000001-new",
    }

    original = material_content_fingerprint(hydrated)
    assert original != material_content_fingerprint(title_changed)
    assert original != material_content_fingerprint(description_changed)
    assert original != material_content_fingerprint(metadata_changed)


def test_phase6_runbook_records_the_fail_closed_verdict_and_no_fake_canary():
    runbook = (ROOT / "docs" / "PHASE6_INCREMENTAL_OPTIMIZATION.md").read_text(
        encoding="utf-8"
    )

    assert "Classification: **A. FULL_CRAWL_ONLY**" in runbook
    assert "No exact manual incremental canary command is published" in runbook
    assert "There is no future activation diff" in runbook
    assert "Wise is a blocked research" in runbook

