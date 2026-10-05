from __future__ import annotations

from dataclasses import replace

import pytest

from scripts import validate_source_integrity_canaries as canaries
from src.scraper.company_careers.adapters import source_xml
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.source_health import finalize_source_scan


class XMLResponse:
    def __init__(
        self,
        content: bytes,
        *,
        url: str = "https://careers.example.com/jobs/xml/?rss=true",
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunk_size: int | None = None,
    ) -> None:
        self.content = content
        self.url = url
        self.status_code = status
        self.headers = {"content-type": "text/xml", **(headers or {})}
        self.history: list[object] = []
        self.closed = False
        self.chunk_size = chunk_size

    def iter_content(self, chunk_size: int = 1):
        size = self.chunk_size or chunk_size
        for offset in range(0, len(self.content), size):
            yield self.content[offset : offset + size]

    def close(self) -> None:
        self.closed = True


class XMLSession:
    def __init__(self, response: XMLResponse | list[XMLResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.response = self.responses[0]
        self._index = 0
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> XMLResponse:
        self.calls.append((url, kwargs))
        response = self.responses[min(self._index, len(self.responses) - 1)]
        self._index += 1
        return response


def target(**changes: object) -> CompanyTarget:
    base = CompanyTarget(
        name="Example Corp",
        platform="source_xml",
        careers_url="https://careers.example.com/jobs/",
        listing_url="https://careers.example.com/jobs/",
        endpoint_url="https://careers.example.com/jobs/xml/?rss=true",
        endpoint_variant=source_xml.ENDPOINT_VARIANT,
        identity_regex=r"^Example Corp$",
        id_field="requisitionid",
        title_field="title",
        url_field="url",
        date_field="date",
        location_fields=["location", "city", "state", "country"],
        complete_collection_total_field="totalResults",
        allowed_source_hosts=["careers.example.com"],
        allowed_apply_hosts=["careers.example.com"],
        max_pages=1,
        max_jobs=100,
        max_response_bytes=10_000,
    )
    return replace(base, **changes)


def job(
    requisition: str = "REQ-101",
    title: str = "Controls Engineer",
    url: str = "https://careers.example.com/jobs/controls-engineer-101?utm_source=feed",
) -> str:
    return f"""
    <job>
      <title><![CDATA[{title}]]></title>
      <date>Sun, 04 Oct 2026 07:00:00 GMT</date>
      <requisitionid>{requisition}</requisitionid>
      <url><![CDATA[{url}]]></url>
      <city>Calgary</city><state>Alberta</state><country>Canada</country>
    </job>
    """


def feed(
    jobs: str = "",
    *,
    publisher: str = "Example Corp",
    metadata: str = "",
) -> bytes:
    return (
        f"<?xml version='1.0' encoding='utf-8'?><source>"
        f"<publisher>{publisher}</publisher>"
        f"<publisherUrl>https://www.example.com/</publisherUrl>"
        f"<lastBuildDate>Sun, 04 Oct 2026 22:07:13 GMT</lastBuildDate>"
        f"{metadata}{jobs}</source>"
    ).encode()


def listing(
    total: int | None,
    *,
    title: str = "Jobs | Example Corp Careers",
    url: str = "https://careers.example.com/jobs/",
) -> XMLResponse:
    count = f"<h1>{total:,} Jobs</h1>" if total is not None else "<h1>Jobs</h1>"
    payload = f"<html><head><title>{title}</title></head><body>{count}</body></html>".encode()
    return XMLResponse(
        payload,
        url=url,
        headers={"content-type": "text/html; charset=utf-8"},
    )


def corroborated_target(**changes: object) -> CompanyTarget:
    return target(
        complete_collection_total_field=None,
        listing_identity_regex=r"^Jobs \| Example Corp Careers$",
        listing_total_regex=r"(?i)\b([\d,]+)\s+jobs?\b",
        **changes,
    )


def run_corroborated(
    payload: bytes,
    before: XMLResponse,
    after: XMLResponse,
    configured: CompanyTarget | None = None,
):
    xml = XMLResponse(payload)
    session = XMLSession([before, xml, after])
    jobs = source_xml.fetch_jobs(session, configured or corroborated_target())
    return jobs, source_scan_metadata(session), session


def run(payload: bytes, configured: CompanyTarget | None = None, **response: object):
    reply = XMLResponse(payload, **response)
    session = XMLSession(reply)
    jobs = source_xml.fetch_jobs(session, configured or target())
    return jobs, source_scan_metadata(session), reply


def test_valid_source_job_extraction_is_incomplete_without_authoritative_total():
    jobs, metadata, _response = run(feed(job()))

    assert len(jobs) == 1
    assert jobs[0].job_id == "source_xml:careers.example.com:REQ-101"
    assert jobs[0].title == "Controls Engineer"
    assert jobs[0].url == "https://careers.example.com/jobs/controls-engineer-101"
    assert jobs[0].location == "Calgary | Alberta | Canada"
    assert jobs[0].updated_at == "Sun, 04 Oct 2026 07:00:00 GMT"
    assert jobs[0].raw_metadata["publisher"] == "Example Corp"
    assert metadata["source_identity_status"] == "validated"
    assert metadata["response_fully_read"] is True
    assert metadata["scan_complete"] is False
    assert metadata["scan_completeness_reason"] == "collection_completeness_not_proven"


def test_matching_authoritative_total_proves_complete_collection():
    jobs, metadata, _response = run(feed(job(), metadata="<totalResults>1</totalResults>"))
    finalize_source_scan(metadata, jobs_collected=len(jobs))

    assert metadata["scan_complete"] is True
    assert metadata["scan_state"] == "complete"
    assert metadata["source_health_code"] == "healthy"
    assert metadata["terminal_evidence_type"] == "authoritative_total"
    assert metadata["terminal_evidence_value"] == 1
    assert metadata["unique_stable_ids"] == 1
    assert metadata["unique_canonical_urls"] == 1


def test_only_the_exact_configured_root_count_field_is_authoritative():
    jobs, metadata, _response = run(
        feed(job(), metadata="<expectedJobCount>1</expectedJobCount>"),
        target(complete_collection_total_field="expectedJobCount"),
    )
    finalize_source_scan(metadata, jobs_collected=len(jobs))
    assert metadata["scan_complete"] is True
    assert metadata["total_available"] == 1

    jobs, metadata, _response = run(
        feed(job(), metadata="<expectedJobCount>1</expectedJobCount>")
    )
    assert metadata["scan_complete"] is False
    assert metadata["scan_completeness_reason"] == "collection_completeness_not_proven"


def test_publisher_mismatch_fails_exact_source_identity():
    session = XMLSession(XMLResponse(feed(job(), publisher="Other Corp")))
    with pytest.raises(AdapterError, match="publisher identity mismatch"):
        source_xml.fetch_jobs(session, target())
    assert source_scan_metadata(session)["source_identity_status"] == "failed"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (feed(job(requisition="")), "malformed required"),
        (
            feed(job(url="http://careers.example.com/jobs/101")),
            "malformed required",
        ),
        (
            feed(job(url="https://evil.example/jobs/101")),
            "malformed required",
        ),
    ],
)
def test_missing_stable_ids_and_invalid_job_urls_fail_the_whole_feed(
    payload: bytes, message: str
):
    session = XMLSession(XMLResponse(payload))
    with pytest.raises(AdapterError, match=message):
        source_xml.fetch_jobs(session, target())
    metadata = source_scan_metadata(session)
    assert metadata["rejected_record_count"] == 1
    assert metadata["jobs_normalized"] == 0


