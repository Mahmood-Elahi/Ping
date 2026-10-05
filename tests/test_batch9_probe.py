from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from scripts import probe_batch9_recovery as probe
from src.scraper.company_careers.models import AdapterError, CompanyTarget


def _target(*, identity_regex: str | None = r"Example Corp") -> CompanyTarget:
    return CompanyTarget(
        name="Example",
        platform="static_html",
        careers_url="https://example.com/careers",
        listing_url="https://example.com/jobs/",
        endpoint_variant="server_rendered_cards",
        listing_selector="div.jobs",
        job_card_selector="div.job",
        title_selector="a.title",
        detail_link_selector="a.title",
        external_id_regex=r"/jobs/(\d+)$",
        allowed_apply_hosts=["example.com"],
        identity_regex=identity_regex,
        max_pages=2,
        max_jobs=20,
    )


def _response(
    html: str,
    url: str = "https://example.com/jobs/",
    content_type: str = "text/html; charset=utf-8",
) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.headers["content-type"] = content_type
    response._content = html.encode("utf-8")
    response._content_consumed = True
    request = requests.Request("GET", url).prepare()
    response.request = request
    return response


def _xml_response(xml: str, url: str = "https://example.com/jobs/feed.xml") -> requests.Response:
    return _response(xml, url, "application/rss+xml; charset=utf-8")


def _partition_diagnostic(
    *,
    value: str,
    ids: list[str],
    baseline_total: int,
    observed: list[str],
    anchor_ids: list[str] | None = None,
    partition_total: int | None = None,
    validated: bool = True,
) -> dict:
    anchor_ids = anchor_ids or ["anchor-1", "anchor-2"]
    anchor = {"advertised_total": baseline_total, "stable_ids": anchor_ids}
    total = partition_total if partition_total is not None else len(ids)
    page = {
        "offset": 0,
        "requested_limit": 100,
        "filters": {"country": value},
        "returned_records": len(ids),
        "advertised_total": total,
        "stable_ids": ids,
        "duplicate_ids_within_page": [],
        "partition_value_validated": validated,
        "unexpected_partition_values": [],
        "absent_partition_value_count": 0,
    }
    return {
        "schema_version": 3,
        "company": "Bosch",
        "mode": "smartrecruiters-partition",
        "status": "diagnostic_complete",
        "source_identity_evidence": {
            "status": "validated",
            "company_identifier": "BoschGroup",
        },
        "anchor_pages": [dict(anchor), dict(anchor)],
        "source_observed_dimension_values": observed,
        "absent_dimension_observed_in_anchor": False,
        "partition": {
            "dimension": "country",
            "value": value,
            "pages": [page],
            "advertised_totals": [total],
            "retrieved_records": len(ids),
            "unique_stable_ids": len(set(ids)),
            "duplicate_ids": [],
            "failure_reason": None if len(set(ids)) == total else "authoritative_total_not_reached",
            "terminal_evidence": {
                "observed": len(set(ids)) == total,
                "type": "authoritative_total" if len(set(ids)) == total else None,
                "value": total if len(set(ids)) == total else None,
            },
            "stable_ids": ids,
            "partition_values_validated": validated,
            "unexpected_partition_values": [],
            "absent_partition_value_count": 0,
        },
    }


def _page(
    stable_ids: list[str],
    *,
    titles: list[str] | None = None,
    urls: list[str] | None = None,
    status: str = "unproven",
    total: int | None = None,
    rejected: int = 0,
) -> dict:
    return {
        "stable_ids": stable_ids,
        "titles": titles or [f"Job {value}" for value in stable_ids],
        "canonical_urls": urls or [f"https://example.com/jobs/{value}" for value in stable_ids],
        "rejected_records": [{"reason": "bad"}] * rejected,
        "pagination": {"status": status, "reported_total": total},
    }


