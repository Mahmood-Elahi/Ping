"""Bounded adapter for public Exalead career-card inventories."""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


SUPPORTED_VARIANT = "exalead_career_cards_v1"
MAX_PAGES = 2
MAX_RESULTS = 20
MAX_PAGE_SIZE = 15
HEADERS = {
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
    "Accept": "application/json,application/javascript",
}
QUERY = '#all card_content_lang:en   (card_content_type="career") '


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    validate_target(target)
    maximum_pages = min(max(1, int(target.max_pages)), MAX_PAGES)
    maximum_jobs = min(max(1, int(target.max_jobs)), MAX_RESULTS)
    page_size = min(max(1, int(target.page_size)), MAX_PAGE_SIZE)
    jobs: list[JobRecord] = []
    seen: set[str] = set()

    for page in range(maximum_pages):
        response = session.get(
            str(target.endpoint_url),
            params={
                "q": QUERY,
                "s": "desc(card_content_start_datetime)",
                "b": page * page_size,
                "hf": page_size,
                "output_format": "json",
            },
            headers=HEADERS,
            timeout=timeout,
        )
        if response.status_code != 200:
            raise AdapterError(f"Exalead Careers HTTP {response.status_code} for {target.name}")
        try:
            payload = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise AdapterError(f"Exalead Careers returned invalid JSON: {exc}") from exc
        items, total = _inventory(payload)
        if page == 0 and not items and total != 0:
            raise AdapterError("Exalead Careers response did not prove an empty jobs collection")

        new_ids = 0
        for item in items:
            job = _normalize(item, target)
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= maximum_jobs:
                break

        if items and new_ids == 0:
            raise AdapterError("Exalead Careers page repeated without new stable job IDs")
        if len(jobs) >= maximum_jobs or len(items) < page_size or len(jobs) >= total:
            break

    return jobs


def validate_target(target: CompanyTarget) -> None:
    if target.endpoint_variant != SUPPORTED_VARIANT:
        raise AdapterError(f"Exalead unsupported endpoint variant: {target.endpoint_variant}")
    parsed = urlparse(str(target.endpoint_url or ""))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path.rstrip("/") != "/apisearch/card_search_api"
        or parsed.query
        or parsed.fragment
    ):
        raise AdapterError("Exalead endpoint must be an observed public HTTPS card_search_api URL")
    if not target.allowed_apply_hosts:
        raise AdapterError("Exalead target requires allowed_apply_hosts")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Exalead max_pages cannot exceed {MAX_PAGES}")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Exalead max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"Exalead page_size cannot exceed {MAX_PAGE_SIZE}")


def _inventory(payload: Any) -> tuple[list[Mapping[str, Any]], int]:
    if not isinstance(payload, Mapping):
        raise AdapterError("Exalead Careers response must be a JSON object")
    total = payload.get("nhits")
    items = payload.get("hits")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Exalead Careers nhits must be a non-negative integer")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise AdapterError("Exalead Careers hits must be a list of objects")
    if total == 0 and items:
        raise AdapterError("Exalead Careers zero-job response unexpectedly contained jobs")
    return items, total


def _normalize(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    metas = _metas(item.get("metas"))
    job_id = _one(metas, "card_id")
    title = _one(metas, "content_title")
    url = _one(metas, "content_cta_1_url") or _one(metas, "content_cta_1_url_id")
    location = _one(metas, "content_info_2_value")
    if not job_id or not title or not url:
        raise AdapterError("Exalead career card is missing title, URL, or stable ID")
    parsed = urlparse(url)
    allowed = {str(host).casefold() for host in target.allowed_apply_hosts}
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or host not in allowed:
        raise AdapterError("Exalead career card has no official allowed-host detail URL")
    if not parsed.path.rstrip("/").endswith("-" + job_id):
        raise AdapterError("Exalead career-card URL does not corroborate its stable ID")

    department = _one(metas, "content_type_display_text")
    return JobRecord(
        job_id=f"exalead:{job_id}",
        title=title,
        company=target.name,
        location=location,
        url=url,
        updated_at=_one(metas, "content_start_datetime"),
        source=f"exalead:{urlparse(str(target.endpoint_url)).hostname}",
        departments=[department] if department else [],
        offices=[location] if location else [],
        description=_one(metas, "content_summary") or None,
        raw_metadata={
            "platform": "exalead",
            "endpoint_variant": target.endpoint_variant,
            "external_job_id": job_id,
            "apply_url": _one(metas, "content_cta_2_url"),
        },
    )


def _metas(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, list):
        raise AdapterError("Exalead career card metas must be a list")
    result: dict[str, list[str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise AdapterError("Exalead career-card meta must be an object")
        name = str(item.get("name") or "").strip()
        observed = str(item.get("value") or "").strip()
        if name and observed:
            result.setdefault(name, []).append(observed)
    return result


def _one(values: Mapping[str, Iterable[str]], name: str) -> str:
    observed = list(dict.fromkeys(values.get(name, [])))
    if len(observed) > 1:
        raise AdapterError(f"Exalead career card has ambiguous {name}")
    return observed[0] if observed else ""
