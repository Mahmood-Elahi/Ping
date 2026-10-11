from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.validate_source_integrity import validate_source_integrity
from scripts.validate_source_integrity_canaries import (
    CanaryBudgetReached,
    DEFAULT_CANARIES,
    run_canary,
    selected_targets,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.source_health import (
    SourceHealthCode,
    SourceScanState,
    assess_source_response,
    finalize_source_scan,
)
from src.scraper.company_careers.source_integrity import (
    IntegrityAction,
    SourceActivationState,
    assess_registry,
    assess_registry_entry,
    load_registry_entries,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "target_companies.yaml"


class CanonicalSourceHealthTests(unittest.TestCase):
    def test_process_success_and_coverage_health_are_separate(self):
        complete = CompanyScanResult(
            target=CompanyTarget(name="Complete", platform="greenhouse"),
            scan_complete=True,
            scan_state="complete",
            source_health_code="healthy",
        )
        incomplete = CompanyScanResult(
            target=CompanyTarget(name="Incomplete", platform="phenom"),
            scan_complete=False,
            scan_state="incomplete",
            source_health_code="safety_limit_reached",
        )
        failed = CompanyScanResult(
            target=CompanyTarget(name="Failed", platform="workday"),
            status=CoverageStatus.FAILED_TECHNICAL,
            scan_state="failed",
            source_health_code="source_stale",
        )
        summary = CompanyCareersReport([complete, incomplete, failed]).source_health_summary()
        self.assertEqual(summary["process_status"], "succeeded")
        self.assertEqual(summary["coverage_health"], "degraded")
        self.assertEqual(
            {key: summary[key] for key in ("complete", "incomplete", "failed")},
            {"complete": 1, "incomplete": 1, "failed": 1},
        )

    def test_authoritative_total_is_explicit_terminal_evidence_at_exact_cap(self):
        metadata = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="maximum_results_reached",
            safety_limit_reached=True,
            authoritative_total_present=True,
            total_available=71,
        )
        finalize_source_scan(metadata, jobs_collected=71)

        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_state"], SourceScanState.COMPLETE.value)
        self.assertEqual(metadata["terminal_evidence_type"], "authoritative_total")
        self.assertEqual(metadata["terminal_evidence_value"], 71)
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["source_identity_status"], "unproven")

    def test_positive_progress_cannot_retain_not_started_stop_reason(self):
        for field in ("requests_started", "pages_requested", "jobs_normalized"):
            with self.subTest(field=field):
                metadata = SourceScanMetadata(**{field: 1})
                finalize_source_scan(
                    metadata,
                    jobs_collected=1 if field == "jobs_normalized" else 0,
                    failed=True,
                    failure_code="adapter failed after progress",
                )
                self.assertNotEqual(metadata["stop_reason"], "not_started")
                self.assertEqual(metadata["stop_reason"], "adapter_error_after_progress")

    def test_atco_prefix_cannot_be_complete_before_total_or_terminal(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="reported_total_reached",
            stop_reason="maximum_results_reached",
            authoritative_total_present=True,
            total_available=71,
            safety_limit_reached=True,
            has_more=True,
        )
        finalize_source_scan(metadata, jobs_collected=20)

        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_state"], SourceScanState.INCOMPLETE.value)
        self.assertIn(
            metadata["source_health_code"],
            {
                SourceHealthCode.TERMINAL_CONTRADICTION.value,
                SourceHealthCode.AUTHORITATIVE_TOTAL_NOT_REACHED.value,
            },
        )

    def test_all_pagination_failure_classes_remain_incomplete(self):
        cases = {
            "max_jobs_with_continuation": dict(
                scan_complete=True, safety_limit_reached=True, has_more=True
            ),
            "max_pages_without_terminal": dict(
                scan_complete=True, safety_limit_reached=True
            ),
            "authoritative_total_not_reached": dict(
                scan_complete=True,
                authoritative_total_present=True,
                total_available=30,
            ),
            "repeated_page": dict(scan_complete=True, stop_reason="repeated_page"),
            "repeated_cursor": dict(scan_complete=True, stop_reason="repeated_cursor"),
            "page_number_ignored": dict(scan_complete=True, stop_reason="page_number_ignored"),
            "short_intermediate_page": dict(
                scan_complete=True,
                terminal_evidence_type="terminal_page",
                terminal_page_observed=True,
                has_more=True,
            ),
            "empty_page_with_active_next": dict(
                scan_complete=True,
                complete_collection_response=True,
                terminal_evidence_type="complete_collection_response",
                next_url_present=True,
            ),
            "malformed_final_page": dict(
                scan_complete=True, pagination_evidence_malformed=True
            ),
            "collection_changed": dict(
                scan_complete=True, collection_changed_during_scan=True
            ),
            "total_increased": dict(scan_complete=True, total_count_changed=True),
            "total_decreased": dict(scan_complete=True, total_count_changed=True),
            "rejected_record": dict(scan_complete=True, rejected_record_count=1),
        }
        for name, values in cases.items():
            with self.subTest(name=name):
                metadata = SourceScanMetadata(**values)
                finalize_source_scan(metadata, jobs_collected=20)
                self.assertFalse(metadata["scan_complete"])
                self.assertEqual(metadata["scan_state"], "incomplete")
                self.assertNotEqual(metadata["source_health_code"], "healthy")

    def test_zero_jobs_requires_a_reviewed_empty_terminal_contract(self):
        unproven = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="adapter_claimed_success",
            stop_reason="empty_page",
        )
        finalize_source_scan(unproven, jobs_collected=0)
        self.assertFalse(unproven["scan_complete"])
        self.assertEqual(unproven["source_health_code"], "unproven_empty")

        valid = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="validated_zero_jobs",
            stop_reason="validated_zero_jobs",
            authoritative_total_present=True,
            total_available=0,
        )
        finalize_source_scan(valid, jobs_collected=0)
        self.assertTrue(valid["scan_complete"])
        self.assertEqual(valid["terminal_evidence_type"], "authoritative_total")
        self.assertEqual(valid["terminal_evidence_value"], 0)

    def test_partial_activation_can_collect_but_never_prove_absence(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="complete_collection_response",
            stop_reason="complete_collection_response",
        )
        finalize_source_scan(metadata, jobs_collected=3, activation_state="partial")
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "source_not_authoritative")

    def test_canary_budget_is_incomplete_safety_limit_not_source_failure(self):
        metadata = SourceScanMetadata(
            scan_complete=False,
            scan_completeness_reason="safety_limit_reached",
            stop_reason="canary_budget_reached",
            safety_limit_reached=True,
            authoritative_total_present=True,
            total_available=100,
            has_more=True,
        )
        finalize_source_scan(metadata, jobs_collected=7)

        self.assertEqual(metadata["scan_state"], "incomplete")
        self.assertEqual(metadata["source_health_code"], "safety_limit_reached")
        self.assertNotEqual(metadata["source_health_code"], "source_unreachable")

    def test_reaching_a_raised_job_bound_is_not_terminal_evidence(self):
        metadata = SourceScanMetadata(
            scan_complete=True,
            scan_completeness_reason="maximum_results_reached",
            stop_reason="maximum_results_reached",
            safety_limit_reached=True,
        )
        finalize_source_scan(metadata, jobs_collected=300)

        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_state"], "incomplete")
        self.assertEqual(metadata["source_health_code"], "pagination_unproven")

    def test_no_nonhealthy_state_can_establish_absence_authority(self):
        disallowed = (
            "safety_limit_reached",
            "source_migration_suspected",
            "source_unreachable",
            "pagination_unproven",
            "terminal_contradiction",
            "record_validation_failure",
            "malformed_records",
            "collection_changed",
            "unproven_empty",
            "canary_budget_reached",
        )
        for code in disallowed:
            with self.subTest(code=code):
                result = CompanyScanResult(
                    target=CompanyTarget(name="Example", platform="greenhouse"),
                    scan_complete=True,
                    scan_state="complete",
                    source_health_code=code,
                    source_activation_state="active",
                )
                self.assertFalse(result.authoritative_for_absence)

    def test_historical_complete_claim_cannot_override_current_incomplete_result(self):
        historical = {"scan_state": "complete", "source_health_code": "healthy"}
        current = CompanyScanResult(
            target=CompanyTarget(name="Example", platform="greenhouse"),
            scan_complete=False,
            scan_state="incomplete",
            source_health_code="safety_limit_reached",
        )
        self.assertEqual(historical["scan_state"], "complete")
        self.assertFalse(current.authoritative_for_absence)


