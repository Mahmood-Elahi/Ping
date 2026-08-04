"""Unit tests for official-source platform fingerprinting (no live HTTP)."""

from __future__ import annotations

import unittest
import csv
from pathlib import Path

import yaml

from scripts.fingerprint_company_platforms import (
    FINGERPRINT_FIELDS,
    FetchEvidence,
    adapter_priority_score,
    build_fingerprint,
    detect_platform,
    merge_fingerprint_batch,
)


def source(company: str = "Acme", url: str = "https://careers.acme.example/jobs") -> dict[str, str]:
    return {
        "Company": company,
        "Official Careers URL": url,
        "Official Job Search URL": url,
        "Research Status": "manual_review",
    }


def evidence(url: str, body: str = "<html><body>Careers</body></html>", status: int = 200) -> FetchEvidence:
    return FetchEvidence(url, url, status, "text/html", body, f"HTTP {status}", {})


class FakeClient:
    def __init__(self, supplied: FetchEvidence | None = None) -> None:
        self.supplied = supplied
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchEvidence:
        self.calls.append(url)
        return self.supplied or evidence(url)


class PlatformDetectionTests(unittest.TestCase):
    def test_oracle_recruiting_detection(self):
        item = detect_platform(evidence(
            "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions"
        ))
        self.assertEqual(item.family, "oracle_recruiting")
        self.assertIn("recruitingCEJobRequisitions", item.endpoint_url)
        self.assertEqual(item.stable_job_id, "true")

    def test_sap_successfactors_detection(self):
        item = detect_platform(evidence(
            "https://jobs.acme.example/",
            '<script src="https://career5.successfactors.eu/career?company=acme"></script>',
        ))
        self.assertEqual(item.family, "sap_successfactors")
        self.assertEqual(item.vendor, "SAP SuccessFactors")

    def test_icims_detection(self):
        item = detect_platform(evidence(
            "https://careers.acme.example/jobs",
            '<iframe src="https://careers-acme.icims.com/jobs/search"></iframe>',
        ))
        self.assertEqual(item.family, "icims")

    def test_smartrecruiters_detection(self):
        item = detect_platform(evidence("https://jobs.smartrecruiters.com/Acme"))
        self.assertEqual(item.family, "smartrecruiters")
        self.assertEqual(item.endpoint_url, "https://api.smartrecruiters.com/v1/companies/Acme/postings")

    def test_phenom_detection(self):
        item = detect_platform(evidence(
            "https://careers.acme.example/search-results",
            '<script src="https://cdn.phenompeople.com/acme.js"></script>',
        ))
        self.assertEqual(item.family, "phenom")

    def test_eightfold_detection(self):
        item = detect_platform(evidence("https://acme.eightfold.ai/careers"))
        self.assertEqual(item.family, "eightfold")

    def test_workday_url_missed_by_earlier_research(self):
        item = detect_platform(evidence(
            "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers"
        ))
        self.assertEqual(item.family, "workday")
        self.assertIn("/wday/cxs/medtronic/MedtronicCareers/jobs", item.endpoint_url)

    def test_static_html_detection(self):
        item = detect_platform(evidence(
            "https://acme.example/careers",
            '<html><a href="/jobs/engineer-123">Engineer</a><a href="/jobs/designer-456">Designer</a></html>',
        ))
        self.assertEqual(item.family, "static_html")
        self.assertEqual(item.stable_job_id, "true")
        self.assertEqual(item.apply_url, "true")

    def test_static_html_is_not_inferred_from_error_page_links(self):
        item = detect_platform(evidence(
            "https://acme.example/missing",
            '<html><a href="/jobs/one">One</a><a href="/jobs/two">Two</a></html>',
            404,
        ))
        self.assertEqual(item.family, "unknown")

    def test_javascript_embedded_data_detection(self):
        item = detect_platform(evidence(
            "https://acme.example/careers",
            '<script id="__NEXT_DATA__" type="application/json">{"jobs":[{"jobId":"J-42","applyUrl":"https://acme.example/apply/J-42"}]}</script>',
        ))
        self.assertEqual(item.family, "javascript_embedded_data")
        self.assertEqual(item.stable_job_id, "true")
        self.assertEqual(item.apply_url, "true")

    def test_parent_company_source_classification(self):
        row = build_fingerprint(
            source("Uber Eats", "https://www.uber.com/us/en/careers/list/?team=UberEats"),
            evidence("https://www.uber.com/us/en/careers/list/?team=UberEats"),
        )
        self.assertEqual(row["Detected Platform Family"], "parent_company_source")
        self.assertEqual(row["Parent Company Source"], "Uber")
        self.assertIn("team=UberEats", row["Required Parameters"])

    def test_stable_job_id_evidence_is_recorded(self):
        row = build_fingerprint(
            source("Acme", "https://acme.wd5.myworkdayjobs.com/External"),
            evidence("https://acme.wd5.myworkdayjobs.com/External"),
        )
        self.assertEqual(row["Stable Job ID Available"], "true")
        self.assertIn("site=External", row["Notes"])

    def test_anti_bot_classification(self):
        item = detect_platform(evidence(
            "https://careers.acme.example/jobs",
            "<html><title>Attention Required! | Cloudflare</title><div class='cf-chl-widget'></div></html>",
            403,
        ))
        self.assertEqual(item.family, "browser_required")
        self.assertEqual(item.anti_bot, "true")


