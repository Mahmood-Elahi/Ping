import unittest

from src.scraper.company_careers.models import CompanyTarget, JobRecord
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.student_roles import (
    STUDENT_ROLE_QUERIES,
    build_student_search_diagnostics,
    classify_student_location,
    configure_student_targets,
    detect_student_seasons,
    rank_student_jobs,
    score_student_job,
    student_job_exclusion_reason,
    student_job_signals,
    student_search_terms,
)


class SearchScopeExpansionV1Tests(unittest.TestCase):
    def job(self, title, location="Remote", *, metadata=None, job_id=None, source="fixture"):
        return JobRecord(
            job_id=job_id or title,
            title=title,
            company="Example",
            location=location,
            url=f"https://example.test/jobs/{job_id or len(title)}",
            updated_at="2026-08-01T00:00:00Z",
            source=source,
            raw_metadata=metadata or {},
        )

    def test_us_onsite_hybrid_and_remote_are_accepted(self):
        cases = {
            "Seattle, WA": "eligible_us_onsite",
            "Austin, Texas, United States - Hybrid": "eligible_us_hybrid",
            "Remote US": "eligible_remote_us",
            "Remote - USA only": "eligible_remote_us",
        }
        for location, expected in cases.items():
            with self.subTest(location=location):
                self.assertEqual(
                    classify_student_location(self.job("Software Intern", location)),
                    expected,
                )

    def test_canada_combined_and_remote_scope_are_accepted(self):
        cases = {
            "Canada": "eligible_canada",
            "Canada / United States": "eligible_canada",
            "Remote Canada": "eligible_remote_canada",
            "Remote North America": "eligible_remote_north_america",
            "Remote - Worldwide": "eligible_worldwide",
            "Remote": "eligible_remote_unspecified",
        }
        for location, expected in cases.items():
            with self.subTest(location=location):
                self.assertEqual(
                    classify_student_location(self.job("Firmware Intern", location)),
                    expected,
                )

    def test_international_roles_are_rejected_unless_worldwide_remote(self):
        for location in (
            "London, United Kingdom", "Paris, France", "Remote - Europe",
            "Mönsheim, DE, 71297", "Bengaluru, IN",
        ):
            with self.subTest(location=location):
                self.assertEqual(
                    classify_student_location(self.job("Software Intern", location)),
                    "excluded_other_location",
                )
        self.assertEqual(
            classify_student_location(self.job("Software Intern", "Remote Worldwide - Europe OK")),
            "eligible_worldwide",
        )

    def test_all_strong_student_title_variants(self):
        titles = (
            "Software INTERNS",
            "Data Internships",
            "Embedded CO-OPS",
            "Firmware Co/Op",
            "Electrical Co Op",
            "Robotics Students",
            "Student/Engineer",
            "Undergraduates - Hardware",
            "University-Interns",
            "Research/Intern",
            "Student-Researchers",
            "Summer Analysts",
            "Off-Cycle Intern",
            "Industrial Placements",
            "Year-in-Industry Engineer",
            "Working-Student",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(student_job_exclusion_reason(self.job(title)))

    def test_contextual_signals_require_corroboration(self):
        self.assertEqual(
            student_job_exclusion_reason(self.job("Summer Software Engineer")),
            "job_type",
        )
        structured = self.job(
            "Summer Software Engineer",
            metadata={"employmentType": "Temporary Internship"},
        )
        program = self.job(
            "Fall Hardware Engineer",
            metadata={"program_name": "University early talent"},
        )
        uncorroborated_pair = self.job("Summer Campus Engineer")
        self.assertIsNone(student_job_exclusion_reason(structured))
        self.assertIsNone(student_job_exclusion_reason(program))
        self.assertEqual(student_job_exclusion_reason(uncorroborated_pair), "job_type")
        self.assertEqual(
            student_job_signals(structured)["matching_rule"],
            "structured_job_type:internship",
        )

    def test_new_grad_and_permanent_entry_level_are_rejected(self):
        cases = (
            "New Grad Software Engineer",
            "New Graduate Engineer",
            "Graduate Program - Engineering",
            "Entry-Level Software Engineer",
            "Software Engineer I",
        )
        for title in cases:
            metadata = {"employment_type": "Full-time permanent"}
            with self.subTest(title=title):
                self.assertIsNotNone(student_job_exclusion_reason(self.job(title, metadata=metadata)))
        self.assertIsNone(
            student_job_exclusion_reason(self.job("Entry-Level Software Intern"))
        )

    def test_seniority_and_restricted_programs_are_rejected(self):
        for prefix in ("Senior", "Staff", "Principal", "Director", "Head", "VP", "Executive"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    student_job_exclusion_reason(self.job(f"{prefix} Product Manager Intern")),
                    "seniority",
                )
        for title in (
            "MBA Intern", "PhD Research Intern", "Master's Intern", "Postdoctoral Intern",
            "Experienced-Hire Program Intern",
        ):
            with self.subTest(title=title):
                self.assertEqual(student_job_exclusion_reason(self.job(title)), "eligibility")
        experienced_metadata = self.job(
            "Research Associate",
            metadata={"employment_type": "Intern", "category": ["Experienced Professionals"]},
        )
        self.assertEqual(student_job_exclusion_reason(experienced_metadata), "eligibility")

    def test_product_and_program_manager_intern_titles_are_accepted(self):
        for title in (
            "Product Manager Intern",
            "Program Manager Intern",
            "Technical Program Manager Intern",
        ):
            with self.subTest(title=title):
                self.assertIsNone(student_job_exclusion_reason(self.job(title)))
        self.assertEqual(
            student_job_exclusion_reason(self.job("Senior Product Manager Intern")),
            "seniority",
        )

    def test_query_expansion_is_exact_deterministic_and_board_bounded(self):
        expected = [
            "intern", "internship", "co-op", "student", "undergraduate",
            "engineering intern", "software intern", "research intern",
            "machine learning intern", "AI intern", "robotics intern", "embedded intern",
            "firmware intern", "hardware intern", "electrical engineering intern",
        ]
        self.assertEqual(student_search_terms(), expected)
        self.assertEqual(tuple(expected), STUDENT_ROLE_QUERIES)
        targets = [
            CompanyTarget(name="Google", platform="google", search_terms=["Staff"]),
            CompanyTarget(name="Board", platform="greenhouse", search_terms=["keep-me"]),
        ]
        configured = configure_student_targets(targets)
        self.assertEqual(configured[0].search_terms, expected)
        self.assertEqual(configured[1].search_terms, ["keep-me"])
        self.assertEqual(targets[0].search_terms, ["Staff"])

    def test_dedupe_uses_canonical_source_and_stable_job_id(self):
        first = self.job("Software Intern", job_id="stable-1", source="custom:google")
        second = self.job("Software Engineering Intern", job_id="stable-1", source="custom:google")
        second.url = "https://example.test/jobs/stable-1?query=internship"
        other_source = self.job("Software Intern", job_id="stable-1", source="custom:meta")
        deduped = CompanyCareersScanner(targets=[])._dedupe([first, second, other_source])
        self.assertEqual(deduped, [first, other_source])

    def test_season_detection_annotation_and_ranking_boost(self):
        targeted = self.job("Fall 2026 Machine Learning Intern", job_id="target")
        metadata_date = self.job(
            "Software Intern",
            metadata={"program": {"start_date": "2027-05-03"}},
            job_id="metadata",
        )
        plain = self.job("Machine Learning Intern", job_id="plain")
        self.assertEqual(detect_student_seasons(targeted), ["Fall 2026"])
        self.assertIn("May 2027", detect_student_seasons(metadata_date))
        ranked = rank_student_jobs([plain, targeted])
        self.assertEqual(ranked[0]["job_id"], "target")
        self.assertIn("target_season:Fall 2026", ranked[0]["positive_signals"])

    def test_priority_domains_rank_above_unrelated_student_roles(self):
        priority = self.job("FPGA ASIC Design Verification Intern", job_id="priority")
        unrelated = self.job("Marketing Intern", job_id="other")
        ranked = rank_student_jobs([unrelated, priority])
        self.assertEqual(ranked[0]["job_id"], "priority")
        self.assertIn("fpga_asic", ranked[0]["matched_categories"])

    def test_sponsorship_metadata_is_annotated_not_filtered(self):
        job = self.job(
            "Software Engineering Intern",
            "Austin, TX",
            metadata={
                "employment_type": "Internship",
                "visa_sponsorship": "Not available",
                "work_authorization": "US authorization required",
            },
        )
        self.assertIsNone(student_job_exclusion_reason(job))
        scored = score_student_job(job)
        self.assertEqual(scored["work_authorization"]["visa_sponsorship"], "Not available")
        self.assertEqual(
            scored["work_authorization"]["work_authorization"],
            "US authorization required",
        )

    def test_diagnostics_compare_same_fixture_before_and_after(self):
        fixtures = [
            self.job("Software Intern", "Seattle, WA", job_id="us"),
            self.job("Product Manager Intern", "Canada", job_id="pm"),
            self.job("Summer Analyst", "Remote", job_id="analyst"),
            self.job("Senior Software Intern", "Canada", job_id="senior"),
            self.job("Software Engineer", "Canada", job_id="permanent"),
            self.job("Software Intern", "London, UK", job_id="uk"),
        ]
        diagnostics = build_student_search_diagnostics(fixtures)
        self.assertEqual(diagnostics["total_normalized_jobs"], 6)
        self.assertEqual(diagnostics["before_retained_jobs"], 0)
        self.assertEqual(diagnostics["after_retained_jobs"], 3)
        self.assertEqual(diagnostics["exclusions_by_reason"], {"job_type": 1, "location": 1, "seniority": 1})
        self.assertEqual(sum(diagnostics["newly_accepted_jobs_by_matching_rule"].values()), 3)
        self.assertTrue(diagnostics["newly_accepted_examples"])
        self.assertTrue(diagnostics["still_rejected_examples"])


if __name__ == "__main__":
    unittest.main()
