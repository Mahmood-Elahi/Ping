"""Company career-site scanner orchestration."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable, Iterable, List, Optional, Sequence

import requests

from src.scraper.company_careers.adapters import (
    adp,
    amazon,
    apple,
    avature,
    ashby,
    bamboohr,
    beesite,
    atlassian,
    browser_rendered_html,
    clearcompany,
    cornerstone_csod,
    dayforce,
    deel,
    eightfold,
    embedded_data,
    exalead,
    google,
    greenhouse,
    hawksearch_v2,
    hibob,
    ibm_search,
    icims,
    lever,
    meta,
    mcloud_cws,
    oracle_recruiting,
    paycor,
    peoplesoft,
    phenom,
    sap_successfactors,
    shopify,
    smartrecruiters,
    static_html,
    startdate,
    talentbrew,
    teamtailor,
    uber,
    ukg_pro,
    workday,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
    clear_source_scan_metadata,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import DEFAULT_REGISTRY_PATH, load_target_companies
from src.scraper.company_careers.relevance import is_relevant_job
from src.scraper.company_careers.parent_sources import (
    DEFAULT_PARENT_MAPPING_PATH,
    DEFAULT_ORIGINAL_COMPANY_INVENTORY_PATH,
    ParentSourceMapping,
    annotate_child_brand_metadata,
    load_parent_source_mappings,
    original_company_coverage,
)


AdapterFn = Callable[[requests.Session, CompanyTarget, int], List[JobRecord]]
JobFilterFn = Callable[[JobRecord], Optional[str]]
LocationFilterFn = Callable[[JobRecord], Optional[str]]
SessionFactory = Callable[[], requests.Session]


ADAPTERS: dict[str, AdapterFn] = {
    "adp": adp.fetch_jobs,
    "apple": apple.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
    "workday": workday.fetch_jobs,
    "oracle_recruiting": oracle_recruiting.fetch_jobs,
    "phenom": phenom.fetch_jobs,
    "sap_successfactors": sap_successfactors.fetch_jobs,
    "eightfold": eightfold.fetch_jobs,
    "avature": avature.fetch_jobs,
    "icims": icims.fetch_jobs,
    "static_html": static_html.fetch_jobs,
    "javascript_embedded_data": embedded_data.fetch_jobs,
    "exalead": exalead.fetch_jobs,
    "browser_rendered_html": browser_rendered_html.fetch_jobs,
    "clearcompany": clearcompany.fetch_jobs,
    "cornerstone_csod": cornerstone_csod.fetch_jobs,
    "dayforce": dayforce.fetch_jobs,
    "deel": deel.fetch_jobs,
    "bamboohr": bamboohr.fetch_jobs,
    "beesite": beesite.fetch_jobs,
    "startdate": startdate.fetch_jobs,
    "talentbrew": talentbrew.fetch_jobs,
    "teamtailor": teamtailor.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
    "hawksearch_v2": hawksearch_v2.fetch_jobs,
    "hibob": hibob.fetch_jobs,
    "ukg_pro": ukg_pro.fetch_jobs,
    "paycor": paycor.fetch_jobs,
    "peoplesoft": peoplesoft.fetch_jobs,
    "mcloud_cws": mcloud_cws.fetch_jobs,
    "amazon": amazon.fetch_jobs,
    "custom:amazon": amazon.fetch_jobs,
    "shopify": shopify.fetch_jobs,
    "custom:shopify": shopify.fetch_jobs,
    "google": google.fetch_jobs,
    "custom:google": google.fetch_jobs,
    "meta": meta.fetch_jobs,
    "custom:meta": meta.fetch_jobs,
    "atlassian": atlassian.fetch_jobs,
    "custom:atlassian": atlassian.fetch_jobs,
    "uber": uber.fetch_jobs,
    "custom:uber": uber.fetch_jobs,
    "ibm_search": ibm_search.fetch_jobs,
    "custom:ibm_search": ibm_search.fetch_jobs,
}


class CompanyCareersScanner:
    def __init__(
        self,
        targets: Optional[List[CompanyTarget]] = None,
        config_path: str = DEFAULT_REGISTRY_PATH,
        session: Optional[requests.Session] = None,
        session_factory: SessionFactory = requests.Session,
        timeout: int = 60,
        parent_source_mappings: Optional[Sequence[ParentSourceMapping]] = None,
        parent_mapping_path: str = DEFAULT_PARENT_MAPPING_PATH,
        original_company_inventory_path: str = DEFAULT_ORIGINAL_COMPANY_INVENTORY_PATH,
    ):
        self.targets = (
            targets
            if targets is not None
            else load_target_companies(
                config_path,
                apply_reliability_repairs=(
                    Path(config_path).resolve() == Path(DEFAULT_REGISTRY_PATH).resolve()
                ),
            )
        )
        self.session = session or requests.Session()
        self._isolate_adp_sessions = session is None
        self._session_factory = session_factory
        self.timeout = timeout
        self.original_company_inventory_path = original_company_inventory_path
        self.parent_source_mappings = list(
            parent_source_mappings
            if parent_source_mappings is not None
            else load_parent_source_mappings(parent_mapping_path)
        )

    def scan(
        self,
        keywords: Optional[str] = None,
        locations: Optional[Iterable[str]] = None,
        max_jobs_per_company: Optional[int] = None,
        job_filter: Optional[JobFilterFn] = None,
        location_filter: Optional[LocationFilterFn] = None,
    ) -> CompanyCareersReport:
        """Scan one registry, sharing a single Chromium process when needed."""
        options = dict(
            keywords=keywords,
            locations=locations,
            max_jobs_per_company=max_jobs_per_company,
            job_filter=job_filter,
            location_filter=location_filter,
        )
        if not any(target.platform == "browser_rendered_html" for target in self.targets):
            return self._scan(**options)
        with browser_rendered_html.BrowserBatch(timeout=self.timeout) as browser_batch:
            return self._scan(browser_batch=browser_batch, **options)

    def _scan(
        self,
        keywords: Optional[str] = None,
        locations: Optional[Iterable[str]] = None,
        max_jobs_per_company: Optional[int] = None,
        job_filter: Optional[JobFilterFn] = None,
        location_filter: Optional[LocationFilterFn] = None,
        browser_batch: Optional[browser_rendered_html.BrowserBatch] = None,
    ) -> CompanyCareersReport:
        results: List[CompanyScanResult] = []

        for target in self.targets:
            adapter = ADAPTERS.get(target.platform)
            if target.platform == "browser_rendered_html" and browser_batch is not None:
                adapter = lambda _session, current, timeout: browser_batch.fetch_jobs(current, timeout)
            if target.platform in {"manual", "manual_review"}:
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.MANUAL_REVIEW_REQUIRED,
                        error=f"Manual review required for platform: {target.platform}",
                        error_type="manual_review_required",
                        remediation="Add a reusable adapter or verify this company manually.",
                    )
                )
                continue
            if adapter is None:
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.NOT_IMPLEMENTED,
                        error=f"No adapter registered for platform: {target.platform}",
                        error_type="not_implemented",
                        remediation="Register an adapter for this platform or mark the company manual_review.",
                    )
                )
                continue

            adapter_session = self.session
            close_adapter_session = False
            if target.platform == "adp" and self._isolate_adp_sessions:
                # Workforce Now can set tenant cookies for the parent .adp.com
                # domain. Keep anonymous tenants isolated so one source cannot
                # change the request contract observed by the next source.
                adapter_session = self._session_factory()
                close_adapter_session = True

            try:
                clear_source_scan_metadata(adapter_session)
                fetched_jobs = self._dedupe(adapter(adapter_session, target, self.timeout))
                scan_metadata = source_scan_metadata(adapter_session)
                fetched_jobs = [
                    annotate_child_brand_metadata(job, self.parent_source_mappings)
                    for job in fetched_jobs
                ]
                mapped_child_brand_counts = self._mapped_brand_counts(fetched_jobs)
                matched_jobs, exclusion_counts = self._filter_jobs(
                    fetched_jobs,
                    keywords,
                    locations,
                    job_filter=job_filter,
                    location_filter=location_filter,
                )
                if max_jobs_per_company:
                    limited_count = max(0, len(matched_jobs) - max_jobs_per_company)
                    if limited_count:
                        exclusion_counts["limit"] = limited_count
                        scan_metadata["scan_complete"] = False
                        scan_metadata["scan_completeness_reason"] = (
                            "post_filter_safety_limit_reached"
                        )
                        scan_metadata["stop_reason"] = (
                            "post_filter_safety_limit_reached"
                        )
                        scan_metadata["safety_limit_reached"] = True
                    matched_jobs = matched_jobs[:max_jobs_per_company]

                status = (
                    CoverageStatus.FETCHED
                    if matched_jobs
                    else CoverageStatus.FETCHED_NO_MATCHING_ROLES
                )
                results.append(
                    CompanyScanResult(
                        target=target,
                        jobs=matched_jobs,
                        normalized_jobs=fetched_jobs,
                        status=status,
                        fetched_count=len(fetched_jobs),
                        scan_complete=bool(scan_metadata["scan_complete"]),
                        scan_completeness_reason=str(
                            scan_metadata["scan_completeness_reason"]
                        ),
                        total_available=scan_metadata["total_available"],
                        pages_requested=int(scan_metadata["pages_requested"]),
                        pages_completed=int(scan_metadata["pages_completed"]),
                        stop_reason=str(scan_metadata["stop_reason"]),
                        safety_limit_reached=bool(
                            scan_metadata["safety_limit_reached"]
                        ),
                        exclusion_counts=exclusion_counts,
                        mapped_child_brand_counts=mapped_child_brand_counts,
                    )
                )
            except AdapterError as exc:
                scan_metadata = source_scan_metadata(adapter_session)
                error_type, remediation = self._adapter_failure_details(str(exc))
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=str(exc),
                        error_type=error_type,
                        remediation=remediation,
                        scan_complete=False,
                        scan_completeness_reason=str(
                            scan_metadata["scan_completeness_reason"]
                        ),
                        total_available=scan_metadata["total_available"],
                        pages_requested=int(scan_metadata["pages_requested"]),
                        pages_completed=int(scan_metadata["pages_completed"]),
                        stop_reason=str(scan_metadata["stop_reason"]),
                        safety_limit_reached=bool(
                            scan_metadata["safety_limit_reached"]
                        ),
                    )
                )
            except requests.RequestException as exc:
                scan_metadata = source_scan_metadata(adapter_session)
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=str(exc),
                        error_type="request_error",
                        remediation="Retry the scan and verify network access or upstream availability.",
                        scan_complete=False,
                        scan_completeness_reason=str(
                            scan_metadata["scan_completeness_reason"]
                        ),
                        total_available=scan_metadata["total_available"],
                        pages_requested=int(scan_metadata["pages_requested"]),
                        pages_completed=int(scan_metadata["pages_completed"]),
                        stop_reason=str(scan_metadata["stop_reason"]),
                        safety_limit_reached=bool(
                            scan_metadata["safety_limit_reached"]
                        ),
                    )
                )
            except Exception as exc:
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=f"{exc.__class__.__name__}: {exc}",
                        error_type="unexpected_error",
                        remediation="Inspect the adapter stack locally; the scanner contained the failure to this company.",
                    )
                )
            finally:
                if close_adapter_session:
                    close = getattr(adapter_session, "close", None)
                    if callable(close):
                        close()

        return CompanyCareersReport(
            results=results,
            mapped_coverage=self._mapped_coverage(results),
            original_coverage=original_company_coverage(
                self.targets,
                self.parent_source_mappings,
                inventory_path=self.original_company_inventory_path,
            ),
        )

    @staticmethod
    def _mapped_brand_counts(jobs: Iterable[JobRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in jobs:
            for child_brand in job.child_brands:
                counts[child_brand] = counts.get(child_brand, 0) + 1
        return counts

    @staticmethod
    def _adapter_failure_details(error: str) -> tuple[str, str]:
        """Classify adapter failures without changing their fail-closed contract."""
        lowered = error.casefold()
        if (
            re.search(r"\bhttp (408|425|429|500|502|503|504)\b", lowered)
            or "transient upstream maintenance redirect" in lowered
        ):
            return (
                "transient_upstream_http",
                "Retry later; the bounded public source returned a transient HTTP failure.",
            )
        if "blocked response" in lowered or "captcha" in lowered or "challenge" in lowered:
            return (
                "external_access_blocked",
                "The official public source blocked bounded anonymous access; do not bypass its access controls.",
            )
        if "response exceeded" in lowered or "byte limit" in lowered or "size bound" in lowered:
            return (
                "response_size_safety_rejection",
                "Find a narrower structured source; do not raise the global response limit.",
            )
        if "browser-rendered" in lowered and ("timeout" in lowered or "selector" in lowered):
            return (
                "browser_rendering_failure",
                "Verify the reviewed selector or migrate to a public structured source.",
            )
        if "did not match" in lowered or "repeated stable" in lowered or "duplicate stable" in lowered:
            return (
                "validation_identity_mismatch",
                "Inspect listing/detail identifiers or pagination; preserve stable-ID validation.",
            )
        if "unsupported" in lowered:
            return (
                "unsupported_ats_variant",
                "Model and test the observed public ATS variant before enabling it.",
            )
        if "malformed" in lowered or "missing" in lowered or "unexpected content-type" in lowered:
            return (
                "adapter_schema_mismatch",
                "Inspect the upstream schema and add a bounded, fixture-backed contract if reusable.",
            )
        if re.search(r"\bhttp 404\b", lowered):
            return (
                "stale_company_configuration",
                "Verify the employer's current official career board and update the registry.",
            )
        return (
            "adapter_error",
            "Inspect the adapter endpoint, request shape, and parser assumptions.",
        )

    def _mapped_coverage(
        self,
        results: Sequence[CompanyScanResult],
    ) -> list[dict[str, Any]]:
        by_parent = {
            result.target.name.casefold(): result
            for result in results
        }
        rows: list[dict[str, Any]] = []
        for mapping in self.parent_source_mappings:
            parent = by_parent.get(mapping.parent_company.casefold())
            mapped_jobs = (
                parent.mapped_child_brand_counts.get(mapping.child_company, 0)
                if parent is not None
                else 0
            )
            if parent is None:
                status = CoverageStatus.PARENT_SOURCE_NOT_CONFIGURED
                parent_scan_status = "not_scanned"
                fetched_count = 0
                error = None
                warning = "Canonical parent source is not present in this registry."
            elif parent.status == CoverageStatus.FAILED_TECHNICAL:
                status = CoverageStatus.PARENT_FAILED_TECHNICAL
                parent_scan_status = parent.status
                fetched_count = parent.fetched_count
                error = parent.error
                warning = "Mapping inherited the canonical parent's technical failure."
            else:
                status = (
                    CoverageStatus.COVERED_VIA_PARENT_SOURCE
                    if mapped_jobs
                    else CoverageStatus.MAPPED_NO_CURRENT_JOBS
                )
                parent_scan_status = parent.status
                fetched_count = parent.fetched_count
                error = None
                warning = "" if mapped_jobs else "Parent scan succeeded with zero exact mapped jobs."

            rows.append(
                {
                    "company": mapping.child_company,
                    "platform": mapping.canonical_source,
                    "status": status,
                    "fetched_count": fetched_count,
                    "matched_count": mapped_jobs,
                    "mapped_jobs": mapped_jobs,
                    "coverage_relationship": "parent_company_source",
                    "canonical_parent": mapping.parent_company,
                    "parent_source": mapping.canonical_source,
                    "parent_scan_status": parent_scan_status,
                    "mapping_enabled": mapping.enabled,
                    "match_field": mapping.match_field,
                    "match_type": mapping.match_type,
                    "accepted_values": list(mapping.exact_values),
                    "error": error,
                    "error_type": "inherited_parent_failure" if error else None,
                    "remediation": None,
                    "warnings": [warning] if warning else [],
                }
            )
        return rows

    def _filter_jobs(
        self,
        jobs: List[JobRecord],
        keywords: Optional[str],
        locations: Optional[Iterable[str]],
        *,
        job_filter: Optional[JobFilterFn] = None,
        location_filter: Optional[LocationFilterFn] = None,
    ) -> tuple[List[JobRecord], dict[str, int]]:
        matched_jobs: List[JobRecord] = []
        exclusion_counts: dict[str, int] = {}

        for job in jobs:
            exclusion_reason = (
                job_filter(job)
                if job_filter is not None
                else (None if self._keyword_matches(job, keywords) else "keyword")
            )
            if exclusion_reason:
                exclusion_counts[exclusion_reason] = (
                    exclusion_counts.get(exclusion_reason, 0) + 1
                )
                continue
            location_exclusion = (
                location_filter(job)
                if location_filter is not None
                else (None if self._location_matches(job, locations) else "location")
            )
            if location_exclusion:
                exclusion_counts[location_exclusion] = (
                    exclusion_counts.get(location_exclusion, 0) + 1
                )
                continue
            matched_jobs.append(job)

        return matched_jobs, exclusion_counts

    def _keyword_matches(self, job: JobRecord, keywords: Optional[str]) -> bool:
        if not keywords:
            return True

        return is_relevant_job(job, keywords=keywords)

    def _location_matches(self, job: JobRecord, locations: Optional[Iterable[str]]) -> bool:
        requested = [loc.strip().lower() for loc in (locations or []) if loc and loc.strip()]
        if not requested:
            return True
        if not job.location:
            return True

        job_location = job.location.lower()
        return any(location in job_location or job_location in location for location in requested)

    def _dedupe(self, jobs: List[JobRecord]) -> List[JobRecord]:
        seen: set[tuple[str, str, str]] = set()
        unique_jobs: List[JobRecord] = []
        for job in jobs:
            source_identity = (job.source or job.company).strip().casefold()
            stable_id = job.job_id.strip()
            if stable_id:
                key = ("id", source_identity, stable_id)
            elif job.url.strip():
                key = ("url", source_identity, job.url.split("#", 1)[0].rstrip("/"))
            else:
                key = (
                    "fallback",
                    source_identity,
                    f"{job.title.casefold()}\x1f{job.location.casefold()}",
                )
            if key in seen:
                continue
            seen.add(key)
            unique_jobs.append(job)
        return unique_jobs
