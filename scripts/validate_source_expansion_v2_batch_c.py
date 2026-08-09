"""Build and anonymously validate Source Expansion v2 Batch C.

The batch starts from the exact 180-source production baseline. It writes only
Batch C candidate, validated, inventory, and ignored-report artifacts; it never
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
    "scripts._source_expansion_v2_batch_c_engine",
    ROOT / "scripts" / "validate_browser_expansion_v1b.py",
)
if _ENGINE_SPEC is None or _ENGINE_SPEC.loader is None:
    raise ImportError("Unable to load the isolated batch-validation engine")
engine = importlib.util.module_from_spec(_ENGINE_SPEC)
_ENGINE_SPEC.loader.exec_module(engine)


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_source_expansion_v2_batch_c_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.source_expansion_v2_batch_c_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.source_expansion_v2_batch_c_validated.yaml"
REPORT = ROOT / "output" / "source_expansion_v2_batch_c_validation.json"

HISTORICAL_PRODUCTION_COUNT = 180
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 175
HISTORICAL_UNCOVERED_COUNT = 71
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000


INDUSTRIES = {
    "Tesla": "Automotive and energy",
    "Marvell Technology": "Semiconductors",
    "Oracle": "Enterprise software and cloud",
    "Block": "Financial technology",
    "Bloomberg": "Financial data and media",
    "Citadel Securities": "Market making and financial technology",
    "Mitsubishi Electric": "Industrial automation and electronics",
    "FANUC": "Industrial automation",
    "Yaskawa Electric": "Industrial automation",
    "Mercedes-Benz": "Automotive",
    "Continental": "Automotive technology",
    "Aptiv": "Automotive technology",
    "Sony": "Consumer electronics and entertainment",
    "Suncor Energy": "Energy",
    "Firefly Aerospace": "Aerospace",
}

REVIEWED_COMPANIES = [
    "Tesla", "Marvell Technology", "Oracle", "Block", "Bloomberg",
    "Citadel Securities", "Mitsubishi Electric", "FANUC", "Yaskawa Electric",
    "Mercedes-Benz", "Continental", "Aptiv", "Sony", "Suncor Energy",
    "Firefly Aerospace",
]

OFFICIAL_CAREERS_URLS = {
    "Tesla": "https://www.tesla.com/careers",
    "Marvell Technology": "https://www.marvell.com/company/careers.html",
    "Oracle": "https://www.oracle.com/careers/",
    "Block": "https://block.xyz/careers",
    "Bloomberg": "https://www.bloomberg.com/company/careers/",
    "Citadel Securities": "https://www.citadelsecurities.com/careers/",
    "Mitsubishi Electric": "https://www.mitsubishielectric.com/en/careers/",
    "FANUC": "https://www.fanucamerica.com/careers",
    "Yaskawa Electric": "https://www.yaskawa.com/about-us/careers",
    "Mercedes-Benz": "https://group.mercedes-benz.com/careers/",
    "Continental": "https://www.continental.com/en/career/",
    "Aptiv": "https://www.aptiv.com/jobs",
    "Sony": "https://www.sony.com/en/SonyInfo/Careers/",
    "Suncor Energy": "https://www.suncor.com/en-ca/careers",
    "Firefly Aerospace": "https://fireflyspace.com/careers/",
}


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
        35, "Marvell Technology", "workday",
        OFFICIAL_CAREERS_URLS["Marvell Technology"],
        identity_regex=r"marvell", allowed_apply_hosts=["marvell.wd1.myworkdayjobs.com"],
        tenant="marvell", wd_subdomain="wd1", site_id="MarvellCareers",
        search_url="https://marvell.wd1.myworkdayjobs.com/MarvellCareers",
        identity_url="https://marvell.wd1.myworkdayjobs.com/MarvellCareers",
    ),
    _direct(
        42, "Oracle", "oracle_recruiting",
        "https://careers.oracle.com/en/sites/jobsearch/jobs",
        identity_regex=r"Oracle", allowed_apply_hosts=["careers.oracle.com"],
        endpoint_url=(
            "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions"
        ),
        site_number="CX_45001", locale="en", max_pages=1,
    ),
    _direct(
        55, "Block", "greenhouse", OFFICIAL_CAREERS_URLS["Block"],
        identity_regex=r"\bBlock\b", allowed_apply_hosts=["block.xyz"],
        slug="block", search_url="https://block.xyz/careers/jobs", max_pages=1,
    ),
    _direct(
        137, "Sony", "workday", OFFICIAL_CAREERS_URLS["Sony"],
        identity_regex=r"sonyglobal", allowed_apply_hosts=["sonyglobal.wd1.myworkdayjobs.com"],
        tenant="sonyglobal", wd_subdomain="wd1", site_id="SonyGlobalCareers",
        search_url="https://sonyglobal.wd1.myworkdayjobs.com/SonyGlobalCareers",
        identity_url="https://sonyglobal.wd1.myworkdayjobs.com/SonyGlobalCareers",
    ),
    _direct(
        170, "Suncor Energy", "workday", OFFICIAL_CAREERS_URLS["Suncor Energy"],
        identity_regex=r"suncor", allowed_apply_hosts=["suncor.wd1.myworkdayjobs.com"],
        tenant="suncor", wd_subdomain="wd1", site_id="Suncor_External",
        search_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
        identity_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
    ),
    _direct(
        190, "Firefly Aerospace", "clearcompany",
        OFFICIAL_CAREERS_URLS["Firefly Aerospace"],
        identity_regex=r"Firefly Aerospace", allowed_apply_hosts=["firefly.clearcompany.com"],
        site_id="00ed92c3-5bfb-7bfb-456d-4d9d77fef9a5", max_pages=1,
        search_url="https://fireflyspace.com/careers/#jobopenings",
        endpoint_url=(
            "https://careers-api.clearcompany.com/v1/"
            "00ed92c3-5bfb-7bfb-456d-4d9d77fef9a5"
        ),
    ),
]


def _excluded(
    rank: int,
    company: str,
    listing_url: str,
    platform: str,
    classification: str,
    reason: str,
    identity_regex: str,
) -> dict[str, Any]:
    return {
        "master_rank": rank,
        "company": company,
        "careers_url": OFFICIAL_CAREERS_URLS[company],
        "listing_url": listing_url,
        "current_platform": platform,
        "adapter_platform": "none",
        "classification": classification,
        "identity_regex": identity_regex,
        "exclusion_reason": reason,
    }


EXCLUDED_REVIEWS: list[dict[str, Any]] = [
    _excluded(
        6, "Tesla", "https://www.tesla.com/careers/search/", "tesla_client_jobs",
        "blocked",
        "The official job search returns HTTP 403 to bounded anonymous validation. "
        "The indexed collection is not used because collection would depend on anti-bot access.",
        r"Tesla|Careers",
    ),
    _excluded(
        59, "Bloomberg", "https://www.bloomberg.com/company/careers/", "bloomberg_careers",
        "blocked",
        "The official Bloomberg careers source returns HTTP 403 to bounded anonymous validation; "
        "no CAPTCHA or anti-bot bypass is attempted.",
        r"Bloomberg|Careers",
    ),
    _excluded(
        75, "Citadel Securities",
        "https://www.citadelsecurities.com/careers/open-opportunities/",
        "citadel_server_rendered_jobs", "blocked",
        "Indexed job-specific detail pages are visible, but the official collection returns an "
        "HTTP 403 challenge to the validator. Depending on that challenge is rejected.",
        r"Citadel Securities|Open Opportunities",
    ),
    _excluded(
        97, "Mitsubishi Electric", "https://www.mitsubishielectric.com/en/careers/",
        "regional_careers_directory", "insufficient_structured_attribution",
        "The official global page is a directory of regional company career sites, not one job "
        "collection with exact Mitsubishi Electric attribution. Promoting one region would be partial "
        "and speculative.",
        r"Mitsubishi Electric|Careers",
    ),
    _excluded(
        98, "FANUC", "https://myjobs.adp.com/fanuc/cx", "adp_career_center",
        "insufficient_structured_attribution",
        "The discoverable source is FANUC America on ADP, a regional subsidiary source. It cannot "
        "be attributed to the canonical global FANUC company without an exact parent mapping.",
        r"FANUC|Careers",
    ),
    _excluded(
        100, "Yaskawa Electric", "https://www.yaskawa.com/about-us/careers/search-jobs",
        "liferay_ultipro_regional", "insufficient_structured_attribution",
        "The current job collection is explicitly Yaskawa America and links to regional UKG/UltiPro "
        "boards. It does not exactly represent the canonical Yaskawa Electric identity.",
        r"Yaskawa|Careers",
    ),
    _excluded(
        111, "Mercedes-Benz", "https://jobs.mercedes-benz.com/en",
        "beesite_public_api", "unsupported_public_api",
        "The anonymous Nuxt site identifies jobs.api.mercedes-benz.com, but no existing adapter "
        "validates that API's stable-ID, pagination, and job-specific apply URL contract.",
        r"Mercedes-Benz|Careers",
    ),
    _excluded(
        124, "Continental", "https://jobs.continental.com/en/",
        "continental_public_rest", "unsupported_public_api",
        "The page exposes a public configuration and result-list API, but it is an endpoint-specific "
        "contract unsupported by the existing adapters; it is not replaced with browser rendering.",
        r"Continental|Career|Jobs",
    ),
    _excluded(
        126, "Aptiv", "https://www.aptiv.com/en/jobs/search",
        "hawksearch_public_api", "unsupported_public_api",
        "The official page embeds a public HawkSearch endpoint and client ID, but the existing "
        "adapters do not support its request contract and stable official apply-URL verification.",
        r"Aptiv|Search Jobs",
    ),
]


PLATFORM_NOTES = {
    "Tesla": ("none", "excluded_blocked", "Tesla client-rendered official jobs"),
    "Marvell Technology": ("workday", "existing_direct_adapter", "Workday marvell/MarvellCareers"),
    "Oracle": ("oracle_recruiting", "existing_direct_adapter", "Oracle Recruiting CX_45001"),
    "Block": ("greenhouse", "existing_direct_adapter", "Greenhouse boards API: block"),
    "Bloomberg": ("none", "excluded_blocked", "Bloomberg official careers"),
    "Citadel Securities": ("none", "excluded_blocked", "Citadel Securities server-rendered jobs"),
    "Mitsubishi Electric": ("none", "excluded_regional_directory", "Mitsubishi Electric regional directory"),
    "FANUC": ("none", "excluded_regional_identity", "FANUC America ADP Career Center"),
    "Yaskawa Electric": ("none", "excluded_regional_identity", "Yaskawa America Liferay/UKG"),
    "Mercedes-Benz": ("none", "excluded_unsupported_api", "Mercedes-Benz BeeSite public API"),
    "Continental": ("none", "excluded_unsupported_api", "Continental result-list REST API"),
    "Aptiv": ("none", "excluded_unsupported_api", "Aptiv HawkSearch public API"),
    "Sony": ("workday", "existing_direct_adapter", "Workday sonyglobal/SonyGlobalCareers"),
    "Suncor Energy": ("workday", "existing_direct_adapter", "Workday suncor/Suncor_External"),
    "Firefly Aerospace": ("clearcompany", "new_public_api_adapter", "ClearCompany public career-site API"),
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
    for name, value in {
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS,
        "CANDIDATE_CSV": CANDIDATE_CSV, "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY,
        "VALIDATED_REGISTRY": VALIDATED_REGISTRY, "REPORT": REPORT,
        "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS, "MAX_PAGES": MAX_PAGES,
        "MAX_JOBS": MAX_JOBS, "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES,
        "REVIEWED_COMPANIES": REVIEWED_COMPANIES, "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES,
        "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS, "PLATFORM_NOTES": PLATFORM_NOTES,
        "CSV_FIELDS": CSV_FIELDS,
    }.items():
        setattr(engine, name, value)


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
        raise ValueError("Master reconciliation is not the exact 246/71 Batch C baseline")
    missing = [name for name in REVIEWED_COMPANIES if name not in uncovered]
    if missing:
        raise ValueError(f"Batch C selections are not currently uncovered: {missing}")
    ranks = [
        int((entry or review)["original_workbook_rank" if entry else "master_rank"])
        for name in REVIEWED_COMPANIES
        for entry, review in [(_entry_for(name), _review_for(name))]
    ]
    if ranks != sorted(ranks):
        raise ValueError("Batch C selections are not in deterministic master order")
    return production


def _decorate_result(result: dict[str, Any]) -> dict[str, Any]:
    company = str(result["company"])
    safe = bool(result.get("safe_to_activate"))
    result.update(
        industry=INDUSTRIES[company], careers_url=OFFICIAL_CAREERS_URLS[company],
        detected_platform=PLATFORM_NOTES[company][2],
        result="validated" if safe else "excluded",
        adapter_used=PLATFORM_NOTES[company][0],
        jobs_normalized=int(result.get("validated_count") or 0),
        reason_if_excluded="" if safe else str(result.get("exclusion_reason") or ""),
    )
    return result


def _finalize_result(entry: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    return _decorate_result(_engine_finalize_result(entry, runs))


def _probe_exclusion(review: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    return _decorate_result(_engine_probe_exclusion(review, timeout))


def _identity_check(entry: Mapping[str, Any], session: Any, timeout: int) -> dict[str, Any]:
    if entry.get("platform") == "workday" and entry.get("identity_url"):
        response = session.get(
            str(entry["identity_url"]),
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"},
            timeout=timeout,
        )
        expected_host = f"{entry['tenant']}.{entry['wd_subdomain']}.myworkdayjobs.com"
        actual_host = (urlparse(response.url).hostname or "").casefold()
        if response.status_code != 200 or not response.content:
            raise AdapterError(f"Official Workday identity page HTTP {response.status_code}")
        if len(response.content) > MAX_RENDERED_HTML_BYTES:
            raise AdapterError("Official Workday identity page exceeded 3 MB")
        tenant_marker = f'tenant: "{entry["tenant"]}"'
        site_marker = f'siteId: "{entry["site_id"]}"'
        if (
            actual_host != expected_host.casefold()
            or tenant_marker not in response.text
            or site_marker not in response.text
        ):
            raise AdapterError("Official Workday page has missing, mixed, or ambiguous tenant identity")
        return {"status": response.status_code, "url": response.url, "bytes": len(response.content)}
    return _engine_identity_check(entry, session, timeout)


def _entry_for(name: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in CANDIDATE_ENTRIES if entry["name"] == name), None)


def _review_for(name: str) -> Optional[dict[str, Any]]:
    return next((review for review in EXCLUDED_REVIEWS if review["company"] == name), None)


def _stable_contract(entry: Optional[Mapping[str, Any]]) -> str:
    if not entry:
        return "not available; excluded"
    if entry.get("platform") == "workday":
        return "Workday requisition ID corroborated by detail path"
    if entry.get("platform") == "oracle_recruiting":
        return "Oracle Recruiting numeric requisition ID from official detail path"
    if entry.get("platform") == "greenhouse":
        return "Greenhouse numeric job ID"
    if entry.get("platform") == "clearcompany":
        return "ClearCompany UUID job ID corroborated by apply path"
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
            "company": name, "industry": INDUSTRIES[name],
            "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "detected_platform": detected,
            "result": "validated" if safe else "excluded" if result else "pending",
            "adapter_used": adapter,
            "jobs_normalized": result.get("validated_count", 0) if result else "",
            "implementation_path": implementation,
            "source_identity": engine.source_key(entry) if entry else source.get("current_platform", ""),
            "stable_id_contract": _stable_contract(entry),
            "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
            "anonymous_access": "true", "validation_status": status,
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
    payload["candidate_registry_has_exact_180_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_156_entry_prefix"
    )
    payload["validated_registry_has_exact_180_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_156_entry_prefix"
    )
    payload["selection"] = {
        "master_companies": 246, "baseline_covered": HISTORICAL_ORIGINAL_COVERAGE,
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
        print(f"Source expansion v2 Batch C validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
