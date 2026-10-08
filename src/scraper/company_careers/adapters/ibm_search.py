"""IBM Careers search adapter used for HashiCorp roles after the IBM acquisition."""

from __future__ import annotations

import math
import re
from typing import Any, List

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence


SEARCH_URL = "https://www-api.ibm.com/search/api/v2"
PAGE_SIZE = 100
DEFAULT_SEARCH_TERMS = ["hashicorp"]


@explicit_adapter_result
def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    by_id: dict[str, JobRecord] = {}
    metadata = SourceScanMetadata(
        endpoint_variant="search_terms_offset_v2",
        page_size=PAGE_SIZE,
        maximum_pages=max(1, target.max_pages),
    )
    publish_source_scan_metadata(session, metadata)
    safety_limited = False
    extra_request_allowance = 10
    source_request_ceiling = len(search_terms) * max(1, target.max_pages) + extra_request_allowance

    for term in search_terms:
        term_complete = False
        observed_total: int | None = None
        normal_pages = max(1, target.max_pages)
        for page in range(normal_pages + extra_request_allowance):
            if page >= normal_pages:
                required_pages = (
                    math.ceil(observed_total / PAGE_SIZE)
                    if observed_total is not None
                    else None
                )
                if (
                    required_pages is None
                    or required_pages > normal_pages + extra_request_allowance
                    or metadata["pages_requested"] >= source_request_ceiling
                ):
                    break
            metadata["pages_requested"] += 1
            response = session.post(
                SEARCH_URL,
                json=_request_body(term, page),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Referer": "https://www.ibm.com/",
                },
                timeout=timeout,
            )
            if response.status_code != 200:
                raise AdapterError(f"IBM Careers search HTTP {response.status_code}")

            items, current_total = _listing(response.json())
            metadata["pages_completed"] += 1
            if current_total is not None:
                if observed_total is not None and current_total != observed_total:
                    raise AdapterError(
                        "IBM Careers authoritative total changed during pagination"
                    )
                observed_total = current_total
                if len(search_terms) == 1:
                    metadata["total_available"] = current_total
                    metadata["authoritative_total_present"] = True
            if not items:
                term_complete = True
                break

            for hit in items:
                source = hit.get("_source") if isinstance(hit, dict) else {}
                if not isinstance(source, dict):
                    raise AdapterError("IBM Careers returned a malformed search hit")
                job_id = _job_id(source)
                if not job_id:
                    raise AdapterError("IBM Careers returned a job without a stable ID")
                location = str(source.get("field_keyword_19") or "")
                job = JobRecord(
                        job_id=job_id,
                        title=str(source.get("title") or ""),
                        company=target.name,
                        location=location,
                        url=str(source.get("url") or ""),
                        updated_at="",
                        source="custom:ibm_search",
                        departments=[str(source.get("field_keyword_08"))]
                        if source.get("field_keyword_08")
                        else [],
                        offices=[location] if location else [],
                        description=source.get("description"),
                    )
                if job_id in seen:
                    if by_id[job_id].to_dict() != job.to_dict():
                        raise AdapterError("IBM Careers returned a conflicting duplicate stable ID")
                    continue
                seen.add(job_id)
                by_id[job_id] = job
                jobs.append(job)

            observed_results = page * PAGE_SIZE + len(items)
            if observed_total is not None and observed_results >= observed_total:
                term_complete = True
                break
            if len(items) < PAGE_SIZE:
                term_complete = True
                break
        if not term_complete:
            safety_limited = True

    if not safety_limited:
        metadata["total_available"] = len(jobs)
        record_terminal_evidence(
            metadata,
            "authoritative_total_or_short_page",
            "all_search_terms_exhausted",
            has_more=False,
            terminal_page_observed=True,
        )
    metadata["scan_complete"] = not safety_limited
    metadata["scan_completeness_reason"] = (
        "safety_limit_reached" if safety_limited else "all_search_terms_exhausted"
    )
    metadata["stop_reason"] = (
        "maximum_pages_reached" if safety_limited else "all_search_terms_exhausted"
    )
    metadata["safety_limit_reached"] = safety_limited
    return jobs


def _listing(payload: Any) -> tuple[list[Any], int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), dict):
        raise AdapterError("IBM Careers search response is missing its hits object")
    hits = payload["hits"]
    items = hits.get("hits")
    if not isinstance(items, list):
        raise AdapterError("IBM Careers search response is missing its hits list")
    raw_total = hits.get("total")
    if raw_total is None:
        return items, None
    if isinstance(raw_total, dict):
        if raw_total.get("relation", "eq") != "eq":
            return items, None
        raw_total = raw_total.get("value")
    if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total < 0:
        raise AdapterError("IBM Careers hits.total must be a non-negative integer")
    return items, raw_total


def _request_body(term: str, page: int) -> dict:
    return {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {
            "bool": {
                "must": [
                    {
                        "simple_query_string": {
                            "query": term,
                            "fields": [
                                "keywords^1",
                                "body^1",
                                "url^2",
                                "description^2",
                                "h1s_content^2",
                                "title^3",
                                "field_text_01",
                            ],
                        }
                    }
                ]
            }
        },
        "from": page * PAGE_SIZE,
        "size": PAGE_SIZE,
        "sort": [{"_score": "desc"}, {"pageviews": "desc"}],
        "lang": "zz",
        "localeSelector": {},
        "sm": {"query": term, "lang": "zz"},
        "_source": [
            "_id",
            "title",
            "url",
            "description",
            "language",
            "entitled",
            "field_keyword_17",
            "field_keyword_08",
            "field_keyword_18",
            "field_keyword_19",
        ],
    }


def _job_id(source: dict[str, Any]) -> str:
    url = str(source.get("url") or "")
    match = re.search(r"[?&]jobId=(\d+)", url)
    if match:
        return match.group(1)
    return str(source.get("_id") or url or source.get("title") or "")
