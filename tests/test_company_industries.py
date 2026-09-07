import unittest

from src.integrations.company_industries import (
    INDUSTRY_GROUP_NAMES,
    CompanyIndustryConfigurationError,
    build_company_industry_lookup,
    load_company_industry_groups,
)
from src.scraper.company_careers.registry import load_target_companies


class CompanyIndustryConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.groups = load_company_industry_groups()

    def test_configuration_contains_exactly_the_six_approved_groups(self):
        self.assertEqual(tuple(self.groups), INDUSTRY_GROUP_NAMES)
        self.assertEqual(len(self.groups), 6)

    def test_every_production_target_has_exactly_one_industry(self):
        target_names = [company.name for company in load_target_companies()]

        lookup = build_company_industry_lookup(
            self.groups,
            expected_companies=target_names,
        )

        self.assertTrue(all(name.strip().casefold() in lookup for name in target_names))
        self.assertEqual(
            {
                name
                for companies in self.groups.values()
                for name in companies
                if name not in target_names
            },
            {
                "Google DeepMind",
                "Weights & Biases",
                "Universal Robots",
                "Instagram",
                "WhatsApp",
            },
        )

    def test_duplicate_company_assignment_is_rejected(self):
        groups = {title: [] for title in INDUSTRY_GROUP_NAMES}
        groups["Tech & AI"] = ["Example Co"]
        groups["Finance & Fintech"] = [" Example Co "]

        with self.assertRaisesRegex(
            CompanyIndustryConfigurationError,
            "assigned more than once",
        ):
            build_company_industry_lookup(groups)

    def test_unmapped_target_company_is_rejected(self):
        with self.assertRaisesRegex(
            CompanyIndustryConfigurationError,
            "Missing Co",
        ):
            build_company_industry_lookup(
                self.groups,
                expected_companies=["OpenAI", "Missing Co"],
            )

    def test_unapproved_or_missing_industry_group_is_rejected(self):
        groups = dict(self.groups)
        groups.pop("Consumer & Health")
        groups["Other"] = []

        with self.assertRaisesRegex(
            CompanyIndustryConfigurationError,
            "exactly the six approved groups",
        ):
            build_company_industry_lookup(groups)


if __name__ == "__main__":
    unittest.main()
