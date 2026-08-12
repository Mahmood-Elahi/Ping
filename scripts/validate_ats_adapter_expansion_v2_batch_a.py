"""Build and validate ATS Adapter Expansion v2 Batch A without activation."""

from __future__ import annotations

import argparse
import csv
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_company_registry as registry_validator
from scripts.refingerprint_uncovered_companies import load_master_workbook, reconcile_master
from src.scraper.company_careers.adapters import cornerstone_csod, sap_successfactors
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.parent_sources import load_parent_source_mappings
from src.scraper.company_careers.registry import load_target_companies


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_ats_adapter_expansion_v2_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.ats_adapter_expansion_v2_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.ats_adapter_expansion_v2_batch_a_validated.yaml"
REPORT = ROOT / "output" / "ats_adapter_expansion_v2_batch_a_validation.json"

BASELINE_SOURCES = 336
BASELINE_MAPPINGS = 4
BASELINE_COVERAGE = 207
MASTER_TOTAL = 246
MAX_TIMEOUT_SECONDS = 30

FIXTURES = [
    "CN Rail", "OHB SE", "WestJet", "SMART Technologies", "Ross Video",
    "Chemelex", "Blackline Safety", "AltaLink", "Hugging Face", "Skyworks Solutions",
]

CANDIDATE_ENTRIES: list[dict[str, Any]] = [
    {
        "name": "CN Rail",
        "platform": "cornerstone_csod",
        "careers_url": "https://www.cn.ca/en/careers",
        "search_url": "https://cn360.csod.com/ux/ats/careersite/1/home?c=cn360&lang=en-US",
        "endpoint_url": "https://us.api.csod.com/rec-job-search/external/jobs",
        "endpoint_variant": cornerstone_csod.ENDPOINT_VARIANT,
        "tenant": "cn360",
        "site_id": "1",
        "locale": "en-US",
        "source_identity_regex": r"Canadian National|\bCN\b",
        "identity_regex": r"Canadian National|\bCN\b",
        "allowed_apply_hosts": ["cn360.csod.com"],
        "page_size": 25,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    },
    {
        "name": "OHB SE",
        "platform": "cornerstone_csod",
        "careers_url": "https://www.ohb.de/en/career",
        "search_url": "https://career-ohb.csod.com/ux/ats/careersite/4/home?c=career-ohb&lang=en-GB",
        "endpoint_url": "https://eu-fra.api.csod.com/rec-job-search/external/jobs",
        "endpoint_variant": cornerstone_csod.ENDPOINT_VARIANT,
        "tenant": "career-ohb",
        "site_id": "4",
        "locale": "en-GB",
        "source_identity_regex": r"\bOHB\b",
        "identity_regex": r"\bOHB\b",
        "allowed_apply_hosts": ["career-ohb.csod.com"],
        "original_workbook_rank": 197,
        "page_size": 25,
        "max_pages": 1,
        "max_jobs": 20,
        "max_response_bytes": 3_000_000,
    },
]

BLOCKED_FIXTURES = [
    {
        "company": "WestJet", "platform": "dayforce",
        "careers_url": "https://jobs.dayforcehcm.com/en-CA/WestJet/CANDIDATEPORTAL",
        "endpoint_url": "https://jobs.dayforcehcm.com/api/geo/WestJet/jobposting/search",
        "tenant": "WestJet", "locale": "en-CA", "board": "CANDIDATEPORTAL",
        "expected_status": "session_or_waf_dependency",
    },
    {
        "company": "SMART Technologies", "platform": "dayforce",
        "careers_url": "https://jobs.dayforcehcm.com/en-US/smarttech/CANDIDATEPORTAL",
        "endpoint_url": "https://jobs.dayforcehcm.com/api/geo/smarttech/jobposting/search",
        "tenant": "smarttech", "locale": "en-US", "board": "CANDIDATEPORTAL",
        "expected_status": "session_or_waf_dependency",
    },
    {
        "company": "Ross Video", "platform": "dayforce",
        "careers_url": "https://jobs.dayforcehcm.com/en-US/rossvideo/CANDIDATEPORTAL",
        "endpoint_url": "https://jobs.dayforcehcm.com/api/geo/rossvideo/jobposting/search",
        "tenant": "rossvideo", "locale": "en-US", "board": "CANDIDATEPORTAL",
        "expected_status": "session_or_waf_dependency",
    },
    {
        "company": "Chemelex", "platform": "dayforce",
        "careers_url": "https://jobs.dayforcehcm.com/en-US/chemelex/CANDIDATEPORTAL",
        "endpoint_url": "https://jobs.dayforcehcm.com/api/geo/chemelex/jobposting/search",
        "tenant": "chemelex", "locale": "en-US", "board": "CANDIDATEPORTAL",
        "expected_status": "session_or_waf_dependency",
    },
    {
        "company": "Blackline Safety", "platform": "workable",
        "careers_url": "https://apply.workable.com/blacklinesafety/",
        "endpoint_url": "https://apply.workable.com/api/v1/widget/accounts/blacklinesafety",
        "tenant": "blacklinesafety", "expected_status": "captcha_dependency",
    },
    {
        "company": "AltaLink", "platform": "workable",
        "careers_url": "https://apply.workable.com/altalink/",
        "endpoint_url": "https://apply.workable.com/api/v1/widget/accounts/altalink",
        "tenant": "altalink", "expected_status": "captcha_dependency",
    },
    {
        "company": "Hugging Face", "platform": "workable",
        "careers_url": "https://apply.workable.com/huggingface/",
        "endpoint_url": "https://apply.workable.com/api/v1/widget/accounts/huggingface",
        "tenant": "huggingface", "expected_status": "captcha_dependency",
    },
    {
        "company": "Skyworks Solutions", "platform": "sap_successfactors_unify",
        "careers_url": "https://careers.skyworksinc.com/search/",
        "endpoint_url": "https://careers.skyworksinc.com/services/recruiting/v1/jobs",
        "tenant": "careers.skyworksinc.com", "locale": "en_US",
        "expected_status": "session_state_dependency",
    },
]

