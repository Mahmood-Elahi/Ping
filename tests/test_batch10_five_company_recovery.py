from __future__ import annotations

from dataclasses import replace
import json
import unittest

from src.scraper.company_careers.adapters import public_json, static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(
        self,
        value,
        *,
        url: str,
        status: int = 200,
        content_type: str | None = None,
    ) -> None:
        self.status_code = status
        self.url = url
        self.value = value
        self.text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        self.content = self.text.encode("utf-8")
        self.headers = {
            "content-type": content_type
            or ("application/json" if isinstance(value, (dict, list)) else "text/html")
        }
        self.history = []

    def json(self):
        if not isinstance(self.value, (dict, list)):
            raise ValueError("not JSON")
        return self.value


class Session:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.responses.pop(0)


def configured(name: str) -> CompanyTarget:
    return next(
        target
        for target in load_target_companies(apply_reliability_repairs=True)
        if target.name == name
    )


def box_page(job_id: int, *, next_href: str | None) -> str:
    next_link = (
        f'<li class="page-item next"><a rel="next" href="{next_href}">Next</a></li>'
        if next_href
        else ""
    )
    return f"""
    <html><body><div id="js-job-search-results">
      <div class="card-job" data-id="{job_id}"><h2 class="card-title">
        <a href="/en/jobs/{job_id}/engineer-{job_id}/">Engineer {job_id}</a></h2>
        <ul class="job-meta"><li>Warsaw, Poland</li><li>Engineering</li></ul>
      </div>
    </div><nav class="pagination"><ul>{next_link}</ul></nav></body></html>
    """


def espressif_page(job_id: str, *, next_href: str | None) -> str:
    next_link = (
        f'<li class="pager-next"><a href="{next_href}">next</a></li>'
        if next_href
        else ""
    )
    return f"""
    <html><body><div class="view-job-search"><div class="item-wrap">
      <div class="item-title">Embedded Engineer {job_id}</div>
      <div class="item-location">Singapore</div>
      <div class="item-function">Engineering</div>
      <a class="item-share" data-share="https://www.espressif.com/job-detail/{job_id}">Share</a>
    </div></div><ul class="pager">{next_link}</ul></body></html>
    """


def visible_total_page(job_ids: list[int], *, total: int, has_next: bool) -> str:
    cards = "".join(
        f"""
        <div class="attrax-vacancy-tile" data-jobid="{job_id}">
          <a class="attrax-vacancy-tile__title"
             href="/job/engineer-in-london-jid-{job_id}">Engineer {job_id}</a>
          <div class="attrax-vacancy-tile__location-freetext">
            <p class="attrax-vacancy-tile__item-value">London</p></div>
          <div class="attrax-vacancy-tile__option-job-family-group">
            <p class="attrax-vacancy-tile__item-value">Technology</p></div>
        </div>
        """
        for job_id in job_ids
    )
    next_link = (
        '<a href="javascript:pagination(2)" aria-label="Next pagination page">Next</a>'
        if has_next
        else ""
    )
    return (
        '<html><body><div class="attrax-list-pagination-widget">'
        f'<span class="attrax-pagination__total-results">{total} results</span>'
        f" 1 2 Results per page 12 24 48 {next_link}</div>{cards}</body></html>"
    )


def dyson_identity(target: CompanyTarget, *, script_url: str | None = None) -> Response:
    script_url = script_url or "https://careers.dyson.com/js/search-fixture.min.js"
    return Response(
        f"<html><body>Dyson Careers<script src='{script_url}'></script></body></html>",
        url=str(target.careers_url),
    )


def dyson_row(index: int) -> dict:
    external_id = f"JR{38000 + index}"
    return {
        "id": external_id,
        "title": f"Engineer {index}",
        "teamsText": "Engineering",
        "locationsText": "United Kingdom - Malmesbury Office",
        "description": "Build reliable products.",
        "url": (
            "http://careers.dyson.com/en-gb/job-description/"
            f"engineer-{index}/{external_id}/"
        ),
        "postedDateIso": "2026-10-06T07:00:00+00:00",
    }


