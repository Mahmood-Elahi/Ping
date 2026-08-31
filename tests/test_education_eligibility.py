import unittest

from src.scraper.company_careers.education_eligibility import (
    AMBIGUOUS,
    GRADUATE_ONLY,
    NOT_STATED,
    UNDERGRADUATE_ALLOWED,
    classify_education_eligibility,
)
from src.scraper.company_careers.models import JobRecord
from src.scraper.company_careers.student_roles import (
    build_student_search_diagnostics,
    rank_student_jobs,
    student_job_exclusion_reason,
)


class EducationEligibilityTests(unittest.TestCase):
    def job(self, *, title="Software Intern", description=None, metadata=None, job_id="1"):
        return JobRecord(
            job_id=job_id,
            title=title,
            company="Example Co",
            location="Remote",
            url=f"https://example.test/jobs/{job_id}",
            updated_at="2026-08-01T00:00:00Z",
            source="fixture",
            description=description,
            raw_metadata=metadata or {},
        )

    def assert_classification(self, expected, **kwargs):
        result = classify_education_eligibility(self.job(**kwargs))
        self.assertEqual(result["education_eligibility"], expected)
        return result

    def test_undergraduate_routes_are_allowed(self):
        cases = (
            "Bachelor's students are eligible to apply.",
            "Bachelor's, Master's, or PhD candidates are accepted.",
            "Undergraduate or graduate students are accepted.",
            "Undergraduate and graduate students may apply.",
            "Bachelor's required, Master's preferred.",
            "Graduate from a bachelor's degree program between December 2027 and June 2028.",
            "We are seeking analytical minds from undergraduate and graduate programs.",
        )
        for description in cases:
            with self.subTest(description=description):
                self.assert_classification(UNDERGRADUATE_ALLOWED, description=description)

    def test_conclusive_graduate_only_requirements_are_excluded(self):
        cases = (
            "Master's students only.",
            "PhD or doctoral students only.",
            "Graduate students only.",
            "Master's or PhD required.",
            "You must be currently enrolled in a Master's program.",
            "Currently enrolled in a PhD program.",
            "We're looking for current PhD students with a passion for robotics research.",
            "Minimum education is a graduate degree.",
            "Applicants must hold a Master of Science degree.",
            "Required: an MBA or JD.",
            "Expected graduation from a graduate program in 2027.",
            "This role is unavailable to undergraduate students.",
            "This is our selective MBA leadership development program.",
        )
        for description in cases:
            with self.subTest(description=description):
                result = self.assert_classification(GRADUATE_ONLY, description=description)
                self.assertEqual(result["education_evidence"], description)
                self.assertEqual(result["exclusion_reason"], "graduate_only")

    def test_mba_jd_md_postdoc_residency_and_fellowship_titles_are_excluded(self):
        titles = (
            "MBA Intern",
            "JD Legal Intern",
            "MD Clinical Intern",
            "Postdoctoral Research Intern",
            "Medical Residency Student",
            "Graduate Fellowship Intern",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assert_classification(GRADUATE_ONLY, title=title)
                self.assertEqual(student_job_exclusion_reason(self.job(title=title)), "graduate_only")

    def test_explicit_phd_only_student_and_research_titles_are_excluded(self):
        titles = (
            "Student Researcher (AI Foundation Models) - 2026 Start (PhD)",
            "PhD Intern",
            "PhD Internship",
            "PhD Student Researcher",
            "PhD Research Intern",
            "PhD Candidate Intern",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assert_classification(GRADUATE_ONLY, title=title)
                self.assertEqual(student_job_exclusion_reason(self.job(title=title)), "graduate_only")

    def test_phd_title_mentions_without_phd_only_candidate_targeting_are_retained(self):
        cases = (
            "Student Researcher, AI Infrastructure",
            "Student Researcher (Bachelor's, Master's, or PhD)",
            "Software Intern building tools for PhD researchers",
        )
        for title in cases:
            with self.subTest(title=title):
                result = classify_education_eligibility(self.job(title=title))
                self.assertNotEqual(result["education_eligibility"], GRADUATE_ONLY)
                self.assertIsNone(student_job_exclusion_reason(self.job(title=title)))

    def test_company_acronym_is_not_mistaken_for_juris_doctor(self):
        title = "JD Young Supply Chain Assistant Internship"
        self.assert_classification(NOT_STATED, title=title)
        self.assertIsNone(student_job_exclusion_reason(self.job(title=title)))

    def test_preferred_degrees_and_negated_requirements_are_retained(self):
        preferred = (
            "Master's preferred.",
            "Master's degree is a plus.",
        )
        for description in preferred:
            with self.subTest(description=description):
                self.assert_classification(AMBIGUOUS, description=description)
                self.assertIsNone(student_job_exclusion_reason(self.job(description=description)))
        self.assert_classification(AMBIGUOUS, title="Master's Preferred Intern")
        self.assertIsNone(student_job_exclusion_reason(self.job(title="Master's Preferred Intern")))
        optional = "A graduate degree is preferred but not required."
        self.assert_classification(UNDERGRADUATE_ALLOWED, description=optional)
        self.assertIsNone(student_job_exclusion_reason(self.job(description=optional)))
        negated = "Not required to be enrolled in graduate school."
        self.assert_classification(UNDERGRADUATE_ALLOWED, description=negated)
        self.assertIsNone(student_job_exclusion_reason(self.job(description=negated)))

    def test_unrestricted_and_unrelated_degree_text_is_not_stated(self):
        cases = (
            "Candidates should be pursuing a degree.",
            "Our founder holds a graduate degree.",
            "You will build tools used by PhD graduates in our research division.",
            "Maintain the master data catalog for graduate recruiting analytics.",
        )
        for description in cases:
            with self.subTest(description=description):
                self.assert_classification(NOT_STATED, description=description)

    def test_ambiguous_eligibility_is_retained(self):
        cases = (
            "Master's students are encouraged to learn more.",
            "Candidates in a doctoral program may apply.",
            "Qualifications include graduate-level coursework.",
        )
        for description in cases:
            with self.subTest(description=description):
                self.assert_classification(AMBIGUOUS, description=description)
                self.assertIsNone(student_job_exclusion_reason(self.job(description=description)))

    def test_structured_education_and_qualification_fields_are_classified(self):
        result = self.assert_classification(
            GRADUATE_ONLY,
            metadata={"education": {"minimumEducation": "Master's degree"}},
        )
        self.assertEqual(result["education_evidence"], "Master's degree")
        self.assert_classification(
            UNDERGRADUATE_ALLOWED,
            metadata={"qualifications": ["Bachelor's students are accepted"]},
        )
        self.assert_classification(
            GRADUATE_ONLY,
            metadata={"requiredQualifications": ["PhD degree required"]},
        )

    def test_sentence_and_clause_scope_avoids_global_substring_rejection(self):
        description = (
            "Bachelor's students are eligible to apply. "
            "A Master's degree is preferred for a separate research assignment."
        )
        result = self.assert_classification(UNDERGRADUATE_ALLOWED, description=description)
        self.assertEqual(result["education_evidence"], "Bachelor's students are eligible to apply.")
        self.assertIsNone(student_job_exclusion_reason(self.job(description=description)))

    def test_filter_annotation_precedes_ranking_and_persistence_payload(self):
        retained = self.job(description="Undergraduate and graduate students may apply.")
        self.assertIsNone(student_job_exclusion_reason(retained))
        self.assertEqual(retained.raw_metadata["education_eligibility"], UNDERGRADUATE_ALLOWED)
        ranked = rank_student_jobs([retained])[0]
        self.assertEqual(ranked["education_eligibility"], UNDERGRADUATE_ALLOWED)
        self.assertEqual(
            ranked["raw_metadata"]["education_evidence"],
            "Undergraduate and graduate students may apply.",
        )

    def test_diagnostics_report_removed_ambiguous_and_false_positive_samples(self):
        jobs = [
            self.job(description="Master's students only.", job_id="graduate"),
            self.job(description="Master's preferred.", job_id="ambiguous"),
            self.job(description="Bachelor's, Master's, or PhD candidates are accepted.", job_id="allowed"),
        ]
        diagnostics = build_student_search_diagnostics(jobs)
        self.assertEqual(diagnostics["before_retained_jobs"], 3)
        self.assertEqual(diagnostics["after_retained_jobs"], 2)
        self.assertEqual(len(diagnostics["graduate_only_roles_removed"]), 1)
        self.assertEqual(len(diagnostics["ambiguous_roles_retained"]), 1)
        self.assertEqual(len(diagnostics["false_positive_review_samples"]), 2)
        self.assertEqual(
            diagnostics["graduate_only_roles_removed"][0]["education_evidence"],
            "Master's students only.",
        )


if __name__ == "__main__":
    unittest.main()
