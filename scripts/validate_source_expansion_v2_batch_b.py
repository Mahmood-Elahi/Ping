"""Build and anonymously validate Source Expansion v2 Batch B.

The batch starts from the exact 171-source production baseline.  It writes only
Batch B candidate, validated, inventory, and ignored-report artifacts; it never
activates sources or changes parent mappings, persistence, or dashboard code.
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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.models import AdapterError
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


_ENGINE_SPEC = importlib.util.spec_from_file_location(
    "scripts._source_expansion_v2_batch_b_engine",
    ROOT / "scripts" / "validate_browser_expansion_v1b.py",
)
if _ENGINE_SPEC is None or _ENGINE_SPEC.loader is None:
    raise ImportError("Unable to load the isolated batch-validation engine")
engine = importlib.util.module_from_spec(_ENGINE_SPEC)
_ENGINE_SPEC.loader.exec_module(engine)


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_source_expansion_v2_batch_b_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.source_expansion_v2_batch_b_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.source_expansion_v2_batch_b_validated.yaml"
REPORT = ROOT / "output" / "source_expansion_v2_batch_b_validation.json"

HISTORICAL_PRODUCTION_COUNT = 171
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 166
HISTORICAL_UNCOVERED_COUNT = 80
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000


INDUSTRIES = {
    "Analog Devices": "Semiconductors",
    "STMicroelectronics": "Semiconductors",
    "Microchip Technology": "Semiconductors",
    "Omron": "Industrial automation",
    "Keyence": "Industrial automation",
    "BMW": "Automotive",
    "Magna International": "Automotive",
    "Epic Games": "AR/VR/XR",
    "Autodesk": "AR/VR/XR",
    "Ocado Technology": "E-commerce",
    "LinkedIn": "Social",
    "Twitch": "Social",
    "Quora": "Social",
    "Bumble": "Social",
    "Medium": "Social",
}

REVIEWED_COMPANIES = [
    "Analog Devices",
    "STMicroelectronics",
    "Microchip Technology",
    "Omron",
    "Keyence",
    "BMW",
    "Magna International",
    "Epic Games",
    "Autodesk",
    "Ocado Technology",
    "LinkedIn",
    "Twitch",
    "Quora",
    "Bumble",
    "Medium",
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


CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    _direct(
        31,
        "Analog Devices",
        "workday",
        "https://www.analog.com/en/careers.html",
        identity_regex=r"analogdevices",
        allowed_apply_hosts=["analogdevices.wd1.myworkdayjobs.com"],
        tenant="analogdevices",
        wd_subdomain="wd1",
        site_id="External",
        search_url="https://analogdevices.wd1.myworkdayjobs.com/External",
        identity_url="https://analogdevices.wd1.myworkdayjobs.com/External",
    ),
    _direct(
        34,
        "STMicroelectronics",
        "eightfold",
        "https://stmicroelectronics.eightfold.ai/careers",
        identity_regex=r"STMicroelectronics",
        allowed_apply_hosts=["stmicroelectronics.eightfold.ai"],
        endpoint_url="https://stmicroelectronics.eightfold.ai/api/pcsx/search",
        endpoint_variant="pcsx",
        organization_id="stmicroelectronics.com",
        max_pages=2,
    ),
    _direct(
        39,
        "Microchip Technology",
        "workday",
        "https://www.microchip.com/en-us/about/careers",
        identity_regex=r"microchiphr",
        allowed_apply_hosts=["microchiphr.wd5.myworkdayjobs.com"],
        tenant="microchiphr",
        wd_subdomain="wd5",
        site_id="External",
        search_url="https://microchiphr.wd5.myworkdayjobs.com/External",
        identity_url="https://microchiphr.wd5.myworkdayjobs.com/External",
    ),
    _direct(
        101,
        "Omron",
        "sap_successfactors",
        "https://careers.omron.com/",
        identity_regex=r"OMRON",
        allowed_apply_hosts=["careers.omron.com"],
        search_url="https://careers.omron.com/search/",
        endpoint_variant="rmk_table",
    ),
    _direct(
        103,
        "Keyence",
        "sap_successfactors",
        "https://careers.keyence.com/",
        identity_regex=r"Keyence",
        allowed_apply_hosts=["careers.keyence.com"],
        search_url="https://careers.keyence.com/search/",
        endpoint_variant="rmk_table",
    ),
    _direct(
        112,
        "BMW",
        "sap_successfactors",
        "https://www.bmwgroup.jobs/",
        identity_regex=r"BMW Group",
        allowed_apply_hosts=["jobs.bmwgroup.com"],
        search_url="https://jobs.bmwgroup.com/search/",
        endpoint_variant="rmk_table",
        identity_url="https://jobs.bmwgroup.com/?locale=en_US",
    ),
    _direct(
        127,
        "Magna International",
        "workday",
        "https://www.magna.com/careers",
        identity_regex=r"Magna",
        allowed_apply_hosts=["magna.wd3.myworkdayjobs.com"],
        tenant="magna",
        wd_subdomain="wd3",
        site_id="Magna",
        search_url="https://magna.wd3.myworkdayjobs.com/Magna",
    ),
    _direct(
        144,
        "Autodesk",
        "workday",
        "https://www.autodesk.com/careers",
        identity_regex=r"autodesk",
        allowed_apply_hosts=["autodesk.wd1.myworkdayjobs.com"],
        tenant="autodesk",
        wd_subdomain="wd1",
        site_id="Ext",
        search_url="https://autodesk.wd1.myworkdayjobs.com/Ext",
        identity_url="https://autodesk.wd1.myworkdayjobs.com/Ext",
    ),
    _direct(
        238,
        "Quora",
        "ashby",
        "https://careers.quora.com/",
        identity_regex=r"Quora",
        allowed_apply_hosts=["jobs.ashbyhq.com"],
        slug="quora",
        search_url="https://jobs.ashbyhq.com/quora",
    ),
    _direct(
        239,
        "Bumble",
        "lever",
        "https://team.bumble.com/en/life",
        identity_regex=r"Bumble",
        allowed_apply_hosts=["jobs.lever.co"],
        slug="bumbleinc",
        search_url="https://jobs.lever.co/bumbleinc",
        endpoint_variant="postings_api_v0",
    ),
    _direct(
        244,
        "Medium",
        "greenhouse",
        "https://medium.com/jobs-at-medium/work-at-medium-959d1a85284e",
        identity_regex=r"Medium",
        allowed_apply_hosts=["job-boards.greenhouse.io"],
        slug="medium",
        search_url="https://job-boards.greenhouse.io/medium",
        identity_url="https://job-boards.greenhouse.io/medium",
    ),
]


EXCLUDED_REVIEWS: list[dict[str, Any]] = [
    {
        "master_rank": 136,
        "company": "Epic Games",
        "careers_url": "https://www.epicgames.com/site/careers/jobs",
        "listing_url": "https://www.epicgames.com/site/careers/jobs",
        "current_platform": "epic_client_rendered_jobs",
        "adapter_platform": "none",
        "classification": "blocked",
        "identity_regex": r"Epic Games|Careers",
        "exclusion_reason": (
            "The official job collection is anonymously visible in indexed output, but bounded "
            "HTTP validation receives an access-control HTTP 403. Anti-bot controls are not bypassed."
        ),
    },
    {
        "master_rank": 221,
        "company": "Ocado Technology",
        "careers_url": "https://careers.ocadogroup.com/teams/technology",
        "listing_url": "https://careers.ocadogroup.com/jobs",
        "current_platform": "shared_ocado_group_greenhouse",
        "adapter_platform": "none",
        "classification": "insufficient_structured_parent_attribution",
        "identity_regex": r"Ocado|Technology",
        "exclusion_reason": (
            "The official technology page links to Ocado Group's shared Greenhouse collection. "
            "The standard job records do not expose an exact stable Ocado Technology company field, "
            "so activating the group board as this child identity would be speculative and mixed."
        ),
    },
    {
        "master_rank": 226,
        "company": "LinkedIn",
        "careers_url": "https://careers.linkedin.com/",
        "listing_url": "https://www.linkedin.com/company/linkedin/jobs/",
        "current_platform": "linkedin_session_job_search",
        "adapter_platform": "none",
        "classification": "session_required",
        "identity_regex": r"LinkedIn",
        "exclusion_reason": (
            "The official careers site sends job search to LinkedIn's session-oriented company-jobs "
            "experience. No existing adapter proves anonymous stable apply URLs without login state."
        ),
    },
    {
        "master_rank": 228,
        "company": "Twitch",
        "careers_url": "https://www.twitch.tv/jobs/",
        "listing_url": "https://www.amazon.jobs/content/en/teams/twitch",
        "current_platform": "shared_amazon_source",
        "adapter_platform": "none",
        "classification": "stale_parent_route",
        "identity_regex": r"Twitch",
        "exclusion_reason": (
            "The official Twitch jobs route now resolves to the general Twitch site and the recorded "
            "Amazon team route is HTTP 404. Amazon's search API exposes no enforced exact Twitch team "
            "filter in the existing adapter, so attribution would be speculative."
        ),
    },
]


PLATFORM_NOTES = {
    "Analog Devices": ("workday", "existing_direct_adapter", "Workday analogdevices/External"),
    "STMicroelectronics": ("eightfold", "existing_direct_adapter_blocked", "Eightfold PCSx public search"),
    "Microchip Technology": ("workday", "existing_direct_adapter", "Workday microchiphr/External"),
    "Omron": ("sap_successfactors", "existing_direct_adapter", "SuccessFactors RMK table"),
    "Keyence": ("sap_successfactors", "existing_direct_adapter", "SuccessFactors RMK table"),
    "BMW": ("sap_successfactors", "existing_adapter_incompatible", "SuccessFactors custom BMW search layout"),
    "Magna International": ("workday", "existing_direct_adapter", "Workday magna/Magna"),
    "Epic Games": ("none", "excluded_blocked", "Epic client-rendered official jobs"),
    "Autodesk": ("workday", "existing_direct_adapter", "Workday autodesk/Ext"),
    "Ocado Technology": ("none", "excluded_shared_parent_identity", "Ocado Group Greenhouse board"),
    "LinkedIn": ("none", "excluded_session_job_search", "LinkedIn company jobs experience"),
    "Twitch": ("none", "excluded_stale_parent_route", "Amazon shared source route"),
    "Quora": ("ashby", "existing_direct_adapter", "Ashby posting API: quora"),
    "Bumble": ("lever", "existing_direct_adapter", "Lever postings API: bumbleinc"),
    "Medium": ("greenhouse", "existing_direct_adapter", "Greenhouse boards API: medium"),
}


CSV_FIELDS = [
    "master_rank", "company", "industry", "official_careers_url", "listing_url",
    "detected_platform", "result", "adapter_used", "jobs_normalized",
    "implementation_path", "source_identity", "stable_id_contract",
    "allowed_apply_hosts", "anonymous_access", "validation_status",
    "safe_to_activate", "job_count_run_1", "job_count_run_2",
    "runtime_seconds_run_1", "runtime_seconds_run_2", "excluded", "exclusion_reason",
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
    reconciliation = reconcile_master(
        load_master_workbook(MASTER_WORKBOOK),
        load_target_companies(str(PRODUCTION)),
        load_parent_source_mappings(PARENT_MAPPINGS),
    )
    return reconciliation.uncovered_names, reconciliation.total


def reconcile() -> list[dict[str, Any]]:
    _configure_engine()
    production = _engine_reconcile()
    uncovered, master_total = _current_uncovered()
    if (master_total, len(uncovered)) != (246, HISTORICAL_UNCOVERED_COUNT):
        raise ValueError("Master reconciliation is not the exact 246/80 Batch B baseline")
    missing = [name for name in REVIEWED_COMPANIES if name not in uncovered]
    if missing:
        raise ValueError(f"Batch B selections are not currently uncovered: {missing}")
    ranks = [
        int((entry or review)["original_workbook_rank" if entry else "master_rank"])
        for name in REVIEWED_COMPANIES
        for entry, review in [(_entry_for(name), _review_for(name))]
    ]
    if ranks != sorted(ranks):
        raise ValueError("Batch B selections are not in deterministic master order")
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
    return _decorate_result(_engine_finalize_result(entry, runs), str(entry["careers_url"]))


def _probe_exclusion(review: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    return _decorate_result(_engine_probe_exclusion(review, timeout), str(review["careers_url"]))


def _identity_check(entry: Mapping[str, Any], session: Any, timeout: int) -> dict[str, Any]:
    checked = dict(entry)
    checked["careers_url"] = entry.get("identity_url") or entry["careers_url"]
    if entry.get("platform") == "workday" and entry.get("identity_url"):
        response = session.get(
            str(checked["careers_url"]),
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
            timeout=timeout,
        )
        expected_host = f"{entry['tenant']}.{entry['wd_subdomain']}.myworkdayjobs.com"
        actual_host = (urlparse(response.url).hostname or "").casefold()
        if response.status_code != 200 or not response.content:
            raise AdapterError(f"Official Workday identity page HTTP {response.status_code}")
        if len(response.content) > MAX_RENDERED_HTML_BYTES:
            raise AdapterError("Official Workday identity page exceeded 3 MB")
        bootstrap = response.text
        tenant_marker = f'tenant: "{entry["tenant"]}"'
        site_marker = f'siteId: "{entry["site_id"]}"'
        if actual_host != expected_host.casefold() or tenant_marker not in bootstrap or site_marker not in bootstrap:
            raise AdapterError("Official Workday page has missing, mixed, or ambiguous tenant identity")
        return {"status": response.status_code, "url": response.url, "bytes": len(response.content)}
    return _engine_identity_check(checked, session, timeout)


def _entry_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in CANDIDATE_ENTRIES if entry["name"] == name), None)


def _review_for(name: str) -> Optional[dict[str, Any]]:
    return next((review for review in EXCLUDED_REVIEWS if review["company"] == name), None)


def _stable_contract(entry: Optional[Mapping[str, Any]]) -> str:
    if not entry:
        return "not available; excluded"
    platform = str(entry.get("platform"))
    if platform == "workday":
        return "Workday requisition ID corroborated by detail path"
    if platform == "sap_successfactors":
        return "SuccessFactors numeric job ID from official detail path"
    if platform == "eightfold":
        return "Eightfold requisition ID corroborated by numeric detail path"
    if platform == "ashby":
        return "Ashby UUID job ID"
    if platform == "lever":
        return "Lever UUID posting ID"
    if platform == "greenhouse":
        return "Greenhouse numeric job ID"
    return "adapter-specific stable job ID"


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
        status = str(result.get("validation_status") or (review or {}).get("classification") or "pending_validation")
        rows.append({
            "master_rank": source["original_workbook_rank" if entry else "master_rank"],
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
            "exclusion_reason": result.get("exclusion_reason") or (review or {}).get("exclusion_reason", ""),
        })
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
    payload["candidate_registry_has_exact_171_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_156_entry_prefix"
    )
    payload["validated_registry_has_exact_171_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_156_entry_prefix"
    )
    payload["selection"] = {
        "master_companies": 246,
        "baseline_covered": HISTORICAL_ORIGINAL_COVERAGE,
        "baseline_uncovered": HISTORICAL_UNCOVERED_COUNT,
        "selected_from_current_uncovered": True,
        "priority_industries": list(dict.fromkeys(INDUSTRIES.values())),
    }
    engine._write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
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
        print(f"Source expansion v2 Batch B validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
