from __future__ import annotations

import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts import investigate_unknown_companies as mod


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.log = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.get(url)
        if value is None:
            return mod.ResponseEvidence(url, url, 404, [{"url": url, "status": 404}])
        return value


def response(url, text="", status=200, final=None, headers=None):
    final = final or url
    return mod.ResponseEvidence(url, final, status, [{"url": final, "status": status}], "text/html", text, "Jobs", headers or {})


def source(name="Example", rank="1", url="https://example.com/careers"):
    return {
        "company": name,
        "normalized_company": mod.normalize_company_name(name),
        "inventory_original_rank": rank,
        "inventory_original_priority": "high",
        "inventory_original_careers_url": url,
        "inventory_original_notes": "Robotics",
        "v2_platform_family": "unknown",
        "v2_current_classification": "unknown",
        "v2_confidence": "low",
        "v2_evidence": "prior evidence",
        "v2_urls_tested": url,
        "v2_current_job_search_url": url,
        "v2_current_official_careers_url": url,
        "v2_original_careers_url": url,
        "v2_final_url": url,
    }


def investigate(html_text, *, status=200, name="Example", url="https://example.com/careers", extra=None):
    responses = {url: response(url, html_text, status=status)}
    responses.update(extra or {})
    return mod.investigate_company(source(name=name, url=url), FakeClient(responses), max_scripts=4, max_script_bytes=10000, max_pages=1, max_jobs=20, timestamp="2026-08-04T00:00:00+00:00")


