"""Build and anonymously validate Source Expansion v2 Batch A.

The batch is selected from the current uncovered master-company set and favors
existing direct adapters, then server-rendered HTML, with bounded browser
rendering only for sources whose public job data is client-rendered. Production,
parent mappings, persistence layers, and dashboard code are never modified.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.refingerprint_uncovered_companies import (
    load_master_workbook,
    reconcile_master,
)
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


_ENGINE_SPEC = importlib.util.spec_from_file_location(
    "scripts._source_expansion_v2_batch_a_engine",
    ROOT / "scripts" / "validate_browser_expansion_v1b.py",
)
if _ENGINE_SPEC is None or _ENGINE_SPEC.loader is None:
    raise ImportError("Unable to load the isolated batch-validation engine")
engine = importlib.util.module_from_spec(_ENGINE_SPEC)
_ENGINE_SPEC.loader.exec_module(engine)


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_source_expansion_v2_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.source_expansion_v2_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.source_expansion_v2_batch_a_validated.yaml"
REPORT = ROOT / "output" / "source_expansion_v2_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 160
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 155
HISTORICAL_UNCOVERED_COUNT = 91
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000


INDUSTRIES = {
    "Infineon Technologies": "Semiconductors",
    "MediaTek": "Semiconductors",
    "GlobalFoundries": "Semiconductors",
    "Beckhoff Automation": "Industrial automation",
    "Lucid Motors": "Automotive/mobility",
    "Varjo": "AR/VR/XR",
    "Sanctuary AI": "Robotics",
    "Clearpath Robotics": "Robotics",
    "OTTO Motors": "Robotics",
    "Instacart": "E-commerce/logistics",
    "Faire": "E-commerce/logistics",
    "Deliveroo": "E-commerce/logistics",
    "ByteDance": "Social media/creator platforms",
    "Vimeo": "Social media/creator platforms",
    "Substack": "Social media/creator platforms",
}

REVIEWED_COMPANIES = [
    "Infineon Technologies",
    "MediaTek",
    "GlobalFoundries",
    "Beckhoff Automation",
    "Lucid Motors",
    "Varjo",
    "Sanctuary AI",
    "Clearpath Robotics",
    "OTTO Motors",
    "Instacart",
    "Faire",
    "Deliveroo",
    "ByteDance",
    "Vimeo",
    "Substack",
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
    return engine._direct(
        rank,
        name,
        platform,
        careers_url,
        identity_regex=identity_regex,
        allowed_apply_hosts=allowed_apply_hosts,
        **identifiers,
    )


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
    external_id_regex: str,
    allowed_apply_hosts: list[str],
    identity_selector: str,
    identity_regex: str,
) -> dict[str, Any]:
    return engine._browser(
        rank,
        name,
        careers_url,
        listing_url,
        wait_selector=wait_selector,
        listing_selector=listing_selector,
        job_card_selector=job_card_selector,
        title_selector=title_selector,
        detail_link_selector=detail_link_selector,
        external_id_regex=external_id_regex,
        allowed_apply_hosts=allowed_apply_hosts,
        identity_selector=identity_selector,
        identity_regex=identity_regex,
    )


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _direct(
        33,
        "Infineon Technologies",
        "eightfold",
        "https://jobs.infineon.com/careers",
        identity_regex=r"Infineon",
        allowed_apply_hosts=["jobs.infineon.com"],
        endpoint_url="https://jobs.infineon.com/api/pcsx/search",
        endpoint_variant="pcsx",
        organization_id="infineon.com",
        identity_url="https://www.infineon.com/careers",
        max_pages=2,
    ),
    _browser(
        36,
        "MediaTek",
        "https://careers.mediatek.com/en/jobs",
        "https://careers.mediatek.com/en/jobs?query=&locations=",
        wait_selector="a[href*='/en/jobs/']",
        listing_selector="a[href*=/en/jobs/]",
        job_card_selector="a[href*=/en/jobs/]",
        title_selector="h1.h3",
        detail_link_selector=":self",
        external_id_regex=r"/jobs/(MTK[0-9A-Za-z]+)",
        allowed_apply_hosts=["careers.mediatek.com"],
        identity_selector="head title",
        identity_regex=r"MediaTek",
    ),
    _direct(
        37,
        "GlobalFoundries",
        "eightfold",
        "https://careers.gf.com/careers",
        identity_regex=r"GlobalFoundries",
        allowed_apply_hosts=["careers.gf.com"],
        endpoint_url="https://careers.gf.com/api/pcsx/search",
        endpoint_variant="pcsx",
        organization_id="globalfoundries.com",
        identity_url="https://gf.com/careers/",
        max_pages=2,
    ),
    _browser(
        102,
        "Beckhoff Automation",
        "https://www.beckhoff.com/en-us/company/job-opportunities/",
        "https://www.paycomonline.net/v4/ats/web.php/portal/D42904D53A8DFA8C80CE390CB7CAFCEF/career-page",
        wait_selector="a[href*='/jobs/']",
        listing_selector="a[href*=/jobs/]",
        job_card_selector="a[href*=/jobs/]",
        title_selector="h2[data-testid=typography]",
        detail_link_selector=":self",
        external_id_regex=r"/jobs/([0-9]+)",
        allowed_apply_hosts=["www.paycomonline.net"],
        identity_selector="body",
        identity_regex=r"Beckhoff Automation",
    ),
    _direct(
        122,
        "Lucid Motors",
        "greenhouse",
        "https://lucidmotors.com/careers",
        identity_regex=r"Lucid",
        allowed_apply_hosts=["job-boards.greenhouse.io"],
        slug="lucidmotors",
        search_url="https://job-boards.greenhouse.io/lucidmotors",
    ),
    _direct(
        155,
        "Sanctuary AI",
        "static_html",
        "https://sanctuary.ai/careers/",
        identity_regex=r"Sanctuary AI",
        allowed_apply_hosts=["careers.kula.ai"],
        listing_url="https://careers.kula.ai/sanctuary-ai",
        endpoint_variant="server_rendered_cards",
        listing_selector="div[role=region]",
        job_card_selector="div[role=region]",
        title_selector="p:first-child",
        detail_link_selector="a[href^=/sanctuary-ai/]",
        external_id_regex=r"/sanctuary-ai/([0-9]+)/?",
    ),
    _direct(
        206,
        "Instacart",
        "greenhouse",
        "https://www.instacart.careers/en-ca/current-openings",
        identity_regex=r"Instacart",
        allowed_apply_hosts=["instacart.careers"],
        slug="instacart",
        search_url="https://job-boards.greenhouse.io/instacart",
    ),
    _direct(
        217,
        "Faire",
        "greenhouse",
        "https://www.faire.com/careers",
        identity_regex=r"Faire",
        allowed_apply_hosts=["boards.greenhouse.io"],
        slug="faire",
        search_url="https://boards.greenhouse.io/faire",
    ),
    _browser(
        220,
        "Deliveroo",
        "https://careers.deliveroo.co.uk/",
        "https://careers.deliveroo.co.uk/join-the-team/",
        wait_selector="a[href*='/role/']",
        listing_selector="a[href*=/role/]",
        job_card_selector="a[href*=/role/]",
        title_selector="h4",
        detail_link_selector=":self",
        external_id_regex=r"/role/[^/?#]+-([0-9a-f]{12})/?",
        allowed_apply_hosts=["careers.deliveroo.co.uk"],
        identity_selector="head title",
        identity_regex=r"Deliveroo",
    ),
    _browser(
        222,
        "ByteDance",
        "https://jobs.bytedance.com/en/",
        "https://joinbytedance.com/search",
        wait_selector="a[href*='/search/']",
        listing_selector="a[href*=/search/]",
        job_card_selector="a[href*=/search/]",
        title_selector="span.bd-title",
        detail_link_selector=":self",
        external_id_regex=r"/search/([0-9]+)",
        allowed_apply_hosts=["joinbytedance.com"],
        identity_selector="body",
        identity_regex=r"ByteDance",
    ),
    _direct(
        245,
        "Substack",
        "ashby",
        "https://substack.com/jobs",
        identity_regex=r"Substack",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
        slug="substack",
        search_url="https://jobs.ashbyhq.com/substack",
    ),
]


EXCLUDED_REVIEWS: list[dict[str, Any]] = [
    {
        "master_rank": 147,
        "company": "Varjo",
        "careers_url": "https://varjo.com/jobs",
        "listing_url": "https://varjo.com/jobs",
        "current_platform": "hubspot_marketing_page",
        "adapter_platform": "none",
        "classification": "no_current_job_collection",
        "identity_regex": r"Varjo|VR Jobs",
        "exclusion_reason": (
            "The current official Varjo jobs page contains employee-story links but no current "
            "job-specific apply URLs or stable vacancy IDs, so no non-empty source contract can be proven."
        ),
    },
    {
        "master_rank": 166,
        "company": "Clearpath Robotics",
        "careers_url": "https://clearpathrobotics.com/robotics-careers/",
        "listing_url": "https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation",
        "current_platform": "shared_rockwell_workday",
        "adapter_platform": "none",
        "classification": "insufficient_structured_parent_attribution",
        "identity_regex": r"Clearpath",
        "exclusion_reason": (
            "The official page links to Rockwell Automation's shared Workday source. A current "
            "anonymous search returns no Clearpath jobs, and Workday records expose no exact structured "
            "Clearpath brand field; title or description inference is not a safe parent mapping."
        ),
    },
    {
        "master_rank": 167,
        "company": "OTTO Motors",
        "careers_url": "https://ottomotors.com/careers/",
        "listing_url": "https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation",
        "current_platform": "shared_rockwell_workday",
        "adapter_platform": "none",
        "classification": "insufficient_structured_parent_attribution",
        "identity_regex": r"OTTO|Rockwell Automation",
        "exclusion_reason": (
            "The official page links to Rockwell Automation's shared Workday source. Search matches "
            "contain only generic Workday fields (title, path, location, posted date, requisition ID) "
            "and no exact structured OTTO brand field, so a parent mapping would be speculative."
        ),
    },
    {
        "master_rank": 241,
        "company": "Vimeo",
        "careers_url": "https://vimeo.com/careers",
        "listing_url": "https://vimeo.com/careers",
        "current_platform": "builder_nextjs_without_live_jobs",
        "adapter_platform": "none",
        "classification": "no_current_job_collection",
        "identity_regex": r"Vimeo",
        "exclusion_reason": (
            "The current official Vimeo careers page exposes no current job-specific apply URLs. "
            "The previously referenced Greenhouse proxy routes return HTTP 404, so no stable non-empty "
            "official source can be validated without inventing a speculative contract."
        ),
    },
]


PLATFORM_NOTES = {
    "Infineon Technologies": ("eightfold", "existing_direct_adapter", "Eightfold PCSx public search"),
    "MediaTek": ("browser_rendered_html", "browser_required", "MediaTek Next.js rendered job cards"),
    "GlobalFoundries": ("eightfold", "existing_direct_adapter", "Eightfold PCSx public search"),
    "Beckhoff Automation": ("browser_rendered_html", "browser_required", "Paycom rendered job cards"),
    "Lucid Motors": ("greenhouse", "existing_direct_adapter", "Greenhouse boards API: lucidmotors"),
    "Varjo": ("none", "excluded_no_live_collection", "HubSpot careers content without live vacancies"),
    "Sanctuary AI": ("static_html", "existing_static_adapter", "Kula server-rendered job cards"),
    "Clearpath Robotics": ("none", "excluded_shared_parent_identity", "Rockwell Automation shared Workday"),
    "OTTO Motors": ("none", "excluded_shared_parent_identity", "Rockwell Automation shared Workday"),
    "Instacart": ("greenhouse", "existing_direct_adapter", "Greenhouse boards API: instacart"),
    "Faire": ("greenhouse", "existing_direct_adapter", "Greenhouse boards API: faire"),
    "Deliveroo": ("browser_rendered_html", "browser_required", "WordPress REST-backed rendered role cards"),
    "ByteDance": ("browser_rendered_html", "browser_required", "ByteDance public supplier API rendered cards"),
    "Vimeo": ("none", "excluded_no_live_collection", "Builder/Next.js careers page without live vacancies"),
    "Substack": ("ashby", "existing_direct_adapter", "Ashby posting API: substack"),
}


CSV_FIELDS = [
    "master_rank",
    "company",
    "industry",
    "official_careers_url",
    "listing_url",
    "detected_platform",
    "result",
    "adapter_used",
    "jobs_normalized",
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


_engine_reconcile = engine.reconcile
_engine_finalize_result = engine._finalize_result
_engine_probe_exclusion = engine._probe_exclusion
_engine_build_candidate_registry = engine.build_candidate_registry
_engine_identity_check = engine._identity_check


def _configure_engine() -> None:
    engine.PRODUCTION = PRODUCTION
    engine.PARENT_MAPPINGS = PARENT_MAPPINGS
    engine.CANDIDATE_CSV = CANDIDATE_CSV
    engine.CANDIDATE_REGISTRY = CANDIDATE_REGISTRY
    engine.VALIDATED_REGISTRY = VALIDATED_REGISTRY
    engine.REPORT = REPORT
    engine.HISTORICAL_PRODUCTION_COUNT = HISTORICAL_PRODUCTION_COUNT
    engine.HISTORICAL_PARENT_MAPPINGS = HISTORICAL_PARENT_MAPPINGS
    engine.HISTORICAL_ORIGINAL_COVERAGE = HISTORICAL_ORIGINAL_COVERAGE
    engine.MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_SECONDS
    engine.MAX_PAGES = MAX_PAGES
    engine.MAX_JOBS = MAX_JOBS
    engine.MAX_RENDERED_HTML_BYTES = MAX_RENDERED_HTML_BYTES
    engine.REVIEWED_COMPANIES = REVIEWED_COMPANIES
    engine.CANDIDATE_ENTRIES = CANDIDATE_ENTRIES
    engine.EXCLUDED_REVIEWS = EXCLUDED_REVIEWS
    engine.PLATFORM_NOTES = PLATFORM_NOTES
    engine.CSV_FIELDS = CSV_FIELDS


def _current_uncovered() -> tuple[list[str], int]:
    master = load_master_workbook(MASTER_WORKBOOK)
    targets = load_target_companies(str(PRODUCTION))
    mappings = load_parent_source_mappings(PARENT_MAPPINGS)
    reconciliation = reconcile_master(master, targets, mappings)
    return reconciliation.uncovered_names, reconciliation.total


def reconcile() -> list[dict[str, Any]]:
    _configure_engine()
    production = _engine_reconcile()
    uncovered, master_total = _current_uncovered()
    if master_total != 246 or len(uncovered) != HISTORICAL_UNCOVERED_COUNT:
        raise ValueError("Master reconciliation is not the exact 246/91 Batch A baseline")
    missing = [name for name in REVIEWED_COMPANIES if name not in uncovered]
    if missing:
        raise ValueError(f"Batch A selections are not currently uncovered: {missing}")
    ranks = [
        int((entry or review)["original_workbook_rank" if entry else "master_rank"])
        for name in REVIEWED_COMPANIES
        for entry, review in [(_entry_for(name), _review_for(name))]
    ]
    if ranks != sorted(ranks):
        raise ValueError("Batch A selections are not in deterministic master order")
    return production


def _decorate_result(result: dict[str, Any], careers_url: str) -> dict[str, Any]:
    company = str(result["company"])
    safe = bool(result.get("safe_to_activate"))
    result.update(
        industry=INDUSTRIES[company],
        careers_url=careers_url,
        detected_platform=PLATFORM_NOTES[company][2],
        result="validated" if safe else "excluded",
        adapter_used=PLATFORM_NOTES[company][0],
        jobs_normalized=int(result.get("validated_count") or 0),
        reason_if_excluded="" if safe else str(result.get("exclusion_reason") or ""),
    )
    return result


def _finalize_result(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    return _decorate_result(
        _engine_finalize_result(entry, runs),
        str(entry["careers_url"]),
    )


def _probe_exclusion(review: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    return _decorate_result(
        _engine_probe_exclusion(review, timeout),
        str(review["careers_url"]),
    )


def _identity_check(entry: Mapping[str, Any], session: Any, timeout: int) -> dict[str, Any]:
    checked = dict(entry)
    checked["careers_url"] = entry.get("identity_url") or entry["careers_url"]
    return _engine_identity_check(checked, session, timeout)


def _entry_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in CANDIDATE_ENTRIES if entry["name"] == name), None)


def _review_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in EXCLUDED_REVIEWS if entry["company"] == name), None)


def _stable_contract(entry: Optional[Mapping[str, Any]]) -> str:
    if not entry:
        return "not available; excluded"
    if entry.get("platform") == "greenhouse":
        return "Greenhouse numeric job ID"
    if entry.get("platform") == "ashby":
        return "Ashby UUID job ID"
    if entry.get("platform") == "eightfold":
        return "Eightfold requisition ID corroborated by numeric detail path"
    return str(entry.get("external_id_regex") or entry.get("external_id_attribute") or "")


def _write_inventory(results: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
    by_name = {str(item["company"]): item for item in (results or [])}
    rows: list[dict[str, Any]] = []
    for name in REVIEWED_COMPANIES:
        entry, review = _entry_for(name), _review_for(name)
        source = entry or review or {}
        result = by_name.get(name, {})
        runs = list(result.get("runs") or [])
        adapter, implementation, detected = PLATFORM_NOTES[name]
        safe = bool(result.get("safe_to_activate", False))
        status = str(
            result.get("validation_status")
            or (review or {}).get("classification")
            or "pending_validation"
        )
        rows.append(
            {
                "master_rank": source[
                    "original_workbook_rank" if entry is not None else "master_rank"
                ],
                "company": name,
                "industry": INDUSTRIES[name],
                "official_careers_url": source.get("careers_url", ""),
                "listing_url": source.get("listing_url") or source.get("search_url", ""),
                "detected_platform": detected,
                "result": "validated" if safe else "excluded" if result else "pending",
                "adapter_used": adapter,
                "jobs_normalized": result.get("validated_count", 0) if result else "",
                "implementation_path": implementation,
                "source_identity": engine.source_key(entry) if entry else source.get("current_platform", ""),
                "stable_id_contract": _stable_contract(entry),
                "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
                "anonymous_access": "true",
                "validation_status": status,
                "safe_to_activate": str(safe).lower(),
                "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
                "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
                "runtime_seconds_run_1": runs[0].get("runtime_seconds", "") if runs else "",
                "runtime_seconds_run_2": runs[1].get("runtime_seconds", "") if len(runs) > 1 else "",
                "excluded": str(bool(result) and not safe).lower(),
                "exclusion_reason": result.get("exclusion_reason")
                or (review or {}).get("exclusion_reason", ""),
            }
        )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    engine._write_bytes(CANDIDATE_CSV, buffer.getvalue().encode("utf-8"))


_configure_engine()
engine.reconcile = reconcile
engine._finalize_result = _finalize_result
engine._probe_exclusion = _probe_exclusion
engine._write_inventory = _write_inventory
engine._identity_check = _identity_check

source_key = engine.source_key
_duplicates = engine._duplicates
_verify_jobs = engine._verify_jobs
_status_for_error = engine._status_for_error
_run_direct = engine._run_direct
_run_browser = engine._run_browser
def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    additions: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    chosen = CANDIDATE_ENTRIES if additions is None else additions
    return _engine_build_candidate_registry(production, destination, chosen)


engine.build_candidate_registry = build_candidate_registry


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    payload = engine.validate(timeout)
    payload["candidate_registry_has_exact_160_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_156_entry_prefix"
    )
    payload["validated_registry_has_exact_160_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_156_entry_prefix"
    )
    if payload["totals"]["validated"] < 10 or payload["totals"]["validated"] > 15:
        raise RuntimeError("Batch A did not recover the requested 10-15 companies")
    payload["selection"] = {
        "master_companies": 246,
        "baseline_covered": HISTORICAL_ORIGINAL_COVERAGE,
        "baseline_uncovered": HISTORICAL_UNCOVERED_COUNT,
        "selected_from_current_uncovered": True,
        "priority_industries": list(dict.fromkeys(INDUSTRIES.values())),
    }
    engine._write_bytes(
        REPORT,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return payload


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
            validated = engine._registry_bytes(PRODUCTION, [])
            engine._write_bytes(VALIDATED_REGISTRY, validated)
            _write_inventory()
            payload: dict[str, Any] = {
                "candidate_registry_count": len(load_target_companies(str(CANDIDATE_REGISTRY))),
                "validated_registry_count": len(load_target_companies(str(VALIDATED_REGISTRY))),
                "candidate_bytes": len(candidate),
            }
        else:
            payload = validate(args.timeout)
    except (AdapterError, OSError, RuntimeError, ValueError) as exc:
        print(f"Source expansion v2 Batch A validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
