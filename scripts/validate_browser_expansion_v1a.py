"""Build and anonymously validate Browser Expansion Batch A.

The batch prefers existing direct adapters over rendering. Browser candidates
share one Chromium process while every attempt receives a fresh context/page.
Only milestone registries, a reviewed CSV, and an ignored JSON report are
written; production, mappings, dashboards, and persistence are never touched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers import scanner
from src.scraper.company_careers.adapters import browser_rendered_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
CANDIDATE_CSV = ROOT / "config" / "company_browser_expansion_v1a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.browser_expansion_v1a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.browser_expansion_v1a_validated.yaml"
REPORT = ROOT / "output" / "browser_expansion_v1a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 148
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 143
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000
REVIEWED_COMPANIES = [
    "SpaceX",
    "Broadcom",
    "ASML",
    "Adyen",
    "Jane Street",
    "Goldman Sachs",
    "Bosch",
    "Aurora Innovation",
    "Dassault Systèmes",
    "DJI",
]
SAFE_STATUSES = {"validated_with_jobs", "validated_zero_jobs"}


def _direct(
    rank: int,
    name: str,
    platform: str,
    careers_url: str,
    *,
    identity_regex: str,
    allowed_apply_hosts: list[str],
    **identifiers: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "platform": platform,
        "careers_url": careers_url,
        "source_identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": rank,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    entry.update(identifiers)
    return entry


def _browser(
    rank: int,
    name: str,
    careers_url: str,
    listing_url: str,
    *,
    wait_selector: str,
    listing_selector: str,
    job_card_selector: str,
    title_selector: str,
    location_selector: Optional[str],
    detail_link_selector: str,
    allowed_apply_hosts: list[str],
    identity_selector: str,
    identity_regex: str,
    external_id_regex: Optional[str] = None,
    external_id_attribute: Optional[str] = None,
    department_selector: Optional[str] = None,
    date_selector: Optional[str] = None,
    scroll_steps: int = 0,
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": "browser_rendered_html",
        "careers_url": careers_url,
        "listing_url": listing_url,
        "endpoint_variant": "browser_rendered_html_v1",
        "browser_wait_selector": wait_selector,
        "browser_scroll_steps": scroll_steps,
        "browser_static_variant": "server_rendered_links",
        "browser_identity_selector": identity_selector,
        "browser_identity_regex": identity_regex,
        "listing_selector": listing_selector,
        "job_card_selector": job_card_selector,
        "title_selector": title_selector,
        "location_selector": location_selector,
        "detail_link_selector": detail_link_selector,
        "external_id_regex": external_id_regex,
        "external_id_attribute": external_id_attribute,
        "department_selector": department_selector,
        "date_selector": date_selector,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": rank,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _direct(
        14,
        "SpaceX",
        "greenhouse",
        "https://www.spacex.com/careers/",
        identity_regex=r"SpaceX",
        allowed_apply_hosts=["boards.greenhouse.io"],
        slug="spacex",
        search_url="https://boards.greenhouse.io/spacex",
    ),
    _direct(
        22,
        "Broadcom",
        "workday",
        "https://www.broadcom.com/company/careers",
        identity_regex=r"Broadcom",
        allowed_apply_hosts=["broadcom.wd1.myworkdayjobs.com"],
        tenant="broadcom",
        wd_subdomain="wd1",
        site_id="External_Career",
        search_url="https://broadcom.wd1.myworkdayjobs.com/External_Career",
    ),
    _browser(
        26,
        "ASML",
        "https://www.asml.com/en/careers",
        "https://www.asml.com/en/careers/find-your-job",
        wait_selector="a.search-results__item",
        listing_selector="a.search-results__item",
        job_card_selector="a.search-results__item",
        title_selector="h2.search-results-title-text",
        location_selector="ul.search-results__fields li:first-child",
        department_selector="ul.search-results__fields li:nth-child(2)",
        detail_link_selector=":self",
        external_id_regex=r"-([jJ][0-9]+)$",
        allowed_apply_hosts=["www.asml.com"],
        identity_selector="head title",
        identity_regex=r"ASML",
    ),
    _direct(
        62,
        "Adyen",
        "greenhouse",
        "https://careers.adyen.com/",
        identity_regex=r"Adyen",
        allowed_apply_hosts=["job-boards.greenhouse.io"],
        slug="adyen",
        search_url="https://job-boards.greenhouse.io/adyen",
    ),
    _direct(
        74,
        "Jane Street",
        "greenhouse",
        "https://www.janestreet.com/join-jane-street/",
        identity_regex=r"Jane Street",
        allowed_apply_hosts=["www.janestreet.com"],
        slug="janestreet",
        search_url="https://www.janestreet.com/join-jane-street/open-roles/",
    ),
    _browser(
        84,
        "Goldman Sachs",
        "https://www.goldmansachs.com/careers",
        "https://higher.gs.com/results",
        wait_selector="a[href^='/roles/']",
        listing_selector="a[href^=/roles/]",
        job_card_selector="a[href^=/roles/]",
        title_selector="span.gs-text:first-child",
        location_selector="div[data-testid=location]",
        detail_link_selector=":self",
        external_id_regex=r"/roles/([0-9]+)$",
        allowed_apply_hosts=["higher.gs.com"],
        identity_selector="head title",
        identity_regex=r"Goldman Sachs",
    ),
    _browser(
        131,
        "Aurora Innovation",
        "https://aurora.tech/careers",
        "https://aurora.tech/careers",
        wait_selector="div.section-job-board__list-item",
        listing_selector="div.section-job-board__list-item",
        job_card_selector="div.section-job-board__list-item",
        title_selector="p.section-job-board__list-item-heading",
        location_selector="p.section-job-board__list-item-meta span:nth-child(2)",
        department_selector="p.section-job-board__list-item-meta span:first-child",
        detail_link_selector="a.section-job-board__link",
        external_id_regex=r"/careers/([0-9a-f-]+)$",
        allowed_apply_hosts=["aurora.tech"],
        identity_selector="body",
        identity_regex=r"Aurora",
    ),
    _browser(
        145,
        "Dassault Systèmes",
        "https://www.3ds.com/careers",
        "https://www.3ds.com/careers/jobs",
        wait_selector="div.ds-jobs-details-wraper",
        listing_selector="div.ds-jobs-details-wraper",
        job_card_selector="div.ds-jobs-details-wraper",
        title_selector="h2.job-details-title",
        location_selector="div.job-details-place",
        department_selector="div.job-details-subtitle",
        detail_link_selector="div.job-details-apply a[href]",
        external_id_regex=r"[?&]job=([0-9]+)",
        allowed_apply_hosts=["talentacquisition.3ds.com"],
        identity_selector="head title",
        identity_regex=r"Dassault Systèmes",
    ),
    _browser(
        150,
        "DJI",
        "https://careers.dji.com/zh-CN/",
        "https://apply.careers.dji.com/social-recruitment/dji/170070?hash=%23%2Fjobs",
        wait_selector="a[href^='#/job/']",
        listing_selector="a[href^=#/job/]",
        job_card_selector="a[href^=#/job/]",
        title_selector="span[class*=title-]",
        location_selector=None,
        detail_link_selector=":self",
        external_id_attribute="href",
        allowed_apply_hosts=["apply.careers.dji.com"],
        identity_selector="head title",
        identity_regex=r"DJI|大疆",
    ),
]


EXCLUDED_REVIEWS: list[dict[str, Any]] = [
    {
        "master_rank": 96,
        "company": "Bosch",
        "careers_url": "https://jobs.bosch.com/en",
        "listing_url": "https://jobs.bosch.com/en",
        "current_platform": "smartrecruiters_public_endpoint",
        "adapter_platform": "none",
        "classification": "unsupported_public_endpoint",
        "identity_regex": r"Bosch",
        "exclusion_reason": (
            "The official Bosch site identifies SmartRecruiters/BoschGroup, but this repository "
            "has no existing declarative SmartRecruiters adapter; browser rendering is not "
            "required to establish the public endpoint and no company-specific branch was added."
        ),
    }
]


PLATFORM_NOTES = {
    "SpaceX": ("greenhouse", "existing_direct_adapter", "boards-api.greenhouse.io/v1/boards/spacex"),
    "Broadcom": ("workday", "existing_direct_adapter", "broadcom.wd1.myworkdayjobs.com"),
    "ASML": ("browser_rendered_html", "browser_required", "rendered ASML search-results cards"),
    "Adyen": ("greenhouse", "existing_direct_adapter", "boards-api.greenhouse.io/v1/boards/adyen"),
    "Jane Street": ("greenhouse", "existing_direct_adapter", "boards-api.greenhouse.io/v1/boards/janestreet"),
    "Goldman Sachs": ("browser_rendered_html", "browser_required", "rendered higher.gs.com role links"),
    "Bosch": ("none", "unsupported_public_endpoint", "SmartRecruiters BoschGroup public source"),
    "Aurora Innovation": ("browser_rendered_html", "browser_required", "rendered Aurora UUID role cards"),
    "Dassault Systèmes": ("browser_rendered_html", "browser_required", "rendered 3DS/Taleo detail card"),
    "DJI": ("browser_rendered_html", "browser_candidate", "rendered Mokahr hash-route cards"),
}


CSV_FIELDS = [
    "master_rank",
    "company",
    "official_careers_url",
    "listing_url",
    "current_platform",
    "adapter_platform",
    "implementation_path",
    "source_identity",
    "stable_id_contract",
    "allowed_apply_hosts",
    "anonymous_access",
    "validation_status",
    "safe_to_activate",
    "job_count_run_1",
    "job_count_run_2",
    "runtime_seconds_run_1",
    "runtime_seconds_run_2",
    "excluded",
    "exclusion_reason",
]


def _name_key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("name") or entry.get("company") or "").casefold().split())


def source_key(entry: Mapping[str, Any]) -> Optional[str]:
    if str(entry.get("platform") or "").casefold() != "browser_rendered_html":
        return registry_validator.source_key(entry)
    host = (urlparse(str(entry.get("listing_url") or "")).hostname or "").casefold()
    return f"browser_rendered_html:{host}" if host else None


def _duplicates(
    baseline: Iterable[Mapping[str, Any]], additions: Iterable[Mapping[str, Any]]
) -> tuple[dict[int, str], dict[int, str]]:
    name_owner = {_name_key(entry): str(entry.get("name")) for entry in baseline}
    source_owner = {source_key(entry): str(entry.get("name")) for entry in baseline if source_key(entry)}
    duplicate_names: dict[int, str] = {}
    duplicate_sources: dict[int, str] = {}
    for index, entry in enumerate(additions):
        name, source = _name_key(entry), source_key(entry)
        if name in name_owner:
            duplicate_names[index] = name_owner[name]
        else:
            name_owner[name] = str(entry["name"])
        if source and source in source_owner:
            duplicate_sources[index] = source_owner[source]
        elif source:
            source_owner[source] = str(entry["name"])
    return duplicate_names, duplicate_sources


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def _registry_bytes(production: Path, additions: Iterable[Mapping[str, Any]]) -> bytes:
    return registry_validator.build_candidate_registry_bytes(production, list(additions))


def reconcile() -> list[dict[str, Any]]:
    _, production = registry_validator.load_registry(PRODUCTION)
    if len(production) != HISTORICAL_PRODUCTION_COUNT:
        raise ValueError("Production registry is not the exact 148-entry baseline")
    if len(load_parent_source_mappings(PARENT_MAPPINGS)) != HISTORICAL_PARENT_MAPPINGS:
        raise ValueError("Parent mappings are not the exact four-entry baseline")
    reviewed = sorted(
        [entry["name"] for entry in CANDIDATE_ENTRIES]
        + [entry["company"] for entry in EXCLUDED_REVIEWS],
        key=lambda name: REVIEWED_COMPANIES.index(name),
    )
    if reviewed != REVIEWED_COMPANIES:
        raise ValueError("Batch A must review exactly the requested ten companies")
    duplicate_names, duplicate_sources = _duplicates(production, CANDIDATE_ENTRIES)
    if duplicate_names:
        raise ValueError(f"duplicate_company: {duplicate_names}")
    if duplicate_sources:
        raise ValueError(f"duplicate_source: {duplicate_sources}")
    for entry in CANDIDATE_ENTRIES:
        if int(entry["max_pages"]) > MAX_PAGES or int(entry["max_jobs"]) > MAX_JOBS:
            raise ValueError(f"{entry['name']}: candidate exceeds page or job limits")
        if int(entry["max_response_bytes"]) > MAX_RENDERED_HTML_BYTES:
            raise ValueError(f"{entry['name']}: candidate exceeds response byte limit")
        if entry["platform"] == "browser_rendered_html":
            browser_rendered_html.validate_target(CompanyTarget.from_dict(entry))
        elif entry["platform"] not in scanner.ADAPTERS:
            raise ValueError(f"{entry['name']}: unsupported existing adapter")
    return production


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    additions: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    chosen = list(additions if additions is not None else CANDIDATE_ENTRIES)
    if production.resolve() == PRODUCTION.resolve() and additions is None:
        reconcile()
    _, baseline = registry_validator.load_registry(production)
    duplicate_names, duplicate_sources = _duplicates(baseline, chosen)
    if duplicate_names:
        raise ValueError(f"duplicate_company: {duplicate_names}")
    if duplicate_sources:
        raise ValueError(f"duplicate_source: {duplicate_sources}")
    content = _registry_bytes(production, chosen)
    if not content.startswith(production.read_bytes()):
        raise ValueError("Candidate registry lost the exact production byte prefix")
    _write_bytes(destination, content)
    return content


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _verify_jobs(jobs: Iterable[Any], entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    inspected = list(jobs)[:MAX_JOBS]
    ids = [str(job.job_id) for job in inspected]
    urls = [str(job.url) for job in inspected]
    if not inspected:
        raise AdapterError("Candidate did not prove a valid non-empty collection")
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise AdapterError("Candidate has missing or duplicate stable IDs")
    if any(not value for value in urls) or len(urls) != len(set(urls)):
        raise AdapterError("Candidate apply URLs are missing or not job-specific")
    allowed = [str(value).casefold() for value in entry.get("allowed_apply_hosts") or []]
    for url in urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not _allowed_host(host, allowed):
            raise AdapterError("Candidate has an unofficial apply URL")
    return list(zip(ids, urls))


_REJECTED_TEXT = (
    "access denied",
    "request blocked",
    "verify you are human",
    "captcha",
    "consent required",
    "cookie consent required",
    "session required",
    "session expired",
)


def _identity_check(entry: Mapping[str, Any], session: requests.Session, timeout: int) -> dict[str, Any]:
    response = session.get(
        str(entry["careers_url"]),
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
        timeout=timeout,
    )
    if response.status_code in {401, 407}:
        raise AdapterError("Official careers identity page requires authentication")
    if response.status_code in {403, 429, 451}:
        raise AdapterError("Official careers identity page is blocked")
    if response.status_code != 200:
        raise AdapterError(f"Official careers identity page HTTP {response.status_code}")
    if not response.content or len(response.content) > MAX_RENDERED_HTML_BYTES:
        raise AdapterError("Official careers identity page is malformed or exceeded 3 MB")
    try:
        root = html.fromstring(response.content, base_url=response.url)
    except (TypeError, ValueError) as exc:
        raise AdapterError("Official careers identity page is malformed") from exc
    for node in root.xpath("//script|//style|//noscript"):
        node.drop_tree()
    text = " ".join(root.text_content().split())
    title = " ".join(root.xpath("string(//title)").split())
    folded = f"{title} {text}".casefold()
    marker = next((value for value in _REJECTED_TEXT if value in folded), None)
    if marker:
        raise AdapterError(f"Official careers identity page rejected ({marker})")
    if not re.search(str(entry["source_identity_regex"]), f"{title} {text}", re.IGNORECASE):
        raise AdapterError("Official careers page has missing, mixed, or ambiguous company identity")
    return {"status": response.status_code, "url": response.url, "bytes": len(response.content)}


def _status_for_error(exc: BaseException) -> str:
    message = str(exc).casefold()
    if "duplicate" in message and "id" in message:
        return "unstable_ids"
    if "apply" in message or "allowed-host" in message:
        return "unofficial_apply_url"
    if "identity" in message:
        return "mixed_or_ambiguous_identity"
    if "authentication" in message or "login" in message:
        return "authentication_required"
    if "session" in message:
        return "session_required"
    if "captcha" in message or "blocked" in message or "consent" in message:
        return "blocked"
    if "timeout" in message or "wait selector" in message:
        return "timeout_or_wait_selector_missing"
    if "malformed" in message or "byte" in message or "limit" in message:
        return "malformed_or_limit_failure"
    return "failed_technical"


def _run_direct(entry: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    session = requests.Session()
    try:
        identity = _identity_check(entry, session, timeout)
        adapter = scanner.ADAPTERS[str(entry["platform"])]
        jobs = adapter(session, CompanyTarget.from_dict(dict(entry)), timeout)
        signature = _verify_jobs(jobs, entry)
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": len(jobs),
            "jobs_normalized": len(signature),
            "stable_ids": [job_id for job_id, _url in signature],
            "apply_urls": [url for _job_id, url in signature],
            "pages_requested": 2,
            "identity": identity,
            "success": True,
        }
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": 0,
            "jobs_normalized": 0,
            "stable_ids": [],
            "apply_urls": [],
            "pages_requested": 2,
            "success": False,
            "status": _status_for_error(exc),
            "error": str(exc),
        }
    finally:
        session.close()


def _run_browser(
    entry: Mapping[str, Any], batch: browser_rendered_html.BrowserBatch, timeout: int
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        jobs = batch.fetch_jobs(CompanyTarget.from_dict(dict(entry)), timeout)
        signature = _verify_jobs(jobs, entry)
        diagnostics = dict(batch.last_run)
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": len(jobs),
            "jobs_normalized": len(signature),
            "stable_ids": [job_id for job_id, _url in signature],
            "apply_urls": [url for _job_id, url in signature],
            "pages_rendered": diagnostics.get("pages_rendered", 0),
            "rendered_bytes": diagnostics.get("rendered_bytes", 0),
            "rendered_urls": diagnostics.get("rendered_urls", []),
            "success": True,
        }
    except (AdapterError, ValueError, TypeError) as exc:
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": 0,
            "jobs_normalized": 0,
            "stable_ids": [],
            "apply_urls": [],
            "success": False,
            "status": _status_for_error(exc),
            "error": str(exc),
        }


def _finalize_result(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    platform, implementation, source = PLATFORM_NOTES[str(entry["name"])]
    result: dict[str, Any] = {
        "company": entry["name"],
        "master_rank": entry["original_workbook_rank"],
        "platform": platform,
        "implementation_path": implementation,
        "source_identity": source,
        "runs": runs,
        "safe_to_activate": False,
        "validated_count": 0,
        "stable_id_result": "not_validated",
        "apply_url_result": "not_validated",
        "identity_result": "not_validated",
    }
    failures = [run for run in runs if not run.get("success")]
    if failures:
        failure = failures[0]
        result.update(
            validation_status=failure.get("status", "failed_technical"),
            exclusion_reason=failure.get("error", "validation failed"),
        )
        return result
    signatures = [list(zip(run["stable_ids"], run["apply_urls"])) for run in runs]
    if len(signatures) != 2 or signatures[0] != signatures[1]:
        result.update(
            validation_status="non_deterministic",
            exclusion_reason="Repeated runs produced different stable IDs or apply URLs",
        )
        return result
    result.update(
        validation_status="validated_with_jobs",
        safe_to_activate=True,
        validated_count=len(signatures[0]),
        stable_id_result="stable_across_two_runs",
        apply_url_result="stable_official_and_job_specific_across_two_runs",
        identity_result="exact_official_identity_across_two_runs",
    )
    return result


def _probe_exclusion(review: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for run_number in (1, 2):
        started = time.monotonic()
        session = requests.Session()
        try:
            response = session.get(
                str(review["listing_url"]),
                headers={"Accept": "text/html", "User-Agent": "job-radar/1.0"},
                timeout=timeout,
            )
            if response.status_code != 200 or not response.content:
                raise AdapterError(f"Official source HTTP {response.status_code}")
            document = html.fromstring(response.content)
            identity = f"{document.xpath('string(//title)')} {document.text_content()}"
            if not re.search(str(review["identity_regex"]), identity, re.IGNORECASE):
                raise AdapterError("Official source identity is ambiguous")
            runs.append(
                {
                    "run": run_number,
                    "runtime_seconds": round(time.monotonic() - started, 3),
                    "jobs_observed": 0,
                    "jobs_normalized": 0,
                    "pages_requested": 1,
                    "http_status": response.status_code,
                    "success": True,
                }
            )
        except (AdapterError, requests.RequestException, TypeError, ValueError) as exc:
            runs.append(
                {
                    "run": run_number,
                    "runtime_seconds": round(time.monotonic() - started, 3),
                    "jobs_observed": 0,
                    "jobs_normalized": 0,
                    "pages_requested": 1,
                    "success": False,
                    "error": str(exc),
                }
            )
        finally:
            session.close()
    platform, implementation, source = PLATFORM_NOTES[str(review["company"])]
    return {
        "company": review["company"],
        "master_rank": review["master_rank"],
        "platform": platform,
        "implementation_path": implementation,
        "source_identity": source,
        "validation_status": review["classification"],
        "safe_to_activate": False,
        "validated_count": 0,
        "stable_id_result": "not_available_without_supported_adapter",
        "apply_url_result": "not_available_without_supported_adapter",
        "identity_result": "exact_official_identity" if all(run["success"] for run in runs) else "probe_failed",
        "exclusion_reason": review["exclusion_reason"],
        "runs": runs,
    }


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    production_before = PRODUCTION.read_bytes()
    mappings_before = PARENT_MAPPINGS.read_bytes()
    baseline = reconcile()
    candidate_bytes = build_candidate_registry()
    results_by_name: dict[str, dict[str, Any]] = {}

    for entry in CANDIDATE_ENTRIES:
        if entry["platform"] == "browser_rendered_html":
            continue
        runs = []
        for run_number in (1, 2):
            run = _run_direct(entry, timeout)
            run["run"] = run_number
            runs.append(run)
        results_by_name[str(entry["name"])] = _finalize_result(entry, runs)

    browser_entries = [entry for entry in CANDIDATE_ENTRIES if entry["platform"] == "browser_rendered_html"]
    if browser_entries:
        with browser_rendered_html.BrowserBatch(timeout=timeout) as batch:
            for entry in browser_entries:
                runs = []
                for run_number in (1, 2):
                    run = _run_browser(entry, batch, timeout)
                    run["run"] = run_number
                    runs.append(run)
                results_by_name[str(entry["name"])] = _finalize_result(entry, runs)

    for review in EXCLUDED_REVIEWS:
        results_by_name[str(review["company"])] = _probe_exclusion(review, timeout)

    results = [results_by_name[name] for name in REVIEWED_COMPANIES]
    validated_names = {result["company"] for result in results if result["safe_to_activate"]}
    validated_additions = [entry for entry in CANDIDATE_ENTRIES if entry["name"] in validated_names]
    validated_bytes = _registry_bytes(PRODUCTION, validated_additions)
    if not validated_bytes.startswith(production_before):
        raise RuntimeError("Validated registry lost the exact production byte prefix")
    _write_bytes(VALIDATED_REGISTRY, validated_bytes)
    _write_inventory(results)

    counts = Counter(str(result["validation_status"]) for result in results)
    payload = {
        "evidence_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "production_registry_sha256": hashlib.sha256(production_before).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mappings_before).hexdigest().upper(),
        "reviewed_companies": REVIEWED_COMPANIES,
        "limits": {
            "browser_processes": 1,
            "fresh_contexts_per_browser_company": 2,
            "maximum_timeout_seconds_per_page": MAX_TIMEOUT_SECONDS,
            "maximum_pages_per_run": MAX_PAGES,
            "maximum_jobs_inspected_per_run": MAX_JOBS,
            "maximum_rendered_html_bytes": MAX_RENDERED_HTML_BYTES,
            "maximum_scroll_steps": browser_rendered_html.MAX_SCROLL_STEPS,
        },
        "candidate_registry_has_exact_148_entry_prefix": candidate_bytes.startswith(production_before),
        "validated_registry_has_exact_148_entry_prefix": validated_bytes.startswith(production_before),
        "totals": {
            "production_sources": len(baseline),
            "parent_mappings": HISTORICAL_PARENT_MAPPINGS,
            "reviewed": len(results),
            "candidate_registry_count": len(baseline) + len(CANDIDATE_ENTRIES),
            "validated": len(validated_additions),
            "excluded": len(results) - len(validated_additions),
            "validated_registry_count": len(baseline) + len(validated_additions),
            "projected_production_sources": len(baseline) + len(validated_additions),
            "projected_original_company_coverage": HISTORICAL_ORIGINAL_COVERAGE + len(validated_additions),
            "statuses": dict(sorted(counts.items())),
        },
        "results": results,
        "publication": {
            "production_activated": False,
            "live_dashboard_scan": False,
            "dashboard_publication": False,
            "supabase": False,
            "google_sheets": False,
            "sqlite": False,
        },
    }
    _write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    if PRODUCTION.read_bytes() != production_before or PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Production registry or parent mappings changed during validation")
    return payload


def _entry_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in CANDIDATE_ENTRIES if entry["name"] == name), None)


def _review_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in EXCLUDED_REVIEWS if entry["company"] == name), None)


def _stable_contract(entry: Optional[Mapping[str, Any]]) -> str:
    if not entry:
        return "unsupported adapter; not promoted"
    if entry.get("slug"):
        return "Greenhouse numeric job id"
    if entry.get("platform") == "workday":
        return "Workday requisition id corroborated by detail path"
    return str(entry.get("external_id_regex") or entry.get("external_id_attribute") or "")


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    by_name = {str(item["company"]): item for item in (results or [])}
    rows: list[dict[str, Any]] = []
    for name in REVIEWED_COMPANIES:
        entry, review = _entry_for(name), _review_for(name)
        result = by_name.get(name, {})
        runs = list(result.get("runs") or [])
        platform, implementation, source = PLATFORM_NOTES[name]
        status = str(result.get("validation_status") or (review or {}).get("classification") or "pending_validation")
        safe = bool(result.get("safe_to_activate", False))
        rows.append(
            {
                "master_rank": (entry or review or {})["original_workbook_rank" if entry else "master_rank"],
                "company": name,
                "official_careers_url": (entry or review or {}).get("careers_url", ""),
                "listing_url": (entry or review or {}).get("listing_url") or (entry or {}).get("search_url", ""),
                "current_platform": source,
                "adapter_platform": platform,
                "implementation_path": implementation,
                "source_identity": source_key(entry) if entry else "smartrecruiters:BoschGroup",
                "stable_id_contract": _stable_contract(entry),
                "allowed_apply_hosts": ";".join((entry or {}).get("allowed_apply_hosts") or []),
                "anonymous_access": "true",
                "validation_status": status,
                "safe_to_activate": str(safe).lower(),
                "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
                "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
                "runtime_seconds_run_1": runs[0].get("runtime_seconds", "") if runs else "",
                "runtime_seconds_run_2": runs[1].get("runtime_seconds", "") if len(runs) > 1 else "",
                "excluded": str(not safe).lower(),
                "exclusion_reason": result.get("exclusion_reason") or (review or {}).get("exclusion_reason", ""),
            }
        )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write_bytes(CANDIDATE_CSV, buffer.getvalue().encode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.build_only:
            candidate = build_candidate_registry()
            validated = _registry_bytes(PRODUCTION, [])
            _write_bytes(VALIDATED_REGISTRY, validated)
            _write_inventory()
            payload = {
                "candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))),
                "validated_registry_count": len(load_target_companies(str(VALIDATED_REGISTRY))),
                "candidate_bytes": len(candidate),
            }
        else:
            payload = validate(args.timeout)
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Browser expansion v1a validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
