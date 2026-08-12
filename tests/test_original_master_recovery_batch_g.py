from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import validate_original_master_recovery_batch_g as batch
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.adapters import smartrecruiters, static_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


VALIDATED = [item["name"] for item in batch.CANDIDATE_ENTRIES]
EXCLUDED = {item["company"] for item in batch.EXCLUDED_REVIEWS}


def entries(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})["target_companies"]


class Response:
    def __init__(self, body: str, *, url: str):
        self.status_code = 200
        self.url = url
        self.content = body.encode()
        self.text = body
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.history = []


class Session:
    def __init__(self, response: Response):
        self.response = response

    def get(self, _url: str, **_kwargs: object) -> Response:
        return self.response


class OriginalMasterRecoveryBatchGArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_bytes = batch.PRODUCTION.read_bytes()
        cls.candidate_bytes = batch.CANDIDATE_REGISTRY.read_bytes()
        cls.validated_bytes = batch.VALIDATED_REGISTRY.read_bytes()
        cls.historical_bytes = cls.validated_bytes[:cls.validated_bytes.index(b"  - name: PETRONAS")]
        cls.historical = (yaml.safe_load(cls.historical_bytes) or {})["target_companies"]
        cls.production = entries(batch.PRODUCTION)
        cls.candidates = entries(batch.CANDIDATE_REGISTRY)
        cls.validated = entries(batch.VALIDATED_REGISTRY)
        cls.report = json.loads(batch.REPORT.read_text(encoding="utf-8"))
        with batch.CANDIDATE_CSV.open(newline="", encoding="utf-8") as handle:
            cls.inventory = list(csv.DictReader(handle))

    def test_01_activation_preserves_exact_257_entry_production_prefix(self):
        self.assertEqual(len(self.production), 336)
        self.assertEqual(len(load_parent_source_mappings(batch.PARENT_MAPPINGS)), 4)
        self.assertEqual(len(self.historical), 257)
        self.assertEqual(self.production[:257], self.historical)
        self.assertEqual(self.candidates[:257], self.historical)
        self.assertEqual(self.validated[:257], self.historical)
        self.assertEqual(self.production[:262], self.validated)
        self.assertEqual(self.candidate_bytes, self.validated_bytes)
        self.assertTrue(self.report["candidate_registry_has_exact_257_entry_prefix"])
        self.assertTrue(self.report["validated_registry_has_exact_257_entry_prefix"])

    def test_02_candidate_and_validated_registries_have_only_five_additions(self):
        self.assertEqual((len(self.candidates), len(self.validated)), (262, 262))
        self.assertEqual([item["name"] for item in self.candidates[257:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.validated[257:]], VALIDATED)
        self.assertEqual([item["name"] for item in self.production[257:262]], VALIDATED)
        self.assertEqual(len(VALIDATED), 5)
        self.assertTrue(EXCLUDED.isdisjoint({item["name"] for item in self.candidates}))
        production_names = [item["name"] for item in self.production]
        later_promotions = {
            "Astrobotic", "Intuitive Machines", "FANUC", "Mercedes-Benz",
        }
        self.assertTrue((EXCLUDED - later_promotions).isdisjoint(production_names))
        for name in later_promotions:
            self.assertEqual(production_names.count(name), 1)
        for name in VALIDATED:
            self.assertEqual(production_names.count(name), 1)

    def test_03_report_and_csv_cover_every_requested_company(self):
        self.assertEqual([row["company"] for row in self.inventory], batch.REVIEWED_COMPANIES)
        self.assertEqual([row["company"] for row in self.report["results"]], batch.REVIEWED_COMPANIES)
        safe = [item["company"] for item in self.report["results"] if item["safe_to_activate"]]
        excluded = {item["company"] for item in self.report["results"] if not item["safe_to_activate"]}
        self.assertEqual(safe, VALIDATED)
        self.assertEqual(excluded, EXCLUDED)
        for item in self.report["results"]:
            self.assertTrue({
                "company", "official_careers_url", "platform", "adapter", "jobs_normalized",
                "current_student_intern_hits", "canada_us_remote_locations_represented",
                "stable_id_result", "apply_url_result", "identity_ownership_location_evidence",
                "canada_us_remote_student_compatibility", "exclusion_reason",
            }.issubset(item))
            self.assertLessEqual(item["jobs_normalized"], 20)

    def test_04_repeat_runs_have_stable_ids_and_official_apply_urls(self):
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
            "PETRONAS": 20,
            "Nubank": 20,
            "Tower Research Capital": 2,
            "Mitsubishi Electric": 20,
            "Yaskawa Electric": 20,
        }
        self.assertEqual({name: by_name[name]["jobs_normalized"] for name in VALIDATED}, expected_counts)
        self.assertEqual(sum(by_name[name]["jobs_normalized"] for name in VALIDATED), 82)
        self.assertEqual(by_name["Mitsubishi Electric"]["current_student_intern_hits"], 1)
        self.assertEqual(sum(by_name[name]["current_student_intern_hits"] for name in VALIDATED), 1)
        self.assertIn("Toronto", by_name["Nubank"]["canada_us_remote_locations_represented"])
        self.assertIn("New York, NY, United States", by_name["Tower Research Capital"]["canada_us_remote_locations_represented"])
        self.assertIn("Mississauga, ON Canada", by_name["Yaskawa Electric"]["canada_us_remote_locations_represented"])
        self.assertIn("Remote", by_name["Yaskawa Electric"]["canada_us_remote_locations_represented"])

    def test_06_master_membership_and_projections(self):
        master = reconcile_master(
            load_master_workbook(batch.MASTER_WORKBOOK),
            load_target_companies(str(batch.PRODUCTION)),
            load_parent_source_mappings(batch.PARENT_MAPPINGS),
        )
        self.assertEqual(len(master.canonical_names) + len(master.mapped_names), 207)
        self.assertTrue(all(item["original_246_master"] for item in self.report["results"]))
        totals = self.report["totals"]
        self.assertEqual((totals["validated"], totals["excluded"]), (5, 10))
        self.assertEqual(totals["projected_canonical_production_count"], 262)
        self.assertEqual(totals["projected_original_master_coverage_ratio"], "193/246")
        self.assertEqual(totals["original_master_companies_recovered_names"], VALIDATED)
        self.assertEqual((totals["fresh_targets"], totals["recovery_targets"]), (0, 15))

    def test_07_platforms_and_adapters_are_exact(self):
        expected = {
            "PETRONAS": "oracle_recruiting",
            "Nubank": "ashby",
            "Tower Research Capital": "smartrecruiters",
            "Mitsubishi Electric": "static_html",
            "Yaskawa Electric": "static_html",
        }
        self.assertEqual({item["name"]: item["platform"] for item in batch.CANDIDATE_ENTRIES}, expected)
        for item in batch.CANDIDATE_ENTRIES:
            CompanyTarget.from_dict(item)

    def test_08_exclusions_respect_browser_waf_and_session_boundaries(self):
        by_name = {item["company"]: item for item in self.report["results"]}
        self.assertEqual(by_name["OHB SE"]["validation_status"], "browser_required")
        self.assertEqual(by_name["BMW"]["validation_status"], "network_inaccessible")
        for name in ("Revolut", "SoFi", "Hitachi", "Cognex"):
            self.assertEqual(by_name[name]["validation_status"], "blocked")
        for name in ("Astrobotic", "Intuitive Machines", "FANUC", "Mercedes-Benz"):
            self.assertEqual(by_name[name]["validation_status"], "unsupported_platform")
        reasons = " ".join(by_name[name]["exclusion_reason"] for name in EXCLUDED).casefold()
        self.assertIn("no security-checkpoint", reasons)
        self.assertIn("no waf", reasons)
        self.assertIn("no challenge", reasons)
        self.assertIn("no browser/session workaround", reasons)

    def test_09_adapter_opportunities_are_explicit_and_bounded(self):
        opportunities = {item["platform"]: item for item in self.report["adapter_opportunities"]}
        adp = opportunities["ADP Workforce Now / ADP Recruiting"]
        self.assertIs(adp["reusable_adapter_justified"], True)
        self.assertEqual(adp["companies"], ["Astrobotic", "Intuitive Machines", "FANUC"])
        self.assertEqual(
            opportunities["Cornerstone CSOD"]["reusable_adapter_justified"],
            "research justified; implementation not yet justified",
        )
        self.assertIs(opportunities["Beesite"]["reusable_adapter_justified"], False)

    def test_10_exact_regional_and_shared_board_attribution(self):
        by_name = {item["name"]: item for item in batch.CANDIDATE_ENTRIES}
        self.assertEqual(by_name["Tower Research Capital"]["slug"], "TowerResearchCapitalLLC")
        self.assertIn("Mitsubishi Electric US", by_name["Mitsubishi Electric"]["identity_regex"])
        self.assertIn("YAS1000YAAMI", batch.IDENTITY_LOCATION_EVIDENCE["Yaskawa Electric"])
        mappings = batch.PARENT_MAPPINGS.read_text(encoding="utf-8")
        self.assertTrue(all(name not in mappings for name in batch.REVIEWED_COMPANIES))

    def test_11_hash_ignore_and_publication_state(self):
        self.assertEqual(
            hashlib.sha256(self.historical_bytes).hexdigest().upper(),
            self.report["production_registry_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(batch.PARENT_MAPPINGS.read_bytes()).hexdigest().upper(),
            self.report["parent_mappings_sha256"],
        )
        ignored = subprocess.run(
            ["git", "check-ignore", str(batch.REPORT.relative_to(batch.ROOT))],
            cwd=batch.ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue(all(value is False for value in self.report["publication"].values()))

    def test_12_validator_imports_no_persistence_or_publisher(self):
        tree = ast.parse(Path(batch.__file__).read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        forbidden = ("supabase", "gspread", "sqlite3", "update_student_dashboard", "run_student_job_scan")
        self.assertFalse(any(any(token in module for token in forbidden) for module in modules))

    def test_13_candidate_builder_rejects_active_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory) / "production.yaml"
            destination = Path(directory) / "candidate.yaml"
            production.write_text(
                "target_companies:\n  - name: PETRONAS\n    platform: greenhouse\n    slug: petronas\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate_company"):
                batch.build_candidate_registry(production, destination, [batch.CANDIDATE_ENTRIES[0]])


class OriginalMasterRecoveryBatchGAdapterTests(unittest.TestCase):
    def test_14_static_html_supports_comma_locations_and_intermediate_nth_child(self):
        body = """<html><body><table class='jobs'><tbody class='table-data'><tr>
            <td>1</td><td><a href='/job?opportunityId=12345678-1234-1234-1234-123456789012'>Engineer</a></td>
            <td class='cities'>Mason</td><td class='state'>OH</td><td>Engineering</td>
            </tr></tbody></table></body></html>"""
        target = CompanyTarget.from_dict({
            "name": "Example", "platform": "static_html",
            "careers_url": "https://jobs.example.test/jobs",
            "listing_url": "https://jobs.example.test/jobs",
            "endpoint_variant": "server_rendered_cards", "listing_selector": "table.jobs",
            "job_card_selector": "tbody.table-data tr",
            "title_selector": "td:nth-child(2) a[href*=opportunityId=]",
            "location_selector": "td.cities, td.state",
            "department_selector": "td:nth-child(5)",
            "detail_link_selector": "td:nth-child(2) a[href*=opportunityId=]",
            "external_id_regex": r"opportunityId=([0-9a-f-]{36})",
            "allowed_apply_hosts": ["jobs.example.test"], "max_pages": 1, "max_jobs": 20,
        })
        jobs = static_html.fetch_jobs(
            Session(Response(body, url="https://jobs.example.test/jobs")), target,
        )
        self.assertEqual((jobs[0].title, jobs[0].location), ("Engineer", "Mason | OH"))
        self.assertTrue(jobs[0].job_id.endswith(":12345678-1234-1234-1234-123456789012"))

    def test_15_smartrecruiters_accepts_corroborated_legacy_numeric_ids_only(self):
        self.assertEqual(smartrecruiters._posting_id("110706947"), "110706947")
        self.assertEqual(smartrecruiters._posting_id("82885838"), "82885838")
        for invalid in ("12345", "1234567890123456789", "ABC12345"):
            with self.subTest(invalid=invalid), self.assertRaises(AdapterError):
                smartrecruiters._posting_id(invalid)


if __name__ == "__main__":
    unittest.main()
