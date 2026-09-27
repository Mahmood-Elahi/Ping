from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path

import yaml

from scripts.validate_company_registry import source_key
from src.scraper.company_careers.models import CompanyTarget


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "target_companies.yaml"
EXPECTED_PLATFORMS = {
    "workday": 35,
    "ashby": 9,
    "sap_successfactors": 8,
    "greenhouse": 7,
    "smartrecruiters": 7,
    "lever": 6,
    "static_html": 2,
    "talentbrew": 1,
}
RECOVERED_SMARTRECRUITERS_BOUNDS = {
    "Assent": (1, 30),
    "Arista Networks": (3, 300),
    "Gameloft": (1, 75),
    "Ubisoft": (4, 350),
}


class CanadianEmployerExpansionV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        entries = (yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {})[
            "target_companies"
        ]
        start = next(index for index, entry in enumerate(entries) if entry["name"] == "Ada")
        cls.entries = entries
        # This test documents the bounded 75-employer V4 artifact. Later
        # production additions must not retroactively change that artifact.
        cls.additions = entries[start : start + 75]

    def test_exactly_75_additions_with_unique_names_and_sources(self) -> None:
        self.assertEqual(len(self.additions), 75)
        names = [" ".join(entry["name"].casefold().split()) for entry in self.entries]
        self.assertEqual(len(names), len(set(names)))
        keys = [source_key(entry) for entry in self.additions]
        self.assertNotIn(None, keys)
        self.assertEqual(len(keys), len(set(keys)))

    def test_platform_mix_and_high_value_canadian_gaps(self) -> None:
        self.assertEqual(
            Counter(entry["platform"] for entry in self.additions),
            Counter(EXPECTED_PLATFORMS),
        )
        names = {entry["name"] for entry in self.additions}
        expected = {
            "Lumentum", "EXFO", "Solace", "Arista Networks", "Trimble",
            "SaskPower", "FortisBC", "EPCOR", "AIMCo", "AltaML",
            "Interac", "HOOPP", "Aecon", "EllisDon", "NAV CANADA",
        }
        self.assertTrue(expected.issubset(names))

    def test_every_addition_loads_without_new_schema_fields(self) -> None:
        for entry in self.additions:
            target = CompanyTarget.from_dict(entry)
            self.assertTrue(target.name)
            self.assertTrue(target.careers_url)
            recovered = RECOVERED_SMARTRECRUITERS_BOUNDS.get(target.name)
            if recovered:
                self.assertEqual((target.max_pages, target.max_jobs), recovered)
            else:
                self.assertLessEqual(target.max_pages, 1)
                self.assertLessEqual(target.max_jobs, 20)

    def test_no_new_defense_or_manual_sources(self) -> None:
        forbidden = {"boeing", "lockheed martin", "raytheon", "ultra maritime", "pratt & whitney"}
        names = {entry["name"].casefold() for entry in self.additions}
        self.assertTrue(names.isdisjoint(forbidden))
        self.assertFalse(any(entry["platform"] in {"manual", "manual_review"} for entry in self.additions))


if __name__ == "__main__":
    unittest.main()
