"""Build and run the isolated browser-rendered HTML collector pilot.

The validator launches one headless Chromium process, uses two fresh anonymous
contexts per reviewed source, writes only pilot registries and an ignored JSON
report, and never initializes dashboard or persistence integrations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from src.scraper.company_careers.adapters import browser_rendered_html
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
V4_INVENTORY = ROOT / "config" / "company_uncovered_inventory_v4.csv"
CANDIDATE_CSV = ROOT / "config" / "company_browser_pilot_v1_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.browser_pilot_v1_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.browser_pilot_v1_validated.yaml"
REPORT = ROOT / "output" / "browser_pilot_v1_validation.json"

HISTORICAL_PRODUCTION_COUNT = 145
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 140
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000
REVIEWED_COMPANIES = ["Apple", "Figure AI", "LINE"]
SAFE_STATUSES = {"validated_with_jobs", "validated_zero_jobs"}


def _entry(
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
    external_id_regex: str,
    allowed_apply_hosts: list[str],
    identity_regex: str,
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
        "browser_identity_selector": "title",
        "browser_identity_regex": identity_regex,
        "listing_selector": listing_selector,
        "job_card_selector": job_card_selector,
        "title_selector": title_selector,
        "location_selector": location_selector,
        "detail_link_selector": detail_link_selector,
        "external_id_regex": external_id_regex,
        "department_selector": department_selector,
        "date_selector": date_selector,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": rank,
        "max_pages": 1,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _entry(
        3,
        "Apple",
        "https://www.apple.com/careers/us/",
        "https://jobs.apple.com/en-us/search?location=united-states-USA",
        wait_selector="h3 a[href*='/details/']",
        listing_selector="h3 a[href*=/details/]",
        job_card_selector="h3 a[href*=/details/]",
        title_selector=":self",
        location_selector=None,
        detail_link_selector=":self",
        external_id_regex=r"/details/([^/?#]+)",
        allowed_apply_hosts=["jobs.apple.com"],
        identity_regex=r"Careers at Apple|Jobs.*Apple",
        scroll_steps=1,
    ),
    _entry(
        154,
        "Figure AI",
        "https://www.figure.ai/careers",
        "https://www.figure.ai/careers",
        wait_selector="a.careers-listing-module-job",
        listing_selector="a.careers-listing-module-job",
        job_card_selector="a.careers-listing-module-job",
        title_selector="h1.careers-listing-module-job__heading",
        location_selector="address.careers-listing-module-job__location",
        detail_link_selector=":self",
        external_id_regex=r"/jobs/([0-9]+)(?:[/?#]|$)",
        allowed_apply_hosts=["job-boards.greenhouse.io"],
        identity_regex=r"Figure",
        scroll_steps=2,
    ),
    _entry(
        237,
        "LINE",
        "https://careers.linecorp.com/",
        "https://careers.linecorp.com/jobs/?ca=All",
        wait_selector="a[href^='/jobs/'][href$='/']",
        listing_selector="a[href^=/jobs/][href$=/]",
        job_card_selector="a[href^=/jobs/][href$=/]",
        title_selector="h3.title",
        location_selector="div.text_filter span:first-child",
        detail_link_selector=":self",
        external_id_regex=r"/jobs/([0-9]+)/?",
        allowed_apply_hosts=["careers.linecorp.com"],
        identity_regex=r"LINE CAREERS",
        department_selector="div.text_filter span:nth-child(3)",
        date_selector="span.date",
        scroll_steps=1,
    ),
]


SELECTION_RATIONALE = {
    "Apple": "Medium-confidence v4 hydration evidence plus a large anonymous rendered result list, same-host role-number URLs, and reusable rich-card DOM coverage.",
    "Figure AI": "Medium-confidence v4 Next.js evidence plus anonymous rendered role links with stable Greenhouse numeric IDs; exercises an official cross-host apply allowlist.",
    "LINE": "Mandatory pilot source with high-confidence prior Gatsby-shell evidence; current anonymous DOM exposes numeric first-party job-detail links.",
}


CSV_FIELDS = [
    "master_rank",
    "company",
    "selected",
    "selection_rationale",
    "prior_classification",
    "listing_url",
    "wait_selector",
    "job_card_selector",
    "stable_id_contract",
    "allowed_apply_hosts",
    "anonymous_access",
    "validation_status",
    "validated_count",
    "runtime_seconds_run_1",
    "runtime_seconds_run_2",
    "excluded",
    "exclusion_reason",
]


def _key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("name") or "").casefold().split())


def source_key(entry: Mapping[str, Any]) -> Optional[str]:
    if str(entry.get("platform") or "").casefold() != "browser_rendered_html":
        return registry_validator.source_key(entry)
    host = (urlparse(str(entry.get("listing_url") or "")).hostname or "").casefold()
    return f"browser_rendered_html:{host}" if host else None


def _duplicates(baseline: Iterable[Mapping[str, Any]], additions: Iterable[Mapping[str, Any]]) -> tuple[dict[int, str], dict[int, str]]:
    name_owner = {_key(entry): str(entry.get("name")) for entry in baseline}
    source_owner = {source_key(entry): str(entry.get("name")) for entry in baseline if source_key(entry)}
    duplicate_names: dict[int, str] = {}
    duplicate_sources: dict[int, str] = {}
    for index, entry in enumerate(additions):
        name, source = _key(entry), source_key(entry)
        if name in name_owner:
            duplicate_names[index] = name_owner[name]
        else:
            name_owner[name] = str(entry["name"])
        if source and source in source_owner:
            duplicate_sources[index] = source_owner[source]
        elif source:
            source_owner[source] = str(entry["name"])
    return duplicate_names, duplicate_sources


def _registry_bytes(production: Path, additions: Iterable[Mapping[str, Any]]) -> bytes:
    return registry_validator.build_candidate_registry_bytes(production, list(additions))


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    content = (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
    _write_bytes(path, content)


def reconcile() -> list[dict[str, Any]]:
    _, production = registry_validator.load_registry(PRODUCTION)
    if len(production) != HISTORICAL_PRODUCTION_COUNT:
        raise ValueError("Production registry is not the exact 145-entry baseline")
    if len(load_parent_source_mappings(PARENT_MAPPINGS)) != HISTORICAL_PARENT_MAPPINGS:
        raise ValueError("Parent mappings are not the exact four-entry baseline")
    if [entry["name"] for entry in CANDIDATE_ENTRIES] != REVIEWED_COMPANIES:
        raise ValueError("Pilot must review exactly Apple, Figure AI, and LINE in workbook order")
    inventory = {row["company"]: row for row in csv.DictReader(V4_INVENTORY.open(newline="", encoding="utf-8"))}
    if any(name not in inventory and name != "LINE" for name in REVIEWED_COMPANIES):
        raise ValueError("Pilot selection is not present in the v4 inventory")
    duplicate_names, duplicate_sources = _duplicates(production, CANDIDATE_ENTRIES)
    if duplicate_names:
        raise ValueError(f"duplicate_company: {duplicate_names}")
    if duplicate_sources:
        raise ValueError(f"duplicate_source: {duplicate_sources}")
    for entry in CANDIDATE_ENTRIES:
        browser_rendered_html.validate_target(CompanyTarget.from_dict(entry))
    return production


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY) -> bytes:
    if production.resolve() == PRODUCTION.resolve():
        reconcile()
    _, baseline = registry_validator.load_registry(production)
    duplicate_names, duplicate_sources = _duplicates(baseline, CANDIDATE_ENTRIES)
    if duplicate_names:
        raise ValueError(f"duplicate_company: {duplicate_names}")
    if duplicate_sources:
        raise ValueError(f"duplicate_source: {duplicate_sources}")
    content = _registry_bytes(production, CANDIDATE_ENTRIES)
    _write_bytes(destination, content)
    if not content.startswith(production.read_bytes()):
        raise ValueError("Candidate registry lost the exact production byte prefix")
    return content


def _status_for_error(exc: BaseException) -> str:
    message = str(exc).casefold()
    if "duplicate" in message and "id" in message:
        return "unstable_ids"
    if "apply" in message or "allowed-host" in message:
        return "unofficial_apply_url"
    if "identity" in message:
        return "ambiguous_company_identity"
    if "login" in message or "authentication" in message:
        return "authentication_required"
    if "session" in message:
        return "session_required"
    if "captcha" in message or "blocked" in message or "consent" in message:
        return "blocked"
    if "timeout" in message or "wait selector" in message:
        return "timeout_or_wait_selector_missing"
    if "playwright" in message or "chromium" in message or "browser batch" in message:
        return "browser_runtime_missing"
    if "malformed" in message or "byte" in message or "limit" in message:
        return "malformed_or_limit_failure"
    return "failed_technical"


def _job_signature(jobs: Iterable[Any]) -> list[tuple[str, str]]:
    return [(str(job.job_id), str(job.url)) for job in jobs]


def _official_jobs(jobs: list[Any], entry: Mapping[str, Any]) -> None:
    if len(jobs) > MAX_JOBS:
        raise AdapterError("Browser pilot normalized job limit exceeded")
    ids = [job.job_id for job in jobs]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise AdapterError("Browser pilot has missing or duplicate stable IDs")
    allowed = [str(value).casefold() for value in entry["allowed_apply_hosts"]]
    for job in jobs:
        parsed = urlparse(job.url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not any(host == value or host.endswith("." + value) for value in allowed):
            raise AdapterError("Browser pilot job has an unofficial apply URL")


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    production_before = PRODUCTION.read_bytes()
    mappings_before = PARENT_MAPPINGS.read_bytes()
    baseline = reconcile()
    candidate_bytes = build_candidate_registry()
    results: list[dict[str, Any]] = []
    with browser_rendered_html.BrowserBatch(timeout=timeout) as batch:
        for entry in CANDIDATE_ENTRIES:
            result: dict[str, Any] = {
                "company": entry["name"],
                "master_rank": entry["original_workbook_rank"],
                "validation_status": "pending",
                "safe_to_activate": False,
                "runs": [],
                "selection_rationale": SELECTION_RATIONALE[entry["name"]],
            }
            try:
                signatures: list[list[tuple[str, str]]] = []
                for run_number in (1, 2):
                    started = time.monotonic()
                    jobs = batch.fetch_jobs(CompanyTarget.from_dict(entry), timeout)
                    runtime = round(time.monotonic() - started, 3)
                    _official_jobs(jobs, entry)
                    signature = _job_signature(jobs)
                    signatures.append(signature)
                    diagnostics = dict(batch.last_run)
                    valid_empty = not jobs and bool(
                        diagnostics.get("static_parser", {}).get("zero_job_contract")
                    )
                    result["runs"].append(
                        {
                            "run": run_number,
                            "runtime_seconds": runtime,
                            "jobs_normalized": len(jobs),
                            "stable_ids": [job_id for job_id, _url in signature],
                            "apply_urls": [url for _job_id, url in signature],
                            "pages_rendered": diagnostics.get("pages_rendered", 0),
                            "rendered_bytes": diagnostics.get("rendered_bytes", 0),
                            "valid_empty": valid_empty,
                        }
                    )
                if signatures[0] != signatures[1]:
                    raise AdapterError("Browser pilot repeated runs produced unstable IDs or apply URLs")
                valid_empty = not signatures[0] and all(
                    run.get("jobs_normalized") == 0 and run.get("valid_empty") is True
                    for run in result["runs"]
                )
                if not signatures[0] and not valid_empty:
                    raise AdapterError("Browser pilot did not conclusively validate zero jobs")
                result.update(
                    validation_status="validated_with_jobs" if signatures[0] else "validated_zero_jobs",
                    safe_to_activate=True,
                    validated_count=len(signatures[0]),
                    stable_id_result="stable_across_two_runs" if signatures[0] else "not_applicable_zero_jobs",
                    apply_url_result="stable_and_official_across_two_runs" if signatures[0] else "not_applicable_zero_jobs",
                )
            except (AdapterError, ValueError, TypeError) as exc:
                result.update(
                    validation_status=_status_for_error(exc),
                    exclusion_reason=str(exc),
                    validated_count=0,
                )
            results.append(result)

    validated_names = {result["company"] for result in results if result["validation_status"] in SAFE_STATUSES}
    validated_additions = [entry for entry in CANDIDATE_ENTRIES if entry["name"] in validated_names]
    validated_bytes = _registry_bytes(PRODUCTION, validated_additions)
    _write_bytes(VALIDATED_REGISTRY, validated_bytes)
    _write_inventory(results)
    counts = Counter(result["validation_status"] for result in results)
    payload = {
        "evidence_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "production_registry_sha256": hashlib.sha256(production_before).hexdigest().upper(),
        "parent_mappings_sha256": hashlib.sha256(mappings_before).hexdigest().upper(),
        "reviewed_companies": REVIEWED_COMPANIES,
        "limits": {
            "browser_processes": 1,
            "fresh_contexts_per_company": 2,
            "maximum_timeout_seconds_per_page": MAX_TIMEOUT_SECONDS,
            "maximum_pages_per_run": MAX_PAGES,
            "maximum_jobs_per_run": MAX_JOBS,
            "maximum_rendered_html_bytes": MAX_RENDERED_HTML_BYTES,
            "maximum_scroll_steps": browser_rendered_html.MAX_SCROLL_STEPS,
        },
        "candidate_registry_has_exact_145_entry_prefix": candidate_bytes.startswith(production_before),
        "validated_registry_has_exact_145_entry_prefix": validated_bytes.startswith(production_before),
        "totals": {
            "production_sources": len(baseline),
            "parent_mappings": HISTORICAL_PARENT_MAPPINGS,
            "reviewed": len(results),
            "validated": len(validated_additions),
            "excluded": len(results) - len(validated_additions),
            "validated_registry_count": len(baseline) + len(validated_additions),
            "projected_original_coverage": HISTORICAL_ORIGINAL_COVERAGE + len(validated_additions),
            "statuses": dict(sorted(counts.items())),
        },
        "results": results,
        "publication": {
            "production_activated": False,
            "dashboard_scan": False,
            "supabase": False,
            "google_sheets": False,
            "sqlite": False,
        },
    }
    _write_json(REPORT, payload)
    if PRODUCTION.read_bytes() != production_before or PARENT_MAPPINGS.read_bytes() != mappings_before:
        raise RuntimeError("Production registry or parent mappings changed during browser validation")
    return payload


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    by_name = {str(item["company"]): item for item in (results or [])}
    rows = []
    for entry in CANDIDATE_ENTRIES:
        result = by_name.get(str(entry["name"]), {})
        runs = list(result.get("runs") or [])
        status = str(result.get("validation_status") or "pending_validation")
        rows.append(
            {
                "master_rank": entry["original_workbook_rank"],
                "company": entry["name"],
                "selected": "true",
                "selection_rationale": SELECTION_RATIONALE[entry["name"]],
                "prior_classification": "browser_required",
                "listing_url": entry["listing_url"],
                "wait_selector": entry["browser_wait_selector"],
                "job_card_selector": entry["job_card_selector"],
                "stable_id_contract": entry["external_id_regex"],
                "allowed_apply_hosts": ";".join(entry["allowed_apply_hosts"]),
                "anonymous_access": "true",
                "validation_status": status,
                "validated_count": result.get("validated_count", ""),
                "runtime_seconds_run_1": runs[0].get("runtime_seconds", "") if runs else "",
                "runtime_seconds_run_2": runs[1].get("runtime_seconds", "") if len(runs) > 1 else "",
                "excluded": str(status not in SAFE_STATUSES).lower(),
                "exclusion_reason": result.get("exclusion_reason", ""),
            }
        )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write_bytes(CANDIDATE_CSV, buffer.getvalue().encode("utf-8"))


def inspect_dom(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Bounded selector research for the exact three reviewed anonymous pages."""
    sync_playwright = browser_rendered_html._load_sync_playwright()
    manager = sync_playwright().start()
    browser = manager.chromium.launch(headless=True)
    findings: list[dict[str, Any]] = []
    try:
        for entry in CANDIDATE_ENTRIES:
            context = browser.new_context()
            page = context.new_page()
            context.route("**/*", browser_rendered_html._route_resources)
            try:
                page.goto(entry["listing_url"], wait_until="domcontentloaded", timeout=min(timeout, 30) * 1000)
                page.wait_for_timeout(5000)
                content = page.content().encode("utf-8")
                if len(content) > MAX_RENDERED_HTML_BYTES:
                    raise AdapterError("Inspection HTML exceeded 3 MB")
                from lxml import html as lxml_html
                document = lxml_html.fromstring(content)
                anchors = []
                for node in document.xpath("//a[@href]"):
                    href = str(node.get("href") or "")
                    text = " ".join(node.text_content().split())
                    if any(token in href.casefold() for token in ("/jobs/", "/details/", "ashbyhq.com")):
                        parent = node.getparent()
                        anchors.append(
                            {
                                "href": href,
                                "text": text[:180],
                                "class": node.get("class", ""),
                                "parent_tag": getattr(parent, "tag", ""),
                                "parent_class": parent.get("class", "") if parent is not None else "",
                                "children": [
                                    {
                                        "tag": child.tag,
                                        "class": child.get("class", ""),
                                        "text": " ".join(child.text_content().split())[:120],
                                    }
                                    for child in node.xpath(".//*")[:12]
                                ],
                            }
                        )
                body_text = " ".join(document.text_content().split())
                findings.append({"company": entry["name"], "url": page.url, "title": page.title(), "bytes": len(content), "body_text": body_text[:1000], "anchors": anchors[:30]})
            finally:
                page.close()
                context.close()
    finally:
        browser.close()
        manager.stop()
    return {"reviewed": REVIEWED_COMPANIES, "findings": findings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--inspect-dom", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.inspect_dom:
            payload = inspect_dom(args.timeout)
        elif args.build_only:
            content = build_candidate_registry()
            _write_inventory()
            payload = {"candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))), "bytes": len(content)}
        else:
            payload = validate(args.timeout)
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Browser pilot validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
