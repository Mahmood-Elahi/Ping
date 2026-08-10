"""Build and validate Original Master Recovery Batch G without changing production."""

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

from scripts import validate_requested_expansion_v3_batch_f as prior
from scripts import validate_calgary_student_employers_v1_batch_a as engine


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_original_master_recovery_batch_g_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_g_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.original_master_recovery_batch_g_validated.yaml"
REPORT = ROOT / "output" / "original_master_recovery_batch_g_validation.json"

HISTORICAL_PRODUCTION_COUNT = 257
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 188
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

# OHB and Yaskawa use the exact canonical names in the original workbook.
REVIEWED_COMPANIES = [
    "PETRONAS", "Astrobotic", "Intuitive Machines", "OHB SE", "Revolut",
    "SoFi", "Nubank", "Tower Research Capital", "Mitsubishi Electric", "FANUC",
    "Yaskawa Electric", "Hitachi", "Cognex", "Mercedes-Benz", "BMW",
]
RECOVERY_TARGETS = set(REVIEWED_COMPANIES)
MASTER_RANKS = {
    "Revolut": 66, "SoFi": 72, "Nubank": 73, "Tower Research Capital": 83,
    "Mitsubishi Electric": 97, "FANUC": 98, "Yaskawa Electric": 100,
    "Hitachi": 107, "Cognex": 109, "Mercedes-Benz": 111, "BMW": 112,
    "PETRONAS": 179, "Astrobotic": 189, "Intuitive Machines": 194, "OHB SE": 197,
}

OFFICIAL_CAREERS_URLS = {
    "PETRONAS": "https://careers.petronas.com/en/sites/CX_1/jobs",
    "Astrobotic": "https://www.astrobotic.com/company/careers/",
    "Intuitive Machines": "https://www.intuitivemachines.com/discover/careers",
    "OHB SE": "https://www.ohb.de/en/career",
    "Revolut": "https://www.revolut.com/careers/",
    "SoFi": "https://www.sofi.com/careers/",
    "Nubank": "https://international.nubank.com.br/careers/",
    "Tower Research Capital": "https://tower-research.com/careers/",
    "Mitsubishi Electric": "https://www.mitsubishielectric.com/en/careers/",
    "FANUC": "https://www.fanucamerica.com/careers",
    "Yaskawa Electric": "https://www.yaskawa-global.com/jobs",
    "Hitachi": "https://careers.hitachi.com/",
    "Cognex": "https://www.cognex.com/company/careers",
    "Mercedes-Benz": "https://jobs.mercedes-benz.com/en",
    "BMW": "https://www.bmwgroup.jobs/ca/en/jobs.html",
}

IDENTITY_LOCATION_EVIDENCE = {
    "PETRONAS": "The exact PETRONAS vanity host identifies PETRONAS and site CX_1, backed by the epuc Oracle Recruiting tenant. The separate exact CX_4 internship site is not mixed into this canonical source.",
    "Astrobotic": "Astrobotic Technology's exact Pittsburgh careers page links tenant dde86c74-1cf6-4830-b396-33b9bd085ff9 on ADP Workforce Now.",
    "Intuitive Machines": "The exact Intuitive Machines careers route identifies the Houston-based company and links ADP tenant b0e24f83-6e4d-492d-9d6a-bc0fea197d6a; rebrand-related Lanteris content is not treated as a separate board.",
    "OHB SE": "OHB SE's exact corporate careers page links company code career-ohb and careersite 4 on Cornerstone CSOD.",
    "Revolut": "Revolut's exact first-party careers collection uses official UUID position paths, but anonymous validation receives an HTTP 403 security checkpoint.",
    "SoFi": "SoFi's exact first-party careers page remains the authoritative SoFi collection, but bounded anonymous requests receive HTTP 403.",
    "Nubank": "Nubank's exact corporate careers page directly links the nubank Ashby board and documents Canada and United States technology centres.",
    "Tower Research Capital": "Tower Research Capital's exact corporate careers flow leads to company identifier TowerResearchCapitalLLC; every API row and detail repeats the exact Tower Research Capital LLC identity.",
    "Mitsubishi Electric": "Mitsubishi Electric's global directory links the exact U.S. regional site, which links the Mitsubishi Electric US, Inc. HRMDirect collection. Other Mitsubishi companies are not inferred or merged.",
    "FANUC": "FANUC America's exact careers page links the fanuc ADP Recruiting board; this recovers the requested FANUC identity only at the verified U.S. regional boundary.",
    "Yaskawa Electric": "Yaskawa Electric's global directory delegates to the exact Yaskawa America search. Its server-rendered rows link only tenant YAS1000YAAMI boards and preserve exact per-job locations.",
    "Hitachi": "The current exact Hitachi global careers collection is a Radancy TalentBrew board with per-row subsidiary attribution, but anonymous validator requests are denied with HTTP 403.",
    "Cognex": "The exact Cognex careers route currently returns a Vercel Security Checkpoint with HTTP 429 to bounded anonymous requests.",
    "Mercedes-Benz": "The exact Mercedes-Benz job site is a Beesite-backed Nuxt shell referencing jobs.api.mercedes-benz.com; indexed details remain first-party but the anonymous listing contract is not registered.",
    "BMW": "The exact BMW Group Canada job finder documents BMW Group attribution and Canadian vacancies, but repeated bounded network requests time out in this environment.",
}

