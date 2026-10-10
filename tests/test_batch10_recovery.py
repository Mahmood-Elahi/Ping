from __future__ import annotations

import json
import unittest

from src.scraper.company_careers.adapters import dayforce, jobvite
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies


class Response:
    def __init__(
        self,
        body,
        *,
        url: str = "",
        content_type: str = "application/json",
        status_code: int = 200,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": content_type}
        self.payload = body if isinstance(body, (dict, list)) else None
        self.text = json.dumps(body) if self.payload is not None else str(body)
        self.content = self.text.encode("utf-8")

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class Session:
    def __init__(self, gets, posts=()) -> None:
        self.gets = list(gets)
        self.posts = list(posts)
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if not self.gets:
            raise AssertionError(f"unexpected GET {url}")
        return self.gets.pop(0)

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if not self.posts:
            raise AssertionError(f"unexpected POST {url}")
        return self.posts.pop(0)


def jobvite_identity(*, tenant: str = "cameco", company: str = "Cameco") -> Response:
    return Response(
        f"<html><body>{company} Careers "
        f"<a href='https://jobs.jobvite.com/{tenant}/jobs'>Open jobs</a>"
        "</body></html>",
        url="https://www.cameco.com/careers",
        content_type="text/html",
    )


def dayforce_shell() -> Response:
    state = {
        "query": {
            "clientNamespace": "chemelex",
            "careerSiteXRefCode": "CANDIDATEPORTAL",
        },
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["site-info", {"clientNamespace": "chemelex"}],
                            "state": {
                                "data": {
                                    "clientNamespace": "chemelex",
                                    "jobBoardCode": "candidateportal",
                                    "cultureCode": "en-US",
                                    "jobBoardId": 71,
                                }
                            },
                        }
                    ]
                }
            }
        },
    }
    return Response(
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(state)
        + "</script></html>",
        content_type="text/html",
    )


def dayforce_row(index: int) -> dict:
    return {
        "clientNamespace": "chemelex",
        "jobBoardId": 71,
        "jobPostingId": 10_000 + index,
        "jobReqId": 20_000 + index,
        "jobTitle": f"Engineer {index}",
        "jobDescription": "Build reliable systems.",
        "hasVirtualLocation": False,
        "postingStartTimestampUTC": "2026-10-01T00:00:00+00:00",
        "postingExpiryTimestampUTC": "2026-12-01T00:00:00+00:00",
        "postingLocations": [
            {
                "formattedAddress": "Redwood City, CA, United States",
                "cityName": "Redwood City",
                "stateCode": "CA",
                "isoCountryCode": "US",
            }
        ],
    }


def dayforce_page(start: int, count: int, *, total: int = 71) -> Response:
    rows = [dayforce_row(index) for index in range(start, start + count)]
    return Response(
        {
            "jobPostings": rows,
            "maxCount": total,
            "offset": start - 1,
            "count": len(rows),
        }
    )


def dayforce_page_rows(rows: list[dict], *, total: int, offset: int) -> Response:
    return Response(
        {
            "jobPostings": rows,
            "maxCount": total,
            "offset": offset,
            "count": len(rows),
        }
    )


def dayforce_detail(row: dict) -> Response:
    state = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"state": {"data": row}}]
                }
            }
        }
    }
    return Response(
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(state)
        + "</script></html>",
        content_type="text/html",
    )


