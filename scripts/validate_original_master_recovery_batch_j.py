"""Build and validate Original Master Recovery Batch J without changing production."""

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

from scripts import validate_original_master_recovery_batch_i as prior


engine = prior.engine
PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_original_master_recovery_batch_j_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_j_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_j_validated.yaml"
REPORT = ROOT / "output" / "original_master_recovery_batch_j_validation.json"
SAFE_TO_STAGE_FILES = [
    "config/company_original_master_recovery_batch_j_candidates.csv",
    "config/target_companies.original_master_recovery_batch_j_candidates.yaml",
    "config/target_companies.original_master_recovery_batch_j_validated.yaml",
    "scripts/validate_original_master_recovery_batch_j.py",
    "src/scraper/company_careers/adapters/workday.py",
    "src/scraper/company_careers/models.py",
    "tests/test_original_master_recovery_batch_j.py",
]

HISTORICAL_PRODUCTION_COUNT = 333
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 204
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Temu", "Uber Eats", "Flipkart", "Wayfair", "Rakuten", "SHEIN",
    "Target Tech", "Best Buy", "Costco Technology", "Ocado Technology",
    "X", "LinkedIn", "Twitch", "Telegram", "Threads",
]
RECOVERY_TARGETS = set(REVIEWED_COMPANIES)
MASTER_RANKS = {
    "Temu": 204, "Uber Eats": 208, "Flipkart": 209, "Wayfair": 211,
    "Rakuten": 212, "SHEIN": 213, "Target Tech": 214, "Best Buy": 215,
    "Costco Technology": 216, "Ocado Technology": 221, "X": 223,
    "LinkedIn": 226, "Twitch": 228, "Telegram": 230, "Threads": 233,
}

OFFICIAL_CAREERS_URLS = {
    "Temu": "https://www.temu.com/careers.html",
    "Uber Eats": "https://jobs.uber.com/en/jobs/",
    "Flipkart": "https://www.flipkartcareers.com/",
    "Wayfair": "https://www.wayfair.com/careers",
    "Rakuten": "https://global.rakuten.com/corp/careers/",
    "SHEIN": "https://careers.shein.com/",
    "Target Tech": "https://corporate.target.com/careers/career-areas/corporate/target-tech",
    "Best Buy": "https://jobs.bestbuy.com/",
    "Costco Technology": "https://careers.costco.com/?lang=en-US",
    "Ocado Technology": "https://careers.ocadogroup.com/teams/technology",
    "X": "https://careers.x.com/en",
    "LinkedIn": "https://careers.linkedin.com/",
    "Twitch": "https://careers.twitch.com/en/careers",
    "Telegram": "https://telegram.org/jobs",
    "Threads": "https://www.metacareers.com/jobs",
}

IDENTITY_LOCATION_EVIDENCE = {
    "Temu": "Temu's exact first-party careers URL returns a JavaScript challenge from a Temu CDN to bounded anonymous requests.",
    "Uber Eats": "Uber's current first-party Next.js search exposes a public search API, but the legacy UberEats team value returns zero and keyword matches have no exact child-team field.",
    "Flipkart": "Flipkart's first-party SSR page states a current opening count but cards omit complete titles, stable IDs, and job-specific detail links.",
    "Wayfair": "Wayfair's first-party client references a custom careers search endpoint, but the current public contract remains session-oriented and has no reviewed SSR collection.",
    "Rakuten": "Rakuten's global careers page delegates to its exact English Japan careers host, whose Radancy/TalentBrew HTML publishes stable job cards and IDs.",
    "SHEIN": "SHEIN's exact careers shell references a custom POST contract, but bounded direct requests do not return a proven public vacancy collection.",
    "Target Tech": "Target's exact first-party Target Tech page delegates to a Technology-filtered Target Workday route; Workday reports one exact facet ID whose count equals the filtered total.",
    "Best Buy": "Best Buy's legacy jobs route is now a 404 and the official alternate all-jobs route is a ServiceNow SPA without a reviewed anonymous static collection.",
    "Costco Technology": "Costco's new first-party Jibe/iCIMS API is public, but its advertised Information Technology category is absent from the API category facets and returns no attributable collection.",
    "Ocado Technology": "Ocado's exact technology page delegates to its parent Greenhouse board; current metadata has several technology-like families but no exact Ocado Technology child field.",
    "X": "X's former careers URL redirects to the merged xAI careers site. The xAI board has no exact X child-company field, so parent roles are not attributed to X.",
    "LinkedIn": "LinkedIn's first-party careers site delegates job actions to its LinkedIn company jobs page, which is session-oriented rather than a deterministic first-party job feed.",
    "Twitch": "Twitch's exact first-party careers page embeds the Greenhouse board slug twitch, proving current ownership and a public stable collection.",
    "Telegram": "Telegram's first-party jobs page exposes six static role descriptions but one shared bot apply link and undated or yearless deadlines rather than current job-specific records.",
    "Threads": "Threads remains part of Meta's shared careers collection; current rows expose no exact structured Threads child attribution.",
}

