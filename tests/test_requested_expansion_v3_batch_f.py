from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from scripts import validate_requested_expansion_v3_batch_f as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


def fake_job(job_id: str = "2", url: str = "https://jobs.example.test/job/2") -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, url=url)


class Response:
    def __init__(self, content: str, *, url: str = "https://example.test/jobs"):
        self.status_code = 200
        self.url = url
        self.content = content.encode()
        self.text = content
        self.headers = {"content-type": "text/html; charset=utf-8"}


class Session:
    def __init__(self, response: Response):
        self.response = response

    def get(self, _url: str, **_kwargs: object) -> Response:
        return self.response


class RequestedExpansionV3BatchFArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: Mistral AI")]
        cls.historical = (yaml.safe_load(cls.historical_bytes) or {})["target_companies"]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_exact_249_entry_production_prefix(self):
        self.assertEqual(len(self.production), 255)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), 249)
        self.assertEqual(self.production[:249], self.historical)
        self.assertEqual(self.candidates[:249], self.historical)
        self.assertEqual(self.validated[:249], self.historical)
        self.assertEqual(self.production, self.validated)
        self.assertEqual(self.candidate_bytes, self.validated_bytes)
        self.assertTrue(self.report["candidate_registry_has_exact_249_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_249_entry_prefix"])

    def test_02_candidate_and_validated_registries_have_only_six_additions(self):
        self.assertEqual((len(self.candidates), len(self.validated)), (255, 255))
        self.assertEqual([item["name"] for item in self.candidates[249:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[249:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[249:]], VALIDATED)
        for name in VALIDATED:
            self.assertEqual([item["name"] for item in self.production].count(name), 1)
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.production}))

    def test_03_report_and_csv_cover_every_requested_company(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        required = {
            "company", "fresh_vs_recovery_target", "official_careers_url", "platform", "adapter",
            "result", "jobs_normalized", "current_student_intern_hits",
            "canada_us_remote_locations_represented", "original_246_master", "validation_status",
            "stable_id_result", "apply_url_result", "identity_ownership_location_evidence",
            "canada_us_remote_student_compatibility", "exclusion_reason",
        }
        for item in self.report["results"]:
            self.assertTrue(required.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_repeat_runs_have_stable_ids_and_official_job_urls(self):
        for item in self.report["results"]:
            self.assertEqual(len(item["runs"]), 2)
            if item["safe_to_activate"]:
                first, second = item["runs"]
                self.assertTrue(first["success"] and second["success"])
                self.assertEqual(first["stable_ids"], second["stable_ids"])
                self.assertEqual(first["apply_urls"], second["apply_urls"])
                self.assertEqual(first["student_hits"], second["student_hits"])
                self.assertEqual(first["canadian_locations"], second["canadian_locations"])

    def test_05_live_counts_student_hits_and_compatible_locations(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        expected_counts = {
            "Mistral AI": 20, "Perplexity AI": 20, "Espressif Systems": 15,
            "iRobot": 14, "Dyson": 10, "SharkNinja": 20,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(by_name["Mistral AI"]["current_student_intern_hits"], 3)
        self.assertEqual(by_name["Perplexity AI"]["current_student_intern_hits"], 4)
        self.assertEqual(by_name["Espressif Systems"]["current_student_intern_hits"], 1)
        self.assertTrue(all(by_name[name]["current_student_intern_hits"] == 0 for name in ("iRobot", "Dyson", "SharkNinja")))
        self.assertEqual(by_name["Mistral AI"]["canada_us_remote_locations_represented"], ["New York", "Palo Alto"])
        self.assertEqual(by_name["Perplexity AI"]["canada_us_remote_locations_represented"], ["New York City", "San Francisco"])
        self.assertEqual(by_name["iRobot"]["canada_us_remote_locations_represented"], ["Bedford, MA"])
        self.assertEqual(by_name["SharkNinja"]["canada_us_remote_locations_represented"], ["Needham, MA"])

    def test_06_master_membership_and_projections(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK), load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 186)
        self.assertEqual({item["company"] for item in self.report["results"] if item["original_246_master"]}, set(batch.MASTER_RANKS))
        self.assertTrue(all(not next(item for item in self.report["results"] if item["company"] == name)["original_246_master"] for name in VALIDATED))
        totals = self.report["totals"]
        self.assertEqual((totals["validated"], totals["excluded"]), (6, 9))
        self.assertEqual(totals["projected_canonical_production_count"], 255)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "186/246")
        self.assertEqual((totals["fresh_targets"], totals["recovery_targets"]), (7, 8))

    def test_07_platforms_and_adapters_are_exact(self):
        expected = {
            "Mistral AI": "ashby", "Perplexity AI": "ashby",
            "Espressif Systems": "static_html", "iRobot": "static_html",
            "Dyson": "static_html", "SharkNinja": "static_html",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            self.assertIn(item["platform"], batch.scanner.ADAPTERS)
            CompanyTarget.from_dict(item)

    def test_08_recovery_and_anti_bot_safeguards(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        recoveries = {name for name, row in by_name.items() if row["fresh_vs_recovery_target"] == "recovery"}
        self.assertEqual(recoveries, batch.RECOVERY_TARGETS)
        self.assertTrue(all(not by_name[name]["safe_to_activate"] for name in recoveries))
        reasons = {name: by_name[name]["exclusion_reason"] for name in recoveries}
        self.assertIn("Akamai", reasons["Tesla"])
        self.assertIn("reCAPTCHA", reasons["Netflix"])
        self.assertIn("reCAPTCHA", reasons["STMicroelectronics"])
        self.assertIn("Incapsula", reasons["Baker Hughes"])
        self.assertIn("Turnstile", reasons["Hugging Face"])
        self.assertIn("SmartRecruiters", reasons["Bosch"])
        self.assertIn("custom REST", reasons["Continental"])
        self.assertIn("HawkSearch", reasons["Aptiv"])

    def test_09_identity_ownership_and_attribution_boundaries(self):
        evidence = batch.IDENTITY_LOCATION_EVIDENCE
        self.assertIn("not acquire", evidence["Covariant"])
        self.assertIn("Picea acquired 100%", evidence["iRobot"])
        self.assertIn("BoschGroup", evidence["Bosch"])
        self.assertIn("retired jobs.aptiv.com", next(item for item in batch.EXCLUDED_REVIEWS if item["company"] == "Aptiv")["exclusion_reason"])
        mappings = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        for name in batch.REVIEWED_COMPANIES:
            self.assertNotIn(name, mappings)

    def test_10_hash_ignore_and_publication_state(self):
        self.assertEqual(hashlib.sha256(self.historical_bytes).hexdigest().upper(), self.report["production_registry_sha256"])
        self.assertEqual(hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(), self.report["parent_mappings_sha256"])
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))], cwd=batch.ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_11_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))


