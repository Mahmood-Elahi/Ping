from __future__ import annotations

import unittest

from lxml import html

from scripts.validate_company_registry import source_key
from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import CompanyTarget


def _target(slug: str) -> CompanyTarget:
    return CompanyTarget(
        name=slug.title(),
        platform="static_html",
        slug=slug,
        listing_url=f"https://jobs.jobvite.com/{slug}/jobs",
        endpoint_variant="server_rendered_cards",
        listing_selector="table",
        job_card_selector="tr",
        title_selector="td.jv-job-list-name a",
        location_selector="td.jv-job-list-location",
        detail_link_selector="td.jv-job-list-name a",
        external_id_regex=rf"/{slug}/job/([^/?#]+)",
        allowed_apply_hosts=["jobs.jobvite.com"],
        max_pages=1,
        max_jobs=20,
    )


class StaticHtmlSharedHostNamespaceTests(unittest.TestCase):
    def test_static_html_slug_namespaces_shared_ats_host(self) -> None:
        target = _target("epcor")
        card = html.fromstring(
            '<tr><td class="jv-job-list-name"><a href="/epcor/job/abc123">Engineering Student</a></td>'
            '<td class="jv-job-list-location">Edmonton, Alberta</td></tr>'
        )
        job = static_html._normalize(card, target, target.listing_url or "")
        self.assertEqual(job.source, "static_html:epcor")
        self.assertEqual(job.job_id, "static_html:epcor:abc123")

    def test_static_html_registry_source_key_uses_optional_slug(self) -> None:
        epcor = {"platform": "static_html", "slug": "epcor", "listing_url": "https://jobs.jobvite.com/epcor/jobs"}
        parkland = {"platform": "static_html", "slug": "parkland", "listing_url": "https://jobs.jobvite.com/parkland/jobs"}
        self.assertEqual(source_key(epcor), "static_html:epcor")
        self.assertEqual(source_key(parkland), "static_html:parkland")


if __name__ == "__main__":
    unittest.main()
