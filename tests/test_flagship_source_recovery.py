from __future__ import annotations

import unittest

from scripts.validate_company_registry import source_key
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import apple
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


class FlagshipSourceRecoveryRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = load_target_companies()
        cls.effective = load_target_companies(apply_reliability_repairs=True)
        cls.raw_by_name = {target.name: target for target in cls.raw}
        cls.effective_by_name = {target.name: target for target in cls.effective}

    def test_production_count_and_names_remain_canonical(self):
        self.assertEqual(len(self.raw), 533)
        self.assertEqual(len(self.effective), 533)
        names = [target.name.casefold() for target in self.effective]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("tesla", names)

    def test_nvidia_remains_on_its_healthy_workday_source(self):
        target = self.effective_by_name["Nvidia"]
        self.assertEqual(
            (target.platform, target.tenant, target.wd_subdomain, target.site_id),
            ("workday", "nvidia", "wd5", "NVIDIAExternalCareerSite"),
        )

    def test_apple_production_source_is_first_party_api(self):
        self.assertEqual(self.raw_by_name["Apple"].platform, "apple")
        target = self.effective_by_name["Apple"]
        self.assertEqual(target.platform, "apple")
        self.assertEqual(target.endpoint_url, apple.SEARCH_ENDPOINT)
        self.assertEqual(target.endpoint_variant, apple.ENDPOINT_VARIANT)
        self.assertEqual(target.allowed_apply_hosts, ["jobs.apple.com"])
        self.assertIs(scanner.ADAPTERS["apple"], apple.fetch_jobs)

    def test_waymo_and_spacex_keep_existing_validated_adapters(self):
        waymo = self.effective_by_name["Waymo"]
        self.assertEqual(waymo.platform, "static_html")
        self.assertEqual(waymo.listing_url, "https://careers.withwaymo.com/jobs/search")
        self.assertEqual(waymo.allowed_apply_hosts, ["careers.withwaymo.com"])
        spacex = self.effective_by_name["SpaceX"]
        self.assertEqual((spacex.platform, spacex.slug), ("greenhouse", "spacex"))
        self.assertEqual(spacex.allowed_apply_hosts, ["boards.greenhouse.io"])

    def test_deepmind_uses_one_exact_google_parent_mapping(self):
        mappings = [
            mapping
            for mapping in load_parent_source_mappings()
            if mapping.child_company == "Google DeepMind"
        ]
        self.assertEqual(len(mappings), 1)
        mapping = mappings[0]
        self.assertEqual(mapping.parent_company, "Google")
        self.assertEqual(mapping.canonical_source, "custom:google")
        self.assertEqual(mapping.match_field, "departments")
        self.assertEqual(mapping.exact_values, ("DeepMind",))
        self.assertNotIn("Google DeepMind", self.effective_by_name)

    def test_effective_flagship_sources_have_no_duplicate_canonical_source(self):
        names = {"Nvidia", "Apple", "Waymo", "SpaceX"}
        entries = [
            {
                "name": target.name,
                "platform": target.platform,
                "tenant": target.tenant,
                "wd_subdomain": target.wd_subdomain,
                "site_id": target.site_id,
                "slug": target.slug,
                "endpoint_url": target.endpoint_url,
                "listing_url": target.listing_url,
            }
            for target in self.effective
            if target.name in names
        ]
        keys = [source_key(entry) for entry in entries]
        self.assertTrue(all(keys))
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
