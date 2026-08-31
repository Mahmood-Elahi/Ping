from __future__ import annotations

import unittest

from src.scraper.company_careers.models import JobRecord
from src.scraper.company_careers.student_roles import (
    _student_relevance_band,
    rank_student_jobs,
    score_student_job,
    student_job_exclusion_reason,
)


def job(
    job_id: str,
    title: str,
    *,
    company: str = "Example",
    location: str = "Calgary, Alberta, Canada",
    description: str | None = None,
    raw_metadata: dict | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        url=f"https://example.com/jobs/{job_id}",
        updated_at="",
        source="test",
        description=description,
        raw_metadata=raw_metadata or {},
    )


class JobRankingV2CharacterizationTests(unittest.TestCase):
    def test_relevance_bands_use_v2_calibrated_score_boundaries(self):
        expected_bands = {
            100: "high",
            57: "high",
            56: "good",
            55: "good",
            54: "possible",
            53: "low",
            46: "low",
            45: "low",
            0: "low",
        }

        for score, expected_band in expected_bands.items():
            with self.subTest(score=score):
                self.assertEqual(_student_relevance_band(score), expected_band)

    def test_canadian_role_beats_equivalent_us_onsite_role(self):
        canada = job(
            "ca",
            "Firmware Engineering Intern",
            location="Calgary, Alberta, Canada",
        )
        us = job(
            "us",
            "Firmware Engineering Intern",
            location="San Francisco, California, USA",
        )

        canada_score = score_student_job(canada)["score"]
        us_score = score_student_job(us)["score"]

        self.assertGreater(canada_score, us_score)

    def test_explicit_intern_title_beats_metadata_only_student_classification(self):
        explicit = job(
            "explicit",
            "Software Engineer Intern",
        )
        metadata_only = job(
            "metadata",
            "Software Engineer",
            raw_metadata={"employmentType": "Internship"},
        )

        explicit_score = score_student_job(explicit)["score"]
        metadata_score = score_student_job(metadata_only)["score"]

        self.assertGreater(explicit_score, metadata_score)

    def test_explicit_undergraduate_eligibility_beats_not_stated(self):
        confirmed = job(
            "confirmed",
            "Embedded Systems Intern",
            description=(
                "Candidates must be currently pursuing a bachelor's degree "
                "in electrical or computer engineering."
            ),
        )
        unknown = job(
            "unknown",
            "Embedded Systems Intern",
        )

        confirmed_result = score_student_job(confirmed)
        unknown_result = score_student_job(unknown)

        self.assertEqual(
            confirmed_result["education_eligibility"],
            "undergraduate_allowed",
        )
        self.assertGreater(
            confirmed_result["score"],
            unknown_result["score"],
        )

    def test_financial_control_is_not_controls_engineering(self):
        financial = job(
            "finance",
            "Student, Financial Control Specialist (Winter 2027)",
            company="Sun Life",
            location="Toronto, Ontario, Canada",
        )

        result = score_student_job(financial)

        self.assertNotIn("controls", result["matched_categories"])

    def test_specialized_firmware_role_is_not_below_generic_software(self):
        firmware = job(
            "firmware",
            "Firmware Engineering Intern",
        )
        software = job(
            "software",
            "Software Engineering Intern",
        )

        firmware_score = score_student_job(firmware)["score"]
        software_score = score_student_job(software)["score"]

        self.assertGreaterEqual(firmware_score, software_score)

    def test_exact_duplicate_jobs_are_removed_before_final_ranking(self):
        first = job(
            "duplicate-1",
            "Software Verification Engineer (Co-op/Intern)",
            company="Lumentum",
            location="Canada - Ottawa",
        )
        second = job(
            "duplicate-1",
            "Software Verification Engineer (Co-op/Intern)",
            company="Lumentum",
            location="Canada - Ottawa",
        )

        ranked = rank_student_jobs([first, second])

        self.assertEqual(len(ranked), 1)

    def test_explicit_phd_student_researcher_is_excluded_but_general_role_is_retained(self):
        phd_only = job(
            "phd",
            "Student Researcher (AI Foundation Models) - 2026 Start (PhD)",
        )
        general = job(
            "general",
            "Student Researcher, AI Infrastructure",
        )
        mixed = job(
            "mixed",
            "Student Researcher (Bachelor's/Master's/PhD)",
        )

        self.assertEqual(student_job_exclusion_reason(phd_only), "graduate_only")
        self.assertIsNone(student_job_exclusion_reason(general))
        self.assertIsNone(student_job_exclusion_reason(mixed))

    def test_research_domain_credit_requires_a_technical_research_role(self):
        positive_titles = (
            "Machine Learning Researcher",
            "Research Scientist Intern, AI",
            "AI Research Engineer Intern",
            "Applied Scientist Intern",
            "Robotics Research Intern",
        )
        negative_titles = (
            "Investment Research Intern",
            "Economics Research Student",
            "UX Research Intern",
            "Market Research Intern",
            "Creative Design Research Intern",
        )

        for index, title in enumerate(positive_titles):
            with self.subTest(title=title):
                result = score_student_job(job(f"positive-{index}", title))
                self.assertIn("research_applied_science", result["matched_categories"])
        for index, title in enumerate(negative_titles):
            with self.subTest(title=title):
                result = score_student_job(job(f"negative-{index}", title))
                self.assertNotIn("research_applied_science", result["matched_categories"])

    def test_no_domain_student_job_is_penalized_but_remains_rankable(self):
        accounting = job(
            "accounting",
            "Accounting Student Intern (Winter 2027)",
            description="Bachelor's students are eligible to apply.",
        )
        asic = job(
            "asic",
            "ASIC Engineering Student Intern (Winter 2027)",
            description="Bachelor's students are eligible to apply.",
        )

        accounting_result = score_student_job(accounting)
        ranked = rank_student_jobs([accounting, asic])

        self.assertGreater(accounting_result["score"], 0)
        self.assertEqual(accounting_result["matched_categories"], [])
        self.assertIn("no_target_domain_match", accounting_result["negative_signals"])
        self.assertEqual([item["job_id"] for item in ranked], ["asic", "accounting"])

    def test_explicit_military_and_veteran_programs_are_restricted(self):
        restricted_titles = (
            "Veteran Internship Program",
            "Military Veteran Internship",
            "Transitioning Service Member Internship",
        )
        for index, title in enumerate(restricted_titles):
            with self.subTest(title=title):
                self.assertEqual(
                    student_job_exclusion_reason(job(f"restricted-{index}", title)),
                    "eligibility",
                )

    def test_veteran_eeo_language_does_not_restrict_an_ordinary_internship(self):
        descriptions = (
            "We do not discriminate based on protected veteran status.",
            "Veterans are encouraged to apply.",
        )
        for index, description in enumerate(descriptions):
            with self.subTest(description=description):
                ordinary = job(
                    f"ordinary-{index}",
                    "Software Engineering Intern",
                    description=description,
                )
                self.assertIsNone(student_job_exclusion_reason(ordinary))

    def test_explicit_description_restriction_is_detected(self):
        restricted = job(
            "description-restricted",
            "Software Engineering Intern",
            description=(
                "Eligibility is limited to active duty transitioning service members "
                "and military veterans only."
            ),
        )

        self.assertEqual(student_job_exclusion_reason(restricted), "eligibility")

    def test_named_university_partner_program_gets_manual_review_signal(self):
        named_partner = job("drexel", "AI Co-op with Drexel University")
        generic_titles = (
            "University Intern",
            "University Recruiting Software Intern",
            "Software Intern for University Students",
            "Campus Internship",
            "Campus Software Engineer Intern",
        )

        named_result = score_student_job(named_partner)

        self.assertIn("university_partner_program", named_result["negative_signals"])
        self.assertGreater(named_result["score"], 0)
        self.assertIsNone(student_job_exclusion_reason(named_partner))
        for index, title in enumerate(generic_titles):
            with self.subTest(title=title):
                result = score_student_job(job(f"generic-{index}", title))
                self.assertNotIn("university_partner_program", result["negative_signals"])


if __name__ == "__main__":
    unittest.main()
