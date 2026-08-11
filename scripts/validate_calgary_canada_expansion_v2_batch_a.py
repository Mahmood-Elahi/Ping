"""Build and validate Calgary/Canada Expansion v2 Batch A without activation."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_requested_expansion_v3_batch_f as prior
from scripts import validate_calgary_student_employers_v1_batch_a as engine


PRODUCTION = ROOT / "config" / "target_companies.yaml"
PARENT_MAPPINGS = ROOT / "config" / "company_parent_source_mappings.yaml"
MASTER_WORKBOOK = ROOT / "config" / "company_watchlist.xlsx"
CANDIDATE_CSV = ROOT / "config" / "company_calgary_canada_expansion_v2_batch_a_candidates.csv"
CANDIDATE_REGISTRY = ROOT / "config" / "target_companies.calgary_canada_expansion_v2_batch_a_candidates.yaml"
VALIDATED_REGISTRY = ROOT / "config" / "target_companies.calgary_canada_expansion_v2_batch_a_validated.yaml"
REPORT = ROOT / "output" / "calgary_canada_expansion_v2_batch_a_validation.json"

HISTORICAL_PRODUCTION_COUNT = 262
HISTORICAL_PARENT_MAPPINGS = 4
HISTORICAL_ORIGINAL_COVERAGE = 193
MASTER_COMPANY_COUNT = 246
MAX_TIMEOUT_SECONDS = 30
MAX_PAGES = 2
MAX_JOBS = 20
MAX_RENDERED_HTML_BYTES = 3_000_000

REVIEWED_COMPANIES = [
    "CPKC", "WestJet", "NOVA Chemicals", "Trans Mountain",
    "Computer Modelling Group (CMG)", "Enerflex", "Neo Financial", "MNP",
    "Absorb Software", "Helcim", "PurposeMed", "Eavor Technologies",
    "Parkland Corporation", "Blackline Safety", "AltaLink",
]
RECOVERY_TARGETS = {"Parkland Corporation", "Blackline Safety", "AltaLink"}
MASTER_RANKS: dict[str, int] = {}

OFFICIAL_CAREERS_URLS = {
    "CPKC": "https://www.cpkcr.com/en/careers",
    "WestJet": "https://www.westjet.com/en-ca/careers",
    "NOVA Chemicals": "https://www.novachem.com/careers/",
    "Trans Mountain": "https://careers.transmountain.com/?locale=en_US",
    "Computer Modelling Group (CMG)": "https://www.cmgl.ca/careers/",
    "Enerflex": "https://www.enerflex.com/careers/",
    "Neo Financial": "https://www.neofinancial.com/company/careers?lang=en-CA",
    "MNP": "https://www.mnp.ca/en/careers",
    "Absorb Software": "https://www.absorbai.com/company/careers",
    "Helcim": "https://www.helcim.com/careers/",
    "PurposeMed": "https://www.purposemed.com/careers",
    "Eavor Technologies": "https://eavor.com/careers/",
    "Parkland Corporation": "https://www.parkland.ca/en/careers",
    "Blackline Safety": "https://careers.blacklinesafety.com/home",
    "AltaLink": "https://www.altalink.ca/join-our-team/",
}

IDENTITY_LOCATION_EVIDENCE = {
    "CPKC": "The exact CPKC corporate careers page links careers.cpr.ca for Canada and US roles and identifies Calgary headquarters.",
    "WestJet": "The exact WestJet careers page identifies Calgary as national headquarters and links the WestJet Dayforce tenant.",
    "NOVA Chemicals": "The former NOVA careers route still redirects to an exact NOVA Chemicals SuccessFactors board after NOVA combined into Borouge International.",
    "Trans Mountain": "The exact Trans Mountain board publishes Canadian and US pipeline roles, including a large current Calgary collection.",
    "Computer Modelling Group (CMG)": "The exact Computer Modelling Group Ltd. WordPress careers collection publishes company-owned job pages from its Calgary headquarters.",
    "Enerflex": "The exact Enerflex corporate site links its global SuccessFactors board, which publishes Canada and US roles including Calgary.",
    "Neo Financial": "The exact Neo Financial corporate page links the neofinancial Ashby board and identifies Calgary, Winnipeg, and Toronto offices.",
    "MNP": "The exact MNP careers page separately links its MNP5000MNPL UKG Campus Career Opportunities board.",
    "Absorb Software": "The exact Absorb Software remote-first careers page embeds the absorblms Ashby board for Canada, US, and global roles.",
    "Helcim": "The exact Helcim careers page identifies Calgary and Seattle offices and links helcim.careers.hibob.com.",
    "PurposeMed": "The exact Calgary PurposeMed careers route embeds the purposemed Greenhouse board for Canada and USA roles.",
    "Eavor Technologies": "The exact Eavor Technologies careers page identifies its Calgary company and lists Canada and European positions.",
    "Parkland Corporation": "The exact Parkland page still links parkland-careers.ttcportals.com; the contract is unchanged from the prior blocked review.",
    "Blackline Safety": "The exact Blackline careers hub still links the blacklinesafety Workable account; the contract is unchanged from the prior Turnstile review.",
    "AltaLink": "The exact AltaLink page still links the altalink Workable account; the contract is unchanged from the prior Turnstile review.",
}

COMPATIBILITY = {
    "CPKC": "Canada/US and student compatible in principle; the current columnized RMK collection is not safely normalized by the existing adapter.",
    "WestJet": "Canada and student compatible in principle; the exact Dayforce shell advertises reCAPTCHA and no Dayforce adapter is registered.",
    "NOVA Chemicals": "Canada/US and student-program compatible; the exact NOVA source is checked for current student titles.",
    "Trans Mountain": "Canada/US and student-program compatible; current normalized roles are checked for student titles and Calgary locations.",
    "Computer Modelling Group (CMG)": "Canada/US/global and junior-role compatible; exact current static postings are normalized.",
    "Enerflex": "Canada/US/global and student/early-career compatible; current normalized titles include an internship.",
    "Neo Financial": "Canada/Remote compatible; current normalized roles are checked for student titles and Calgary locations.",
    "MNP": "Canada campus compatible; the exact campus-only source is used intentionally and current intern/co-op titles are normalized.",
    "Absorb Software": "Canada/US/Remote compatible; current normalized roles are checked for student titles.",
    "Helcim": "Canada/US and junior-career compatible in principle; HiBob has no registered safe adapter.",
    "PurposeMed": "Canada/US/Remote compatible; current normalized roles are checked for student titles.",
    "Eavor Technologies": "Canada/US/Europe and new-graduate compatible in principle; current headings have no stable job-specific apply URLs.",
    "Parkland Corporation": "Canada/US and new-graduate compatible in principle; the unchanged TTC portal remains Cloudflare-blocked.",
    "Blackline Safety": "Canada/US compatible in principle; the unchanged Workable apply flow remains Turnstile-protected.",
    "AltaLink": "Calgary/Canada student compatible in principle; the unchanged Workable apply flow remains Turnstile-protected.",
}

ATTRIBUTION_EVIDENCE = {
    name: evidence for name, evidence in IDENTITY_LOCATION_EVIDENCE.items()
}

STUDENT_PROGRAM_EVIDENCE = {
    "CPKC": OFFICIAL_CAREERS_URLS["CPKC"],
    "WestJet": OFFICIAL_CAREERS_URLS["WestJet"],
    "NOVA Chemicals": "https://jobs.novachem.com/content/Students-/?locale=en_US",
    "Trans Mountain": OFFICIAL_CAREERS_URLS["Trans Mountain"],
    "Computer Modelling Group (CMG)": OFFICIAL_CAREERS_URLS["Computer Modelling Group (CMG)"],
    "Enerflex": "https://www.enerflex.com/careers/students-and-early-career/",
    "Neo Financial": OFFICIAL_CAREERS_URLS["Neo Financial"],
    "MNP": OFFICIAL_CAREERS_URLS["MNP"],
    "Absorb Software": OFFICIAL_CAREERS_URLS["Absorb Software"],
    "Helcim": OFFICIAL_CAREERS_URLS["Helcim"],
    "PurposeMed": OFFICIAL_CAREERS_URLS["PurposeMed"],
    "Eavor Technologies": OFFICIAL_CAREERS_URLS["Eavor Technologies"],
    "Parkland Corporation": OFFICIAL_CAREERS_URLS["Parkland Corporation"],
    "Blackline Safety": OFFICIAL_CAREERS_URLS["Blackline Safety"],
    "AltaLink": OFFICIAL_CAREERS_URLS["AltaLink"],
}


def _entry(name: str, platform: str, careers_url: str, *, identity_regex: str,
           allowed_apply_hosts: list[str], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name, "platform": platform, "careers_url": careers_url,
        "source_identity_regex": identity_regex, "identity_regex": identity_regex,
        "allowed_apply_hosts": allowed_apply_hosts, "original_workbook_rank": None,
        "max_pages": 1, "max_jobs": MAX_JOBS,
        "max_response_bytes": MAX_RENDERED_HTML_BYTES,
    }
    item.update(fields)
    return item


CANDIDATE_ENTRIES = [
    _entry("NOVA Chemicals", "sap_successfactors", "https://jobs.novachem.com",
           identity_regex=r"NOVA Chemicals", allowed_apply_hosts=["jobs.novachem.com"],
           identity_url="https://jobs.novachem.com", search_url="https://jobs.novachem.com/search/",
           endpoint_variant="rmk_table"),
    _entry("Trans Mountain", "sap_successfactors", OFFICIAL_CAREERS_URLS["Trans Mountain"],
           identity_regex=r"Trans Mountain", allowed_apply_hosts=["careers.transmountain.com"],
           identity_url="https://careers.transmountain.com/?locale=en_US",
           search_url="https://careers.transmountain.com/search/", endpoint_variant="rmk_tile"),
    _entry("Computer Modelling Group (CMG)", "static_html", OFFICIAL_CAREERS_URLS["Computer Modelling Group (CMG)"],
           identity_regex=r"Computer Modelling Group|Careers at CMG", allowed_apply_hosts=["www.cmgl.ca"],
           identity_url="https://www.cmgl.ca/careers/", listing_url="https://www.cmgl.ca/careers/",
           endpoint_variant="server_rendered_cards", listing_selector=".jet-listing-grid--539",
           job_card_selector=".jet-listing-grid__item", title_selector="h3.elementor-heading-title",
           location_selector=".elementor-element-ae0a93e .elementor-heading-title",
           detail_link_selector=".jet-engine-listing-overlay-wrap", reference_url_attribute="data-url",
           external_id_attribute="data-post-id"),
    _entry("Enerflex", "sap_successfactors", OFFICIAL_CAREERS_URLS["Enerflex"],
           identity_regex=r"Enerflex", allowed_apply_hosts=["jobs.enerflex.com"],
           identity_url="https://www.enerflex.com/careers/", search_url="https://jobs.enerflex.com/search/",
           endpoint_variant="rmk_tile"),
    _entry("Neo Financial", "ashby", OFFICIAL_CAREERS_URLS["Neo Financial"],
           identity_regex=r"Neo Financial Jobs", allowed_apply_hosts=["jobs.ashbyhq.com"],
           identity_url="https://jobs.ashbyhq.com/neofinancial", slug="neofinancial"),
    _entry("MNP", "ukg_pro",
           "https://recruiting.ultipro.ca/MNP5000MNPL/JobBoard/d1e870eb-9c8c-4ce5-88ee-9f042cf2a12f",
           identity_regex=r"^Campus Career Opportunities$", source_identity_regex=r"MNP",
           allowed_apply_hosts=["recruiting.ultipro.ca"], identity_url="https://www.mnp.ca/en/careers"),
    _entry("Absorb Software", "ashby", OFFICIAL_CAREERS_URLS["Absorb Software"],
           identity_regex=r"Absorb Jobs", allowed_apply_hosts=["jobs.ashbyhq.com"],
           identity_url="https://jobs.ashbyhq.com/absorblms", slug="absorblms"),
    _entry("PurposeMed", "greenhouse", OFFICIAL_CAREERS_URLS["PurposeMed"],
           identity_regex=r"PurposeMed", allowed_apply_hosts=["www.purposemed.com"],
           identity_url="https://www.purposemed.com/careers/open", slug="purposemed"),
]


def _excluded(company: str, platform: str, listing_url: str, classification: str,
              reason: str, identity_regex: str) -> dict[str, Any]:
    return {"company": company, "platform": platform, "adapter": "none",
            "listing_url": listing_url, "classification": classification,
            "exclusion_reason": reason, "identity_regex": identity_regex}


EXCLUDED_REVIEWS = [
    _excluded("CPKC", "SAP SuccessFactors RMK columnized search", "https://careers.cpr.ca/search/",
              "unsupported_contract", "The official CPKC Canada/US link still targets careers.cpr.ca, but its current BS3 columnized RMK page no longer exposes the static table/tile contract supported by the existing SuccessFactors adapter. No session-bound search API is inferred.", r"CPKC|Careers"),
    _excluded("WestJet", "Dayforce", "https://jobs.dayforcehcm.com/en-CA/WestJet/CANDIDATEPORTAL",
              "unsupported_platform", "The exact WestJet page links its WestJet Dayforce tenant. Dayforce is not registered, and the public shell advertises reCAPTCHA; no CAPTCHA or session flow is used.", r"WestJet|Great careers"),
    _excluded("Helcim", "HiBob", "https://helcim.careers.hibob.com/", "unsupported_platform",
              "The exact Helcim page now links its helcim HiBob board. HiBob has no registered adapter and this batch does not infer an undocumented SPA API.", r"Helcim|Careers"),
    _excluded("Eavor Technologies", "WordPress/static email application", OFFICIAL_CAREERS_URLS["Eavor Technologies"],
              "no_stable_job_specific_urls", "The exact Eavor page lists current position headings but routes applications to shared email addresses; it exposes no stable per-job IDs or job-specific detail/apply URLs.", r"Eavor|Careers"),
    _excluded("Parkland Corporation", "TTC portals / Cloudflare", "https://parkland-careers.ttcportals.com/",
              "unchanged_protected_contract", "Revisited only to compare the contract: Parkland still links the same TTC portal and it still returns a Cloudflare challenge. No contract change or newly safe adapter support was found.", r"Parkland|Careers"),
    _excluded("Blackline Safety", "Workable / Turnstile", "https://apply.workable.com/blacklinesafety/",
              "unchanged_protected_contract", "Revisited only to compare the contract: Blackline still links the same Workable account and the apply shell still enables Turnstile. No contract change or newly safe adapter support was found.", r"Blackline|Careers"),
    _excluded("AltaLink", "Workable / Turnstile", "https://apply.workable.com/altalink/",
              "unchanged_protected_contract", "Revisited only to compare the contract: AltaLink still links the same Workable account and the apply shell still enables Turnstile. No contract change or newly safe adapter support was found.", r"AltaLink|Join our Team"),
]

PLATFORM_NOTES = {
    "CPKC": ("SAP SuccessFactors RMK columnized search", "none"),
    "WestJet": ("Dayforce / reCAPTCHA", "none"),
    "NOVA Chemicals": ("SAP SuccessFactors RMK", "sap_successfactors"),
    "Trans Mountain": ("SAP SuccessFactors RMK tiles", "sap_successfactors"),
    "Computer Modelling Group (CMG)": ("WordPress JetEngine static HTML", "static_html"),
    "Enerflex": ("SAP SuccessFactors RMK tiles", "sap_successfactors"),
    "Neo Financial": ("Ashby", "ashby"), "MNP": ("UKG Pro campus board", "ukg_pro"),
    "Absorb Software": ("Ashby", "ashby"), "Helcim": ("HiBob", "none"),
    "PurposeMed": ("Greenhouse", "greenhouse"),
    "Eavor Technologies": ("WordPress/static email application", "none"),
    "Parkland Corporation": ("TTC portals / Cloudflare", "none"),
    "Blackline Safety": ("Workable / Turnstile", "none"),
    "AltaLink": ("Workable / Turnstile", "none"),
}

LOCATION_RE = re.compile(
    r"\b(?:Canada|Remote|United States|USA|Alberta|British Columbia|Ontario|Quebec|Manitoba|Saskatchewan|"
    r"Calgary|Edmonton|Vancouver|Toronto|Ottawa|Montreal|Mississauga|Houston|Seattle|Bellingham)\b|"
    r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|"
    r"ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
    re.IGNORECASE,
)
STUDENT_ROLE_RE = re.compile(engine.STUDENT_ROLE_RE.pattern + r"|\bestagi[aá]ri[oa]s?\b", re.IGNORECASE)

CSV_FIELDS = [
    "company", "fresh_vs_recovery_target", "official_careers_url", "listing_url",
    "company_attribution_evidence", "identity_ownership_location_evidence",
    "canada_us_remote_student_compatibility", "platform", "adapter", "result",
    "jobs_normalized", "current_student_intern_hits", "current_student_role_evidence",
    "canada_us_remote_locations_represented", "calgary_locations_found",
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
        "MAX_RENDERED_HTML_BYTES": MAX_RENDERED_HTML_BYTES, "REVIEWED_COMPANIES": REVIEWED_COMPANIES,
        "MASTER_RANKS": MASTER_RANKS, "RECOVERY_TARGETS": RECOVERY_TARGETS,
        "OFFICIAL_CAREERS_URLS": OFFICIAL_CAREERS_URLS,
        "IDENTITY_LOCATION_EVIDENCE": IDENTITY_LOCATION_EVIDENCE,
        "COMPATIBILITY": COMPATIBILITY, "STUDENT_PROGRAM_EVIDENCE": STUDENT_PROGRAM_EVIDENCE,
        "CANDIDATE_ENTRIES": CANDIDATE_ENTRIES, "EXCLUDED_REVIEWS": EXCLUDED_REVIEWS,
        "PLATFORM_NOTES": PLATFORM_NOTES, "LOCATION_RE": LOCATION_RE,
    }
    for name, value in values.items():
        setattr(prior, name, value)


@contextmanager
def _isolated_prior() -> Iterable[None]:
    """Contain the legacy validator chain's intentional module-global wiring."""
    modules: list[ModuleType] = []
    for module in (prior, prior.prior, engine):
        if all(module is not existing for existing in modules):
            modules.append(module)
    snapshots = [(module, dict(vars(module))) for module in modules]
    try:
        yield
    finally:
        for module, snapshot in reversed(snapshots):
            namespace = vars(module)
            for name in set(namespace) - set(snapshot):
                del namespace[name]
            namespace.update(snapshot)


