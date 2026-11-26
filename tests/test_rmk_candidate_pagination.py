from functools import lru_cache
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from src.scraper.company_careers.adapters import successfactors_table_recovery as reviewed
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.scanner import CompanyCareersScanner
from tests.successfactors_activation_fixtures import Response

CANDIDATES = Path(__file__).resolve().parents[1] / "reports/recovery-2026-11-25/sap-candidates.yaml"
BOARD = {
    "Aecon": ("Aecon Jobs", "en-US", "English (Canada)", 25),
    "John Deere": ("John Deere Jobs", "de-DE", "Deutsch (Deutschland)", 25),
    "Volvo Cars": ("Volvo Car Corporation Jobs", "en-US", "English", 20),
}


@lru_cache()
def candidate_targets():
    return {t.name: t for t in load_target_companies(str(CANDIDATES))}


class ListingSession:
    def __init__(self, name, scenario):
        self.name = name
        self.scenario = scenario
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        assert len(self.calls) <= 4
        title, lang, menu_label, size = BOARD[self.name]
        offset = int(parse_qs(urlsplit(url).query).get("startrow", ["0"])[0])
        assert "locale=" not in url
        query = "q=&sortColumn=referencedate&sortDirection=desc"
        if self.name == "Aecon":
            query += "&searchby=location&d=10"
        total = size + 1
        if self.scenario == "changed" and offset:
            total += 1
        next_link = f'<a href="?{escape(query)}&amp;startrow={size}">2</a>' if offset == 0 else ""
        selector = ""
        if offset:
            href = f"?{query}&startrow={offset}&locale={lang.replace('-', '_')}"
            if self.scenario == "menu_next":
                href = href.replace(f"startrow={offset}", f"startrow={offset+size}")
            selector = f'<a href="{escape(href)}" role="menuItem" lang="{lang}">{escape(menu_label)}</a>'
        if self.scenario == "scope":
            next_link = next_link.replace("&amp;d=10", "&amp;d=100")
        if self.scenario == "wrong_identity":
            title = "Different Employer"
        headers = '<th id="hdrTitle">Title</th><th id="hdrLocation">Location</th>'
        if self.name == "Volvo Cars":
            headers = "".join(f'<td><input data-column="{key}"/></td>' for key in ["title", "location", "department", "referencedate"])
        rows = []
        for i in range(offset, min(total, offset + size)):
            cells = f'<td class="coltitle" headers="hdrTitle"><a class="jobTitle-link" href="/job/Engineer/{100000+i}/">Engineer</a></td>'
            cells += '<td class="collocation" headers="hdrLocation">Edmonton</td>'
            if self.name == "Volvo Cars":
                cells += '<td class="coldepartment" headers="hdrDepartment">Engineering</td><td class="coldate" headers="hdrDate">Nov 25, 2026</td>'
                if self.scenario == "attribution":
                    cells = cells.replace('class="coldate"', 'class="collocation"')
            rows.append(f'<tr class="data-row">{cells}</tr>')
        body = (f'<html><title>{title}</title><span class="paginationLabel">Results {offset+1} – '
                f'{offset+len(rows)} of {total}</span>{selector}{next_link}<table id="searchresults">'
                f'<tr>{headers}</tr>{"".join(rows)}</table></html>')
        return Response(body, url)


@pytest.mark.parametrize("name", BOARD)
@pytest.mark.parametrize("scenario", ["stable", "changed", "menu_next", "wrong_identity"])
def test_candidate_in_production_scanner(name, scenario, monkeypatch):
    monkeypatch.setattr("src.scraper.company_careers.adapters.sap_successfactors.time.sleep", lambda _: None)
    target = candidate_targets()[name]
    assert reviewed.is_reviewed(target)
    session = ListingSession(name, scenario)
    result = CompanyCareersScanner(targets=[target], session=session, parent_source_mappings=[]).scan().results[0]
    assert result.authoritative_for_absence is (scenario == "stable")
    if scenario == "stable":
        assert len(session.calls) == 2
        assert result.fetched_count == BOARD[name][-1] + 1
    if scenario == "changed":
        assert len(session.calls) == 4
        assert result.source_health_code == "collection_changed"


@pytest.mark.parametrize("name,scenario", [("Aecon", "scope"), ("Volvo Cars", "attribution")])
def test_candidate_rejects_changed_contract(name, scenario):
    result = CompanyCareersScanner(targets=[candidate_targets()[name]], session=ListingSession(name, scenario),
                                   parent_source_mappings=[]).scan().results[0]
    assert not result.authoritative_for_absence


def test_certified_contracts_are_active_in_production():
    production = {t.name: t for t in load_target_companies(apply_reliability_repairs=True)}
    assert all(reviewed.is_reviewed(production[name]) and production[name] == candidate_targets()[name]
               for name in BOARD)
