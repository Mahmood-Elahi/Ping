import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import validate_source_integrity_canaries as canaries
from src.scraper.company_careers.adapters import greenhouse
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyScanResult,
    CoverageStatus,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_health import finalize_source_scan


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"


class StreamingResponse:
    def __init__(self, payload=None, *, body=None, status_code=200):
        self.body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), min(chunk_size, 37)):
            yield self.body[offset : offset + min(chunk_size, 37)]

    def close(self):
        self.closed = True


class StreamingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.requests_started = 0
        self.last_final_url = None
        self.last_redirect_count = 0

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        self.requests_started += 1
        self.last_final_url = url
        if not self.responses:
            raise AssertionError(f"Unexpected GET: {url}")
        return self.responses.pop(0)

    def close(self):
        pass


def listing(job_id, *, url=None):
    return {
        "id": job_id,
        "title": f"Waymo role {job_id}",
        "location": {"name": "Mountain View, CA"},
        "absolute_url": url
        or f"https://careers.withwaymo.com/jobs?gh_jid={job_id}",
        "updated_at": "2026-10-01T12:00:00Z",
    }


def detail(job_id):
    return {
        "id": job_id,
        "departments": [{"name": "Engineering"}],
        "offices": [{"name": "Mountain View"}],
        "content": f"Waymo job {job_id}",
    }


class WaymoGreenhouseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = next(
            target
            for target in load_target_companies(str(PRODUCTION))
            if target.name == "Waymo"
        )

    def test_production_waymo_explicitly_opts_in(self):
        self.assertEqual(self.target.name, "Waymo")
        self.assertEqual(self.target.platform, "greenhouse")
        self.assertEqual(self.target.slug, "waymo")
        self.assertEqual(self.target.endpoint_variant, greenhouse.STRICT_BOARD_VARIANT)
        self.assertEqual(self.target.allowed_apply_hosts, ["careers.withwaymo.com"])
        self.assertEqual(
            self.target.apply_url_template,
            "https://careers.withwaymo.com/jobs?gh_jid={id}",
        )
        self.assertEqual(self.target.source_activation, "active")
        self.assertEqual(canaries.selected_targets(["Waymo"], PRODUCTION), [self.target])

    def test_nonempty_board_preserves_waymo_identity_ids_and_canonical_urls(self):
        session = StreamingSession(
            [
                StreamingResponse({"jobs": [listing("101"), listing("202")]}),
                StreamingResponse(detail("101")),
                StreamingResponse(detail("202")),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, self.target)
        metadata = source_scan_metadata(session)
        canaries._record_job_integrity(metadata, jobs)
        finalize_source_scan(
            metadata,
            jobs_collected=len(jobs),
            activation_state=self.target.source_activation,
        )

        self.assertEqual(
            [call[0] for call in session.get_calls],
            [
                "https://boards-api.greenhouse.io/v1/boards/waymo/jobs",
                "https://boards-api.greenhouse.io/v1/boards/waymo/jobs/101",
                "https://boards-api.greenhouse.io/v1/boards/waymo/jobs/202",
            ],
        )
        self.assertEqual([job.company for job in jobs], ["Waymo", "Waymo"])
        self.assertEqual([job.job_id for job in jobs], ["101", "202"])
        self.assertEqual([job.source for job in jobs], ["greenhouse:waymo"] * 2)
        self.assertEqual(
            [job.url for job in jobs],
            [
                "https://careers.withwaymo.com/jobs?gh_jid=101",
                "https://careers.withwaymo.com/jobs?gh_jid=202",
            ],
        )
        self.assertEqual(metadata["source_identity_status"], "validated")
        self.assertEqual(metadata["unique_stable_ids"], 2)
        self.assertEqual(metadata["unique_canonical_urls"], 2)
        self.assertFalse(metadata["duplicate_ids"])
        self.assertFalse(metadata["duplicate_canonical_urls"])
        self.assertTrue(
            canaries._integrity_count_matches(
                metadata, "unique_stable_ids", "duplicate_ids"
            )
        )
        self.assertTrue(
            canaries._integrity_count_matches(
                metadata, "unique_canonical_urls", "duplicate_canonical_urls"
            )
        )
        self.assertEqual(metadata["terminal_evidence_type"], "complete_collection_response")
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["scan_state"], "complete")

    def test_authoritative_empty_collection_is_valid(self):
        session = StreamingSession([StreamingResponse({"jobs": []})])

        jobs = greenhouse.fetch_jobs(session, self.target)
        metadata = source_scan_metadata(session)
        finalize_source_scan(
            metadata,
            jobs_collected=0,
            activation_state=self.target.source_activation,
        )

        self.assertEqual(jobs, [])
        self.assertEqual(metadata["source_identity_status"], "validated")
        self.assertEqual(metadata["total_available"], 0)
        self.assertEqual(metadata["terminal_evidence_type"], "complete_collection_response")
        self.assertTrue(metadata["scan_complete"])

    def test_malformed_and_identity_invalid_responses_fail_closed(self):
        cases = (
            (
                StreamingSession([StreamingResponse(body=b"{")]),
                "malformed JSON",
            ),
            (
                StreamingSession(
                    [
                        StreamingResponse(
                            {"jobs": [listing("101", url="https://evil.test/waymo/jobs/101")]}
                        )
                    ]
                ),
                "exact board and stable ID",
            ),
            (
                StreamingSession(
                    [
                        StreamingResponse(
                            {
                                "jobs": [
                                    listing(
                                        "101",
                                        url="https://job-boards.greenhouse.io/not-waymo/jobs/101",
                                    )
                                ]
                            }
                        )
                    ]
                ),
                "exact board and stable ID",
            ),
        )
        for session, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AdapterError, message):
                    greenhouse.fetch_jobs(session, self.target)
                metadata = source_scan_metadata(session)
                self.assertFalse(metadata["scan_complete"])
                self.assertEqual(metadata["scan_completeness_reason"], "adapter_failure")

    def test_configuration_absent_preserves_old_strict_board_path_contract(self):
        target = type(self.target)(
            **{
                **self.target.__dict__,
                "allowed_apply_hosts": ["job-boards.greenhouse.io"],
                "apply_url_template": None,
            }
        )
        session = StreamingSession(
            [
                StreamingResponse(
                    {
                        "jobs": [
                            listing(
                                "101",
                                url="https://job-boards.greenhouse.io/waymo/jobs/101",
                            )
                        ]
                    }
                ),
                StreamingResponse(detail("101")),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, target)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "101")
        self.assertEqual(
            jobs[0].url,
            "https://job-boards.greenhouse.io/waymo/jobs/101",
        )

    def test_existing_strict_mode_url_matrix_is_unchanged(self):
        target = type(self.target)(
            **{
                **self.target.__dict__,
                "allowed_apply_hosts": ["job-boards.greenhouse.io"],
                "apply_url_template": None,
            }
        )
        valid_url = "https://job-boards.greenhouse.io/waymo/jobs/8234553"
        session = StreamingSession(
            [
                StreamingResponse(
                    {"jobs": [listing("8234553", url=valid_url)]}
                ),
                StreamingResponse(detail("8234553")),
            ]
        )
        self.assertEqual(greenhouse.fetch_jobs(session, target)[0].url, valid_url)

        invalid_urls = (
            "https://evil.example/waymo/jobs/8234553",
            "https://job-boards.greenhouse.io/not-waymo/jobs/8234553",
            "https://job-boards.greenhouse.io/waymo/other/8234553",
            "https://job-boards.greenhouse.io/waymo/jobs/9999999",
            "https://job-boards.greenhouse.io/waymo/jobs/8234553?gh_jid=8234553",
            "https://job-boards.greenhouse.io/waymo/jobs/8234553#fragment",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                session = StreamingSession(
                    [StreamingResponse({"jobs": [listing("8234553", url=url)]})]
                )
                with self.assertRaisesRegex(AdapterError, "exact board and stable ID"):
                    greenhouse.fetch_jobs(session, target)

    def test_custom_query_id_mode_accepts_only_the_exact_waymo_contract(self):
        valid_url = "https://careers.withwaymo.com/jobs?gh_jid=8234553"
        session = StreamingSession(
            [
                StreamingResponse(
                    {"jobs": [listing("8234553", url=valid_url)]}
                ),
                StreamingResponse(detail("8234553")),
            ]
        )
        self.assertEqual(
            greenhouse.fetch_jobs(session, self.target)[0].url,
            valid_url,
        )

        invalid_rows = (
            ("8234553", "https://careers.withwaymo.com/jobs?gh_jid=9999999"),
            ("8234553", "https://careers.withwaymo.com/jobs"),
            ("8234553", "https://careers.withwaymo.com/other?gh_jid=8234553"),
            ("8234553", "https://evil.example/jobs?gh_jid=8234553"),
            ("8234553", "https://careers.withwaymo.com/jobs?other=8234553"),
            (
                "8234553",
                "https://careers.withwaymo.com/jobs?gh_jid=8234553&foo=bar",
            ),
            (
                "8234553",
                "https://careers.withwaymo.com/jobs?gh_jid=8234553#fragment",
            ),
            (
                "8234553",
                "https://careers.withwaymo.com/jobs?gh_jid=8234553#",
            ),
            (
                "8234553",
                "https://careers.withwaymo.com/jobs?gh_jid=8234553&gh_jid=8234553",
            ),
            ("8234553", "https://careers.withwaymo.com/jobs?gh_jid="),
            ("abc", "https://careers.withwaymo.com/jobs?gh_jid=abc"),
            (
                " 8234553 ",
                "https://careers.withwaymo.com/jobs?gh_jid=8234553",
            ),
            ("8234553", "http://careers.withwaymo.com/jobs?gh_jid=8234553"),
            (
                "8234553",
                "https://careers.withwaymo.com:443/jobs?gh_jid=8234553",
            ),
        )
        for job_id, url in invalid_rows:
            with self.subTest(job_id=job_id, url=url):
                session = StreamingSession(
                    [StreamingResponse({"jobs": [listing(job_id, url=url)]})]
                )
                with self.assertRaisesRegex(AdapterError, "exact board and stable ID"):
                    greenhouse.fetch_jobs(session, self.target)

        session = StreamingSession(
            [StreamingResponse({"jobs": [listing("", url=valid_url)]})]
        )
        with self.assertRaisesRegex(AdapterError, "missing a stable ID"):
            greenhouse.fetch_jobs(session, self.target)

    def test_malformed_custom_contract_configuration_fails_before_request(self):
        invalid_overrides = (
            {
                "apply_url_template": (
                    "https://evil.example/jobs?gh_jid={id}"
                )
            },
            {
                "apply_url_template": (
                    "https://careers.withwaymo.com/jobs?gh_jid={id}&other={id}"
                )
            },
            {
                "apply_url_template": (
                    "https://careers.withwaymo.com/jobs?gh_jid={id}#fragment"
                )
            },
            {"endpoint_variant": None},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                target = type(self.target)(
                    **{**self.target.__dict__, **overrides}
                )
                session = StreamingSession([])
                with self.assertRaisesRegex(AdapterError, "apply_url_template"):
                    greenhouse.fetch_jobs(session, target)
                self.assertEqual(session.get_calls, [])

    def test_custom_contract_does_not_change_collection_lifecycle(self):
        strict_target = type(self.target)(
            **{
                **self.target.__dict__,
                "allowed_apply_hosts": ["job-boards.greenhouse.io"],
                "apply_url_template": None,
            }
        )
        targets_and_urls = (
            (
                strict_target,
                "https://job-boards.greenhouse.io/waymo/jobs/8234553",
            ),
            (
                self.target,
                "https://careers.withwaymo.com/jobs?gh_jid=8234553",
            ),
        )
        snapshots = []
        request_urls = []
        for target, url in targets_and_urls:
            session = StreamingSession(
                [
                    StreamingResponse(
                        {"jobs": [listing("8234553", url=url)]}
                    ),
                    StreamingResponse(detail("8234553")),
                ]
            )
            jobs = greenhouse.fetch_jobs(session, target)
            metadata = source_scan_metadata(session)
            snapshots.append(
                {
                    key: metadata.get(key)
                    for key in (
                        "pages_requested",
                        "pages_completed",
                        "scan_complete",
                        "scan_completeness_reason",
                        "total_available",
                        "stop_reason",
                        "safety_limit_reached",
                        "collection_changed_during_scan",
                    )
                }
            )
            snapshots[-1]["pagination"] = {
                key: value
                for key, value in session.greenhouse_pagination.items()
                if key not in {"listing_bytes", "detail_bytes"}
            }
            snapshots[-1]["jobs_collected"] = len(jobs)
            request_urls.append([call[0] for call in session.get_calls])

        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(request_urls[0], request_urls[1])

    def test_clinch_style_waymo_url_fails_closed_with_bounded_canary_evidence(self):
        returned_url = (
            "https://careers.withwaymo.com/jobs/"
            "software-engineer-mountain-view-california-united-states"
        )
        session = StreamingSession(
            [StreamingResponse({"jobs": [listing("101", url=returned_url)]})]
        )

        with patch.object(canaries, "BoundedSession", return_value=session):
            row = canaries.run_canary(self.target, 21)

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["canary_outcome"], "FAIL_CONTRACT")
        self.assertEqual(row["source_health_code"], "identity_failure")
        self.assertEqual(row["source_identity_status"], "failed")
        self.assertEqual(row["requests_started"], 1)
        self.assertIn("exact board and stable ID", row["error"])
        self.assertEqual(
            row["greenhouse_url_identity_failure"],
            {
                "offending_job_id": "101",
                "offending_returned_job_url": returned_url,
                "expected_board_token": "waymo",
                "expected_hostnames": ["careers.withwaymo.com"],
                "expected_path": "/jobs",
                "expected_url_patterns": [
                    "https://careers.withwaymo.com/jobs?gh_jid=101"
                ],
                "actual_scheme": "https",
                "actual_hostname": "careers.withwaymo.com",
                "actual_path": (
                    "/jobs/software-engineer-mountain-view-california-united-states"
                ),
                "actual_query_present": False,
                "actual_query_parameter_names": [],
                "actual_fragment_present": False,
                "board_token_path_segment_present": False,
                "stable_job_id_path_segment_present": False,
                "stable_job_id_query_parameters": [],
                "violations": [
                    "path_does_not_match_board_and_stable_id",
                    "query_does_not_match_stable_id",
                ],
                "expected_stable_id_query_parameter": "gh_jid",
            },
        )

    def test_capped_inventory_is_incomplete_and_never_authoritative_for_absence(self):
        target = type(self.target)(**{**self.target.__dict__, "max_jobs": 2})
        session = StreamingSession(
            [
                StreamingResponse(
                    {"jobs": [listing("101"), listing("202"), listing("303")]}
                ),
                StreamingResponse(detail("101")),
                StreamingResponse(detail("202")),
            ]
        )

        jobs = greenhouse.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        finalize_source_scan(
            metadata,
            jobs_collected=len(jobs),
            activation_state=target.source_activation,
        )
        result = CompanyScanResult(
            target=target,
            jobs=jobs,
            status=CoverageStatus.FETCHED,
            scan_complete=metadata["scan_complete"],
            scan_state=metadata["scan_state"],
            source_health_code=metadata["source_health_code"],
            source_activation_state=target.source_activation,
            jobs_collected=len(jobs),
        )

        self.assertEqual(len(jobs), 2)
        self.assertTrue(metadata["safety_limit_reached"])
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_state"], "incomplete")
        self.assertFalse(result.authoritative_for_absence)


if __name__ == "__main__":
    unittest.main()
