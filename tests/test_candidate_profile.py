from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.scraper.company_careers.candidate_profile import (
    DEFAULT_CANDIDATE_PROFILE_PATH,
    candidate_profile_exclusion_reason,
    load_candidate_profile,
    score_candidate_preferences,
)
from src.scraper.company_careers.models import JobRecord
from src.scraper.company_careers.student_roles import (
    build_student_search_diagnostics,
    build_student_location_filter,
    rank_student_jobs,
    score_student_job,
    student_job_exclusion_reason,
    write_student_ranked_jobs_artifacts,
)


PROFILE = load_candidate_profile()


def job(
    job_id: str,
    title: str = "Embedded Systems Intern",
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


def preference(record: JobRecord) -> dict:
    relevance = score_student_job(record, candidate_profile=PROFILE)
    return relevance


class CandidateProfileConfigurationTests(unittest.TestCase):
    def test_default_profile_loads_expected_general_configuration(self):
        self.assertEqual(PROFILE.target_term.year, 2027)
        self.assertEqual(PROFILE.target_term.preferred_start_month, 5)
        self.assertTrue(PROFILE.allows_international)
        self.assertEqual(
            [cluster.name for cluster in PROFILE.domains],
            [
                "ai_ml", "edge_ai", "embedded", "perception",
                "robotics_autonomy", "hardware_compute", "systems",
            ],
        )

    def test_invalid_component_weights_fail_config_validation(self):
        source = Path(DEFAULT_CANDIDATE_PROFILE_PATH).read_text(encoding="utf-8")
        invalid = source.replace("domains: 0.55", "domains: 0.65")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.yaml"
            path.write_text(invalid, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sum to 1"):
                load_candidate_profile(str(path))


class CandidateProfileTermTests(unittest.TestCase):
    def test_may_2027_start_is_preferred(self):
        result = preference(job("may", description="The internship starts May 2027."))
        self.assertEqual(result["term_match"], "preferred")
        self.assertIn("+ Summer 2027 start", result["candidate_preference_reasons"])

    def test_summer_2027_wording_is_preferred(self):
        result = preference(job("summer", title="Embedded Systems Intern - Summer 2027"))
        self.assertEqual(result["term_match"], "preferred")

    def test_year_before_summer_is_also_preferred(self):
        result = preference(job("summer-reversed", title="AI Intern - 2027 Summer"))
        self.assertEqual(result["term_match"], "preferred")

    def test_fall_2027_receives_severe_ranking_penalty(self):
        summer = preference(job("summer", title="Firmware Intern - Summer 2027"))
        fall = preference(job("fall", title="Firmware Intern - Fall 2027"))
        self.assertEqual(fall["term_match"], "mismatch")
        self.assertGreaterEqual(summer["ranking_score"] - fall["ranking_score"], 30)

    def test_may_to_december_remains_term_aligned(self):
        result = preference(
            job("long", description="Runs from May 2027 through December 2027.")
        )
        self.assertEqual(result["term_match"], "preferred")
        self.assertEqual(result["duration_months"], 8)

    def test_missing_start_date_is_neutral_not_rejected(self):
        record = job("unknown")
        result = preference(record)
        self.assertEqual(result["term_match"], "unknown")
        self.assertIsNone(student_job_exclusion_reason(record, candidate_profile=PROFILE))
        self.assertGreater(result["candidate_preference_score"], 0)


class CandidateProfileDurationTests(unittest.TestCase):
    def _scored(self, months: int) -> dict:
        return preference(
            job(
                str(months),
                title="Embedded Systems Intern - Summer 2027",
                description=f"This is a {months}-month internship.",
            )
        )

    def test_duration_curve_preserves_requested_order(self):
        scored = {months: self._scored(months) for months in (4, 8, 12, 16)}
        self.assertGreater(scored[4]["candidate_preference_score"], scored[8]["candidate_preference_score"])
        self.assertGreater(scored[8]["candidate_preference_score"], scored[12]["candidate_preference_score"])
        self.assertGreater(scored[12]["candidate_preference_score"], scored[16]["candidate_preference_score"])

    def test_strong_long_technical_role_can_beat_weak_short_role(self):
        strong = job(
            "strong",
            title="Embedded AI Computer Vision Intern - Summer 2027",
            description=(
                "A 16-month role using C++, Embedded Linux, CUDA, TensorRT, camera pipelines, "
                "neural-network inference, sensor fusion, and robotics software."
            ),
        )
        weak = job(
            "weak",
            title="Generic Engineering Intern - Summer 2027",
            description="A 4-month internship supporting routine documentation.",
        )
        ranked = rank_student_jobs([weak, strong], candidate_profile=PROFILE)
        self.assertEqual(ranked[0]["job_id"], "strong")
        self.assertEqual(ranked[0]["duration_months"], 16)

    def test_unknown_duration_is_not_rejected_or_worse_than_sixteen_months(self):
        unknown = preference(job("unknown", title="Firmware Intern - Summer 2027"))
        long = self._scored(16)
        self.assertEqual(unknown["duration_match"], "unknown")
        self.assertGreater(unknown["candidate_preference_score"], long["candidate_preference_score"])

    def test_unlisted_duration_is_neutral_not_mislabeled_as_preferred(self):
        result = self._scored(3)
        self.assertEqual(result["duration_match"], "other")
        self.assertNotIn("+ Preferred 3-month duration", result["candidate_preference_reasons"])


class CandidateProfileGeographyAndArrangementTests(unittest.TestCase):
    def test_canada_is_preferred_but_us_and_international_remain_eligible(self):
        canada = preference(job("ca", location="Toronto, Ontario, Canada"))
        us = preference(job("us", location="San Jose, California, United States"))
        international_job = job("de", location="Munich, Germany")
        international = preference(international_job)
        location_filter = build_student_location_filter(allow_international=True)

        self.assertGreater(canada["candidate_preference_score"], us["candidate_preference_score"])
        self.assertGreater(us["candidate_preference_score"], international["candidate_preference_score"])
        self.assertIsNone(location_filter(international_job))
        self.assertTrue(international["international_work_authorization_required"])

    def test_geography_does_not_overpower_major_technical_fit(self):
        strong_us = job(
            "strong-us",
            title="Embedded AI and Computer Vision Intern",
            location="San Jose, California, USA",
            description="TensorRT inference on NVIDIA Jetson with C++, CUDA, sensors, and camera pipelines.",
        )
        weak_canada = job(
            "weak-ca",
            title="Generic Engineering Intern",
            description="Prepare documents and coordinate routine projects.",
        )
        ranked = rank_student_jobs([weak_canada, strong_us], candidate_profile=PROFILE)
        self.assertEqual(ranked[0]["job_id"], "strong-us")

    def test_work_arrangement_has_no_scoring_effect(self):
        arrangements = [
            job("onsite", location="Calgary, Alberta, Canada"),
            job("hybrid", location="Hybrid - Calgary, Alberta, Canada"),
            job("remote", location="Remote - Calgary, Alberta, Canada"),
        ]
        results = [preference(record) for record in arrangements]
        self.assertEqual(
            [result["work_arrangement"] for result in results],
            ["onsite_or_unspecified", "hybrid", "remote"],
        )
        self.assertEqual(len({result["candidate_preference_score"] for result in results}), 1)
        self.assertEqual(len({result["ranking_score"] for result in results}), 1)


class CandidateProfileDomainTests(unittest.TestCase):
    def test_representative_domain_language_is_detected(self):
        cases = {
            "ai_ml": "Machine learning and deep learning model development",
            "edge_ai": "TinyML model compression, quantization, and TensorRT inference",
            "embedded": "Firmware, RTOS, microcontrollers, and device drivers",
            "perception": "Computer vision, LiDAR perception, and sensor fusion",
            "robotics_autonomy": "Robotics autonomy, SLAM, navigation, and motion planning",
            "hardware_compute": "FPGA, CUDA GPU computing, SoC, and digital design",
            "systems": "Systems programming, Linux systems, and high-performance computing",
        }
        for cluster, description in cases.items():
            with self.subTest(cluster=cluster):
                result = preference(job(cluster, description=description))
                self.assertIn(cluster, result["matched_domain_clusters"])

    def test_legitimate_cross_domain_role_gets_bounded_bonus(self):
        record = job(
            "cross",
            title="Embedded Vision Intern",
            description=(
                "C++ and Embedded Linux on NVIDIA Jetson using CUDA and TensorRT for "
                "camera pipelines, computer vision, sensor fusion, and neural-network inference."
            ),
        )
        result = preference(record)
        self.assertGreaterEqual(len(result["matched_domain_clusters"]), 5)
        self.assertGreater(result["multi_domain_bonus"], 0)
        self.assertLessEqual(result["multi_domain_bonus"], PROFILE.multi_domain_bonus_cap)
        self.assertLessEqual(result["domain_match_score"], 100)

    def test_repeated_keyword_stuffing_does_not_increase_domain_score(self):
        once = score_candidate_preferences(job("once", description="Machine learning"), PROFILE)
        stuffed = score_candidate_preferences(
            job("stuffed", description=" ".join(["machine learning"] * 100)), PROFILE
        )
        self.assertEqual(once["domain_match_score"], stuffed["domain_match_score"])
        self.assertEqual(once["multi_domain_bonus"], stuffed["multi_domain_bonus"])

    def test_role_fit_beats_industry_name(self):
        agricultural_tech = job(
            "ag-tech",
            title="Embedded AI Perception Intern",
            company="Plain Agricultural Equipment",
            description="Firmware, TensorRT, computer vision, and edge inference on farm machinery.",
        )
        exciting_industry = job(
            "generic-av",
            title="Generic Engineering Intern",
            company="Exciting Autonomous Vehicle Company",
            description="Prepare presentations and coordinate documentation.",
        )
        ranked = rank_student_jobs([exciting_industry, agricultural_tech], candidate_profile=PROFILE)
        self.assertEqual(ranked[0]["job_id"], "ag-tech")


class CandidateProfileDefenseAndCompatibilityTests(unittest.TestCase):
    def test_direct_defense_contractor_and_defense_work_are_excluded(self):
        contractor = job("contractor", company="Lockheed Martin")
        defense_unit = job(
            "unit",
            company="Mixed Business Company",
            description="Build targeting software for missile systems in a defense program.",
        )
        for record in (contractor, defense_unit):
            with self.subTest(record=record.job_id):
                self.assertEqual(candidate_profile_exclusion_reason(record, PROFILE), "defense")
                self.assertEqual(student_job_exclusion_reason(record, candidate_profile=PROFILE), "defense")
        self.assertEqual(rank_student_jobs([contractor, defense_unit], candidate_profile=PROFILE), [])

    def test_profile_diagnostics_count_defense_and_retain_international(self):
        contractor = job("contractor", company="Lockheed Martin")
        international = job("international", location="Munich, Germany")
        diagnostics = build_student_search_diagnostics(
            [contractor, international], candidate_profile=PROFILE
        )
        self.assertEqual(diagnostics["exclusions_by_reason"], {"defense": 1})
        self.assertEqual(diagnostics["after_retained_jobs"], 1)

    def test_generic_security_and_veteran_language_do_not_trigger_defense(self):
        ordinary = job(
            "ordinary",
            title="Cybersecurity Intern",
            description=(
                "Apply defense in depth to commercial systems. We do not discriminate "
                "based on protected veteran status."
            ),
        )
        self.assertIsNone(candidate_profile_exclusion_reason(ordinary, PROFILE))
        self.assertIsNone(student_job_exclusion_reason(ordinary, candidate_profile=PROFILE))

    def test_single_company_overview_defense_reference_is_not_enough(self):
        commercial = job(
            "commercial",
            company="Commercial Robotics Company",
            description=(
                "We build commercial autonomous trucks and have also delivered one "
                "prototype to the U.S. Department of Defense. In this role, you will "
                "develop perception for commercial highway operations."
            ),
        )
        self.assertIsNone(candidate_profile_exclusion_reason(commercial, PROFILE))

    def test_profile_disabled_preserves_legacy_score_and_schema(self):
        record = job("legacy", title="Firmware Intern - Summer 2027")
        legacy = score_student_job(record)
        ranked = rank_student_jobs([record])[0]
        self.assertEqual(ranked["relevance_score"], legacy["score"])
        self.assertNotIn("candidate_preference_score", ranked)
        self.assertNotIn("ranking_score", ranked)

    def test_same_input_and_profile_are_deterministic(self):
        record = job(
            "deterministic",
            title="Embedded AI Intern - Summer 2027",
            description="A 4-month computer vision and TensorRT role.",
        )
        first = rank_student_jobs([record], candidate_profile=PROFILE)
        second = rank_student_jobs([record], candidate_profile=PROFILE)
        self.assertEqual(first, second)

    def test_ranked_artifacts_expose_explainable_profile_fields(self):
        record = job(
            "artifact",
            title="Embedded AI Intern - Summer 2027",
            description="A 4-month TensorRT and computer vision internship.",
        )
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "ranked.json"
            csv_path = Path(directory) / "ranked.csv"
            write_student_ranked_jobs_artifacts(
                [record],
                json_path=str(json_path),
                csv_path=str(csv_path),
                candidate_profile=PROFILE,
            )
            item = json.loads(json_path.read_text(encoding="utf-8"))[0]
            csv_text = csv_path.read_text(encoding="utf-8")

        for field in (
            "role_relevance_score", "candidate_preference_score", "ranking_score",
            "term_match", "duration_match", "geography_match", "matched_domain_clusters",
            "domain_match_score", "multi_domain_bonus", "candidate_preference_reasons",
        ):
            self.assertIn(field, item)
            self.assertIn(field, csv_text.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