source_key = prior.source_key
_duplicates = prior._duplicates
_verify_jobs = prior._verify_jobs
_status_for_error = prior._status_for_error


def reconcile() -> list[dict[str, Any]]:
    with _isolated_prior():
        _configure_prior()
        return prior.reconcile()


def build_candidate_registry(production: Path = PRODUCTION, destination: Path = CANDIDATE_REGISTRY,
                             additions: Optional[Iterable[Mapping[str, Any]]] = None) -> bytes:
    with _isolated_prior():
        _configure_prior()
        return prior.build_candidate_registry(production, destination,
                                              CANDIDATE_ENTRIES if additions is None else additions)


def _calgary_locations(result: Mapping[str, Any]) -> list[str]:
    values = result.get("canada_us_remote_locations_represented") or []
    return [str(value) for value in values if re.search(r"\bCalgary\b", str(value), re.IGNORECASE)]


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
            "company_attribution_evidence": ATTRIBUTION_EVIDENCE[name],
            "identity_ownership_location_evidence": IDENTITY_LOCATION_EVIDENCE[name],
            "canada_us_remote_student_compatibility": COMPATIBILITY[name],
            "platform": PLATFORM_NOTES[name][0], "adapter": PLATFORM_NOTES[name][1],
            "result": result["result"], "jobs_normalized": result["jobs_normalized"],
            "current_student_intern_hits": result["current_student_intern_hits"],
            "current_student_role_evidence": "; ".join(hit["title"] for hit in result["current_student_role_evidence"]),
            "canada_us_remote_locations_represented": " | ".join(result.get("canada_us_remote_locations_represented") or []),
            "calgary_locations_found": " | ".join(result.get("calgary_locations_found") or []),
            "student_program_evidence_url": STUDENT_PROGRAM_EVIDENCE[name],
            "exclusion_reason": result["exclusion_reason"], "original_246_master": "false",
            "master_rank": "", "validation_status": result["validation_status"],
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
    with _isolated_prior():
        _configure_prior()
        engine.STUDENT_ROLE_RE = STUDENT_ROLE_RE
        payload = prior.validate(timeout)
        payload["candidate_registry_has_exact_262_entry_prefix"] = payload.pop("candidate_registry_has_exact_249_entry_prefix")
        payload["validated_registry_has_exact_262_entry_prefix"] = payload.pop("validated_registry_has_exact_249_entry_prefix")
        for result in payload["results"]:
            name = str(result["company"])
            result["company_attribution_evidence"] = ATTRIBUTION_EVIDENCE[name]
            result["calgary_locations_found"] = _calgary_locations(result)
        totals = payload["totals"]
        totals["fresh_targets"] = len(REVIEWED_COMPANIES) - len(RECOVERY_TARGETS)
        totals["recovery_targets"] = len(RECOVERY_TARGETS)
        totals["projected_original_master_coverage_ratio"] = "193/246"
        totals["statuses"] = dict(sorted(Counter(str(item["validation_status"]) for item in payload["results"]).items()))
        prior.prior._write_bytes(CANDIDATE_CSV, _inventory_bytes(payload["results"]))
        prior.prior._write_bytes(REPORT, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        return payload


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.build_only:
            reconcile(); build_candidate_registry()
            prior.prior._write_bytes(VALIDATED_REGISTRY, engine._registry_bytes(PRODUCTION, []))
            return 0
        payload = validate(args.timeout)
        print(json.dumps(payload["totals"], indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Calgary/Canada Expansion v2 Batch A validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
