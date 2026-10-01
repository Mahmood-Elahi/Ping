from __future__ import annotations

import copy
import json

import pytest

from src.scraper.company_careers.adapters import shopify, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner


POSTING_ID = "11111111-1111-4111-8111-111111111111"
ATS_JOB_ID = "22222222-2222-4222-8222-222222222222"


class Response:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    url = shopify.CAREERS_URL

    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")


class Session:
    def __init__(self, *responses: Response):
        self.responses = list(responses)

    def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


class StaticResponse:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, text: str, url: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url


def target(**overrides):
    values = {"name": "Shopify", "platform": "shopify"}
    values.update(overrides)
    return CompanyTarget(**values)


def route_entry(posting_id: str = POSTING_ID, ats_job_id: str = ATS_JOB_ID):
    url = f"{shopify.CAREERS_URL}?ashby_jid={posting_id}"
    return {
        "job": {"id": ats_job_id, "jobPostingIds": [posting_id]},
        "jobPosting": {
            "id": posting_id,
            "jobId": ats_job_id,
            "title": "Engineer",
            "status": "Published",
            "isListed": True,
            "departmentName": "Engineering",
            "locationName": "Canada",
            "publishedDate": "2026-09-01",
            "externalLink": url,
            "applyLink": url,
        },
    }


def route(*entries):
    return {
        "url": shopify.CAREERS_URL,
        "canonicalUrl": shopify.CAREERS_URL,
        "rootDomain": "shopify.com",
        "fileRoutePath": "/careers",
        "handle": "careers",
        "jobPostingsWithJobs": list(entries or [route_entry()]),
    }


def page(value):
    encoded = [{"loaderData": {shopify.ROUTE_KEY: value}}]
    stream = json.dumps(json.dumps(encoded))
    return (
        "<script>window.__reactRouterContext.streamController.enqueue("
        f"{stream});</script>"
    )


def fetch(value):
    return shopify.fetch_jobs(Session(Response(page(value))), target())


def test_effective_registry_uses_reviewed_batch8_contracts():
    targets = {
        item.name: item
        for item in load_target_companies(apply_reliability_repairs=True)
    }
    assert targets["Plaid"].slug == "plaid"
    assert targets["Kraken"].slug == "kraken.com"
    assert targets["Plaid"].endpoint_variant is None
    assert targets["Kraken"].endpoint_variant is None
    assert targets["Zalando"].pagination_url_template is None
    assert targets["Zalando"].max_pages == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonicalUrl", "https://evil.example/careers"),
        ("rootDomain", "evil.example"),
        ("keyword", "intern"),
        ("pageInfo", {}),
    ],
)
def test_shopify_route_identity_filters_and_pagination_fail_closed(field, value):
    value_route = route()
    value_route[field] = value
    with pytest.raises(AdapterError):
        fetch(value_route)


def test_shopify_cross_linked_identity_fails_closed():
    entry = route_entry()
    entry["job"]["id"] = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(AdapterError, match="crossed its linked ATS job identity"):
        fetch(route(entry))


def test_shopify_duplicate_id_and_canonical_url_fail_closed():
    entry = route_entry()
    with pytest.raises(AdapterError, match="repeated a stable job ID"):
        fetch(route(entry, copy.deepcopy(entry)))

    second = route_entry(
        posting_id="33333333-3333-4333-8333-333333333333",
        ats_job_id="44444444-4444-4444-8444-444444444444",
    )
    second["jobPosting"]["externalLink"] = entry["jobPosting"]["externalLink"]
    second["jobPosting"]["applyLink"] = entry["jobPosting"]["applyLink"]
    with pytest.raises(AdapterError, match="canonical job-specific URL"):
        fetch(route(entry, second))


def test_shopify_nonpublished_or_malformed_records_fail_closed():
    entry = route_entry()
    entry["jobPosting"]["isListed"] = False
    with pytest.raises(AdapterError, match="non-published posting"):
        fetch(route(entry))
    with pytest.raises(AdapterError, match="lacked job/posting objects"):
        fetch({**route(), "jobPostingsWithJobs": [{"jobPosting": route_entry()["jobPosting"]}]})


