"""Workday career-site adapter."""

from __future__ import annotations

import re
from typing import Dict, List

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
FACET_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
FACET_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
PATH_REQUISITION_RE = re.compile(r"(?:^|_)((?:[A-Z]{1,4})?R\d{4,})$", re.IGNORECASE)
BULLET_REQUISITION_RE = re.compile(r"^[A-Za-z]{0,8}[-_]?\d{1,20}$")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.tenant or not target.wd_subdomain or not (target.site_id or target.site_ids):
        raise AdapterError("Workday target needs tenant, wd_subdomain, and site_id/site_ids")

    _validate_applied_facets(target)
    site_ids = _candidate_site_ids(target)
    last_error = None

    for site_id in site_ids:
        api_url = (
            f"https://{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
            f"/wday/cxs/{target.tenant}/{site_id}/jobs"
        )
        body_count = 1 if target.applied_facets else len(_body_variants(0, {}))
        for body_index in range(body_count):
            try:
                return _fetch_pages(session, target, api_url, site_id, body_index, timeout)
            except AdapterError as exc:
                last_error = exc
                continue

    raise AdapterError(str(last_error) if last_error else f"Workday failed for {target.name}")


def _candidate_site_ids(target: CompanyTarget) -> List[str]:
    site_ids = []
    if target.site_id:
        site_ids.append(target.site_id)
    site_ids.extend(target.site_ids)
    return list(dict.fromkeys(site_ids))


def _body_variants(
    offset: int,
    applied_facets: Dict[str, List[str]],
) -> List[Dict[str, object]]:
    return [
        {
            "appliedFacets": applied_facets,
            "limit": 20,
            "offset": offset,
            "searchText": "",
        },
        {"limit": 20, "offset": offset},
        {"limit": 20, "offset": offset, "searchText": ""},
    ]


def _validate_applied_facets(target: CompanyTarget) -> None:
    """Accept one exact, observed Workday facet without an unfiltered fallback."""
    facets = target.applied_facets
    if not facets:
        return
    if len(facets) != 1:
        raise AdapterError("Workday applied_facets must contain exactly one facet")
    key, values = next(iter(facets.items()))
    if not FACET_KEY_RE.fullmatch(key):
        raise AdapterError("Workday applied_facets contains an invalid facet key")
    if len(values) != 1 or not FACET_VALUE_RE.fullmatch(values[0]):
        raise AdapterError("Workday applied_facets must contain one safe opaque value")


def _verify_applied_facets(payload: object, target: CompanyTarget) -> None:
    """Fail closed when Workday does not recognize or apply the configured facet."""
    if not target.applied_facets:
        return
    if not isinstance(payload, dict):
        raise AdapterError("Workday filtered response must be a JSON object")
    key, values = next(iter(target.applied_facets.items()))
    requested = values[0]
    matching = next(
        (
            facet
            for facet in payload.get("facets", [])
            if isinstance(facet, dict) and facet.get("facetParameter") == key
        ),
        None,
    )
    if not matching:
        raise AdapterError("Workday filtered response omitted the configured facet")
    value = next(
        (
            item
            for item in matching.get("values", [])
            if isinstance(item, dict) and str(item.get("id") or "") == requested
        ),
        None,
    )
    total = payload.get("total")
    count = value.get("count") if value else None
    if (
        value is None
        or isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or total != count
    ):
        raise AdapterError("Workday did not conclusively apply the configured exact facet")


def _stable_job_id(bullet_fields: List[object], external_path: str) -> str:
    """Prefer the requisition field corroborated by Workday's detail path.

    Most sites put the requisition ID first. Some prepend a presentation badge
    such as ``Spotlight Job`` and put the stable ID later. Matching a bullet
    field against the terminal path token preserves the common case while
    avoiding a non-unique badge as the source identifier.
    """
    path_token = external_path.rstrip("/").rsplit("/", 1)[-1]
    for value in bullet_fields:
        candidate = str(value).strip()
        if (
            BULLET_REQUISITION_RE.fullmatch(candidate)
            and path_token.casefold().endswith(candidate.casefold())
        ):
            return candidate
    path_match = PATH_REQUISITION_RE.search(path_token)
    if path_match:
        return path_match.group(1)
    if path_token:
        return path_token
    return str(bullet_fields[0]).strip() if bullet_fields else ""


def _location(
    locations_text: object,
    bullet_fields: List[object],
    job_id: str,
    external_path: str,
) -> str:
    """Return Workday's location text, falling back to non-ID listing metadata.

    A small set of official Workday tenants omit ``locationsText`` while still
    returning city/region components in ``bulletFields``.  The requisition ID
    is removed using the already-corroborated stable ID; the remaining values
    are deterministic, public listing metadata rather than inferred geography.
    """
    explicit = " ".join(str(locations_text or "").split())
    if explicit:
        return explicit
    values: List[str] = []
    seen = set()
    for value in bullet_fields:
        candidate = " ".join(str(value or "").split())
        key = candidate.casefold()
        if candidate and key != job_id.casefold() and key not in seen:
            seen.add(key)
            values.append(candidate)
    if values:
        return ", ".join(values)
    parts = [part for part in external_path.split("/") if part]
    if "job" in parts:
        index = parts.index("job")
        if index + 2 < len(parts):
            return parts[index + 1].replace("-", " ").strip()
    return ""


def _fetch_pages(
    session: requests.Session,
    target: CompanyTarget,
    api_url: str,
    site_id: str,
    body_index: int,
    timeout: int,
) -> List[JobRecord]:
    base_url = f"https://{target.tenant}.{target.wd_subdomain}.myworkdayjobs.com"
    jobs: List[JobRecord] = []

    for page in range(max(1, target.max_pages)):
        offset = page * 20
        body = _body_variants(offset, target.applied_facets)[body_index]
        response = session.post(api_url, json=body, headers=HEADERS, timeout=timeout)
        if response.status_code != 200:
            if page == 0:
                raise AdapterError(
                    f"Workday HTTP {response.status_code} for {target.name} site={site_id} body={body_index}"
                )
            break

        payload = response.json()
        if page == 0:
            _verify_applied_facets(payload, target)
        items = payload.get("jobPostings", [])
        if not items:
            break

        for item in items:
            external_path = item.get("externalPath", "") or ""
            bullet_fields = item.get("bulletFields") or []
            job_id = _stable_job_id(bullet_fields, external_path)
            location = _location(
                item.get("locationsText", ""), bullet_fields, job_id, external_path
            )
            jobs.append(
                JobRecord(
                    job_id=job_id,
                    title=item.get("title", ""),
                    company=target.name,
                    location=location,
                    url=f"{base_url}/en-US/{site_id}{external_path}",
                    updated_at=item.get("postedOn", ""),
                    source=f"workday:{target.tenant}",
                    departments=[],
                    offices=[location] if location else [],
                )
            )

        if len(items) < 20:
            break

    return jobs
