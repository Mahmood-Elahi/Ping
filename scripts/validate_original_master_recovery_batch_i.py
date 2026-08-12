"""Build and validate Original Master Recovery Batch I without changing production."""

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

from scripts import validate_original_master_recovery_batch_h as prior


engine = prior.engine
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_original_master_recovery_batch_i_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_i_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_i_validated.yaml"
REPORT = ROOT / "output" / "original_master_recovery_batch_i_validation.json"
SAFE_TO_STAGE_FILES = [
    "config/company_original_master_recovery_batch_i_candidates.csv",
    "config/target_companies.original_master_recovery_batch_i_candidates.yaml",
    "config/target_companies.original_master_recovery_batch_i_validated.yaml",
    "scripts/validate_original_master_recovery_batch_i.py",
    "src/scraper/company_careers/adapters/embedded_data.py",
    "tests/test_original_master_recovery_batch_i.py",
]

HISTORICAL_PRODUCTION_COUNT = 330
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 201
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 1
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "BYD", "CATL", "Epic Games", "Niantic", "HTC Vive", "Varjo", "Canon",
    "DJI", "Baker Hughes", "Sarcos Robotics", "Clearpath Robotics",
    "OTTO Motors", "OHB SE", "Alibaba", "Walmart Global Tech",
]
RECOVERY_TARGETS = set(REVIEWED_COMPANIES)
MASTER_RANKS = {
    "BYD": 133, "CATL": 134, "Epic Games": 136, "Niantic": 141,
    "HTC Vive": 142, "Varjo": 147, "Canon": 149, "DJI": 150,
    "Baker Hughes": 159, "Sarcos Robotics": 165,
    "Clearpath Robotics": 166, "OTTO Motors": 167, "OHB SE": 197,
    "Alibaba": 198, "Walmart Global Tech": 201,
}

OFFICIAL_CAREERS_URLS = {
    "BYD": "https://www.bydglobal.com/en/Society.html",
    "CATL": "https://talent.catl.com/",
    "Epic Games": "https://www.epicgames.com/site/en-US/careers/jobs",
    "Niantic": "https://nianticlabs.com/careers",
    "HTC Vive": "https://careers.htc.com/",
    "Varjo": "https://varjo.com/jobs/",
    "Canon": "https://global.canon/en/employ/",
    "DJI": "https://careers.dji.com/zh-CN/",
    "Baker Hughes": "https://careers.bakerhughes.com/global/en",
    "Sarcos Robotics": "https://www.sarcos.com/careers/",
    "Clearpath Robotics": "https://clearpathrobotics.com/careers/",
    "OTTO Motors": "https://ottomotors.com/careers/",
    "OHB SE": "https://www.ohb.de/en/career",
    "Alibaba": "https://www.alibabagroup.com/en-US/careers",
    "Walmart Global Tech": "https://tech.walmart.com/content/walmart-global-tech/en_us/careers.html",
}

IDENTITY_LOCATION_EVIDENCE = {
    "BYD": "BYD's exact global recruiting page publishes first-party Oracle WebCenter Sites REST URLs, but the reviewed collections repeatedly time out within the bounded anonymous window.",
    "CATL": "CATL's first-party talent site delegates to exact Mokahr organization catlhr and site 98098; every server bootstrap row repeats orgId=catlhr.",
    "Epic Games": "Epic's exact first-party job search remains protected by an HTTP 403 response to bounded anonymous requests.",
    "Niantic": "Niantic's careers URL now redirects to Scopely's mixed parent portal; no exact independently attributable Niantic collection is exposed.",
    "HTC Vive": "HTC's exact careers site links the globalcareers-htcvive iCIMS tenant; its public search page proves a current zero-job collection.",
    "Varjo": "Varjo's exact jobs page is public but currently exposes employee stories rather than a stable vacancy collection or job IDs.",
    "Canon": "Canon's global employment URL now redirects to the corporate home; available career links are regional and are not inferred as one canonical global source.",
    "DJI": "DJI's first-party careers site delegates to exact Mokahr organization dji and site 170070; every server bootstrap row repeats orgId=dji.",
    "Baker Hughes": "The exact Baker Hughes Phenom search returns an Incapsula unsuccessful-request shell instead of a public collection.",
    "Sarcos Robotics": "Sarcos's careers URL redirects to Palladyne AI after the corporate rename; current Palladyne roles cannot be attributed back to the legacy Sarcos master identity.",
    "Clearpath Robotics": "Clearpath's official page delegates to Rockwell Automation Workday, where exact Clearpath searches return zero and rows lack an exact structured child-company field.",
    "OTTO Motors": "OTTO's official page delegates to Rockwell Automation Workday; matching descriptions exist, but structured hiring organizations are Rockwell entities rather than exact OTTO Motors attribution.",
    "OHB SE": "OHB's exact corporate page links Cornerstone company code career-ohb and careersite 4, but the current collection depends on an anonymous player-token bootstrap not covered by a reviewed adapter.",
    "Alibaba": "Alibaba's exact group careers page delegates to talent.alibaba.com, whose custom client application exposes no reviewed bounded static collection contract.",
    "Walmart Global Tech": "Walmart Global Tech's official page delegates to a shared Walmart parent collection; a technology filter does not prove exact child attribution on every row.",
}