class FingerprintBatchTests(unittest.TestCase):
    def test_reviewed_fingerprint_is_preserved(self):
        existing = {field: "" for field in FINGERPRINT_FIELDS}
        existing.update({
            "Company": "Acme",
            "Detected Platform Family": "icims",
            "Confidence": "high",
            "Reviewed": "true",
            "Notes": "human-reviewed evidence",
        })
        client = FakeClient()
        rows, decisions = merge_fingerprint_batch([source()], [existing], client)
        self.assertEqual(rows[0]["Notes"], "human-reviewed evidence")
        self.assertEqual(decisions[0]["decision"], "preserved_reviewed")
        self.assertEqual(client.calls, [])

    def test_batch_size_is_capped_at_25(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 25"):
            merge_fingerprint_batch([source()], [], FakeClient(), batch_size=26)

    def test_resume_skips_completed_company(self):
        existing = {field: "" for field in FINGERPRINT_FIELDS}
        existing.update({
            "Company": "Acme",
            "Detected Platform Family": "unknown",
            "Confidence": "low",
            "Reviewed": "false",
        })
        client = FakeClient()
        rows, decisions = merge_fingerprint_batch([source()], [existing], client, resume=True)
        self.assertEqual(rows[0]["Detected Platform Family"], "unknown")
        self.assertEqual(decisions[0]["decision"], "resume_skipped_completed")
        self.assertEqual(client.calls, [])

    def test_adapter_priority_scoring_rewards_reusable_endpoint(self):
        strong = {
            "Confidence": "high",
            "API or Endpoint URL": "https://api.example/jobs",
            "Endpoint Method": "GET",
            "Pagination Method": "offset + limit",
            "Stable Job ID Available": "true",
            "Apply URL Available": "true",
            "Anti-Bot Observed": "false",
            "JavaScript Required": "false",
            "Official Job Search URL": "https://example/jobs",
            "Detected Platform Family": "smartrecruiters",
        }
        weak = dict(strong)
        weak.update({
            "Confidence": "low",
            "API or Endpoint URL": "",
            "Endpoint Method": "browser",
            "Pagination Method": "unknown",
            "Stable Job ID Available": "unknown",
            "Apply URL Available": "unknown",
            "Anti-Bot Observed": "true",
            "JavaScript Required": "true",
            "Detected Platform Family": "browser_required",
        })
        strong_score, _ = adapter_priority_score([strong, strong, strong])
        weak_score, _ = adapter_priority_score([weak])
        self.assertGreater(strong_score, weak_score)
        self.assertLessEqual(strong_score, 100)

    def test_live_registry_remains_untouched_and_has_45_entries(self):
        path = Path("config/target_companies.yaml")
        before = path.read_bytes()
        data = yaml.safe_load(before.decode("utf-8"))
        entries = data.get("target_companies") or data.get("companies")
        self.assertEqual(len(entries), 45)
        detect_platform(evidence("https://jobs.smartrecruiters.com/Acme"))
        self.assertEqual(path.read_bytes(), before)

    def test_generated_artifact_covers_every_remaining_company(self):
        research_path = Path("config/company_watchlist_research.csv")
        fingerprint_path = Path("config/company_platform_fingerprints.csv")
        if not fingerprint_path.exists():
            self.skipTest("generated fingerprint artifact is not present")
        with research_path.open("r", encoding="utf-8-sig", newline="") as file:
            research = list(csv.DictReader(file))
        with fingerprint_path.open("r", encoding="utf-8-sig", newline="") as file:
            fingerprints = list(csv.DictReader(file))
        remaining = {
            row["Company"] for row in research
            if row["Research Status"] not in {"existing_config", "validated_supported"}
        }
        fingerprinted = {row["Company"] for row in fingerprints}
        self.assertEqual(fingerprinted, remaining)
        self.assertEqual(len(fingerprints), 208)
        self.assertTrue(all(row["Detected Platform Family"] for row in fingerprints))


if __name__ == "__main__":
    unittest.main()
