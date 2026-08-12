"""Build and validate Original Master Recovery Batch H without changing production."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_original_master_recovery_batch_g as prior


engine = prior.engine
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_original_master_recovery_batch_h_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_h_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_h_validated.yaml"
REPORT = ROOT / "output" / "original_master_recovery_batch_h_validation.json"
SAFE_TO_STAGE_FILES = [
    "config/company_original_master_recovery_batch_h_candidates.csv",
    "config/target_companies.original_master_recovery_batch_h_candidates.yaml",
    "config/target_companies.original_master_recovery_batch_h_validated.yaml",
    "scripts/validate_original_master_recovery_batch_h.py",
    "src/scraper/company_careers/adapters/beesite.py",
    "src/scraper/company_careers/adapters/embedded_data.py",
    "src/scraper/company_careers/adapters/mcloud_cws.py",
    "src/scraper/company_careers/models.py",
    "src/scraper/company_careers/scanner.py",
    "tests/test_original_master_recovery_batch_h.py",
]

HISTORICAL_PRODUCTION_COUNT = 325
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 196
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Tesla",
    "STMicroelectronics",
    "Netflix",
    "Bloomberg",
    "Revolut",
    "SoFi",
    "Citadel Securities",
    "Hitachi",
    "Cognex",
    "Mercedes-Benz",
    "BMW",
    "Porsche",
    "Continental",
    "Denso",
    "Stellantis",
]
RECOVERY_TARGETS = set(REVIEWED_COMPANIES)
MASTER_RANKS = {
    "Tesla": 6,
    "STMicroelectronics": 34,
    "Netflix": 48,
    "Bloomberg": 59,
    "Revolut": 66,
    "SoFi": 72,
    "Citadel Securities": 75,
    "Hitachi": 107,
    "Cognex": 109,
    "Mercedes-Benz": 111,
    "BMW": 112,
    "Porsche": 114,
    "Continental": 124,
    "Denso": 128,
    "Stellantis": 132,
}

OFFICIAL_CAREERS_URLS = {
    "Tesla": "https://www.tesla.com/careers/search",
    "STMicroelectronics": "https://stmicroelectronics.eightfold.ai/careers",
    "Netflix": "https://jobs.netflix.com/",
    "Bloomberg": "https://www.bloomberg.com/company/careers/",
    "Revolut": "https://www.revolut.com/careers/",
    "SoFi": "https://www.sofi.com/careers/",
    "Citadel Securities": "https://www.citadelsecurities.com/careers/open-opportunities/",
    "Hitachi": "https://careers.hitachi.com/",
    "Cognex": "https://www.cognex.com/company/careers",
    "Mercedes-Benz": "https://jobs.mercedes-benz.com/en",
    "BMW": "https://www.bmwgroup.jobs/ca/en/jobfinder.html",
    "Porsche": "https://jobs.porsche.com/",
    "Continental": "https://jobs.continental.com/en/",
    "Denso": "https://www.denso.com/global/en/careers/",
    "Stellantis": "https://careers.stellantis.com/",
}

IDENTITY_LOCATION_EVIDENCE = {
    "Tesla": "Tesla's exact first-party search route is still indexed with current vacancies, but bounded anonymous requests receive an Access Denied response.",
    "STMicroelectronics": "The exact STMicroelectronics Eightfold tenant loads a branded shell, but its anonymous PCSX collection says that PCSX is not enabled for this user.",
    "Netflix": "Netflix's first-party jobs site delegates to the exact explore.jobs.netflix.net Eightfold tenant, whose anonymous PCSX collection is currently disabled.",
    "Bloomberg": "Bloomberg's exact Avature tenant exposes a server-rendered public collection; all detail URLs remain on bloomberg.avature.net and identify Bloomberg.",
    "Revolut": "Revolut's exact first-party collection remains indexed with UUID job paths, but bounded anonymous requests receive HTTP 403.",
    "SoFi": "SoFi's exact careers route and university program remain official, but bounded anonymous requests receive HTTP 403.",
    "Citadel Securities": "The exact Citadel Securities opportunities route returns a Cloudflare challenge to bounded anonymous requests.",
    "Hitachi": "Hitachi's exact global TalentBrew collection remains indexed with subsidiary and location facets, but bounded anonymous requests receive HTTP 403.",
    "Cognex": "The exact Cognex careers route returns a Vercel Security Checkpoint with HTTP 429.",
    "Mercedes-Benz": "The first-party Mercedes-Benz Nuxt shell publishes jobs.api.mercedes-benz.com; the public BeeSite response attributes every bounded row to Mercedes-Benz organizations.",
    "BMW": "The BMW Group job finder remains indexed with current BMW Group vacancies, but all reviewed canonical paths time out under bounded anonymous requests.",
    "Porsche": "The first-party Porsche job site publishes the Porsche BeeSite GJB endpoint; every bounded row has Porsche organization attribution and a jobs.porsche.com detail URL.",
    "Continental": "Continental's first-party jobs configuration publishes its anonymous result-list endpoint; every bounded row has client=continental and a jobs.continental.com detail URL.",
    "Denso": "DENSO's exact global careers route returns HTTP 403; regional collections are not merged into the global master identity without an explicit stable delegation contract.",
    "Stellantis": "The first-party Stellantis page publishes an exact M-Cloud organization UUID and API host; the adapter re-verifies that binding before accepting first-party Stellantis job URLs.",
}

COMPATIBILITY = {
    "Tesla": "Canada/US and internship compatible in the indexed collection, but current hits cannot be safely normalized through the access control.",
    "STMicroelectronics": "Canada/US and internship compatible in indexed details, but the disabled anonymous PCSX collection prevents safe current normalization.",
    "Netflix": "Canada/US/Remote and internship compatible in principle, but the disabled anonymous PCSX collection prevents safe current normalization.",
    "Bloomberg": "Canada/US compatible; the bounded 20-job prefix contains United States roles and no current student-title hit.",
    "Revolut": "Remote and internship compatible in principle; current Canada/US hits cannot be safely normalized through the protected listing.",
    "SoFi": "US and university-recruiting compatible; current hits cannot be safely normalized through the protected listing.",
    "Citadel Securities": "US and internship compatible in principle; the challenge prevents current normalization.",
    "Hitachi": "Canada/US/Remote and internship compatible in the indexed collection; the current access response prevents safe normalization.",
    "Cognex": "Canada/US/global and student compatible in principle; the security checkpoint prevents current validation.",
    "Mercedes-Benz": "The global bounded prefix includes German student-employment titles, while the current English student matcher reports zero hits; its newest 20 jobs do not represent Canada/US/Remote locations.",
    "BMW": "Canada/US and internship/co-op compatible in the indexed job finder; repeated bounded timeouts prevent current normalization.",
    "Porsche": "The bounded prefix has multiple current intern and working-student roles; it does not currently represent Canada/US/Remote locations.",
    "Continental": "Canada/US compatible; the bounded prefix contains multiple current United States internship roles.",
    "Denso": "Canada/US and student programs exist regionally, but the global source boundary and current collection contract remain unsafe to infer.",
    "Stellantis": "Canada/US compatible; the bounded prefix contains multiple US roles and no current student-title hit.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Tesla": OFFICIAL_CAREERS_URLS["Tesla"],
    "STMicroelectronics": OFFICIAL_CAREERS_URLS["STMicroelectronics"],
    "Netflix": OFFICIAL_CAREERS_URLS["Netflix"],
    "Bloomberg": OFFICIAL_CAREERS_URLS["Bloomberg"],
    "Revolut": OFFICIAL_CAREERS_URLS["Revolut"],
    "SoFi": "https://www.sofi.com/careers/university-recruiting/",
    "Citadel Securities": OFFICIAL_CAREERS_URLS["Citadel Securities"],
    "Hitachi": "https://careers.hitachi.com/search/internship/jobs",
    "Cognex": OFFICIAL_CAREERS_URLS["Cognex"],
    "Mercedes-Benz": OFFICIAL_CAREERS_URLS["Mercedes-Benz"],
    "BMW": "https://www.bmwgroup.jobs/ca/en/opportunities/student.html",
    "Porsche": OFFICIAL_CAREERS_URLS["Porsche"],
    "Continental": OFFICIAL_CAREERS_URLS["Continental"],
    "Denso": OFFICIAL_CAREERS_URLS["Denso"],
    "Stellantis": OFFICIAL_CAREERS_URLS["Stellantis"],
}


def _entry(
    name: str,
    platform: str,
    careers_url: str,
    *,
    identity_regex: str,
    allowed_apply_hosts: list[str],
    **fields: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "platform": platform,
        "careers_url": careers_url,
        "source_identity_regex": identity_regex,
        "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS[name],
        "max_pages": MAX_PAGES,
        "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES = [
    _entry(
        "Bloomberg",
        "avature",
        OFFICIAL_CAREERS_URLS["Bloomberg"],
        identity_regex=r"Bloomberg Careers|Find the right role",
        allowed_apply_hosts=["bloomberg.avature.net"],
        identity_url="https://bloomberg.avature.net/careers/SearchJobs",
        search_url="https://bloomberg.avature.net/careers/SearchJobs",
        endpoint_variant="avature_portal_html",
        board_id="bloomberg",
        page_size=12,
    ),
    _entry(
        "Mercedes-Benz",
        "beesite",
        OFFICIAL_CAREERS_URLS["Mercedes-Benz"],
        identity_regex=r"Mercedes-Benz",
        allowed_apply_hosts=["jobs.mercedes-benz.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Mercedes-Benz"],
        endpoint_url="https://jobs.api.mercedes-benz.com/search",
        endpoint_variant="gjb_search_v1",
        locale="en",
        allowed_source_hosts=["jobs.api.mercedes-benz.com"],
        job_attribution_regex=r"(?i)Mercedes[- ]Benz",
        page_size=10,
    ),
    _entry(
        "Porsche",
        "beesite",
        OFFICIAL_CAREERS_URLS["Porsche"],
        identity_regex=r"Porsche",
        allowed_apply_hosts=["jobs.porsche.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Porsche"],
        endpoint_url="https://porsche-beesite-production-gjb.app.beesite.de/search/",
        endpoint_variant="gjb_search_v1",
        locale="en",
        allowed_source_hosts=["porsche-beesite-production-gjb.app.beesite.de"],
        job_attribution_regex=r"(?i)Porsche",
        page_size=10,
    ),
    _entry(
        "Continental",
        "javascript_embedded_data",
        OFFICIAL_CAREERS_URLS["Continental"],
        identity_regex=r'"client"\s*:\s*"continental"',
        allowed_apply_hosts=["jobs.continental.com"],
        identity_url=(
            "https://jobs.continental.com/en/api/result-list/pagetype-jobs/"
            "?tx_conjobs_api%5BcurrentPage%5D=1&tx_conjobs_api%5BitemsPerPage%5D=10"
        ),
        listing_url=(
            "https://jobs.continental.com/en/api/result-list/pagetype-jobs/"
            "?tx_conjobs_api%5BcurrentPage%5D=1&tx_conjobs_api%5BitemsPerPage%5D=10"
        ),
        pagination_url_template=(
            "https://jobs.continental.com/en/api/result-list/pagetype-jobs/"
            "?tx_conjobs_api%5BcurrentPage%5D={page}&tx_conjobs_api%5BitemsPerPage%5D=10"
        ),
        endpoint_variant="public_json_endpoint",
        embedded_json_path="result.list",
        id_field="uuid",
        title_field="title",
        location_fields=["cityLabel", "countryLabel", "jobFlexibilityLabel"],
        url_field="absoluteUrl",
        date_field="publicationDate",
        department_field="fieldOfWorkLabel",
        job_attribution_field="client",
        job_attribution_regex=r"^continental$",
        allowed_source_hosts=["jobs.continental.com"],
        page_size=10,
    ),
    _entry(
        "Stellantis",
        "mcloud_cws",
        OFFICIAL_CAREERS_URLS["Stellantis"],
        identity_regex=r"Stellantis",
        allowed_apply_hosts=["careers.stellantis.com"],
        identity_url=OFFICIAL_CAREERS_URLS["Stellantis"],
        endpoint_url="https://jobsapi-google.m-cloud.io/api/job/search",
        endpoint_variant="google_jobs_v1",
        organization_id="companies/16115603-6c1b-4c45-b544-238a4e6c51b3",
        job_attribution_regex=r"(?i)FCA|Stellantis",
        allowed_source_hosts=["jobsapi-google.m-cloud.io"],
        page_size=10,
    ),
]


def _excluded(
    company: str,
    platform: str,
    listing_url: str,
    classification: str,
    reason: str,
    identity_regex: str,
) -> dict[str, Any]:
    return {
        "company": company,
        "platform": platform,
        "adapter": "none",
        "listing_url": listing_url,
        "classification": classification,
        "exclusion_reason": reason,
        "identity_regex": identity_regex,
    }


EXCLUDED_REVIEWS = [
    _excluded("Tesla", "Custom first-party / Access Denied", OFFICIAL_CAREERS_URLS["Tesla"], "blocked", "Tesla's exact search route returns HTTP 403 Access Denied. No WAF, cookie, browser, or session bypass is attempted.", r"Tesla|Access Denied"),
    _excluded("STMicroelectronics", "Eightfold", "https://stmicroelectronics.eightfold.ai/api/pcsx/search?domain=stmicroelectronics.com&query=&location=&start=0", "anonymous_contract_disabled", "The branded Eightfold shell is public, but its anonymous PCSX endpoint returns HTTP 403 with PCSX not enabled. No reCAPTCHA or session workaround is attempted.", r"PCSX|not enabled|STMicroelectronics"),
    _excluded("Netflix", "Eightfold", "https://explore.jobs.netflix.net/api/pcsx/search?domain=netflix.com&query=&location=&start=0", "anonymous_contract_disabled", "Netflix delegates to its exact Eightfold tenant, but the anonymous PCSX endpoint returns HTTP 403 with PCSX not enabled. No reCAPTCHA or session workaround is attempted.", r"PCSX|not enabled|Netflix"),
    _excluded("Revolut", "Custom first-party / HTTP 403", OFFICIAL_CAREERS_URLS["Revolut"], "blocked", "The exact first-party collection and UUID position paths remain official, but bounded anonymous requests receive HTTP 403. No security-checkpoint, cookie, or session bypass is attempted.", r"Revolut|Access Denied"),
    _excluded("SoFi", "Custom first-party / HTTP 403", OFFICIAL_CAREERS_URLS["SoFi"], "blocked", "The exact first-party careers page and university program remain official, but bounded anonymous requests receive HTTP 403. No WAF, cookie, or browser bypass is attempted.", r"SoFi|Welcome"),
    _excluded("Citadel Securities", "Custom first-party / Cloudflare", OFFICIAL_CAREERS_URLS["Citadel Securities"], "blocked", "The exact opportunities route returns a Cloudflare challenge. No challenge, cookie, browser, or session bypass is attempted.", r"Citadel Securities|Just a moment|Cloudflare"),
    _excluded("Hitachi", "Radancy TalentBrew / HTTP 403", "https://careers.hitachi.com/search/jobs", "blocked", "The exact global TalentBrew collection remains indexed, but bounded anonymous requests receive HTTP 403. The static adapter is not forced through the access control.", r"Hitachi|Careers"),
    _excluded("Cognex", "Vercel Security Checkpoint", OFFICIAL_CAREERS_URLS["Cognex"], "blocked", "The exact Cognex careers route returns HTTP 429 with a Vercel Security Checkpoint. No challenge or session bypass is attempted.", r"Cognex|Security Checkpoint"),
    _excluded("BMW", "BMW Group custom job finder", OFFICIAL_CAREERS_URLS["BMW"], "network_inaccessible", "The exact BMW Group job finder is indexed with current vacancies, but three canonical paths time out under repeated bounded requests. No alternate browser or session path is used.", r"BMW Group|Jobs at the BMW Group"),
    _excluded("Denso", "Custom first-party / HTTP 403", OFFICIAL_CAREERS_URLS["Denso"], "blocked", "DENSO's exact global careers route returns HTTP 403. Regional boards are not inferred as the global master source without a stable explicit delegation contract.", r"DENSO|Forbidden|Access Denied"),
]

PLATFORM_NOTES = {
    "Tesla": ("Custom first-party / HTTP 403", "none"),
    "STMicroelectronics": ("Eightfold / PCSX disabled", "none"),
    "Netflix": ("Eightfold / PCSX disabled", "none"),
    "Bloomberg": ("Avature portal HTML", "avature"),
    "Revolut": ("Custom first-party / HTTP 403", "none"),
    "SoFi": ("Custom first-party / HTTP 403", "none"),
    "Citadel Securities": ("Custom first-party / Cloudflare", "none"),
    "Hitachi": ("Radancy TalentBrew / HTTP 403", "none"),
    "Cognex": ("Vercel Security Checkpoint", "none"),
    "Mercedes-Benz": ("BeeSite GJB search", "beesite"),
    "BMW": ("BMW Group custom job finder", "none"),
    "Porsche": ("BeeSite GJB search", "beesite"),
    "Continental": ("TYPO3 public JSON result list", "javascript_embedded_data"),
    "Denso": ("Custom first-party / HTTP 403", "none"),
    "Stellantis": ("M-Cloud CWS Google Jobs", "mcloud_cws"),
}

ADAPTER_OPPORTUNITIES = [
    {
        "platform": "BeeSite GJB search",
        "companies": ["Mercedes-Benz", "Porsche"],
        "reusable_adapter_justified": True,
        "recommendation": "Implemented one bounded generic adapter with explicit source/apply hosts, per-row organization attribution, stable ID checks, and repeated-page protection.",
    },
    {
        "platform": "Public JSON collection",
        "companies": ["Continental"],
        "reusable_adapter_justified": True,
        "recommendation": "Extended the generic embedded-data adapter with a direct JSON variant requiring explicit source hosts and exact per-row attribution.",
    },
    {
        "platform": "M-Cloud CWS Google Jobs",
        "companies": ["Stellantis"],
        "reusable_adapter_justified": True,
        "recommendation": "Implemented a generic bounded adapter that re-verifies the official page's cws_opts organization/API binding before each anonymous API scan.",
    },
    {
        "platform": "Eightfold PCSX",
        "companies": ["STMicroelectronics", "Netflix"],
        "reusable_adapter_justified": False,
        "recommendation": "Retain the existing Eightfold adapter; both exact tenants currently disable the anonymous PCSX contract, so no session or CAPTCHA path is appropriate.",
    },
]

LOCATION_RE = re.compile(
    r"\b(?:Canada|Remote|United States|New York|Fort Mill|Auburn Hills|Kokomo|Warren)\b|"
    r"(?:,|\|)\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
    re.IGNORECASE,
)

CSV_FIELDS = [
    "company", "fresh_vs_recovery_target", "official_careers_url", "listing_url",
    "identity_ownership_location_evidence", "canada_us_remote_student_compatibility",
    "platform", "adapter", "result", "jobs_normalized", "current_student_intern_hits",
    "current_student_role_evidence", "canada_us_remote_locations_represented",
    "student_program_evidence_url", "exclusion_reason", "original_246_master", "master_rank",
    "validation_status", "stable_id_result", "apply_url_result", "allowed_apply_hosts",
    "job_count_run_1", "job_count_run_2",
]


def _configure_prior() -> None:
    values = {
        "PRODUCTION": PRODUCTION,
        "PARENT_MAPPINGS": PARENT_MAPPINGS,
        "MASTER_WORKBOOK": MASTER_WORKBOOK,
        "CANDIDATE_CSV": CANDIDATE_CSV,
        "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY,
        "VALIDATED_REGISTRY": VALIDATED_REGISTRY,
        "REPORT": REPORT,
        "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT,
        "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS,
        "MAX_PAGES": MAX_PAGES,
        "MAX_JOBS": MAX_JOBS,
        "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES,
        "REVIEWED_COMPANIES": REVIEWED_COMPANIES,
        "MASTER_RANKS": MASTER_RANKS,
        "RECOVERY_TARGETS": RECOVERY_TARGETS,
        "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS,
        "IDENTITY_LOCATION_EVIDENCE": IDENTITY_LOCATION_EVIDENCE,
        "COMPATIBILITY": COMPATIBILITY,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES,
        "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES,
        "ADAPTER_OPPORTUNITIES": ADAPTER_OPPORTUNITIES,
        "LOCATION_RE": LOCATION_RE,
        "CSV_FIELDS": CSV_FIELDS,
    }
    for name, value in values.items():
        setattr(prior, name, value)


@contextmanager
def _isolated_prior_configuration() -> Iterator[None]:
    modules = (prior, prior.prior, engine)
    snapshots = [(module, dict(module.__dict__)) for module in modules]
    try:
        _configure_prior()
        yield
    finally:
        for module, snapshot in reversed(snapshots):
            for name in set(module.__dict__) - set(snapshot):
                module.__dict__.pop(name, None)
            module.__dict__.update(snapshot)


source_key = prior.source_key
_duplicates = prior._duplicates
_verify_jobs = prior._verify_jobs
_status_for_error = prior._status_for_error


def reconcile() -> list[dict[str, Any]]:
    with _isolated_prior_configuration():
        return prior.reconcile()


def build_candidate_registry(
    production: Path = PRODUCTION,
    destination: Path = CANDIDATE_REGISTRY,
    additions: Optional[Iterable[Mapping[str, Any]]] = None,
) -> bytes:
    chosen = CANDIDATE_ENTRIES if additions is None else additions
    with _isolated_prior_configuration():
        return prior.build_candidate_registry(production, destination, chosen)


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    with _isolated_prior_configuration():
        payload = prior.validate(timeout)
    payload["candidate_registry_has_exact_325_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_257_entry_prefix"
    )
    payload["validated_registry_has_exact_325_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_257_entry_prefix"
    )
    payload["adapter_opportunities"] = ADAPTER_OPPORTUNITIES
    payload["safe_to_stage_files"] = SAFE_TO_STAGE_FILES
    totals = payload["totals"]
    totals["fresh_targets"] = 0
    totals["recovery_targets"] = len(REVIEWED_COMPANIES)
    totals["statuses"] = dict(
        sorted(
            Counter(
                str(item["validation_status"]) for item in payload["results"]
            ).items()
        )
    )
    engine._write_bytes(
        REPORT,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return payload


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.build_only:
            reconcile()
            build_candidate_registry()
            engine._write_bytes(
                VALIDATED_REGISTRY, engine._registry_bytes(PRODUCTION, [])
            )
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            f"Original Master Recovery Batch H validation failed: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