COMPATIBILITY = {
    name: (
        "Canada/US/remote and student compatibility is measured only from exact, "
        "currently attributable normalized rows; excluded parent or unstable collections are not inferred."
    )
    for name in REVIEWED_COMPANIES
}
COMPATIBILITY.update({
    "Rakuten": "The bounded exact Rakuten prefix is global; no current student or intern title is present.",
    "Target Tech": "The exact Technology facet contains US roles; no current student or intern title is present in the bounded prefix.",
    "Twitch": "The exact Twitch board contains US and remote roles; no current student or intern title is present in the bounded prefix.",
})
STUDENT_PROGRAM_EVIDENCE = {name: OFFICIAL_CAREERS_URLS[name] for name in REVIEWED_COMPANIES}


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
        "Rakuten",
        "static_html",
        OFFICIAL_CAREERS_URLS["Rakuten"],
        identity_regex=r"Rakuten|data-organization-ids",
        allowed_apply_hosts=["japan-job-en.rakuten.careers"],
        identity_url="https://japan-job-en.rakuten.careers/search-jobs/",
        listing_url="https://japan-job-en.rakuten.careers/search-jobs/",
        pagination_url_template="https://japan-job-en.rakuten.careers/search-jobs/?p={page}",
        endpoint_variant="server_rendered_cards",
        listing_selector="section#search-results",
        job_card_selector="section#search-results-list li",
        title_selector="a[data-job-id] h2",
        location_selector="span.job-location",
        department_selector="span.job-category",
        detail_link_selector="a[data-job-id]",
        external_id_regex=r"/32045/([0-9]+)$",
    ),
    _entry(
        "Target Tech",
        "workday",
        OFFICIAL_CAREERS_URLS["Target Tech"],
        identity_regex=r"Target|tenant:\s*\"target\"|siteId:\s*\"targetcareers\"",
        allowed_apply_hosts=["target.wd5.myworkdayjobs.com"],
        identity_url="https://target.wd5.myworkdayjobs.com/targetcareers",
        tenant="target",
        wd_subdomain="wd5",
        site_id="targetcareers",
        applied_facets={"jobFamilyGroup": ["daccab9f1d25018677ebcc363457460e"]},
    ),
    _entry(
        "Twitch",
        "greenhouse",
        OFFICIAL_CAREERS_URLS["Twitch"],
        identity_regex=r"Twitch|for=twitch",
        allowed_apply_hosts=["job-boards.greenhouse.io"],
        identity_url=OFFICIAL_CAREERS_URLS["Twitch"],
        slug="twitch",
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
    _excluded("Temu", "First-party JavaScript challenge", OFFICIAL_CAREERS_URLS["Temu"], "blocked", "The exact first-party careers URL returns a JavaScript challenge. No CAPTCHA, WAF, cookie, browser, or session bypass is attempted.", r"Temu|careers"),
    _excluded("Uber Eats", "Uber Next.js public search API", "https://jobs.uber.com/en/jobs/?team=UberEats", "ambiguous_attribution", "The former exact UberEats team filter now returns zero. Broad Uber Eats keyword results expose no exact structured child-team field, so parent Uber rows are not inferred.", r"Uber Eats|UberEats"),
    _excluded("Flipkart", "First-party SSR cards", OFFICIAL_CAREERS_URLS["Flipkart"], "unstable_contract", "The SSR page reports openings but its cards omit complete titles, stable job IDs, and job-specific detail links. The former TurboHire contract is not assumed current.", r"Flipkart"),
    _excluded("Wayfair", "Custom Avature-style client endpoint", OFFICIAL_CAREERS_URLS["Wayfair"], "session_required", "The current client references a custom job_search_data endpoint, but no reviewed bounded session-free SSR or public feed contract was found.", r"Wayfair"),
    _excluded("SHEIN", "Custom first-party POST application", OFFICIAL_CAREERS_URLS["SHEIN"], "unsupported_platform", "The careers shell references a custom POST API, but bounded anonymous requests do not yield a proven public collection. A singleton custom adapter is not production-safe.", r"SHEIN"),
    _excluded("Best Buy", "ServiceNow careers SPA", "https://careers.bestbuy.com/bby?id=all_jobs&spa=1", "session_required", "The legacy jobs URL is a 404 and the official alternate is a ServiceNow SPA without a reviewed anonymous stable collection. An unrelated three-row SmartRecruiters tenant is not inferred as canonical.", r"Best Buy"),
    _excluded("Costco Technology", "Jibe/iCIMS shared parent API", OFFICIAL_CAREERS_URLS["Costco Technology"], "ambiguous_attribution", "The public Costco parent API does not expose the advertised Information Technology value in its returned category facets, and the query returns no exact child-attributable collection.", r"Costco|Technology"),
    _excluded("Ocado Technology", "Greenhouse shared parent board", "https://boards-api.greenhouse.io/v1/boards/ocadogroup/jobs", "ambiguous_attribution", "Ocado Group jobs expose multiple technology-like job families but no exact Ocado Technology child field; no subset is inferred from descriptions or broad families.", r"Ocado Technology|Ocado Group"),
    _excluded("X", "Merged xAI Greenhouse parent", "https://x.ai/careers", "ambiguous_attribution", "The former X careers URL redirects to xAI. Current xAI rows have no exact structured X child-company field, and title mentions are insufficient for parent-child attribution.", r"X|xAI"),
    _excluded("LinkedIn", "LinkedIn company jobs page", OFFICIAL_CAREERS_URLS["LinkedIn"], "session_required", "First-party career actions delegate to LinkedIn's session-oriented company jobs page; no deterministic public first-party collection contract was found.", r"LinkedIn"),
    _excluded("Telegram", "First-party static role page", OFFICIAL_CAREERS_URLS["Telegram"], "stale_source", "Six static role descriptions share one jobs bot apply link and lack current stable posting IDs or dated currency; yearless deadlines cannot prove active vacancies.", r"Telegram"),
    _excluded("Threads", "Meta shared parent collection", OFFICIAL_CAREERS_URLS["Threads"], "blocked", "The shared Meta endpoint is currently rate-limited and, independently, exposes no exact structured Threads child field. No bypass or title-only attribution is attempted.", r"Threads|Meta"),
]

