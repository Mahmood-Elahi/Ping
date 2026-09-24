"""Google Careers adapter."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, List, Optional

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import record_terminal_evidence


SEARCH_URL = "https://www.google.com/about/careers/applications/jobs/results/"
DEFAULT_SEARCH_TERMS = [
    "Principal Engineer",
    "Staff Software Engineer",
    "Senior Staff",
]


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    search_terms = target.search_terms or DEFAULT_SEARCH_TERMS
    jobs: List[JobRecord] = []
    seen: set[str] = set()
    by_id: dict[str, JobRecord] = {}
    metadata = SourceScanMetadata(
        endpoint_variant="search_terms_page_v1",
        maximum_pages=max(1, target.max_pages),
    )
    publish_source_scan_metadata(session, metadata)
    safety_limited = False

    for term in search_terms:
        term_complete = False
        for page in range(1, max(1, target.max_pages) + 1):
            metadata["pages_requested"] += 1
            response = session.get(
                SEARCH_URL,
                params={"q": term, "page": page},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=timeout,
            )
            if response.status_code != 200:
                raise AdapterError(f"Google Careers HTTP {response.status_code}")

            page_jobs = _extract_jobs(response.text, target.name)
            metadata["pages_completed"] += 1
            if not page_jobs:
                term_complete = True
                break

            for job in page_jobs:
                key = job.job_id or job.url or job.title
                if key in seen:
                    if by_id[key].to_dict() != job.to_dict():
                        raise AdapterError("Google Careers returned a conflicting duplicate stable ID")
                    continue
                seen.add(key)
                by_id[key] = job
                jobs.append(job)
        if not term_complete:
            safety_limited = True

    metadata["total_available"] = len(jobs) if not safety_limited else None
    metadata["scan_complete"] = not safety_limited
    metadata["scan_completeness_reason"] = (
        "safety_limit_reached" if safety_limited else "all_search_terms_exhausted"
    )
    metadata["stop_reason"] = (
        "maximum_pages_reached" if safety_limited else "all_search_terms_exhausted"
    )
    metadata["safety_limit_reached"] = safety_limited
    record_terminal_evidence(
        metadata,
        "search_terms_exhausted" if not safety_limited else "search_terms_incomplete",
        not safety_limited,
        has_more=safety_limited,
        terminal_page_observed=not safety_limited,
    )
    return jobs


def _extract_jobs(html: str, company: str) -> List[JobRecord]:
    data = _extract_af_data(html, "ds:1")
    if not data or not isinstance(data, list) or not data[0]:
        return []

    jobs = []
    for item in data[0]:
        if not isinstance(item, list) or len(item) < 10:
            raise AdapterError("Google Careers data contained a malformed job row")
        job_id = str(item[0] or "")
        title = str(item[1] or "")
        url = str(item[2] or "")
        if not job_id or not title or not url:
            raise AdapterError("Google Careers job row lacked identity, title, or URL")
        locations = _locations(item[9])
        description = _text_from_html(_safe_nested_text(item, 10) or _safe_nested_text(item, 3))
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=title,
                company=company,
                location=", ".join(locations),
                url=url,
                updated_at=_timestamp_to_iso(_safe_get(item, 14)) or _timestamp_to_iso(_safe_get(item, 13)),
                source="custom:google",
                departments=[str(item[7])] if len(item) > 7 and item[7] else [],
                offices=locations,
                description=description,
            )
        )
    return jobs


def _extract_af_data(html: str, key: str) -> Optional[Any]:
    marker = f"AF_initDataCallback({{key: '{key}'"
    marker_index = html.find(marker)
    if marker_index < 0:
        return None

    data_index = html.find("data:", marker_index)
    if data_index < 0:
        return None

    start = data_index + len("data:")
    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] != "[":
        return None

    end = _balanced_json_array_end(html, start)
    if end is None:
        return None

    try:
        return json.loads(html[start:end])
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Could not decode Google Careers data: {exc}") from exc


def _balanced_json_array_end(text: str, start: int) -> Optional[int]:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return index + 1
    return None


def _locations(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    locations = []
    for item in value:
        if isinstance(item, list) and item and item[0]:
            locations.append(str(item[0]))
    return locations


def _safe_get(item: list, index: int) -> Any:
    return item[index] if len(item) > index else None


def _safe_nested_text(item: list, index: int) -> str:
    value = _safe_get(item, index)
    if isinstance(value, list) and len(value) > 1 and value[1]:
        return str(value[1])
    return ""


def _timestamp_to_iso(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    try:
        seconds = int(value[0])
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _text_from_html(value: str) -> str:
    text = unescape(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:li|p|h3|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()
