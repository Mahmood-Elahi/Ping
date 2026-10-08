from __future__ import annotations

import unittest
from unittest.mock import patch
from typing import get_type_hints

from src.scraper.company_careers.models import (
    AdapterError,
    AdapterResult,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
    source_scan_metadata,
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
)
from src.scraper.company_careers.scanner import ADAPTERS, CompanyCareersScanner


class FakeSession:
    def close(self) -> None:
        pass


def _job(job_id: str = "1") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        title="Engineer",
        company="Example",
        location="Calgary",
        url=f"https://example.test/jobs/{job_id}",
        updated_at="",
        source="test:example",
    )


def _explicit_metadata(*, pages_requested: int = 1) -> SourceScanMetadata:
    return SourceScanMetadata(
        scan_complete=True,
        scan_completeness_reason="reported_total_reached",
        total_available=1,
        authoritative_total_present=True,
        pages_requested=pages_requested,
        pages_completed=1,
        stop_reason="reported_total_reached",
        terminal_evidence_type="reported_total",
        terminal_evidence_value=1,
        terminal_page_observed=True,
        source_identity_status="validated",
    )


class AdapterResultTests(unittest.TestCase):
    def test_all_canonical_dispatcher_boundaries_are_explicit(self) -> None:
        batch_one = {
            "greenhouse",
            "lever",
            "ashby",
            "workday",
            "oracle_recruiting",
            "phenom",
            "sap_successfactors",
            "eightfold",
            "icims",
            "jobvite",
            "public_json",
            "first_party_json",
            "source_xml",
            "static_html",
            "javascript_embedded_data",
            "dayforce",
            "bamboohr",
            "teamtailor",
            "smartrecruiters",
        }
        batch_two = {
            "apple",
            "avature",
            "exalead",
            "clearcompany",
            "cornerstone_csod",
            "deel",
            "beesite",
            "startdate",
            "talentbrew",
            "hawksearch_v2",
            "hibob",
            "ukg_pro",
            "paycor",
            "peoplesoft",
            "amazon",
            "shopify",
            "google",
            "atlassian",
            "uber",
            "ibm_search",
        }
        final_batch = {"adp", "meta", "mcloud_cws"}
        decorated = batch_one | batch_two | final_batch
        for platform in decorated:
            self.assertTrue(
                getattr(
                    ADAPTERS[platform], "job_radar_explicit_adapter_result", False
                ),
                platform,
            )
        self.assertIs(
            get_type_hints(ADAPTERS["browser_rendered_html"])["return"],
            AdapterResult,
        )
        self.assertEqual(len(batch_one), 19)
        self.assertEqual(len(batch_two), 20)
        self.assertEqual(len(final_batch), 3)
        self.assertEqual(len(decorated) + 1, 43)
        self.assertEqual(
            {
                platform
                for platform, adapter in ADAPTERS.items()
                if not getattr(adapter, "job_radar_explicit_adapter_result", False)
                and platform != "browser_rendered_html"
            },
            set(),
        )
        aliases = {
            platform: canonical
            for platform, canonical in {
                "custom:amazon": "amazon",
                "custom:atlassian": "atlassian",
                "custom:google": "google",
                "custom:ibm_search": "ibm_search",
                "custom:meta": "meta",
                "custom:shopify": "shopify",
                "custom:uber": "uber",
            }.items()
            if ADAPTERS[platform] is ADAPTERS[canonical]
        }
        self.assertEqual(len(ADAPTERS), 50)
        self.assertEqual(len({id(adapter) for adapter in ADAPTERS.values()}), 43)
        self.assertEqual(len(aliases), 7)
        self.assertIs(ADAPTERS["custom:meta"], ADAPTERS["meta"])
        self.assertTrue(
            getattr(
                ADAPTERS["custom:meta"],
                "job_radar_explicit_adapter_result",
                False,
            )
        )

    def test_mutation_restart_returns_explicit_observations_without_policy_change(self) -> None:
        session = FakeSession()
        calls = 0

        def stable_once() -> list[JobRecord]:
            nonlocal calls
            calls += 1
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=True,
                    pages_requested=1,
                    pages_completed=1,
                    collection_changed_during_scan=False,
                ),
            )
            return [_job("stable")]

        stable = fetch_with_mutation_restart(session, stable_once)

        self.assertIsInstance(stable, AdapterResult)
        self.assertEqual(calls, 1)
        self.assertEqual([job.job_id for job in stable.jobs], ["stable"])
        self.assertEqual(stable.scan["pages_requested"], 1)

        traversals = iter(
            (
                ([_job("discarded")], True, False),
                ([_job("replacement")], False, True),
            )
        )

        def changing_once() -> list[JobRecord]:
            jobs, changed, complete = next(traversals)
            publish_source_scan_metadata(
                session,
                SourceScanMetadata(
                    scan_complete=complete,
                    pages_requested=2,
                    pages_completed=2,
                    collection_changed_during_scan=changed,
                ),
            )
            return jobs

        restarted = fetch_with_mutation_restart(session, changing_once)

        self.assertIsInstance(restarted, AdapterResult)
        self.assertEqual([job.job_id for job in restarted.jobs], ["replacement"])
        self.assertEqual(restarted.scan["collection_restart_count"], 1)
        self.assertEqual(restarted.scan["first_traversal_jobs"], 1)
        self.assertEqual(restarted.scan["total_pages_requested_across_traversals"], 4)
        self.assertTrue(restarted.scan["stabilized_after_collection_retry"])

    def test_result_is_typed_explicit_and_sequence_compatible(self) -> None:
        job = _job()
        original_metadata = _explicit_metadata()
        result = AdapterResult(jobs=[job], scan=original_metadata)
        original_metadata["pages_requested"] = 99

        self.assertEqual(result.jobs, [job])
        self.assertEqual(result.scan["pages_requested"], 1)
        self.assertEqual(result.item_failures, ())
        self.assertEqual(result.capabilities_used, frozenset())
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], job)
        self.assertEqual(list(result), [job])

    def test_migration_decorator_returns_explicit_result_and_keeps_diagnostic_alias(self) -> None:
        session = FakeSession()

        @explicit_adapter_result
        def adapter(current: FakeSession, _target: CompanyTarget, _timeout: int):
            metadata = _explicit_metadata(pages_requested=2)
            publish_source_scan_metadata(current, metadata)
            return [_job()]

        result = adapter(session, CompanyTarget(name="Example", platform="test"), 5)

        self.assertIsInstance(result, AdapterResult)
        self.assertEqual(result.scan["pages_requested"], 2)
        self.assertEqual(source_scan_metadata(session)["pages_requested"], 2)

    def test_migration_decorator_preserves_exception_type_and_attaches_evidence(self) -> None:
        session = FakeSession()

        @explicit_adapter_result
        def adapter(current: FakeSession, _target: CompanyTarget, _timeout: int):
            publish_source_scan_metadata(
                current,
                SourceScanMetadata(
                    pages_requested=3,
                    stop_reason="malformed_pagination",
                ),
            )
            raise AdapterError("unchanged failure")

        with self.assertRaisesRegex(AdapterError, "unchanged failure") as raised:
            adapter(session, CompanyTarget(name="Example", platform="test"), 5)

        metadata = getattr(raised.exception, "job_radar_adapter_scan_metadata")
        self.assertEqual(metadata["pages_requested"], 3)
        self.assertEqual(metadata["stop_reason"], "malformed_pagination")

    def test_failure_before_progress_attaches_default_observations(self) -> None:
        session = FakeSession()

        @explicit_adapter_result
        def adapter(_current: FakeSession, _target: CompanyTarget, _timeout: int):
            raise AdapterError("identity failed before listing")

        with self.assertRaisesRegex(
            AdapterError, "identity failed before listing"
        ) as raised:
            adapter(session, CompanyTarget(name="Example", platform="test"), 5)

        metadata = getattr(raised.exception, "job_radar_adapter_scan_metadata")
        self.assertEqual(metadata["pages_requested"], 0)
        self.assertEqual(metadata["stop_reason"], "not_started")

    def test_scanner_accepts_legacy_and_explicit_results(self) -> None:
        session = FakeSession()
        scanner = CompanyCareersScanner(
            targets=[], session=session, parent_source_mappings=[]
        )

        def legacy(current: FakeSession, _target: CompanyTarget, _timeout: int):
            publish_source_scan_metadata(
                current, _explicit_metadata(pages_requested=4)
            )
            return [_job("legacy")]

        def explicit(current: FakeSession, _target: CompanyTarget, _timeout: int):
            publish_source_scan_metadata(
                current, SourceScanMetadata(pages_requested=99)
            )
            return AdapterResult(
                jobs=[_job("explicit")],
                scan=_explicit_metadata(pages_requested=5),
            )

        common = dict(
            keywords=None,
            locations=None,
            max_jobs_per_company=None,
            job_filter=None,
            location_filter=None,
            browser_batch=None,
            browser_batch_error=None,
        )
        with patch.dict(
            "src.scraper.company_careers.scanner.ADAPTERS",
            {"legacy_test": legacy, "explicit_test": explicit},
            clear=False,
        ):
            legacy_result = scanner._scan_target(
                CompanyTarget(name="Legacy", platform="legacy_test"), **common
            )
            explicit_result = scanner._scan_target(
                CompanyTarget(name="Explicit", platform="explicit_test"), **common
            )

        self.assertEqual(legacy_result.pages_requested, 4)
        self.assertEqual(explicit_result.pages_requested, 5)
        self.assertEqual(explicit_result.normalized_jobs[0].job_id, "explicit")
        self.assertEqual(source_scan_metadata(session)["pages_requested"], 0)

    def test_scanner_prevents_metadata_leakage_across_sequential_mixed_adapters(self) -> None:
        session = FakeSession()
        scanner = CompanyCareersScanner(
            targets=[], session=session, parent_source_mappings=[]
        )
        observed_sessions: list[FakeSession] = []

        @explicit_adapter_result
        def explicit_one(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            observed_sessions.append(current)
            publish_source_scan_metadata(
                current, SourceScanMetadata(pages_requested=91)
            )
            return AdapterResult(
                jobs=[_job("explicit-one")],
                scan=_explicit_metadata(pages_requested=1),
            )

        @explicit_adapter_result
        def explicit_two(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            observed_sessions.append(current)
            publish_source_scan_metadata(
                current, SourceScanMetadata(pages_requested=92)
            )
            return AdapterResult(
                jobs=[_job("explicit-two")],
                scan=_explicit_metadata(pages_requested=2),
            )

        @explicit_adapter_result
        def explicit_failure(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            observed_sessions.append(current)
            publish_source_scan_metadata(
                current,
                SourceScanMetadata(
                    pages_requested=93,
                    pages_completed=1,
                    stop_reason="malformed_pagination",
                ),
            )
            raise AdapterError("explicit failure")

        def legacy_with_metadata(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            observed_sessions.append(current)
            publish_source_scan_metadata(
                current, _explicit_metadata(pages_requested=4)
            )
            return [_job("legacy-with-metadata")]

        def legacy_without_metadata(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            observed_sessions.append(current)
            return [_job("legacy-without-metadata")]

        common = dict(
            keywords=None,
            locations=None,
            max_jobs_per_company=None,
            job_filter=None,
            location_filter=None,
            browser_batch=None,
            browser_batch_error=None,
        )

        def scan(platform: str):
            return scanner._scan_target(
                CompanyTarget(name=platform, platform=platform), **common
            )

        adapters = {
            "explicit_one": explicit_one,
            "explicit_two": explicit_two,
            "explicit_failure": explicit_failure,
            "legacy_with_metadata": legacy_with_metadata,
            "legacy_without_metadata": legacy_without_metadata,
        }
        with patch.dict(
            "src.scraper.company_careers.scanner.ADAPTERS", adapters, clear=False
        ):
            first_explicit = scan("explicit_one")
            second_explicit = scan("explicit_two")
            failed_explicit = scan("explicit_failure")
            success_after_failure = scan("explicit_two")
            legacy_before_explicit = scan("legacy_with_metadata")
            explicit_after_legacy = scan("explicit_one")
            explicit_before_legacy = scan("explicit_two")
            legacy_after_explicit = scan("legacy_without_metadata")

        self.assertEqual(first_explicit.pages_requested, 1)
        self.assertEqual(second_explicit.pages_requested, 2)
        self.assertEqual(failed_explicit.pages_requested, 93)
        self.assertEqual(failed_explicit.stop_reason, "malformed_pagination")
        self.assertEqual(success_after_failure.pages_requested, 2)
        self.assertEqual(legacy_before_explicit.pages_requested, 4)
        self.assertEqual(explicit_after_legacy.pages_requested, 1)
        self.assertEqual(explicit_before_legacy.pages_requested, 2)
        self.assertEqual(legacy_after_explicit.pages_requested, 0)
        self.assertFalse(legacy_after_explicit.scan_complete)
        self.assertTrue(observed_sessions)
        self.assertTrue(all(current is session for current in observed_sessions))
        self.assertEqual(source_scan_metadata(session)["pages_requested"], 0)

    def test_named_alias_restart_meta_alias_and_adp_variants_do_not_leak(self) -> None:
        session = FakeSession()
        scanner = CompanyCareersScanner(
            targets=[], session=session, parent_source_mappings=[]
        )

        @explicit_adapter_result
        def restart_adapter(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            traversals = iter((True, False))

            def fetch_once() -> list[JobRecord]:
                changed = next(traversals)
                publish_source_scan_metadata(
                    current,
                    SourceScanMetadata(
                        scan_complete=not changed,
                        pages_requested=1,
                        pages_completed=1,
                        collection_changed_during_scan=changed,
                    ),
                )
                return [_job("restart-second" if not changed else "restart-first")]

            setattr(
                current,
                "restart_provider_pagination",
                SourceScanMetadata(pages_requested=701),
            )
            return fetch_with_mutation_restart(current, fetch_once)

        @explicit_adapter_result
        def plain_adapter(
            current: FakeSession, _target: CompanyTarget, _timeout: int
        ):
            setattr(
                current,
                "plain_provider_pagination",
                SourceScanMetadata(pages_requested=702),
            )
            publish_source_scan_metadata(current, _explicit_metadata(pages_requested=8))
            return [_job("plain")]

        @explicit_adapter_result
        def meta_alias_adapter(
            current: FakeSession, target: CompanyTarget, _timeout: int
        ):
            setattr(
                current,
                "meta_pagination",
                SourceScanMetadata(pages_requested=703),
            )
            pages = 10 if target.platform == "meta" else 11
            publish_source_scan_metadata(current, _explicit_metadata(pages_requested=pages))
            return [_job(target.platform)]

        @explicit_adapter_result
        def adp_variant_adapter(
            current: FakeSession, target: CompanyTarget, _timeout: int
        ):
            setattr(
                current,
                "adp_pagination",
                SourceScanMetadata(pages_requested=704),
            )
            pages = 12 if target.endpoint_variant == "variant-a" else 13
            publish_source_scan_metadata(current, _explicit_metadata(pages_requested=pages))
            return [_job(str(target.endpoint_variant))]

        common = dict(
            keywords=None,
            locations=None,
            max_jobs_per_company=None,
            job_filter=None,
            location_filter=None,
            browser_batch=None,
            browser_batch_error=None,
        )
        adapters = {
            "restart_test": restart_adapter,
            "plain_test": plain_adapter,
            "meta": meta_alias_adapter,
            "custom:meta": meta_alias_adapter,
            "adp": adp_variant_adapter,
        }
        with patch.dict(
            "src.scraper.company_careers.scanner.ADAPTERS", adapters, clear=False
        ):
            restart = scanner._scan_target(
                CompanyTarget(name="Restart", platform="restart_test"), **common
            )
            plain = scanner._scan_target(
                CompanyTarget(name="Plain", platform="plain_test"), **common
            )
            canonical_meta = scanner._scan_target(
                CompanyTarget(name="Meta", platform="meta"), **common
            )
            custom_meta = scanner._scan_target(
                CompanyTarget(name="Meta", platform="custom:meta"), **common
            )
            adp_a = scanner._scan_target(
                CompanyTarget(
                    name="ADP A", platform="adp", endpoint_variant="variant-a"
                ),
                **common,
            )
            adp_b = scanner._scan_target(
                CompanyTarget(
                    name="ADP B", platform="adp", endpoint_variant="variant-b"
                ),
                **common,
            )

        self.assertEqual(restart.pages_requested, 1)
        self.assertEqual(plain.pages_requested, 8)
        self.assertEqual(canonical_meta.pages_requested, 10)
        self.assertEqual(custom_meta.pages_requested, 11)
        self.assertEqual(adp_a.pages_requested, 12)
        self.assertEqual(adp_b.pages_requested, 13)
        self.assertEqual(source_scan_metadata(session)["pages_requested"], 0)

    def test_capabilities_cannot_create_terminal_evidence_or_absence_authority(self) -> None:
        session = FakeSession()
        scanner = CompanyCareersScanner(
            targets=[], session=session, parent_source_mappings=[]
        )

        def adapter(_session: FakeSession, _target: CompanyTarget, _timeout: int):
            return AdapterResult(
                jobs=[],
                scan=SourceScanMetadata(),
                capabilities_used=frozenset({"complete_collection"}),
            )

        with patch.dict(
            "src.scraper.company_careers.scanner.ADAPTERS",
            {"capability_test": adapter},
            clear=False,
        ):
            result = scanner._scan_target(
                CompanyTarget(name="Capability", platform="capability_test"),
                keywords=None,
                locations=None,
                max_jobs_per_company=None,
                job_filter=None,
                location_filter=None,
                browser_batch=None,
                browser_batch_error=None,
            )

        self.assertFalse(result.scan_complete)
        self.assertFalse(result.authoritative_for_absence)
        self.assertIsNone(result.terminal_evidence_type)


if __name__ == "__main__":
    unittest.main()