def test_shopify_multiple_atomic_collections_are_ambiguous():
    body = page(route()) + page(route())
    with pytest.raises(AdapterError, match="one exact atomic collection"):
        shopify.fetch_jobs(Session(Response(body)), target())


def test_shopify_devalue_out_of_range_reference_fails_closed():
    with pytest.raises(AdapterError, match="out-of-range"):
        shopify._decode_devalue([[1]])


@pytest.mark.parametrize(
    "url",
    [
        "https://www.shopify.com:443/careers",
        "https://www.shopify.com:8443/careers",
        "https://user:pass@www.shopify.com/careers",
        "https://shopify.com/careers",
        "https://www.shopify.com/careers#jobs",
    ],
)
def test_shopify_careers_url_requires_exact_approved_https_origin(url):
    assert shopify._canonical_careers_url(url) == ""


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.shopify.com:443/careers?ashby_jid={POSTING_ID}",
        f"https://www.shopify.com:8443/careers?ashby_jid={POSTING_ID}",
        f"https://user:pass@www.shopify.com/careers?ashby_jid={POSTING_ID}",
        f"https://shopify.com/careers?ashby_jid={POSTING_ID}",
        f"https://www.shopify.com/careers?ashby_jid={POSTING_ID}#apply",
        "https://www.shopify.com/careers?ashby_jid=33333333-3333-4333-8333-333333333333",
    ],
)
def test_shopify_job_url_rejects_ports_credentials_hosts_fragments_and_wrong_ids(url):
    with pytest.raises(AdapterError, match="canonical job-specific URL"):
        shopify._job_url(url, POSTING_ID)


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.shopify.com/careers?ashby_jid={POSTING_ID}",
        f"https://www.shopify.com/careers/?ashby_jid={POSTING_ID}",
    ],
)
def test_shopify_job_url_preserves_legitimate_existing_urls(url):
    assert shopify._job_url(url, POSTING_ID) == url


def _zalando_page(job_id: int, *, disabled_next: bool) -> str:
    next_control = (
        '<button class="next disabled" aria-disabled="true">Next</button>'
        if disabled_next
        else '<a class="next" rel="next" href="?page=2">Next</a>'
    )
    return (
        "<html><body><main>"
        f'<a href="/en/jobs/{job_id}-software-engineer">Software Engineer</a>'
        f"{next_control}</main></body></html>"
    )


def test_zalando_unproven_identity_cannot_finalize_healthy_from_effective_config(
    monkeypatch,
):
    monkeypatch.setattr(static_html.time, "sleep", lambda _seconds: None)
    zalando = next(
        item
        for item in load_target_companies(apply_reliability_repairs=True)
        if item.name == "Zalando"
    )

    activated = copy.deepcopy(zalando)
    activated.max_pages = 2
    activated.pagination_url_template = (
        "https://jobs.zalando.com/en/jobs?page={page}"
    )
    activated_session = Session(
        StaticResponse(
            _zalando_page(1001, disabled_next=False),
            "https://jobs.zalando.com/en/jobs?page=1",
        ),
        StaticResponse(
            _zalando_page(1002, disabled_next=True),
            "https://jobs.zalando.com/en/jobs?page=2",
        ),
    )
    activated_result = CompanyCareersScanner(
        targets=[activated], session=activated_session
    ).scan().results[0]
    assert activated_result.scan_complete
    assert activated_result.scan_state == "complete"
    assert activated_result.source_health_code == "healthy"
    assert activated_result.source_identity_status == "unproven"
    assert activated_result.authoritative_for_absence

    effective_result = CompanyCareersScanner(
        targets=[zalando],
        session=Session(
            StaticResponse(
                _zalando_page(1001, disabled_next=False),
                "https://jobs.zalando.com/en/jobs",
            )
        ),
    ).scan().results[0]
    assert not effective_result.scan_complete
    assert effective_result.scan_state == "incomplete"
    assert effective_result.source_health_code == "terminal_contradiction"
    assert effective_result.source_identity_status == "unproven"
    assert not effective_result.authoritative_for_absence