def dyson_payload(rows: list[dict], *, total: int, page: int) -> dict:
    return {
        "success": True,
        "totalItems": total,
        "isFirst": page == 1,
        "isLast": page * public_json.DYSON_PAGE_SIZE >= total,
        "originalQuery": {"page": page, "minPage": page, "maxPage": page},
        "items": rows,
    }


def dyson_responses(target: CompanyTarget, payloads: list[dict]) -> list[Response]:
    return [
        dyson_identity(target),
        Response(
            'search.url="/"+site.html.attr("lang")+"/api/search/jobs/"',
            url="https://careers.dyson.com/js/search-fixture.min.js",
            content_type="application/javascript",
        ),
        *[
            Response(payload, url=str(target.endpoint_url))
            for payload in payloads
        ],
    ]


def roblox_row(job_id: int = 123) -> dict:
    return {
        "id": job_id,
        "title": "Software Engineer",
        "location": "San Mateo, CA, United States",
        "employment_type": "Salaried Employee",
        "groups": ["Engineering"],
        "department": ["Infrastructure"],
        "early_career": False,
    }


def roblox_responses(target: CompanyTarget, payload, *, bound: bool = True) -> list[Response]:
    script_url = "https://careers.roblox.com/_next/static/chunks/fixture.js"
    return [
        Response(
            f"<html><body>Roblox Careers<script src='{script_url}'></script></body></html>",
            url=str(target.careers_url),
        ),
        Response(
            public_json.ROBLOX_ENDPOINT if bound else "no reviewed endpoint",
            url=script_url,
            content_type="application/javascript",
        ),
        Response(payload, url=str(target.endpoint_url)),
    ]


class Batch10FiveCompanyRegistryTests(unittest.TestCase):
    def test_recovered_sources_and_unresolved_reversions_are_explicit(self) -> None:
        expected = {
            "Box": ("static_html", "server_rendered_cards", 1, 20),
            "Dyson": ("public_json", public_json.DYSON_JOBS, 50, 500),
            "Espressif Systems": ("static_html", "server_rendered_cards", 1, 20),
            "Roblox": ("public_json", public_json.ROBLOX_JOBS, 1, 500),
            "Wise": ("static_html", "server_rendered_cards", 1, 20),
        }
        for name, contract in expected.items():
            with self.subTest(name=name):
                target = configured(name)
                self.assertEqual(
                    (target.platform, target.endpoint_variant, target.max_pages, target.max_jobs),
                    contract,
                )
        self.assertIsNone(configured("Box").next_page_selector)
        self.assertIsNone(configured("Espressif Systems").next_page_selector)
        self.assertIsNone(configured("Box").pagination_contract)
        self.assertIsNone(configured("Espressif Systems").pagination_contract)
        self.assertIsNone(configured("Wise").pagination_contract)
        self.assertIsNone(configured("Wise").pagination_url_template)