COMPATIBILITY = {
    "BYD": "Global and campus/social recruiting are supported in principle; the timed-out feeds prevent current safe normalization.",
    "CATL": "The current bounded social prefix is China-focused and has no English student-title hit.",
    "Epic Games": "Canada/US and internship compatible in principle; access control prevents current normalization.",
    "Niantic": "Scopely has Canada/US roles, but those cannot be attributed to Niantic without an exact child field.",
    "HTC Vive": "The exact global iCIMS collection currently proves zero jobs and therefore zero student hits.",
    "Varjo": "Global and student compatibility cannot be measured without a current job collection.",
    "Canon": "Regional careers and student programs exist, but no canonical global job collection is safely attributable.",
    "DJI": "The current bounded social prefix is China-focused and has no English student-title hit.",
    "Baker Hughes": "Canada/US and student roles are plausible, but the protected response prevents current normalization.",
    "Sarcos Robotics": "Palladyne roles are not counted for the legacy Sarcos identity.",
    "Clearpath Robotics": "Canada-compatible in principle; the parent collection has no current exact child-attributed rows.",
    "OTTO Motors": "Canada/US robotics matches exist in the parent collection, but descriptions alone do not satisfy exact structured attribution.",
    "OHB SE": "European student programs are official; the tokenized collection is not normalized.",
    "Alibaba": "Global and student compatibility cannot be safely measured from the unreviewed custom contract.",
    "Walmart Global Tech": "Canada/US and student compatible at Walmart parent level, but not exact-child attributable.",
}