class UnknownInvestigationTests(unittest.TestCase):
    def test_01_exact_unknown_extraction(self):
        fp = [{"company": "A", "normalized_company": "a", "platform_family": "unknown"}]
        inv = [{"company": "A", "normalized_company": "a", "original_rank": "1", "production_covered": "false"}]
        self.assertEqual(["A"], [row["company"] for row in mod.extract_unknown_records(fp, inv)])

    def test_02_non_unknown_is_excluded(self):
        fp = [{"company": "A", "normalized_company": "a", "platform_family": "icims"}]
        inv = [{"company": "A", "normalized_company": "a", "original_rank": "1", "production_covered": "false"}]
        self.assertEqual([], mod.extract_unknown_records(fp, inv))

    def test_03_every_source_gets_one_record(self):
        sources = [source("A", "1"), source("B", "2")]
        rows = mod.merge_results(sources, [], lambda row: {"normalized_company": row["normalized_company"], "primary_classification": "still_unresolved"}, resume=False)
        self.assertEqual(2, len(rows))

    def test_04_normalization_is_not_substring_matching(self):
        self.assertNotEqual(mod.normalize_company_name("Meta"), mod.normalize_company_name("Metadata Inc"))

    def test_05_v2_evidence_is_preserved(self):
        row = investigate("<html><body>Careers</body></html>")
        self.assertEqual("prior evidence", row["v2_evidence"])

    def test_06_resume_preserves_completed(self):
        old = {"normalized_company": "a", "primary_classification": "icims", "sentinel": "keep"}
        rows = mod.merge_results([source("A")], [old], lambda _: self.fail("called"), resume=True)
        self.assertEqual("keep", rows[0]["sentinel"])

    def test_07_resume_only_investigates_incomplete(self):
        calls = []
        sources = [source("A", "1"), source("B", "2")]
        old = {"normalized_company": "a", "primary_classification": "icims"}
        rows = mod.merge_results(sources, [old], lambda row: calls.append(row["company"]) or {"normalized_company": row["normalized_company"], "primary_classification": "still_unresolved"}, resume=True)
        self.assertEqual(["B"], calls); self.assertEqual(2, len(rows))

    def test_08_batch_size_capped_at_25(self):
        with self.assertRaises(ValueError): mod.merge_results([], [], lambda x: x, resume=False, batch_size=26)

    def test_09_deterministic_output_ordering(self):
        sources = [source("B", "2"), source("A", "1")]
        rows = mod.merge_results(sources, [], lambda row: {"normalized_company": row["normalized_company"], "primary_classification": "still_unresolved"}, resume=False)
        self.assertEqual(["b", "a"], [row["normalized_company"] for row in rows])

    def test_10_atomic_csv_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"; mod.write_csv(path, [{"a": "1"}], ["a"])
            self.assertTrue(path.exists()); self.assertFalse(Path(str(path) + ".tmp").exists())

    def test_11_redirect_chain_recorded(self):
        url = "https://example.com/careers"; final = "https://jobs.example.com/"
        ev = response(url, "<html>jobs</html>", final=final); ev.redirect_chain = [{"url": url, "status": 302, "location": final}, {"url": final, "status": 200}]
        row = mod.investigate_company(source(url=url), FakeClient({url: ev}), max_scripts=0, max_script_bytes=10, max_pages=1, max_jobs=1, timestamp="x")
        self.assertIn("302", row["redirect_chain"])

    def test_12_current_url_recovery(self):
        old = "https://example.com/old"; new = "https://example.com/jobs"
        page = f'<a href="{new}">Jobs</a>'
        row = mod.investigate_company(source(url=old), FakeClient({old: response(old, page, 404), new: response(new, '<a href="/jobs/123">Role</a><a href="/jobs/456">Role</a>')}), max_scripts=0, max_script_bytes=10, max_pages=2, max_jobs=1, timestamp="x")
        self.assertEqual(new, row["current_job_search_url"])

    def test_13_stale_url_classification(self):
        ev = response("https://x.test/old", status=404)
        self.assertEqual("stale", mod._stored_url_status("https://x.test/old", ev, "https://x.test/new"))

    def test_14_jsonld_jobposting_detection(self):
        analysis = mod.analyze_html("https://x.test", '<script type="application/ld+json">{"@type":"JobPosting","title":"X"}</script>')
        self.assertEqual(1, analysis.jsonld_job_count)

    def test_15_inline_json_detection(self):
        self.assertTrue(mod.analyze_html("https://x.test", '<script type="application/json">{"jobs":[]}</script>').inline_json)

    def test_16_nextjs_state_detection(self):
        self.assertIn("nextjs", mod.analyze_html("https://x.test", '<script id="__NEXT_DATA__">{}</script>').serialized_state)

    def test_17_search_form_contract_detection(self):
        analysis = mod.analyze_html("https://x.test/careers", '<form method="get" action="/jobs"></form>')
        self.assertEqual("GET", analysis.forms[0]["method"])

    def test_18_rest_endpoint_detection(self):
        analysis = mod.analyze_html("https://x.test", "")
        detected = mod.detect_contract("https://x.test", 'const u="https://x.test/api/jobs"', analysis)
        self.assertEqual("custom_rest_api", detected.family)

    def test_19_graphql_endpoint_detection(self):
        analysis = mod.analyze_html("https://x.test", "")
        detected = mod.detect_contract("https://x.test", 'ApolloClient({uri:"https://x.test/graphql"})', analysis)
        self.assertEqual("custom_graphql", detected.family)

    def test_20_static_html_detection(self):
        html = '<a href="/jobs/123">One</a><a href="/jobs/456">Two</a>'
        detected = mod.detect_contract("https://x.test", html, mod.analyze_html("https://x.test", html))
        self.assertEqual("static_html", detected.family)

    def test_21_javascript_shell_detection(self):
        self.assertTrue(mod.analyze_html("https://x.test", '<div id="root"></div><noscript>Enable JavaScript</noscript>').js_shell)

    def test_22_public_endpoint_from_script(self):
        page = '<script src="/assets/jobs.js"></script>'
        script_url = "https://example.com/assets/jobs.js"
        row = investigate(page, extra={script_url: response(script_url, 'const endpoint="https://example.com/api/jobs"')})
        self.assertEqual("custom_rest_api", row["platform_family"])

    def test_23_script_count_limit(self):
        urls = [f"https://x.test/jobs-{i}.js" for i in range(10)]
        self.assertEqual(3, len(mod.choose_relevant_scripts(urls, 3)))

    def test_24_script_byte_limit_argument(self):
        args = mod.build_parser().parse_args(["--max-script-bytes", "2000001"])
        with self.assertRaises(ValueError): mod._validate_args(args)

    def test_25_production_adapter_compatibility_classification(self):
        detected = mod.detect_contract("https://jobs.lever.co/acme", "", mod.PageAnalysis())
        self.assertIn(detected.family, mod.EXISTING_ADAPTERS)

    def test_26_existing_adapter_reuse(self):
        row = investigate("", url="https://jobs.lever.co/acme", extra={"https://api.lever.co/v0/postings/acme?mode=json": response("https://api.lever.co/v0/postings/acme?mode=json", "[]")})
        self.assertEqual("existing_adapter_reuse", row["primary_classification"])

    def test_27_existing_adapter_extension(self):
        detection = mod.Detection("workday", extension_required=True)
        self.assertEqual("existing_adapter_extension", mod._classification(response("https://x.test"), mod.PageAnalysis(), detection, 0, "", False))

    def test_28_avature_classification(self):
        detected = mod.detect_contract("https://jobs.avature.net/careers", "", mod.PageAnalysis())
        self.assertEqual("avature", detected.family)

    def test_29_icims_classification(self):
        detected = mod.detect_contract("https://careers-acme.icims.com/jobs", "", mod.PageAnalysis())
        self.assertEqual("icims", detected.family)

    def test_30_parent_company_classification(self):
        detection = mod.Detection("parent_company_source")
        self.assertEqual("parent_company_source", mod._classification(response("https://x.test"), mod.PageAnalysis(), detection, 0, "", False))

    def test_31_deterministic_child_brand_field_output(self):
        record = {"company":"X", "parent_company":"P", "parent_source_candidate":"true", "deterministic_brand_fields":"team=X"}
        self.assertEqual("team=X", mod.build_parents([record])[0]["deterministic_identifying_fields"])

    def test_32_duplicate_source_risk_recording(self):
        record = {"company":"X", "parent_company":"P", "parent_source_candidate":"true", "duplicate_source_risk":"high"}
        self.assertEqual("high", mod.build_parents([record])[0]["duplicate_source_risk"])

    def test_33_browser_required_needs_script_attempt(self):
        shell = mod.PageAnalysis(js_shell=True)
        self.assertEqual("still_unresolved", mod._classification(response("https://x.test"), shell, mod.Detection(), 0, "", False))
        self.assertEqual("browser_required", mod._classification(response("https://x.test"), shell, mod.Detection(), 1, "", False))

    def test_34_antibot_classification(self):
        ev = response("https://x.test", "Access Denied", status=403)
        self.assertTrue(mod._anti_bot(ev))

    def test_35_network_inaccessible_classification(self):
        self.assertEqual("network_inaccessible", mod._classification(mod.ResponseEvidence("x"), mod.PageAnalysis(), mod.Detection(), 0, "", False))

    def test_36_invalid_url_classification(self):
        self.assertEqual("invalid_careers_url", mod._classification(response("https://x.test", status=404), mod.PageAnalysis(), mod.Detection(), 0, "", False))

    def test_37_acquired_or_renamed_is_allowed(self):
        self.assertIn("acquired_or_renamed", mod.FINAL_CLASSIFICATIONS)

    def test_38_unresolved_preserves_attempts(self):
        row = investigate("<html>Careers</html>")
        self.assertEqual("still_unresolved", row["primary_classification"]); self.assertIn("example.com", row["urls_tested"])

    def test_39_stable_job_id_evidence(self):
        html = '<a href="/jobs/abc123">One</a><a href="/jobs/def456">Two</a>'
        row = investigate(html)
        self.assertEqual("true", row["stable_job_id_evidence"])

    def test_40_apply_url_evidence(self):
        html = '<a href="/jobs/abc123">One</a><a href="/jobs/def456">Two</a><a href="/apply/abc123">Apply</a>'
        self.assertEqual("true", investigate(html)["official_apply_url_evidence"])

    def test_41_pagination_evidence(self):
        detected = mod.detect_contract("https://jobs.smartrecruiters.com/Acme", "", mod.PageAnalysis())
        self.assertEqual("offset/limit", detected.pagination)

    def test_42_cluster_construction(self):
        clusters = mod.build_clusters([{"company":"A","platform_family":"icims","platform_variant":"v1","primary_classification":"icims"}])
        self.assertEqual("1", clusters[0]["company_count"])

    def test_43_incompatible_json_contracts_stay_separate(self):
        rows = [{"company":"A","platform_family":"custom_rest_api","platform_variant":"v1"},{"company":"B","platform_family":"custom_rest_api","platform_variant":"v2"}]
        self.assertEqual(2, len(mod.build_clusters(rows)))

    def test_44_priority_scoring_deterministic(self):
        args = (source(), mod.Detection("icims", confidence="medium"), "icims")
        self.assertEqual(mod._scores(*args), mod._scores(*args))

    def test_45_production_registry_is_not_written_by_dry_run(self):
        path = Path("config/target_companies.yaml"); before = path.read_bytes()
        self.assertEqual(0, mod.main(["--dry-run", "--limit", "1"])); self.assertEqual(before, path.read_bytes())

    def test_46_quick_wins_files_not_referenced_as_outputs(self):
        defaults = vars(mod.build_parser().parse_args([]))
        self.assertFalse(any("quick_wins" in str(value) for value in defaults.values()))

    def test_47_no_supabase_initialization(self):
        self.assertNotIn("supabase", Path(mod.__file__).read_text(encoding="utf-8").casefold())

    def test_48_no_google_sheets_initialization(self):
        self.assertNotIn("google_sheets", Path(mod.__file__).read_text(encoding="utf-8").casefold())

    def test_49_no_sqlite_initialization(self):
        self.assertNotIn("sqlite", Path(mod.__file__).read_text(encoding="utf-8").casefold())

    def test_50_repo_unknown_count_is_97(self):
        rows = mod.extract_unknown_records(mod.read_csv(mod.DEFAULT_FINGERPRINTS), mod.read_csv(mod.DEFAULT_INVENTORY))
        self.assertEqual(97, len(rows))

    def test_reuse_rows_are_dedicated(self):
        record = {"company":"A","primary_classification":"existing_adapter_reuse","existing_adapter":"lever","endpoint_url":"https://api.lever.co/v0/postings/a","adapter_validation_result":"compatible_json_contract"}
        self.assertEqual("lever", mod.build_reuse([record])[0]["production_adapter"])

    def test_max_pages_is_two(self):
        args = mod.build_parser().parse_args(["--max-pages", "3"])
        with self.assertRaises(ValueError): mod._validate_args(args)

    def test_max_jobs_is_twenty(self):
        args = mod.build_parser().parse_args(["--max-jobs", "21"])
        with self.assertRaises(ValueError): mod._validate_args(args)

    def test_request_cache_avoids_duplicate_requests(self):
        class Session:
            calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                class Raw:
                    def read(self, *_args, **_kwargs): return b"ok"
                class R:
                    status_code=200; headers={}; raw=Raw(); encoding="utf-8"
                    def close(self): pass
                return R()
        session = Session(); client = mod.ResearchClient(request_delay=0, session=session)
        client.fetch("https://example.com/x"); client.fetch("https://example.com/x")
        self.assertEqual(1, session.calls)

    def test_malformed_embedded_url_is_ignored(self):
        self.assertEqual([], mod._urls_from_text("https://x.test", '"https://[invalid]/api/jobs"'))

    def test_generic_script_bundles_are_not_downloaded(self):
        self.assertEqual([], mod.choose_relevant_scripts(["https://cdn.test/main.chunk.js"], 3))

    def test_shared_exact_endpoint_is_parent_candidate(self):
        endpoint = "https://rockwell.wd1.myworkdayjobs.com/wday/cxs/rockwell/Careers/jobs"
        rows = [{"company":"Rockwell", "endpoint_url":endpoint}, {"company":"Child", "endpoint_url":endpoint}]
        annotated = mod.annotate_shared_sources(rows)
        self.assertEqual("parent_company_source", annotated[1]["primary_classification"])

    def test_workday_contract_uses_production_parser(self):
        endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
        payload = {"jobPostings":[{"title":"Engineer","externalPath":"/job/Engineer_R1","locationsText":"Remote","bulletFields":["R1"],"postedOn":"Today"}]}
        client = FakeClient({endpoint: response(endpoint, json.dumps(payload))})
        result = mod.validate_public_endpoint(client, mod.Detection("workday", endpoint_url=endpoint), 20)
        self.assertEqual("production_parser_compatible", result[0]); self.assertEqual(1, result[1])

    def test_shared_parent_confidence_is_conservative(self):
        endpoint = "https://parent.test/api/jobs"
        rows = [{"company":"Parent", "endpoint_url":endpoint, "confidence":"high"}, {"company":"Child", "endpoint_url":endpoint, "confidence":"high"}]
        self.assertEqual("medium", mod.annotate_shared_sources(rows)[1]["confidence"])


if __name__ == "__main__":
    unittest.main()
