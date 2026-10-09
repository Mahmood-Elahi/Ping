"""Reusable company career-site scanner."""

from src.scraper.company_careers.models import (
    AdapterResult,
    CompanyCareersReport,
    CompanyScanResult,
    CompanyTarget,
    CoverageStatus,
    InventoryItem,
    ItemFailure,
    JobRecord,
    SourceScanMetadata,
)
from src.scraper.company_careers.scanner import CompanyCareersScanner

__all__ = [
    "AdapterResult",
    "CompanyCareersReport",
    "CompanyCareersScanner",
    "CompanyScanResult",
    "CompanyTarget",
    "CoverageStatus",
    "InventoryItem",
    "ItemFailure",
    "JobRecord",
    "SourceScanMetadata",
]