CSV_FIELDS = [
    "company", "platform", "adapter", "result", "official_careers_url", "listing_url",
    "endpoint_url", "endpoint_variant", "tenant", "site_id", "locale", "request_method",
    "validation_status", "validation_runs", "jobs_normalized_each_run", "total_available_each_run",
    "stable_id_result", "apply_url_result", "deterministic_result", "production_ready",
    "original_246_master", "master_rank", "exclusion_reason",
]

SAFE_TO_STAGE_FILES = [
    "config/company_ats_adapter_expansion_v2_batch_a_candidates.csv",
    "config/target_companies.ats_adapter_expansion_v2_batch_a_candidates.yaml",
    "config/target_companies.ats_adapter_expansion_v2_batch_a_validated.yaml",
    "scripts/investigate_remaining_companies.py",
    "scripts/investigate_unknown_companies.py",
    "scripts/company_watchlist_common.py",
    "scripts/validate_ats_adapter_expansion_v2_batch_a.py",
    "scripts/validate_company_registry.py",
    "src/scraper/company_careers/adapters/__init__.py",
    "src/scraper/company_careers/adapters/cornerstone_csod.py",
    "src/scraper/company_careers/scanner.py",
    "tests/test_ats_adapter_expansion_v2_batch_a.py",
]


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def _signature(jobs: Iterable[Any]) -> str:
    rows = [(job.source, job.job_id, job.title, job.location, job.url, job.updated_at) for job in jobs]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _run_candidate(entry: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    target = CompanyTarget.from_dict(dict(entry))
    adapter = {
        "cornerstone_csod": cornerstone_csod.fetch_jobs,
        "sap_successfactors": sap_successfactors.fetch_jobs,
    }[target.platform]
    runs: list[list[Any]] = []
    totals: list[Optional[int]] = []
    for _ in range(2):
        session = requests.Session()
        jobs = adapter(session, target, timeout)
        runs.append(jobs)
        metadata = getattr(session, f"{target.platform}_pagination", {})
        totals.append(metadata.get("total_available"))
    signatures = [_signature(jobs) for jobs in runs]
    stable_ids = all(
        len({(job.source, job.job_id) for job in jobs}) == len(jobs) and all(job.job_id for job in jobs)
        for jobs in runs
    )
    apply_urls = all(
        all((urlparse(job.url).hostname or "").casefold() in target.allowed_apply_hosts for job in jobs)
        for jobs in runs
    )
    if not runs[0] or len(runs[0]) != len(runs[1]) or len(set(signatures)) != 1:
        raise RuntimeError(f"{target.name} did not return a repeatable non-empty collection")
    if not stable_ids or not apply_urls:
        raise RuntimeError(f"{target.name} failed stable ID or apply URL validation")
    return {
        "company": target.name,
        "platform": target.platform,
        "adapter": target.platform,
        "result": "recovered",
        "official_careers_url": target.careers_url,
        "listing_url": target.search_url,
        "endpoint_url": target.endpoint_url,
        "endpoint_variant": target.endpoint_variant,
        "tenant": target.tenant or "",
        "site_id": target.site_id or "",
        "locale": target.locale or "",
        "request_method": "GET bootstrap + POST search",
        "validation_status": "repeatable_anonymous",
        "validation_runs": 2,
        "jobs_normalized_each_run": len(runs[0]),
        "total_available_each_run": totals,
        "stable_id_result": "valid",
        "apply_url_result": "valid",
        "deterministic_result": "stable_ordered_signature",
        "production_ready": True,
        "original_246_master": target.name == "OHB SE",
        "master_rank": 197 if target.name == "OHB SE" else "",
        "exclusion_reason": "",
        "signature": signatures[0],
    }


def _next_data(text: str) -> Mapping[str, Any]:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.I | re.S)
    if not match:
        raise RuntimeError("Dayforce shell omitted __NEXT_DATA__")
    value = json.loads(match.group(1))
    if not isinstance(value, Mapping):
        raise RuntimeError("Dayforce __NEXT_DATA__ was malformed")
    return value


