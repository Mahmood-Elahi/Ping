"""Production targets and central-finalizer regression fixtures for retained board evidence."""

from pathlib import Path
from urllib.parse import urlencode

import pytest

from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.student_roles import configure_student_targets
from tests.test_closeout_activation import scan, targets
from tests.test_company_careers import FakeResponse, FakeSession
from tests.test_incomplete_source_recovery_batch import Response, Session, legacy_page
from tests.test_public_json_adapter import wp_response, wordpress_row


def test_atlassian_first_party_regional_postings():
    samples = [
        (25481, "careers-australia-atlassian.icims.com", "account-executive%2c-mid-market"),
        (26070, "campus-globalcareers-atlassian.icims.com", "software-engineer%2c-2027-graduate-canada"),
        (26074, "campus-americas.icims.com", "ai-researcher-%28phd%29%2c-2027-graduate-canada"),
    ]
    rows = [
        {"id": i, "title": slug, "applyUrl": f"https://{host}/jobs/{i}/{slug}/job?mode=apply"}
        for i, host, slug in samples
    ]
    result = scan("Atlassian", FakeSession(get_responses=[FakeResponse(rows)]))
    assert result.authoritative_for_absence
    assert result.fetched_count == 3


@pytest.mark.parametrize("path", [
    "/jobs/9999/engineer/job", "/jobs/25481/a%2fb/job",
    "/jobs/25481/engineer/job?redirect=elsewhere", "/jobs/25481/engineer/job/extra",
])
def test_atlassian_regional_posting_must_match(path):
    row = {"id": 25481, "title": "Engineer", "applyUrl": "https://campus-americas.icims.com" + path}
    result = scan("Atlassian", FakeSession(get_responses=[FakeResponse([row])]))
    assert not result.authoritative_for_absence


def deliveroo_page(count, offset, total, pages, *, row_change=None):
    target = targets()["Deliveroo"]
    rows = []
    for index in range(offset + 1, offset + count + 1):
        row = wordpress_row(index, type="role", status="publish", **{
            "_links": {"self": [{"href": f"{target.endpoint_url}/{index}"}]},
            "link": f"https://careers.deliveroo.co.uk/role/engineer-{index}/",
        })
        rows.append(row)
    if row_change:
        rows[-1].update(row_change)
    response = wp_response(rows, total=total, pages=pages)
    params = {
        "_adjust_ids": "true", "locations_slug": "", "teams_slug": "", "order": "asc",
        "orderby": "title", "per_page": "20", "page": str(offset // 20 + 1),
    }
    response.url = f"{target.endpoint_url}?{urlencode(params)}"
    return response


def scan_deliveroo_candidate(pages):
    path = Path(__file__).parents[1] / "reports/recovery-2026-11-25/deliveroo-candidate.yaml"
    targets = configure_student_targets(load_target_companies(str(path), apply_reliability_repairs=False))
    scanner = CompanyCareersScanner(
        targets=targets, session=FakeSession(get_responses=pages), parent_source_mappings=[]
    )
    return scanner.scan().results[0]


def test_deliveroo_reaches_tenth_page_and_validates_exact_published_roles():
    pages = [deliveroo_page(20, i * 20, 198, 10) for i in range(9)]
    pages.append(deliveroo_page(18, 180, 198, 10))
    result = scan_deliveroo_candidate(pages)
    assert result.authoritative_for_absence
    assert len(result.inventory_items) == 198


def test_empty_deliveroo_page_cannot_authorize_absence():
    response = deliveroo_page(0, 0, 0, 1)
    assert not scan_deliveroo_candidate([response]).authoritative_for_absence


@pytest.mark.parametrize("row_change", [
    {"type": "roles"}, {"status": "draft"}, {"_links": {"self": [{"href": "https://other.invalid/1"}]}},
    {"link": "https://careers.deliveroo.co.uk/teams/engineering/"},
])
def test_deliveroo_identity_drift_has_no_absence_authority(row_change):
    response = deliveroo_page(1, 0, 1, 1, row_change=row_change)
    assert not scan_deliveroo_candidate([response]).authoritative_for_absence


@pytest.mark.parametrize("company", ["Hexagon Autonomy & Positioning / NovAtel", "SECURE Energy Services"])
@pytest.mark.parametrize("terminal", ["exact", "wrong_index", "bare_empty", "nonempty", "scope_change"])
def test_legacy_unadvertised_short_page_requires_explicit_indexed_empty(company, terminal):
    base = targets()[company].search_url
    separator = "&" if "?" in base else "?"
    first_url = base + separator + urlencode({"pr": 0})
    next_url = base + separator + urlencode({"pr": 2 if terminal == "wrong_index" else 1})
    if terminal == "scope_change":
        next_url += "&department=other"
    first = Response(legacy_page("12345"), url=first_url)
    second = Response(
        legacy_page("23456") if terminal == "nonempty" else legacy_page(no_results=terminal != "bare_empty"),
        url=next_url,
    )
    result = scan(company, Session(first, second))
    assert result.authoritative_for_absence == (terminal == "exact")
