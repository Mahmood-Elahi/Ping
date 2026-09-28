from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.scraper.company_careers.adapters import embedded_data, lever
from src.scraper.company_careers.models import source_scan_metadata
from src.scraper.company_careers.registry import load_target_companies


TARGET_PLATFORMS = {
    "Apple": "apple",
    "Amazon": "amazon",
    "Bank of America": "avature",
    "Lenovo": "avature",
    "TSMC": "avature",
    "Google": "google",
    "Meta": "meta",
    "Continental": "javascript_embedded_data",
    "Veeva Systems": "lever",
    "Berkshire Grey": "bamboohr",
    "Diodes Incorporated": "adp",
    "Intuitive Machines": "adp",
}


class Response:
    def __init__(self, payload: object, *, content_type: str = "application/json"):
        self.payload = payload
        self.status_code = 200
        self.headers = {"content-type": content_type}
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")
        self.url = "https://jobs.example.test"
        self.history = []

    def json(self) -> object:
        return self.payload


class Session:
    def __init__(self, *responses: Response):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append((url, dict(kwargs)))
        if not self.responses:
            raise AssertionError(f"Unexpected GET {url}")
        response = self.responses.pop(0)
        response.url = url
        return response


class TwelveSourceRecoveryBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = {
            target.name: target
            for target in load_target_companies(apply_reliability_repairs=True)
            if target.name in TARGET_PLATFORMS
        }

    def test_all_requested_sources_resolve_to_the_reviewed_current_configuration(self):
        self.assertEqual(set(self.targets), set(TARGET_PLATFORMS))
        self.assertEqual(
            {name: target.platform for name, target in self.targets.items()},
            TARGET_PLATFORMS,
        )
        self.assertEqual(self.targets["Continental"].endpoint_variant, "public_json_endpoint")
        self.assertEqual(self.targets["Veeva Systems"].endpoint_variant, "postings_api_v0")
        self.assertEqual(
            self.targets["Diodes Incorporated"].endpoint_variant,
            "workforce_now_requisitions_v1",
        )
        self.assertEqual(
            self.targets["Intuitive Machines"].endpoint_variant,
            "workforce_now_requisitions_v1",
        )

    @patch.object(embedded_data.time, "sleep", return_value=None)
    def test_continental_reaches_explicit_final_page_beyond_saved_120_prefix(
        self, _sleep
    ):
        target = self.targets["Continental"]
        total = 125
        responses = []
        for page_number, offset in enumerate(range(0, total, 10), start=1):
            rows = [
                {
                    "uuid": f"continental-{index}",
                    "title": f"Engineer {index}",
                    "cityLabel": "Calgary",
                    "countryLabel": "Canada",
                    "jobFlexibilityLabel": "Hybrid",
                    "absoluteUrl": f"https://jobs.continental.com/en/detail/{index}",
                    "publicationDate": "2026-09-01",
                    "fieldOfWorkLabel": "Engineering",
                    "client": "continental",
                }
                for index in range(offset, min(offset + 10, total))
            ]
            responses.append(
                Response(
                    {
                        "result": {
                            "list": rows,
                            "pagination": {
                                "currentPage": page_number,
                                "nextPage": (
                                    page_number + 1 if page_number < 13 else None
                                ),
                                "totalPages": 13,
                            },
                        }
                    }
                )
            )
        session = Session(*responses)

        jobs = embedded_data.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), total)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "explicit_terminal_page")
        self.assertEqual(metadata["pages_completed"], 13)

    @patch.object(lever.time, "sleep", return_value=None)
    def test_veeva_reaches_short_terminal_page_beyond_saved_700_prefix(self, _sleep):
        target = self.targets["Veeva Systems"]
        total = 705
        responses = []
        for offset in range(0, total, lever.PAGE_SIZE):
            rows = [
                {
                    "id": f"veeva-{index}",
                    "text": f"Engineer {index}",
                    "hostedUrl": f"https://jobs.lever.co/veeva/veeva-{index}",
                    "createdAt": index,
                    "categories": {
                        "department": "Engineering",
                        "team": "Platform",
                        "location": "Remote",
                    },
                }
                for index in range(offset, min(offset + lever.PAGE_SIZE, total))
            ]
            responses.append(Response(rows))
        session = Session(*responses)

        jobs = lever.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)

        self.assertEqual(len(jobs), total)
        self.assertTrue(metadata["scan_complete"])
        self.assertFalse(metadata["safety_limit_reached"])
        self.assertEqual(metadata["stop_reason"], "natural_terminal_page")
        self.assertEqual(metadata["pages_completed"], 36)


if __name__ == "__main__":
    unittest.main()
