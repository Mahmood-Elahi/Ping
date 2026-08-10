"""Workable public widget contract (kept unregistered until CAPTCHA-free validation)."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


MAX_RESULTS = 100
MAX_RESPONSE_BYTES = 3_000_000
SHORTCODE_RE = re.compile(r"^[A-Z0-9]{6,32}$")
TURNSTILE_ENABLED_RE = re.compile(
    r'["\']wjb_acp_turnstile_captcha_enabled["\']\s*:\s*true',
    re.IGNORECASE,
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> list[JobRecord]:
    """Fetch the public widget only when the official apply flow is CAPTCHA-free."""
    config = validate_target(target)
    portal = session.get(
        str(target.careers_url),
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if getattr(portal, "status_code", None) != 200:
        raise AdapterError(f"Workable portal HTTP {getattr(portal, 'status_code', None)}")
    portal_text = str(getattr(portal, "text", "") or "")
    if len(portal_text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise AdapterError("Workable portal exceeded the byte limit")
    if TURNSTILE_ENABLED_RE.search(portal_text) or "cf-chl-" in portal_text.casefold():
        raise AdapterError("Workable official apply flow requires CAPTCHA or challenge state")

    response = session.get(
        config["endpoint_url"],
        headers={"Accept": "application/json", "User-Agent": "job-radar/1.0"},
        timeout=min(max(1, int(timeout)), 30),
    )
    if getattr(response, "status_code", None) != 200:
        raise AdapterError(f"Workable widget HTTP {getattr(response, 'status_code', None)}")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"Workable widget returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise AdapterError("Workable widget response is missing its jobs list")
    identity = str(payload.get("name") or "").strip()
    if not identity or not re.search(str(target.identity_regex), identity, re.IGNORECASE):
        raise AdapterError("Workable widget has mixed or ambiguous company identity")
    jobs = payload["jobs"]
    if len(jobs) > MAX_RESULTS:
        raise AdapterError("Workable widget exceeded the hard result limit")
    return [_normalize(item, target, config["slug"]) for item in jobs[: int(target.max_jobs)]]


def validate_target(target: CompanyTarget) -> dict[str, str]:
    slug = str(target.slug or "").strip().casefold()
    careers = urlparse(str(target.careers_url or ""))
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
        raise AdapterError("Workable target needs a valid account slug")
    if careers.scheme != "https" or (careers.hostname or "").casefold() != "apply.workable.com":
        raise AdapterError("Workable careers_url must use the public apply.workable.com host")
    if [part.casefold() for part in careers.path.split("/") if part] != [slug]:
        raise AdapterError("Workable careers_url slug does not match configuration")
    if not target.identity_regex:
        raise AdapterError("Workable target needs identity_regex")
    try:
        re.compile(str(target.identity_regex), re.IGNORECASE)
    except re.error as exc:
        raise AdapterError(f"Workable identity_regex is invalid: {exc}") from exc
    if int(target.max_pages) != 1 or not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Workable page or result limit exceeds the hard maximum")
    return {
        "slug": slug,
        "endpoint_url": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
    }


def _normalize(item: Any, target: CompanyTarget, slug: str) -> JobRecord:
    if not isinstance(item, Mapping):
        raise AdapterError("Workable job must be a JSON object")
    job_id = str(item.get("shortcode") or "").strip().upper()
    title = str(item.get("title") or "").strip()
    apply_url = str(item.get("application_url") or "").strip()
    parsed = urlparse(apply_url)
    if not SHORTCODE_RE.fullmatch(job_id) or not title:
        raise AdapterError("Workable job is missing a stable shortcode or title")
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "apply.workable.com"
        or parsed.path.rstrip("/").casefold() != f"/j/{job_id}/apply".casefold()
    ):
        raise AdapterError("Workable job is missing an official job-specific apply URL")
    locations = item.get("locations") or []
    if not isinstance(locations, list):
        raise AdapterError("Workable job locations must be a list")
    formatted: list[str] = []
    for location in locations:
        if not isinstance(location, Mapping):
            raise AdapterError("Workable job location must be an object")
        value = ", ".join(
            text
            for text in (
                str(location.get("city") or "").strip(),
                str(location.get("region") or "").strip(),
                str(location.get("country") or "").strip(),
            )
            if text
        )
        if value:
            formatted.append(value)
    department = str(item.get("department") or "").strip()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=" | ".join(dict.fromkeys(formatted)),
        url=apply_url,
        updated_at=str(item.get("published_on") or "").strip(),
        source=f"workable:{slug}",
        departments=[department] if department else [],
        offices=list(dict.fromkeys(formatted)),
        raw_metadata={"platform": "workable", "code": item.get("code")},
    )
