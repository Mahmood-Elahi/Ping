from dataclasses import replace
from functools import lru_cache
from html import escape

import pytest

from scripts import validate_closeout_recovery as proof
from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.student_roles import configure_student_targets
from tests import successfactors_activation_fixtures as tables
from tests.test_static_html_adapter import Response, Session

SAP_TITLES = {
    "ExxonMobil": "ExxonMobil Jobs",
    "FortisBC": "FortisBC Jobs",
    "Halliburton": "Halliburton Jobs",
    "NOVA Chemicals": "NOVA Chemicals Corporation Jobs",
    "PCL Construction": "PCL Construction Jobs",
    "Spartan Controls": "Spartan Controls Ltd. Jobs",
    "Teradyne": "Teradyne Jobs",
}
BOARDS = {"Applied Materials": "95", "BlackRock": "45831", "Chevron": "38138"}


@lru_cache(maxsize=1)
def targets():
    return {
        t.name: t
        for t in configure_student_targets(
            load_target_companies(apply_reliability_repairs=True)
        )
    }


def scan(name, session):
    report = CompanyCareersScanner(
        targets=[targets()[name]], session=session, parent_source_mappings=[]
    ).scan()
    return report.results[0]


@pytest.mark.parametrize("name", SAP_TITLES)
@pytest.mark.parametrize(
    "scenario,complete",
    [("pagination", True), ("duplicates", False), ("identity", False)],
)
def test_production_table_routes(monkeypatch, name, scenario, complete):
    monkeypatch.setitem(
        tables.COMPANIES,
        name,
        {"identity_evidence": {"observed_public_page_title": SAP_TITLES[name]}},
    )
    result = scan(name, tables.SnapshotSession(name, scenario))
    assert result.authoritative_for_absence is complete
    assert result.inventory_complete is complete
    if complete:
        assert result.fetched_count == 26


def radancy_page(name, page, cards, total, per_page):
    heading = f"Search our Job Opportunities at {name}"
    cells = []
    for job_id, path in cards:
        anchor = f'<a class="section3__search-results-a" data-job-id="{job_id}" href="{escape(path)}">'
        if name == "Applied Materials":
            cell = f'<li class="search-results-list__item"><h2 class="search-results-list__job-title">{anchor}Engineer</a></h2><span class="search-results-list__location">Calgary</span></li>'
        elif name == "BlackRock":
            cell = f'{anchor}<h2 class="section3__job-title">Engineer</h2><span class="job-location"><span class="section3__job-info">Calgary</span></span></a>'
        else:
            cell = f'<li>{anchor}<h2>Engineer</h2><span class="job-location">Calgary</span></a></li>'
        cells.append(cell)
    page_count = (total + per_page - 1) // per_page
    return f"""<html><title>{heading}</title><section id="search-results"
        data-total-results="{total}" data-total-job-results="{total}" data-total-pages="{page_count}"
        data-current-page="{page}" data-records-per-page="{per_page}">
        <section id="search-results-list"><ul class="section3__search-results-ul">
        {"".join(cells)}</ul></section></section></html>"""


@pytest.mark.parametrize("name", BOARDS)
@pytest.mark.parametrize(
    "scenario", ["success", "duplicate", "foreign_board", "first_page_changed"]
)
def test_counted_pages_and_recheck(name, scenario):
    board = BOARDS[name]
    first = radancy_page(name, 1, [("101", f"/job/calgary/one/{board}/101")], 2, 1)
    second_id = "101" if scenario == "duplicate" else "102"
    next_board = "999" if scenario == "foreign_board" else board
    second = radancy_page(
        name, 2, [(second_id, f"/job/calgary/two/{next_board}/{second_id}")], 2, 1
    )
    recheck = first.replace("101", "103") if scenario == "first_page_changed" else first
    result = scan(name, Session(Response(first), Response(second), Response(recheck)))
    assert result.authoritative_for_absence is (scenario == "success")
    if scenario == "success":
        assert result.fetched_count == 2


def etsy_page(paths, start, end, total, navigation=""):
    cards = "".join(
        f'<article class="job-search-results-card-col"><h3 class="job-search-results-card-title"><a href="{escape(path)}">Engineer</a></h3><span class="job-search-results-card-location">Calgary</span></article>'
        for path in paths
    )
    return f'<html><title>Search Page</title><p aria-live="polite">Displaying {start} - {end} of {total} in total</p>{cards}{navigation}</html>'


@pytest.mark.parametrize(
    "scenario", ["success", "foreign_filter", "changed_range", "first_page_changed"]
)
def test_etsy_ranges_and_recheck(scenario):
    first = etsy_page(["/jobs/one"], 1, 1, 2, '<a href="?page=2">Next</a>')
    if scenario == "foreign_filter":
        first = first.replace("page=2", "q=engineering")
    second = etsy_page(["/jobs/two"], 2, 2, 3 if scenario == "changed_range" else 2)
    recheck = (
        first.replace("/jobs/one", "/jobs/three")
        if scenario == "first_page_changed"
        else first
    )
    result = scan("Etsy", Session(Response(first), Response(second), Response(recheck)))
    assert result.authoritative_for_absence is (scenario == "success")


def test_target_drift_cannot_enter_counted_collectors():
    for name in [*BOARDS, "Etsy"]:
        with pytest.raises(AdapterError):
            static_html.validate_target(
                replace(targets()[name], source_scope="student")
            )


def test_campaign_counts_and_byte_exact_rollback():
    jobs = proof.validate()
    assert len(jobs) == 29 and sum(jobs.values()) == 15245
    for path in (proof.REGISTRY, proof.OVERLAY):
        raw = (proof.ROOT / path).read_bytes()
        restored = proof.predecessor_bytes(path, raw)
        assert proof.digest(restored) == proof.certificate()["files"][path]["previous"]
        with pytest.raises(ValueError):
            proof.predecessor_bytes(path, raw + b"\n")