def _probe_blocked(item: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    session = requests.Session()
    headers = {"User-Agent": "job-radar/1.0"}
    totals: list[Any] = []
    portal = session.get(str(item["careers_url"]), headers=headers, timeout=timeout)
    if portal.status_code != 200:
        raise RuntimeError(f"{item['company']} public shell HTTP {portal.status_code}")
    if item["platform"] == "dayforce":
        data = _next_data(portal.text)
        query = data.get("query") or {}
        if (
            str(query.get("clientNamespace") or "").casefold() != str(item["tenant"]).casefold()
            or str(query.get("careerSiteXRefCode") or "").casefold() != str(item["board"]).casefold()
        ):
            raise RuntimeError(f"{item['company']} Dayforce shell crossed tenant boundaries")
        body = {
            "clientNamespace": item["tenant"], "jobBoardCode": item["board"],
            "cultureCode": item["locale"], "paginationStart": 0,
        }
        response = session.post(
            str(item["endpoint_url"]), json=body,
            headers={**headers, "Accept": "application/json", "Content-Type": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 403:
            raise RuntimeError(
                f"{item['company']} Dayforce contract changed (expected fail-closed HTTP 403, got {response.status_code})"
            )
        status = "session_or_waf_dependency"
        reason = "Official public search POST returns HTTP 403; no WAF, browser, CAPTCHA, or session bypass is attempted."
        request_method = "GET shell + POST search"
    elif item["platform"] == "workable":
        enabled = bool(re.search(
            r'["\']wjb_acp_turnstile_captcha_enabled["\']\s*:\s*true', portal.text, re.I
        ))
        if not enabled:
            raise RuntimeError(f"{item['company']} Workable CAPTCHA contract changed and needs review")
        status = "captcha_dependency"
        reason = "Official Workable shell enables Turnstile; collection remains fail-closed before the widget request."
        request_method = "GET shell only"
    else:
        tokens = set(re.findall(r'var\s+CSRFToken\s*=\s*"([^"]+)"', portal.text))
        if len(tokens) != 1:
            raise RuntimeError("Skyworks Unify shell omitted an unambiguous public CSRF token")
        search_headers = {
            **headers,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://careers.skyworksinc.com",
            "Referer": str(item["careers_url"]),
            "X-CSRF-Token": next(iter(tokens)),
        }
        body = {
            "keywords": "", "locale": item["locale"], "location": "",
            "pageNumber": 0, "sortBy": "recent",
        }
        responses = [
            session.post(
                str(item["endpoint_url"]), headers=search_headers, json=body, timeout=timeout
            )
            for _ in range(2)
        ]
        payloads = [response.json() for response in responses]
        totals = [payload.get("totalJobs") for payload in payloads]
        ids = [
            [
                str(envelope.get("response", {}).get("id") or "")
                for envelope in payload.get("jobSearchResult", [])
            ]
            for payload in payloads
        ]
        if any(response.status_code != 200 for response in responses) or not all(ids):
            raise RuntimeError("Skyworks Unify public JSON contract did not return two valid observations")
        if totals[0] != totals[1] or ids[0] == ids[1]:
            raise RuntimeError("Skyworks Unify session-state fingerprint changed and needs review")
        status = "session_state_dependency"
        reason = (
            "Identical pageNumber=0 requests under one anonymous bootstrap return different "
            "ID sets; the collection is not deterministic and is not promoted."
        )
        request_method = "GET shell + two identical POST observations"
    return {
        "company": item["company"], "platform": item["platform"], "adapter": "none",
        "result": "excluded", "official_careers_url": item["careers_url"],
        "listing_url": item["careers_url"], "endpoint_url": item["endpoint_url"],
        "endpoint_variant": (
            "nextjs_jobposting_search" if item["platform"] == "dayforce"
            else "rmk_unify_session_state" if item["platform"] == "sap_successfactors_unify"
            else "widget_accounts_v1"
        ),
        "tenant": item["tenant"], "site_id": "", "locale": item.get("locale", ""),
        "request_method": request_method, "validation_status": status, "validation_runs": 1,
        "jobs_normalized_each_run": 0,
        "total_available_each_run": totals if item["platform"] == "sap_successfactors_unify" else [],
        "stable_id_result": "not_validated", "apply_url_result": "not_validated",
        "deterministic_result": "fail_closed", "production_ready": False,
        "original_246_master": False, "master_rank": "", "exclusion_reason": reason,
    }


def _write_csv(rows: list[Mapping[str, Any]]) -> None:
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})
    _write_bytes(CANDIDATE_CSV, output.getvalue().encode("utf-8"))


def _assert_baseline() -> None:
    targets = load_target_companies(str(PRODUCTION))
    mappings = load_parent_source_mappings(PARENT_MAPPINGS)
    master = reconcile_master(load_master_workbook(MASTER_WORKBOOK), targets, mappings)
    observed = (len(targets), len(mappings), len(master.canonical_names) + len(master.mapped_names), master.total)
    expected = (BASELINE_SOURCES, BASELINE_MAPPINGS, BASELINE_COVERAGE, MASTER_TOTAL)
    if observed != expected:
        raise RuntimeError(f"Production baseline changed: expected {expected}, observed {observed}")


def build_registries(validated: Iterable[Mapping[str, Any]] = ()) -> None:
    _write_bytes(
        CANDIDATE_REGISTRY,
        registry_validator.build_candidate_registry_bytes(PRODUCTION, CANDIDATE_ENTRIES),
    )
    _write_bytes(
        VALIDATED_REGISTRY,
        registry_validator.build_validated_registry_bytes(PRODUCTION, validated),
    )


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    _assert_baseline()
    build_registries()
    recovered = [_run_candidate(entry, timeout) for entry in CANDIDATE_ENTRIES]
    blockers = [_probe_blocked(item, timeout) for item in BLOCKED_FIXTURES]
    rows_by_name = {row["company"]: row for row in recovered + blockers}
    rows = [rows_by_name[name] for name in FIXTURES]
    build_registries(CANDIDATE_ENTRIES)
    _write_csv(rows)
    report = {
        "batch": "ats_adapter_expansion_v2_batch_a",
        "production_registry_activated": False,
        "publication": {"production_run": False, "staged": False, "committed": False, "pushed": False},
        "baseline": {
            "production_sources": BASELINE_SOURCES, "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": BASELINE_COVERAGE, "original_master_total": MASTER_TOTAL,
        },
        "projection": {
            "production_sources": BASELINE_SOURCES + len(recovered),
            "parent_mappings": BASELINE_MAPPINGS,
            "original_master_coverage": BASELINE_COVERAGE + 1,
            "original_master_total": MASTER_TOTAL,
            "companies_recovered": len(recovered),
            "jobs_normalized": sum(int(row["jobs_normalized_each_run"]) for row in recovered),
        },
        "supported_platforms": ["cornerstone_csod"],
        "recovered_companies": [row["company"] for row in recovered],
        "remaining_blockers": blockers,
        "results": rows,
        "contract_guarantees": {
            "cornerstone_csod": [
                "exact first-party careers link to tenant and numeric site",
                "fresh anonymous negative-subject player token scoped to external search",
                "official regional api.csod.com endpoint and stable numeric requisition ID",
                "one-based 25-row page with hard page, result, byte, and timeout limits",
                "official tenant/site requisition URL; no browser, cookie, login, or token fabrication",
            ],
        },
        "candidate_registry": str(CANDIDATE_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "validated_registry": str(VALIDATED_REGISTRY.relative_to(ROOT)).replace("\\", "/"),
        "safe_to_stage_files": SAFE_TO_STAGE_FILES,
    }
    _write_bytes(REPORT, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return report


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        _assert_baseline()
        if args.build_only:
            build_registries()
            return 0
        report = validate(min(max(1, args.timeout), MAX_TIMEOUT_SECONDS))
        print(json.dumps(report["projection"], indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, requests.RequestException, yaml.YAMLError) as exc:
        print(f"ATS Adapter Expansion v2 Batch A validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
