from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import validate_canada_priority_source_recovery_batch2 as validator
from scripts.validate_company_registry import source_key
from src.scraper.company_careers.adapters import icims
from src.scraper.company_careers.models import AdapterError, CompanyTarget, source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(
        self,
        payload=None,
        *,
        status: int = 200,
        content_type: str = "application/json",
        text: str | None = None,
        url: str = "https://careers.amd.com/api/jobs",
    ) -> None:
        self.payload = payload
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.text = json.dumps(payload) if text is None else text
        self.url = url

    def json(self):  # type: ignore[no-untyped-def]
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses) -> None:  # type: ignore[no-untyped-def]
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def amd_target(**overrides) -> CompanyTarget:  # type: ignore[no-untyped-def]
    values = dict(
        name="AMD",
        platform="icims",
        tenant="amd",
        board_id="amd-public",
        endpoint_url="https://careers.amd.com/api/jobs",
        search_url="https://careers.amd.com/careers-home/jobs",
        endpoint_variant=icims.JIBE_API,
        allowed_apply_hosts=[
            "careers-amd.icims.com",
            "canadacareers-amd.icims.com",
            "global-external-amd.icims.com",
        ],
        max_pages=2,
        max_jobs=20,
        page_size=10,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def amd_item(
    job_id: str,
    *,
    host: str = "canadacareers-amd.icims.com",
    client_code: str = "amd",
) -> dict:
    return {
        "data": {
            "slug": job_id,
            "req_id": job_id,
            "title": f"Engineer {job_id}",
            "client_code": client_code,
            "apply_url": f"https://{host}/jobs/{job_id}/login",
            "full_location": "Markham, Ontario, Canada",
            "posted_date": "2026-09-01",
        }
    }


def jibe_page(*items, total: int) -> Response:  # type: ignore[no-untyped-def]
    return Response({"jobs": list(items), "totalCount": total, "count": total})


def legacy_target() -> CompanyTarget:
    return CompanyTarget(
        name="Kinaxis",
        platform="icims",
        tenant="kinaxis",
        board_id="kinaxis-public",
        search_url="https://careers-kinaxis.icims.com/jobs/search",
        endpoint_variant=icims.LEGACY_HTML,
        allowed_apply_hosts=["careers-kinaxis.icims.com"],
        max_pages=1,
        max_jobs=20,
        page_size=50,
    )


def legacy_page(*, job: bool, active_next: bool, disabled_next: bool = False) -> str:
    card = ""
    if job:
        card = """
        <li class="iCIMS_JobCardItem">
          <div class="header left"><span>CA-ON-Ottawa</span></div>
          <div class="title"><a href="https://careers-kinaxis.icims.com/jobs/101/engineer/job">
            <h3>Engineer</h3></a></div>
        </li>
        """
    empty = "" if job else '<div class="iCIMS_JobSearchNoResults">No jobs found</div>'
    next_control = '<a rel="next" href="?pr=2">Next</a>' if active_next else ""
    if disabled_next:
        next_control = '<a class="next disabled" aria-disabled="true">Next</a>'
    return f"<html><body><ul>{card}</ul>{empty}{next_control}</body></html>"


class CanadaPrioritySourceRecoveryBatch2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    def test_four_effective_sources_are_active_with_exact_contracts(self):
        expected = {
            "AMD": (
                "icims:careers.amd.com:amd-public",
                icims.JIBE_API,
                "amd",
                2,
                20,
                10,
            ),
            "Kinaxis": (
                "icims:careers-kinaxis.icims.com:kinaxis-public",
                icims.LEGACY_HTML,
                "kinaxis",
                1,
                20,
                50,
            ),
            "SECURE Energy Services": (
                "icims:careers-canada-secure.icims.com:secure-canada",
                icims.LEGACY_HTML,
                "secure",
                1,
                20,
                20,
            ),
            "Hexagon Autonomy & Positioning / NovAtel": (
                "icims:careers-hexagonpositioning.icims.com:hexagonpositioning-public",
                icims.LEGACY_HTML,
                "hexagonpositioning",
                1,
                20,
                50,
            ),
        }
        for company, contract in expected.items():
            with self.subTest(company=company):
                target = self.targets[company]
                entry = vars(target)
                self.assertEqual(target.platform, "icims")
                self.assertEqual(
                    (
                        source_key(entry),
                        target.endpoint_variant,
                        target.tenant,
                        target.max_pages,
                        target.max_jobs,
                        target.page_size,
                    ),
                    contract,
                )
                icims.validate_target(target)

    @patch.object(icims.time, "sleep", return_value=None)
    def test_retained_legacy_empty_page_with_active_next_stays_incomplete(self, _sleep):
        session = Session(
            [
                Response(
                    text=legacy_page(job=True, active_next=True),
                    content_type="text/html",
                    url="https://careers-kinaxis.icims.com/jobs/search?pr=0",
                ),
                Response(
                    text=legacy_page(job=False, active_next=True),
                    content_type="text/html",
                    url="https://careers-kinaxis.icims.com/jobs/search?pr=1",
                ),
            ]
        )
        jobs = icims.fetch_jobs(session, legacy_target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(metadata["scan_complete"])
        self.assertTrue(metadata["has_more"])
        self.assertEqual(metadata["stop_reason"], "empty_page_without_terminal_contract")

    @patch.object(icims.time, "sleep", return_value=None)
    def test_legacy_empty_page_requires_explicit_no_continuation(self, _sleep):
        session = Session(
            [
                Response(
                    text=legacy_page(job=True, active_next=True),
                    content_type="text/html",
                    url="https://careers-kinaxis.icims.com/jobs/search?pr=0",
                ),
                Response(
                    text=legacy_page(
                        job=False, active_next=False, disabled_next=True
                    ),
                    content_type="text/html",
                    url="https://careers-kinaxis.icims.com/jobs/search?pr=1",
                ),
            ]
        )
        jobs = icims.fetch_jobs(session, legacy_target())
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["has_more"])
        self.assertEqual(
            metadata["scan_completeness_reason"], "explicit_next_page_exhausted"
        )

    def test_unproven_first_page_empty_still_fails(self):
        response = Response(
            text="<html><body><div class='iCIMS_MainWrapper'></div></body></html>",
            content_type="text/html",
            url="https://careers-kinaxis.icims.com/jobs/search?pr=0",
        )
        with self.assertRaisesRegex(AdapterError, "did not prove an empty"):
            icims.fetch_jobs(Session([response]), legacy_target())

    def test_amd_row_shape_preserves_stable_identity_and_source(self):
        session = Session([jibe_page(amd_item("91638"), total=1)])
        job = icims.fetch_jobs(session, amd_target())[0]
        self.assertEqual(job.job_id, "icims:amd:91638")
        self.assertEqual(job.raw_metadata["external_job_id"], "91638")
        self.assertEqual(job.company, "AMD")
        self.assertEqual(job.source, "icims:careers.amd.com:amd-public")
        self.assertEqual(url_host(job.url), "canadacareers-amd.icims.com")

    def test_valid_amd_sibling_survives_bounded_malformed_row(self):
        session = Session(
            [
                jibe_page(
                    amd_item("91638"),
                    amd_item("99999", host="evil.example"),
                    total=2,
                )
            ]
        )
        jobs = icims.fetch_jobs(session, amd_target())
        metadata = source_scan_metadata(session)
        self.assertEqual([job.job_id for job in jobs], ["icims:amd:91638"])
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertEqual(metadata["rejected_record_ids"], ["99999"])
        self.assertIn("official allowed-host", metadata["rejected_record_reason"])
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["scan_completeness_reason"], "record_validation_failure")

    @patch.object(icims.time, "sleep", return_value=None)
    def test_later_all_rejected_amd_page_retains_prior_valid_jobs(self, _sleep):
        session = Session(
            [
                jibe_page(amd_item("1"), amd_item("2"), total=4),
                jibe_page(
                    amd_item("3", host="evil.example"),
                    amd_item("4", client_code="other-company"),
                    total=4,
                ),
            ]
        )
        jobs = icims.fetch_jobs(session, amd_target(page_size=2))
        metadata = source_scan_metadata(session)
        self.assertEqual([job.job_id for job in jobs], ["icims:amd:1", "icims:amd:2"])
        self.assertEqual(metadata["rejected_record_count"], 2)
        self.assertEqual(metadata["stop_reason"], "all_records_rejected")
        self.assertFalse(metadata["scan_complete"])

    def test_all_rejected_amd_inventory_still_fails(self):
        session = Session(
            [jibe_page(amd_item("1", host="evil.example"), total=1)]
        )
        with self.assertRaisesRegex(AdapterError, "none were valid"):
            icims.fetch_jobs(session, amd_target())
        metadata = source_scan_metadata(session)
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertFalse(metadata["scan_complete"])

    def test_amd_tenant_mismatch_is_rejected(self):
        session = Session(
            [jibe_page(amd_item("1", client_code="other-company"), total=1)]
        )
        with self.assertRaisesRegex(AdapterError, "none were valid"):
            icims.fetch_jobs(session, amd_target())
        self.assertIn(
            "client_code does not match",
            source_scan_metadata(session)["rejected_record_reason"],
        )

    def test_amd_rejection_diagnostics_are_bounded(self):
        bad = [amd_item(str(index), host="evil.example") for index in range(6)]
        session = Session([jibe_page(amd_item("100"), *bad, total=7)])
        jobs = icims.fetch_jobs(session, amd_target())
        metadata = source_scan_metadata(session)
        self.assertEqual([job.job_id for job in jobs], ["icims:amd:100"])
        self.assertEqual(metadata["rejected_record_count"], 6)
        self.assertEqual(len(metadata["rejected_record_ids"]), 5)
        self.assertEqual(len(metadata["rejected_record_diagnostics"]), 5)
        self.assertFalse(metadata["scan_complete"])

    def test_validator_scope_and_request_bound(self):
        self.assertEqual(
            validator.COMPANIES,
            (
                "Hexagon Autonomy & Positioning / NovAtel",
                "Kinaxis",
                "SECURE Energy Services",
                "AMD",
            ),
        )
        session = validator.BoundedSession()
        session.request_count = validator.MAX_REQUESTS_PER_SOURCE
        with self.assertRaisesRegex(AdapterError, "request bound"):
            session.request("GET", "https://example.test")
        session.close()


def url_host(value: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(value).hostname or "").casefold()


if __name__ == "__main__":
    unittest.main()
