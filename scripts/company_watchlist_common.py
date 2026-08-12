"""Shared helpers for safe company-watchlist enrichment and importing."""

from __future__ import annotations

import csv
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import yaml


ENRICHED_FIELDS = [
    "Rank",
    "Company",
    "Careers URL",
    "Priority",
    "Notes",
    "ATS Platform",
    "ATS Slug",
    "Workday Tenant",
    "Workday Subdomain",
    "Workday Site ID",
    "Import Status",
    "Validation Notes",
    "Source URL",
]
RESEARCH_FIELDS = [
    "Company",
    "Official Careers URL",
    "Official Job Search URL",
    "Detected Platform",
    "Required Adapter",
    "Existing Adapter Reusable",
    "Source URL",
    "Research Status",
    "Research Notes",
    "Reviewed",
    "ATS Slug",
    "Workday Tenant",
    "Workday Subdomain",
    "Workday Site ID",
]
RESEARCH_STATUSES = {
    "existing_config",
    "validated_supported",
    "validated_custom_adapter_required",
    "manual_review",
    "invalid",
}

DEFAULT_REGISTRY_PATH = Path("config/target_companies.yaml")
SUPPORTED_IMPORT_PLATFORMS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "amazon",
    "google",
    "meta",
    "ibm_search",
    "atlassian",
    "uber",
    "shopify",
}
DETECTED_BUT_UNSUPPORTED = {"workable", "rippling"}
DETECTED_REQUIRES_REVIEW = {"adp", "cornerstone_csod", "hibob", "smartrecruiters"}
EXISTING_ONLY_PLATFORMS = {"ibm_search"}
CUSTOM_PLATFORMS = {
    "amazon",
    "google",
    "meta",
    "ibm_search",
    "atlassian",
    "uber",
    "shopify",
}
PROHIBITED_JOB_BOARD_DOMAINS = {
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "simplify.jobs",
    "ziprecruiter.com",
    "monster.com",
    "wellfound.com",
}


@dataclass(frozen=True)
class ATSDetection:
    platform: str = ""
    slug: str = ""
    tenant: str = ""
    wd_subdomain: str = ""
    site_id: str = ""
    note: str = ""

    @property
    def complete(self) -> bool:
        if self.platform in {"greenhouse", "lever", "ashby"}:
            return bool(self.slug)
        if self.platform == "workday":
            return bool(self.tenant and self.wd_subdomain and self.site_id)
        return self.platform in CUSTOM_PLATFORMS


