"""Workday career-site adapter."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
    source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion
from src.scraper.company_careers.mutable_collection import (
    finish_changed,
    finish_terminal,
    immutable_job_fingerprint,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
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
MAX_DISCOVERY_CANDIDATES = 3
MAX_DISCOVERY_BYTES = 3_000_000
SITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
WORKDAY_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
WD_SUBDOMAIN_RE = re.compile(r"^wd[0-9]{1,4}$", re.IGNORECASE)


class _FirstPageHttpError(AdapterError):
    """A retryable first-page site/request-contract rejection."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 20) -> List[JobRecord]:
    if not target.tenant or not target.wd_subdomain or not (target.site_id or target.site_ids):
        raise AdapterError("Workday target needs tenant, wd_subdomain, and site_id/site_ids")
    if not TENANT_RE.fullmatch(str(target.tenant)) or not WD_SUBDOMAIN_RE.fullmatch(
        str(target.wd_subdomain)
    ):
        raise AdapterError("Workday target has an invalid tenant or wd_subdomain")

    _validate_applied_facets(target)
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Workday max_pages must be between 1 and {MAX_PAGES}")
    site_ids = _candidate_site_ids(target)
    last_error = None
    discovery_allowed = True
    configured_attempts: list[dict[str, str]] = []

    for site_id in site_ids:
        result, last_error, site_discovery_allowed = _try_site(
            session, target, site_id, timeout, hostname=_expected_workday_host(target)
        )
        if result is not None:
            return result
        configured_attempts.append(
            {"site_id": site_id, "reason": str(last_error or "request failed")[:160]}
        )
        discovery_allowed = discovery_allowed and site_discovery_allowed

    # A configured Workday site can be retired while the authoritative public
    # careers page starts linking its replacement. Only first-page 404/422
    # responses enable this bounded fallback. Candidate pages must live on the
    # exact configured tenant host and repeat both tenant and siteId in
    # Workday's own bootstrap metadata before their CXS endpoint is tried.
    discovery = _empty_discovery_diagnostic(target, site_ids)
    discovery["configured_attempts"] = configured_attempts
    if discovery_allowed:
        configured = {
            (_expected_workday_host(target), value.casefold()) for value in site_ids
        }
        discovered, discovery = _discover_site_ids(session, target, timeout)
        discovery["configured_attempts"] = configured_attempts
        for hostname, site_id in discovered:
            if (hostname, site_id.casefold()) in configured:
                continue
            result, last_error, _ = _try_site(
                session, target, site_id, timeout, hostname=hostname
            )
            if result is not None:
                metadata = source_scan_metadata(session)
                metadata["site_discovery"] = "authoritative_workday_metadata"
                metadata["configured_site_ids"] = list(site_ids)
                metadata["resolved_site_id"] = site_id
                metadata["resolved_hostname"] = hostname
                metadata.setdefault("diagnostics", []).append(discovery)
                return result
            discovery["candidates"].append(
                {
                    "hostname": hostname,
                    "site_id": site_id,
                    "status": "rejected",
                    "reason": str(last_error or "CXS request failed")[:160],
                }
            )
    else:
        discovery["discovery_status"] = "not_attempted_non_contract_failure"

    metadata = source_scan_metadata(session)
    metadata.setdefault("diagnostics", []).append(discovery)
    publish_source_scan_metadata(session, metadata)
    diagnostic = _format_discovery_diagnostic(discovery)

    error = str(last_error) if last_error else f"Workday failed for {target.name}"
    raise AdapterError(f"{error} ({diagnostic})")


def _try_site(
    session: requests.Session,
    target: CompanyTarget,
    site_id: str,
    timeout: int,
    *,
    hostname: str,
) -> tuple[List[JobRecord] | None, AdapterError | None, bool]:
    api_url = (
        f"https://{hostname}/wday/cxs/{target.tenant}/{site_id}/jobs"
    )
    body_count = 1 if target.applied_facets else len(_body_variants(0, {}))
    last_error: AdapterError | None = None
    discovery_allowed = True
    for body_index in range(body_count):
        try:
            return (
                _fetch_pages(
                    session,
                    target,
                    api_url,
                    site_id,
                    body_index,
                    timeout,
                    base_url=f"https://{hostname}",
                ),
                None,
                discovery_allowed,
            )
        except AdapterError as exc:
            last_error = exc
            # Body variants and alternate site IDs are first-page
            # compatibility fallbacks. Never restart an enumeration after
            # a later page has already been accepted.
            if source_scan_metadata(session)["pages_completed"]:
                raise
            if not isinstance(exc, _FirstPageHttpError) or exc.status_code not in {404, 422}:
                discovery_allowed = False
            continue
    return None, last_error, discovery_allowed


