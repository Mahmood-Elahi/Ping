"""Data models for company career-site scanning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


class CoverageStatus:
    FETCHED = "fetched"
    FETCHED_NO_MATCHING_ROLES = "fetched_no_matching_roles"
    PARTIAL_LINKEDIN_ONLY = "partial_linkedin_only"
    FAILED_TECHNICAL = "failed_technical"
    NOT_IMPLEMENTED = "not_implemented"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    COVERED_VIA_PARENT_SOURCE = "covered_via_parent_source"
    MAPPED_NO_CURRENT_JOBS = "mapped_no_current_jobs"
    PARENT_FAILED_TECHNICAL = "parent_failed_technical"
    PARENT_SOURCE_NOT_CONFIGURED = "parent_source_not_configured"


class AdapterError(Exception):
    """Raised when a career-site adapter cannot fetch or parse a response."""


ADAPTER_SCAN_METADATA_ATTRIBUTE = "job_radar_scan_metadata"


# These are enumeration safety budgets, not API page sizes or presentation
# limits.  Adapters with fixed/native page sizes validate those independently.
# Historical registry artifacts intentionally retain the original limits.
# The active-registry loader asks ``CompanyTarget.from_dict`` to migrate only
# the exact legacy values below, avoiding append-only rollout-data rewrites.
ADAPTER_SAFETY_BUDGET_DEFAULTS: Dict[str, Dict[str, int]] = {
    "ashby": {"max_pages": 1, "max_jobs": 10_000},
    "greenhouse": {"max_pages": 1, "max_jobs": 10_000},
    "lever": {"max_pages": 25, "max_jobs": 500},
    "phenom": {"max_pages": 50, "max_jobs": 500},
    "sap_successfactors": {"max_pages": 20, "max_jobs": 500},
    "workday": {"max_pages": 25, "max_jobs": 500},
}

LEGACY_ADAPTER_SAFETY_BUDGETS: Dict[str, Dict[str, set[int]]] = {
    platform: {"max_pages": {1, 2}, "max_jobs": {20}}
    for platform in ADAPTER_SAFETY_BUDGET_DEFAULTS
}


def _adapter_safety_budget(
    data: Mapping[str, Any],
    platform: str,
    field_name: str,
    fallback: int,
    *,
    migrate_legacy: bool,
) -> int:
    configured = data.get(field_name)
    defaults = ADAPTER_SAFETY_BUDGET_DEFAULTS.get(platform)
    if defaults is None:
        return int(configured or fallback)
    if configured is None and migrate_legacy:
        return defaults[field_name]
    if configured is None:
        return fallback
    value = int(configured)
    legacy_values = LEGACY_ADAPTER_SAFETY_BUDGETS[platform][field_name]
    return defaults[field_name] if migrate_legacy and value in legacy_values else value


class SourceScanMetadata(dict[str, Any]):
    """Serializable adapter evidence describing whether enumeration completed."""

    def __init__(self, **values: Any) -> None:
        super().__init__(
            scan_complete=False,
            scan_completeness_reason="adapter_completeness_not_proven",
            total_available=None,
            pages_requested=0,
            pages_completed=0,
            stop_reason="not_started",
            safety_limit_reached=False,
        )
        self.update(values)

    @classmethod
    def from_value(cls, value: Any) -> "SourceScanMetadata":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(**dict(value))
        return cls()


def publish_source_scan_metadata(session: Any, metadata: SourceScanMetadata) -> None:
    setattr(session, ADAPTER_SCAN_METADATA_ATTRIBUTE, metadata)


def clear_source_scan_metadata(session: Any) -> None:
    if hasattr(session, ADAPTER_SCAN_METADATA_ATTRIBUTE):
        delattr(session, ADAPTER_SCAN_METADATA_ATTRIBUTE)


def source_scan_metadata(session: Any) -> SourceScanMetadata:
    return SourceScanMetadata.from_value(
        getattr(session, ADAPTER_SCAN_METADATA_ATTRIBUTE, None)
    )


def _clean_list(values: Optional[List[Any]]) -> List[str]:
    if not values:
        return []
    return [str(value).strip() for value in values if str(value).strip()]


@dataclass
class JobRecord:
    job_id: str
    title: str
    company: str
    location: str
    url: str
    updated_at: str
    source: str
    departments: List[str] = field(default_factory=list)
    offices: List[str] = field(default_factory=list)
    description: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.job_id = str(self.job_id or "").strip()
        self.title = str(self.title or "").strip()
        self.company = str(self.company or "").strip()
        self.location = str(self.location or "").strip()
        self.url = str(self.url or "").strip()
        self.updated_at = str(self.updated_at or "").strip()
        self.source = str(self.source or "").strip()
        self.departments = _clean_list(self.departments)
        self.offices = _clean_list(self.offices)
        if self.description is not None:
            self.description = str(self.description)
        self.raw_metadata = dict(self.raw_metadata or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "url": self.url,
            "updated_at": self.updated_at,
            "source": self.source,
            "departments": self.departments,
            "offices": self.offices,
            "description": self.description,
            "raw_metadata": self.raw_metadata,
            "child_brands": self.child_brands,
        }

    @property
    def child_brands(self) -> List[str]:
        """Return deterministic, case-insensitively deduplicated brand metadata."""
        values = self.raw_metadata.get("child_brands")
        if not isinstance(values, list):
            return []
        unique: Dict[str, str] = {}
        for value in values:
            cleaned = str(value).strip()
            if cleaned:
                unique.setdefault(cleaned.casefold(), cleaned)
        return [unique[key] for key in sorted(unique)]


@dataclass
class CompanyTarget:
    name: str
    platform: str
    slug: Optional[str] = None
    tenant: Optional[str] = None
    wd_subdomain: Optional[str] = None
    site_id: Optional[str] = None
    site_ids: List[str] = field(default_factory=list)
    applied_facets: Dict[str, List[str]] = field(default_factory=dict)
    careers_url: Optional[str] = None
    search_url: Optional[str] = None
    endpoint_variant: Optional[str] = None
    endpoint_url: Optional[str] = None
    listing_url: Optional[str] = None
    pagination_url_template: Optional[str] = None
    listing_selector: Optional[str] = None
    job_card_selector: Optional[str] = None
    title_selector: Optional[str] = None
    location_selector: Optional[str] = None
    fixed_location: Optional[str] = None
    detail_link_selector: Optional[str] = None
    external_id_attribute: Optional[str] = None
    external_id_selector: Optional[str] = None
    external_id_regex: Optional[str] = None
    date_selector: Optional[str] = None
    department_selector: Optional[str] = None
    next_page_selector: Optional[str] = None
    empty_selector: Optional[str] = None
    allow_url_id_fallback: bool = False
    browser_wait_selector: Optional[str] = None
    browser_scroll_steps: int = 0
    browser_static_variant: Optional[str] = None
    browser_identity_selector: Optional[str] = None
    browser_identity_regex: Optional[str] = None
    identity_regex: Optional[str] = None
    job_attribution_regex: Optional[str] = None
    job_attribution_exclude_regex: Optional[str] = None
    embedded_script_selector: Optional[str] = None
    embedded_json_path: Optional[str] = None
    reference_url_attribute: Optional[str] = None
    id_field: Optional[str] = None
    title_field: Optional[str] = None
    location_field: Optional[str] = None
    location_fields: List[str] = field(default_factory=list)
    url_field: Optional[str] = None
    date_field: Optional[str] = None
    department_field: Optional[str] = None
    remote_field: Optional[str] = None
    job_attribution_field: Optional[str] = None
    job_attribution_selector: Optional[str] = None
    department_allowlist: List[str] = field(default_factory=list)
    max_scripts: int = 20
    max_response_bytes: int = 3_000_000
    max_script_bytes: int = 1_000_000
    site_number: Optional[str] = None
    organization_id: Optional[str] = None
    board_id: Optional[str] = None
    client_guid: Optional[str] = None
    index_name: Optional[str] = None
    ref_num: Optional[str] = None
    locale: Optional[str] = None
    allowed_source_hosts: List[str] = field(default_factory=list)
    allowed_apply_hosts: List[str] = field(default_factory=list)
    apply_url_template: Optional[str] = None
    apply_url_id_query_param: Optional[str] = None
    search_terms: List[str] = field(default_factory=list)
    max_pages: int = 5
    max_jobs: int = 20
    page_size: int = 10

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        apply_legacy_safety_defaults: bool = False,
    ) -> "CompanyTarget":
        platform = str(data["platform"])
        return cls(
            name=str(data["name"]),
            platform=platform,
            slug=data.get("slug"),
            tenant=data.get("tenant"),
            wd_subdomain=data.get("wd_subdomain"),
            site_id=data.get("site_id"),
            site_ids=list(data.get("site_ids") or []),
            applied_facets={
                str(key): _clean_list(value if isinstance(value, list) else [])
                for key, value in dict(data.get("applied_facets") or {}).items()
            },
            careers_url=data.get("careers_url"),
            search_url=data.get("search_url"),
            endpoint_variant=data.get("endpoint_variant"),
            endpoint_url=data.get("endpoint_url"),
            listing_url=data.get("listing_url"),
            pagination_url_template=data.get("pagination_url_template"),
            listing_selector=data.get("listing_selector"),
            job_card_selector=data.get("job_card_selector"),
            title_selector=data.get("title_selector"),
            location_selector=data.get("location_selector"),
            fixed_location=data.get("fixed_location"),
            detail_link_selector=data.get("detail_link_selector"),
            external_id_attribute=data.get("external_id_attribute"),
            external_id_selector=data.get("external_id_selector"),
            external_id_regex=data.get("external_id_regex"),
            date_selector=data.get("date_selector"),
            department_selector=data.get("department_selector"),
            next_page_selector=data.get("next_page_selector"),
            empty_selector=data.get("empty_selector"),
            allow_url_id_fallback=bool(data.get("allow_url_id_fallback", False)),
            browser_wait_selector=data.get("browser_wait_selector"),
            browser_scroll_steps=int(data.get("browser_scroll_steps") or 0),
            browser_static_variant=data.get("browser_static_variant"),
            browser_identity_selector=data.get("browser_identity_selector"),
            browser_identity_regex=data.get("browser_identity_regex"),
            identity_regex=data.get("identity_regex"),
            job_attribution_regex=data.get("job_attribution_regex"),
            job_attribution_exclude_regex=data.get("job_attribution_exclude_regex"),
            embedded_script_selector=data.get("embedded_script_selector"),
            embedded_json_path=data.get("embedded_json_path"),
            reference_url_attribute=data.get("reference_url_attribute"),
            id_field=data.get("id_field"),
            title_field=data.get("title_field"),
            location_field=data.get("location_field"),
            location_fields=list(data.get("location_fields") or []),
            url_field=data.get("url_field"),
            date_field=data.get("date_field"),
            department_field=data.get("department_field"),
            remote_field=data.get("remote_field"),
            job_attribution_field=data.get("job_attribution_field"),
            job_attribution_selector=data.get("job_attribution_selector"),
            department_allowlist=list(data.get("department_allowlist") or []),
            max_scripts=int(data.get("max_scripts") or 20),
            max_response_bytes=int(data.get("max_response_bytes") or 3_000_000),
            max_script_bytes=int(data.get("max_script_bytes") or 1_000_000),
            site_number=data.get("site_number"),
            organization_id=data.get("organization_id"),
            board_id=data.get("board_id"),
            client_guid=data.get("client_guid"),
            index_name=data.get("index_name"),
            ref_num=data.get("ref_num"),
            locale=data.get("locale"),
            allowed_source_hosts=list(data.get("allowed_source_hosts") or []),
            allowed_apply_hosts=list(data.get("allowed_apply_hosts") or []),
            apply_url_template=data.get("apply_url_template"),
            apply_url_id_query_param=data.get("apply_url_id_query_param"),
            search_terms=list(data.get("search_terms") or []),
            max_pages=_adapter_safety_budget(
                data,
                platform,
                "max_pages",
                5,
                migrate_legacy=apply_legacy_safety_defaults,
            ),
            max_jobs=_adapter_safety_budget(
                data,
                platform,
                "max_jobs",
                20,
                migrate_legacy=apply_legacy_safety_defaults,
            ),
            page_size=int(data.get("page_size") or 10),
        )


@dataclass
class CompanyScanResult:
    target: CompanyTarget
    jobs: List[JobRecord] = field(default_factory=list)
    normalized_jobs: List[JobRecord] = field(default_factory=list, repr=False)
    status: str = CoverageStatus.FETCHED
    error: Optional[str] = None
    error_type: Optional[str] = None
    remediation: Optional[str] = None
    fetched_count: int = 0
    scan_complete: bool = False
    scan_completeness_reason: str = "not_established"
    total_available: Optional[int] = None
    pages_requested: int = 0
    pages_completed: int = 0
    stop_reason: str = "not_started"
    safety_limit_reached: bool = False
    exclusion_counts: Dict[str, int] = field(default_factory=dict)
    mapped_child_brand_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def matched_count(self) -> int:
        return len(self.jobs)

    @property
    def scan_health(self) -> str:
        if self.status in {
            CoverageStatus.FETCHED,
            CoverageStatus.FETCHED_NO_MATCHING_ROLES,
        }:
            return "complete" if self.scan_complete else "incomplete"
        return "failed"

    def to_coverage_dict(self) -> Dict[str, Any]:
        return {
            "company": self.target.name,
            "platform": self.target.platform,
            "slug": self.target.slug,
            "tenant": self.target.tenant,
            "site_id": self.target.site_id,
            "site_ids": self.target.site_ids,
            "status": self.status,
            "fetched_count": self.fetched_count,
            "scan_complete": self.scan_complete,
            "scan_health": self.scan_health,
            "scan_completeness_reason": self.scan_completeness_reason,
            "total_available": self.total_available,
            "pages_requested": self.pages_requested,
            "pages_completed": self.pages_completed,
            "stop_reason": self.stop_reason,
            "safety_limit_reached": self.safety_limit_reached,
            "matched_count": self.matched_count,
            "exclusion_counts": dict(self.exclusion_counts),
            "error": self.error,
            "error_type": self.error_type,
            "remediation": self.remediation,
        }


@dataclass
class CompanyCareersReport:
    results: List[CompanyScanResult]
    mapped_coverage: List[Dict[str, Any]] = field(default_factory=list)
    original_coverage: Dict[str, Any] = field(default_factory=dict)

    @property
    def jobs(self) -> List[JobRecord]:
        return [job for result in self.results for job in result.jobs]

    @property
    def normalized_jobs(self) -> List[JobRecord]:
        """Return all normalized jobs, including jobs later excluded by filters."""
        return [job for result in self.results for job in result.normalized_jobs]

    def coverage(self) -> List[Dict[str, Any]]:
        return [result.to_coverage_dict() for result in self.results] + [
            dict(item) for item in self.mapped_coverage
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_companies": len(self.results),
            "canonical_sources_checked": len(self.results),
            "mapped_child_brands": len(self.mapped_coverage),
            "original_coverage": dict(self.original_coverage),
            "total_jobs": len(self.jobs),
            "coverage": self.coverage(),
            "jobs": [job.to_dict() for job in self.jobs],
        }
