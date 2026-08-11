import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import refingerprint_uncovered_companies as mod
from src.scraper.company_careers.models import AdapterError, JobRecord


ROOT = Path(__file__).resolve().parents[1]
CALGARY_MILESTONE = (
    ROOT / "config" / "target_companies.calgary_student_employers_v1_batch_a_validated.yaml"
)
CALGARY_PROMOTIONS = [
    "Spartan Controls", "Garmin Canada", "ENMAX", "ATCO",
    "AESO (Alberta Electric System Operator)", "TransAlta", "Cenovus Energy",
    "Imperial Oil", "ARC Resources", "Nutrien", "Stantec",
    "YYC Calgary Airport Authority",
]
REQUESTED_EXPANSION_PROMOTIONS = [
    "Precision Drilling", "AltaGas", "Whitecap Resources", "Vermilion Energy",
    "SECURE Energy Services", "ExxonMobil", "SLB", "BlackBerry/QNX", "CAE Inc.",
]
REQUESTED_EXPANSION_BATCH_B_PROMOTIONS = [
    "Cohere", "Waabi", "OpenText", "Celestica", "Kinaxis",
    "Descartes Systems Group", "TELUS", "BCE / Bell", "Absolute Software",
]
CANADIAN_EXPANSION_BATCH_C_PROMOTIONS = [
    "Clio", "Benevity", "Hexagon Autonomy & Positioning / NovAtel", "Geotab",
    "Miovision", "ATS Corporation", "Linamar", "1Password", "D2L", "Hootsuite",
    "PointClickCare",
]
ADAPTER_EXPANSION_V1_BATCH_A_PROMOTIONS = [
    "CoolIT Systems", "Ballard Power Systems", "City of Calgary",
]
REQUESTED_EXPANSION_BATCH_D_PROMOTIONS = [
    "IBM", "Applied Intuition", "Apptronik", "1X Technologies", "Nuro",
    "Kodiak Robotics", "Gatik", "Physical Intelligence", "Serve Robotics",
    "Scale AI", "Renesas Electronics", "Silicon Labs", "TEKTELIC Communications",
]
REQUESTED_EXPANSION_BATCH_E_PROMOTIONS = [
    "Gibson Energy", "Strathcona Resources", "SensorUp", "Semtech",
    "Pason Systems", "Trican Well Service",
]
REQUESTED_EXPANSION_BATCH_F_PROMOTIONS = [
    "Mistral AI", "Perplexity AI", "Espressif Systems", "iRobot", "Dyson", "SharkNinja",
]
ADAPTER_EXPANSION_V1_BATCH_B_PROMOTIONS = ["Bosch", "Aptiv"]
CALGARY_CANADA_EXPANSION_V2_BATCH_A_PROMOTIONS = [
    "NOVA Chemicals", "Trans Mountain", "Computer Modelling Group (CMG)",
    "Neo Financial", "MNP", "Absorb Software", "PurposeMed",
]
ADAPTER_EXPANSION_V1_BATCH_C_PROMOTIONS = [
    "Astrobotic", "Intuitive Machines", "FANUC", "Helcim", "CPKC",
]
ONTARIO_CANADA_EXPANSION_V1_BATCH_A_PROMOTIONS = [
    "RBC", "TD Bank", "Scotiabank", "BMO", "CIBC", "Wealthsimple",
    "Thomson Reuters", "Manulife", "Sun Life", "Ciena", "Nokia Canada",
    "Ericsson Canada", "Xanadu", "ApplyBoard",
]
HISTORICAL_PRODUCTION_ONLY = [
    "Datadog", "Elastic", "GitLab", "Twilio", "Zscaler", "CrowdStrike",
    "HashiCorp", "Confluent", "Teradyne",
]


def historical_prefix_bytes(path: Path) -> bytes:
    content = path.read_bytes()
    marker = f"  - name: {CALGARY_PROMOTIONS[0]}".encode()
    return content[:content.index(marker)]


class FakeRaw:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, size: int, decode_content: bool = True) -> bytes:
        return self.body[:size]