PLATFORM_NOTES = {
    "Temu": ("First-party JavaScript challenge", "none"),
    "Uber Eats": ("Uber Next.js public search API", "none"),
    "Flipkart": ("First-party SSR cards", "none"),
    "Wayfair": ("Custom Avature-style client endpoint", "none"),
    "Rakuten": ("Radancy/TalentBrew server-rendered HTML", "static_html"),
    "SHEIN": ("Custom first-party POST application", "none"),
    "Target Tech": ("Workday exact facet", "workday"),
    "Best Buy": ("ServiceNow careers SPA", "none"),
    "Costco Technology": ("Jibe/iCIMS shared parent API", "none"),
    "Ocado Technology": ("Greenhouse shared parent board", "none"),
    "X": ("Merged xAI Greenhouse parent", "none"),
    "LinkedIn": ("LinkedIn company jobs page", "none"),
    "Twitch": ("Greenhouse", "greenhouse"),
    "Telegram": ("First-party static role page", "none"),
    "Threads": ("Meta shared parent collection", "none"),
}

ADAPTER_OPPORTUNITIES = [
    {
        "platform": "Workday exact facet",
        "companies": ["Target Tech"],
        "reusable_adapter_justified": True,
        "recommendation": "Extended the existing Workday adapter for one validated opaque facet value, with no unfiltered fallback and response count corroboration.",
    },
    {
        "platform": "Radancy/TalentBrew server-rendered HTML",
        "companies": ["Rakuten"],
        "reusable_adapter_justified": True,
        "recommendation": "Reuse the existing bounded static_html adapter; no platform-specific code is needed.",
    },
    {
        "platform": "ServiceNow careers",
        "companies": ["Best Buy"],
        "reusable_adapter_justified": "research justified; implementation not yet justified",
        "recommendation": "Prove a stable anonymous multi-tenant job collection before implementing a generic adapter.",
    },
    {
        "platform": "Custom first-party POST APIs",
        "companies": ["SHEIN"],
        "reusable_adapter_justified": False,
        "recommendation": "Do not add a singleton adapter until a stable public contract and a second compatible tenant are proven.",
    },
    {
        "platform": "Shared parent collections",
        "companies": ["Uber Eats", "Costco Technology", "Ocado Technology", "X", "Threads"],
        "reusable_adapter_justified": False,
        "recommendation": "Do not add mappings or filtered sources without an exact structured child field on every normalized row.",
    },
]

