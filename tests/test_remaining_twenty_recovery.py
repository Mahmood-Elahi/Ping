from __future__ import annotations

import unittest

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


RECOVERED = {
    "Computer Modelling Group (CMG)",
    "H2O.ai",
    "Mitsubishi Electric",
    "Ouster",
    "Symend",
    "TEKTELIC Communications",
}


class Response:
    def __init__(self, body: str, url: str) -> None:
        self.status_code = 200
        self.url = url
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = {"content-type": "text/html"}
        self.history = []


class Session:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs) -> Response:
        self.calls.append(url)
        return Response(self.body, url)


def jazzhr_page() -> str:
    return """
    <html><body><ul class="list-group">
      <li class="list-group-item">
        <h3 class="list-group-item-heading">
          <a href="/apply/stable-role-1/software-engineer">Software Engineer</a>
        </h3>
        <ul class="list-inline list-group-item-text">
          <li>Calgary, Alberta</li><li>Engineering</li>
        </ul>
      </li>
    </ul></body></html>
    """


def company_page(name: str) -> str:
    if name == "Computer Modelling Group (CMG)":
        return """
        <html><body><div class="elementor-element-bbaea52">
          <div class="jet-listing-grid__item" data-post-id="17884">
            <h3 class="elementor-heading-title">Cloud Developer</h3>
            <div class="elementor-element-ae0a93e">
              <span class="elementor-heading-title">Calgary, AB</span>
            </div>
            <div class="jet-engine-listing-overlay-wrap"
                 data-url="https://www.cmgl.ca/careers/cloud-developer/"></div>
          </div>
        </div></body></html>
        """
    if name in {"H2O.ai", "Ouster", "Symend"}:
        return jazzhr_page()
    if name == "Mitsubishi Electric":
        return """
        <html><body><table class="reqResultTable"><tr data-req-id="123">
          <td class="posTitle"><a href="/employment/job-opening.php?req=123&amp;req_loc=456">
            Controls Engineer</a></td>
          <td class="cities">Calgary</td><td class="state">AB</td>
          <td class="custSort1">Engineering</td>
        </tr></table></body></html>
        """
    if name == "TEKTELIC Communications":
        return """
        <html><body><div class="vacancies-list"><div class="col-lg-12">
          <p class="vacancy-title">Embedded Engineer</p>
          <div class="loc"><span>Calgary, AB</span></div>
          <a href="https://tektelic.com/career/vacancies/embedded-engineer/">Apply</a>
        </div></div></body></html>
        """
    raise AssertionError(name)


class RemainingTwentyRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    def test_only_reviewed_atomic_sources_are_enabled(self):
        for name in RECOVERED:
            with self.subTest(name=name):
                target = self.targets[name]
                self.assertEqual(target.platform, "static_html")
                self.assertTrue(target.single_page_collection)
                self.assertFalse(target.pagination_url_template)
                self.assertFalse(target.next_page_selector)
                self.assertEqual(target.max_pages, 1)

        unresolved = {
            "Athabasca Oil",
            "Cameco",
            "General Motors",
            "ServiceNow",
            "Zalando",
            "ecobee",
            "u-blox",
        }
        for name in unresolved:
            with self.subTest(name=name):
                self.assertFalse(self.targets[name].single_page_collection)

    def test_malformed_structured_sources_remain_fail_closed(self):
        chemelex = self.targets["Chemelex"]
        novarc = self.targets["Novarc Technologies"]
        self.assertEqual((chemelex.platform, chemelex.max_pages, chemelex.max_jobs), ("dayforce", 4, 100))
        self.assertEqual((novarc.platform, novarc.max_pages, novarc.max_jobs), ("adp", 2, 20))


class ReviewedAtomicCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    def test_each_reviewed_collection_completes_atomically(self):
        for name in sorted(RECOVERED):
            with self.subTest(name=name):
                target = self.targets[name]
                session = Session(company_page(name))
                jobs = static_html.fetch_jobs(session, target)
                metadata = source_scan_metadata(session)

                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(len({job.job_id for job in jobs}), 1)
                self.assertEqual(len({job.url for job in jobs}), 1)
                self.assertTrue(metadata["scan_complete"])
                self.assertEqual(
                    metadata["terminal_evidence_type"],
                    "configured_single_page_collection",
                )
                self.assertEqual(metadata["rejected_record_count"], 0)

    def test_single_page_contract_cannot_hide_pagination(self):
        target = CompanyTarget(
            name="Contradictory atomic fixture",
            platform="static_html",
            listing_url="https://jobs.example.test/jobs",
            endpoint_variant="server_rendered_cards",
            listing_selector="main",
            job_card_selector="article",
            title_selector="a",
            detail_link_selector="a",
            external_id_attribute="data-id",
            next_page_selector="a.next",
            allowed_apply_hosts=["jobs.example.test"],
            single_page_collection=True,
            max_pages=1,
            max_jobs=20,
        )
        with self.assertRaisesRegex(AdapterError, "cannot define pagination"):
            static_html.fetch_jobs(Session("<main></main>"), target)

    def test_different_ids_cannot_share_one_canonical_url(self):
        target = CompanyTarget(
            name="Canonical conflict fixture",
            platform="static_html",
            listing_url="https://jobs.example.test/jobs",
            endpoint_variant="server_rendered_cards",
            listing_selector="main",
            job_card_selector="article",
            title_selector="a.title",
            detail_link_selector="a.title",
            external_id_attribute="data-id",
            allowed_apply_hosts=["jobs.example.test"],
            single_page_collection=True,
            max_pages=1,
            max_jobs=20,
        )
        body = """
        <html><body><main>
          <article data-id="one"><a class="title" href="/jobs/shared">Engineer</a></article>
          <article data-id="two"><a class="title" href="/jobs/shared">Engineer</a></article>
        </main></body></html>
        """
        session = Session(body)
        with self.assertRaisesRegex(AdapterError, "canonical job URL mapped"):
            static_html.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "conflicting_canonical_url")


if __name__ == "__main__":
    unittest.main()
