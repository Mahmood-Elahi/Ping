"""Meta Careers adapter."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


SEARCH_URL = "https://www.metacareers.com/jobsearch"
GRAPHQL_URL = "https://www.metacareers.com/api/graphql/"
JOBS_URL = "https://www.metacareers.com/jobs/{job_id}/"
DOC_ID = "29615178951461218"
FRIENDLY_NAME = "CareersJobSearchResultsDataQuery"
DEFAULT_SEARCH_TERMS = [
    "Principal Software Engineer",
    "Staff Software Engineer",
    "Production Engineer",
]


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()

    for term in search_terms:
        page_context = _fetch_search_context(session, term, timeout)
        for page in range(1, max(1, target.max_pages) + 1):
            payload = _fetch_results_page(session, term, page, page_context, timeout)
            page_jobs = _extract_jobs(payload, target.name)
            if not page_jobs:
                break

            for job in page_jobs:
                key = job.job_id or job.url or job.title
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(job)

    return jobs


def _fetch_search_context(session: requests.Session, term: str, timeout: int) -> Dict[str, str]:
    referer = f"{SEARCH_URL}?{urlencode({'q': term})}"
    response = session.get(
        referer,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise AdapterError(f"Meta Careers search HTTP {response.status_code}")

    lsd = _extract_pattern(response.text, r'\["LSD",\[\],\{"token":"([^"]+)"')
    if not lsd:
        raise AdapterError("Could not find Meta Careers LSD token")

    return {
        "referer": referer,
        "lsd": lsd,
        "client_revision": _extract_pattern(response.text, r'"client_revision":(\d+)') or "0",
        "hsi": _extract_pattern(response.text, r'"hsi":"(\d+)"') or "",
    }


def _fetch_results_page(
    session: requests.Session,
    term: str,
    page: int,
    context: Dict[str, str],
    timeout: int,
) -> Dict[str, Any]:
    search_input = {
        "q": term,
        "divisions": [],
        "offices": [],
        "roles": [],
        "leadership_levels": [],
        "saved_jobs": [],
        "saved_searches": [],
        "sub_teams": [],
        "teams": [],
        "is_leadership": False,
        "is_remote_only": False,
        "sort_by_new": False,
        "page": page,
    }
    data = {
        "av": "0",
        "__user": "0",
        "__a": "1",
        "__req": str(page),
        "__rev": context["client_revision"],
        "__hsi": context["hsi"],
        "__ccg": "GOOD",
        "__comet_req": "15",
        "lsd": context["lsd"],
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": FRIENDLY_NAME,
        "variables": json.dumps({"search_input": search_input}, separators=(",", ":")),
        "server_timestamps": "true",
        "doc_id": DOC_ID,
    }
    response = session.post(
        GRAPHQL_URL,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.metacareers.com",
            "Referer": context["referer"],
            "X-FB-LSD": context["lsd"],
            "X-FB-Friendly-Name": FRIENDLY_NAME,
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise AdapterError(f"Meta Careers GraphQL HTTP {response.status_code}")

    return _decode_graphql_payload(response.text)


def _extract_jobs(payload: Dict[str, Any], company: str) -> List[JobRecord]:
    job_search = (payload.get("data") or {}).get("job_search_with_featured_jobs") or {}
    items = job_search.get("all_jobs") or []
    if not isinstance(items, list):
        raise AdapterError("Meta Careers response did not include an all_jobs list")

    jobs: List[JobRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            continue

        locations = _clean_strings(item.get("locations"))
        teams = _clean_strings(item.get("teams"))
        sub_teams = _clean_strings(item.get("sub_teams"))
        description = _summary(teams, sub_teams)
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=company,
                location=", ".join(locations),
                url=JOBS_URL.format(job_id=job_id),
                updated_at="",
                source="custom:meta",
                departments=teams,
                offices=locations,
                description=description,
            )
        )

    return jobs


def _decode_graphql_payload(text: str) -> Dict[str, Any]:
    body = text.strip()
    if body.startswith("for (;;);"):
        body = body[len("for (;;);") :].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Could not decode Meta Careers response: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterError("Meta Careers response was not a JSON object")
    if payload.get("errors"):
        raise AdapterError(f"Meta Careers GraphQL error: {payload['errors']}")
    return payload


def _extract_pattern(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _clean_strings(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _summary(teams: List[str], sub_teams: List[str]) -> Optional[str]:
    parts = []
    if teams:
        parts.append(f"Teams: {', '.join(teams)}")
    if sub_teams:
        parts.append(f"Sub-teams: {', '.join(sub_teams)}")
    return "; ".join(parts) if parts else None
