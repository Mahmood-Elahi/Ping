"""Data models for company career-site scanning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class CoverageStatus:
    FETCHED = "fetched"
    FETCHED_NO_MATCHING_ROLES = "fetched_no_matching_roles"
    PARTIAL_LINKEDIN_ONLY = "partial_linkedin_only"
    FAILED_TECHNICAL = "failed_technical"
    NOT_IMPLEMENTED = "not_implemented"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class AdapterError(Exception):
    """Raised when a career-site adapter cannot fetch or parse a response."""


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
        }


@dataclass
class CompanyTarget:
    name: str
    platform: str
    slug: Optional[str] = None
    tenant: Optional[str] = None
    wd_subdomain: Optional[str] = None
    site_id: Optional[str] = None
    site_ids: List[str] = field(default_factory=list)
    careers_url: Optional[str] = None
    endpoint_url: Optional[str] = None
    site_number: Optional[str] = None
    organization_id: Optional[str] = None
    locale: Optional[str] = None
    search_terms: List[str] = field(default_factory=list)
    max_pages: int = 5

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompanyTarget":
        return cls(
            name=str(data["name"]),
            platform=str(data["platform"]),
            slug=data.get("slug"),
            tenant=data.get("tenant"),
            wd_subdomain=data.get("wd_subdomain"),
            site_id=data.get("site_id"),
            site_ids=list(data.get("site_ids") or []),
            careers_url=data.get("careers_url"),
            endpoint_url=data.get("endpoint_url"),
            site_number=data.get("site_number"),
            organization_id=data.get("organization_id"),
            locale=data.get("locale"),
            search_terms=list(data.get("search_terms") or []),
            max_pages=int(data.get("max_pages") or 5),
        )


@dataclass
class CompanyScanResult:
    target: CompanyTarget
    jobs: List[JobRecord] = field(default_factory=list)
    status: str = CoverageStatus.FETCHED
    error: Optional[str] = None
    error_type: Optional[str] = None
    remediation: Optional[str] = None
    fetched_count: int = 0
    exclusion_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def matched_count(self) -> int:
        return len(self.jobs)

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
            "matched_count": self.matched_count,
            "exclusion_counts": dict(self.exclusion_counts),
            "error": self.error,
            "error_type": self.error_type,
            "remediation": self.remediation,
        }


@dataclass
class CompanyCareersReport:
    results: List[CompanyScanResult]

    @property
    def jobs(self) -> List[JobRecord]:
        return [job for result in self.results for job in result.jobs]

    def coverage(self) -> List[Dict[str, Any]]:
        return [result.to_coverage_dict() for result in self.results]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_companies": len(self.results),
            "total_jobs": len(self.jobs),
            "coverage": self.coverage(),
            "jobs": [job.to_dict() for job in self.jobs],
        }
