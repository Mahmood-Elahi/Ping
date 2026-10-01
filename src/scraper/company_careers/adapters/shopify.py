"""Shopify careers adapter.

Shopify's public careers page embeds job postings in React Router stream data.
The old `/api/jobs/search` endpoint no longer returns jobs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, List
from urllib.parse import parse_qs, urlparse, urlunparse

import requests

from src.scraper.company_careers.listing_completion import (
    MAX_RAW_JOBS_PER_SOURCE,
    record_terminal_evidence,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)


CAREERS_URL = "https://www.shopify.com/careers"
CAREERS_HOST = "www.shopify.com"
ROUTE_KEY = "($locale)/careers"
COLLECTION_KEY = "jobPostingsWithJobs"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
STREAM_RE = re.compile(
    r"window\.__reactRouterContext\.streamController\.enqueue\((\".*?\")\);</script>",
    re.S,
)


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if target.name != "Shopify" or target.platform != "shopify":
        raise AdapterError("Shopify adapter requires the exact Shopify target")
    response = session.get(
        CAREERS_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise AdapterError(f"Shopify HTTP {response.status_code}")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Shopify careers response was not HTML")
    if len(bytes(getattr(response, "content", b"") or b"")) > int(
        target.max_response_bytes
    ):
        raise AdapterError("Shopify careers response exceeded max_response_bytes")
    final_url = str(getattr(response, "url", "") or CAREERS_URL)
    if _canonical_careers_url(final_url) != CAREERS_URL:
        raise AdapterError("Shopify careers response left the exact canonical route")

    route, entries = _extract_atomic_collection(response.text)
    if len(entries) > MAX_RAW_JOBS_PER_SOURCE:
        raise AdapterError("Shopify atomic collection exceeded the hard job ceiling")
    _validate_route_identity(route)
    postings = [_validate_collection_entry(entry) for entry in entries]
    if not postings:
        raise AdapterError("Shopify careers page did not contain job postings")

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for posting in postings:
        job_id = _uuid(posting.get("id"), "posting ID")
        title = _clean(posting.get("title"))
        if not title:
            raise AdapterError("Shopify embedded collection has a job without a title")
        if job_id in seen_ids:
            raise AdapterError("Shopify embedded collection repeated a stable job ID")
        url = _job_url(posting.get("externalLink"), job_id)
        if _job_url(posting.get("applyLink"), job_id) != url:
            raise AdapterError("Shopify embedded collection has conflicting job URLs")
        if url in seen_urls:
            raise AdapterError("Shopify embedded collection repeated a canonical job URL")
        seen_ids.add(job_id)
        seen_urls.add(url)
        location = (
            _clean(posting.get("locationExternalName"))
            or _clean(posting.get("locationName"))
            or _clean(posting.get("workplaceType"))
        )
        department = _clean(posting.get("departmentName"))
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=target.name,
                location=location,
                url=url,
                updated_at=_clean(posting.get("updatedAt"))
                or _clean(posting.get("publishedDate")),
                source="custom:shopify",
                departments=[department] if department else [],
                offices=[location] if location else [],
                raw_metadata={
                    "platform": "shopify",
                    "endpoint_variant": "react_router_atomic_collection",
                    "collection_key": COLLECTION_KEY,
                    "posting_id": job_id,
                    "ats_job_id": _uuid(posting.get("jobId"), "ATS job ID"),
                },
            )
        )

    metadata = SourceScanMetadata(
        endpoint_variant="react_router_atomic_collection",
        request_method="GET",
        total_available=len(jobs),
        pages_requested=1,
        pages_completed=1,
        jobs_normalized=len(jobs),
        maximum_results=MAX_RAW_JOBS_PER_SOURCE,
        rejected_record_count=0,
        source_identity_status="validated",
        source_identity_diagnostic=(
            "The exact canonical Shopify careers route supplied the named "
            "jobPostingsWithJobs collection and every posting URL matched its UUID"
        ),
        scan_complete=True,
        scan_completeness_reason="complete_embedded_collection",
        stop_reason="complete_embedded_collection",
        complete_collection_response=True,
    )
    record_terminal_evidence(
        metadata,
        "complete_collection_response",
        COLLECTION_KEY,
        has_more=False,
        terminal_page_observed=True,
        complete_collection_response=True,
    )
    publish_source_scan_metadata(session, metadata)
    return jobs


def _extract_atomic_collection(document: str) -> tuple[Mapping[str, Any], list[Any]]:
    candidates: list[tuple[Mapping[str, Any], list[Any]]] = []
    for match in STREAM_RE.finditer(document):
        try:
            stream_payload = json.loads(match.group(1))
            encoded_values = json.loads(stream_payload)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"Could not decode Shopify embedded data: {exc}") from exc
        if not isinstance(encoded_values, list):
            raise AdapterError("Shopify embedded stream was not a devalue reference list")
        root = _decode_devalue(encoded_values)
        loader_data = root.get("loaderData") if isinstance(root, Mapping) else None
        route = loader_data.get(ROUTE_KEY) if isinstance(loader_data, Mapping) else None
        if isinstance(route, Mapping) and COLLECTION_KEY in route:
            collection = route.get(COLLECTION_KEY)
            if not isinstance(collection, list):
                raise AdapterError("Shopify atomic careers collection was not a list")
            candidates.append((route, collection))
    if len(candidates) != 1:
        raise AdapterError("Shopify careers page did not expose one exact atomic collection")
    return candidates[0]


def _extract_job_postings(html: str) -> List[dict]:
    match = STREAM_RE.search(html)
    if not match:
        return []

    try:
        stream_payload = json.loads(match.group(1))
        encoded_values = json.loads(stream_payload)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Could not decode Shopify embedded data: {exc}") from exc

    root = _decode_devalue(encoded_values)
    postings: List[dict] = []
    _walk_job_postings(root, postings)
    return postings


def _decode_devalue(values: list) -> Any:
    def decode_ref(index: int, seen: set[int]) -> Any:
        if index < 0:
            return None
        if index >= len(values):
            raise AdapterError("Shopify embedded data referenced an out-of-range value")
        if index in seen:
            return None
        seen.add(index)
        decoded = decode_value(values[index], seen)
        seen.remove(index)
        return decoded

    def decode_key(key: str, seen: set[int]) -> str:
        if re.fullmatch(r"_\d+", key):
            return str(decode_ref(int(key[1:]), seen))
        return key

    def decode_value(value: Any, seen: set[int]) -> Any:
        if isinstance(value, dict):
            return {
                decode_key(key, seen): decode_ref(item, seen)
                if isinstance(item, int) and not isinstance(item, bool)
                else decode_value(item, seen)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                decode_ref(item, seen)
                if isinstance(item, int) and not isinstance(item, bool)
                else decode_value(item, seen)
                for item in value
            ]
        return value

    if not values:
        raise AdapterError("Shopify embedded data was empty")
    return decode_ref(0, set())


def _walk_job_postings(value: Any, postings: List[dict]) -> None:
    if isinstance(value, dict):
        posting = value.get("jobPosting")
        if isinstance(posting, dict) and posting.get("title"):
            postings.append(posting)
        for item in value.values():
            _walk_job_postings(item, postings)
    elif isinstance(value, list):
        for item in value:
            _walk_job_postings(item, postings)


def _validate_route_identity(route: Mapping[str, Any]) -> None:
    if (
        _canonical_careers_url(route.get("url")) != CAREERS_URL
        or _canonical_careers_url(route.get("canonicalUrl")) != CAREERS_URL
        or route.get("rootDomain") != "shopify.com"
        or route.get("fileRoutePath") != "/careers"
        or route.get("handle") != "careers"
    ):
        raise AdapterError(
            "Shopify embedded collection did not match the canonical careers route"
        )
    forbidden = {
        "cursor", "nextCursor", "hasMore", "pagination", "page", "pageInfo",
        "totalPages", "query", "keyword", "filters",
    }
    if forbidden.intersection(route):
        raise AdapterError(
            "Shopify embedded careers collection exposed filter or pagination state"
        )


def _validate_collection_entry(entry: Any) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise AdapterError("Shopify atomic collection contained a malformed entry")
    job = entry.get("job")
    posting = entry.get("jobPosting")
    if not isinstance(job, Mapping) or not isinstance(posting, Mapping):
        raise AdapterError("Shopify atomic collection entry lacked job/posting objects")
    posting_id = _uuid(posting.get("id"), "posting ID")
    ats_job_id = _uuid(posting.get("jobId"), "ATS job ID")
    if _uuid(job.get("id"), "linked job ID") != ats_job_id:
        raise AdapterError("Shopify posting crossed its linked ATS job identity")
    posting_ids = job.get("jobPostingIds")
    if not isinstance(posting_ids, list) or posting_id not in posting_ids:
        raise AdapterError("Shopify linked ATS job did not corroborate the posting ID")
    if posting.get("isListed") is not True or posting.get("status") != "Published":
        raise AdapterError("Shopify atomic collection contained a non-published posting")
    return posting


def _canonical_careers_url(value: Any) -> str:
    parsed = urlparse(_clean(value))
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != CAREERS_HOST
        or parsed.path.rstrip("/") != "/careers"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return ""
    return CAREERS_URL


def _job_url(value: Any, posting_id: str) -> str:
    parsed = urlparse(_clean(value))
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != CAREERS_HOST
        or parsed.path.rstrip("/") != "/careers"
        or parsed.params
        or parsed.fragment
        or parsed.username
        or parsed.password
        or query != {"ashby_jid": [posting_id]}
    ):
        raise AdapterError("Shopify posting has no canonical job-specific URL")
    return urlunparse(parsed._replace(fragment=""))


def _uuid(value: Any, label: str) -> str:
    result = _clean(value)
    if not UUID_RE.fullmatch(result):
        raise AdapterError(f"Shopify embedded collection has an invalid {label}")
    return result


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())
