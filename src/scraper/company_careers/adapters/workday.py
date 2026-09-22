"""Workday career-site adapter."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
    source_scan_metadata,
)


HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
FACET_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
FACET_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
PATH_REQUISITION_RE = re.compile(r"(?:^|_)((?:[A-Z]{1,4})?R\d{4,})$", re.IGNORECASE)
BULLET_REQUISITION_RE = re.compile(r"^[A-Za-z]{0,8}[-_]?\d{1,20}$")
PAGE_SIZE = 20
MAX_PAGES = 25
MAX_RESULTS = PAGE_SIZE * MAX_PAGES


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.tenant or not target.wd_subdomain or not (target.site_id or target.site_ids):
        raise AdapterError("Workday target needs tenant, wd_subdomain, and site_id/site_ids")

    _validate_applied_facets(target)
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Workday max_pages must be between 1 and {MAX_PAGES}")
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
                # Body variants and alternate site IDs are first-page
                # compatibility fallbacks. Never restart an enumeration after
                # a later page has already been accepted.
                if source_scan_metadata(session)["pages_completed"]:
                    raise
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
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        },
        {"limit": PAGE_SIZE, "offset": offset},
        {"limit": PAGE_SIZE, "offset": offset, "searchText": ""},
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
    matching = _find_facet(payload.get("facets", []), key)
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


def _find_facet(facets: object, key: str) -> object:
    """Return a flat or grouped Workday facet with the requested parameter.

    Workday represents some location filters below a ``locationMainGroup``
    wrapper.  The filter is still sent and enforced using the nested child's
    ``facetParameter``.  Walking only facet-shaped values keeps verification
    tied to Workday's structured response rather than presentation text.
    """
    if not isinstance(facets, list):
        return None
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        if facet.get("facetParameter") == key:
            return facet
        nested = [
            value
            for value in facet.get("values", [])
            if isinstance(value, dict) and isinstance(value.get("values"), list)
        ]
        matching = _find_facet(nested, key)
        if matching is not None:
            return matching
    return None


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
    metadata = SourceScanMetadata(
        page_size=PAGE_SIZE,
        maximum_pages=int(target.max_pages),
        maximum_results=MAX_RESULTS,
        stop_reason="not_started",
    )
    publish_source_scan_metadata(session, metadata)
    expected_total: int | None = None
    seen_page_signatures: set[tuple[str, ...]] = set()
    seen_item_identities: set[str] = set()

    for page in range(max(1, target.max_pages)):
        offset = page * PAGE_SIZE
        body = _body_variants(offset, target.applied_facets)[body_index]
        metadata["pages_requested"] += 1
        try:
            response = session.post(
                api_url, json=body, headers=HEADERS, timeout=timeout
            )
        except requests.RequestException:
            metadata["stop_reason"] = "request_failure"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise
        if response.status_code != 200:
            metadata["stop_reason"] = "http_error"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError(
                f"Workday HTTP {response.status_code} for {target.name} "
                f"site={site_id} body={body_index} offset={offset}"
            )

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            metadata["stop_reason"] = "malformed_response"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError(f"Workday returned malformed JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            metadata["stop_reason"] = "malformed_response"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday response must be a JSON object")
        if page == 0:
            _verify_applied_facets(payload, target)
        items = payload.get("jobPostings")
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) for item in items
        ):
            metadata["stop_reason"] = "malformed_response"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday response must contain a jobPostings list")
        if len(items) > PAGE_SIZE:
            metadata["stop_reason"] = "malformed_response"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday response exceeded the requested page size")

        try:
            reported_total = _reported_total(payload)
        except AdapterError:
            metadata["stop_reason"] = "malformed_total"
            metadata["scan_completeness_reason"] = "malformed_total"
            raise
        # Many Workday CXS tenants expose the board total only on offset zero
        # and send a literal zero on subsequent pages. A zero alongside a
        # positive first-page total is an observed "not repeated" sentinel,
        # not a changed source total.
        if offset and expected_total and reported_total == 0:
            reported_total = None
        if reported_total is not None:
            if expected_total is None:
                expected_total = reported_total
                metadata["total_available"] = reported_total
            elif reported_total != expected_total:
                metadata["stop_reason"] = "inconsistent_total"
                metadata["scan_completeness_reason"] = "inconsistent_total"
                raise AdapterError("Workday reported a changing total job count")
            if expected_total < offset + len(items):
                metadata["stop_reason"] = "malformed_total"
                metadata["scan_completeness_reason"] = "malformed_total"
                raise AdapterError("Workday returned more jobs than its reported total")
            if expected_total > offset and not items:
                metadata["stop_reason"] = "malformed_total"
                metadata["scan_completeness_reason"] = "malformed_total"
                raise AdapterError("Workday ended before its reported total job count")

        signature = tuple(_page_item_identity(item) for item in items)
        signature_ids = {value.casefold() for value in signature if value}
        if (
            items
            and (
                signature in seen_page_signatures
                or len(signature_ids) != len(signature)
                or bool(signature_ids & seen_item_identities)
            )
        ):
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday pagination repeated a previously returned job or page")
        if items:
            seen_page_signatures.add(signature)
            seen_item_identities.update(signature_ids)

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
                    raw_metadata={"source_posted_at": item.get("postedOn")},
                )
            )
        metadata["pages_completed"] += 1

        enumerated = offset + len(items)
        if expected_total is not None and enumerated == expected_total:
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            metadata["stop_reason"] = "reported_total_reached"
            break
        if len(items) < PAGE_SIZE:
            if expected_total is not None:
                metadata["stop_reason"] = "malformed_total"
                metadata["scan_completeness_reason"] = "malformed_total"
                raise AdapterError("Workday ended before its reported total job count")
            metadata["total_available"] = enumerated
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "natural_terminal_page"
            metadata["stop_reason"] = "natural_terminal_page"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True

    return jobs


def _reported_total(payload: Mapping[str, Any]) -> int | None:
    """Return a validated Workday total when the tenant exposes one."""
    values = [payload.get(key) for key in ("total", "totalResults") if key in payload]
    if not values:
        return None
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise AdapterError("Workday total must be a non-negative integer")
    if len(set(values)) != 1:
        raise AdapterError("Workday response contains inconsistent total fields")
    return values[0]


def _page_item_identity(item: Mapping[str, Any]) -> str:
    path = str(item.get("externalPath") or "").strip()
    bullets = item.get("bulletFields") or []
    if not isinstance(bullets, list):
        bullets = []
    return _stable_job_id(bullets, path) or path