@dataclass(frozen=True)
class URLValidation:
    valid: bool
    final_url: str
    note: str


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_company_name(value: Any) -> str:
    """Return a comparison key without changing the display name."""
    text = unicodedata.normalize("NFKD", clean_text(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", text)


def canonicalize_url(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme:
        parsed = urlsplit("https://" + text)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return text
    try:
        port = parsed.port
    except ValueError:
        return text
    netloc = host if port in (None, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def hostname_matches(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def prohibited_job_board(url: str) -> bool:
    host = (urlsplit(canonicalize_url(url)).hostname or "").lower()
    return any(hostname_matches(host, domain) for domain in PROHIBITED_JOB_BOARD_DOMAINS)


def detect_ats(url: Any) -> ATSDetection:
    canonical = canonicalize_url(url)
    if not canonical:
        return ATSDetection(note="Missing careers URL")
    parsed = urlsplit(canonical)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if prohibited_job_board(canonical):
        return ATSDetection(note="Third-party job-board URLs are prohibited")

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        slug = parts[0] if parts else ""
        return ATSDetection("greenhouse", slug=slug, note=_slug_note(slug))
    if host == "jobs.lever.co":
        slug = parts[0] if parts else ""
        return ATSDetection("lever", slug=slug, note=_slug_note(slug))
    if host == "jobs.ashbyhq.com":
        slug = parts[0] if parts else ""
        return ATSDetection("ashby", slug=slug, note=_slug_note(slug))

    workday_match = re.fullmatch(
        r"(?P<tenant>[a-z0-9-]+)\.(?P<subdomain>wd\d+)\.myworkdayjobs\.com",
        host,
    )
    if workday_match:
        site_parts = parts
        if site_parts and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", site_parts[0]):
            site_parts = site_parts[1:]
        site_id = site_parts[0] if site_parts else ""
        note = "Detected Workday identifiers" if site_id else "Workday site ID is missing"
        return ATSDetection(
            "workday",
            tenant=workday_match.group("tenant"),
            wd_subdomain=workday_match.group("subdomain"),
            site_id=site_id,
            note=note,
        )

    if host == "jobs.smartrecruiters.com" and parts:
        return ATSDetection(
            "smartrecruiters",
            slug=parts[0],
            note="Detected ATS; reviewed identity and bounds configuration required",
        )
    csod_match = re.fullmatch(r"([a-z0-9-]+)\.csod\.com", host)
    csod_path = re.fullmatch(r"/ux/ats/careersite/([1-9][0-9]{0,5})/home/?", parsed.path)
    if csod_match and csod_path:
        tenant = str((parse_qs(parsed.query).get("c") or [csod_match.group(1)])[0]).casefold()
        return ATSDetection(
            "cornerstone_csod",
            tenant=tenant,
            site_id=csod_path.group(1),
            note="Detected ATS; reviewed official attribution and endpoint configuration required",
        )
    if host == "apply.workable.com" and parts:
        return ATSDetection("workable", slug=parts[0], note="Detected ATS; no scanner adapter")
    if host == "ats.rippling.com" and parts and len(parts) >= 2 and parts[1].lower() == "jobs":
        return ATSDetection("rippling", slug=parts[0], note="Detected ATS; no scanner adapter")

    if hostname_matches(host, "amazon.jobs"):
        return ATSDetection("amazon", note="Detected official Amazon Jobs site")
    if hostname_matches(host, "google.com") and "/careers" in parsed.path.lower():
        return ATSDetection("google", note="Detected official Google Careers site")
    if hostname_matches(host, "metacareers.com"):
        return ATSDetection("meta", note="Detected official Meta Careers site")
    if hostname_matches(host, "ibm.com") and "/careers" in parsed.path.lower():
        return ATSDetection("ibm_search", note="Detected official IBM Careers site")
    if hostname_matches(host, "atlassian.com") and "/careers" in parsed.path.lower():
        return ATSDetection("atlassian", note="Detected existing custom adapter")
    if hostname_matches(host, "uber.com") and "/careers" in parsed.path.lower():
        return ATSDetection("uber", note="Detected existing custom adapter")
    if hostname_matches(host, "shopify.com") and "/careers" in parsed.path.lower():
        return ATSDetection("shopify", note="Detected existing custom adapter")

    return ATSDetection(note="Unsupported or ambiguous careers system")


def _slug_note(slug: str) -> str:
    return "Detected ATS slug" if slug else "ATS slug is missing"


def detection_from_registry(entry: Mapping[str, Any]) -> ATSDetection:
    platform = clean_text(entry.get("platform"))
    return ATSDetection(
        platform=platform,
        slug=clean_text(entry.get("slug")),
        tenant=clean_text(entry.get("tenant")),
        wd_subdomain=clean_text(entry.get("wd_subdomain")),
        site_id=clean_text(entry.get("site_id")),
        note="Preserved existing validated configuration",
    )


def registry_source_url(entry: Mapping[str, Any]) -> str:
    detection = detection_from_registry(entry)
    if detection.platform == "greenhouse" and detection.slug:
        return f"https://boards.greenhouse.io/{detection.slug}"
    if detection.platform == "lever" and detection.slug:
        return f"https://jobs.lever.co/{detection.slug}"
    if detection.platform == "ashby" and detection.slug:
        return f"https://jobs.ashbyhq.com/{detection.slug}"
    if detection.platform == "workday" and detection.complete:
        return (
            f"https://{detection.tenant}.{detection.wd_subdomain}.myworkdayjobs.com/"
            f"{detection.site_id}"
        )
    return {
        "amazon": "https://www.amazon.jobs/",
        "google": "https://www.google.com/about/careers/applications/jobs/results/",
        "meta": "https://www.metacareers.com/jobs/",
        "ibm_search": "https://www.ibm.com/careers/search",
        "atlassian": "https://www.atlassian.com/company/careers",
        "uber": "https://www.uber.com/us/en/careers/list/",
        "shopify": "https://www.shopify.com/careers",
    }.get(detection.platform, "")


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry_path = Path(path)
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    entries = data.get("target_companies") or data.get("companies") or []
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ValueError(f"Invalid company registry structure: {registry_path}")
    return data, entries


def registry_by_name(entries: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        key = normalize_company_name(entry.get("name"))
        if not key:
            raise ValueError("Registry contains an entry with no company name")
        if key in result:
            raise ValueError(f"Duplicate company in registry: {entry.get('name')}")
        result[key] = entry
    return result


def read_enriched_csv(path: Path | str) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = [field for field in ENRICHED_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Enriched CSV is missing columns: {', '.join(missing)}")
        return [{key: clean_text(value) for key, value in row.items()} for row in reader]


def read_research_csv(path: Path | str) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = [field for field in RESEARCH_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Research CSV is missing columns: {', '.join(missing)}")
        rows = [{key: clean_text(value) for key, value in row.items()} for row in reader]
    reviewed_names: set[str] = set()
    for row in rows:
        if not parse_bool(row.get("Reviewed")):
            continue
        key = normalize_company_name(row.get("Company"))
        if key in reviewed_names:
            raise ValueError(f"Duplicate reviewed research company: {row.get('Company')}")
        reviewed_names.add(key)
    return rows


def parse_bool(value: Any) -> bool:
    return clean_text(value).casefold() in {"true", "1", "yes", "y"}


def validate_research_row(row: Mapping[str, Any]) -> tuple[bool, str]:
    company = clean_text(row.get("Company"))
    careers_url = clean_text(row.get("Official Careers URL"))
    job_search_url = clean_text(row.get("Official Job Search URL"))
    source_url = clean_text(row.get("Source URL"))
    platform = clean_text(row.get("Detected Platform"))
    status = clean_text(row.get("Research Status"))
    reusable = parse_bool(row.get("Existing Adapter Reusable"))
    required_adapter = clean_text(row.get("Required Adapter"))

    if not normalize_company_name(company):
        return False, "Research company is missing"
    if status not in RESEARCH_STATUSES:
        return False, f"Unsupported research status: {status or 'missing'}"
    if not platform:
        return False, "Detected platform is missing"
    if not required_adapter:
        return False, "Required adapter decision is missing"
    for label, url in (
        ("Official Careers URL", careers_url),
        ("Official Job Search URL", job_search_url),
        ("Source URL", source_url),
    ):
        allowed, note = reviewed_official_url(company, url, platform)
        if not allowed:
            return False, f"{label}: {note}"
    if status == "validated_custom_adapter_required":
        allowed_custom = platform.startswith("custom_") or platform in DETECTED_BUT_UNSUPPORTED | DETECTED_REQUIRES_REVIEW | {"custom"}
        if not allowed_custom:
            return False, "Custom-adapter-required research must identify a custom or unsupported platform"
        if reusable:
            return False, "A required custom adapter cannot be marked reusable"
    if status == "validated_supported" and platform not in SUPPORTED_IMPORT_PLATFORMS:
        return False, f"Platform is not supported by the current scanner: {platform}"
    if status == "validated_supported":
        researched_detection = ATSDetection(
            platform=platform,
            slug=clean_text(row.get("ATS Slug")),
            tenant=clean_text(row.get("Workday Tenant")),
            wd_subdomain=clean_text(row.get("Workday Subdomain")),
            site_id=clean_text(row.get("Workday Site ID")),
        )
        fields_valid, fields_note = validate_detection_fields(researched_detection)
        if not fields_valid:
            return False, fields_note
    return True, "Reviewed official-domain research"


def reviewed_official_url(company: str, url: str, platform: str) -> tuple[bool, str]:
    if not url:
        return False, "URL is missing"
    canonical = canonicalize_url(url)
    parsed = urlsplit(canonical)
    if parsed.scheme != "https" or not parsed.hostname:
        return False, "Only HTTPS URLs with a hostname are allowed"
    if parsed.username or parsed.password:
        return False, "Credentials in URLs are not allowed"
    if prohibited_job_board(canonical):
        return False, "Aggregator and third-party job-board URLs are prohibited"

    host_key = normalize_company_name(parsed.hostname)
    company_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", company).casefold())
        if len(token) >= 3 and token not in {"the", "inc", "corporation", "company", "limited"}
    ]
    company_domain_match = any(token in host_key for token in company_tokens)
    detected = detect_ats(canonical)
    supported_provider_match = bool(platform and detected.platform == platform)
    if not company_domain_match and not supported_provider_match:
        return False, "URL is not on the company domain or its detected supported provider"
    return True, ""


def write_csv(path: Path | str, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: clean_text(row.get(field)) for field in fields})


def validate_public_https_url(url: str, session: Any, timeout: int = 15, max_redirects: int = 5) -> URLValidation:
    """Validate an HTTPS URL while containing redirects and SSRF-prone targets."""
    current = canonicalize_url(url)
    for redirect_count in range(max_redirects + 1):
        safe, note = _safe_public_https_target(current)
        if not safe:
            return URLValidation(False, current, note)
        try:
            response = session.get(
                current,
                allow_redirects=False,
                timeout=timeout,
                headers={"User-Agent": "JobRadarWatchlist/1.0", "Accept": "text/html,*/*;q=0.8"},
                stream=True,
            )
        except Exception as exc:  # requests is intentionally injected for unit-test isolation.
            return URLValidation(False, current, f"Request failed: {exc.__class__.__name__}: {exc}")
        try:
            status = int(response.status_code)
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                if not location:
                    return URLValidation(False, current, f"HTTP {status} redirect had no Location")
                if redirect_count == max_redirects:
                    return URLValidation(False, current, "Too many redirects")
                current = canonicalize_url(urljoin(current, location))
                continue
            if 200 <= status < 300:
                return URLValidation(True, current, f"HTTP {status}")
            return URLValidation(False, current, f"HTTP {status}")
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
    return URLValidation(False, current, "Too many redirects")


def _safe_public_https_target(url: str) -> tuple[bool, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        return False, "Only HTTPS careers URLs are allowed"
    if parsed.username or parsed.password:
        return False, "Credentials in careers URLs are not allowed"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False, "Careers URL has no hostname"
    if prohibited_job_board(url):
        return False, "Third-party job-board URLs are prohibited"
    if host == "localhost" or host.endswith(".local"):
        return False, "Local network targets are not allowed"
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        return False, f"Hostname resolution failed: {exc}"
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False, "Private or non-global network targets are not allowed"
    return True, ""


def validate_detection_fields(detection: ATSDetection) -> tuple[bool, str]:
    if not detection.platform:
        return False, detection.note or "ATS could not be detected"
    if detection.platform in DETECTED_BUT_UNSUPPORTED:
        return False, f"{detection.platform} is detected but has no current scanner adapter"
    if detection.platform in DETECTED_REQUIRES_REVIEW:
        return False, (
            f"{detection.platform} requires reviewed endpoint, identity, apply-host, "
            "and pagination configuration"
        )
    if detection.platform in EXISTING_ONLY_PLATFORMS:
        return False, f"{detection.platform} requires reviewed search terms and is preserved only from existing configurations"
    if detection.platform not in SUPPORTED_IMPORT_PLATFORMS:
        return False, f"Unsupported platform: {detection.platform}"
    if detection.platform in {"greenhouse", "lever", "ashby"} and not detection.slug:
        return False, f"{detection.platform} slug is missing"
    if detection.platform == "workday" and not detection.complete:
        return False, "Workday tenant, subdomain, and site ID are all required"
    return True, "Configuration fields are complete"
