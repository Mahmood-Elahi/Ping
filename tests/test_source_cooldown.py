from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from src.scraper.company_careers.models import (
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
)
from src.scraper.company_careers.scanner import CompanyCareersScanner
from src.scraper.company_careers.source_cooldown import (
    classify_failure,
    cooldown_active,
    reconcile_source_health,
    source_health_key,
)


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def target() -> CompanyTarget:
    return CompanyTarget(name="Example", platform="lever", slug="example")


def transient_result() -> CompanyScanResult:
    return CompanyScanResult(
        target=target(),
        status=CoverageStatus.FAILED_TECHNICAL,
        error="ConnectionError: offline",
        error_type="request_error",
        stop_reason="request_failure",
        scan_state="failed",
        source_health_code="source_unreachable",
    )


def successful_result() -> CompanyScanResult:
    return CompanyScanResult(
        target=target(),
        status=CoverageStatus.FETCHED,
        scan_complete=True,
        scan_state="complete",
        source_health_code="healthy",
        inventory_complete=True,
    )


class SourceCooldownTests(unittest.TestCase):
    def test_third_fourth_and_fifth_transient_failures_back_off(self):
        key = source_health_key(target())
        state = {}
        expected = [(1, 0), (2, 0), (3, 6), (4, 12), (5, 24)]
        for index, (count, hours) in enumerate(expected):
            at = NOW + timedelta(days=index)
            rows = reconcile_source_health([transient_result()], state, as_of=at)
            row = rows[0]
            self.assertEqual(row["consecutive_transient_failures"], count)
            if hours:
                self.assertTrue(cooldown_active(row, as_of=at))
                self.assertEqual(
                    datetime.fromisoformat(row["cooldown_until"]),
                    at + timedelta(hours=hours),
                )
            else:
                self.assertIsNone(row["cooldown_until"])
            state = {key: row}

    def test_authoritative_recovery_probe_clears_streak(self):
        key = source_health_key(target())
        previous = {
            key: {
                "source_key": key,
                "consecutive_transient_failures": 4,
                "cooldown_until": (NOW - timedelta(minutes=1)).isoformat(),
                "last_failure_classification": "transient_transport",
            }
        }

        row = reconcile_source_health(
            [successful_result()], previous, as_of=NOW
        )[0]

        self.assertEqual(row["consecutive_transient_failures"], 0)
        self.assertIsNone(row["cooldown_until"])
        self.assertEqual(
            row["last_successful_authoritative_inventory"], NOW.isoformat()
        )

    def test_non_authoritative_result_does_not_clear_streak(self):
        key = source_health_key(target())
        previous = {
            key: {
                "source_key": key,
                "consecutive_transient_failures": 4,
                "cooldown_until": (NOW - timedelta(minutes=1)).isoformat(),
                "last_failure_classification": "transient_transport",
            }
        }
        partial = CompanyScanResult(
            target=target(),
            status=CoverageStatus.FETCHED,
            jobs=[],
            scan_complete=False,
            scan_state="incomplete",
            source_health_code="pagination_unproven",
            inventory_complete=False,
        )

        row = reconcile_source_health([partial], previous, as_of=NOW)[0]

        self.assertEqual(row["consecutive_transient_failures"], 4)
        self.assertEqual(
            row["last_failure_classification"], "transient_transport"
        )
        self.assertEqual(
            row["cooldown_until"], (NOW - timedelta(minutes=1)).isoformat()
        )

    def test_source_configuration_change_bypasses_stale_cooldown(self):
        old = target()
        repaired = CompanyTarget(
            name=old.name,
            platform=old.platform,
            slug="repaired-example",
        )
        old_key = source_health_key(old)
        new_key = source_health_key(repaired)
        self.assertNotEqual(old_key, new_key)

        scanner = CompanyCareersScanner(
            targets=[repaired],
            session=object(),
            parent_source_mappings=[],
            cooldown_states={
                old_key: {
                    "source_key": old_key,
                    "cooldown_until": (NOW + timedelta(hours=1)).isoformat(),
                }
            },
            as_of=NOW,
        )

        # The repaired source is not skipped; the deliberately minimal session
        # reaches normal adapter execution and is contained as a source failure.
        result = scanner.scan().results[0]
        self.assertNotEqual(result.status, CoverageStatus.COOLDOWN_SKIPPED)

    def test_one_tenant_failure_does_not_affect_another_source(self):
        failed = CompanyTarget(
            name="Example A", platform="lever", slug="tenant-a"
        )
        healthy = CompanyTarget(
            name="Example B", platform="lever", slug="tenant-b"
        )
        failed_key = source_health_key(failed)
        healthy_key = source_health_key(healthy)
        self.assertNotEqual(failed_key, healthy_key)

        scanner = CompanyCareersScanner(
            targets=[healthy],
            session=object(),
            parent_source_mappings=[],
            cooldown_states={
                failed_key: {
                    "source_key": failed_key,
                    "cooldown_until": (NOW + timedelta(hours=1)).isoformat(),
                }
            },
            as_of=NOW,
        )

        result = scanner.scan().results[0]
        self.assertNotEqual(result.status, CoverageStatus.COOLDOWN_SKIPPED)

    def test_configuration_and_identity_failures_do_not_advance_streak(self):
        key = source_health_key(target())
        previous = {
            key: {
                "source_key": key,
                "consecutive_transient_failures": 2,
                "cooldown_until": None,
                "last_failure_classification": "transient_transport",
            }
        }
        for error_type, health_code in (
            ("adapter_protocol_failure", "source_unreachable"),
            ("validation_identity_mismatch", "identity_failure"),
            ("adapter_schema_mismatch", "malformed_collection"),
        ):
            with self.subTest(error_type=error_type):
                result = CompanyScanResult(
                    target=target(),
                    status=CoverageStatus.FAILED_TECHNICAL,
                    error="deterministic failure",
                    error_type=error_type,
                    scan_state="failed",
                    source_health_code=health_code,
                )
                row = reconcile_source_health(
                    [result], previous, as_of=NOW
                )[0]
                self.assertEqual(row["consecutive_transient_failures"], 2)
                self.assertIsNone(row["cooldown_until"])

    def test_cooldown_skip_performs_no_request_and_has_no_absence_authority(self):
        current = target()
        key = source_health_key(current)
        scanner = CompanyCareersScanner(
            targets=[current],
            session=object(),
            parent_source_mappings=[],
            cooldown_states={
                key: {
                    "source_key": key,
                    "cooldown_until": (NOW + timedelta(hours=1)).isoformat(),
                }
            },
            as_of=NOW,
        )

        result = scanner.scan().results[0]

        self.assertEqual(result.status, CoverageStatus.COOLDOWN_SKIPPED)
        self.assertEqual(result.source_health_code, "cooldown_skipped")
        self.assertFalse(result.authoritative_for_absence)
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.pages_requested, 0)

    def test_dayforce_and_successfactors_identity_fields_are_fingerprinted_but_budget_is_not(self):
        dayforce = CompanyTarget(
            name="Example",
            platform="dayforce",
            tenant="tenant-a",
            site_id="BOARD",
            locale="en-CA",
            endpoint_variant="nextjs_jobposting_search",
            search_url="https://jobs.dayforcehcm.com/en-CA/tenant-a/BOARD",
            endpoint_url="https://jobs.dayforcehcm.com/api/geo/tenant-a/jobposting/search",
            dayforce_identity={
                "official_source_url": "https://example.com/careers",
                "expected_tenant": "tenant-a",
                "expected_board": "BOARD",
            },
        )
        tuned = CompanyTarget(**{**dayforce.__dict__, "protocol_request_budget": 17})
        reboarded = CompanyTarget(**{**dayforce.__dict__, "site_id": "OTHER"})
        self.assertEqual(source_health_key(dayforce), source_health_key(tuned))
        self.assertNotEqual(source_health_key(dayforce), source_health_key(reboarded))

        successfactors = CompanyTarget(
            name="Example",
            platform="sap_successfactors",
            careers_url="https://example.com/careers",
            search_url="https://jobs.example.com/search/",
            endpoint_variant="rmk_table",
            locale="en_US",
            identity_regex="Example",
        )
        changed_locale = CompanyTarget(
            **{**successfactors.__dict__, "locale": "fr_CA"}
        )
        changed_identity = CompanyTarget(
            **{**successfactors.__dict__, "identity_regex": "Example Canada"}
        )
        self.assertNotEqual(
            source_health_key(successfactors), source_health_key(changed_locale)
        )
        self.assertNotEqual(
            source_health_key(successfactors), source_health_key(changed_identity)
        )

    def test_protocol_contradictions_do_not_advance_cooldown_but_transient_http_does(self):
        for reason in (
            "bootstrap_invalid",
            "identity_unproven",
            "pagination_contradiction",
            "protocol_contract_invalid",
            "dayforce_request_budget_exhausted",
            "successfactors_request_budget_exhausted",
        ):
            with self.subTest(reason=reason):
                result = CompanyScanResult(
                    target=target(),
                    status=CoverageStatus.FETCHED,
                    scan_complete=False,
                    stop_reason=reason,
                    scan_state="incomplete",
                    source_health_code=(
                        "safety_limit_reached" if "budget" in reason else "pagination_unproven"
                    ),
                )
                self.assertEqual(classify_failure(result), "non_transient")

        transient = CompanyScanResult(
            target=target(),
            status=CoverageStatus.FAILED_TECHNICAL,
            error="HTTP 503",
            error_type="transient_upstream_http",
            stop_reason="request_failure",
            scan_state="failed",
            source_health_code="temporary_unexpected_response",
        )
        self.assertTrue(classify_failure(transient).startswith("transient_"))


if __name__ == "__main__":
    unittest.main()