def _candidate_site_ids(target: CompanyTarget) -> List[str]:
    site_ids = []
    if target.site_id:
        site_ids.append(target.site_id)
    site_ids.extend(target.site_ids)
    unique = list(dict.fromkeys(str(value).strip() for value in site_ids if str(value).strip()))
    if not unique or any(not SITE_ID_RE.fullmatch(value) for value in unique):
        raise AdapterError("Workday site_id/site_ids contains an invalid site identifier")
    return unique


def _discover_site_ids(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Discover a bounded set of site IDs from authoritative Workday metadata."""
    expected_host = _expected_workday_host(target)
    diagnostic = _empty_discovery_diagnostic(target, _candidate_site_ids(target))
    seeds: list[str] = []
    for value in (target.search_url, target.careers_url):
        url = str(value or "").strip()
        if url and url not in seeds:
            seeds.append(url)
    for site_id in _candidate_site_ids(target):
        shell_url = f"https://{expected_host}/{site_id}"
        if shell_url not in seeds:
            seeds.append(shell_url)
    if not seeds:
        return [], diagnostic

    candidate_urls: list[str] = []
    validated: list[tuple[str, str]] = []
    configured = {
        (expected_host, value.casefold()) for value in _candidate_site_ids(target)
    }
    for seed in seeds[:MAX_DISCOVERY_CANDIDATES]:
        response, rejection = _discovery_page(session, seed, target, timeout)
        if response is None:
            diagnostic["page_rejections"].append(
                {
                    "hostname": (urlparse(seed).hostname or "").casefold(),
                    "reason": rejection,
                }
            )
            continue
        resolved_url, text = response
        resolved = urlparse(resolved_url)
        resolved_host = (resolved.hostname or "").casefold()
        if (
            resolved_host.endswith(".myworkdayjobs.com")
            and resolved_host not in diagnostic["candidate_hostnames"]
        ):
            diagnostic["candidate_hostnames"].append(resolved_host)
        if _tenant_workday_hostname(resolved_host, target):
            site_id = _site_id_from_any_workday_url(resolved_url)
            if site_id and _workday_metadata_matches(text, target, site_id):
                validated.append((resolved_host, site_id))
                _record_workday_candidate(
                    diagnostic, resolved_host, site_id, "accepted", "matching tenant/site metadata"
                )
            elif site_id:
                _record_workday_candidate(
                    diagnostic, resolved_host, site_id, "rejected", "tenant/site metadata mismatch"
                )
        for candidate_url in _workday_candidate_urls(text):
            if candidate_url not in candidate_urls:
                candidate_urls.append(candidate_url)
            if len(candidate_urls) >= MAX_DISCOVERY_CANDIDATES:
                break
        if candidate_urls or any(
            (host, value.casefold()) not in configured for host, value in validated
        ):
            break

    for candidate_url in candidate_urls[:MAX_DISCOVERY_CANDIDATES]:
        candidate = urlparse(candidate_url)
        candidate_host = (candidate.hostname or "").casefold()
        if candidate_host and candidate_host not in diagnostic["candidate_hostnames"]:
            diagnostic["candidate_hostnames"].append(candidate_host)
        visible_site = _site_id_from_any_workday_url(candidate_url)
        if not _tenant_workday_hostname(candidate_host, target):
            _record_workday_candidate(
                diagnostic,
                candidate_host,
                visible_site,
                "rejected",
                "candidate hostname tenant does not match configured tenant",
            )
            continue
        site_id = visible_site
        if not site_id:
            _record_workday_candidate(
                diagnostic, candidate_host, visible_site, "rejected", "invalid site path"
            )
            continue
        if (candidate_host, site_id.casefold()) in {
            (host, value.casefold()) for host, value in validated
        }:
            continue
        response, rejection = _discovery_page(session, candidate_url, target, timeout)
        if response is None:
            _record_workday_candidate(
                diagnostic, candidate_host, site_id, "rejected", rejection
            )
            continue
        resolved_url, text = response
        resolved_host = (urlparse(resolved_url).hostname or "").casefold()
        resolved_site = _site_id_from_any_workday_url(resolved_url)
        if resolved_site and _workday_metadata_matches(text, target, resolved_site):
            validated.append((resolved_host, resolved_site))
            _record_workday_candidate(
                diagnostic,
                resolved_host,
                resolved_site,
                "accepted",
                "matching tenant/site metadata",
            )
        else:
            _record_workday_candidate(
                diagnostic,
                (urlparse(resolved_url).hostname or "").casefold(),
                resolved_site or site_id,
                "rejected",
                "tenant/site metadata mismatch",
            )
    unique = list(dict.fromkeys(validated))[:MAX_DISCOVERY_CANDIDATES]
    diagnostic["candidate_site_ids"] = list(
        dict.fromkeys(
            item["site_id"] for item in diagnostic["candidates"] if item.get("site_id")
        )
    )[:MAX_DISCOVERY_CANDIDATES]
    diagnostic["candidate_hostnames"] = diagnostic["candidate_hostnames"][
        :MAX_DISCOVERY_CANDIDATES
    ]
    diagnostic["discovery_status"] = "validated_candidates" if unique else "no_valid_candidate"
    return unique, diagnostic


def _discovery_page(
    session: requests.Session,
    url: str,
    target: CompanyTarget,
    timeout: int,
) -> tuple[tuple[str, str] | None, str]:
    try:
        response = session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": HEADERS["User-Agent"],
            },
            timeout=timeout,
        )
    except requests.RequestException:
        return None, "request failed"
    if getattr(response, "status_code", None) != 200:
        return None, f"HTTP {getattr(response, 'status_code', None)}"
    content = bytes(getattr(response, "content", b"") or b"")
    text = str(getattr(response, "text", "") or "")
    if not content:
        content = text.encode("utf-8", errors="replace")
    if len(content) > min(int(target.max_response_bytes), MAX_DISCOVERY_BYTES):
        return None, "response exceeded byte limit"
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        return None, "response was not HTML"
    resolved_url = str(getattr(response, "url", "") or url)
    initial_host = (urlparse(url).hostname or "").casefold()
    resolved_host = (urlparse(resolved_url).hostname or "").casefold()
    expected_host = (
        f"{str(target.tenant).casefold()}.{str(target.wd_subdomain).casefold()}"
        ".myworkdayjobs.com"
    )
    if resolved_host not in {initial_host, expected_host} and not _tenant_workday_hostname(
        resolved_host, target
    ):
        return None, f"redirect hostname rejected: {resolved_host or 'missing'}"
    return (resolved_url, text), ""


def _workday_candidate_urls(text: str) -> List[str]:
    normalized = text.replace(r"\/", "/")
    pattern = re.compile(
        r"https://[A-Za-z0-9-]+\.wd[0-9]{1,4}\.myworkdayjobs\.com/"
        r"(?:(?:[a-z]{2}-[A-Z]{2})/)?"
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?:[/\"'?#]|$)",
        re.IGNORECASE,
    )
    return [match.group(0).rstrip('/\"\'?#') for match in pattern.finditer(normalized)]


def _site_id_from_workday_url(url: str, expected_host: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != expected_host:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if parts and WORKDAY_LOCALE_RE.fullmatch(parts[0]):
        parts = parts[1:]
    site_id = parts[0] if parts else ""
    return site_id if SITE_ID_RE.fullmatch(site_id) else ""


def _site_id_from_any_workday_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not re.fullmatch(
        r"[a-z0-9-]+\.wd[0-9]{1,4}\.myworkdayjobs\.com", host
    ):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if parts and WORKDAY_LOCALE_RE.fullmatch(parts[0]):
        parts = parts[1:]
    site_id = parts[0] if parts else ""
    return site_id if SITE_ID_RE.fullmatch(site_id) else ""


def _workday_metadata_matches(text: str, target: CompanyTarget, site_id: str) -> bool:
    tenant = re.escape(str(target.tenant))
    site = re.escape(site_id)
    return bool(
        re.search(
            rf"[\"']?tenant[\"']?\s*[:=]\s*[\"']{tenant}[\"']",
            text,
            re.IGNORECASE,
        )
        and re.search(
            rf"[\"']?siteId[\"']?\s*[:=]\s*[\"']{site}[\"']",
            text,
            re.IGNORECASE,
        )
    )


def _expected_workday_host(target: CompanyTarget) -> str:
    return (
        f"{str(target.tenant).casefold()}.{str(target.wd_subdomain).casefold()}"
        ".myworkdayjobs.com"
    )


def _tenant_workday_hostname(hostname: str, target: CompanyTarget) -> bool:
    match = re.fullmatch(
        r"([a-z0-9-]+)\.(wd[0-9]{1,4})\.myworkdayjobs\.com",
        hostname.casefold(),
    )
    return bool(match and match.group(1) == str(target.tenant).casefold())


def _empty_discovery_diagnostic(
    target: CompanyTarget, configured_site_ids: List[str]
) -> dict[str, Any]:
    return {
        "kind": "workday_site_discovery",
        "configured_site_ids": list(configured_site_ids),
        "configured_hostname": _expected_workday_host(target),
        "candidate_site_ids": [],
        "candidate_hostnames": [],
        "candidates": [],
        "page_rejections": [],
        "discovery_status": "not_started",
    }


def _record_workday_candidate(
    diagnostic: dict[str, Any],
    hostname: str,
    site_id: str,
    status: str,
    reason: str,
) -> None:
    candidate = {
        "hostname": hostname or "missing",
        "site_id": site_id or "missing",
        "status": status,
        "reason": reason[:160],
    }
    if candidate not in diagnostic["candidates"]:
        diagnostic["candidates"].append(candidate)
        del diagnostic["candidates"][MAX_DISCOVERY_CANDIDATES * 2 :]


def _format_discovery_diagnostic(diagnostic: Mapping[str, Any]) -> str:
    configured = ",".join(diagnostic.get("configured_site_ids", [])) or "none"
    sites = ",".join(diagnostic.get("candidate_site_ids", [])) or "none"
    hosts = ",".join(diagnostic.get("candidate_hostnames", [])) or "none"
    candidates = "; ".join(
        f"{item.get('hostname')}/{item.get('site_id')}: {item.get('reason')}"
        for item in diagnostic.get("candidates", [])
        if isinstance(item, Mapping)
    ) or "none"
    return (
        f"configured_sites=[{configured}]; candidate_sites=[{sites}]; "
        f"candidate_hostnames=[{hosts}]; candidate_results=[{candidates}]"
    )


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
    *,
    base_url: str,
) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    metadata = SourceScanMetadata(
        page_size=PAGE_SIZE,
        maximum_pages=int(target.max_pages),
        maximum_results=MAX_RESULTS,
        stop_reason="not_started",
    )
    listing_budget = AdaptiveListingCompletion(
        normal_request_budget=max(1, int(target.max_pages)),
        normal_job_budget=max(1, int(target.max_jobs)),
        allow_listing_only_second_stage=True,
    )
    listing_budget.record(metadata)
    publish_source_scan_metadata(session, metadata)
    initialize_mutable_collection(metadata)
    expected_total: int | None = None
    seen_page_signatures: set[tuple[str, ...]] = set()
    seen_item_identities: set[str] = set()
    seen_item_fingerprints: dict[str, tuple[Any, ...]] = {}

    for page in range(listing_budget.hard_request_ceiling):
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
            error = (
                f"Workday HTTP {response.status_code} for {target.name} "
                f"site={site_id} body={body_index} offset={offset}"
            )
            if offset == 0:
                raise _FirstPageHttpError(error, response.status_code)
            raise AdapterError(error)

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
            record_total_observation(
                metadata,
                reported_total,
                offset=offset,
                source_label="Workday",
            )
            expected_total = reported_total
            listing_budget.record_decision(
                metadata,
                expected_total,
                PAGE_SIZE,
                requests_completed=int(metadata["pages_requested"]),
                results_observed=offset + len(items),
            )
            if (
                expected_total < offset + len(items)
                and not metadata["collection_changed_during_scan"]
            ):
                metadata["stop_reason"] = "malformed_total"
                metadata["scan_completeness_reason"] = "malformed_total"
                raise AdapterError("Workday returned more jobs than its reported total")
            if expected_total > offset and not items:
                metadata["pages_completed"] += 1
                if metadata["collection_changed_during_scan"]:
                    finish_changed(
                        metadata,
                        terminal_evidence="premature_terminal_page",
                    )
                else:
                    metadata["stop_reason"] = "premature_terminal_page"
                    metadata["scan_completeness_reason"] = "premature_terminal_page"
                break

        identities = tuple(_page_item_identity(item) for item in items)
        if items and not all(identities):
            metadata["stop_reason"] = "malformed_response"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday job posting has no stable identity")
        # Fingerprint the identity set rather than presentation order. A page
        # containing exactly the same jobs in a different order is still
        # non-advancing, while a partially overlapping page can legitimately
        # arise from Workday's moving, recently-sorted result set.
        signature = tuple(sorted({value.casefold() for value in identities}))
        if items and signature in seen_page_signatures:
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday pagination repeated a complete page")
        new_identities = {
            value.casefold() for value in identities
            if value.casefold() not in seen_item_identities
        }
        if items and not new_identities:
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Workday pagination returned no new stable jobs")
        if items:
            seen_page_signatures.add(signature)

        page_duplicates = 0
        for item, identity in zip(items, identities):
            identity_key = identity.casefold()
            external_path = item.get("externalPath", "") or ""
            bullet_fields = item.get("bulletFields") or []
            job_id = _stable_job_id(bullet_fields, external_path)
            location = _location(
                item.get("locationsText", ""), bullet_fields, job_id, external_path
            )
            job = JobRecord(
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
            fingerprint = immutable_job_fingerprint(job)
            if identity_key in seen_item_identities:
                if seen_item_fingerprints[identity_key] != fingerprint:
                    metadata["stop_reason"] = "conflicting_repeated_job_id"
                    metadata["scan_completeness_reason"] = "pagination_failure"
                    raise AdapterError(
                        f"Workday conflicting repeated stable job ID: {job.job_id}"
                    )
                page_duplicates += 1
                continue
            seen_item_identities.add(identity_key)
            seen_item_fingerprints[identity_key] = fingerprint
            jobs.append(job)
        if page_duplicates:
            mark_collection_changed(
                metadata, offset=offset, overlap_count=page_duplicates
            )
            metadata["diagnostics"][-1]["new_jobs"] = len(new_identities)
        metadata["pages_completed"] += 1

        enumerated = offset + len(items)
        if expected_total is not None and enumerated == expected_total:
            finish_terminal(metadata, complete_reason="reported_total_reached")
            break
        if len(items) < PAGE_SIZE:
            if expected_total is not None:
                if metadata["collection_changed_during_scan"]:
                    finish_changed(
                        metadata, terminal_evidence="premature_terminal_page"
                    )
                else:
                    metadata["stop_reason"] = "premature_terminal_page"
                    metadata["scan_completeness_reason"] = "premature_terminal_page"
                break
            metadata["total_available"] = enumerated
            finish_terminal(metadata, complete_reason="natural_terminal_page")
            break
        if page + 1 >= int(
            metadata.get(
                "effective_listing_request_ceiling",
                listing_budget.normal_request_budget,
            )
        ):
            metadata["stop_reason"] = "maximum_pages_reached"
            metadata["safety_limit_reached"] = True
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="maximum_pages_reached")
            else:
                metadata["scan_completeness_reason"] = "safety_limit_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
        metadata["safety_limit_reached"] = True
        if metadata["collection_changed_during_scan"]:
            finish_changed(metadata, terminal_evidence="maximum_pages_reached")
        else:
            metadata["scan_completeness_reason"] = "safety_limit_reached"

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
