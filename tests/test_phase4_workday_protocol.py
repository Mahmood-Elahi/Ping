from __future__ import annotations

import unittest
from pathlib import Path

import requests
import yaml

from src.scraper.company_careers.adapters import workday
from src.scraper.company_careers.models import (
    CompanyScanResult,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.source_cooldown import classify_failure
from src.scraper.company_careers.source_health import finalize_source_scan


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def item(index: int) -> dict:
    return {
        "title": f"Role {index}",
        "locationsText": "Remote",
        "externalPath": f"/job/Remote/Role_R{100000 + index}",
        "bulletFields": [f"R{100000 + index}"],
    }


def identity() -> dict[str, str]:
    return {
        "official_source_url": "https://careers.example.com/work-with-us",
        "expected_workday_tenant": "example",
        "expected_workday_site": "External",
        "expected_workday_hostname": "example.wd1.myworkdayjobs.com",
    }


def target(**overrides) -> CompanyTarget:
    values = {
        "name": "Example",
        "platform": "workday",
        "tenant": "example",
        "wd_subdomain": "wd1",
        "site_id": "External",
        "max_pages": 25,
        "max_jobs": 500,
        "workday_identity": identity(),
    }
    values.update(overrides)
    return CompanyTarget(**values)


class DatasetSession:
    """Deterministic Workday CXS model with optional overlapping facets."""

    facet = "locationHierarchy1"

    def __init__(
        self,
        total: int,
        partitions: dict[str, list[int]] | None = None,
        *,
        fail_partition: str | None = None,
        root_totals: list[int] | None = None,
        partition_total_delta: dict[str, int] | None = None,
        repeat_after_cap: bool = False,
    ) -> None:
        self.total = total
        self.root_ids = list(range(total))
        self.partitions = partitions or {}
        self.fail_partition = fail_partition
        self.root_totals = list(root_totals or [])
        self.partition_total_delta = partition_total_delta or {}
        self.repeat_after_cap = repeat_after_cap
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        body = kwargs["json"]
        offset = int(body.get("offset", 0))
        applied = body.get("appliedFacets") or {}
        if not applied:
            reported = self.root_totals.pop(0) if self.root_totals else self.total
            ids = self.root_ids
            if self.repeat_after_cap and offset >= workday.SAFE_QUERY_CAPACITY:
                ids = self.root_ids[: workday.PAGE_SIZE]
                offset = 0
            return Response(self._payload(ids, offset, reported, selected=None))
        selected = str(applied[self.facet][0])
        if selected == self.fail_partition:
            return Response({}, status_code=503)
        ids = self.partitions[selected]
        reported = len(ids) + int(self.partition_total_delta.get(selected, 0))
        return Response(self._payload(ids, offset, reported, selected=selected))

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        raise AssertionError("Configured Workday identity must not make live identity requests")

    def _payload(self, ids, offset, reported, *, selected):
        if selected is None:
            facets = [
                {
                    "facetParameter": self.facet,
                    "values": [
                        {"id": name, "count": len(values)}
                        for name, values in self.partitions.items()
                    ],
                }
            ] if self.partitions else []
        else:
            facets = [
                {
                    "facetParameter": self.facet,
                    "values": [{"id": selected, "count": reported}],
                }
            ]
        return {
            "total": reported,
            "jobPostings": [item(index) for index in ids[offset:offset + workday.PAGE_SIZE]],
            "facets": facets,
        }


class RaisingSession(DatasetSession):
    def post(self, url, **kwargs):
        body = kwargs["json"]
        applied = body.get("appliedFacets") or {}
        if applied and str(applied[self.facet][0]) == self.fail_partition:
            self.post_calls.append((url, kwargs))
            raise requests.Timeout("fixture timeout")
        return super().post(url, **kwargs)


class ConflictingIdentitySession(DatasetSession):
    def _payload(self, ids, offset, reported, *, selected):
        payload = super()._payload(ids, offset, reported, selected=selected)
        if selected == "B" and offset == 0 and payload["jobPostings"]:
            payload["jobPostings"][0] = {
                "title": "Conflicting role",
                "locationsText": "Elsewhere",
                "externalPath": "/job/Elsewhere/Conflicting_R100000",
                "bulletFields": ["R100000"],
            }
        return payload


class TotalLessSession(DatasetSession):
    def _payload(self, ids, offset, reported, *, selected):
        payload = super()._payload(ids, offset, reported, selected=selected)
        payload.pop("total", None)
        return payload


class WorkdayPhase4ProtocolTests(unittest.TestCase):
    def test_1999_root_is_below_cap_and_completes(self):
        session = DatasetSession(1_999)
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), 1_999)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata.get("workday_partitioned", False))

    def test_exactly_2000_without_facets_is_unsplittable(self):
        session = DatasetSession(2_000)
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), workday.PAGE_SIZE)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "workday_unsplittable_partition")

    def test_2000_rows_without_total_is_provider_cap_not_terminal(self):
        session = TotalLessSession(2_000)
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), 2_000)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "workday_provider_cap_reached")

    def test_2001_is_partitioned_and_complete(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(2_001, partitions)
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), 2_001)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["partition_count"], 3)

    def test_2500_is_partitioned_and_complete(self):
        partitions = {"A": list(range(1_250)), "B": list(range(1_250, 2_500))}
        result = workday.fetch_jobs(DatasetSession(2_500, partitions), target())
        self.assertEqual(len(result), 2_500)

    def test_4000_plus_overlapping_partitions_are_deduplicated(self):
        partitions = {
            "A": list(range(0, 1_500)),
            "B": list(range(1_400, 2_900)),
            "C": list(range(2_501, 4_001)),
        }
        session = DatasetSession(4_001, partitions)
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), 4_001)
        self.assertEqual(metadata["duplicate_rows_across_partitions"], 499)
        self.assertEqual(metadata["raw_rows_fetched"], 4_520)
        self.assertTrue(metadata["scan_complete"])

    def test_duplicate_stable_id_with_conflicting_path_fails_closed(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = ConflictingIdentitySession(2_001, partitions)
        workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "workday_partition_contradiction")
        self.assertEqual(metadata["collection_restart_count"], 1)

    def test_missing_partition_eliminates_global_authority(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(2_001, partitions, fail_partition="B")
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertGreater(len(result), 0)
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["stop_reason"], "workday_partition_transient_http")

    def test_transient_partition_transport_failure_keeps_partial_progress(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = RaisingSession(2_001, partitions, fail_partition="B")
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), 1_100)
        self.assertEqual(metadata["stop_reason"], "workday_partition_transport_failure")

    def test_large_child_without_finer_facet_is_unsplittable(self):
        partitions = {"A": list(range(2_001)), "B": list(range(2_001, 2_101))}
        session = DatasetSession(2_101, partitions)
        result = workday.fetch_jobs(session, target())
        self.assertGreater(len(result), 0)
        self.assertEqual(
            source_scan_metadata(session)["stop_reason"],
            "workday_unsplittable_partition",
        )

    def test_partition_depth_limit_is_a_safety_stop(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(2_001, partitions)
        workday.fetch_jobs(session, target(workday_max_partition_depth=0))
        metadata = source_scan_metadata(session)
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "workday_partition_depth_limit")

    def test_partition_count_limit_is_a_safety_stop(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(2_001, partitions)
        workday.fetch_jobs(session, target(workday_max_partitions=2))
        self.assertEqual(
            source_scan_metadata(session)["stop_reason"],
            "workday_partition_count_limit",
        )

    def test_source_wide_request_budget_exhaustion_is_incomplete(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(2_001, partitions)
        workday.fetch_jobs(session, target(workday_request_budget=3))
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["requests_used"], 3)
        self.assertEqual(metadata["stop_reason"], "workday_request_budget_exhausted")

    def test_contradictory_partition_total_restarts_once_then_fails(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(
            2_001, partitions, partition_total_delta={"B": 1}
        )
        workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["collection_restart_count"], 1)
        self.assertTrue(metadata["collection_retry_exhausted"])
        self.assertFalse(metadata["scan_complete"])

    def test_root_total_mutation_restarts_whole_partition_traversal(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        session = DatasetSession(
            2_001,
            partitions,
            root_totals=[2_001, 2_002, 2_001, 2_001],
        )
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(result), 2_001)
        self.assertEqual(metadata["collection_restart_count"], 1)
        self.assertTrue(metadata["stabilized_after_collection_retry"])

    def test_invalid_configured_identity_fails_before_cxs_traversal(self):
        evidence = identity()
        evidence["expected_workday_site"] = "Wrong"
        session = DatasetSession(0)
        result = workday.fetch_jobs(session, target(workday_identity=evidence))
        metadata = source_scan_metadata(session)
        self.assertEqual(list(result), [])
        self.assertEqual(session.post_calls, [])
        self.assertEqual(metadata["source_identity_status"], "failed")

    def test_authoritative_zero_requires_validated_identity(self):
        validated = DatasetSession(0)
        result = workday.fetch_jobs(validated, target())
        metadata = source_scan_metadata(validated)
        finalize_source_scan(metadata, jobs_collected=len(result))
        self.assertTrue(metadata["inventory_complete"])

        unproven = DatasetSession(0)
        result = workday.fetch_jobs(unproven, target(workday_identity={}))
        metadata = source_scan_metadata(unproven)
        finalize_source_scan(metadata, jobs_collected=len(result))
        self.assertFalse(metadata["inventory_complete"])

    def test_explicit_identity_is_cached_without_identity_requests(self):
        session = DatasetSession(1)
        workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["workday_identity_checks"], 1)
        self.assertEqual(session.get_calls, [])

    def test_duplicate_partition_state_guard_is_deterministic(self):
        state = workday._partition_state({"locationHierarchy1": ["CA"]})
        equivalent = workday._partition_state({"LOCATIONHIERARCHY1": ["ca"]})
        self.assertEqual(state, equivalent)

    def test_partial_partition_result_has_inventory_items(self):
        partitions = {"A": list(range(1_100)), "B": list(range(1_100, 2_001))}
        result = workday.fetch_jobs(
            DatasetSession(2_001, partitions, fail_partition="B"), target()
        )
        self.assertEqual(len(result.inventory_items), len(result.jobs))
        self.assertGreater(len(result.inventory_items), 0)

    def test_partition_contract_failures_do_not_enter_cooldown(self):
        # Health finalization maps this deterministic contract failure to the
        # non-transient partition class; cooldown classification must preserve it.
        partitions = {"A": list(range(2_001)), "B": list(range(2_001, 2_101))}
        session = DatasetSession(2_101, partitions)
        result = workday.fetch_jobs(session, target())
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["stop_reason"], "workday_unsplittable_partition")
        self.assertFalse(metadata["scan_complete"])

        deterministic = CompanyScanResult(
            target=target(),
            stop_reason="workday_unsplittable_partition",
            source_health_code="unsplittable_partition",
        )
        self.assertEqual(classify_failure(deterministic), "non_transient")

    def test_transient_partition_failures_do_enter_cooldown_policy(self):
        transient = CompanyScanResult(
            target=target(),
            stop_reason="workday_partition_transport_failure",
            source_health_code="temporary_unexpected_response",
        )
        self.assertEqual(classify_failure(transient), "transient_transport")

    def test_offsets_at_or_beyond_cap_are_never_used_as_terminal_proof(self):
        session = DatasetSession(2_001, repeat_after_cap=True)
        workday.fetch_jobs(session, target())
        offsets = [call[1]["json"].get("offset", 0) for call in session.post_calls]
        self.assertNotIn(workday.SAFE_QUERY_CAPACITY, offsets)
        self.assertEqual(
            source_scan_metadata(session)["stop_reason"],
            "workday_unsplittable_partition",
        )

    def test_known_phase4_targets_are_fixture_backed_without_activation(self):
        fixture = yaml.safe_load(
            Path("tests/fixtures/workday_phase4_known_targets.yaml").read_text(
                encoding="utf-8"
            )
        )
        rows = fixture["targets"]
        self.assertEqual(
            {row["company"] for row in rows},
            {
                "Applied Materials",
                "BlackRock",
                "Caterpillar",
                "Chevron",
                "General Motors",
                "Zalando",
            },
        )
        self.assertTrue(all(row["tenant"] and row["site"] for row in rows))
        self.assertTrue(all("wd_subdomain" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