COMPATIBILITY = {
    "PETRONAS": "The exact general board is global but currently Malaysia-only in the bounded prefix. PETRONAS's separate official CX_4 board proves a substantial internship program, also Malaysia-only rather than Canada/US/Remote.",
    "Astrobotic": "Pittsburgh/US compatible; student-title compatibility cannot be safely normalized without an ADP adapter.",
    "Intuitive Machines": "US locations including Houston and California are compatible; current student-title compatibility is not safely normalized without an ADP adapter.",
    "OHB SE": "College-student and hybrid-work programs are official, but the source is Europe-focused rather than Canada/US/Remote.",
    "Revolut": "Remote and internship programs are official, but current Canada/US student hits cannot be safely normalized through the protected listing.",
    "SoFi": "US and university-recruiting compatible; current hits cannot be safely normalized through the protected listing.",
    "Nubank": "Canada/US compatible; the bounded Ashby prefix contains Miami, Palo Alto, and Toronto roles but no current student title.",
    "Tower Research Capital": "US compatible; the exact SmartRecruiters prefix contains New York roles and no current student title.",
    "Mitsubishi Electric": "US and internship compatible; the bounded exact regional collection contains a current Human Resources Intern role.",
    "FANUC": "US compatible; current internship compatibility cannot be safely normalized without an ADP adapter.",
    "Yaskawa Electric": "Canada/US/Remote compatible; current rows include Mississauga, multiple U.S. locations, and Remote, with no current student title.",
    "Hitachi": "Canada/US/Remote and internship compatible in the official collection, but the current access response prevents safe normalization.",
    "Cognex": "Canada/US/global and student compatible in principle; the security checkpoint prevents current validation.",
    "Mercedes-Benz": "Canada/US and internship compatible; first-party details include the Canada MYMI program, but no reviewed anonymous collection adapter exists.",
    "BMW": "Canada/US and internship/co-op compatible; the exact job finder cannot be normalized after repeated bounded timeouts.",
}