class Batch9ProbeContractTests(unittest.TestCase):
    def test_valid_pagination_progression_and_explicit_terminal_evidence(self) -> None:
        first, _fixture = probe.analyze_static_page(
            b"""
            <html><head><title>Example Corp jobs</title></head><body>
              <div class='jobs'><div class='job'><a class='title' href='/jobs/101'>Engineer</a></div></div>
              <a rel='next' href='/jobs/?page=2'>Next</a>
            </body></html>
            """,
            _response("<html></html>"),
            _target(),
        )
        second_response = _response("<html></html>", "https://example.com/jobs/?page=2")
        second, _fixture = probe.analyze_static_page(
            b"""
            <html><head><title>Example Corp jobs</title></head><body>
              <div class='jobs'><div class='job'><a class='title' href='/jobs/102'>Analyst</a></div></div>
              <a aria-label='Next' aria-disabled='true'>Next</a>
            </body></html>
            """,
            second_response,
            _target(),
        )
        summary = probe.summarize_static_pages([first, second])
        self.assertEqual(first["pagination"]["status"], "continuation")
        self.assertEqual(second["pagination"]["status"], "terminal")
        self.assertEqual(summary["unique_stable_ids"], 2)
        self.assertIsNone(summary["failure_reason"])

    def test_repeated_page_is_not_terminal(self) -> None:
        page = _page(["101", "102"], status="continuation")
        summary = probe.summarize_static_pages([page, dict(page)])
        self.assertTrue(summary["repeated_page"])
        self.assertEqual(summary["failure_reason"], "repeated_page")

    def test_contradictory_smartrecruiters_totals_fail_closed(self) -> None:
        pages = [
            {"stable_ids": ["1"], "advertised_total": 10, "duplicate_ids_within_page": []},
            {"stable_ids": ["2"], "advertised_total": 11, "duplicate_ids_within_page": []},
        ]
        summary = probe.summarize_smartrecruiters_pages(pages)
        self.assertTrue(summary["collection_mutation"])
        self.assertEqual(summary["failure_reason"], "contradictory_totals")

    def test_duplicate_smartrecruiters_ids_fail_closed(self) -> None:
        pages = [
            {"stable_ids": ["1"], "advertised_total": 2, "duplicate_ids_within_page": []},
            {"stable_ids": ["1"], "advertised_total": 2, "duplicate_ids_within_page": []},
        ]
        summary = probe.summarize_smartrecruiters_pages(pages)
        self.assertEqual(summary["duplicate_ids"], ["1"])
        self.assertEqual(summary["failure_reason"], "duplicate_ids_across_offsets")

    def test_malformed_response_is_rejected(self) -> None:
        response = _response("")
        with self.assertRaisesRegex(AdapterError, "empty HTTP 200"):
            probe._checked_response(response, maximum_bytes=100)

        oversized = _response("x" * 101)
        with self.assertRaisesRegex(AdapterError, "exceeded the 100-byte"):
            probe._checked_response(oversized, maximum_bytes=100)

    def test_correct_and_incorrect_source_identity_are_distinguished(self) -> None:
        content = b"""
        <html><head><title>Example Corp jobs</title></head><body>
          <div class='jobs'><div class='job'><a class='title' href='/jobs/101'>Engineer</a></div></div>
        </body></html>
        """
        valid, _fixture = probe.analyze_static_page(content, _response(""), _target())
        invalid, _fixture = probe.analyze_static_page(
            content, _response(""), _target(identity_regex=r"Other Corp")
        )
        self.assertEqual(valid["source_identity"]["status"], "validated")
        self.assertEqual(invalid["source_identity"]["status"], "failed")

    def test_missing_terminal_evidence_fails_closed(self) -> None:
        summary = probe.summarize_static_pages([_page(["101"])])
        self.assertEqual(
            summary["failure_reason"], "missing_authoritative_terminal_evidence"
        )

    def test_collection_mutation_is_detected(self) -> None:
        pages = [
            _page(["101"], titles=["Engineer"]),
            _page(["101", "102"], titles=["Different title", "Analyst"]),
        ]
        summary = probe.summarize_static_pages(pages)
        self.assertTrue(summary["collection_mutation"])
        self.assertEqual(summary["failure_reason"], "collection_changed_during_scan")

    def test_canonical_url_collision_is_detected(self) -> None:
        page = _page(
            ["101", "102"],
            urls=["https://example.com/jobs/shared", "https://example.com/jobs/shared"],
        )
        summary = probe.summarize_static_pages([page])
        self.assertEqual(summary["failure_reason"], "canonical_url_collision")

    def test_hard_request_ceiling_counts_attempts(self) -> None:
        response = _response("<html></html>")
        session = probe.BoundedProbeSession(
            allowed_hosts={"example.com"}, request_limit=1
        )
        prepared = requests.Request("GET", "https://example.com/jobs/").prepare()
        with patch.object(requests.Session, "send", return_value=response):
            session.send(prepared)
            with self.assertRaisesRegex(probe.RequestCeilingReached, "hard request ceiling"):
                session.send(prepared)
        self.assertEqual(session.requests_started, 1)

    def test_request_host_and_tls_are_validated(self) -> None:
        session = probe.BoundedProbeSession(
            allowed_hosts={"example.com"}, request_limit=1
        )
        wrong_host = requests.Request("GET", "https://evil.example/jobs").prepare()
        with self.assertRaisesRegex(AdapterError, "approved source host"):
            session.send(wrong_host)
        insecure = requests.Request("GET", "http://example.com/jobs").prepare()
        with self.assertRaisesRegex(AdapterError, "anonymous HTTPS"):
            session.send(insecure)

    def test_explicit_allowlist_limits_and_non_overwriting_artifacts(self) -> None:
        self.assertEqual(probe.parse_request_limits(["Bosch=4"]), {"Bosch": 4})
        self.assertEqual(
            probe.parse_xml_response_limits(["Coupang=4000000"]),
            {"Coupang": 4_000_000},
        )
        with self.assertRaises(ValueError):
            probe.parse_request_limits(["Other=3"])
        for invalid in ("Bosch=4000000", "Coupang=2000000", "Coupang=8000001"):
            with self.assertRaises(ValueError):
                probe.parse_xml_response_limits([invalid])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            probe._write_new_json(path, {"ok": True})
            with self.assertRaises(FileExistsError):
                probe._write_new_json(path, {"ok": False})

    def test_feed_discovery_distinguishes_real_links_from_unverified_strings(self) -> None:
        content = b"""
        <html><head>
          <link rel='alternate' type='application/rss+xml' href='/jobs/xml/?rss=true'>
        </head><body>
          <script>var guessed = '/jobs/inferred-feed.xml';</script>
          <a href='https://evil.example/feed.xml'>Unapproved feed</a>
        </body></html>
        """
        page, fixture = probe.analyze_static_page(content, _response(""), _target())
        self.assertEqual(
            page["source_discovered_feed_links"][0]["url"],
            "https://example.com/jobs/xml/?rss=true",
        )
        self.assertEqual(
            page["source_discovered_feed_links"][0]["provenance"],
            "html_link_element",
        )
        self.assertNotIn(
            "https://evil.example/feed.xml",
            [item["url"] for item in page["source_discovered_feed_links"]],
        )
        self.assertIn("source_discovered_feed_links", fixture)
        self.assertTrue(
            any("inferred-feed.xml" in value for value in page["unverified_structured_endpoint_candidates"])
        )

    def test_rss_without_authoritative_total_is_not_atomic(self) -> None:
        page, _fixture = probe.analyze_xml_document(
            b"""<?xml version='1.0'?>
            <rss version='2.0'><channel><title>Example Corp jobs</title>
              <item><guid>101</guid><title>Engineer</title><link>https://example.com/jobs/101</link><pubDate>today</pubDate></item>
            </channel></rss>""",
            _xml_response(""),
            _target(),
        )
        summary = probe.summarize_xml_pages([page])
        self.assertEqual(page["format"], "rss")
        self.assertEqual(page["publication_fields_present"], ["pubDate"])
        self.assertEqual(summary["source_identity_evidence"]["status"], "validated")
        self.assertFalse(summary["terminal_evidence"]["observed"])
        self.assertEqual(
            summary["failure_reason"], "missing_authoritative_completeness_evidence"
        )

    def test_atom_with_matching_authoritative_total_can_be_complete(self) -> None:
        page, _fixture = probe.analyze_xml_document(
            b"""<feed xmlns='http://www.w3.org/2005/Atom' xmlns:os='http://a9.com/-/spec/opensearch/1.1/'>
              <title>Example Corp careers</title><os:totalResults>1</os:totalResults>
              <entry><id>abc-1</id><title>Analyst</title><updated>2026-10-01T00:00:00Z</updated>
                <link rel='alternate' href='https://example.com/jobs/101'/></entry>
            </feed>""",
            _xml_response("", "https://example.com/jobs/feed.atom"),
            _target(),
        )
        summary = probe.summarize_xml_pages([page])
        self.assertEqual(page["format"], "atom")
        self.assertTrue(summary["terminal_evidence"]["observed"])
        self.assertIsNone(summary["failure_reason"])

    def test_xml_entities_and_external_continuation_are_rejected(self) -> None:
        payload = b"""<?xml version='1.0'?><!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>
        <rss><channel><title>&xxe;</title></channel></rss>"""
        with self.assertRaisesRegex(AdapterError, "DTD and entity"):
            probe.analyze_xml_document(payload, _xml_response(""), _target())

        page, _fixture = probe.analyze_xml_document(
            b"""<feed xmlns='http://www.w3.org/2005/Atom'>
              <title>Example Corp jobs</title><link rel='next' href='https://evil.example/next'/>
              <entry><id>1</id><title>One</title><link href='https://example.com/jobs/1'/></entry>
            </feed>""",
            _xml_response("", "https://example.com/jobs/feed.atom"),
            _target(),
        )
        self.assertEqual(page["continuation_urls"], [])

    def test_xml_source_identity_failure_is_not_completion(self) -> None:
        page, _fixture = probe.analyze_xml_document(
            b"""<rss><channel><title>Other Company jobs</title>
              <item><guid>1</guid><title>One</title><link>https://example.com/jobs/1</link></item>
            </channel></rss>""",
            _xml_response(""),
            _target(),
        )
        summary = probe.summarize_xml_pages([page])
        self.assertEqual(page["source_identity"]["status"], "failed")
        self.assertEqual(summary["failure_reason"], "source_identity_failed")

    def test_xml_record_integrity_and_truncation_fail_closed(self) -> None:
        page, _fixture = probe.analyze_xml_document(
            b"""<rss xmlns:os='http://a9.com/-/spec/opensearch/1.1/'><channel>
              <title>Example Corp jobs</title><os:totalResults>3</os:totalResults>
              <item><title>Missing ID</title><link>https://example.com/jobs/0</link></item>
              <item><guid>1</guid><title>One</title><link>https://example.com/jobs/shared</link></item>
              <item><guid>2</guid><title>Two</title><link>https://example.com/jobs/shared</link></item>
            </channel></rss>""",
            _xml_response(""),
            _target(),
        )
        summary = probe.summarize_xml_pages([page])
        self.assertEqual(page["rejected_record_count"], 1)
        self.assertTrue(summary["canonical_url_collisions"])
        self.assertTrue(summary["feed_appears_truncated"])
        self.assertEqual(summary["failure_reason"], "canonical_url_collision")

    def test_xml_duplicate_repeated_page_and_conflicting_totals(self) -> None:
        base = {
            "stable_ids": ["1"],
            "titles": ["One"],
            "canonical_urls": ["https://example.com/jobs/1"],
            "total_metadata": [{"field": "totalResults", "value": 2, "authoritative": True}],
            "rejected_record_count": 0,
            "source_identity": {"status": "validated"},
            "continuation_urls": ["https://example.com/jobs/feed.xml?page=2"],
        }
        second = dict(base)
        second["total_metadata"] = [
            {"field": "totalResults", "value": 3, "authoritative": True}
        ]
        summary = probe.summarize_xml_pages([base, second])
        self.assertTrue(summary["repeated_page"])
        self.assertEqual(summary["failure_reason"], "conflicting_totals")

    def test_xml_phase_and_byte_ceilings_are_exact(self) -> None:
        response = _xml_response("<rss/>")
        session = probe.BoundedProbeSession(
            allowed_hosts={"example.com"},
            request_limit=10,
            phase_limits={"xml": 2},
        )
        session.set_phase("xml")
        prepared = requests.Request("GET", "https://example.com/feed.xml").prepare()
        with patch.object(requests.Session, "send", return_value=response):
            session.send(prepared)
            session.send(prepared)
            with self.assertRaisesRegex(probe.RequestCeilingReached, "hard xml request ceiling"):
                session.send(prepared)
        self.assertEqual(session.phase_requests_started["xml"], 2)
        oversized = _xml_response("x" * (probe.MAX_XML_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(AdapterError, "2000000-byte"):
            probe._checked_response(oversized, maximum_bytes=probe.MAX_XML_RESPONSE_BYTES)

    def test_xml_metadata_precedes_large_body_and_override_requires_confirmed_xml(self) -> None:
        declared_large = _xml_response("<rss><channel/></rss>")
        declared_large.headers["content-length"] = str(probe.MAX_XML_RESPONSE_BYTES + 500_000)
        with self.assertRaises(probe.XMLInspectionError) as captured:
            probe._inspect_xml_response(declared_large)
        inspection = captured.exception.inspection
        self.assertEqual(inspection["content_type"], "application/rss+xml; charset=utf-8")
        self.assertEqual(inspection["content_length"], probe.MAX_XML_RESPONSE_BYTES + 500_000)
        self.assertEqual(inspection["content_classification"], "confirmed_xml_feed")
        self.assertFalse(inspection["complete_body_downloaded"])

        confirmed = _xml_response("<rss><channel><title>Example Corp</title></channel></rss>")
        confirmed.headers["content-length"] = str(probe.MAX_XML_RESPONSE_BYTES + 1)
        content, accepted = probe._inspect_xml_response(
            confirmed, response_limit=probe.MAX_XML_RESPONSE_BYTES + 1
        )
        self.assertTrue(content.startswith(b"<rss>"))
        self.assertTrue(accepted["override_accepted"])
        self.assertTrue(accepted["complete_body_downloaded"])

    def test_non_xml_response_is_rejected_after_bounded_prefix(self) -> None:
        response = _response(
            "<html><head><title>Jobs</title></head><body>not a feed</body></html>",
            "https://example.com/jobs/feed.xml",
            "text/html; charset=utf-8",
        )
        response.headers["content-length"] = str(probe.MAX_XML_RESPONSE_OVERRIDE_BYTES)
        with self.assertRaises(probe.XMLInspectionError) as captured:
            probe._inspect_xml_response(
                response, response_limit=probe.MAX_XML_RESPONSE_OVERRIDE_BYTES
            )
        self.assertEqual(captured.exception.inspection["content_classification"], "html")
        self.assertFalse(captured.exception.inspection["override_accepted"])

    def test_truncated_xml_body_is_not_parsed_recoverably(self) -> None:
        with self.assertRaisesRegex(AdapterError, "malformed XML"):
            probe._safe_xml_root(b"<rss><channel><item>")

    def test_redirect_transactions_remain_host_restricted_and_counted(self) -> None:
        response = _xml_response("<rss/>")
        session = probe.BoundedProbeSession(
            allowed_hosts={"example.com"}, request_limit=3, phase_limits={"xml": 2}
        )
        session.set_phase("xml")
        first = requests.Request("GET", "https://example.com/feed.xml").prepare()
        redirected = requests.Request("GET", "https://evil.example/feed.xml").prepare()
        with patch.object(requests.Session, "send", return_value=response):
            session.send(first)
            with self.assertRaisesRegex(AdapterError, "approved source host"):
                session.send(redirected)
        self.assertEqual(session.requests_started, 1)
        self.assertEqual(session.phase_requests_started["xml"], 1)
        self.assertFalse(session.host_remained_approved)

    def test_population_summary_rejects_overlap_missing_and_mutation(self) -> None:
        partitions = [
            {
                "value": "ca",
                "stable_ids": ["1", "2"],
                "advertised_totals": [2],
                "failure_reason": None,
            },
            {
                "value": "us",
                "stable_ids": ["2", "3"],
                "advertised_totals": [2],
                "failure_reason": None,
            },
        ]
        overlap = probe.summarize_population_partitions(
            partitions,
            baseline_total=3,
            anchors_match=True,
            observed_dimension_values={"ca", "us"},
            absent_dimension_observed=False,
        )
        self.assertEqual(overlap["failure_reason"], "population_overlap")
        missing = probe.summarize_population_partitions(
            partitions[:1],
            baseline_total=2,
            anchors_match=True,
            observed_dimension_values={"ca", "us"},
            absent_dimension_observed=False,
        )
        self.assertEqual(missing["failure_reason"], "missing_observed_population_partitions")
        changed = probe.summarize_population_partitions(
            partitions[:1],
            baseline_total=2,
            anchors_match=False,
            observed_dimension_values={"ca"},
            absent_dimension_observed=False,
        )
        self.assertEqual(
            changed["failure_reason"], "collection_changed_during_partition_traversal"
        )

    def test_population_summary_requires_absent_coverage_and_consistent_totals(self) -> None:
        partition = {
            "value": "engineering",
            "stable_ids": ["1"],
            "advertised_totals": [1, 2],
            "failure_reason": None,
        }
        conflicting = probe.summarize_population_partitions(
            [partition],
            baseline_total=1,
            anchors_match=True,
            observed_dimension_values={"engineering"},
            absent_dimension_observed=False,
        )
        self.assertEqual(conflicting["failure_reason"], "conflicting_partition_totals")
        partition["advertised_totals"] = [1]
        absent = probe.summarize_population_partitions(
            [partition],
            baseline_total=1,
            anchors_match=True,
            observed_dimension_values={"engineering"},
            absent_dimension_observed=True,
        )
        self.assertEqual(absent["failure_reason"], "unfilterable_absent_dimension_records")

    def test_population_filter_contract_is_narrow(self) -> None:
        self.assertEqual(probe.parse_population_filter("country=de"), ("country", "de"))
        self.assertEqual(
            probe.parse_population_filter("department=123"), ("department", "123")
        )
        for invalid in ("region=ab", "country=", "country=not source observed"):
            with self.assertRaises(ValueError):
                probe.parse_population_filter(invalid)

    def test_population_summary_accepts_only_an_exact_complete_union(self) -> None:
        summary = probe.summarize_population_partitions(
            [
                {
                    "value": "ca",
                    "stable_ids": ["1"],
                    "advertised_totals": [1],
                    "failure_reason": None,
                },
                {
                    "value": "us",
                    "stable_ids": ["2"],
                    "advertised_totals": [1],
                    "failure_reason": None,
                },
            ],
            baseline_total=2,
            anchors_match=True,
            observed_dimension_values={"ca", "us"},
            absent_dimension_observed=False,
        )
        self.assertTrue(summary["global_population_complete"])
        self.assertIsNone(summary["failure_reason"])

    def test_partial_partition_and_unexpected_country_values_fail_closed(self) -> None:
        partial = probe.summarize_smartrecruiters_pages(
            [
                {
                    "stable_ids": ["1", "2"],
                    "advertised_total": 3,
                    "duplicate_ids_within_page": [],
                }
            ]
        )
        self.assertEqual(partial["failure_reason"], "authoritative_total_not_reached")
        summary = probe.summarize_population_partitions(
            [
                {
                    "value": "ca",
                    "stable_ids": ["1"],
                    "advertised_totals": [1],
                    "failure_reason": None,
                    "unexpected_partition_values": ["us"],
                    "absent_partition_value_count": 1,
                }
            ],
            baseline_total=1,
            anchors_match=True,
            observed_dimension_values={"ca"},
            absent_dimension_observed=False,
        )
        self.assertEqual(summary["failure_reason"], "unexpected_or_absent_partition_values")

    def test_offline_reconciliation_completes_only_exact_stable_union(self) -> None:
        ca = _partition_diagnostic(
            value="ca", ids=["1"], baseline_total=2, observed=["ca", "us"]
        )
        us = _partition_diagnostic(
            value="us", ids=["2"], baseline_total=2, observed=["ca", "us"]
        )
        summary = probe.reconcile_partition_diagnostics([ca, us], company="Bosch")
        self.assertTrue(summary["global_population_complete"])
        self.assertEqual(summary["aggregate_unique_ids"], 2)
        self.assertFalse(summary["observed_values_are_exhaustive_list"])

    def test_offline_reconciliation_rejects_overlap_mutation_and_total_contradiction(self) -> None:
        ca = _partition_diagnostic(
            value="ca", ids=["1"], baseline_total=2, observed=["ca", "us"]
        )
        overlap = _partition_diagnostic(
            value="us", ids=["1"], baseline_total=2, observed=["ca", "us"]
        )
        self.assertEqual(
            probe.reconcile_partition_diagnostics([ca, overlap], company="Bosch")["failure_reason"],
            "population_overlap",
        )
        mutated = _partition_diagnostic(
            value="us",
            ids=["2"],
            baseline_total=2,
            observed=["ca", "us"],
            anchor_ids=["changed"],
        )
        self.assertEqual(
            probe.reconcile_partition_diagnostics([ca, mutated], company="Bosch")["failure_reason"],
            "collection_changed_during_partition_traversal",
        )
        contradictory = _partition_diagnostic(
            value="us", ids=["2"], baseline_total=3, observed=["ca", "us"]
        )
        self.assertEqual(
            probe.reconcile_partition_diagnostics([ca, contradictory], company="Bosch")["failure_reason"],
            "aggregate_total_contradiction",
        )

    def test_resume_validation_skips_only_contiguous_incomplete_pages(self) -> None:
        ids = [str(index) for index in range(100)]
        payload = _partition_diagnostic(
            value="de",
            ids=ids,
            baseline_total=720,
            observed=["de"],
            partition_total=720,
        )
        pages, signature = probe._validated_resume_pages(
            payload, company="Bosch", dimension="country", value="de"
        )
        self.assertEqual([page["offset"] for page in pages], [0])
        self.assertEqual(len(signature), 64)

        payload["anchor_pages"][1]["stable_ids"] = ["changed"]
        with self.assertRaisesRegex(AdapterError, "inconsistent bracketing anchors"):
            probe._validated_resume_pages(
                payload, company="Bosch", dimension="country", value="de"
            )

    def test_completed_partition_is_never_re_requested_by_resume(self) -> None:
        payload = _partition_diagnostic(
            value="us", ids=["1"], baseline_total=1, observed=["us"]
        )
        with self.assertRaisesRegex(AdapterError, "no HTTP requests are needed"):
            probe._validated_resume_pages(
                payload, company="Bosch", dimension="country", value="us"
            )


if __name__ == "__main__":
    unittest.main()
