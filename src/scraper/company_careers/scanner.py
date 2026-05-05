"""Company career-site scanner orchestration."""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional

import requests

from src.scraper.company_careers.adapters import (
    amazon,
    ashby,
    atlassian,
    google,
    greenhouse,
    ibm_search,
    lever,
    meta,
    shopify,
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


AdapterFn = Callable[[requests.Session, CompanyTarget, int], List[JobRecord]]


ADAPTERS: dict[str, AdapterFn] = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
    "workday": workday.fetch_jobs,
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
    ):
        self.targets = targets if targets is not None else load_target_companies(config_path)
        self.session = session or requests.Session()
        self.timeout = timeout

    def scan(
        self,
        keywords: Optional[str] = None,
        locations: Optional[Iterable[str]] = None,
        max_jobs_per_company: Optional[int] = None,
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
                matched_jobs = self._filter_jobs(fetched_jobs, keywords, locations)
                if max_jobs_per_company:
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

        return CompanyCareersReport(results=results)

    def _filter_jobs(
        self,
        jobs: List[JobRecord],
        keywords: Optional[str],
        locations: Optional[Iterable[str]],
    ) -> List[JobRecord]:
        return [
            job
            for job in jobs
            if self._keyword_matches(job, keywords) and self._location_matches(job, locations)
        ]

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