class SourceResponseIntegrityTests(unittest.TestCase):
    def assess(self, **overrides):
        values = {
            "configured_url": "https://jobs.example.test/ats/search",
            "final_url": "https://jobs.example.test/ats/search",
            "status_code": 200,
            "allowed_hosts": ["jobs.example.test"],
            "body_prefix": "<div data-job-collection>jobs</div>",
            "required_markers": ["data-job-collection"],
        }
        values.update(overrides)
        return assess_source_response(**values)

    def test_expired_404_and_410_are_stale(self):
        for status in (404, 410):
            with self.subTest(status=status):
                result = self.assess(status_code=status)
                self.assertFalse(result.accepted)
                self.assertEqual(result.health_code, SourceHealthCode.SOURCE_STALE)

    def test_http_202_is_not_a_successful_collection_contract(self):
        result = self.assess(status_code=202)
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.health_code, SourceHealthCode.TEMPORARY_UNEXPECTED_RESPONSE
        )

    def test_generic_homepage_redirect_is_not_an_empty_board(self):
        result = self.assess(
            final_url="https://jobs.example.test/",
            redirect_count=1,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.health_code, SourceHealthCode.SOURCE_STALE)
        self.assertTrue(result.host_remained_approved)

    def test_redirect_to_a_different_ats_is_migration_suspected(self):
        result = self.assess(
            final_url="https://new-ats.example.net/company/jobs",
            redirect_count=2,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.health_code, SourceHealthCode.SOURCE_MIGRATION_SUSPECTED
        )
        self.assertFalse(result.host_remained_approved)

    def test_login_block_and_invalid_empty_http_200_are_never_accepted(self):
        cases = (
            ("<html>Sign in to continue</html>", SourceHealthCode.AUTHENTICATION_OR_BLOCK),
            ("<html>Access denied CAPTCHA</html>", SourceHealthCode.AUTHENTICATION_OR_BLOCK),
            ("<html>generic empty page</html>", SourceHealthCode.IDENTITY_FAILURE),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                result = self.assess(body_prefix=body)
                self.assertFalse(result.accepted)
                self.assertEqual(result.health_code, expected)


class RegistryIntegrityGateTests(unittest.TestCase):
    def test_every_current_source_has_a_machine_readable_contract_state(self):
        assessments = assess_registry(load_registry_entries(REGISTRY))
        self.assertEqual(len(assessments), 533)
        self.assertEqual(
            [item for item in assessments if item.action is IntegrityAction.ARCHITECTURAL],
            [],
        )
        self.assertTrue(
            all(item.activation_state is SourceActivationState.ACTIVE for item in assessments)
        )

    def test_registry_activation_does_not_claim_runtime_health(self):
        payload = validate_source_integrity(
            registry_path=REGISTRY,
            retained_pattern="__no_source_integrity_fixture__",
            company="ATCO",
        )
        row = payload["sources"][0]
        self.assertEqual(row["activation_scope"], "declared_source_contract")
        self.assertFalse(row["runtime_health_claimed"])

    def test_new_source_without_terminal_contract_is_quarantined(self):
        assessment = assess_registry_entry(
            {"name": "Future Co", "platform": "new_unreviewed_ats", "max_pages": 1, "max_jobs": 20}
        )
        self.assertEqual(assessment.activation_state, SourceActivationState.QUARANTINED)
        self.assertEqual(assessment.action, IntegrityAction.ARCHITECTURAL)

    def test_known_platform_still_requires_identity_host_and_source_contract(self):
        assessment = assess_registry_entry(
            {"name": "Future Static", "platform": "static_html", "max_pages": 1, "max_jobs": 20}
        )
        self.assertEqual(assessment.activation_state, SourceActivationState.PARTIAL)
        self.assertEqual(assessment.action, IntegrityAction.ARCHITECTURAL)
        self.assertGreaterEqual(len(assessment.reasons), 3)

    def test_config_bound_below_retained_total_is_actionable_when_adapter_cannot_adapt(self):
        assessment = assess_registry_entry(
            {
                "name": "Bounded PeopleSoft",
                "platform": "peoplesoft",
                "identity_regex": "Example",
                "listing_url": "https://jobs.example.test/search",
                "allowed_apply_hosts": ["jobs.example.test"],
                "max_pages": 1,
                "max_jobs": 20,
            },
            retained={"scan_state": "incomplete", "total_available": 71},
        )
        self.assertEqual(assessment.action, IntegrityAction.REVIEW_CONFIG)

    def test_unreachable_job_and_page_bounds_are_detected(self):
        assessment = assess_registry_entry(
            {
                "name": "Impossible PeopleSoft",
                "platform": "peoplesoft",
                "identity_regex": "Example",
                "listing_url": "https://jobs.example.test/search",
                "allowed_apply_hosts": ["jobs.example.test"],
                "max_pages": 1,
                "max_jobs": 300,
                "page_size": 20,
            }
        )
        self.assertEqual(assessment.action, IntegrityAction.REVIEW_CONFIG)
        self.assertIn("unreachable", " ".join(assessment.reasons))

    def test_adaptive_repairs_clear_all_three_current_review_config_sources(self):
        expected = {
            "Pason Systems": ({"max_pages": 1, "max_jobs": 50}, 50, 26),
            "Trican Well Service": ({"max_pages": 1, "max_jobs": 50}, 500, 30),
            "MNP": ({"max_pages": 3, "max_jobs": 150}, 50, 139),
        }
        for company, (bounds, page_capacity, retained_total) in expected.items():
            with self.subTest(company=company):
                payload = validate_source_integrity(
                    registry_path=REGISTRY,
                    company=company,
                )
                self.assertEqual(payload["summary"]["REVIEW_CONFIG"], 0)
                self.assertEqual(payload["sources"][0]["activation_state"], "active")
                self.assertEqual(payload["sources"][0]["configured_bounds"], bounds)
                self.assertGreaterEqual(
                    bounds["max_pages"] * page_capacity,
                    max(bounds["max_jobs"], retained_total),
                )

    def test_offline_tool_supports_one_company_and_makes_no_requests(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network used")):
            payload = validate_source_integrity(
                registry_path=REGISTRY,
                retained_pattern="__no_source_integrity_fixture__",
                company="ATCO",
            )
        self.assertEqual(payload["network_requests"], 0)
        self.assertEqual(payload["summary"]["sources"], 1)
        self.assertEqual(payload["sources"][0]["company"], "ATCO")
        self.assertEqual(payload["sources"][0]["terminal_strategy"], "authoritative_total")

    def test_offline_tool_is_deterministic_for_identical_inputs(self):
        first = validate_source_integrity(
            registry_path=REGISTRY,
            retained_pattern="__no_source_integrity_fixture__",
            company="ATCO",
        )
        second = validate_source_integrity(
            registry_path=REGISTRY,
            retained_pattern="__no_source_integrity_fixture__",
            company="ATCO",
        )
        self.assertEqual(first, second)

    def test_live_canary_plan_is_bounded_and_offline_until_opted_in(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network used")):
            targets = selected_targets(list(DEFAULT_CANARIES))
        self.assertEqual(len(targets), 5)
        self.assertEqual([target.name for target in targets], list(DEFAULT_CANARIES))
        self.assertIn("ATCO", DEFAULT_CANARIES)

    def test_bounded_canary_preserves_progress_and_has_no_persistence_path(self):
        target = CompanyTarget(name="Bounded", platform="fixture")

        def interrupted(session, _target, _timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    jobs_normalized=9,
                    total_available=100,
                    authoritative_total_present=True,
                    has_more=True,
                    source_identity_status="validated",
                ),
            )
            raise CanaryBudgetReached("fixture budget")

        with patch.dict("scripts.validate_source_integrity_canaries.ADAPTERS", {"fixture": interrupted}), patch(
            "builtins.open", side_effect=AssertionError("canary attempted persistence")
        ):
            row = run_canary(target, 1)

        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["canary_outcome"], "PASS_PARTIAL")
        self.assertEqual(row["jobs_collected"], 9)
        self.assertEqual(row["scan_state"], "incomplete")
        self.assertEqual(row["source_health_code"], "safety_limit_reached")
        self.assertEqual(row["source_identity_status"], "validated")

    def test_bounded_canary_surfaces_record_rejection_and_dedup_diagnostics(self):
        target = CompanyTarget(name="Diagnostic", platform="fixture")

        def incomplete(session, _target, _timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=False,
                    scan_completeness_reason="malformed_listing_records",
                    stop_reason="total_jobs_reached",
                    total_available=3,
                    rejected_record_count=1,
                    rejected_record_reason="missing_structured_location",
                    rejected_record_ids=[
                        {"posting_id": 103, "requisition_id": 503}
                    ],
                    rejected_posting_ids=[103],
                    rejected_requisition_ids=[503],
                    duplicate_requisitions_skipped=1,
                    duplicate_posting_ids_skipped=[102],
                    duplicate_requisition_ids=[501],
                ),
            )
            return [object()]

        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": incomplete},
        ):
            row = run_canary(target, 1)

        self.assertEqual(row["rejected_record_reason"], "missing_structured_location")
        self.assertEqual(
            row["rejected_record_ids"],
            [{"posting_id": 103, "requisition_id": 503}],
        )
        self.assertEqual(row["rejected_posting_ids"], [103])
        self.assertEqual(row["rejected_requisition_ids"], [503])
        self.assertEqual(row["duplicate_requisitions_skipped"], 1)
        self.assertEqual(row["duplicate_posting_ids_skipped"], [102])
        self.assertEqual(row["duplicate_requisition_ids"], [501])

    def test_openai_and_microsoft_bounded_canaries_publish_validated_identity(self):
        cases = (
            ("OpenAI", 14, 829),
            ("Microsoft", 150, 2_324),
        )
        for company, collected, total in cases:
            with self.subTest(company=company):
                target = CompanyTarget(name=company, platform="fixture")

                def interrupted(session, _target, _timeout):
                    publish_source_scan_metadata(
                        session,
                        SourceScanMetadata(
                            jobs_normalized=collected,
                            total_available=total,
                            has_more=True,
                            source_identity_status="validated",
                        ),
                    )
                    raise CanaryBudgetReached("fixture budget")

                with patch.dict(
                    "scripts.validate_source_integrity_canaries.ADAPTERS",
                    {"fixture": interrupted},
                ):
                    row = run_canary(target, 15)

                self.assertEqual(row["canary_outcome"], "PASS_PARTIAL")
                self.assertEqual(row["source_identity_status"], "validated")
                self.assertEqual(row["jobs_collected"], collected)
                self.assertEqual(row["total_available"], total)
                self.assertFalse(row["jobs_collected"] == row["total_available"])

    def test_pass_outcomes_require_validated_identity(self):
        def complete(identity_status):
            def adapter(session, _target, _timeout):
                publish_source_scan_metadata(
                    session,
                    SourceScanMetadata(
                        scan_complete=True,
                        scan_completeness_reason="reported_total_reached",
                        stop_reason="reported_total_reached",
                        authoritative_total_present=True,
                        total_available=0,
                        source_identity_status=identity_status,
                    ),
                )
                return []

            return adapter

        target = CompanyTarget(name="Complete", platform="fixture")
        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": complete("unproven")},
        ):
            unproven_complete = run_canary(target, 1)
        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": complete("validated")},
        ):
            validated_complete = run_canary(target, 1)

        self.assertEqual(unproven_complete["scan_state"], "complete")
        self.assertEqual(unproven_complete["canary_outcome"], "FAIL_IDENTITY")
        self.assertEqual(validated_complete["canary_outcome"], "PASS_COMPLETE")
        self.assertEqual(validated_complete["source_identity_status"], "validated")

        def partial(session, _target, _timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(jobs_normalized=1, source_identity_status="unproven"),
            )
            raise CanaryBudgetReached("fixture budget")

        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": partial},
        ):
            unproven_partial = run_canary(target, 1)
        self.assertEqual(unproven_partial["canary_outcome"], "FAIL_CONTRACT")
        self.assertEqual(unproven_partial["status"], "failed")

    def test_atco_complete_canary_behavior_is_unchanged(self):
        target = CompanyTarget(name="ATCO", platform="fixture")

        def complete(session, _target, _timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=True,
                    scan_completeness_reason="reported_total_reached",
                    stop_reason="total_jobs_reached",
                    authoritative_total_present=True,
                    total_available=76,
                    source_identity_status="validated",
                ),
            )
            return [object()] * 76

        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": complete},
        ):
            row = run_canary(target, 1)
        self.assertEqual(row["canary_outcome"], "PASS_COMPLETE")
        self.assertEqual(row["source_health_code"], "healthy")
        self.assertEqual(row["source_identity_status"], "validated")
        self.assertEqual(row["jobs_collected"], 76)
        self.assertEqual(row["total_available"], 76)

    def test_complete_public_json_canary_requires_reported_collection_integrity(self):
        target = CompanyTarget(name="JSON", platform="public_json")

        def complete(integrity: bool):
            def adapter(session, _target, _timeout):
                values = {
                    "scan_complete": True,
                    "scan_completeness_reason": "complete_collection_response",
                    "stop_reason": "complete_collection_response",
                    "complete_collection_response": True,
                    "source_identity_status": "verified",
                    "jobs_normalized": 2,
                }
                if integrity:
                    values.update(
                        collection_integrity_contract=True,
                        unique_stable_ids=2,
                        unique_canonical_urls=2,
                        duplicate_ids=[],
                        duplicate_canonical_urls=[],
                    )
                publish_source_scan_metadata(session, SourceScanMetadata(**values))
                return [object(), object()]

            return adapter

        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"public_json": complete(False)},
        ):
            missing = run_canary(target, 1)
        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"public_json": complete(True)},
        ):
            proven = run_canary(target, 1)

        self.assertEqual(missing["canary_outcome"], "FAIL_CONTRACT")
        self.assertFalse(missing["stable_ids_unique"])
        self.assertEqual(proven["canary_outcome"], "PASS_COMPLETE")
        self.assertTrue(proven["stable_ids_unique"])
        self.assertTrue(proven["canonical_urls_unique"])

    def test_identity_contract_failure_is_fail_contract(self):
        target = CompanyTarget(name="Mismatch", platform="fixture")

        def mismatch(session, _target, _timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(source_identity_status="failed"),
            )
            raise AdapterError("source identity mismatch")

        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": mismatch},
        ):
            row = run_canary(target, 1)
        self.assertEqual(row["canary_outcome"], "FAIL_IDENTITY")
        self.assertEqual(row["source_health_code"], "identity_failure")
        self.assertEqual(row["source_identity_status"], "failed")

    def test_cn_rail_contract_migration_is_not_source_unreachable(self):
        target = CompanyTarget(
            name="CN Rail",
            platform="fixture",
            careers_url="https://www.cn.ca/en/careers",
            tenant="cn360",
            site_id="1",
        )

        def migrated(session, _target, _timeout):
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    configured_tenant="cn360",
                    configured_site="1",
                    source_identity_status="failed",
                    source_identity_diagnostic=(
                        "official careers page did not link the configured tenant and site"
                    ),
                ),
            )
            raise AdapterError(
                "Cornerstone CSOD official careers page did not link the configured tenant and site"
            )

        with patch.dict("scripts.validate_source_integrity_canaries.ADAPTERS", {"fixture": migrated}):
            row = run_canary(target, 1)

        self.assertEqual(row["canary_outcome"], "MIGRATION_SUSPECTED")
        self.assertEqual(row["source_health_code"], "source_migration_suspected")
        self.assertEqual(row["configured_tenant"], "cn360")
        self.assertEqual(row["configured_site"], "1")
        self.assertEqual(row["source_identity_status"], "failed")

    def test_waymo_partial_inventory_plus_http_202_cannot_complete(self):
        target = CompanyTarget(name="Waymo", platform="fixture")

        def accepted_without_contract(session, _target, _timeout):
            session.requests_started = 8
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    jobs_normalized=180,
                    pages_requested=7,
                    total_available=358,
                ),
            )
            raise AdapterError("Static HTML HTTP 202")

        with patch.dict(
            "scripts.validate_source_integrity_canaries.ADAPTERS",
            {"fixture": accepted_without_contract},
        ):
            row = run_canary(target, 15)

        self.assertEqual(row["jobs_collected"], 180)
        self.assertEqual(row["total_available"], 358)
        self.assertEqual(row["scan_state"], "failed")
        self.assertFalse(row["scan_state"] == "complete")
        self.assertEqual(row["canary_outcome"], "FAIL_TRANSIENT")
        self.assertEqual(row["source_identity_status"], "unproven")
        self.assertEqual(
            row["source_health_code"], "temporary_unexpected_response"
        )
        self.assertEqual(row["stop_reason"], "temporary_unexpected_response")
        self.assertNotEqual(row["stop_reason"], "not_started")

    def test_retained_stale_and_migration_states_are_machine_readable(self):
        entries = [
            {"name": "Stale", "platform": "greenhouse", "slug": "stale"},
            {"name": "Moved", "platform": "greenhouse", "slug": "moved"},
        ]
        assessed = assess_registry(
            entries,
            retained_by_company={
                "stale": {"scan_state": "failed", "error_type": "stale_board_identifier"},
                "moved": {"scan_state": "failed", "error_type": "probable_ats_migration"},
            },
        )
        self.assertEqual(assessed[0].action, IntegrityAction.SOURCE_STALE)
        self.assertEqual(assessed[1].action, IntegrityAction.MIGRATION_REQUIRED)


if __name__ == "__main__":
    unittest.main()
