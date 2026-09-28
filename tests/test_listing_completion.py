from __future__ import annotations

import unittest

from lxml import html

from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    MAX_LISTING_REQUESTS_PER_SOURCE,
    MAX_RAW_JOBS_PER_SOURCE,
    PageSizeObservation,
    PaginationEvidence,
    enforce_pagination_fail_closed,
    finish_authoritative_total,
    html_pagination_evidence,
    json_pagination_evidence,
    pagination_terminal_accepted,
    publish_pagination_evidence,
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
        self.assertEqual(policy.request_ceiling(20_001, 500, **kwargs), 1)
        self.assertEqual(policy.job_ceiling(20_001, 500, **kwargs), 20)

    def test_projected_requests_above_absolute_ceiling_keep_both_normal_caps(self):
        policy = AdaptiveListingCompletion(
            90, 90, adaptive_request_allowance=20
        )
        self.assertEqual(policy.request_ceiling(1_001, 1), 90)
        self.assertEqual(policy.job_ceiling(1_001, 1), 90)

    def test_listing_only_second_stage_reaches_absolute_request_ceiling(self):
        policy = AdaptiveListingCompletion(
            5,
            100,
            hard_request_ceiling=1_000,
            hard_job_ceiling=20_000,
            allow_listing_only_second_stage=True,
        )
        self.assertEqual(policy.request_ceiling(1_000, 20), 50)
        self.assertEqual(policy.job_ceiling(1_000, 20), 1_000)

    def test_shared_listing_only_request_ceiling_boundaries(self):
        policy = AdaptiveListingCompletion(
            1,
            1,
            allow_listing_only_second_stage=True,
        )
        self.assertEqual(MAX_LISTING_REQUESTS_PER_SOURCE, 1_000)
        for requests in (999, 1_000):
            with self.subTest(requests=requests):
                self.assertEqual(policy.request_ceiling(requests, 1), requests)
        self.assertEqual(policy.request_ceiling(1_001, 1), 1)
        metadata = {"scan_complete": False}
        policy.record_decision(metadata, 1_001, 1)
        self.assertFalse(metadata["listing_only_second_stage_eligible"])
        self.assertFalse(metadata["scan_complete"])

    def test_shared_listing_only_job_ceiling_boundaries(self):
        policy = AdaptiveListingCompletion(
            1,
            100,
            allow_listing_only_second_stage=True,
        )
        self.assertEqual(MAX_RAW_JOBS_PER_SOURCE, 20_000)
        self.assertEqual(policy.job_ceiling(19_999, 100), 19_999)
        self.assertEqual(policy.job_ceiling(20_000, 100), 20_000)
        self.assertEqual(policy.job_ceiling(20_001, 100), 100)

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

    def test_request_1000_without_reaching_total_remains_incomplete(self):
        policy = AdaptiveListingCompletion(
            1,
            1,
            allow_listing_only_second_stage=True,
        )
        metadata = {"scan_complete": False}
        policy.record_decision(
            metadata,
            1_000,
            1,
            requests_completed=1_000,
            results_observed=999,
        )
        self.assertFalse(metadata["listing_only_second_stage_eligible"])
        self.assertFalse(finish_authoritative_total(metadata, 1_000, 999))
        self.assertFalse(metadata["scan_complete"])

    def test_authoritative_total_reached_on_request_1000_is_complete(self):
        metadata = {"pages_completed": 1_000, "safety_limit_reached": True}
        self.assertTrue(finish_authoritative_total(metadata, 1_000, 1_000))
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "reported_total_reached")

    def test_authoritative_total_at_20000_jobs_is_complete(self):
        metadata = {"safety_limit_reached": True}
        self.assertTrue(finish_authoritative_total(metadata, 20_000, 20_000))
        self.assertTrue(metadata["scan_complete"])

    def test_total_above_20000_jobs_remains_incomplete(self):
        policy = AdaptiveListingCompletion(
            1, 100, allow_listing_only_second_stage=True
        )
        metadata = {"scan_complete": False}
        policy.record_decision(metadata, 20_001, 100)
        self.assertFalse(metadata["listing_only_second_stage_eligible"])
        self.assertEqual(metadata["effective_listing_job_ceiling"], 100)
        self.assertFalse(finish_authoritative_total(metadata, 20_001, 20_000))
        self.assertFalse(metadata["scan_complete"])

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
        self.assertEqual(policy.request_ceiling(20_001, 100), 1)
        self.assertEqual(policy.request_ceiling(10_001, 10), 1)

    def test_explicit_terminal_scan_gets_only_first_stage_allowance(self):
        policy = AdaptiveListingCompletion(2, 20)
        self.assertEqual(policy.deterministic_terminal_request_ceiling(), 1_000)

    def test_disabled_next_is_authoritative_html_terminal(self):
        evidence = html_pagination_evidence(
            html.fromstring(
                "<nav class='pagination'><a class='next disabled' "
                "aria-disabled='true' href='/page/2'>Next</a></nav>"
            )
        )
        self.assertFalse(evidence.has_more)
        self.assertTrue(evidence.disabled_next_control)
        self.assertTrue(evidence.terminal())

    def test_explicit_next_link_is_not_html_terminal(self):
        evidence = html_pagination_evidence(
            html.fromstring(
                "<nav class='pagination'><a rel='next' href='/page/2'>Next</a></nav>"
            )
        )
        self.assertTrue(evidence.has_more)
        self.assertFalse(evidence.terminal())

    def test_html_result_range_reaches_authoritative_total(self):
        evidence = html_pagination_evidence(
            html.fromstring(
                "<div class='results-summary'>Showing 21-25 of 25 jobs</div>"
            )
        )
        self.assertEqual(evidence.reported_total, 25)
        self.assertEqual(evidence.end_index, 25)
        self.assertTrue(evidence.terminal())

    def test_api_has_more_contract(self):
        self.assertTrue(
            json_pagination_evidence({"has_more": False}).terminal()
        )
        self.assertFalse(
            json_pagination_evidence({"has_more": True}).terminal()
        )

    def test_api_cursor_absence_requires_prior_pagination(self):
        evidence = json_pagination_evidence(
            {"pageInfo": {"nextCursor": None}}
        )
        self.assertFalse(evidence.terminal(prior_pagination=False))
        self.assertTrue(evidence.terminal(prior_pagination=True))

    def test_api_total_and_page_count_terminal_evidence(self):
        total = json_pagination_evidence(
            {"pagination": {"total": 20, "endIndex": 20}}
        )
        self.assertTrue(total.terminal())
        not_reached = json_pagination_evidence(
            {"pagination": {"total": 21, "endIndex": 20}}
        )
        self.assertFalse(not_reached.terminal())
        final_page = json_pagination_evidence(
            {"pagination": {"currentPage": 3, "totalPages": 3}}
        )
        self.assertTrue(final_page.terminal())

    def test_continuation_dominates_contradictory_terminal_hints(self):
        cases = (
            PaginationEvidence(has_more=True, disabled_next_control=True),
            json_pagination_evidence(
                {"pagination": {"has_more": True, "currentPage": 3, "totalPages": 3}}
            ),
            json_pagination_evidence(
                {"pagination": {"next": "/page/4", "currentPage": 3, "totalPages": 3}}
            ),
            json_pagination_evidence(
                {"pagination": {"has_more": False, "currentPage": 2, "totalPages": 3}}
            ),
            json_pagination_evidence(
                {"pagination": {"has_more": False, "nextCursor": "continue"}}
            ),
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(evidence.status(), "conflicting")
                self.assertFalse(evidence.terminal())

    def test_compatible_terminal_evidence_is_accepted(self):
        cases = (
            json_pagination_evidence({"has_more": False}),
            PaginationEvidence(disabled_next_control=True),
            json_pagination_evidence(
                {"pagination": {"currentPage": 3, "totalPages": 3}}
            ),
            json_pagination_evidence(
                {"pagination": {"total": 25, "endIndex": 25}}
            ),
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(evidence.status(), "terminal")
                self.assertTrue(evidence.terminal())
        cursor = json_pagination_evidence({"pageInfo": {"nextCursor": None}})
        self.assertEqual(cursor.status(prior_pagination=True), "terminal")

    def test_json_extraction_is_source_scoped(self):
        payload = {
            "facets": {"count": 99},
            "aggregations": {"hits": 88},
            "analytics": {"total": 77},
            "results": [{"total": 66}],
        }
        evidence = json_pagination_evidence(payload)
        self.assertIsNone(evidence.reported_total)
        self.assertEqual(evidence.fields, ())

        declared = json_pagination_evidence(
            {"result": {"total": 12}}, container_paths=("result",)
        )
        self.assertEqual(declared.reported_total, 12)

    def test_conflicting_declared_totals_fail_closed(self):
        evidence = json_pagination_evidence(
            {"pagination": {"total": 10, "totalCount": 11}}
        )
        self.assertIsNone(evidence.reported_total)
        self.assertEqual(evidence.status(), "conflicting")

    def test_malformed_pagination_values_fail_closed(self):
        cases = (
            json_pagination_evidence({"has_more": "maybe"}),
            json_pagination_evidence({"total": "many"}),
            json_pagination_evidence({"pagination": {"totalPages": -1}}),
            json_pagination_evidence({"pageInfo": {"nextCursor": {"id": 1}}}),
            html_pagination_evidence(
                html.fromstring(
                    "<div class='results-summary'>Showing one-two of many jobs</div>"
                )
            ),
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(evidence.status(), "malformed")
                self.assertFalse(evidence.terminal())

    def test_json_pagination_diagnostics_are_field_scoped_and_sanitized(self):
        evidence = json_pagination_evidence(
            {
                "result": {
                    "pagination": {
                        "currentPage": "1",
                        "totalPages": 65,
                        "nextCursor": {"token": "must-not-be-dumped"},
                    }
                }
            },
            container_paths=("result", "result.pagination"),
        )

        self.assertEqual(evidence.status(), "malformed")
        self.assertEqual(
            evidence.field_diagnostics,
            (
                ("result.pagination.nextCursor", "dict", "<dict length=1>"),
                ("result.pagination.totalPages", "int", 65),
                ("result.pagination.currentPage", "str", "1"),
            ),
        )
        metadata = {}
        publish_pagination_evidence(metadata, evidence)
        self.assertEqual(metadata["page_count"], 65)
        self.assertIn("result.pagination.currentPage", metadata["pagination_malformed_fields"])
        self.assertNotIn("must-not-be-dumped", repr(metadata))

    def test_numeric_page_index_contract_accepts_successor_and_terminal_null(self):
        continuing = json_pagination_evidence(
            {"result": {"pagination": {"currentPage": 42, "nextPage": 43, "lastPage": 63}}},
            container_paths=("result.pagination",),
            numeric_page_index_contract=True,
            expected_current_page=42,
            expected_page_count=63,
        )
        self.assertEqual(continuing.status(), "continuation")
        self.assertEqual(continuing.next_page, 43)

        terminal = json_pagination_evidence(
            {"result": {"pagination": {"currentPage": 63, "nextPage": None, "lastPage": 63}}},
            container_paths=("result.pagination",),
            numeric_page_index_contract=True,
            expected_current_page=63,
            expected_page_count=63,
        )
        self.assertEqual(terminal.status(), "terminal")
        self.assertTrue(terminal.explicit_final_page)

    def test_numeric_page_index_contract_rejects_gaps_repeats_and_missing_fields(self):
        cases = (
            {"currentPage": 10, "nextPage": 15, "lastPage": 20},
            {"currentPage": 10, "nextPage": 10, "lastPage": 20},
            {"currentPage": 10, "nextPage": 9, "lastPage": 20},
            {"currentPage": 10, "lastPage": 20},
        )
        for pagination in cases:
            with self.subTest(pagination=pagination):
                evidence = json_pagination_evidence(
                    {"pagination": pagination},
                    container_paths=("pagination",),
                    numeric_page_index_contract=True,
                    expected_current_page=10,
                    expected_page_count=20,
                )
                self.assertEqual(evidence.status(), "malformed")
                self.assertFalse(evidence.terminal())

    def test_numeric_page_index_contract_rejects_changed_final_page(self):
        evidence = json_pagination_evidence(
            {"pagination": {"currentPage": 2, "nextPage": 3, "lastPage": 64}},
            container_paths=("pagination",),
            numeric_page_index_contract=True,
            expected_current_page=2,
            expected_page_count=63,
        )
        self.assertEqual(evidence.status(), "conflicting")
        self.assertIn("page_count_changed", evidence.conflicting_fields)

    def test_conflict_and_malformed_status_are_durable_metadata(self):
        conflict = PaginationEvidence(has_more=True, disabled_next_control=True)
        metadata = {"scan_complete": True}
        publish_pagination_evidence(metadata, conflict)
        self.assertFalse(pagination_terminal_accepted(metadata, conflict))
        self.assertTrue(enforce_pagination_fail_closed(metadata))
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "conflicting_pagination_evidence",
        )
        self.assertTrue(metadata["pagination_evidence_diagnostics"])

        malformed = json_pagination_evidence({"has_more": "maybe"})
        metadata = {"scan_complete": True}
        publish_pagination_evidence(metadata, malformed)
        enforce_pagination_fail_closed(metadata)
        self.assertEqual(
            metadata["scan_completeness_reason"],
            "malformed_pagination_evidence",
        )

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
        self.assertEqual(policy.request_ceiling(15_000, 20), 750)

        metadata = {}
        policy.record_decision(
            metadata,
            15_000,
            15,
            requests_completed=1,
            results_observed=15,
        )

        self.assertTrue(metadata["listing_only_second_stage_eligible"])
        self.assertEqual(metadata["effective_listing_request_ceiling"], 1_000)
        self.assertEqual(metadata["terminal_listing_requests_required"], 1_000)


if __name__ == "__main__":
    unittest.main()