@pytest.mark.parametrize(
    "second",
    [
        job(requisition="REQ-101", title="Another title", url="https://careers.example.com/jobs/other-102"),
        job(requisition="REQ-102", title="Another title"),
    ],
)
def test_duplicate_stable_ids_or_canonical_urls_fail_closed(second: str):
    session = XMLSession(XMLResponse(feed(job() + second)))
    with pytest.raises(AdapterError, match="duplicate stable IDs or canonical"):
        source_xml.fetch_jobs(session, target())
    metadata = source_scan_metadata(session)
    assert metadata["scan_complete"] is False
    assert metadata["rejected_record_count"] > 0


def test_coupang_uses_canonical_posting_identity_and_retains_requisition():
    configured = target(external_id_regex=r"/jobs/([0-9]+)/")
    payload = feed(
        job(
            requisition="R0075090",
            url="https://careers.example.com/jobs/8020308/one?gh_jid=8020308",
        )
        + job(
            requisition="R0075090",
            title="Principal, Data Governance - Taiwan",
            url="https://careers.example.com/jobs/8123456/two?gh_jid=8123456",
        ),
        metadata="<totalResults>2</totalResults>",
    )
    jobs, metadata, _response = run(payload, configured)
    assert [item.job_id.rsplit(":", 1)[-1] for item in jobs] == ["8020308", "8123456"]
    assert {item.raw_metadata["requisition_id"] for item in jobs} == {"R0075090"}
    assert all(item.raw_metadata["posting_identity_source"] == "canonical_url" for item in jobs)
    assert metadata["duplicate_ids"] == []