class FakeResponse:
    def __init__(self, status=200, body=b"<html></html>", headers=None, url="https://example.com/jobs"):
        self.status_code = status
        self.raw = FakeRaw(body)
        self.headers = headers or {"Content-Type": "text/html"}
        self.encoding = "utf-8"
        self.url = url
        self.content = body

    def close(self):
        return None


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def detection(url, body="<html></html>"):
    analysis = mod.analyze_html(url, body)
    return mod.detect_contract(url, body, analysis)


def record(rank, company, classification, platform="unknown", confidence="medium"):
    return {
        "master_rank": str(rank), "company": company,
        "recoverability_class": classification,
        "detected_platform": platform,
        "recommended_family": platform,
        "platform_variant": "default", "evidence_confidence": confidence,
        "next_action": "review", "public_endpoint": "https://example.com/jobs",
        "adapter_compatibility_result": "not_applicable", "jobs_normalized": "0",
        "child_attribution_field": "", "child_attribution_values": "",
        "parent_source_identity": "", "parent_company": "",
    }


class RefingerprintUncoveredV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = mod.load_master_workbook()
        cls.targets = mod.load_target_companies(str(mod.PRODUCTION_REGISTRY))
        cls.mappings = mod.load_parent_source_mappings(mod.PARENT_MAPPINGS)
        cls.reconciliation = mod.reconcile_master(cls.master, cls.targets, cls.mappings)
        with tempfile.TemporaryDirectory() as directory:
            historical = Path(directory) / "historical.yaml"
            historical.write_bytes(historical_prefix_bytes(CALGARY_MILESTONE))
            historical_targets = mod.load_target_companies(str(historical))
        cls.historical_reconciliation = mod.reconcile_master(
            cls.master, historical_targets, cls.mappings,
        )

    def test_01_exact_246_company_master_reconciliation(self):
        self.assertEqual(self.reconciliation.total, 246)

    def test_02_canonical_covered_companies_detected(self):
        self.assertIn("NVIDIA", self.reconciliation.canonical_names)
        self.assertIn("Google", self.reconciliation.canonical_names)

    def test_03_parent_mapped_companies_detected(self):
        self.assertEqual(
            self.reconciliation.mapped_names,
            ["Google DeepMind", "Universal Robots", "WhatsApp", "Instagram"],
        )

    def test_04_canonical_and_mapped_sets_do_not_overlap(self):
        canonical = set(map(mod.normalize_company_name, self.reconciliation.canonical_names))
        mapped = set(map(mod.normalize_company_name, self.reconciliation.mapped_names))
        self.assertFalse(canonical & mapped)

    def test_05_exact_uncovered_set_is_derived_dynamically(self):
        derived = [
            row["company"] for row in self.reconciliation.rows
            if row["current_coverage_status"] == "uncovered"
        ]
        self.assertEqual(derived, self.reconciliation.uncovered_names)
        self.assertEqual(
            len(derived),
            self.reconciliation.total
            - len(self.reconciliation.canonical_names)
            - len(self.reconciliation.mapped_names),
        )

    def test_06_every_original_company_appears_exactly_once(self):
        keys = [mod.normalize_company_name(row["company"]) for row in self.reconciliation.rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_07_production_only_companies_are_separated(self):
        self.assertEqual(
            self.historical_reconciliation.production_only_names,
            HISTORICAL_PRODUCTION_ONLY,
        )
        master_names = {
            mod.normalize_company_name(row["company"]) for row in self.master
        }
        promoted_production_only = [
            name for name in (
                CALGARY_PROMOTIONS
                + REQUESTED_EXPANSION_PROMOTIONS
                + REQUESTED_EXPANSION_BATCH_B_PROMOTIONS
                + CANADIAN_EXPANSION_BATCH_C_PROMOTIONS
                + ADAPTER_EXPANSION_V1_BATCH_A_PROMOTIONS
                + REQUESTED_EXPANSION_BATCH_D_PROMOTIONS
                + REQUESTED_EXPANSION_BATCH_E_PROMOTIONS
                + REQUESTED_EXPANSION_BATCH_F_PROMOTIONS
                + ADAPTER_EXPANSION_V1_BATCH_B_PROMOTIONS
                + CALGARY_CANADA_EXPANSION_V2_BATCH_A_PROMOTIONS
                + ADAPTER_EXPANSION_V1_BATCH_C_PROMOTIONS
                + ONTARIO_CANADA_EXPANSION_V1_BATCH_A_PROMOTIONS
            )
            if mod.normalize_company_name(name) not in master_names
        ]
        self.assertEqual(
            self.reconciliation.production_only_names,
            HISTORICAL_PRODUCTION_ONLY + promoted_production_only,
        )
        self.assertEqual(
            len(self.reconciliation.canonical_names) + len(self.reconciliation.mapped_names),
            196,
        )

    def test_08_normalization_does_not_use_weak_substrings(self):
        self.assertFalse(mod.exact_company_match("Meta", "Metadata Inc"))
        self.assertTrue(mod.exact_company_match("A&B", "A and B"))

    def test_09_master_order_is_deterministic(self):
        ranks = [row["master_rank"] for row in self.master]
        self.assertEqual(ranks, sorted(ranks))
        uncovered_keys = {
            mod.normalize_company_name(name) for name in self.reconciliation.uncovered_names
        }
        expected_order = [
            row["company"] for row in self.master
            if mod.normalize_company_name(row["company"]) in uncovered_keys
        ]
        self.assertEqual(self.reconciliation.uncovered_names, expected_order)

    def test_10_resume_skips_completed_companies(self):
        sources = [{"company": "A"}, {"company": "B"}]
        completed = [{"company": "A", "recoverability_class": "unresolved"}]
        self.assertEqual([row["company"] for row in mod.pending_master_rows(sources, completed)], ["B"])

    def test_11_atomic_writes_replace_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.txt"
            path.write_text("old", encoding="utf-8")
            mod.atomic_write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_12_atomic_writes_leave_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.csv"
            mod.atomic_write_csv(path, [{"a": "1"}], ["a"])
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_13_batch_size_is_capped_at_25(self):
        with self.assertRaisesRegex(ValueError, "batch size"):
            mod.main(["--dry-run", "--batch-size", "26"])

    def test_14_primary_request_limit_is_enforced(self):
        client = mod.BoundedResearchClient(session=FakeSession([FakeResponse()] * 3), request_delay=0, retries=0)
        client.begin_company("Acme")
        for index in range(3):
            client.fetch(f"https://example.com/{index}")
        with self.assertRaisesRegex(RuntimeError, "primary request limit"):
            client.fetch("https://example.com/4")

    def test_15_retry_limit_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "retries"):
            mod.BoundedResearchClient(retries=3)

    def test_16_script_count_limit_is_enforced(self):
        client = mod.BoundedResearchClient(session=FakeSession([FakeResponse()] * 10), request_delay=0, retries=0)
        client.begin_company("Acme")
        for index in range(10):
            client.fetch(f"https://example.com/{index}.js", request_kind="script")
        with self.assertRaisesRegex(RuntimeError, "script-count"):
            client.fetch("https://example.com/overflow.js", request_kind="script")

    def test_17_primary_response_byte_limit_is_enforced(self):
        body = b"x" * mod.MAX_PRIMARY_BYTES
        client = mod.BoundedResearchClient(session=FakeSession([FakeResponse(body=body)]), request_delay=0, retries=0)
        client.begin_company("Acme")
        client.fetch("https://example.com/large")
        with self.assertRaisesRegex(RuntimeError, "byte limit"):
            client.fetch("https://example.com/second")

    def test_18_identical_requests_are_cached(self):
        session = FakeSession([FakeResponse()])
        client = mod.BoundedResearchClient(session=session, request_delay=0, retries=0)
        client.begin_company("Acme")
        first = client.fetch("https://example.com/jobs")
        second = client.fetch("https://example.com/jobs")
        self.assertIs(first, second)
        self.assertEqual(len(session.calls), 1)

    def test_19_redirect_chain_is_recorded(self):
        redirect = FakeResponse(302, headers={"Location": "/jobs"}, url="https://example.com/careers")
        final = FakeResponse(200, url="https://example.com/jobs")
        client = mod.BoundedResearchClient(session=FakeSession([redirect, final]), request_delay=0, retries=0)
        client.begin_company("Acme")
        evidence = client.fetch("https://example.com/careers")
        self.assertEqual([item["status"] for item in evidence.redirect_chain], [302, 200])

    def test_20_official_domain_recovery_is_bounded(self):
        urls = mod.official_seed_urls({}, {
            "current_official_careers_url": "https://corp.example/careers",
            "current_job_search_url": "https://jobs.vendor.example/search",
            "endpoint_url": "https://third.example/api/jobs",
        })
        self.assertLessEqual(len(urls), 3)
        self.assertEqual(urls[0], "https://corp.example/careers")
        self.assertNotIn("https://third.example/api/jobs", urls)

    def test_21_third_party_aggregators_are_rejected(self):
        urls = mod.official_seed_urls({}, {"current_official_careers_url": "https://www.indeed.com/cmp/acme/jobs"})
        self.assertEqual(urls, [])

    def test_22_workday_detection(self):
        self.assertEqual(detection("https://acme.wd5.myworkdayjobs.com/External").family, "workday")

    def test_23_greenhouse_detection(self):
        self.assertEqual(detection("https://job-boards.greenhouse.io/acme").family, "greenhouse")

    def test_24_lever_detection(self):
        self.assertEqual(detection("https://jobs.lever.co/acme").family, "lever")

    def test_25_ashby_detection(self):
        self.assertEqual(detection("https://jobs.ashbyhq.com/acme").family, "ashby")

    def test_26_eightfold_detection(self):
        body = '<script>const endpoint="https://acme.eightfold.ai/api/pcsx/search";</script>'
        self.assertEqual(detection("https://acme.eightfold.ai/careers", body).family, "eightfold")

    def test_27_phenom_detection(self):
        body = '<script>const endpoint="https://jobs.acme.com/api/jobsearch"; /* phenompeople.com */</script>'
        self.assertEqual(detection("https://jobs.acme.com", body).family, "phenom")

    def test_28_successfactors_detection(self):
        body = '<script>const host="https://career5.successfactors.eu/career"; const careerSiteCompanyId="acme";</script>'
        self.assertEqual(detection("https://jobs.acme.com", body).family, "sap_successfactors")

    def test_29_oracle_recruiting_detection(self):
        body = '<script>fetch("https://acme.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions")</script>'
        self.assertEqual(detection("https://careers.acme.com", body).family, "oracle_recruiting")

    def test_30_avature_detection(self):
        self.assertEqual(detection("https://acme.avature.net/careers/SearchJobs").family, "avature")

    def test_31_icims_detection(self):
        self.assertEqual(detection("https://careers-acme.icims.com/jobs/search").family, "icims")

    def test_32_static_html_detection(self):
        body = '<a href="/jobs/abc123">Engineer</a><a href="/jobs/def456">Designer</a>'
        self.assertEqual(detection("https://acme.example/careers", body).family, "static_html")

    def test_33_embedded_data_detection(self):
        body = '<script id="__NEXT_DATA__" type="application/json">{"jobs":[{"jobId":"1","applyUrl":"https://acme.example/apply/1"}]}</script>'
        self.assertEqual(detection("https://acme.example/careers", body).family, "javascript_embedded_data")

    def test_34_public_rest_detection(self):
        body = '<script>fetch("https://acme.example/api/jobs")</script>'
        self.assertEqual(detection("https://acme.example/careers", body).family, "custom_rest_api")

    def test_35_public_graphql_detection(self):
        body = '<script>const u="https://acme.example/graphql"; query Jobs { jobs { id } }</script>'
        self.assertEqual(detection("https://acme.example/careers", body).family, "custom_graphql")

    def test_36_browser_required_classification(self):
        result = mod._classify(FakeEvidence(200), [FakeEvidence(200)], mod.PageAnalysis(js_shell=True), mod.Detection(), "not_applicable", {}, {})
        self.assertEqual(result[0], "browser_required")

    def test_37_session_required_classification(self):
        result = mod._classify(FakeEvidence(200), [FakeEvidence(200)], mod.PageAnalysis(), mod.Detection(), "unsupported_variant:session_required", {}, {})
        self.assertEqual(result[0], "session_required")

    def test_38_authentication_required_classification(self):
        result = mod._classify(FakeEvidence(401), [FakeEvidence(401)], mod.PageAnalysis(), mod.Detection(), "not_applicable", {}, {})
        self.assertEqual(result[0], "authentication_required")

    def test_39_blocked_classification(self):
        result = mod._classify(FakeEvidence(403), [FakeEvidence(403)], mod.PageAnalysis(), mod.Detection(), "not_applicable", {}, {})
        self.assertEqual(result[0], "blocked")

    def test_40_stale_source_classification(self):
        result = mod._classify(FakeEvidence(404), [FakeEvidence(404)], mod.PageAnalysis(), mod.Detection(), "not_applicable", {}, {})
        self.assertEqual(result[0], "stale_or_invalid")

    def test_41_network_inaccessible_classification(self):
        result = mod._classify(FakeEvidence(0), [FakeEvidence(0)], mod.PageAnalysis(), mod.Detection(), "not_applicable", {}, {})
        self.assertEqual(result[0], "network_inaccessible")

    def test_42_acquired_or_renamed_classification(self):
        result = mod._classify(FakeEvidence(200), [FakeEvidence(200)], mod.PageAnalysis(), mod.Detection(), "not_applicable", {}, {"notes": "acquired and renamed"})
        self.assertEqual(result[:2], ("stale_or_invalid", "acquired_or_renamed"))

    def test_43_compatibility_probe_uses_production_dispatch(self):
        client = mod.BoundedResearchClient(session=FakeSession([]), request_delay=0, retries=0)
        client.begin_company("Acme")
        adapter = lambda session, target, timeout: [sample_job()]
        with patch.dict(mod.production_scanner.ADAPTERS, {"greenhouse": adapter}, clear=False):
            result = mod.compatibility_probe("Acme", mod.Detection(family="greenhouse", endpoint_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs"), {}, client)
        self.assertEqual(result[:2], ("production_parser_compatible", 1))

    def test_44_unsupported_adapter_variant_fails_closed(self):
        client = mod.BoundedResearchClient(session=FakeSession([]), request_delay=0, retries=0)
        client.begin_company("Acme")
        def adapter(session, target, timeout):
            raise AdapterError("unsupported")
        with patch.dict(mod.production_scanner.ADAPTERS, {"greenhouse": adapter}, clear=False):
            result = mod.compatibility_probe("Acme", mod.Detection(family="greenhouse", endpoint_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs"), {}, client)
        self.assertTrue(result[0].startswith("unsupported_variant"))

    def test_45_compatibility_pages_are_capped_at_two(self):
        client = mod.BoundedResearchClient(session=FakeSession([]), request_delay=0, retries=0)
        client.begin_company("Acme")
        client.budget.compatibility_pages = 2
        with self.assertRaisesRegex(AdapterError, "page limit"):
            mod.CompatibilitySession(client).get("https://example.com/jobs")

    def test_46_normalized_jobs_are_capped_at_twenty(self):
        client = mod.BoundedResearchClient(session=FakeSession([]), request_delay=0, retries=0)
        client.begin_company("Acme")
        adapter = lambda session, target, timeout: [sample_job(str(index)) for index in range(30)]
        with patch.dict(mod.production_scanner.ADAPTERS, {"greenhouse": adapter}, clear=False):
            result = mod.compatibility_probe("Acme", mod.Detection(family="greenhouse", endpoint_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs"), {}, client)
        self.assertEqual(result[1], 20)

    def test_47_parent_exact_field_evidence_is_accepted(self):
        validations = {"child": [{"parent_company": "Parent", "exact_metadata_field": "brand", "exact_metadata_values": "Child", "status": "validated_parent_mapping_with_current_jobs"}]}
        self.assertEqual(mod._parent_evidence("Child", validations, {"parent"})["status"], "parent_mapping_ready")

    def test_48_title_only_parent_evidence_is_rejected(self):
        validations = {"child": [{"parent_company": "Parent", "title": "Child", "status": "validated"}]}
        self.assertNotEqual(mod._parent_evidence("Child", validations, {"parent"})["status"], "parent_mapping_ready")

    def test_49_description_only_parent_evidence_is_rejected(self):
        validations = {"child": [{"parent_company": "Parent", "description": "Child", "status": "validated"}]}
        self.assertFalse(mod._parent_evidence("Child", validations, {"parent"})["field"])

    def test_50_url_only_parent_evidence_is_rejected(self):
        validations = {"child": [{"parent_company": "Parent", "url": "/child/jobs", "status": "validated"}]}
        self.assertFalse(mod._parent_evidence("Child", validations, {"parent"})["values"])

    def test_51_weak_parent_attribution_is_rejected(self):
        validations = {"child": [{"parent_company": "Parent", "exact_metadata_field": "brand", "exact_metadata_values": "Child", "status": "weak_parent_attribution"}]}
        self.assertEqual(mod._parent_evidence("Child", validations, {"parent"})["status"], "weak_parent_attribution")

    def test_52_one_final_recoverability_class_per_company(self):
        for name in mod.RECOVERABILITY_CLASSES:
            self.assertIsInstance(name, str)
        self.assertEqual(len(mod.RECOVERABILITY_CLASSES), 13)

    def test_53_one_recommended_next_action_per_company(self):
        sample = [record(1, "A", "unresolved")]
        mod.validate_completed(sample, ["A"])

    def test_54_cluster_generation_is_deterministic(self):
        rows = [record(2, "B", "unresolved"), record(1, "A", "unresolved")]
        self.assertEqual(mod.build_clusters(rows), mod.build_clusters(list(reversed(rows))))

    def test_55_recovery_batches_are_deterministic(self):
        rows = [record(2, "B", "browser_required"), record(1, "A", "ready_existing_adapter", "greenhouse")]
        self.assertEqual(mod.build_recovery_batches(rows), mod.build_recovery_batches(list(reversed(rows))))

    def test_56_next_batch_manifest_excludes_unsafe_classes(self):
        rows = [record(index, value, value, confidence="high") for index, value in enumerate(sorted(mod.UNSAFE_NEXT_BATCH_CLASSES), 1)]
        self.assertEqual(mod.build_next_batch(rows), [])

    def test_57_reconciliation_does_not_modify_production_registry(self):
        before = mod.PRODUCTION_REGISTRY.read_bytes()
        mod.reconcile_master(
            mod.load_master_workbook(),
            mod.load_target_companies(str(mod.PRODUCTION_REGISTRY)),
            mod.load_parent_source_mappings(mod.PARENT_MAPPINGS),
        )
        self.assertEqual(mod.PRODUCTION_REGISTRY.read_bytes(), before)

    def test_58_parent_mappings_remain_byte_identical_to_head(self):
        expected = subprocess.check_output(["git", "rev-parse", "HEAD:config/company_parent_source_mappings.yaml"], cwd=ROOT, text=True).strip()
        observed = subprocess.check_output(["git", "hash-object", "--path=config/company_parent_source_mappings.yaml", "config/company_parent_source_mappings.yaml"], cwd=ROOT, text=True).strip()
        self.assertEqual(observed, expected)

    def test_59_no_supabase_initialization_import(self):
        self.assertFalse(self._imports_containing("supabase"))

    def test_60_no_google_sheets_initialization_import(self):
        self.assertFalse(self._imports_containing("google_sheets"))

    def test_61_no_sqlite_initialization_import(self):
        self.assertFalse(self._imports_containing("sqlite"))

    def test_62_no_dashboard_publication_import(self):
        self.assertFalse(self._imports_containing("dashboard"))

    def test_63_no_browser_automation_import(self):
        imports = self._all_imports()
        self.assertFalse(any(value in name for name in imports for value in ("playwright", "selenium", "chromium")))

    def test_64_existing_suite_safety_contract_is_preserved(self):
        source = Path(mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("target_companies.yaml).write", source)
        self.assertNotIn("company_parent_source_mappings.yaml).write", source)

    @staticmethod
    def _all_imports():
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        return names

    def _imports_containing(self, value):
        return [name for name in self._all_imports() if value in name]


class FakeEvidence(mod.ResponseEvidence):
    def __init__(self, status):
        super().__init__("https://example.com/jobs", "https://example.com/jobs", status=status)


def sample_job(identifier="1"):
    return JobRecord(
        job_id=identifier, title="Engineer", company="Acme", location="Remote",
        url=f"https://example.com/jobs/{identifier}", updated_at="2026-08-05",
        source="greenhouse:acme",
    )


if __name__ == "__main__":
    unittest.main()