class Batch10RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
        }

    def test_cameco_unsupported_jobvite_migration_is_reverted(self) -> None:
        target = self.targets["Cameco"]
        self.assertEqual(target.platform, "static_html")
        self.assertEqual(target.endpoint_variant, "server_rendered_cards")
        self.assertEqual(target.listing_url, "https://jobs.jobvite.com/cameco/jobs")
        self.assertEqual((target.max_pages, target.max_jobs), (1, 20))

    def test_cameco_jobvite_candidate_still_requires_official_tenant_link(self) -> None:
        target = CompanyTarget(
            name="Cameco",
            platform="jobvite",
            careers_url="https://www.cameco.com/careers",
            slug="cameco",
            listing_url="https://jobs.jobvite.com/cameco/search",
            endpoint_variant=jobvite.ENDPOINT_VARIANT,
            identity_regex=r"\bCameco\b",
            allowed_apply_hosts=["jobs.jobvite.com"],
            max_pages=1,
            max_jobs=500,
        )
        session = Session([jobvite_identity(tenant="other")])
        with self.assertRaisesRegex(AdapterError, "configured tenant"):
            jobvite.fetch_jobs(session, target)
        self.assertEqual(len(session.calls), 1)

    def test_chemelex_budget_enumerates_reported_71_job_collection(self) -> None:
        target = self.targets["Chemelex"]
        self.assertEqual((target.max_pages, target.max_jobs), (4, 100))
        dayforce.validate_target(target)
        session = Session(
            [dayforce_shell(), Response({"csrfToken": "a" * 64})],
            [
                dayforce_page(1, 25),
                dayforce_page(26, 25),
                dayforce_page(51, 21),
            ],
        )

        jobs = dayforce.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 71)
        self.assertEqual(metadata["total_available"], 71)
        self.assertEqual(metadata["stop_reason"], "identity_unproven")
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(metadata["source_identity_status"], "unproven")
        self.assertEqual(
            [call[2]["json"]["paginationStart"] for call in session.calls if call[0] == "POST"],
            [0, 25, 50],
        )

    def test_chemelex_second_total_mutation_remains_incomplete(self) -> None:
        target = self.targets["Chemelex"]
        session = Session(
            [
                dayforce_shell(),
                Response({"csrfToken": "a" * 64}),
                dayforce_shell(),
                Response({"csrfToken": "b" * 64}),
            ],
            [
                dayforce_page(1, 25),
                dayforce_page(26, 25, total=72),
                dayforce_page(1, 25),
                dayforce_page(26, 25, total=72),
            ],
        )
        result = dayforce.fetch_jobs(session, target)
        self.assertFalse(result.scan["scan_complete"])
        self.assertTrue(result.scan["collection_retry_exhausted"])
        self.assertEqual(
            result.scan["stop_reason"], "collection_changed_during_scan"
        )

    def test_chemelex_68_rows_reconcile_to_66_jobs_duplicate_and_rejection(self) -> None:
        target = self.targets["Chemelex"]
        rows = [dayforce_row(index) for index in range(1, 69)]
        rows[66]["jobReqId"] = rows[65]["jobReqId"]
        rows[67]["postingLocations"] = None
        session = Session(
            [
                dayforce_shell(),
                Response({"csrfToken": "a" * 64}),
                dayforce_detail(rows[67]),
            ],
            [
                dayforce_page_rows(rows[:25], total=68, offset=0),
                dayforce_page_rows(rows[25:50], total=68, offset=25),
                dayforce_page_rows(rows[50:], total=68, offset=50),
            ],
        )

        jobs = dayforce.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), 66)
        self.assertEqual(metadata["total_available"], 68)
        self.assertEqual(metadata["duplicate_requisitions_skipped"], 1)
        self.assertEqual(metadata["duplicate_posting_ids_skipped"], [10067])
        self.assertEqual(metadata["duplicate_requisition_ids"], [20066])
        self.assertEqual(metadata["rejected_record_count"], 1)
        self.assertEqual(
            metadata["rejected_record_reason"], "missing_structured_location"
        )
        self.assertEqual(
            metadata["rejected_record_ids"],
            [{"posting_id": 10068, "requisition_id": 20068}],
        )
        self.assertEqual(metadata["stop_reason"], "identity_unproven")
        self.assertFalse(metadata["scan_complete"])
        self.assertEqual(
            metadata["scan_completeness_reason"], "identity_unproven"
        )
        self.assertEqual(metadata["raw_source_postings"], 68)
        self.assertEqual(metadata["terminal_accounted_items"], 68)
        self.assertEqual(len(jobs.inventory_items), 67)
        self.assertEqual(len(jobs.item_failures), 1)
        self.assertEqual(jobs.item_failures[0].outcome, "rejected")


if __name__ == "__main__":
    unittest.main()