STUDENT_PROGRAM_EVIDENCE = {
    "PETRONAS": "https://careers.petronas.com/en/sites/CX_4/jobs",
    "Astrobotic": OFFICIAL_CAREERS_URLS["Astrobotic"],
    "Intuitive Machines": OFFICIAL_CAREERS_URLS["Intuitive Machines"],
    "OHB SE": "https://www.ohb.de/en/career/students",
    "Revolut": "https://www.revolut.com/careers/",
    "SoFi": "https://www.sofi.com/careers/university-recruiting/",
    "Nubank": "https://international.nubank.com.br/company/nubank-opens-applications-for-the-4th-edition-of-its-internship-program-with-over-140-positions-in-technology-and-corporate-roles/",
    "Tower Research Capital": OFFICIAL_CAREERS_URLS["Tower Research Capital"],
    "Mitsubishi Electric": "https://us.mitsubishielectric.com/en/about/local/careers/",
    "FANUC": OFFICIAL_CAREERS_URLS["FANUC"],
    "Yaskawa Electric": "https://www.yaskawa.com/about-us/careers/student-programs",
    "Hitachi": "https://careers.hitachi.com/search/internship/jobs",
    "Cognex": OFFICIAL_CAREERS_URLS["Cognex"],
    "Mercedes-Benz": OFFICIAL_CAREERS_URLS["Mercedes-Benz"],
    "BMW": "https://www.bmwgroup.jobs/ca/en/opportunities/student.html",
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex, "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts,
        "original_workbook_rank": MASTER_RANKS[name], "max_pages": 1,
        "max_jobs": MAX_JOBS, "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES = [
    _entry("PETRONAS", "oracle_recruiting", OFFICIAL_CAREERS_URLS["PETRONAS"],
           identity_regex=r"PETRONAS", allowed_apply_hosts=["careers.petronas.com"],
           identity_url=OFFICIAL_CAREERS_URLS["PETRONAS"],
           endpoint_url="https://epuc.fa.ap1.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
           endpoint_variant="hcmRestApi_recruitingCEJobRequisitions", site_number="CX_1", locale="en"),
    _entry("Nubank", "ashby", OFFICIAL_CAREERS_URLS["Nubank"],
           identity_regex=r"Nubank|About Nu", allowed_apply_hosts=["jobs.ashbyhq.com"],
           identity_url=OFFICIAL_CAREERS_URLS["Nubank"], slug="nubank"),
    _entry("Tower Research Capital", "smartrecruiters", OFFICIAL_CAREERS_URLS["Tower Research Capital"],
           identity_regex=r"Tower Research Capital LLC", allowed_apply_hosts=["jobs.smartrecruiters.com"],
           identity_url="https://careers.smartrecruiters.com/TowerResearchCapitalLLC",
           slug="TowerResearchCapitalLLC",
           endpoint_url="https://api.smartrecruiters.com/v1/companies/TowerResearchCapitalLLC/postings",
           endpoint_variant="companies_postings_v1", page_size=20),
    _entry("Mitsubishi Electric", "static_html", OFFICIAL_CAREERS_URLS["Mitsubishi Electric"],
           identity_regex=r"MITSUBISHI ELECTRIC UNITED STATES|Mitsubishi Electric US",
           allowed_apply_hosts=["meus-mea.hrmdirect.com"],
           identity_url="https://us.mitsubishielectric.com/en/about/local/careers/",
           listing_url="https://meus-mea.hrmdirect.com/employment/job-openings.php?search=true&dept=-1&city=-1&state=-1",
           endpoint_variant="server_rendered_cards", listing_selector="table.reqResultTable",
           job_card_selector="tr[data-req-id]", title_selector="td.posTitle a[href]",
           location_selector="td.cities, td.state", department_selector="td.custSort1",
           detail_link_selector="td.posTitle a[href]",
           external_id_regex=r"req=([0-9]+&req_loc=[0-9]+)"),
    _entry("Yaskawa Electric", "static_html", OFFICIAL_CAREERS_URLS["Yaskawa Electric"],
           identity_regex=r"Search Jobs - Yaskawa|Yaskawa America",
           allowed_apply_hosts=["recruiting.ultipro.com"],
           identity_url="https://www.yaskawa.com/about-us/careers/search-jobs",
           listing_url="https://www.yaskawa.com/about-us/careers/search-jobs",
           endpoint_variant="server_rendered_cards", listing_selector="tbody.table-data",
           job_card_selector="tbody.table-data tr",
           title_selector="td:nth-child(2) a[href*=opportunityId=]",
           location_selector="td:nth-child(3)", date_selector="td:nth-child(4)",
           department_selector="td:nth-child(5)",
           detail_link_selector="td:nth-child(2) a[href*=opportunityId=]",
           external_id_regex=r"opportunityId=([0-9a-f-]{36})"),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {"company": company, "platform": platform, "adapter": "none", "listing_url": listing_url,
            "classification": classification, "exclusion_reason": reason, "identity_regex": identity_regex}


EXCLUDED_REVIEWS = [
    _excluded("Astrobotic", "ADP Workforce Now", "https://workforcenow.cloud.adp.com/mascsr/default/mdf/recruitment/recruitment.html?ccId=48397579_2&cid=dde86c74-1cf6-4830-b396-33b9bd085ff9&lang=en_US", "unsupported_platform",
              "The exact Astrobotic page links a stable ADP Workforce Now tenant, but ADP has no registered adapter. It belongs with the three-company ADP adapter opportunity; no browser or tenant-specific parsing is added.", r"Astrobotic Technology"),
    _excluded("Intuitive Machines", "ADP Workforce Now", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=b0e24f83-6e4d-492d-9d6a-bc0fea197d6a&ccId=19000101_000001&lang=en_US", "unsupported_platform",
              "The exact Intuitive Machines client bundle links a stable ADP Workforce Now tenant, but ADP has no registered adapter. No browser rendering or tenant-specific parsing is added.", r"Intuitive Machines"),
    _excluded("OHB SE", "Cornerstone CSOD", "https://career-ohb.csod.com/ux/ats/careersite/4/home?c=career-ohb&lang=en-GB", "browser_required",
              "OHB SE's exact page links Cornerstone company code career-ohb and careersite 4, but anonymous static responses expose no job collection, stable IDs, or official apply URLs. No browser/session workaround is attempted.", r"OHB|Career"),
    _excluded("Revolut", "Custom first-party / security checkpoint", OFFICIAL_CAREERS_URLS["Revolut"], "blocked",
              "Revolut's exact first-party careers collection and UUID position paths are official, but bounded anonymous requests receive HTTP 403. No security-checkpoint, cookie, or session bypass is attempted.", r"Revolut|security check"),
    _excluded("SoFi", "Custom first-party / HTTP 403", OFFICIAL_CAREERS_URLS["SoFi"], "blocked",
              "SoFi's exact first-party careers page and university program are official, but bounded anonymous requests receive HTTP 403. No WAF, cookie, or browser bypass is attempted.", r"SoFi|Welcome"),
    _excluded("FANUC", "ADP Recruiting", "https://myjobs.adp.com/fanuc/cx", "unsupported_platform",
              "FANUC America's exact careers page links the stable fanuc ADP board, but ADP has no registered adapter. It is retained in the reusable ADP opportunity instead of receiving custom parsing.", r"FANUC"),
    _excluded("Hitachi", "Radancy TalentBrew / HTTP 403", "https://careers.hitachi.com/search/jobs", "blocked",
              "The exact Hitachi global TalentBrew collection shows Canada, United States, Remote, and Internship facets, but bounded anonymous requests receive HTTP 403. The existing static adapter is not forced through the access control.", r"Hitachi|Careers"),
    _excluded("Cognex", "Vercel Security Checkpoint", OFFICIAL_CAREERS_URLS["Cognex"], "blocked",
              "The exact Cognex careers route returns HTTP 429 with a Vercel Security Checkpoint. No challenge or session bypass is attempted.", r"Cognex|Security Checkpoint"),
    _excluded("Mercedes-Benz", "Beesite custom REST", OFFICIAL_CAREERS_URLS["Mercedes-Benz"], "unsupported_platform",
              "The exact Mercedes-Benz Nuxt shell identifies jobs.api.mercedes-benz.com and stable first-party MER requisition URLs, but no reviewed Beesite collection adapter exists. A singleton endpoint hack is not added.", r"mercedes-benz|jobs.api"),
    _excluded("BMW", "BMW Group custom job finder", OFFICIAL_CAREERS_URLS["BMW"], "network_inaccessible",
              "The exact BMW Group Canada job finder and internship program are indexed, but repeated bounded anonymous requests time out in this environment. No alternate host, browser, or session workaround is used.", r"BMW Group|Jobs at the BMW Group"),
]

PLATFORM_NOTES = {
    "PETRONAS": ("Oracle Recruiting", "oracle_recruiting"),
    "Astrobotic": ("ADP Workforce Now", "none"),
    "Intuitive Machines": ("ADP Workforce Now", "none"),
    "OHB SE": ("Cornerstone CSOD", "none"),
    "Revolut": ("Custom first-party / security checkpoint", "none"),
    "SoFi": ("Custom first-party / HTTP 403", "none"),
    "Nubank": ("Ashby", "ashby"),
    "Tower Research Capital": ("SmartRecruiters", "smartrecruiters"),
    "Mitsubishi Electric": ("HRMDirect server-rendered", "static_html"),
    "FANUC": ("ADP Recruiting", "none"),
    "Yaskawa Electric": ("Liferay aggregate / UKG Pro links", "static_html"),
    "Hitachi": ("Radancy TalentBrew / HTTP 403", "none"),
    "Cognex": ("Vercel Security Checkpoint", "none"),
    "Mercedes-Benz": ("Beesite custom REST", "none"),
    "BMW": ("BMW Group custom job finder", "none"),
}

ADAPTER_OPPORTUNITIES = [
    {"platform": "ADP Workforce Now / ADP Recruiting", "companies": ["Astrobotic", "Intuitive Machines", "FANUC"],
     "reusable_adapter_justified": True,
     "recommendation": "Implement one separately reviewed anonymous ADP adapter covering both workforcenow and myjobs entry points, with tenant/ccId identity checks and stable opportunity/apply contracts."},
    {"platform": "Cornerstone CSOD", "companies": ["OHB SE"],
     "reusable_adapter_justified": "research justified; implementation not yet justified",
     "recommendation": "First prove a stable anonymous generic collection contract on multiple tenants; the current OHB shell alone exposes no static jobs."},
    {"platform": "Beesite", "companies": ["Mercedes-Benz"],
     "reusable_adapter_justified": False,
     "recommendation": "Retain as unsupported until a second exact tenant and a bounded public listing contract establish reuse."},
]

LOCATION_RE = re.compile(
    r"\b(?:Canada|Remote|United States|Miami|Palo Alto|Toronto|New York|Mississauga|Mason|Vernon Hills|"
    r"Suwanee|Cypress|Aiea|Hayward|Countryside|Oakland|Franklin|Waukegan|Buffalo Grove)\b|"
    r"(?:,|\|)\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|"
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
        "IDENTITY_LOCATION_EVIDENCE": IDENTITY_LOCATION_EVIDENCE, "COMPATIBILITY": COMPATIBILITY,
        "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE, "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES,
        "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS, "PLATFORM_NOTES": PLATFORM_NOTES,
        "LOCATION_RE": LOCATION_RE, "CSV_FIELDS": CSV_FIELDS,
    }
    for name, value in values.items():
        setattr(prior, name, value)


@contextmanager
def _isolated_prior_configuration() -> Iterator[None]:
    """Keep the reused Batch F/E engine bindings local to this validator call."""
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


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY,
                             additions: Optional[Iterable[Mapping[str, Any]]] = None) -> bytes:
    chosen = CANDIDATE_ENTRIES if additions is None else additions
    with _isolated_prior_configuration():
        return prior.build_candidate_registry(production, destination, chosen)


def validate(timeout: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    with _isolated_prior_configuration():
        payload = prior.validate(timeout)
    payload["candidate_registry_has_exact_257_entry_prefix"] = payload.pop(
        "candidate_registry_has_exact_249_entry_prefix"
    )
    payload["validated_registry_has_exact_257_entry_prefix"] = payload.pop(
        "validated_registry_has_exact_249_entry_prefix"
    )
    payload["adapter_opportunities"] = ADAPTER_OPPORTUNITIES
    totals = payload["totals"]
    totals["fresh_targets"] = 0
    totals["recovery_targets"] = len(REVIEWED_COMPANIES)
    totals["statuses"] = dict(sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items()))
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
        print(f"Original Master Recovery Batch G validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