STUDENT_PROGRAM_EVIDENCE = {
    name: OFFICIAL_CAREERS_URLS[name] for name in REVIEWED_COMPANIES
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
        "CATL",
        "javascript_embedded_data",
        OFFICIAL_CAREERS_URLS["CATL"],
        identity_regex=r"catlhr|CATL|宁德时代",
        allowed_apply_hosts=["talent.catl.com"],
        identity_url="https://talent.catl.com/social-recruitment/catlhr/98098",
        listing_url="https://talent.catl.com/social-recruitment/catlhr/98098",
        endpoint_variant="html_attribute_json",
        embedded_script_selector="input#init-data",
        reference_url_attribute="value",
        embedded_json_path="jobs",
        id_field="id",
        title_field="title",
        location_fields=["location.address", "location.country"],
        url_field="id",
        date_field="updatedAt",
        department_field="department.name",
        job_attribution_field="orgId",
        job_attribution_regex=r"^catlhr$",
        allowed_source_hosts=["talent.catl.com"],
        apply_url_template="https://talent.catl.com/social-recruitment/catlhr/98098?hash=%23%2Fjob%2F{id}",
        page_size=15,
    ),
    _entry(
        "HTC Vive",
        "icims",
        OFFICIAL_CAREERS_URLS["HTC Vive"],
        identity_regex=r"HTC(?: Europe| United States)?|HTC VIVE",
        allowed_apply_hosts=["globalcareers-htcvive.icims.com"],
        identity_url="https://globalcareers-htcvive.icims.com/jobs/search",
        search_url="https://globalcareers-htcvive.icims.com/jobs/search",
        endpoint_variant="icims_legacy_portal_html",
        tenant="globalcareers-htcvive",
        board_id="htcvive-global-public",
        page_size=20,
    ),
    _entry(
        "DJI",
        "javascript_embedded_data",
        OFFICIAL_CAREERS_URLS["DJI"],
        identity_regex=r"DJI|大疆|\"orgId\":\"dji\"",
        allowed_apply_hosts=["apply.careers.dji.com"],
        identity_url="https://apply.careers.dji.com/social-recruitment/dji/170070?locale=zh-CN",
        listing_url="https://apply.careers.dji.com/social-recruitment/dji/170070?locale=zh-CN",
        endpoint_variant="html_attribute_json",
        embedded_script_selector="input#init-data",
        reference_url_attribute="value",
        embedded_json_path="jobs",
        id_field="id",
        title_field="title",
        location_fields=["location.address", "location.country"],
        url_field="id",
        date_field="updatedAt",
        department_field="department.name",
        job_attribution_field="orgId",
        job_attribution_regex=r"^dji$",
        allowed_source_hosts=["apply.careers.dji.com"],
        apply_url_template="https://apply.careers.dji.com/social-recruitment/dji/170070?locale=zh-CN&hash=%23%2Fjob%2F{id}",
        page_size=15,
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
    _excluded("BYD", "Oracle WebCenter Sites REST", OFFICIAL_CAREERS_URLS["BYD"], "network_inaccessible", "The exact first-party social and campus REST collections repeatedly exceed the 30-second bounded timeout. No session, browser, or alternate-network workaround is attempted.", r"BYD|Recruitment"),
    _excluded("Epic Games", "Custom first-party / HTTP 403", OFFICIAL_CAREERS_URLS["Epic Games"], "blocked", "The exact Epic job search returns HTTP 403. No WAF, cookie, browser, or session bypass is attempted.", r"Epic Games|Access Denied"),
    _excluded("Niantic", "Scopely Phenom shared parent", "https://careers.scopely.com/us/en", "stale_source", "Niantic's former careers URL redirects to Scopely's shared portal, which exposes no exact structured Niantic collection or row attribution. Parent roles are not inferred as Niantic roles.", r"Scopely|Niantic"),
    _excluded("Varjo", "First-party content page", OFFICIAL_CAREERS_URLS["Varjo"], "stale_source", "The public page currently exposes employee stories but no stable vacancy collection, job IDs, or job-specific apply URLs.", r"Varjo|Jobs"),
    _excluded("Canon", "Regional careers directory", OFFICIAL_CAREERS_URLS["Canon"], "stale_source", "The global employment URL redirects to Canon's corporate home and no stable global collection delegation was found. Regional sources are not merged into the global master identity.", r"Canon"),
    _excluded("Baker Hughes", "Phenom / Incapsula", "https://careers.bakerhughes.com/global/en/search-results", "blocked", "The exact Phenom search returns an Incapsula unsuccessful-request shell. No WAF, cookie, browser, or session bypass is attempted.", r"Baker Hughes|Incapsula"),
    _excluded("Sarcos Robotics", "Palladyne AI rename", OFFICIAL_CAREERS_URLS["Sarcos Robotics"], "stale_source", "The legacy careers URL redirects to Palladyne AI. Without an exact structured Sarcos child contract, Palladyne roles are not attributed back to the legacy master identity.", r"Sarcos|Palladyne"),
    _excluded("Clearpath Robotics", "Rockwell Automation Workday", "https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation", "ambiguous_attribution", "The official delegation is a shared Rockwell Workday board. Exact Clearpath searches return zero, and the contract exposes no exact structured child-company field.", r"Clearpath|Rockwell Automation"),
    _excluded("OTTO Motors", "Rockwell Automation Workday", "https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation", "ambiguous_attribution", "Two parent-board search matches mention OTTO in descriptions, but structured hiringOrganization values are Rockwell entities. Description text does not satisfy approved exact parent-child attribution.", r"OTTO|Rockwell Automation"),
    _excluded("OHB SE", "Cornerstone CSOD", "https://career-ohb.csod.com/ux/ats/careersite/4/home?c=career-ohb&lang=en-GB", "browser_required", "The exact public shell identifies career-ohb and careersite 4, but job search depends on an anonymous player-token bootstrap. No singleton token/session adapter is added.", r"OHB|career-ohb"),
    _excluded("Alibaba", "Custom first-party client application", "https://talent.alibaba.com/en/home?lang=en", "browser_required", "The exact talent application exposes no reviewed deterministic static bootstrap or public feed. No one-off client-bundle adapter is added.", r"Alibaba"),
    _excluded("Walmart Global Tech", "Walmart shared parent collection", OFFICIAL_CAREERS_URLS["Walmart Global Tech"], "ambiguous_attribution", "The technology-filtered Walmart collection does not provide exact Walmart Global Tech structured attribution for every row. No parent mapping is added.", r"Walmart Global Tech|Walmart"),
]