def test_canonical_posting_identity_collisions_remain_fail_closed():
    configured = target(external_id_regex=r"/jobs/([0-9]+)/")
    duplicate_url = "https://careers.example.com/jobs/8020308/one?gh_jid=8020308"
    session = XMLSession(
        XMLResponse(
            feed(
                job(requisition="R1", url=duplicate_url)
                + job(requisition="R2", title="Other", url=duplicate_url)
            )
        )
    )
    with pytest.raises(AdapterError, match="duplicate stable IDs or canonical"):
        source_xml.fetch_jobs(session, configured)


def test_conflicting_canonical_posting_identity_is_rejected():
    configured = target(external_id_regex=r"/jobs/([0-9]+)/")
    session = XMLSession(
        XMLResponse(
            feed(
                job(
                    url="https://careers.example.com/jobs/8020308/one?gh_jid=9999999"
                )
            )
        )
    )
    with pytest.raises(AdapterError, match="malformed required"):
        source_xml.fetch_jobs(session, configured)
    assert source_scan_metadata(session)["rejected_record_count"] == 1


def test_malformed_and_truncated_xml_are_not_recovered():
    malformed = XMLSession(XMLResponse(b"<source><publisher>Example Corp</publisher><job>"))
    with pytest.raises(AdapterError, match="malformed XML"):
        source_xml.fetch_jobs(malformed, target())
    assert source_scan_metadata(malformed)["response_truncated"] is True

    payload = feed(job())
    truncated = XMLSession(
        XMLResponse(payload, headers={"content-length": str(len(payload) + 20)})
    )
    with pytest.raises(AdapterError, match="truncated"):
        source_xml.fetch_jobs(truncated, target())
    assert source_scan_metadata(truncated)["stop_reason"] == "truncated_xml_response"


def test_oversized_responses_stop_streaming_at_the_hard_limit():
    configured = target(max_response_bytes=100)
    response = XMLResponse(feed(job()), chunk_size=60)
    session = XMLSession(response)
    with pytest.raises(AdapterError, match="safety limit exceeded"):
        source_xml.fetch_jobs(session, configured)
    metadata = source_scan_metadata(session)
    assert metadata["safety_limit_reached"] is True
    assert metadata["response_fully_read"] is False
    assert response.closed is True


def test_response_size_boundary_is_inclusive_and_one_byte_over_is_rejected():
    payload = feed(job(), metadata="<totalResults>1</totalResults>")
    jobs, metadata, _response = run(payload, target(max_response_bytes=len(payload)))
    assert len(jobs) == 1
    assert metadata["response_fully_read"] is True

    session = XMLSession(XMLResponse(payload))
    with pytest.raises(AdapterError, match="safety limit exceeded"):
        source_xml.fetch_jobs(session, target(max_response_bytes=len(payload) - 1))


def test_listing_response_size_boundary_is_also_fail_closed():
    response = listing(1)
    payload_size = len(response.content)
    metadata = source_scan_metadata(XMLSession(response))
    assert source_xml._listing_response_bytes(
        response, target(max_response_bytes=payload_size), metadata
    ) == response.content

    oversized = listing(1)
    with pytest.raises(AdapterError, match="listing exceeded response size limit"):
        source_xml._listing_response_bytes(
            oversized, target(max_response_bytes=payload_size - 1), metadata
        )
    assert oversized.closed is True


