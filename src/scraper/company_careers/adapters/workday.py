"""Workday career-site adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    AdapterResult,
    CompanyTarget,
    InventoryItem,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
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
SAFE_QUERY_CAPACITY = 2_000
DEFAULT_REQUEST_BUDGET = 500
MAX_REQUEST_BUDGET = 2_000
MAX_PARTITION_DEPTH = 3
MAX_PARTITIONS = 128
MAX_WORKDAY_JOBS = 10_000
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


class _RequestBudgetExceeded(AdapterError):
    """Raised before a request that would exceed the source-wide budget."""


@dataclass
class _RequestBudget:
    limit: int
    used: int = 0
    categories: dict[str, int] = field(default_factory=dict)

    def consume(self, category: str) -> None:
        if self.used >= self.limit:
            raise _RequestBudgetExceeded("Workday source request budget exhausted")
        self.used += 1
        self.categories[category] = self.categories.get(category, 0) + 1

    def record(self, metadata: SourceScanMetadata) -> None:
        metadata["request_budget"] = self.limit
        metadata["requests_used"] = self.used
        counts = {
            key: self.categories.get(key, 0)
            for key in (
                "identity",
                "root_facet_discovery",
                "partition_listing",
                "listing",
                "root_validation",
                "detail",
                "retry",
            )
        }
        metadata["request_counts"] = counts


@dataclass(frozen=True)
class _FacetValue:
    facet: str
    value: str
    count: int


@dataclass
class _PartitionContext:
    budget: _RequestBudget
    metadata: SourceScanMetadata
    api_url: str
    site_id: str
    base_url: str
    timeout: int
    body_index: int
    target: CompanyTarget
    root_total: int
    partitions_seen: set[tuple[tuple[str, tuple[str, ...]], ...]] = field(
        default_factory=set
    )
    partition_count: int = 0
    raw_rows: int = 0
    cross_partition_duplicates: int = 0
    jobs_by_identity: dict[str, JobRecord] = field(default_factory=dict)
    partial_jobs_by_identity: dict[str, JobRecord] = field(default_factory=dict)
    failed: bool = False
    failure_reason: str = ""


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> AdapterResult:
    if not target.tenant or not target.wd_subdomain or not (target.site_id or target.site_ids):
        raise AdapterError("Workday target needs tenant, wd_subdomain, and site_id/site_ids")
    if not TENANT_RE.fullmatch(str(target.tenant)) or not WD_SUBDOMAIN_RE.fullmatch(
        str(target.wd_subdomain)
    ):
        raise AdapterError("Workday target has an invalid tenant or wd_subdomain")

    _validate_applied_facets(target)
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError(f"Workday max_pages must be between 1 and {MAX_PAGES}")
    if not 1 <= int(target.workday_request_budget) <= MAX_REQUEST_BUDGET:
        raise AdapterError(
            f"Workday request budget must be between 1 and {MAX_REQUEST_BUDGET}"
        )
    if not 0 <= int(target.workday_max_partition_depth) <= MAX_PARTITION_DEPTH:
        raise AdapterError(
            f"Workday partition depth must be between 0 and {MAX_PARTITION_DEPTH}"
        )
    if not 1 <= int(target.workday_max_partitions) <= MAX_PARTITIONS:
        raise AdapterError(
            f"Workday partition count must be between 1 and {MAX_PARTITIONS}"
        )
    if not 1 <= int(target.workday_max_jobs) <= MAX_WORKDAY_JOBS:
        raise AdapterError(
            f"Workday job limit must be between 1 and {MAX_WORKDAY_JOBS}"
        )
    budget = _RequestBudget(int(target.workday_request_budget))
    site_ids = _candidate_site_ids(target)
    if target.workday_identity:
        expected_site = str(
            target.workday_identity.get("expected_workday_site") or ""
        ).casefold()
        matching_sites = [
            value for value in site_ids if value.casefold() == expected_site
        ]
        preflight_site = matching_sites[0] if matching_sites else site_ids[0]
        identity_status, identity_diagnostic = _configured_identity_evidence(
            target, preflight_site, _expected_workday_host(target)
        )
        if identity_status == "failed":
            metadata = SourceScanMetadata(
                source_identity_status="failed",
                source_identity_diagnostic=identity_diagnostic,
                scan_completeness_reason="workday_identity_failure",
                stop_reason="workday_identity_failure",
            )
            budget.record(metadata)
            publish_source_scan_metadata(session, metadata)
            return AdapterResult(jobs=[], scan=metadata)
        site_ids = matching_sites
    last_error = None
    discovery_allowed = not bool(target.workday_identity)
    configured_attempts: list[dict[str, str]] = []

    for site_id in site_ids:
        result, last_error, site_discovery_allowed = _try_site(
            session,
            target,
            site_id,
            timeout,
            hostname=_expected_workday_host(target),
            budget=budget,
        )
        if result is not None:
            return _finalize_workday_result(
                session, target, site_id, _expected_workday_host(target), result, budget
            )
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
        discovered, discovery = _discover_site_ids(session, target, timeout, budget)
        discovery["configured_attempts"] = configured_attempts
        for hostname, site_id in discovered:
            if (hostname, site_id.casefold()) in configured:
                continue
            result, last_error, _ = _try_site(
                session, target, site_id, timeout, hostname=hostname, budget=budget
            )
            if result is not None:
                metadata = source_scan_metadata(session)
                metadata["site_discovery"] = "authoritative_workday_metadata"
                metadata["configured_site_ids"] = list(site_ids)
                metadata["resolved_site_id"] = site_id
                metadata["resolved_hostname"] = hostname
                metadata.setdefault("diagnostics", []).append(discovery)
                return _finalize_workday_result(
                    session, target, site_id, hostname, result, budget
                )
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
    if budget.used >= budget.limit:
        metadata.update(
            scan_complete=False,
            authoritative_total_present=False,
            safety_limit_reached=True,
            scan_completeness_reason="workday_request_budget_exhausted",
            stop_reason="workday_request_budget_exhausted",
        )
        budget.record(metadata)
        metadata.setdefault("diagnostics", []).append(discovery)
        publish_source_scan_metadata(session, metadata)
        return AdapterResult(jobs=[], scan=metadata)
    budget.record(metadata)
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
    budget: _RequestBudget,
) -> tuple[List[JobRecord] | None, AdapterError | None, bool]:
    api_url = (
        f"https://{hostname}/wday/cxs/{target.tenant}/{site_id}/jobs"
    )
    body_count = 1 if target.applied_facets else len(_body_variants(0, {}))
    last_error: AdapterError | None = None
    discovery_allowed = True
    for body_index in range(body_count):
        try:
            jobs = _fetch_pages(
                session,
                target,
                api_url,
                site_id,
                body_index,
                timeout,
                base_url=f"https://{hostname}",
                budget=budget,
            )
            first = source_scan_metadata(session)
            if first.get("workday_partitioned") and first.get(
                "collection_changed_during_scan"
            ):
                first_ids = {job.job_id for job in jobs}
                first_requests = int(first.get("pages_requested") or 0)
                retry_request_start = budget.used
                jobs = _fetch_pages(
                    session,
                    target,
                    api_url,
                    site_id,
                    body_index,
                    timeout,
                    base_url=f"https://{hostname}",
                    budget=budget,
                )
                mutation_retry_requests = budget.used - retry_request_start
                budget.categories["retry"] = (
                    budget.categories.get("retry", 0) + mutation_retry_requests
                )
                metadata = source_scan_metadata(session)
                second_ids = {job.job_id for job in jobs}
                metadata.update(
                    collection_restart_count=1,
                    first_traversal_jobs=len(first_ids),
                    stable_id_set_match_across_restart=first_ids == second_ids,
                    stable_ids_added_after_restart=len(second_ids - first_ids),
                    stable_ids_removed_after_restart=len(first_ids - second_ids),
                    total_pages_requested_across_traversals=(
                        first_requests + int(metadata.get("pages_requested") or 0)
                    ),
                    mutation_retry_requests=mutation_retry_requests,
                )
                budget.record(metadata)
                if metadata.get("collection_changed_during_scan"):
                    metadata["collection_retry_exhausted"] = True
                elif metadata.get("scan_complete"):
                    metadata["stabilized_after_collection_retry"] = True
                publish_source_scan_metadata(session, metadata)
            return jobs, None, discovery_allowed
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
    budget: _RequestBudget,
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
        response, rejection = _discovery_page(session, seed, target, timeout, budget)
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
        response, rejection = _discovery_page(
            session, candidate_url, target, timeout, budget
        )
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
    budget: _RequestBudget,
) -> tuple[tuple[str, str] | None, str]:
    try:
        budget.consume("identity")
        response = session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": HEADERS["User-Agent"],
            },
            timeout=timeout,
        )
    except _RequestBudgetExceeded:
        return None, "request budget exhausted"
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


def _finalize_workday_result(
    session: requests.Session,
    target: CompanyTarget,
    site_id: str,
    hostname: str,
    jobs: List[JobRecord],
    budget: _RequestBudget,
) -> AdapterResult:
    """Apply the employer-binding gate once, after the selected site resolves."""
    metadata = source_scan_metadata(session)
    status, diagnostic = _configured_identity_evidence(target, site_id, hostname)
    metadata["source_identity_status"] = status
    metadata["source_identity_diagnostic"] = diagnostic
    metadata["workday_identity_checks"] = 1
    if status not in {"validated", "verified"}:
        # A provider total is useful partition evidence but cannot make an
        # identity-unproven source authoritative.  Clearing this trust bit also
        # prevents central legacy-total normalization from re-promoting it.
        metadata["authoritative_total_present"] = False
        if metadata.get("scan_complete"):
            metadata["scan_complete"] = False
            metadata["scan_completeness_reason"] = "workday_identity_unproven"
            metadata["stop_reason"] = "workday_identity_unproven"
    budget.record(metadata)
    publish_source_scan_metadata(session, metadata)
    inventory = tuple(
        InventoryItem(job.job_id, job.source)
        for job in jobs
        if job.job_id and job.source
    )
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=inventory,
        capabilities_used=frozenset(
            {"inventory_listing", "workday_partitioning"}
            if metadata.get("workday_partitioned")
            else {"inventory_listing"}
        ),
    )


def _configured_identity_evidence(
    target: CompanyTarget, site_id: str, hostname: str
) -> tuple[str, dict[str, Any]]:
    """Validate exact, configuration-bound official recruiting evidence.

    The evidence is intentionally declarative: adding it is a reviewed registry
    action backed by an official employer page.  It is fingerprinted with the
    source configuration and never inferred from CXS jobs or tenant names.
    """
    evidence = dict(target.workday_identity or {})
    required = {
        "evidence_source",
        "expected_apply_hostname",
        "expected_employer_name",
        "expected_source_scope",
        "expected_workday_hostname",
        "official_source_url",
        "official_binding_target_url",
        "official_binding_type",
        "expected_workday_tenant",
        "expected_workday_site",
    }
    base = {
        "proof": "configured_official_recruiting_surface",
        "configured_tenant": str(target.tenant),
        "configured_site_id": site_id,
        "configured_hostname": hostname.casefold(),
    }
    if not evidence:
        return "unproven", {
            **base,
            "proof": "unavailable_in_workday_cxs_listing",
            "reason": (
                "Workday CXS reachability does not prove employer ownership; "
                "no configuration-bound official recruiting evidence is present"
            ),
        }
    missing = sorted(required - evidence.keys())
    official = urlparse(evidence.get("official_source_url", ""))
    expected_host = evidence.get("expected_workday_hostname", hostname).casefold()
    binding_target = urlparse(evidence.get("official_binding_target_url", ""))
    expected_binding_url = f"https://{hostname}/{site_id}"
    binding_type = evidence.get("official_binding_type", "").casefold()
    allowed_binding_types = {
        "direct_link",
        "redirect",
        "embedded_config",
        "retained_first_party_artifact",
    }
    expected_apply_host = evidence.get("expected_apply_hostname", "").casefold()
    allowed_apply_hosts = {
        str(value).casefold() for value in target.allowed_apply_hosts
    }
    exact = (
        evidence.get("expected_workday_tenant", "").casefold()
        == str(target.tenant).casefold()
        and evidence.get("expected_workday_site", "").casefold()
        == site_id.casefold()
        and expected_host == hostname.casefold()
        and evidence.get("expected_employer_name", "").casefold()
        == target.name.casefold()
        and evidence.get("expected_source_scope", "").casefold()
        == target.source_scope.casefold()
        and expected_apply_host == hostname.casefold()
        and expected_apply_host in allowed_apply_hosts
        and binding_type in allowed_binding_types
        and binding_target.scheme == "https"
        and (binding_target.hostname or "").casefold() == hostname.casefold()
        and binding_target.path.rstrip("/").casefold()
        == f"/{site_id}".casefold()
        and not binding_target.query
        and not binding_target.fragment
    )
    official_host = (official.hostname or "").casefold()
    official_is_first_party = bool(
        official.scheme == "https"
        and official_host
        and not official_host.endswith(".myworkdayjobs.com")
    )
    if missing or not exact or not official_is_first_party:
        return "failed", {
            **base,
            "official_source_url": evidence.get("official_source_url"),
            "official_binding_target_url": evidence.get(
                "official_binding_target_url"
            ),
            "expected_binding_target_url": expected_binding_url,
            "missing_fields": missing,
            "exact_binding": exact,
            "official_source_is_first_party_https": official_is_first_party,
            "reason": "configured Workday identity evidence is invalid or mismatched",
        }
    return "validated", {
        **base,
        "official_source_url": evidence["official_source_url"],
        "official_binding_target_url": evidence["official_binding_target_url"],
        "official_binding_type": binding_type,
        "evidence_source": evidence["evidence_source"],
        "expected_employer_name": evidence["expected_employer_name"],
        "expected_source_scope": evidence["expected_source_scope"],
        "expected_workday_hostname": expected_host,
        "expected_apply_hostname": expected_apply_host,
        "reason": (
            "reviewed official recruiting evidence exactly binds employer, scope, "
            "tenant, site, and apply host"
        ),
    }


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
    budget: _RequestBudget,
) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    metadata = SourceScanMetadata(
        page_size=PAGE_SIZE,
        maximum_pages=int(target.max_pages),
        maximum_results=MAX_RESULTS,
        stop_reason="not_started",
        source_identity_status="unproven",
        source_identity_diagnostic={
            "proof": "unavailable_in_workday_cxs_listing",
            "configured_tenant": str(target.tenant),
            "configured_site_id": site_id,
            "configured_hostname": (urlparse(api_url).hostname or "").casefold(),
            "reason": (
                "Workday CXS jobPostings responses expose no independently "
                "verifiable employer identity; exact tenant/site routing alone "
                "is insufficient for validated source identity"
            ),
        },
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
            budget.consume(
                "retry"
                if offset == 0 and body_index > 0
                else ("root_facet_discovery" if offset == 0 else "listing")
            )
            response = session.post(
                api_url, json=body, headers=HEADERS, timeout=timeout
            )
        except _RequestBudgetExceeded:
            metadata["stop_reason"] = "workday_request_budget_exhausted"
            metadata["scan_completeness_reason"] = "workday_request_budget_exhausted"
            metadata["safety_limit_reached"] = True
            budget.record(metadata)
            break
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
        if page == 0 and reported_total is not None and reported_total >= SAFE_QUERY_CAPACITY:
            jobs = _fetch_partitioned_inventory(
                session,
                target,
                api_url,
                site_id,
                body_index,
                timeout,
                base_url=base_url,
                budget=budget,
                root_payload=payload,
                root_total=reported_total,
            )
            return jobs
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
        if expected_total is None and enumerated >= SAFE_QUERY_CAPACITY:
            # Without a trustworthy total, reaching the known provider
            # boundary is itself cap evidence.  A later empty/repeated offset
            # cannot prove that the underlying inventory ended here.
            metadata["stop_reason"] = "workday_provider_cap_reached"
            metadata["scan_completeness_reason"] = "workday_provider_cap_reached"
            metadata["authoritative_total_present"] = False
            metadata["workday_safe_query_capacity"] = SAFE_QUERY_CAPACITY
            break
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
        if expected_total is not None and page + 1 >= int(
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

    budget.record(metadata)
    return jobs


def _fetch_partitioned_inventory(
    session: requests.Session,
    target: CompanyTarget,
    api_url: str,
    site_id: str,
    body_index: int,
    timeout: int,
    *,
    base_url: str,
    budget: _RequestBudget,
    root_payload: Mapping[str, Any],
    root_total: int,
) -> List[JobRecord]:
    """Enumerate a cap-bound query through provider-advertised facets."""
    metadata = source_scan_metadata(session)
    metadata.update(
        workday_partitioned=True,
        workday_safe_query_capacity=SAFE_QUERY_CAPACITY,
        workday_root_total=root_total,
        total_available=root_total,
        authoritative_total_present=False,
        scan_complete=False,
        scan_completeness_reason="provider_cap_requires_partition",
        stop_reason="provider_cap_requires_partition",
        maximum_partition_depth=int(target.workday_max_partition_depth),
        maximum_partitions=int(target.workday_max_partitions),
        maximum_workday_jobs=int(target.workday_max_jobs),
    )
    context = _PartitionContext(
        budget=budget,
        metadata=metadata,
        api_url=api_url,
        site_id=site_id,
        base_url=base_url,
        timeout=timeout,
        body_index=body_index,
        target=target,
        root_total=root_total,
        raw_rows=len(_payload_items(root_payload)),
    )
    root_state = _partition_state(target.applied_facets)
    context.partitions_seen.add(root_state)
    context.partition_count = 1

    root_jobs = _jobs_from_items(
        _payload_items(root_payload), target, site_id, base_url
    )
    for job in root_jobs:
        context.partial_jobs_by_identity.setdefault(job.job_id.casefold(), job)

    options = _partition_dimensions(
        root_payload, root_total, excluded=set(target.applied_facets)
    )
    if root_total > int(target.workday_max_jobs):
        metadata["safety_limit_reached"] = True
        _fail_partition(context, "workday_max_jobs_reached")
    elif not options:
        _fail_partition(context, "workday_unsplittable_partition")
    else:
        facet, values = options[0]
        _traverse_partition_children(
            session,
            context,
            dict(target.applied_facets),
            facet,
            values,
            depth=1,
        )

    if not context.failed:
        recheck = _partition_payload(
            session,
            context,
            dict(target.applied_facets),
            offset=0,
            category="root_validation",
        )
        if recheck is None:
            pass
        else:
            recheck_total = _safe_partition_total(recheck, context)
            root_ids = {
                _page_item_identity(item).casefold()
                for item in _payload_items(root_payload)
            }
            recheck_ids = {
                _page_item_identity(item).casefold()
                for item in _payload_items(recheck)
            }
            if recheck_total != root_total or recheck_ids != root_ids:
                mark_collection_changed(metadata)
                _fail_partition(context, "workday_partition_contradiction")

    unique_jobs = list(context.jobs_by_identity.values())
    if not context.failed and len(unique_jobs) != root_total:
        _fail_partition(context, "workday_partition_contradiction")
    if not context.failed and len(unique_jobs) > int(target.workday_max_jobs):
        metadata["safety_limit_reached"] = True
        _fail_partition(context, "workday_max_jobs_reached")

    metadata.update(
        partition_count=context.partition_count,
        partition_states_seen=len(context.partitions_seen),
        raw_rows_fetched=context.raw_rows,
        duplicate_rows_across_partitions=context.cross_partition_duplicates,
        unique_inventory_ids=len(context.jobs_by_identity),
        reported_partition_totals=metadata.get("reported_partition_totals", []),
    )
    if context.failed:
        for key, job in context.jobs_by_identity.items():
            context.partial_jobs_by_identity.setdefault(key, job)
        jobs = list(context.partial_jobs_by_identity.values())
        metadata["scan_complete"] = False
        metadata["authoritative_total_present"] = False
        metadata["scan_completeness_reason"] = context.failure_reason
        metadata["stop_reason"] = context.failure_reason
    else:
        jobs = unique_jobs
        metadata["total_available"] = root_total
        metadata["authoritative_total_present"] = True
        finish_terminal(
            metadata,
            complete_reason="workday_partition_union_complete",
        )
    budget.record(metadata)
    publish_source_scan_metadata(session, metadata)
    return jobs


def _traverse_partition_children(
    session: requests.Session,
    context: _PartitionContext,
    parent_facets: Dict[str, List[str]],
    facet: str,
    values: List[_FacetValue],
    *,
    depth: int,
) -> None:
    if context.failed:
        return
    if depth > int(context.target.workday_max_partition_depth):
        context.metadata["safety_limit_reached"] = True
        _fail_partition(context, "workday_partition_depth_limit")
        return
    if context.partition_count + len(values) > int(
        context.target.workday_max_partitions
    ):
        context.metadata["safety_limit_reached"] = True
        _fail_partition(context, "workday_partition_count_limit")
        return

    for value in values:
        facets = {key: list(items) for key, items in parent_facets.items()}
        facets[facet] = [value.value]
        state = _partition_state(facets)
        if state in context.partitions_seen:
            _fail_partition(context, "workday_duplicate_partition_state")
            return
        context.partitions_seen.add(state)
        context.partition_count += 1
        payload = _partition_payload(
            session, context, facets, offset=0, category="partition_listing"
        )
        if payload is None:
            return
        total = _safe_partition_total(payload, context)
        if total is None:
            return
        context.metadata.setdefault("reported_partition_totals", []).append(
            {"facets": facets, "total": total}
        )
        if total != value.count:
            mark_collection_changed(context.metadata)
            _fail_partition(context, "workday_partition_contradiction")
            return
        if not _partition_filters_applied(payload, facets, total):
            _fail_partition(context, "workday_partition_contradiction")
            return
        if total >= SAFE_QUERY_CAPACITY:
            options = _partition_dimensions(
                payload, total, excluded=set(facets)
            )
            if not options:
                _fail_partition(context, "workday_unsplittable_partition")
                return
            next_facet, next_values = options[0]
            _traverse_partition_children(
                session,
                context,
                facets,
                next_facet,
                next_values,
                depth=depth + 1,
            )
        else:
            _traverse_leaf(session, context, facets, payload, total)
        if context.failed:
            return


def _traverse_leaf(
    session: requests.Session,
    context: _PartitionContext,
    facets: Dict[str, List[str]],
    first_payload: Mapping[str, Any],
    total: int,
) -> None:
    local: dict[str, JobRecord] = {}
    payload = first_payload
    offset = 0
    while True:
        items = _payload_items(payload)
        context.raw_rows += len(items)
        try:
            jobs = _jobs_from_items(
                items, context.target, context.site_id, context.base_url
            )
        except AdapterError:
            _fail_partition(context, "workday_partition_contradiction")
            return
        for job in jobs:
            key = job.job_id.casefold()
            existing = local.get(key)
            if existing is not None:
                if immutable_job_fingerprint(existing) != immutable_job_fingerprint(job):
                    mark_collection_changed(context.metadata)
                    _fail_partition(context, "workday_partition_contradiction")
                    return
                mark_collection_changed(
                    context.metadata, offset=offset, overlap_count=1
                )
                _fail_partition(context, "workday_partition_contradiction")
                return
            local[key] = job
        context.metadata["pages_completed"] += 1
        if len(local) == total:
            break
        if len(items) < PAGE_SIZE or len(local) > total:
            _fail_partition(context, "workday_partition_contradiction")
            return
        offset += PAGE_SIZE
        payload = _partition_payload(
            session,
            context,
            facets,
            offset=offset,
            category="partition_listing",
        )
        if payload is None:
            return
        observed = _safe_partition_total(payload, context, allow_zero_sentinel=True)
        if observed not in {None, total}:
            mark_collection_changed(context.metadata)
            _fail_partition(context, "workday_partition_contradiction")
            return

    for key, job in local.items():
        existing = context.jobs_by_identity.get(key)
        if existing is not None:
            if immutable_job_fingerprint(existing) != immutable_job_fingerprint(job):
                mark_collection_changed(context.metadata)
                _fail_partition(context, "workday_partition_contradiction")
                return
            context.cross_partition_duplicates += 1
            continue
        context.jobs_by_identity[key] = job


def _partition_payload(
    session: requests.Session,
    context: _PartitionContext,
    facets: Dict[str, List[str]],
    *,
    offset: int,
    category: str,
) -> Mapping[str, Any] | None:
    # Partitioning necessarily uses Workday's explicit appliedFacets contract.
    # Compatibility root bodies that omit this key cannot express a partition.
    body = _body_variants(offset, facets)[0]
    context.metadata["pages_requested"] += 1
    try:
        context.budget.consume(category)
        response = session.post(
            context.api_url,
            json=body,
            headers=HEADERS,
            timeout=context.timeout,
        )
    except _RequestBudgetExceeded:
        context.metadata["safety_limit_reached"] = True
        _fail_partition(context, "workday_request_budget_exhausted")
        return None
    except requests.RequestException:
        _fail_partition(context, "workday_partition_transport_failure")
        return None
    if response.status_code != 200:
        reason = (
            "workday_partition_transient_http"
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}
            else "workday_partition_request_failure"
        )
        _fail_partition(context, reason)
        return None
    try:
        payload = response.json()
    except (TypeError, ValueError):
        _fail_partition(context, "workday_partition_contradiction")
        return None
    if not isinstance(payload, Mapping):
        _fail_partition(context, "workday_partition_contradiction")
        return None
    try:
        _payload_items(payload)
    except AdapterError:
        _fail_partition(context, "workday_partition_contradiction")
        return None
    return payload


def _safe_partition_total(
    payload: Mapping[str, Any],
    context: _PartitionContext,
    *,
    allow_zero_sentinel: bool = False,
) -> int | None:
    try:
        total = _reported_total(payload)
    except AdapterError:
        _fail_partition(context, "workday_partition_contradiction")
        return None
    if total is None:
        _fail_partition(context, "workday_partition_contradiction")
        return None
    if allow_zero_sentinel and total == 0:
        return None
    return total


def _partition_dimensions(
    payload: Mapping[str, Any], parent_total: int, *, excluded: set[str]
) -> List[tuple[str, List[_FacetValue]]]:
    candidates: List[tuple[str, List[_FacetValue]]] = []
    for facet, values in _facet_dimensions(payload.get("facets", [])):
        if facet in excluded:
            continue
        usable = [
            value
            for value in values
            if 0 < value.count < parent_total
        ]
        if len(usable) < 2 or sum(value.count for value in usable) < parent_total:
            continue
        candidates.append((facet, usable))
    candidates.sort(
        key=lambda item: (
            max(value.count for value in item[1]),
            len(item[1]),
            item[0].casefold(),
        )
    )
    return candidates


def _facet_dimensions(
    facets: object,
) -> List[tuple[str, List[_FacetValue]]]:
    found: List[tuple[str, List[_FacetValue]]] = []
    if not isinstance(facets, list):
        return found
    for entry in facets:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("facetParameter") or "")
        raw_values = entry.get("values")
        if FACET_KEY_RE.fullmatch(key) and isinstance(raw_values, list):
            values: List[_FacetValue] = []
            seen: set[str] = set()
            for raw in raw_values:
                if not isinstance(raw, Mapping):
                    continue
                value = str(raw.get("id") or "")
                count = raw.get("count")
                if (
                    FACET_VALUE_RE.fullmatch(value)
                    and not isinstance(count, bool)
                    and isinstance(count, int)
                    and count >= 0
                    and value.casefold() not in seen
                ):
                    seen.add(value.casefold())
                    values.append(_FacetValue(key, value, count))
            if values:
                found.append((key, values))
        nested = [
            raw
            for raw in (raw_values if isinstance(raw_values, list) else [])
            if isinstance(raw, Mapping) and isinstance(raw.get("values"), list)
        ]
        found.extend(_facet_dimensions(nested))
    return found


def _partition_filters_applied(
    payload: Mapping[str, Any], facets: Dict[str, List[str]], total: int
) -> bool:
    dimensions = dict(_facet_dimensions(payload.get("facets", [])))
    for key, selected in facets.items():
        values = dimensions.get(key)
        if not values or len(selected) != 1:
            return False
        match = next(
            (value for value in values if value.value == selected[0]), None
        )
        if match is None or match.count != total:
            return False
    return True


def _partition_state(
    facets: Mapping[str, List[str]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        sorted(
            (
                key.casefold(),
                tuple(sorted(value.casefold() for value in values)),
            )
            for key, values in facets.items()
        )
    )


def _payload_items(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    items = payload.get("jobPostings")
    if not isinstance(items, list) or any(
        not isinstance(item, Mapping) for item in items
    ):
        raise AdapterError("Workday response must contain a jobPostings list")
    if len(items) > PAGE_SIZE:
        raise AdapterError("Workday response exceeded the requested page size")
    return list(items)


def _jobs_from_items(
    items: List[Mapping[str, Any]],
    target: CompanyTarget,
    site_id: str,
    base_url: str,
) -> List[JobRecord]:
    jobs: List[JobRecord] = []
    for item in items:
        identity = _page_item_identity(item)
        if not identity:
            raise AdapterError("Workday job posting has no stable identity")
        external_path = str(item.get("externalPath") or "")
        bullet_fields = item.get("bulletFields") or []
        if not isinstance(bullet_fields, list):
            bullet_fields = []
        job_id = _stable_job_id(bullet_fields, external_path)
        location = _location(
            item.get("locationsText", ""), bullet_fields, job_id, external_path
        )
        jobs.append(
            JobRecord(
                job_id=job_id,
                title=str(item.get("title") or ""),
                company=target.name,
                location=location,
                url=f"{base_url}/en-US/{site_id}{external_path}",
                updated_at=str(item.get("postedOn") or ""),
                source=f"workday:{target.tenant}",
                departments=[],
                offices=[location] if location else [],
                raw_metadata={"source_posted_at": item.get("postedOn")},
            )
        )
    return jobs


def _fail_partition(context: _PartitionContext, reason: str) -> None:
    if not context.failed:
        context.failed = True
        context.failure_reason = reason
    context.metadata["scan_complete"] = False
    context.metadata["authoritative_total_present"] = False
    context.metadata["scan_completeness_reason"] = context.failure_reason
    context.metadata["stop_reason"] = context.failure_reason


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
