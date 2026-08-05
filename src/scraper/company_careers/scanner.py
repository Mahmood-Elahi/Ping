"""Company career-site scanner orchestration."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional, Sequence

import requests

from src.scraper.company_careers.adapters import (
    amazon,
    avature,
    ashby,
    atlassian,
    eightfold,
    embedded_data,
    google,
    greenhouse,
    ibm_search,
    icims,
    lever,
    meta,
    oracle_recruiting,
    phenom,
    sap_successfactors,
    shopify,
    static_html,
    uber,
    workday,
)
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
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


ADAPTERS: dict[str, AdapterFn] = {
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
        timeout: int = 60,
        parent_source_mappings: Optional[Sequence[ParentSourceMapping]] = None,
        parent_mapping_path: str = DEFAULT_PARENT_MAPPING_PATH,
        original_company_inventory_path: str = DEFAULT_ORIGINAL_COMPANY_INVENTORY_PATH,
    ):
        self.targets = targets if targets is not None else load_target_companies(config_path)
        self.session = session or requests.Session()
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
        results: List[CompanyScanResult] = []

        for target in self.targets:
            adapter = ADAPTERS.get(target.platform)
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

            try:
                fetched_jobs = self._dedupe(adapter(self.session, target, self.timeout))
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
                        status=status,
                        fetched_count=len(fetched_jobs),
                        exclusion_counts=exclusion_counts,
                        mapped_child_brand_counts=mapped_child_brand_counts,
                    )
                )
            except AdapterError as exc:
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=str(exc),
                        error_type="adapter_error",
                        remediation="Inspect the adapter endpoint, request shape, and parser assumptions.",
                    )
                )
            except requests.RequestException as exc:
                results.append(
                    CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=str(exc),
                        error_type="request_error",
                        remediation="Retry the scan and verify network access or upstream availability.",
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
            key = (job.url, job.job_id, job.title.lower())
            if key in seen:
                continue
            seen.add(key)
            unique_jobs.append(job)
        return unique_jobs