class StaticPaginationRecoveryTests(unittest.TestCase):
    def test_visible_total_is_generic_and_fails_closed_on_conflicts(self) -> None:
        first_url = "https://careers.example.test/jobs?page=1&size=48"
        second_url = "https://careers.example.test/jobs?page=2&size=48"
        target = CompanyTarget(
            name="Visible Total Fixture",
            platform="static_html",
            pagination_contract=static_html.REVIEWED_PAGINATION_CONTRACT,
            careers_url=first_url,
            listing_url=first_url,
            endpoint_variant="server_rendered_cards",
            listing_selector="body",
            job_card_selector="div.attrax-vacancy-tile",
            title_selector="a.attrax-vacancy-tile__title",
            detail_link_selector="a.attrax-vacancy-tile__title",
            external_id_attribute="data-jobid",
            location_selector=(
                "div.attrax-vacancy-tile__location-freetext "
                "p.attrax-vacancy-tile__item-value"
            ),
            department_selector=(
                "div.attrax-vacancy-tile__option-job-family-group "
                "p.attrax-vacancy-tile__item-value"
            ),
            pagination_url_template=(
                "https://careers.example.test/jobs?page={page}&size=48"
            ),
            allowed_apply_hosts=["careers.example.test"],
            max_pages=2,
            max_jobs=500,
        )
        session = Session(
            [
                Response(
                    visible_total_page([1, 2], total=3, has_next=True),
                    url=first_url,
                ),
                Response(
                    visible_total_page([3], total=3, has_next=False),
                    url=second_url,
                ),
            ]
        )
        jobs = static_html.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 3)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")

        conflicting = (
            visible_total_page([1], total=3, has_next=False).replace(
                "</body>",
                '<span class="attrax-pagination__total-results">4 results</span></body>',
            )
        )
        conflict_target = replace(target, max_pages=1)
        conflict_session = Session(
            [
                Response(conflicting, url=first_url),
                Response(conflicting, url=second_url),
            ]
        )
        static_html.fetch_jobs(conflict_session, conflict_target)
        conflict_metadata = source_scan_metadata(conflict_session)
        self.assertFalse(conflict_metadata["scan_complete"])
        self.assertTrue(conflict_metadata["pagination_evidence_conflicting"])


class DysonPublicJsonRecoveryTests(unittest.TestCase):
    def test_counted_api_enumerates_to_the_authoritative_total(self) -> None:
        target = configured("Dyson")
        rows = [dyson_row(index) for index in range(11)]
        session = Session(
            dyson_responses(
                target,
                [
                    dyson_payload(rows[:10], total=11, page=1),
                    dyson_payload(rows[10:], total=11, page=2),
                ],
            )
        )
        jobs = public_json.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 11)
        self.assertEqual(len({job.job_id for job in jobs}), 11)
        self.assertTrue(all(job.url.startswith("https://careers.dyson.com/") for job in jobs))
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 11)
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")
        self.assertEqual(metadata["unique_stable_ids"], 11)
        self.assertEqual(metadata["unique_canonical_urls"], 11)
        self.assertTrue(metadata["stable_ids_unique"])
        self.assertTrue(metadata["canonical_urls_unique"])

    def test_missing_official_binding_and_duplicate_ids_fail_closed(self) -> None:
        target = configured("Dyson")
        responses = dyson_responses(target, [dyson_payload([dyson_row(1)], total=1, page=1)])
        responses[1] = Response(
            "no reviewed endpoint",
            url="https://careers.dyson.com/js/search-fixture.min.js",
            content_type="application/javascript",
        )
        with self.assertRaisesRegex(AdapterError, "did not bind"):
            public_json.fetch_jobs(Session(responses), target)

        duplicate = dyson_row(1)
        payload = dyson_payload([duplicate, dict(duplicate)], total=2, page=1)
        duplicate_session = Session(dyson_responses(target, [payload]))
        with self.assertRaisesRegex(AdapterError, "repeated"):
            public_json.fetch_jobs(duplicate_session, target)
        duplicate_metadata = source_scan_metadata(duplicate_session)
        self.assertEqual(duplicate_metadata["unique_stable_ids"], 1)
        self.assertEqual(duplicate_metadata["unique_canonical_urls"], 1)
        self.assertEqual(len(duplicate_metadata["duplicate_ids"]), 1)
        self.assertEqual(len(duplicate_metadata["duplicate_canonical_urls"]), 1)


class RobloxExistingRecoveryContractTests(unittest.TestCase):
    def test_atomic_feed_remains_complete_and_requires_official_binding(self) -> None:
        target = configured("Roblox")
        session = Session(roblox_responses(target, [roblox_row()]))
        jobs = public_json.fetch_jobs(session, target)
        self.assertEqual(len(jobs), 1)
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["unique_stable_ids"], 1)
        self.assertEqual(metadata["unique_canonical_urls"], 1)

        with self.assertRaisesRegex(AdapterError, "did not bind"):
            public_json.fetch_jobs(
                Session(roblox_responses(target, [roblox_row()], bound=False)),
                target,
            )


if __name__ == "__main__":
    unittest.main()
