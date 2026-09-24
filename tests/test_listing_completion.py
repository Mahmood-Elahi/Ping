from __future__ import annotations

import unittest

from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    PageSizeObservation,
    finish_authoritative_total,
    record_terminal_evidence,
)


class AdaptiveListingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.policy = AdaptiveListingCompletion(
            normal_request_budget=5,
            normal_job_budget=100,
            adaptive_request_allowance=10,
            hard_request_ceiling=20,
            hard_job_ceiling=500,
        )

    def test_one_page_beyond_normal_cap_is_authorized(self):
        self.assertEqual(self.policy.request_ceiling(120, 20), 6)
        self.assertEqual(self.policy.job_ceiling(120, 20), 120)

    def test_several_pages_beyond_normal_cap_are_authorized(self):
        self.assertEqual(self.policy.request_ceiling(260, 20), 13)

    def test_source_beyond_adaptive_allowance_stays_at_normal_budget(self):
        self.assertEqual(self.policy.request_ceiling(320, 20), 5)
        self.assertEqual(self.policy.job_ceiling(320, 20), 100)

    def test_absolute_request_and_job_ceilings_are_enforced(self):
        request_limited = AdaptiveListingCompletion(18, 100, 10, 20, 500)
        self.assertEqual(request_limited.request_ceiling(420, 20), 18)
        self.assertEqual(self.policy.request_ceiling(501, 50), 5)

    def test_authoritative_total_is_required(self):
        self.assertEqual(self.policy.request_ceiling(None, 20), 5)
        self.assertEqual(self.policy.job_ceiling(None, 20), 100)

    def test_complete_collection_needs_no_additional_listing_request(self):
        self.assertEqual(self.policy.complete_collection_job_ceiling(180), 180)

    def test_complete_collection_in_memory_ignores_legacy_local_result_cap(self):
        policy = AdaptiveListingCompletion(1, 20)
        self.assertEqual(policy.complete_collection_job_ceiling(22), 22)

    def test_authoritative_total_at_exact_cap_wins_over_safety_limit(self):
        metadata = {
            "scan_complete": False,
            "scan_completeness_reason": "safety_limit_reached",
            "stop_reason": "maximum_results_reached",
            "safety_limit_reached": True,
        }
        self.assertTrue(finish_authoritative_total(metadata, 20, 20))
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "reported_total_reached")

    def test_explicit_has_more_false_is_terminal_evidence(self):
        metadata = {}
        record_terminal_evidence(
            metadata,
            "explicit_has_more",
            False,
            has_more=False,
            terminal_page_observed=True,
        )
        self.assertFalse(metadata["has_more"])
        self.assertTrue(metadata["terminal_page_observed"])

    def test_next_cursor_absent_after_advancing_is_terminal_evidence(self):
        metadata = {"pages_completed": 2}
        record_terminal_evidence(
            metadata,
            "next_cursor",
            None,
            next_cursor_present=False,
            terminal_page_observed=True,
        )
        self.assertFalse(metadata["next_cursor_present"])
        self.assertTrue(metadata["terminal_page_observed"])

    def test_one_page_budget_extends_for_exactly_one_remaining_page(self):
        policy = AdaptiveListingCompletion(1, 20)
        metadata = {}
        policy.record_decision(
            metadata,
            26,
            20,
            requests_completed=1,
            results_observed=20,
        )
        self.assertTrue(metadata["adaptive_completion_eligible"])
        self.assertEqual(metadata["effective_listing_request_ceiling"], 2)
        self.assertEqual(metadata["effective_listing_job_ceiling"], 26)

    def test_page_budget_does_not_leave_old_result_cap_in_place(self):
        policy = AdaptiveListingCompletion(2, 10)
        self.assertEqual(
            policy.request_ceiling(
                15, 10, requests_completed=1, results_observed=10
            ),
            2,
        )
        self.assertEqual(
            policy.job_ceiling(
                15, 10, requests_completed=1, results_observed=10
            ),
            15,
        )

    def test_result_budget_does_not_leave_old_page_cap_in_place(self):
        policy = AdaptiveListingCompletion(1, 100)
        self.assertEqual(
            policy.request_ceiling(
                120, 100, requests_completed=1, results_observed=100
            ),
            2,
        )
        self.assertEqual(
            policy.job_ceiling(
                120, 100, requests_completed=1, results_observed=100
            ),
            120,
        )

    def test_remaining_work_beyond_allowance_keeps_both_normal_caps(self):
        policy = AdaptiveListingCompletion(1, 20)
        kwargs = {"requests_completed": 1, "results_observed": 20}
        self.assertEqual(policy.request_ceiling(221, 20, **kwargs), 1)
        self.assertEqual(policy.job_ceiling(221, 20, **kwargs), 20)

    def test_projected_jobs_above_absolute_ceiling_keep_both_normal_caps(self):
        policy = AdaptiveListingCompletion(1, 20)
        kwargs = {"requests_completed": 1, "results_observed": 20}
        self.assertEqual(policy.request_ceiling(5_001, 500, **kwargs), 1)
        self.assertEqual(policy.job_ceiling(5_001, 500, **kwargs), 20)

    def test_projected_requests_above_absolute_ceiling_keep_both_normal_caps(self):
        policy = AdaptiveListingCompletion(
            90, 90, adaptive_request_allowance=20
        )
        self.assertEqual(policy.request_ceiling(101, 1), 90)
        self.assertEqual(policy.job_ceiling(101, 1), 90)

    def test_listing_only_second_stage_reaches_absolute_request_ceiling(self):
        policy = AdaptiveListingCompletion(
            5,
            100,
            hard_request_ceiling=100,
            hard_job_ceiling=5_000,
            allow_listing_only_second_stage=True,
        )
        self.assertEqual(policy.request_ceiling(1_000, 20), 50)
        self.assertEqual(policy.job_ceiling(1_000, 20), 1_000)

    def test_listing_only_total_request_tiers_through_100_are_allowed(self):
        policy = AdaptiveListingCompletion(
            1,
            1,
            hard_request_ceiling=100,
            hard_job_ceiling=5_000,
            allow_listing_only_second_stage=True,
        )
        for requests in (25, 50, 75, 100):
            with self.subTest(requests=requests):
                self.assertEqual(policy.request_ceiling(requests, 1), requests)
        self.assertEqual(policy.request_ceiling(101, 1), 1)

    def test_listing_only_job_ceiling_is_inclusive_at_5000(self):
        policy = AdaptiveListingCompletion(
            1,
            100,
            allow_listing_only_second_stage=True,
        )
        self.assertEqual(policy.job_ceiling(4_999, 100), 4_999)
        self.assertEqual(policy.job_ceiling(5_000, 100), 5_000)
        self.assertEqual(policy.job_ceiling(5_001, 100), 100)

    def test_detail_coupled_source_cannot_use_listing_only_expansion(self):
        policy = AdaptiveListingCompletion(
            1,
            20,
            allow_listing_only_second_stage=True,
        )
        self.assertEqual(policy.request_ceiling(1_000, 20), 50)
        self.assertEqual(
            policy.request_ceiling(1_000, 20, detail_requests_coupled=True),
            1,
        )

    def test_request_100_without_reaching_total_remains_incomplete(self):
        policy = AdaptiveListingCompletion(
            1,
            1,
            allow_listing_only_second_stage=True,
        )
        metadata = {}
        policy.record_decision(
            metadata,
            100,
            1,
            requests_completed=100,
            results_observed=99,
        )
        self.assertFalse(metadata["listing_only_second_stage_eligible"])
        self.assertFalse(finish_authoritative_total(metadata, 100, 99))

    def test_authoritative_total_reached_on_request_100_is_complete(self):
        metadata = {"pages_completed": 100, "safety_limit_reached": True}
        self.assertTrue(finish_authoritative_total(metadata, 100, 100))
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "reported_total_reached")

    def test_second_stage_rejects_mutation_and_detail_coupling(self):
        policy = AdaptiveListingCompletion(
            1, 20, allow_listing_only_second_stage=True
        )
        kwargs = {"requests_completed": 1, "results_observed": 20}
        self.assertEqual(
            policy.request_ceiling(
                1_000, 20, collection_changed=True, **kwargs
            ),
            1,
        )
        self.assertEqual(
            policy.request_ceiling(
                1_000, 20, detail_requests_coupled=True, **kwargs
            ),
            1,
        )

    def test_second_stage_preserves_job_and_request_hard_ceilings(self):
        policy = AdaptiveListingCompletion(
            1, 20, allow_listing_only_second_stage=True
        )
        self.assertEqual(policy.request_ceiling(5_001, 100), 1)
        self.assertEqual(policy.request_ceiling(1_001, 10), 1)

    def test_explicit_terminal_scan_gets_only_first_stage_allowance(self):
        policy = AdaptiveListingCompletion(2, 20)
        self.assertEqual(policy.deterministic_terminal_request_ceiling(), 12)

    def test_observed_page_size_tracks_clamps_expansion_and_variation(self):
        downward = PageSizeObservation(20)
        self.assertEqual(downward.observe(15), 15)
        self.assertEqual(downward.observe(15), 15)
        self.assertTrue(downward.was_clamped)
        self.assertTrue(downward.stable_across_pages)

        upward = PageSizeObservation(20)
        self.assertEqual(upward.observe(25), 25)
        self.assertTrue(upward.was_expanded)

        varying = PageSizeObservation(20)
        varying.observe(15)
        self.assertEqual(varying.observe(12), 12)
        self.assertFalse(varying.stable_across_pages)

    def test_effective_page_size_recalculates_second_stage_ceiling(self):
        policy = AdaptiveListingCompletion(
            5, 100, allow_listing_only_second_stage=True
        )
        self.assertEqual(policy.request_ceiling(1_600, 20), 80)

        metadata = {}
        policy.record_decision(
            metadata,
            1_600,
            15,
            requests_completed=1,
            results_observed=15,
        )

        self.assertFalse(metadata["listing_only_second_stage_eligible"])
        self.assertEqual(metadata["effective_listing_request_ceiling"], 5)
        self.assertEqual(metadata["terminal_listing_requests_required"], 107)


if __name__ == "__main__":
    unittest.main()
