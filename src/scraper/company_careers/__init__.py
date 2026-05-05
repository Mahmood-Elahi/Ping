"""Reusable company career-site scanner."""

from src.scraper.company_careers.models import (
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    JobRecord,
)
from src.scraper.company_careers.scanner import CompanyCareersScanner

__all__ = [
    "CompanyCareersReport",
    "CompanyCareersScanner",
    "CompanyScanResult",
    "CompanyTarget",
    "CoverageStatus",
    "JobRecord",
]