def test_empty_feeds_are_explicit_and_only_zero_total_is_complete():
    jobs, incomplete, _response = run(feed())
    assert jobs == []
    assert incomplete["scan_complete"] is False
    assert incomplete["stop_reason"] == "empty_collection_without_authoritative_total"

    jobs, complete, _response = run(feed(metadata="<totalResults>0</totalResults>"))
    finalize_source_scan(complete, jobs_collected=len(jobs))
    assert jobs == []
    assert complete["scan_complete"] is True
    assert complete["total_available"] == 0


def test_partial_total_and_conflicting_pagination_remain_incomplete():
    jobs, partial, _response = run(feed(job(), metadata="<totalResults>2</totalResults>"))
    finalize_source_scan(partial, jobs_collected=len(jobs))
    assert partial["scan_complete"] is False
    assert partial["source_health_code"] == "authoritative_total_not_reached"

    conflict_metadata = (
        "<totalResults>1</totalResults><hasMore>false</hasMore>"
        "<nextUrl>https://careers.example.com/jobs/xml/?page=2</nextUrl>"
    )
    jobs, conflict, _response = run(feed(job(), metadata=conflict_metadata))
    finalize_source_scan(conflict, jobs_collected=len(jobs))
    assert conflict["scan_complete"] is False
    assert conflict["pagination_evidence_conflicting"] is True
    assert conflict["source_health_code"] == "terminal_contradiction"


def test_same_run_listing_total_agreement_proves_completion():
    jobs, metadata, session = run_corroborated(
        feed(job()), listing(1), listing(1)
    )
    finalize_source_scan(metadata, jobs_collected=len(jobs))
    assert len(session.calls) == 3
    assert metadata["scan_complete"] is True
    assert metadata["scan_state"] == "complete"
    assert metadata["terminal_evidence_type"] == "authoritative_total"
    assert metadata["authoritative_total_source"] == (
        "same_run_official_unfiltered_listing_corroboration"
    )
    assert metadata["listing_population_status"] == "validated_unfiltered"


def test_listing_and_feed_total_disagreement_remains_incomplete():
    jobs, metadata, _session = run_corroborated(
        feed(job()), listing(2), listing(2)
    )
    finalize_source_scan(metadata, jobs_collected=len(jobs))
    assert metadata["scan_complete"] is False
    assert metadata["source_health_code"] == "authoritative_total_not_reached"
    assert metadata["total_available"] == 2


def test_changed_listing_totals_are_collection_mutation():
    jobs, metadata, _session = run_corroborated(
        feed(job()), listing(1), listing(2)
    )
    finalize_source_scan(metadata, jobs_collected=len(jobs))
    assert metadata["scan_complete"] is False
    assert metadata["source_health_code"] == "collection_changed"
    assert metadata["collection_changed_during_scan"] is True


def test_missing_listing_count_remains_incomplete():
    jobs, metadata, _session = run_corroborated(
        feed(job()), listing(None), listing(None)
    )
    finalize_source_scan(metadata, jobs_collected=len(jobs))
    assert metadata["scan_complete"] is False
    assert metadata["scan_completeness_reason"] == "collection_completeness_not_proven"


def test_wrong_listing_publisher_and_filtered_listing_fail_closed():
    wrong = listing(1, title="Jobs | Other Corp Careers")
    session = XMLSession([wrong, XMLResponse(feed(job())), wrong])
    with pytest.raises(AdapterError, match="listing identity mismatch"):
        source_xml.fetch_jobs(session, corroborated_target())

    with pytest.raises(AdapterError, match="must be unfiltered"):
        source_xml.validate_target(
            corroborated_target(listing_url="https://careers.example.com/jobs/?city=Calgary")
        )

    filtered = listing(1, url="https://careers.example.com/jobs/?city=Calgary")
    session = XMLSession([filtered, XMLResponse(feed(job())), filtered])
    with pytest.raises(AdapterError, match="exact unfiltered URL"):
        source_xml.fetch_jobs(session, corroborated_target())