class RequestedExpansionV3BatchFAdapterTests(unittest.TestCase):
    def test_12_static_html_supports_reviewed_reference_url_attribute(self):
        body = """<html><body><main id='jobs'><article class='card'>
            <h2>Intern</h2><span class='place'>Remote</span>
            <div class='share' data-share='/job-detail/RAK'></div>
            </article></main></body></html>"""
        target = CompanyTarget.from_dict({
            "name": "Example", "platform": "static_html", "careers_url": "https://jobs.example.test/",
            "listing_url": "https://jobs.example.test/jobs", "endpoint_variant": "server_rendered_cards",
            "listing_selector": "#jobs", "job_card_selector": ".card", "title_selector": "h2",
            "location_selector": ".place", "detail_link_selector": ".share",
            "reference_url_attribute": "data-share", "external_id_regex": r"/job-detail/([^/]+)$",
            "allowed_apply_hosts": ["jobs.example.test"], "max_pages": 1, "max_jobs": 20,
        })
        jobs = static_html.fetch_jobs(Session(Response(body, url="https://jobs.example.test/jobs")), target)
        self.assertEqual(jobs[0].job_id, "static_html:jobs.example.test:RAK")
        self.assertEqual(jobs[0].url, "https://jobs.example.test/job-detail/RAK")

    def test_13_static_html_only_upgrades_same_host_allowlisted_http_urls(self):
        body = """<html><body><main id='jobs'><article class='card'>
            <h2>Engineer</h2><a class='detail' href='http://jobs.example.test/job/JR42'>Details</a>
            </article></main></body></html>"""
        data = {
            "name": "Example", "platform": "static_html", "careers_url": "https://jobs.example.test/",
            "listing_url": "https://jobs.example.test/jobs", "endpoint_variant": "server_rendered_cards",
            "listing_selector": "#jobs", "job_card_selector": ".card", "title_selector": "h2",
            "detail_link_selector": ".detail", "external_id_regex": r"/(JR[0-9]+)$",
            "allowed_apply_hosts": ["jobs.example.test"], "max_pages": 1, "max_jobs": 20,
        }
        jobs = static_html.fetch_jobs(Session(Response(body, url=data["listing_url"])), CompanyTarget.from_dict(data))
        self.assertEqual(jobs[0].url, "https://jobs.example.test/job/JR42")
        hostile = body.replace("jobs.example.test/job", "other.example.test/job")
        with self.assertRaisesRegex(AdapterError, "allowed-host"):
            static_html.fetch_jobs(Session(Response(hostile, url=data["listing_url"])), CompanyTarget.from_dict(data))

    def test_14_batch_static_contracts_use_observed_stable_identifiers(self):
        by_name = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
        self.assertEqual(by_name["Espressif Systems"]["reference_url_attribute"], "data-share")
        self.assertIn("job-detail", by_name["Espressif Systems"]["external_id_regex"])
        self.assertIn("JR", by_name["Dyson"]["external_id_regex"])
        self.assertIn("42682", by_name["iRobot"]["external_id_regex"])
        self.assertIn("47204", by_name["SharkNinja"]["external_id_regex"])

    def test_15_contract_helpers_preserve_duplicate_and_url_checks(self):
        entry = {"allowed_apply_hosts": ["jobs.example.test"]}
        self.assertEqual(
            batch._verify_jobs([fake_job("2"), fake_job("1", "https://jobs.example.test/job/1")], entry),
            [("1", "https://jobs.example.test/job/1"), ("2", "https://jobs.example.test/job/2")],
        )
        with self.assertRaisesRegex(AdapterError, "duplicate stable IDs"):
            batch._verify_jobs([fake_job(), fake_job(url="https://jobs.example.test/job/3")], entry)
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory) / "production.yaml"; destination = Path(directory) / "candidate.yaml"
            production.write_text("target_companies:\n  - name: Existing\n    platform: greenhouse\n    slug: shared\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(production, destination, [{"name": "Existing", "platform": "greenhouse", "slug": "other"}])


if __name__ == "__main__":
    unittest.main()
