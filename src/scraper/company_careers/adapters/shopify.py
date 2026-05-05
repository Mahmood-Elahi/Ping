"""Shopify careers adapter.

Shopify's public careers page embeds job postings in React Router stream data.
The old `/api/jobs/search` endpoint no longer returns jobs.
"""

from __future__ import annotations

import json
import re
from typing import Any, List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


CAREERS_URL = "https://www.shopify.com/careers"
STREAM_RE = re.compile(
    r"window\.__reactRouterContext\.streamController\.enqueue\((\".*?\")\);</script>",
    re.S,
)


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
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

    postings = _extract_job_postings(response.text)
    if not postings:
        raise AdapterError("Shopify careers page did not contain job postings")

    jobs = []
    for posting in postings:
        if not posting.get("isListed", True):
            continue
        location = (
            posting.get("locationExternalName")
            or posting.get("locationName")
            or posting.get("workplaceType")
            or ""
        )
        jobs.append(
            JobRecord(
                job_id=posting.get("id", "") or posting.get("jobId", ""),
                title=posting.get("title", ""),
                company=target.name,
                location=location,
                url=posting.get("externalLink", "") or posting.get("applyLink", "") or CAREERS_URL,
                updated_at=posting.get("updatedAt", "") or posting.get("publishedDate", ""),
                source="custom:shopify",
                departments=[posting.get("departmentName", "")]
                if posting.get("departmentName")
                else [],
                offices=[location] if location else [],
            )
        )
    return jobs


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
                if isinstance(item, int)
                else decode_value(item, seen)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                decode_ref(item, seen) if isinstance(item, int) else decode_value(item, seen)
                for item in value
            ]
        return value

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