def test_conflicting_or_malformed_total_metadata_never_completes():
    for metadata_xml in (
        "<totalResults>1</totalResults><totalResults>2</totalResults>",
        "<totalResults>many</totalResults>",
    ):
        jobs, metadata, _response = run(feed(job(), metadata=metadata_xml))
        finalize_source_scan(metadata, jobs_collected=len(jobs))
        assert metadata["scan_complete"] is False
        assert metadata["source_health_code"] in {
            "terminal_contradiction",
            "malformed_collection",
        }


def test_dtds_entities_and_unsupported_source_identity_are_rejected():
    payload = (
        b"<?xml version='1.0'?><!DOCTYPE source [<!ENTITY xxe SYSTEM 'file:///x'>]>"
        b"<source><publisher>Example Corp</publisher>&xxe;</source>"
    )
    session = XMLSession(XMLResponse(payload))
    with pytest.raises(AdapterError, match="DTD and entity"):
        source_xml.fetch_jobs(session, target())

    with pytest.raises(AdapterError, match="identity_regex must be exact"):
        source_xml.validate_target(target(identity_regex="Example Corp"))
    with pytest.raises(AdapterError, match="missing identity_regex"):
        source_xml.validate_target(target(identity_regex=None))


def test_three_reviewed_target_fixtures_share_the_bounded_xml_contract():
    expected = {
        "General Motors": {
            "host": "search-careers.gm.com",
            "identity_regex": r"^General Motors$",
            "external_id_regex": None,
        },
        "Caterpillar": {
            "host": "careers.caterpillar.com",
            "identity_regex": r"^Caterpillar$",
            "external_id_regex": None,
        },
        "Coupang": {
            "host": "www.coupang.jobs",
            "identity_regex": r"^Coupang$",
            "external_id_regex": r"/en/jobs/([0-9]+)/",
        },
    }
    targets = {}
    for name, contract in expected.items():
        host = contract["host"]
        listing_url = f"https://{host}/en/jobs/"
        targets[name] = target(
            name=name,
            careers_url=listing_url,
            listing_url=listing_url,
            endpoint_url=f"{listing_url}xml/?rss=true",
            endpoint_variant=source_xml.ENDPOINT_VARIANT,
            identity_regex=contract["identity_regex"],
            id_field="requisitionid",
            title_field="title",
            url_field="url",
            complete_collection_total_field=None,
            listing_identity_regex=rf"^Jobs \| {name} Careers$",
            listing_total_regex=r"(?i)\b([\d,]+)\s+jobs?\b",
            allowed_source_hosts=[host],
            allowed_apply_hosts=[host],
            max_pages=1,
            max_response_bytes=source_xml.MAX_RESPONSE_BYTES,
            external_id_regex=contract["external_id_regex"],
        )

    for name, contract in expected.items():
        host = contract["host"]
        configured = targets[name]
        source_xml.validate_target(configured)
        assert configured.platform == "source_xml"
        assert configured.endpoint_url == f"https://{host}/en/jobs/xml/?rss=true"
        assert configured.endpoint_variant == source_xml.ENDPOINT_VARIANT
        assert configured.identity_regex == contract["identity_regex"]
        assert configured.id_field == "requisitionid"
        assert configured.title_field == "title"
        assert configured.url_field == "url"
        assert configured.allowed_source_hosts == [host]
        assert configured.allowed_apply_hosts == [host]
        assert configured.max_pages == 1
        assert configured.max_response_bytes == source_xml.MAX_RESPONSE_BYTES
        assert configured.complete_collection_total_field is None
        assert configured.listing_identity_regex
        assert configured.listing_total_regex
        assert configured.external_id_regex == contract["external_id_regex"]


def test_valid_but_non_authoritative_xml_is_a_partial_canary_not_healthy():
    jobs, metadata, _response = run(feed(job()))
    finalize_source_scan(metadata, jobs_collected=len(jobs))

    assert metadata["scan_state"] == "incomplete"
    assert metadata["source_health_code"] == "pagination_unproven"
    assert canaries._incomplete_outcome(metadata) == "PASS_PARTIAL"
