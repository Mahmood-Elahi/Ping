"""Synthetic protocol and lifecycle tests; no registry or network required."""
from __future__ import annotations

import pytest
import requests

from test_phase6_smartrecruiters_contract import Response, detail, item, page, target
from src.scraper.company_careers.adapters import smartrecruiters as sr
from src.scraper.company_careers.freshness import stable_job_key
from src.scraper.company_careers.incremental import material_content_fingerprint
from src.scraper.company_careers.models import AdapterError, source_scan_metadata
from src.scraper.company_careers.source_health import finalize_source_scan


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(sr.time, "sleep", lambda _: None)


@pytest.mark.parametrize("total,max_jobs", [(0, 1), (1, 1), (1, 10)])
def test_all_raw_rows_must_fit_total_before_any_result_cap(total, max_jobs):
    # The prior adapter could hide row 2 behind max_jobs=total=1 and finalize
    # a contradictory response as complete. Validate raw cardinality first.
    session = Session([page([item(), item("744000000000002")], total=total)])
    with pytest.raises(AdapterError, match="advertised total"):
        sr.fetch_jobs(session, target(max_jobs=max_jobs))
    scan = finalize_source_scan(source_scan_metadata(session), jobs_collected=0, failed=True)
    assert scan["scan_state"] != "complete"
    assert not scan["scan_complete"]


def test_final_page_cannot_hide_extra_rows_at_configured_total():
    rows = [item(str(744000000000001+i)) for i in range(100)]
    session = Session([page(rows, total=101), page([item("744000000000101"), item("744000000000102")], total=101, offset=100)])
    with pytest.raises(AdapterError, match="advertised total"):
        sr.fetch_jobs(session, target(max_jobs=101))
    assert len(session.calls) == 2


def test_detail_preserves_all_section_bodies_and_optional_metadata_without_title_churn():
    listing = item()
    response = detail(listing)
    response.payload.update({
        "jobAd": {"sections": {
            "companyDescription": {"title": "Company", "text": "<p>Our company.</p>"},
            "jobDescription": {"title": "Role", "text": "<p>Build systems.</p>"},
            "qualifications": {"title": "Qualifications", "text": "<ul><li>Python</li><li>Testing</li></ul>"},
            "additionalInformation": {"title": "Benefits", "text": "<p>Learning.</p>"},
            "customSection": {"title": "Access", "text": "<p>Accommodations.</p>"},
        }},
        "typeOfEmployment": {"label": "Full-time"},
        "experienceLevel": {"label": "Associate"},
        "language": {"code": "en"},
        "location": {"fullLocation": "Calgary, AB, Canada", "remote": True},
    })
    result = sr.fetch_jobs(Session([page([listing]), response]), target(hydrate=True))
    job = result.jobs[0]
    assert job.job_id == listing["id"]
    assert job.source == "smartrecruiters:examplegroup"
    assert job.description == "Our company.\n\nBuild systems.\n\nPython\nTesting\n\nLearning.\n\nAccommodations."
    assert job.raw_metadata["employment_type"] == "Full-time"
    assert job.raw_metadata["job_location_type"] == "remote"
    assert job.raw_metadata["experience_level"] == "Associate"
    assert job.raw_metadata["language"] == "en"
    assert job.location == "Calgary, AB, Canada"


def test_section_title_changes_do_not_change_description_identity_or_material_content():
    listing = item()

    def hydrated(title):
        response = detail(listing)
        response.payload["jobAd"] = {
            "sections": {
                "description": {"title": title, "text": "<p>Build systems.</p>"},
            }
        }
        return sr.fetch_jobs(
            Session([page([listing]), response]), target(hydrate=True)
        ).jobs[0]

    first = hydrated("Job Description")
    renamed = hydrated("Internship and remote opportunities")
    assert first.description == renamed.description == "Build systems."
    assert stable_job_key(first) == stable_job_key(renamed)
    assert material_content_fingerprint(first) == material_content_fingerprint(renamed)


def test_missing_optional_metadata_preserves_legacy_detail_shape():
    listing = item()
    result = sr.fetch_jobs(Session([page([listing]), detail(listing)]), target(hydrate=True))
    assert result.jobs[0].description == "Build."
    assert "employment_type" not in result.jobs[0].raw_metadata
    assert "job_location_type" not in result.jobs[0].raw_metadata


@pytest.mark.parametrize("location,expected", [
    ({"remote": True, "hybrid": False}, "remote"),
    ({"remote": False, "hybrid": True}, "hybrid"),
    ({"remote": True, "hybrid": True}, None),
    ({"hybrid": True}, "hybrid"),
    ({"remote": False, "hybrid": False}, None),
    ({"remote": "false", "hybrid": "true"}, None),
])
def test_workplace_flags_require_explicit_unambiguous_booleans(location, expected):
    listing = item()
    response = detail(listing)
    response.payload["location"].update(location)
    result = sr.fetch_jobs(Session([page([listing]), response]), target(hydrate=True))
    assert result.jobs[0].raw_metadata.get("job_location_type") == expected