LOCATION_RE = re.compile(
    r"\b(?:Canada|Remote|United States|Seattle|Bellevue|Minneapolis|Brooklyn|San Francisco)\b|"
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
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS,
        "MASTER_WORKBOOK": MASTER_WORKBOOK, "CANDIDATE_CSV": CANDIDATE_CSV,
        "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY, "VALIDATED_REGISTRY": VALIDATED_REGISTRY,
        "REPORT": REPORT, "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT, "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS,
        "MAX_PAGES": MAX_PAGES, "MAX_JOBS": MAX_JOBS,
        "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES,
        "REVIEWED_COMPANIES": REVIEWED_COMPANIES, "MASTER_RANKS": MASTER_RANKS,
        "RECOVERY_TARGETS": RECOVERY_TARGETS, "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS,
        "IDENTITY_LOCATION_EVIDENCE": IDENTITY_LOCATION_EVIDENCE,
        "COMPATIBILITY": COMPATIBILITY, "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES, "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES, "ADAPTER_OPPORTUNITIES": ADAPTER_OPPORTUNITIES,
        "LOCATION_RE": LOCATION_RE, "CSV_FIELDS": CSV_FIELDS,
    }
    for name, value in values.items():
        setattr(prior, name, value)


@contextmanager
def _isolated_prior_configuration() -> Iterator[None]:
    modules = (prior, prior.prior, prior.prior.prior, prior.prior.prior.prior, engine)
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
    payload["candidate_registry_has_exact_333_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_330_entry_prefix"
    )
    payload["validated_registry_has_exact_333_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_330_entry_prefix"
    )
    payload["adapter_opportunities"] = ADAPTER_OPPORTUNITIES
    payload["safe_to_stage_files"] = SAFE_TO_STAGE_FILES
    totals = payload["totals"]
    totals["fresh_targets"] = 0
    totals["recovery_targets"] = len(REVIEWED_COMPANIES)
    totals["statuses"] = dict(
        sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items())
    )
    engine._write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
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
        print(f"Original Master Recovery Batch J validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
