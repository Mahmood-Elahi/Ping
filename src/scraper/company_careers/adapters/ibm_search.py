"""IBM Careers search adapter used for HashiCorp roles after the IBM acquisition."""

from __future__ import annotations

import re
from typing import Any, List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


SEARCH_URL = "https://www-api.ibm.com/search/api/v2"
PAGE_SIZE = 100
DEFAULT_SEARCH_TERMS = ["hashicorp"]


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()

    for term in search_terms:
        for page in range(max(1, target.max_pages)):
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

            items = ((response.json().get("hits") or {}).get("hits") or [])
            if not items:
                break

            for hit in items:
                source = hit.get("_source") if isinstance(hit, dict) else {}
                if not isinstance(source, dict):
                    continue
                job_id = _job_id(source)
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                location = str(source.get("field_keyword_19") or "")
                jobs.append(
                    JobRecord(
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
                )

            if len(items) < PAGE_SIZE:
                break

    return jobs


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