@pytest.mark.parametrize("failure", [
    Response({}, status=429), Response({}, status=500), requests.Timeout("synthetic"),
    Response({"totalFound": 1}), page([item(), item()]), page([], total=1),
])
def test_first_page_faults_never_finalize_as_complete(failure):
    session = Session([failure])
    with pytest.raises((AdapterError, requests.RequestException)):
        sr.fetch_jobs(session, target())
    scan = finalize_source_scan(source_scan_metadata(session), jobs_collected=0, failed=True)
    assert not scan["scan_complete"]
    assert scan["scan_state"] != "complete"


def test_malformed_json_never_grants_absence_authority():
    class Malformed(Response):
        def json(self):
            raise ValueError("synthetic malformed JSON")
    session = Session([Malformed({})])
    with pytest.raises(AdapterError, match="invalid JSON"):
        sr.fetch_jobs(session, target())
    assert not finalize_source_scan(source_scan_metadata(session), jobs_collected=0, failed=True)["scan_complete"]


def test_late_timeout_preserves_positive_inventory_without_closure_authority():
    session = Session([page([item()], total=2), requests.Timeout("interrupted")])
    result = sr.fetch_jobs(session, target())
    scan = finalize_source_scan(result.scan, jobs_collected=len(result.jobs), inventory_items_collected=len(result.inventory_items))
    assert len(result.jobs) == 1
    assert not scan["scan_complete"]
    assert scan["scan_state"] != "complete"


@pytest.mark.parametrize("max_pages,max_jobs", [(1, 1000), (10, 1)])
def test_bounds_are_not_terminal_evidence(max_pages, max_jobs):
    result = sr.fetch_jobs(Session([page([item()], total=2)]), target(max_pages=max_pages, max_jobs=max_jobs))
    scan = finalize_source_scan(result.scan, jobs_collected=len(result.jobs), inventory_items_collected=len(result.inventory_items))
    assert not scan["scan_complete"]
    assert scan["safety_limit_reached"]


def test_reviewed_empty_collection_still_works():
    result = sr.fetch_jobs(Session([page([], total=0)]), target())
    scan = finalize_source_scan(result.scan, jobs_collected=0, inventory_items_collected=0)
    assert scan["scan_complete"]
    assert scan["scan_state"] == "complete"


def test_contracting_snapshot_restarts_before_raw_cardinality_validation():
    rows = [item(str(744000000000001+i)) for i in range(100)]
    session = Session([page(rows, total=101), page([], total=0, offset=100), page([], total=0)])
    result = sr.fetch_jobs(session, target())
    assert not result.jobs
    assert not result.inventory_items
    assert result.scan["collection_restart_count"] == 1
    assert result.scan["stabilized_after_collection_retry"]
    assert len(session.calls) == 3
    assert finalize_source_scan(result.scan, jobs_collected=0, inventory_items_collected=0)["scan_complete"]


def test_actual_adapter_faults_cannot_advance_lifecycle_absence():
    from test_inventory_authority import process
    from src.scraper.company_careers.models import CompanyCareersReport
    from src.scraper.company_careers.scanner import CompanyCareersScanner
    from src.scraper.company_careers.freshness import stable_job_key

    configured = target(max_jobs=1)

    def scan(response):
        scanner = CompanyCareersScanner(
            targets=[configured], session=Session([response]), parent_source_mappings=[]
        )
        return scanner._scan_target(
            configured, keywords=None, locations=None, max_jobs_per_company=None,
            job_filter=None, location_filter=None, browser_batch=None,
            browser_batch_error=None,
        )

    seed = scan(page([item(releasedDate="2026-10-12T00:00:00Z")]))
    assert seed.authoritative_for_absence
    key = stable_job_key(seed.jobs[0])
    state = process(CompanyCareersReport([seed])).state
    for _ in range(2):
        faulty = scan(page([item(), item("744000000000002")], total=1))
        assert not faulty.authoritative_for_absence
        state = process(CompanyCareersReport([faulty]), state).state
        assert state["jobs"][key]["missing_scan_count"] == 0
    first_absence = scan(page([], total=0))
    assert first_absence.authoritative_for_absence
    state = process(CompanyCareersReport([first_absence]), state).state
    assert state["jobs"][key]["missing_scan_count"] == 1
    second_absence = scan(page([], total=0))
    state = process(CompanyCareersReport([second_absence]), state).state
    assert state["jobs"][key]["missing_scan_count"] == 2
