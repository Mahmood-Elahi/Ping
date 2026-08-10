"""Build and validate Requested Expansion v3 Batch F without changing production."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_requested_expansion_v3_batch_e as prior
from scripts import validate_calgary_student_employers_v1_batch_a as engine
from src.scraper.company_careers import scanner


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_requested_expansion_v3_batch_f_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_f_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.requested_expansion_v3_batch_f_validated.yaml"
REPORT = ROOT / "output" / "requested_expansion_v3_batch_f_validation.json"

HISTORICAL_PRODUCTION_COUNT = 249
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 186
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "Covariant", "Mistral AI", "Perplexity AI", "Espressif Systems", "iRobot",
    "Dyson", "SharkNinja", "Tesla", "Netflix", "Bosch", "Continental", "Aptiv",
    "STMicroelectronics", "Baker Hughes", "Hugging Face",
]
RECOVERY_TARGETS = {
    "Tesla", "Netflix", "Bosch", "Continental", "Aptiv", "STMicroelectronics",
    "Baker Hughes", "Hugging Face",
}
MASTER_RANKS = {
    "Tesla": 6, "STMicroelectronics": 34, "Netflix": 48, "Bosch": 96,
    "Continental": 124, "Aptiv": 126, "Baker Hughes": 159,
}

OFFICIAL_CAREERS_URLS = {
    "Covariant": "https://covariant.ai/careers/",
    "Mistral AI": "https://mistral.ai/careers/",
    "Perplexity AI": "https://www.perplexity.ai/careers",
    "Espressif Systems": "https://www.espressif.com/en/careers",
    "iRobot": "https://careers.irobot.com/",
    "Dyson": "https://careers.dyson.com/en-gb/",
    "SharkNinja": "https://careers.sharkninja.com/",
    "Tesla": "https://www.tesla.com/careers/search",
    "Netflix": "https://jobs.netflix.com/",
    "Bosch": "https://jobs.bosch.com/en/",
    "Continental": "https://jobs.continental.com/en/",
    "Aptiv": "https://www.aptiv.com/en/jobs",
    "STMicroelectronics": "https://stmicroelectronics.eightfold.ai/careers",
    "Baker Hughes": "https://careers.bakerhughes.com/global/en/search-results",
    "Hugging Face": "https://apply.workable.com/huggingface/",
}

IDENTITY_LOCATION_EVIDENCE = {
    "Covariant": "Covariant remains a separate Bay Area company after Amazon's non-exclusive technology licence and transfer of some staff; Amazon did not acquire Covariant. Its former careers route now returns 404.",
    "Mistral AI": "The exact Mistral corporate careers page links the mistral.ai Ashby board; the board has US and global offices.",
    "Perplexity AI": "The exact Perplexity Ashby board is the company's current job collection and exposes US and global roles.",
    "Espressif Systems": "The exact Espressif Systems global site identifies the Shanghai-headquartered public semiconductor company and publishes its own job collection.",
    "iRobot": "The exact iRobot board remains live after Picea acquired 100% of iRobot in January 2026; iRobot is now a private, Boston-based Picea-owned company and is not attributed to Picea's mixed jobs.",
    "Dyson": "The exact global Dyson careers site publishes Dyson-only roles and identifies Canada, the United States, and global locations.",
    "SharkNinja": "The exact global SharkNinja careers site is attributed to SharkNinja, Inc. and exposes Canada, US, and global filters.",
    "Tesla": "The exact Tesla careers search remains protected by Akamai and returns HTTP 403 to bounded anonymous validation.",
    "Netflix": "The exact Netflix board remains an Eightfold collection with reCAPTCHA in its public shell.",
    "Bosch": "The exact Bosch global site now delegates country collections to the BoschGroup SmartRecruiters tenant.",
    "Continental": "The exact Continental global jobs site loads its collection through a Continental custom REST configuration contract.",
    "Aptiv": "The exact Aptiv corporate jobs page now embeds an Aptiv-specific HawkSearch v2 contract; the retired jobs.aptiv.com host is not used.",
    "STMicroelectronics": "The exact STMicroelectronics Eightfold board remains a reCAPTCHA-advertising source.",
    "Baker Hughes": "The exact Baker Hughes Phenom search still returns an Incapsula interstitial to anonymous validation.",
    "Hugging Face": "The exact Hugging Face Workable board remains protected by Turnstile; no shared or third-party board is substituted.",
}

COMPATIBILITY = {
    "Covariant": "Bay Area/US relevant historically, but no current official collection can establish student or remote compatibility.",
    "Mistral AI": "US/global and internship/new-graduate compatible; current normalized roles are checked for US and student titles.",
    "Perplexity AI": "US/global and internship compatible; current normalized roles include US offices and an internship title.",
    "Espressif Systems": "Global, remote-filter, graduate, and internship compatible; the current normalized internship is in Singapore, not Canada/US/Remote.",
    "iRobot": "US/global and student-program compatible; the current normalized sample includes Bedford, Massachusetts roles.",
    "Dyson": "Canada/US/global and early-career compatible; the current bounded first page has no Canada/US/Remote student title.",
    "SharkNinja": "Canada/US/global and early-talent compatible; the bounded sample includes US roles and a later early-talent title.",
    "Tesla": "Canada/US/Remote and internship compatible in principle; safe current normalization remains blocked.",
    "Netflix": "US/Remote and internship compatible in principle; safe current normalization remains blocked.",
    "Bosch": "Canada/US and student compatible in principle; the current SmartRecruiters contract has no registered adapter.",
    "Continental": "Canada/US/global and student compatible in principle; the custom REST contract has no reviewed adapter.",
    "Aptiv": "Canada/US/global and student compatible in principle; the HawkSearch contract has no reviewed adapter.",
    "STMicroelectronics": "Canada/US/global and student compatible in principle; safe current normalization remains blocked.",
    "Baker Hughes": "Canada/US/global and internship compatible in principle; safe current normalization remains blocked.",
    "Hugging Face": "Canada/US/Remote and internship compatible in principle; safe current normalization remains blocked.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "Covariant": OFFICIAL_CAREERS_URLS["Covariant"],
    "Mistral AI": "https://jobs.ashbyhq.com/mistral.ai",
    "Perplexity AI": "https://jobs.ashbyhq.com/perplexity",
    "Espressif Systems": "https://www.espressif.com/en/join-us/campus-recruitment",
    "iRobot": "https://careers.irobot.com/students",
    "Dyson": "https://careers.dyson.com/en-gb/what-you-can-do/early-careers/",
    "SharkNinja": "https://careers.sharkninja.com/en/earlytalent",
    "Tesla": "https://www.tesla.com/careers/search/?type=3",
    "Netflix": "https://jobs.netflix.com/careers/internships",
    "Bosch": "https://jobs.bosch.com/en/students/",
    "Continental": "https://www.continental.com/en/career/students/",
    "Aptiv": OFFICIAL_CAREERS_URLS["Aptiv"],
    "STMicroelectronics": OFFICIAL_CAREERS_URLS["STMicroelectronics"],
    "Baker Hughes": "https://careers.bakerhughes.com/global/en/early-careers-internships",
    "Hugging Face": OFFICIAL_CAREERS_URLS["Hugging Face"],
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex, "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS.get(name), "max_pages": 1,
        "max_jobs": MAX_JOBS, "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES = [
    _entry("Mistral AI", "ashby", OFFICIAL_CAREERS_URLS["Mistral AI"],
           identity_regex=r"Mistral", allowed_apply_hosts=["jobs.ashbyhq.com"],
           identity_url=OFFICIAL_CAREERS_URLS["Mistral AI"], slug="mistral.ai"),
    _entry("Perplexity AI", "ashby", OFFICIAL_CAREERS_URLS["Perplexity AI"],
           identity_regex=r"Perplexity", allowed_apply_hosts=["jobs.ashbyhq.com"],
           identity_url="https://jobs.ashbyhq.com/perplexity", slug="perplexity"),
    _entry("Espressif Systems", "static_html", OFFICIAL_CAREERS_URLS["Espressif Systems"],
           identity_regex=r"Espressif Systems|Job Search", allowed_apply_hosts=["www.espressif.com"],
           identity_url="https://www.espressif.com/en/join-us/job-search",
           listing_url="https://www.espressif.com/en/join-us/job-search",
           endpoint_variant="server_rendered_cards", listing_selector=".view-job-search",
           job_card_selector=".item-wrap", title_selector=".item-title",
           location_selector=".item-location", department_selector=".item-function",
           detail_link_selector=".item-share", reference_url_attribute="data-share",
           external_id_regex=r"/job-detail/([^/?#]+)$"),
    _entry("iRobot", "static_html", OFFICIAL_CAREERS_URLS["iRobot"],
           identity_regex=r"iRobot", allowed_apply_hosts=["careers.irobot.com"],
           identity_url="https://careers.irobot.com/search-jobs",
           listing_url="https://careers.irobot.com/search-jobs",
           pagination_url_template="https://careers.irobot.com/search-jobs?p={page}", max_pages=2,
           endpoint_variant="server_rendered_cards", listing_selector="#search-results-list",
           job_card_selector="#search-results-list li", title_selector="h2",
           location_selector=".job-location", detail_link_selector="a[data-job-id]",
           external_id_regex=r"/42682/([0-9]+)$"),
    _entry("Dyson", "static_html", OFFICIAL_CAREERS_URLS["Dyson"],
           identity_regex=r"Dyson Careers|Dyson", allowed_apply_hosts=["careers.dyson.com"],
           identity_url="https://careers.dyson.com/en-gb/search-results/",
           listing_url="https://careers.dyson.com/en-gb/search-results/",
           endpoint_variant="server_rendered_cards", listing_selector="body",
           job_card_selector="article.m-job-preview", title_selector=".t-job-preview-title",
           location_selector=".t-job-preview-market", department_selector=".m-job-preview__team",
           detail_link_selector=".t-job-preview-faux-link", external_id_regex=r"/(JR[0-9]+)/?$"),
    _entry("SharkNinja", "static_html", OFFICIAL_CAREERS_URLS["SharkNinja"],
           identity_regex=r"SharkNinja", allowed_apply_hosts=["careers.sharkninja.com"],
           identity_url="https://careers.sharkninja.com/search-jobs",
           listing_url="https://careers.sharkninja.com/search-jobs",
           pagination_url_template="https://careers.sharkninja.com/search-jobs?p={page}", max_pages=2,
           endpoint_variant="server_rendered_cards", listing_selector="#search-results-list",
           job_card_selector="#search-results-list li", title_selector="h2",
           location_selector=".job-location", date_selector=".job-date-posted",
           detail_link_selector="a[data-job-id]", external_id_regex=r"/47204/([0-9]+)$"),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {"company": company, "platform": platform, "adapter": "none", "listing_url": listing_url,
            "classification": classification, "exclusion_reason": reason, "identity_regex": identity_regex}


EXCLUDED_REVIEWS = [
    _excluded("Covariant", "Netlify/static", OFFICIAL_CAREERS_URLS["Covariant"], "no_current_official_job_collection",
              "Amazon's August 2024 agreement was a non-exclusive licence and staff transfer, not an acquisition. Covariant remains a separate company, but its current careers route returns 404 and exposes no stable official job collection.", r"Covariant|Page not found"),
    _excluded("Tesla", "Custom Tesla / Akamai", OFFICIAL_CAREERS_URLS["Tesla"], "blocked",
              "The exact Tesla careers search still returns Akamai HTTP 403. No anti-bot, cookie, session, or browser bypass is attempted.", r"Tesla|Access Denied"),
    _excluded("Netflix", "Eightfold", "https://explore.jobs.netflix.net/careers", "blocked",
              "Netflix's exact Eightfold board still advertises reCAPTCHA and its anonymous PCSX collection does not satisfy the normal safe contract. No CAPTCHA or session safeguard is weakened.", r"Netflix|Careers"),
    _excluded("Bosch", "SmartRecruiters", OFFICIAL_CAREERS_URLS["Bosch"], "unsupported_platform",
              "Bosch now links exact BoschGroup country boards on SmartRecruiters, but no SmartRecruiters adapter is registered and this batch does not normalize an unreviewed contract.", r"Bosch|Careers at Bosch"),
    _excluded("Continental", "Custom REST", OFFICIAL_CAREERS_URLS["Continental"], "unsupported_platform",
              "Continental's exact site exposes a custom /api/configuration/pagetype-jobs contract, but no reviewed custom REST adapter is registered.", r"Continental|Jobs"),
    _excluded("Aptiv", "HawkSearch v2", OFFICIAL_CAREERS_URLS["Aptiv"], "unsupported_platform",
              "Aptiv's current official /en/jobs page exposes an Aptiv-specific HawkSearch v2 endpoint, but no reviewed HawkSearch adapter is registered; the retired jobs.aptiv.com host is not reused.", r"Aptiv|Jobs"),
    _excluded("STMicroelectronics", "Eightfold", OFFICIAL_CAREERS_URLS["STMicroelectronics"], "blocked",
              "The exact STMicroelectronics Eightfold shell still advertises reCAPTCHA, so the prior CAPTCHA recovery is not promoted and no token or session bypass is attempted.", r"STMicroelectronics|Careers"),
    _excluded("Baker Hughes", "Phenom / Incapsula", OFFICIAL_CAREERS_URLS["Baker Hughes"], "blocked",
              "The exact Baker Hughes search still returns an Incapsula interstitial to bounded anonymous requests. No WAF or session bypass is attempted.", r"Baker Hughes|Incapsula"),
    _excluded("Hugging Face", "Workable / Turnstile", OFFICIAL_CAREERS_URLS["Hugging Face"], "blocked",
              "The exact Hugging Face Workable board still loads Turnstile/reCAPTCHA dependencies. It is not promoted and no CAPTCHA safeguard is weakened.", r"Hugging Face|Current Openings"),
]

PLATFORM_NOTES = {
    "Covariant": ("Netlify/static", "none"), "Mistral AI": ("Ashby", "ashby"),
    "Perplexity AI": ("Ashby", "ashby"), "Espressif Systems": ("Drupal/static HTML", "static_html"),
    "iRobot": ("Radancy TalentBrew", "static_html"), "Dyson": ("Umbraco/server-rendered", "static_html"),
    "SharkNinja": ("Radancy TalentBrew", "static_html"), "Tesla": ("Custom Tesla / Akamai", "none"),
    "Netflix": ("Eightfold / reCAPTCHA", "none"), "Bosch": ("SmartRecruiters", "none"),
    "Continental": ("Custom REST", "none"), "Aptiv": ("HawkSearch v2", "none"),
    "STMicroelectronics": ("Eightfold / reCAPTCHA", "none"),
    "Baker Hughes": ("Phenom / Incapsula", "none"),
    "Hugging Face": ("Workable / Turnstile", "none"),
}

LOCATION_RE = re.compile(
    r"\b(?:Canada|Canadian|Remote|United States|USA|Alberta|British Columbia|Ontario|Quebec|"
    r"Calgary|Edmonton|Vancouver|Toronto|Ottawa|Montreal|Mississauga|San Francisco|New York(?: City)?|"
    r"Palo Alto|Bedford|Needham|Chicago|Austin|Seattle|Boston|Los Angeles)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|"
    r"ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
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
        "PRODUCTION": PRODUCTION, "PARENT_MAPPINGS": PARENT_MAPPINGS, "MASTER_WORKBOOK": MASTER_WORKBOOK,
        "CANDIDATE_CSV": CANDIDATE_CSV, "CANDIDATE_REGISTRY": CANDIDATE_REGISTRY,
        "VALIDATED_REGISTRY": VALIDATED_REGISTRY, "REPORT": REPORT,
        "HISTORICAL_PRODUCTION_COUNT": HISTORICAL_PRODUCTION_COUNT,
        "HISTORICAL_PARENT_MAPPINGS": HISTORICAL_PARENT_MAPPINGS,
        "HISTORICAL_ORIGINAL_COVERAGE": HISTORICAL_ORIGINAL_COVERAGE,
        "MASTER_COMPANY_COUNT": MASTER_COMPANY_COUNT, "MAX_TIMEOUT_SECONDS": MAX_TIMEOUT_SECONDS,
        "MAX_PAGES": MAX_PAGES, "MAX_JOBS": MAX_JOBS, "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES,
        "REVIEWED_COMPANIES": REVIEWED_COMPANIES, "MASTER_RANKS": MASTER_RANKS,
        "RECOVERY_TARGETS": RECOVERY_TARGETS, "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS,
        "CANADA_RELEVANCE": IDENTITY_LOCATION_EVIDENCE, "COMPATIBILITY": COMPATIBILITY,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE, "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES,
        "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS, "PLATFORM_NOTES": PLATFORM_NOTES,
        "CANADIAN_LOCATION_RE": LOCATION_RE,
    }
    for name, value in values.items():
        setattr(prior, name, value)


source_key = prior.source_key
_duplicates = prior._duplicates
_verify_jobs = prior._verify_jobs
_status_for_error = prior._status_for_error


def reconcile() -> list[dict[str, Any]]:
    _configure_prior()
    return prior.reconcile()


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY,
                             additions: Optional[Iterable[Mapping[str, Any]]] = None) -> bytes:
    _configure_prior()
    chosen = CANDIDATE_ENTRIES if additions is None else additions
    return prior.build_candidate_registry(production, destination, chosen)


def _inventory_bytes(results: Iterable[Mapping[str, Any]]) -> bytes:
    by_name = {str(item["company"]): item for item in results}
    rows = []
    for name in REVIEWED_COMPANIES:
        entry = next((item for item in CANDIDATE_ENTRIES if item["name"] == name), None)
        review = next((item for item in EXCLUDED_REVIEWS if item["company"] == name), None)
        source = entry or review or {}; result = by_name[name]; runs = list(result.get("runs") or [])
        rows.append({
            "company": name, "fresh_vs_recovery_target": "recovery" if name in RECOVERY_TARGETS else "fresh",
            "official_careers_url": OFFICIAL_CAREERS_URLS[name],
            "listing_url": source.get("listing_url") or source.get("search_url") or source.get("careers_url", ""),
            "identity_ownership_location_evidence": IDENTITY_LOCATION_EVIDENCE[name],
            "canada_us_remote_student_compatibility": COMPATIBILITY[name],
            "platform": PLATFORM_NOTES[name][0], "adapter": PLATFORM_NOTES[name][1],
            "result": result["result"], "jobs_normalized": result["jobs_normalized"],
            "current_student_intern_hits": result["current_student_intern_hits"],
            "current_student_role_evidence": "; ".join(hit["title"] for hit in result["current_student_role_evidence"]),
            "canada_us_remote_locations_represented": " | ".join(result.get("canada_us_remote_locations_represented") or []),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name],
            "exclusion_reason": result["exclusion_reason"], "original_246_master": str(name in MASTER_RANKS).lower(),
            "master_rank": MASTER_RANKS.get(name, ""), "validation_status": result["validation_status"],
            "stable_id_result": result["stable_id_result"], "apply_url_result": result["apply_url_result"],
            "allowed_apply_hosts": ";".join(source.get("allowed_apply_hosts") or []),
            "job_count_run_1": runs[0].get("jobs_normalized", "") if runs else "",
            "job_count_run_2": runs[1].get("jobs_normalized", "") if len(runs) > 1 else "",
        })
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    _configure_prior()
    payload = prior.validate(timeout)
    payload["candidate_registry_has_exact_249_entry_prefix"] = payload.pop("candidate_registry_has_exact_243_entry_prefix")
    payload["validated_registry_has_exact_249_entry_prefix"] = payload.pop("validated_registry_has_exact_243_entry_prefix")
    for result in payload["results"]:
        result["identity_ownership_location_evidence"] = result.pop("canadian_identity_location_evidence")
        result["canada_us_remote_locations_represented"] = result.pop("canadian_locations_represented")
    totals = payload["totals"]
    totals["fresh_targets"] = len(REVIEWED_COMPANIES) - len(RECOVERY_TARGETS)
    totals["recovery_targets"] = len(RECOVERY_TARGETS)
    totals["statuses"] = dict(sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items()))
    prior._write_bytes(CANDIDATE_CSV, _inventory_bytes(payload["results"]))
    prior._write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return payload


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.build_only:
            reconcile(); build_candidate_registry()
            prior._write_bytes(VALIDATED_REGISTRY, engine._registry_bytes(PRODUCTION, []))
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Requested expansion v3 Batch F validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
