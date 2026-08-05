"""Build and anonymously validate Browser Expansion Batch B.

The batch prefers existing direct adapters and reviewed embedded data. Browser
rendering is used only for the MDA Space UKG/UltiPro board. Every successful
source is checked twice with fresh HTTP sessions or browser contexts. Only
milestone artifacts and an ignored JSON report are written.
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
CANDIDATE_CSV = ROOT / "config" / "company_browser_expansion_v1b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.browser_expansion_v1b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.browser_expansion_v1b_validated.yaml"
REPORT = ROOT / "output" / "browser_expansion_v1b_validation.json"

HISTORICAL_PRODUCTION_COUNT = 156
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 151
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000
REVIEWED_COMPANIES = [
    "Tourmaline Oil",
    "Astrobotic",
    "MDA Space",
    "Intuitive Machines",
    "Walmart Global Tech",
    "JD.com",
    "Patreon",
]


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
    detail_link_selector: str,
    allowed_apply_hosts: list[str],
    identity_selector: str,
    identity_regex: str,
    external_id_regex: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "platform": "browser_rendered_html",
        "careers_url": careers_url,
        "listing_url": listing_url,
        "endpoint_variant": "browser_rendered_html_v1",
        "browser_wait_selector": wait_selector,
        "browser_scroll_steps": 0,
        "browser_static_variant": "server_rendered_links",
        "browser_identity_selector": identity_selector,
        "browser_identity_regex": identity_regex,
        "listing_selector": listing_selector,
        "job_card_selector": job_card_selector,
        "title_selector": title_selector,
        "location_selector": None,
        "detail_link_selector": detail_link_selector,
        "external_id_regex": external_id_regex,
        "external_id_attribute": None,
        "department_selector": None,
        "date_selector": None,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": rank,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _direct(
        181,
        "Tourmaline Oil",
        "javascript_embedded_data",
        "https://www.tourmalineoil.com/careers",
        identity_regex=r"Tourmaline Oil",
        allowed_apply_hosts=["tourmaline.cdn.prismic.io"],
        listing_url="https://www.tourmalineoil.com/careers",
        endpoint_variant="next_data",
        embedded_script_selector="script#__NEXT_DATA__",
        embedded_json_path="props.pageProps.pageData.data.body[1].items",
        id_field="link.id",
        title_field="label",
        url_field="link.url",
        max_scripts=1,
        max_script_bytes=1_000_000,
    ),
    _browser(
        192,
        "MDA Space",
        "https://mda.space/careers",
        "https://recruiting.ultipro.ca/MAC5000MCDW/JobBoard/664818ff-3594-4bec-9f30-3394e59e19f3/",
        wait_selector="a[data-automation='job-title']",
        listing_selector="div#Opportunities",
        job_card_selector="a[data-automation=job-title]",
        title_selector=":self",
        detail_link_selector=":self",
        external_id_regex=r"[?&]opportunityId=([0-9a-f-]+)",
        allowed_apply_hosts=["recruiting.ultipro.ca"],
        identity_selector="body",
        identity_regex=r"MDA Space",
    ),
    _direct(
        203,
        "JD.com",
        "workday",
        "https://corporate.jd.com/careers",
        identity_regex=r"JoyGen",
        allowed_apply_hosts=["jd.wd103.myworkdayjobs.com"],
        tenant="jd",
        wd_subdomain="wd103",
        site_id="Campus_Career_Site",
        search_url="https://jd.wd103.myworkdayjobs.com/Campus_Career_Site",
    ),
    _direct(
        243,
        "Patreon",
        "ashby",
        "https://www.patreon.com/careers",
        identity_regex=r"Patreon",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
        slug="patreon",
        search_url="https://jobs.ashbyhq.com/patreon",
    ),
]


EXCLUDED_REVIEWS: list[dict[str, Any]] = [
    {
        "master_rank": 189,
        "company": "Astrobotic",
        "careers_url": "https://www.astrobotic.com/company/careers/",
        "listing_url": "https://workforcenow.cloud.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=dde86c74-1cf6-4830-b396-33b9bd085ff9&ccId=48397579_2&lang=en_US",
        "current_platform": "adp_workforce_now",
        "adapter_platform": "none",
        "classification": "missing_job_specific_apply_urls",
        "identity_regex": r"Astrobotic",
        "exclusion_reason": (
            "The official page identifies an anonymous ADP Workforce Now board, but the rendered "
            "cards expose placeholder # links rather than official job-specific URLs. No existing "
            "declarative adapter can safely normalize the click-only routing."
        ),
    },
    {
        "master_rank": 194,
        "company": "Intuitive Machines",
        "careers_url": "https://www.intuitivemachines.com/careers",
        "listing_url": "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=b0e24f83-6e4d-492d-9d6a-bc0fea197d6a&ccId=19000101_000001&lang=en_US",
        "current_platform": "adp_workforce_now",
        "adapter_platform": "none",
        "classification": "missing_job_specific_apply_urls",
        "identity_regex": r"Intuitive Machines",
        "exclusion_reason": (
            "The official page identifies an anonymous ADP Workforce Now board, but the rendered "
            "cards expose placeholder # links rather than official job-specific URLs. No existing "
            "declarative adapter can safely normalize the click-only routing."
        ),
    },
    {
        "master_rank": 201,
        "company": "Walmart Global Tech",
        "careers_url": "https://tech.walmart.com/content/walmart-global-tech/en_us/careers.html",
        "listing_url": "https://careers.walmart.com/us/en/results?searchQuery=All&careerareas=Technology",
        "current_platform": "shared_walmart_source",
        "adapter_platform": "none",
        "classification": "mixed_or_ambiguous_identity",
        "identity_regex": r"Walmart Global Tech|Walmart",
        "exclusion_reason": (
            "The technology filter returns a shared Walmart parent collection and does not prove "
            "Walmart Global Tech identity on every role. Activating it as a separate source would "
            "risk mixed identity and duplicate parent-source jobs while mappings remain unchanged."
        ),
    },
]


PLATFORM_NOTES = {
    "Tourmaline Oil": ("javascript_embedded_data", "existing_embedded_data_adapter", "Next.js __NEXT_DATA__ Prismic job links"),
    "Astrobotic": ("none", "excluded_adp_click_routing", "ADP Workforce Now rendered cards"),
    "MDA Space": ("browser_rendered_html", "browser_required", "UKG/UltiPro rendered opportunity links"),
    "Intuitive Machines": ("none", "excluded_adp_click_routing", "ADP Workforce Now rendered cards"),
    "Walmart Global Tech": ("none", "excluded_shared_parent_identity", "Walmart Careers technology-filtered parent source"),
    "JD.com": ("workday", "existing_direct_adapter", "jd.wd103.myworkdayjobs.com Campus_Career_Site"),
    "Patreon": ("ashby", "existing_direct_adapter", "api.ashbyhq.com posting-api job-board/patreon"),
}


CSV_FIELDS = [
    "master_rank", "company", "official_careers_url", "listing_url", "current_platform",
    "adapter_platform", "implementation_path", "source_identity", "stable_id_contract",
    "allowed_apply_hosts", "anonymous_access", "validation_status", "safe_to_activate",
    "job_count_run_1", "job_count_run_2", "runtime_seconds_run_1",
    "runtime_seconds_run_2", "excluded", "exclusion_reason",
]


def _name_key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("name") or entry.get("company") or "").casefold().split())


def source_key(entry: Mapping[str, Any]) -> Optional[str]:
    if str(entry.get("platform") or "").casefold() != "browser_rendered_html":
        return registry_validator.source_key(entry)
    host = (urlparse(str(entry.get("listing_url") or "")).hostname or "").casefold()
    path = urlparse(str(entry.get("listing_url") or "")).path.rstrip("/").casefold()
    return f"browser_rendered_html:{host}:{path}" if host and path else None


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
        raise ValueError("Production registry is not the exact 156-entry baseline")
    if len(load_parent_source_mappings(PARENT_MAPPINGS)) != HISTORICAL_PARENT_MAPPINGS:
        raise ValueError("Parent mappings are not the exact four-entry baseline")
    reviewed = sorted(
        [entry["name"] for entry in CANDIDATE_ENTRIES]
        + [entry["company"] for entry in EXCLUDED_REVIEWS],
        key=REVIEWED_COMPANIES.index,
    )
    if reviewed != REVIEWED_COMPANIES:
        raise ValueError("Batch B must review exactly the requested seven companies")
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
        if parsed.scheme != "https" or not _allowed_host((parsed.hostname or "").casefold(), allowed):
            raise AdapterError("Candidate has an unofficial apply URL")
    return list(zip(ids, urls))


_REJECTED_TEXT = (
    "access denied", "request blocked", "verify you are human", "captcha",
    "consent required", "cookie consent required", "session required", "session expired",
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
        jobs = scanner.ADAPTERS[str(entry["platform"])](
            session, CompanyTarget.from_dict(dict(entry)), timeout
        )
        signature = _verify_jobs(jobs, entry)
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": len(jobs), "jobs_normalized": len(signature),
            "stable_ids": [job_id for job_id, _ in signature],
            "apply_urls": [url for _, url in signature],
            "pages_requested": 2, "identity": identity, "success": True,
        }
    except (AdapterError, requests.RequestException, ValueError, TypeError) as exc:
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [], "apply_urls": [],
            "pages_requested": 2, "success": False, "status": _status_for_error(exc),
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
            "jobs_observed": len(jobs), "jobs_normalized": len(signature),
            "stable_ids": [job_id for job_id, _ in signature],
            "apply_urls": [url for _, url in signature],
            "pages_rendered": diagnostics.get("pages_rendered", 0),
            "rendered_bytes": diagnostics.get("rendered_bytes", 0),
            "rendered_urls": diagnostics.get("rendered_urls", []), "success": True,
        }
    except (AdapterError, ValueError, TypeError) as exc:
        return {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "jobs_observed": 0, "jobs_normalized": 0, "stable_ids": [], "apply_urls": [],
            "success": False, "status": _status_for_error(exc), "error": str(exc),
        }


def _finalize_result(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    platform, implementation, source = PLATFORM_NOTES[str(entry["name"])]
    result: dict[str, Any] = {
        "company": entry["name"], "master_rank": entry["original_workbook_rank"],
        "platform": platform, "implementation_path": implementation,
        "source_identity": source, "runs": runs, "safe_to_activate": False,
        "validated_count": 0, "stable_id_result": "not_validated",
        "apply_url_result": "not_validated", "identity_result": "not_validated",
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
        validation_status="validated_with_jobs", safe_to_activate=True,
        validated_count=len(signatures[0]), stable_id_result="stable_across_two_runs",
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
                str(review["careers_url"]),
                headers={"Accept": "text/html", "User-Agent": "job-radar/1.0"},
                timeout=timeout,
            )
            if response.status_code != 200 or not response.content:
                raise AdapterError(f"Official source HTTP {response.status_code}")
            document = html.fromstring(response.content)
            identity = f"{document.xpath('string(//title)')} {document.text_content()}"
            if not re.search(str(review["identity_regex"]), identity, re.IGNORECASE):
                raise AdapterError("Official source identity is ambiguous")
            runs.append({
                "run": run_number, "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": 0, "jobs_normalized": 0, "pages_requested": 1,
                "http_status": response.status_code, "success": True,
            })
        except (AdapterError, requests.RequestException, TypeError, ValueError) as exc:
            runs.append({
                "run": run_number, "runtime_seconds": round(time.monotonic() - started, 3),
                "jobs_observed": 0, "jobs_normalized": 0, "pages_requested": 1,
                "success": False, "error": str(exc),
            })
        finally:
            session.close()
    platform, implementation, source = PLATFORM_NOTES[str(review["company"])]
    return {
        "company": review["company"], "master_rank": review["master_rank"],
        "platform": platform, "implementation_path": implementation,
        "source_identity": source, "validation_status": review["classification"],
        "safe_to_activate": False, "validated_count": 0,
        "stable_id_result": "not_available_without_supported_adapter",
        "apply_url_result": "not_available_without_supported_adapter",
        "identity_result": "exact_official_identity" if all(run["success"] for run in runs) else "probe_failed",
        "exclusion_reason": review["exclusion_reason"], "runs": runs,
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

    browser_entries = [
        entry for entry in CANDIDATE_ENTRIES if entry["platform"] == "browser_rendered_html"
    ]
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
    validated_additions = [
        entry for entry in CANDIDATE_ENTRIES if entry["name"] in validated_names
    ]
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
            "browser_processes": 1, "fresh_contexts_per_browser_company": 2,
            "fresh_http_sessions_per_direct_company": 2,
            "maximum_timeout_seconds_per_page": MAX_TIMEOUT_SECONDS,
            "maximum_pages_per_run": MAX_PAGES,
            "maximum_jobs_inspected_per_run": MAX_JOBS,
            "maximum_rendered_html_bytes": MAX_RENDERED_HTML_BYTES,
            "maximum_scroll_steps": browser_rendered_html.MAX_SCROLL_STEPS,
        },
        "candidate_registry_has_exact_156_entry_prefix": candidate_bytes.startswith(production_before),
        "validated_registry_has_exact_156_entry_prefix": validated_bytes.startswith(production_before),
        "totals": {
            "production_sources": len(baseline), "parent_mappings": HISTORICAL_PARENT_MAPPINGS,
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
            "production_activated": False, "live_dashboard_scan": False,
            "dashboard_publication": False, "supabase": False,
            "google_sheets": False, "sqlite": False,
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
    if entry.get("platform") == "javascript_embedded_data":
        return str(entry.get("id_field") or entry.get("external_id_regex") or "")
    if entry.get("platform") == "workday":
        return "Workday requisition id corroborated by detail path"
    if entry.get("platform") == "ashby":
        return "Ashby UUID job id"
    return str(entry.get("external_id_regex") or entry.get("external_id_attribute") or "")


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    by_name = {str(item["company"]): item for item in (results or [])}
    rows: list[dict[str, Any]] = []
    for name in REVIEWED_COMPANIES:
        entry, review = _entry_for(name), _review_for(name)
        result = by_name.get(name, {})
        runs = list(result.get("runs") or [])
        platform, implementation, source = PLATFORM_NOTES[name]
        status = str(
            result.get("validation_status")
            or (review or {}).get("classification")
            or "pending_validation"
        )
        safe = bool(result.get("safe_to_activate", False))
        rows.append({
            "master_rank": (entry or review or {})[
                "original_workbook_rank" if entry else "master_rank"
            ],
            "company": name,
            "official_careers_url": (entry or review or {}).get("careers_url", ""),
            "listing_url": (entry or review or {}).get("listing_url")
            or (entry or {}).get("search_url", ""),
            "current_platform": source, "adapter_platform": platform,
            "implementation_path": implementation,
            "source_identity": source_key(entry) if entry else str((review or {}).get("current_platform", "")),
            "stable_id_contract": _stable_contract(entry),
            "allowed_apply_hosts": ";".join((entry or {}).get("allowed_apply_hosts") or []),
            "anonymous_access": "true", "validation_status": status,
            "safe_to_activate": str(safe).lower(),
            "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
            "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
            "runtime_seconds_run_1": runs[0].get("runtime_seconds", "") if runs else "",
            "runtime_seconds_run_2": runs[1].get("runtime_seconds", "") if len(runs) > 1 else "",
            "excluded": str(not safe).lower(),
            "exclusion_reason": result.get("exclusion_reason")
            or (review or {}).get("exclusion_reason", ""),
        })
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
        print(f"Browser expansion v1b validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
