"""Company career-site scanner orchestration."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
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
    public_json,
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
    SourceScanMetadata,
    clear_source_scan_metadata,
    publish_source_scan_metadata,
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
from src.scraper.company_careers.source_health import finalize_source_scan


AdapterFn = Callable[[requests.Session, CompanyTarget, int], List[JobRecord]]
JobFilterFn = Callable[[JobRecord], Optional[str]]
LocationFilterFn = Callable[[JobRecord], Optional[str]]
SessionFactory = Callable[[], requests.Session]
DEFAULT_SCAN_WORKERS = 6


def _completion_result_fields(metadata: SourceScanMetadata) -> dict[str, Any]:
    """Copy the stable completeness contract into persisted coverage rows."""
    return {
        "scan_complete": bool(metadata["scan_complete"]),
        "scan_completeness_reason": str(metadata["scan_completeness_reason"]),
        "total_available": metadata["total_available"],
        "authoritative_total_present": bool(
            metadata.get("authoritative_total_present")
        ),
        "pages_requested": int(metadata["pages_requested"]),
        "pages_completed": int(metadata["pages_completed"]),
        "stop_reason": str(metadata["stop_reason"]),
        "safety_limit_reached": bool(metadata["safety_limit_reached"]),
        "terminal_evidence_type": metadata.get("terminal_evidence_type"),
        "terminal_evidence_value": metadata.get("terminal_evidence_value"),
        "has_more": metadata.get("has_more"),
        "next_url_present": metadata.get("next_url_present"),
        "next_cursor_present": metadata.get("next_cursor_present"),
        "next_page": metadata.get("next_page"),
        "page_count": metadata.get("page_count"),
        "result_count": metadata.get("result_count"),
        "end_index": metadata.get("end_index"),
        "disabled_next_control": bool(metadata.get("disabled_next_control")),
        "explicit_final_page": bool(metadata.get("explicit_final_page")),
        "terminal_page_observed": bool(metadata.get("terminal_page_observed")),
        "complete_collection_response": bool(
            metadata.get("complete_collection_response")
        ),
        "scan_state": str(metadata.get("scan_state") or "incomplete"),
        "source_health_code": str(
            metadata.get("source_health_code") or "pagination_unproven"
        ),
        "source_activation_state": str(
            metadata.get("source_activation_state") or "active"
        ),
        "jobs_collected": int(metadata.get("jobs_collected") or 0),
        "original_configured_url": metadata.get("original_configured_url"),
        "final_url": metadata.get("final_url"),
        "final_host": metadata.get("final_host"),
        "redirect_count": int(metadata.get("redirect_count") or 0),
        "host_remained_approved": metadata.get("host_remained_approved"),
        "source_identity_status": str(
            metadata.get("source_identity_status") or "unproven"
        ),
        "rejected_record_count": int(metadata.get("rejected_record_count") or 0),
        "collection_changed_during_scan": bool(
            metadata.get("collection_changed_during_scan")
        ),
        "detail_requests_coupled": bool(metadata.get("detail_requests_coupled")),
        "pagination_evidence_status": str(
            metadata.get("pagination_evidence_status") or "unproven"
        ),
        "pagination_evidence_conflicting": bool(
            metadata.get("pagination_evidence_conflicting")
        ),
        "pagination_evidence_malformed": bool(
            metadata.get("pagination_evidence_malformed")
        ),
        "pagination_evidence_diagnostics": list(
            metadata.get("pagination_evidence_diagnostics") or []
        ),
    }


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
    "public_json": public_json.fetch_jobs,
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
        max_workers: int = DEFAULT_SCAN_WORKERS,
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
        if int(max_workers) < 1:
            raise ValueError("max_workers must be at least 1")
        self.session = session
        self._session_factory = session_factory
        # Injected sessions are normally deterministic test doubles and may not
        # be thread-safe. Production callers get one isolated Session per source.
        self.max_workers = 1 if session is not None else int(max_workers)
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
        browser_batch = browser_rendered_html.BrowserBatch(timeout=self.timeout)
        try:
            browser_batch.__enter__()
        except Exception as exc:
            return self._scan(browser_batch_error=exc, **options)
        try:
            return self._scan(browser_batch=browser_batch, **options)
        finally:
            try:
                browser_batch.close()
            except Exception:
                # Browser cleanup cannot invalidate already-contained source
                # results or prevent publication of independent HTTP sources.
                pass

    def _scan(
        self,
        keywords: Optional[str] = None,
        locations: Optional[Iterable[str]] = None,
        max_jobs_per_company: Optional[int] = None,
        job_filter: Optional[JobFilterFn] = None,
        location_filter: Optional[LocationFilterFn] = None,
        browser_batch: Optional[browser_rendered_html.BrowserBatch] = None,
        browser_batch_error: Optional[Exception] = None,
    ) -> CompanyCareersReport:
        results: list[Optional[CompanyScanResult]] = [None] * len(self.targets)
        indexed_targets = list(enumerate(self.targets))

        if self.max_workers == 1:
            for index, target in indexed_targets:
                results[index] = self._scan_target(
                    target,
                    keywords=keywords,
                    locations=locations,
                    max_jobs_per_company=max_jobs_per_company,
                    job_filter=job_filter,
                    location_filter=location_filter,
                    browser_batch=browser_batch,
                    browser_batch_error=browser_batch_error,
                )
        else:
            browser_targets = [
                pair for pair in indexed_targets
                if pair[1].platform == "browser_rendered_html"
            ]
            worker_targets = [
                pair for pair in indexed_targets
                if pair[1].platform != "browser_rendered_html"
            ]
            worker_count = min(self.max_workers, max(1, len(worker_targets)))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="job-radar-source",
            ) as executor:
                futures: dict[Future[CompanyScanResult], int] = {
                    executor.submit(
                        self._scan_target,
                        target,
                        keywords=keywords,
                        locations=locations,
                        max_jobs_per_company=max_jobs_per_company,
                        job_filter=job_filter,
                        location_filter=location_filter,
                        browser_batch=None,
                        browser_batch_error=None,
                    ): index
                    for index, target in worker_targets
                }
                # Playwright's sync API stays on the owning thread and reuses a
                # single Chromium process, while ordinary HTTP sources progress.
                for index, target in browser_targets:
                    results[index] = self._scan_target(
                        target,
                        keywords=keywords,
                        locations=locations,
                        max_jobs_per_company=max_jobs_per_company,
                        job_filter=job_filter,
                        location_filter=location_filter,
                        browser_batch=browser_batch,
                        browser_batch_error=browser_batch_error,
                    )
                for future, index in futures.items():
                    results[index] = future.result()

        ordered_results = [result for result in results if result is not None]
        if len(ordered_results) != len(self.targets):
            raise RuntimeError("Company scan aggregation lost a source result")
        return CompanyCareersReport(
            results=ordered_results,
            mapped_coverage=self._mapped_coverage(ordered_results),
            original_coverage=original_company_coverage(
                self.targets,
                self.parent_source_mappings,
                inventory_path=self.original_company_inventory_path,
            ),
        )

    def _scan_target(
        self,
        target: CompanyTarget,
        *,
        keywords: Optional[str],
        locations: Optional[Iterable[str]],
        max_jobs_per_company: Optional[int],
        job_filter: Optional[JobFilterFn],
        location_filter: Optional[LocationFilterFn],
        browser_batch: Optional[browser_rendered_html.BrowserBatch],
        browser_batch_error: Optional[Exception],
    ) -> CompanyScanResult:
        """Scan one source and contain every adapter/session failure to it."""
        adapter = ADAPTERS.get(target.platform)
        if target.platform == "browser_rendered_html" and browser_batch is not None:
            def _browser_adapter(
                session: requests.Session,
                current: CompanyTarget,
                timeout: int,
            ) -> List[JobRecord]:
                jobs = browser_batch.fetch_jobs(current, timeout)
                metadata = SourceScanMetadata.from_value(
                    browser_batch.last_run.get("static_parser")
                )
                metadata["rendered_collection_observed"] = True
                terminal_stop = str(metadata.get("stop_reason") or "")
                browser_terminal = bool(
                    metadata.get("terminal_page_observed")
                    or metadata.get("complete_collection_response")
                    or metadata.get("has_more") is False
                    or metadata.get("next_url_present") is False
                    or metadata.get("disabled_next_control")
                    or metadata.get("explicit_final_page")
                    or terminal_stop
                    in {
                        "validated_zero_jobs",
                        "no_next_page",
                        "explicit_terminal_page",
                        "reported_total_reached",
                        "proven_structured_collection",
                    }
                )
                metadata["browser_terminal_evidence"] = browser_terminal
                if browser_terminal and not metadata.get(
                    "collection_changed_during_scan"
                ):
                    metadata["scan_complete"] = True
                    if metadata.get("scan_completeness_reason") in {
                        None,
                        "adapter_completeness_not_proven",
                    }:
                        metadata["scan_completeness_reason"] = (
                            "validated_browser_terminal"
                        )
                elif not browser_terminal:
                    metadata["scan_complete"] = False
                    metadata["scan_completeness_reason"] = (
                        "browser_exhaustion_not_proven"
                    )
                    if terminal_stop == "no_pagination_configured":
                        metadata["stop_reason"] = "browser_exhaustion_not_proven"
                publish_source_scan_metadata(session, metadata)
                return jobs

            adapter = _browser_adapter
        if target.platform == "browser_rendered_html" and browser_batch_error is not None:
            message = f"{browser_batch_error.__class__.__name__}: {browser_batch_error}"
            error_type, remediation = self._adapter_failure_details(message)
            return CompanyScanResult(
                target=target,
                status=CoverageStatus.FAILED_TECHNICAL,
                error=message,
                error_type=error_type,
                remediation=remediation,
                scan_complete=False,
                scan_completeness_reason="browser_batch_startup_failure",
                stop_reason="browser_batch_startup_failure",
                scan_state="failed",
                source_health_code="source_unreachable",
                source_activation_state=target.source_activation,
            )
        if target.platform in {"manual", "manual_review"}:
            return CompanyScanResult(
                target=target,
                status=CoverageStatus.MANUAL_REVIEW_REQUIRED,
                error=f"Manual review required for platform: {target.platform}",
                error_type="manual_review_required",
                remediation="Add a reusable adapter or verify this company manually.",
                scan_state="failed",
                source_health_code="pagination_unproven",
                source_activation_state=target.source_activation,
            )
        if adapter is None:
            return CompanyScanResult(
                target=target,
                status=CoverageStatus.NOT_IMPLEMENTED,
                error=f"No adapter registered for platform: {target.platform}",
                error_type="not_implemented",
                remediation="Register an adapter for this platform or mark the company manual_review.",
                scan_state="failed",
                source_health_code="source_unreachable",
                source_activation_state=target.source_activation,
            )

        adapter_session: Optional[requests.Session] = None
        close_adapter_session = self.session is None
        try:
                adapter_session = (
                    self.session if self.session is not None else self._session_factory()
                )
                clear_source_scan_metadata(adapter_session)
                fetched_jobs = self._dedupe(adapter(adapter_session, target, self.timeout))
                scan_metadata = source_scan_metadata(adapter_session)
                scan_metadata["original_configured_url"] = _configured_source_url(target)
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
                finalize_source_scan(
                    scan_metadata,
                    jobs_collected=len(fetched_jobs),
                    activation_state=target.source_activation,
                )
                return CompanyScanResult(
                        target=target,
                        jobs=matched_jobs,
                        normalized_jobs=fetched_jobs,
                        status=status,
                        fetched_count=len(fetched_jobs),
                        **_completion_result_fields(scan_metadata),
                        exclusion_counts=exclusion_counts,
                        mapped_child_brand_counts=mapped_child_brand_counts,
                )
        except AdapterError as exc:
                scan_metadata = source_scan_metadata(adapter_session)
                scan_metadata["original_configured_url"] = _configured_source_url(target)
                error_type, remediation = self._adapter_failure_details(str(exc))
                finalize_source_scan(
                    scan_metadata,
                    jobs_collected=int(scan_metadata.get("jobs_normalized") or 0),
                    failed=True,
                    failure_code=error_type,
                    activation_state=target.source_activation,
                )
                return CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=str(exc),
                        error_type=error_type,
                        remediation=remediation,
                        **_completion_result_fields(scan_metadata),
                )
        except requests.RequestException as exc:
                scan_metadata = source_scan_metadata(adapter_session)
                scan_metadata["original_configured_url"] = _configured_source_url(target)
                finalize_source_scan(
                    scan_metadata,
                    jobs_collected=int(scan_metadata.get("jobs_normalized") or 0),
                    failed=True,
                    failure_code="request_error",
                    activation_state=target.source_activation,
                )
                return CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=str(exc),
                        error_type="request_error",
                        remediation="Retry the scan and verify network access or upstream availability.",
                        **_completion_result_fields(scan_metadata),
                )
        except Exception as exc:
                scan_metadata = source_scan_metadata(adapter_session)
                scan_metadata["original_configured_url"] = _configured_source_url(target)
                finalize_source_scan(
                    scan_metadata,
                    jobs_collected=int(scan_metadata.get("jobs_normalized") or 0),
                    failed=True,
                    failure_code="unexpected_error",
                    activation_state=target.source_activation,
                )
                return CompanyScanResult(
                        target=target,
                        status=CoverageStatus.FAILED_TECHNICAL,
                        error=f"{exc.__class__.__name__}: {exc}",
                        error_type="unexpected_error",
                        remediation="Inspect the adapter stack locally; the scanner contained the failure to this company.",
                        **_completion_result_fields(scan_metadata),
                )
        finally:
            if close_adapter_session and adapter_session is not None:
                close = getattr(adapter_session, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        # Session cleanup is source-local and cannot invalidate
                        # a completed result or abort aggregation.
                        pass

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
        static_diagnostics = {
            "access_blocked": (
                "The public source blocked bounded anonymous access; do not bypass its access controls.",
            ),
            "html_too_large": (
                "The bounded HTML prefix did not expose a trusted contract; use a smaller official structured source.",
            ),
            "unsupported_listing_contract": (
                "The HTTP response did not match a reviewed structured or HTML listing contract.",
            ),
            "identity_missing": (
                "A listing lacked a title or stable structured/canonical identity; do not synthesize one from presentation text.",
            ),
            "unproven_empty_collection": (
                "The HTTP 200 response did not prove a zero-job collection, so absence reconciliation remains blocked.",
            ),
            "structured_payload_malformed": (
                "A bounded structured payload was present but malformed or internally inconsistent.",
            ),
        }
        for code, (remediation,) in static_diagnostics.items():
            if f"static source {code}:" in lowered:
                return code, remediation
        if (
            ("candidate_sites=[none]" in lowered or "no_valid_candidate" in lowered)
            and re.search(r"\bhttp (404|422)\b", lowered)
        ):
            return (
                "source_unresolved",
                "The configured source is stale and bounded authoritative discovery found no identity-verified replacement.",
            )
        if (
            "official careers page did not link the configured" in lowered
            or "official careers page no longer links the configured" in lowered
        ):
            return (
                "probable_ats_migration",
                "The official careers page no longer corroborates the configured ATS identity; verify a replacement before migrating the registry.",
            )
        if re.search(r"\bhttp (404|410)\b", lowered):
            return (
                "stale_board_identifier",
                "Verify the current official career board or ATS and migrate the registry only with strong identity evidence.",
            )
        if (
            re.search(r"\bhttp (408|425|429|500|502|503|504)\b", lowered)
            or "transient upstream maintenance redirect" in lowered
        ):
            return (
                "transient_upstream_http",
                "Retry later; the bounded public source returned a transient HTTP failure.",
            )
        if re.search(r"\bhttp 202\b", lowered):
            return (
                "unexpected_upstream_http",
                "Retry later; the listing request was accepted but did not return the reviewed response contract.",
            )
        if (
            "official careers page" in lowered
            and re.search(r"\bhttp 403\b|blocked|captcha|challenge", lowered)
        ):
            return (
                "official_page_blocked",
                "The official careers page blocked bounded anonymous verification; retain the configured source until identity can be checked manually.",
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
        if re.search(r"\bhttp (400|401|405|409|415|422)\b", lowered):
            return (
                "adapter_protocol_failure",
                "Verify the bounded public request contract and source configuration without weakening identity validation.",
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


def _configured_source_url(target: CompanyTarget) -> Optional[str]:
    for value in (
        target.endpoint_url,
        target.search_url,
        target.listing_url,
        target.careers_url,
    ):
        if str(value or "").strip():
            return str(value).strip()
    return None