PLATFORM_NOTES = {
    "BYD": ("Oracle WebCenter Sites REST / timeout", "none"),
    "CATL": ("Mokahr server bootstrap", "javascript_embedded_data"),
    "Epic Games": ("Custom first-party / HTTP 403", "none"),
    "Niantic": ("Scopely Phenom shared parent", "none"),
    "HTC Vive": ("iCIMS legacy portal HTML", "icims"),
    "Varjo": ("First-party content page", "none"),
    "Canon": ("Regional careers directory", "none"),
    "DJI": ("Mokahr server bootstrap", "javascript_embedded_data"),
    "Baker Hughes": ("Phenom / Incapsula", "none"),
    "Sarcos Robotics": ("Palladyne AI rename", "none"),
    "Clearpath Robotics": ("Rockwell Automation Workday shared parent", "none"),
    "OTTO Motors": ("Rockwell Automation Workday shared parent", "none"),
    "OHB SE": ("Cornerstone CSOD token bootstrap", "none"),
    "Alibaba": ("Custom first-party client application", "none"),
    "Walmart Global Tech": ("Walmart shared parent collection", "none"),
}

ADAPTER_OPPORTUNITIES = [
    {
        "platform": "Mokahr server bootstrap",
        "companies": ["CATL", "DJI"],
        "reusable_adapter_justified": True,
        "recommendation": "Extended the existing bounded embedded-data adapter for one explicit HTML attribute, with exact selector cardinality, byte limits, source hosts, stable IDs, and per-row organization attribution.",
    },
    {
        "platform": "Cornerstone CSOD",
        "companies": ["OHB SE"],
        "reusable_adapter_justified": "research justified; implementation not yet justified",
        "recommendation": "Prove the anonymous player-token and rec-job-search contract across multiple tenants before implementing a generic adapter; do not model it as a session bypass.",
    },
    {
        "platform": "Oracle WebCenter Sites REST",
        "companies": ["BYD"],
        "reusable_adapter_justified": False,
        "recommendation": "Retest the exact first-party feeds when bounded access is reliable and seek a second tenant before adding an adapter.",
    },
]

LOCATION_RE = re.compile(
    r"\b(?:Canada|Remote|United States|Dallas|Waterloo)\b|"
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
    modules = (prior, prior.prior, prior.prior.prior, engine)
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
    payload["candidate_registry_has_exact_330_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_325_entry_prefix"
    )
    payload["validated_registry_has_exact_330_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_325_entry_prefix"
    )
    payload["adapter_opportunities"] = ADAPTER_OPPORTUNITIES
    payload["safe_to_stage_files"] = SAFE_TO_STAGE_FILES
    totals = payload["totals"]
    totals["fresh_targets"] = 0
    totals["recovery_targets"] = len(REVIEWED_COMPANIES)
    totals["statuses"] = dict(
        sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items())
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
            engine._write_bytes(VALIDATED_REGISTRY, engine._registry_bytes(PRODUCTION, []))
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Original Master Recovery Batch I validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
